"""
Master GeM Government Tender AI Lead Scraper Engine (V2 — Category-Pooled & Location-Aware)
Orchestrates live scraping from GeM portal (bidplus.gem.gov.in), Serper-safe failover,
Business Category pooling (`__category__:<slug>`), vendor location prioritization,
and atomic Firestore updates preserving earliest `discovered_at` timestamps for Lead Delivery Speed gating.
"""

import os
import re
import sys
import time
import logging
from typing import List, Dict, Any
from collections import defaultdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("GeMLeadScraper")


def _load_env_file_fallback(filepath: str) -> None:
    """Zero-dependency .env parser fallback if python-dotenv is not installed."""
    try:
        from dotenv import load_dotenv
        load_dotenv(filepath)
        return
    except ImportError:
        pass

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)

for env_path in [
    os.path.join(current_dir, ".env.local"),
    os.path.join(current_dir, ".env"),
    os.path.join(parent_dir, "digitalMarketing", ".env.local"),
    os.path.join(parent_dir, ".env.local"),
]:
    if os.path.exists(env_path):
        _load_env_file_fallback(env_path)
        logger.info(f"Loaded environment variables from: {env_path}")
        break

from gem_portal_client import GemPortalClient, detect_indian_location
from search_failover import search_gem_failover
from firebase_sync import (
    FirebaseSyncManager,
    CANONICAL_CATEGORY_BUCKETS,
    get_gem_queries_for_category,
    classify_tender,
)


# Core baseline categories always kept warm so newly upgrading Premium/Titan/Elite vendors see tenders immediately
DEFAULT_WARM_CATEGORY_SLUGS = [
    "saree-apparel",
    "wedding-event-management",
    "digital-marketing-seo",
    "web-app-development",
    "catering-hospitality",
    "medical-oxygen-equipment",
    "water-treatment-etp-stp",
]


def _extract_vendor_location_hints(vendors: List[Dict[str, Any]]) -> List[str]:
    """Extracts unique Indian states/cities from subscribed vendors' physical addresses."""
    hints: List[str] = []
    for v in vendors:
        addr = str(v.get("address") or "").strip()
        if not addr:
            continue
        state, city = detect_indian_location(addr)
        if city and city not in hints:
            hints.append(city)
        if state and state != "National / Central" and state not in hints:
            hints.append(state)
    return hints[:2]


def run_scraper(dry_run: bool = False):
    logger.info("=" * 68)
    logger.info("Starting GeM AI Lead Finder — Category-Pooled Tender Scraper")
    logger.info("Target: Government e-Marketplace (bidplus.gem.gov.in)")
    logger.info(f"Mode: {'DRY RUN (No database writes)' if dry_run else 'LIVE PRODUCTION SYNC'}")
    logger.info("=" * 68)

    portal_client = GemPortalClient()
    sync_manager = FirebaseSyncManager()

    # Step 1: Discover Active Subscribed / AI-Enabled Vendors & Group by Business Category
    vendors = sync_manager.fetch_subscribed_vendors()
    logger.info(f"Discovered {len(vendors)} active vendors for GeM AI Lead Finder.")

    vendors_by_category: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    category_labels: Dict[str, str] = {
        b["slug"]: b["label"] for b in CANONICAL_CATEGORY_BUCKETS
    }

    for v in vendors:
        c_slug = v.get("category_slug") or "general-marketplace"
        c_label = v.get("category_label") or "General Marketplace"
        vendors_by_category[c_slug].append(v)
        category_labels[c_slug] = c_label

    # Ensure core marketplace categories stay warm in Firestore category pools
    active_category_slugs = list(vendors_by_category.keys())
    for warm_slug in DEFAULT_WARM_CATEGORY_SLUGS:
        if warm_slug not in active_category_slugs:
            active_category_slugs.append(warm_slug)

    logger.info(f"Target Business Categories ({len(active_category_slugs)}): {active_category_slugs}")

    all_tenders_by_id: Dict[str, Dict[str, Any]] = {}
    tenders_by_category: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)

    # Step 2: Scrape GeM Portal per Business Category (+ Vendor Location Hints)
    logger.info("\n--- Phase 1: Category & Location-Aware GeM Tender Mining ---")
    for cat_slug in active_category_slugs[:12]:
        cat_label = category_labels.get(cat_slug, cat_slug.replace("-", " ").title())
        cat_vendors = vendors_by_category.get(cat_slug, [])
        queries = get_gem_queries_for_category(cat_slug, cat_label, cat_vendors)
        loc_hints = _extract_vendor_location_hints(cat_vendors)

        logger.info(
            f"[Category: {cat_label} ({cat_slug})] Vendors={len(cat_vendors)} | Queries={queries} | Locations={loc_hints}"
        )

        # Build prioritized search list: location-specific query first (if vendor has address), then national category queries
        effective_queries: List[str] = []
        if loc_hints and queries:
            effective_queries.append(f"{queries[0]} {loc_hints[0]}")
        effective_queries.extend(queries)

        for kw in effective_queries[:4]:
            try:
                kw_bids = portal_client.scrape_keyword_tenders(
                    keyword=kw,
                    category_slug=cat_slug,
                    max_pages=1,
                )
                if not kw_bids:
                    kw_bids = search_gem_failover(
                        keyword=kw,
                        category_slug=cat_slug,
                        location_hint=loc_hints[0] if loc_hints else None,
                    )

                for b in kw_bids:
                    bid_id = b.get("bid_id")
                    if not bid_id:
                        continue
                    tenders_by_category[cat_slug][bid_id] = b
                    all_tenders_by_id[bid_id] = b

                time.sleep(0.8)
            except Exception as e:
                logger.warning(f"Error mining GeM category '{cat_slug}' keyword '{kw}': {e}")

    # Step 3: General Latest All-Bids Sweep (catches broad multi-item tenders)
    logger.info("\n--- Phase 2: General Latest GeM All-Bids Sweep ---")
    try:
        general_bids = portal_client.fetch_all_bids_ajax(keyword="", page=1)
        for b in general_bids:
            bid_id = b.get("bid_id")
            if not bid_id:
                continue
            if not b.get("matched_category_slug"):
                inferred_cat = classify_tender(b)
                if inferred_cat:
                    b["matched_category_slug"] = inferred_cat
            cat_slug = b.get("matched_category_slug")
            if cat_slug:
                tenders_by_category[cat_slug][bid_id] = b
            if bid_id not in all_tenders_by_id:
                all_tenders_by_id[bid_id] = b
    except Exception as e:
        logger.warning(f"General all-bids sweep warning: {e}")

    total_tenders = list(all_tenders_by_id.values())
    logger.info(f"\n[+] Total unique live GeM procurement tenders collected: {len(total_tenders)}")

    if not total_tenders:
        logger.warning("[!] No live tenders could be extracted in this run. Exiting safely.")
        return

    if dry_run:
        logger.info("[Dry Run] Skipping database writes. Sample extracted tenders:")
        for sample in total_tenders[:3]:
            print(f"  - [{sample.get('matched_category_slug') or 'general'}] {sample.get('bid_number')} | Score: {sample.get('match_score')}/100")
            print(f"    Title: {sample.get('title')[:90]}")
            print(f"    Dept:  {sample.get('department')} | Loc: {sample.get('city') or sample.get('state')}")
            print(f"    Doc:   {sample.get('doc_url')} | Closes: {sample.get('expiry_date')}")
        return

    # Step 4: Sync Shared Category Pools (`__category__:<slug>`) & Subscribed Category Vendors
    logger.info("\n--- Phase 3: Category Pool & Subscribed Vendor Sync ---")
    total_synced = 0
    total_pruned = 0

    for cat_slug, bid_map in tenders_by_category.items():
        pool_tenders = list(bid_map.values())
        if not pool_tenders:
            continue
        pool_doc_id = f"__category__:{cat_slug}"
        cat_label = category_labels.get(cat_slug, cat_slug)
        res = sync_manager.sync_target_inquiries(
            target_doc_id=pool_doc_id,
            new_inquiries=pool_tenders,
            category_slug=cat_slug,
            category_label=cat_label,
        )
        total_synced += res.get("synced", 0)
        total_pruned += res.get("pruned", 0)

    for vendor in vendors:
        vendor_id = vendor.get("vendor_id")
        if not vendor_id:
            continue
        c_slug = vendor.get("category_slug") or "general-marketplace"
        c_label = vendor.get("category_label") or "General Marketplace"
        business_name = vendor.get("business_name") or vendor.get("email") or vendor_id

        # Combine category pool tenders + general sweep matching this category for vendor-specific address scoring
        candidate_pool = list(tenders_by_category.get(c_slug, {}).values())
        for t in total_tenders:
            if t.get("matched_category_slug") == c_slug:
                candidate_pool.append(t)
        dedup_candidates = list({t["bid_id"]: t for t in candidate_pool if t.get("bid_id")}.values())
        matched_tenders = sync_manager.match_tenders_to_vendor(vendor, dedup_candidates)

        logger.info(
            f"Vendor '{business_name}' ({vendor_id}) [Category: {c_label}]: Matched {len(matched_tenders)} tenders."
        )
        # Always sync for each vendor so that when a vendor updates niche, stale inquiries are purged even if 0 new tenders match today!
        res = sync_manager.sync_target_inquiries(
            target_doc_id=vendor_id,
            new_inquiries=matched_tenders,
            category_slug=c_slug,
            category_label=c_label,
        )
        total_synced += res.get("synced", 0)
        total_pruned += res.get("pruned", 0)


    logger.info("=" * 68)
    logger.info("GeM AI Lead Finder Scraper Job Completed Successfully!")
    logger.info(f"Unique Live Tenders Extracted : {len(total_tenders)}")
    logger.info(f"Category Pools Updated        : {len(tenders_by_category)}")
    logger.info(f"Total Inquiry Records Synced  : {total_synced}")
    logger.info(f"Expired Tenders Auto-Pruned   : {total_pruned}")
    logger.info("=" * 68)


if __name__ == "__main__":
    is_dry = "--dry-run" in sys.argv
    run_scraper(dry_run=is_dry)
