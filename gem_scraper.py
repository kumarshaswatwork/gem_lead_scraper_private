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
import argparse
import logging
from typing import List, Dict, Any, Optional
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

from gem_portal_client import (
    GemPortalClient,
    detect_indian_location,
    tender_matches_keyword,
    _split_combined_keyword_location,
    STATE_TO_SWEEP_HUBS,
)
from search_failover import search_gem_failover, is_paid_serp_disabled
from free_tender_sources import fetch_gepnic_tenders_for_target, fetch_rss_tender_leads
from firebase_sync import (
    FirebaseSyncManager,
    CANONICAL_CATEGORY_BUCKETS,
    get_gem_queries_for_category,
    classify_tender,
    resolve_vendor_category,
    _vertical_family,
    _has_excluded_tender_noise,
)


# Core baseline categories always kept warm so newly upgrading Premium/Titan/Elite vendors see tenders immediately
DEFAULT_WARM_CATEGORY_SLUGS = [
    "saree-apparel",
    "wedding-event-management",
    "digital-marketing-seo",
    "web-app-development",
    "catering-hospitality",
    "real-estate",
    "paying-guest",
    "gem-services",
    "gst-services",
    "medical-oxygen-equipment",
    "water-treatment-etp-stp",
    "waste-management",
    "doctors-medical-consultants",
    "coaching",
]

UP_SWEEP_KEYWORDS = [
    "Uttar Pradesh", "Lucknow", "Noida", "Kanpur", "Varanasi", "Greater Noida", "Ghaziabad", "Prayagraj",
]

PAN_INDIA_SWEEP_KEYWORDS = [
    "India", "Delhi", "Mumbai", "Bengaluru", "Hyderabad", "Pune", "Uttar Pradesh", "Maharashtra",
]

# Strict 15-Minute Global Execution Timer (14-min safety break before timeout)
GLOBAL_TIMEOUT_SECONDS = 14 * 60


def build_regional_sweep_keywords(location_hint: Optional[str] = None) -> List[str]:
    """Builds GeM regional sweep keywords dynamically for Pan-India, any Indian state, or any city."""
    raw = (location_hint or "").strip()
    if not raw or raw.lower() in ("india", "pan india", "pan-india", "all india"):
        return list(PAN_INDIA_SWEEP_KEYWORDS)
    st, city = detect_indian_location(raw)
    if st != "National / Central":
        hubs = list(STATE_TO_SWEEP_HUBS.get(st, [st]))
        if city and city not in hubs:
            hubs.insert(0, city)
        return hubs[:8]
    return [raw]


def _extract_vendor_location_hints(vendors: List[Dict[str, Any]], location_override: Optional[str] = None) -> List[str]:
    """Extracts unique Indian states/cities from override or subscribed vendors' physical addresses."""
    if location_override and location_override.strip():
        raw = location_override.strip()
        if raw.lower() in ("india", "pan india", "pan-india", "all india"):
            return ["India"]
        st, city = detect_indian_location(raw)
        hints = []
        if city:
            hints.append(city)
        if st != "National / Central" and st not in hints:
            hints.append(st)
        return hints or [raw]

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


def _count_target_location_hits(bids: List[Dict[str, Any]], target_location: Optional[str]) -> int:
    raw = (target_location or "").strip()
    if not raw or raw.lower() in ("india", "pan india", "pan-india", "all india"):
        return len(bids)
    t_state, t_city = detect_indian_location(raw)
    if t_state == "National / Central" and not t_city:
        return sum(1 for b in bids if raw.lower() in f"{b.get('state','')} {b.get('city','')} {b.get('department','')} {b.get('title','')}".lower())
    hits = 0
    for b in bids:
        b_state = str(b.get("state") or "")
        b_city = str(b.get("city") or "")
        if t_city and b_city.lower() == t_city.lower():
            hits += 1
        elif t_state != "National / Central" and b_state.lower() == t_state.lower():
            hits += 1
    return hits


def discover_category_tenders(
    portal_client: GemPortalClient,
    category_slug: str,
    category_label: str,
    vendors_in_cat: List[Dict[str, Any]],
    start_time: float = None,
    global_timeout: int = GLOBAL_TIMEOUT_SECONDS,
    max_pages: int = 2,
    run_failover: bool = True,
    location_hint: Optional[str] = None,
    days_window: int = 30,
    free_only: bool = False,
    enable_ddgs: bool = False,
    use_ai: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """
    Free-First Tender Mining Pipeline:
      1. Direct GeM AJAX (`Bid-Start-Date-Latest`) across category keywords + combined `<kw> <location>` queries
      2. Direct Free GePNIC Public Portals (`etender.up.nic.in`, `eprocure.gov.in`, `etenders.gov.in`, state portals)
      3. Free Civic/Govt RSS Feeds (Bing News RSS + Google News RSS) when still sparse
      4. Quota-capped Open-Web Failover (`search_gem_failover`) ONLY if free channels yield < 3 target hits
         and `run_failover=True` and paid SERP is not disabled!
    """
    loc_hints = _extract_vendor_location_hints(vendors_in_cat, location_override=location_hint)
    primary_loc = loc_hints[0] if loc_hints else (location_hint or "India")
    queries = get_gem_queries_for_category(
        category_slug,
        category_label,
        vendors_in_cat,
        location_hint=primary_loc,
        use_ai=use_ai,
    )
    found: Dict[str, Dict[str, Any]] = {}

    # Build GeM AJAX search list: clean service queries + crisp combined "<root_kw> <location>" queries when state/city targeted
    gem_queries = list(queries[:4])
    t_state_target, t_city = detect_indian_location(primary_loc or "India")
    if primary_loc and primary_loc.lower() not in ("india", "pan india", "pan-india"):
        t_st = t_state_target
        loc_suffix = t_st if t_st != "National / Central" else primary_loc
        q0 = queries[0] if queries else category_label
        q0_low = q0.lower()
        q0_first = q0.split()[0] if q0.split() else q0
        if "waste" in q0_low:
            root_kw = "Waste"
        elif any(d in q0_low for d in ("doctor", "medical")):
            root_kw = "Doctor"
        elif any(c in q0_low for c in ("skill training", "coaching", "capacity")):
            root_kw = "Training"
        elif "saree" in q0_low or "sari" in q0_low:
            root_kw = "Saree"
        elif len(q0.split()) >= 2 and len(q0_first) >= 4 and q0_first.lower() not in ("cold", "hot", "air", "raw", "high"):
            root_kw = q0_first
        else:
            root_kw = q0
        comb_cands = [f"{root_kw} {loc_suffix}"]
        if t_city and t_city.lower() != loc_suffix.lower():
            comb_cands.append(f"{root_kw} {t_city}")
        if q0 and q0.lower() != root_kw.lower():
            comb_cands.append(f"{q0} {loc_suffix}")
        for comb in comb_cands:
            if comb and comb not in gem_queries:
                gem_queries.append(comb)

    logger.info(
        f"[Category: {category_label} ({category_slug})] Vendors={len(vendors_in_cat)} | Target={primary_loc} | Window={days_window}d | Queries={gem_queries}"
    )

    # 1. Free Direct GeM Portal AJAX (`Bid-Start-Date-Latest`)
    for kw in gem_queries[:7]:
        if start_time and (time.time() - start_time > global_timeout):
            break
        try:
            kw_bids = portal_client.scrape_keyword_tenders(
                keyword=kw,
                category_slug=category_slug,
                max_pages=max_pages,
                sort="Bid-Start-Date-Latest",
                days_window=days_window,
            )
            for b in kw_bids:
                bid_id = b.get("bid_id")
                t_title = str(b.get("title") or "")
                t_check = f"{t_title} {b.get('category_name', '')}".lower()
                if not bid_id or _has_excluded_tender_noise(t_check, category_slug):
                    continue
                inferred_cat = classify_tender(b)
                _, loc_suf = _split_combined_keyword_location(kw)
                kw_dept = b.get("department", "") if loc_suf else ""
                if inferred_cat != category_slug and not tender_matches_keyword(t_title, kw_dept, kw):
                    continue
                if t_state_target != "National / Central" and b.get("state") not in (t_state_target, "National / Central", "", None):
                    continue
                if bid_id not in found:
                    found[bid_id] = b
            time.sleep(0.4)
        except Exception as e:
            logger.warning(f"Error mining GeM category '{category_slug}' keyword '{kw}': {e}")

    # 2. Free Direct GePNIC Active Tender Portals (CPPP / eTenders / State eTender)
    if not (start_time and (time.time() - start_time > global_timeout)):
        try:
            gepnic_bids = fetch_gepnic_tenders_for_target(
                keywords=queries[:4],
                category_slug=category_slug,
                location_hint=primary_loc,
            )
            for b in gepnic_bids:
                bid_id = b.get("bid_id")
                if bid_id and bid_id not in found:
                    found[bid_id] = b
        except Exception as e:
            logger.warning(f"GePNIC free portal warning for '{category_slug}': {e}")

    # 3. Free Civic/Municipal RSS Tender Discovery if target hits < 3
    target_hits = _count_target_location_hits(list(found.values()), primary_loc)
    if (len(found) == 0 or target_hits < 3) and not (start_time and (time.time() - start_time > global_timeout)):
        for kw in queries[:2]:
            try:
                rss_bids = fetch_rss_tender_leads(
                    keyword=kw,
                    category_slug=category_slug,
                    location_hint=primary_loc,
                    days_window=days_window,
                )
                for b in rss_bids:
                    bid_id = b.get("bid_id")
                    if bid_id and bid_id not in found:
                        found[bid_id] = b
            except Exception as e:
                logger.debug(f"Free RSS warning for '{category_slug}': {e}")

    # 4. Last-Resort Open-Web SERP Failover (ONLY if enabled, not free_only, and still < 3 target hits)
    target_hits = _count_target_location_hits(list(found.values()), primary_loc)
    if run_failover and not is_paid_serp_disabled(free_only) and (len(found) == 0 or target_hits < 3):
        for kw in queries[:2]:
            if start_time and (time.time() - start_time > global_timeout):
                break
            try:
                fo_bids = search_gem_failover(
                    keyword=kw,
                    category_slug=category_slug,
                    location_hint=primary_loc,
                    days_window=days_window,
                    free_only=free_only,
                    enable_ddgs=enable_ddgs,
                )
                for b in fo_bids:
                    bid_id = b.get("bid_id")
                    t_check = f"{b.get('title', '')} {b.get('category_name', '')}".lower()
                    if not bid_id or _has_excluded_tender_noise(t_check, category_slug):
                        continue
                    if bid_id not in found:
                        found[bid_id] = b
            except Exception as e:
                logger.warning(f"Failover warning for '{category_slug}' ({kw}): {e}")

    return found


def scrape_regional_sweep(
    portal_client: GemPortalClient,
    keywords: Optional[List[str]] = None,
    location_hint: Optional[str] = None,
    days_window: int = 30,
    start_time: float = None,
    global_timeout: int = GLOBAL_TIMEOUT_SECONDS,
) -> List[Dict[str, Any]]:
    """
    Runs a dynamic regional & national GeM ongoing bids sweep (ordered newest start date first),
    classifying every bid purely by work item title (`classify_tender`).
    """
    sweep_kws = keywords or build_regional_sweep_keywords(location_hint)
    collected: Dict[str, Dict[str, Any]] = {}
    for kw in sweep_kws:
        if start_time and (time.time() - start_time > global_timeout):
            break
        try:
            bids = portal_client.scrape_keyword_tenders(
                keyword=kw,
                category_slug=None,
                max_pages=2,
                sort="Bid-Start-Date-Latest",
                days_window=days_window,
            )
            for b in bids:
                bid_id = b.get("bid_id")
                if not bid_id or bid_id in collected:
                    continue
                if not b.get("matched_category_slug"):
                    inferred = classify_tender(b)
                    if inferred:
                        b["matched_category_slug"] = inferred
                collected[bid_id] = b
            time.sleep(0.35)
        except Exception as e:
            logger.warning(f"Regional sweep warning ({kw}): {e}")
    return list(collected.values())


def scrape_up_regional_sweep(
    portal_client: GemPortalClient,
    keywords: List[str] = None,
    start_time: float = None,
    global_timeout: int = GLOBAL_TIMEOUT_SECONDS,
) -> List[Dict[str, Any]]:
    """Backwards-compatible alias for UP regional GeM sweep."""
    return scrape_regional_sweep(
        portal_client=portal_client,
        keywords=keywords or UP_SWEEP_KEYWORDS,
        location_hint="Uttar Pradesh",
        start_time=start_time,
        global_timeout=global_timeout,
    )


def run_scraper(
    dry_run: bool = False,
    category: Optional[str] = None,
    location: Optional[str] = None,
    days_window: Optional[int] = None,
    free_only: bool = False,
    enable_ddgs: bool = False,
    use_ai: bool = True,
):
    start_time = time.time()
    effective_days = int(days_window or os.environ.get("SCRAPE_DAYS_WINDOW") or 30)
    effective_free_only = is_paid_serp_disabled(free_only)
    target_loc = (location or "").strip() or "India"

    logger.info("=" * 68)
    logger.info("Starting GeM AI Lead Finder — Free-First Category-Pooled Tender Scraper (V4)")
    logger.info("Target: GeM (bidplus.gem.gov.in), GePNIC (eProcure/eTenders/State), Civic RSS")
    logger.info(f"Execution Budget: 15 Minutes | Window: {effective_days}d | Location: {target_loc} | Free-Only: {effective_free_only}")
    logger.info(f"Mode: {'DRY RUN (No database writes)' if dry_run else 'LIVE PRODUCTION SYNC'}")
    logger.info("=" * 68)

    portal_client = GemPortalClient()
    sync_manager = FirebaseSyncManager()

    category_labels: Dict[str, str] = {
        b["slug"]: b["label"] for b in CANONICAL_CATEGORY_BUCKETS
    }
    vendors_by_category: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    if category:
        clean_cat = category.strip()
        c_slug, c_label = resolve_vendor_category({"niche": clean_cat, "sub_niche": clean_cat})
        category_labels[c_slug] = c_label
        vendors = []
        vendors_by_category[c_slug] = [{"niche": clean_cat, "sub_niche": clean_cat}]
        active_category_slugs = [c_slug]
    else:
        # Step 1: Discover Active Subscribed / AI-Enabled Vendors & Group by Business Category
        vendors = sync_manager.fetch_subscribed_vendors()
        logger.info(f"Discovered {len(vendors)} active vendors for GeM AI Lead Finder.")
        for v in vendors:
            c_slug = v.get("category_slug") or "general-marketplace"
            c_label = v.get("category_label") or "General Marketplace"
            vendors_by_category[c_slug].append(v)
            category_labels[c_slug] = c_label

        active_category_slugs = list(vendors_by_category.keys())
        for warm_slug in DEFAULT_WARM_CATEGORY_SLUGS:
            if warm_slug not in active_category_slugs:
                active_category_slugs.append(warm_slug)

    logger.info(f"Target Business Categories ({len(active_category_slugs)}): {active_category_slugs}")

    all_tenders_by_id: Dict[str, Dict[str, Any]] = {}
    tenders_by_category: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)

    # Step 2: Run Regional GeM Sweep + General All-Bids Sweep FIRST (100% Free, feeds all categories!)
    logger.info(f"\n--- Phase 1: Free Regional & General GeM Tender Sweeps ({target_loc} | {effective_days}d) ---")
    timed_out = False
    reg_bids = scrape_regional_sweep(
        portal_client,
        location_hint=location,
        days_window=effective_days,
        start_time=start_time,
        global_timeout=GLOBAL_TIMEOUT_SECONDS,
    )
    logger.info(f"[Regional Sweep] Collected {len(reg_bids)} active bids from GeM.")
    for b in reg_bids:
        bid_id = b.get("bid_id")
        if not bid_id:
            continue
        c_match = b.get("matched_category_slug")
        if c_match:
            tenders_by_category[c_match][bid_id] = b
            inf_fam = _vertical_family(c_match)
            if inf_fam:
                for active_slug in active_category_slugs:
                    if _vertical_family(active_slug) == inf_fam:
                        tenders_by_category[active_slug][bid_id] = b
        if bid_id not in all_tenders_by_id:
            all_tenders_by_id[bid_id] = b

    if time.time() - start_time < GLOBAL_TIMEOUT_SECONDS:
        try:
            general_bids = portal_client.fetch_all_bids_ajax(
                keyword="", page=1, sort="Bid-Start-Date-Latest", days_window=effective_days
            )
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

    # Step 3: Per-Category Free GeM + GePNIC + Civic RSS + Optional Last-Resort SERP Failover
    logger.info("\n--- Phase 2: Free-First Category Tender Mining (GeM + GePNIC + RSS -> Last-Resort Failover) ---")
    for cat_slug in active_category_slugs[:14]:
        if time.time() - start_time > GLOBAL_TIMEOUT_SECONDS:
            logger.warning("[Timer] 14-minute safety execution budget reached. Halting discovery to ensure database persistence within 15 minutes.")
            timed_out = True
            break

        cat_label = category_labels.get(cat_slug, cat_slug.replace("-", " ").title())
        cat_vendors = vendors_by_category.get(cat_slug, [])
        existing_cat_count = len(tenders_by_category.get(cat_slug, {}))
        cat_bids = discover_category_tenders(
            portal_client=portal_client,
            category_slug=cat_slug,
            category_label=cat_label,
            vendors_in_cat=cat_vendors,
            start_time=start_time,
            global_timeout=GLOBAL_TIMEOUT_SECONDS,
            max_pages=2,
            run_failover=(not effective_free_only and existing_cat_count < 3),
            location_hint=location,
            days_window=effective_days,
            free_only=effective_free_only,
            enable_ddgs=enable_ddgs,
            use_ai=use_ai,
        )
        for bid_id, b in cat_bids.items():
            tenders_by_category[cat_slug][bid_id] = b
            all_tenders_by_id[bid_id] = b

    if category and len(active_category_slugs) == 1:
        c_only = active_category_slugs[0]
        c_label = category_labels.get(c_only, c_only.replace("-", " ").title())
        dyn_queries = get_gem_queries_for_category(c_only, c_label, vendors_by_category.get(c_only, []), location_hint=location, use_ai=use_ai)
        for bid_id, b in all_tenders_by_id.items():
            if bid_id in tenders_by_category[c_only]:
                continue
            t_title = str(b.get("title") or "")
            t_check = f"{t_title} {b.get('category_name', '')}".lower()
            if _has_excluded_tender_noise(t_check, c_only):
                continue
            if b.get("matched_category_slug") == c_only or any(
                tender_matches_keyword(t_title, b.get("department", ""), q) for q in dyn_queries[:4] if q
            ):
                b_copy = dict(b)
                b_copy["matched_category_slug"] = c_only
                tenders_by_category[c_only][bid_id] = b_copy
        raw_cat_list = list(tenders_by_category.get(c_only, {}).values())
        if location and location.strip().lower() not in ("india", "pan india", "pan-india", "all india"):
            t_state, t_city = detect_indian_location(location)
            if t_state != "National / Central":
                raw_cat_list = [
                    b for b in raw_cat_list
                    if b.get("state") in (t_state, "National / Central", "", None)
                ]
                raw_cat_list.sort(
                    key=lambda b: (
                        0 if (t_city and str(b.get("city") or "").lower() == t_city.lower()) else (1 if b.get("state") == t_state else 2),
                        -int(b.get("match_score") or 0),
                    )
                )
        total_tenders = raw_cat_list
    else:
        total_tenders = list(all_tenders_by_id.values())

    logger.info(f"\n[+] Total unique live GeM/Govt procurement tenders collected: {len(total_tenders)}")

    if not total_tenders:
        logger.warning("[!] No live tenders could be extracted in this run. Exiting safely.")
        return

    if dry_run:
        logger.info("[Dry Run] Skipping database writes. Sample extracted tenders:")
        for sample in total_tenders[:5]:
            print(f"  - [{sample.get('matched_category_slug') or 'general'}] {sample.get('bid_number')} | Score: {sample.get('match_score')}/100 | Source: {sample.get('source') or 'GeM AJAX'}")
            print(f"    Title: {str(sample.get('title'))[:95]}")
            print(f"    Dept:  {sample.get('department')} | Loc: {sample.get('city') or sample.get('state')}")
            print(f"    Doc:   {sample.get('doc_url')} | Closes: {sample.get('expiry_date')}")
        return

    # Step 4: Sync Shared Category Pools (`__category__:<slug>`) & Subscribed Category Vendors
    logger.info("\n--- Phase 3: Category Pool & Subscribed Vendor Sync (Firestore & COMBLOPEZ) ---")
    total_synced = 0
    total_pruned = 0
    total_comblopez_synced = 0

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

        comb_count = sync_manager.sync_to_comblopez(
            target_key=pool_doc_id,
            tenders=pool_tenders,
            category_slug=cat_slug,
            category_label=cat_label,
        )
        total_comblopez_synced += comb_count

    for vendor in vendors:
        vendor_id = vendor.get("vendor_id")
        if not vendor_id:
            continue
        c_slug = vendor.get("category_slug") or "general-marketplace"
        c_label = vendor.get("category_label") or "General Marketplace"
        business_name = vendor.get("business_name") or vendor.get("email") or vendor_id
        vendor_email = str(vendor.get("email") or "").strip().lower()

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

        # Sync to COMBLOPEZ scraped_leads under vendor_id
        comb_count1 = sync_manager.sync_to_comblopez(
            target_key=str(vendor_id),
            tenders=matched_tenders,
            category_slug=c_slug,
            category_label=c_label,
        )
        total_comblopez_synced += comb_count1

        # Also sync under vendor_email if present and distinct (for instant dashboard email lookup)
        if vendor_email and vendor_email != str(vendor_id).lower():
            comb_count2 = sync_manager.sync_to_comblopez(
                target_key=vendor_email,
                tenders=matched_tenders,
                category_slug=c_slug,
                category_label=c_label,
            )
            total_comblopez_synced += comb_count2

    elapsed_seconds = int(time.time() - start_time)
    logger.info("=" * 68)
    logger.info(f"GeM AI Lead Finder Scraper Job Completed in {elapsed_seconds // 60}m {elapsed_seconds % 60}s!")
    logger.info(f"Unique Live Tenders Extracted : {len(total_tenders)}")
    logger.info(f"Category Pools Updated        : {len(tenders_by_category)}")
    logger.info(f"Firestore Inquiries Synced    : {total_synced}")
    logger.info(f"COMBLOPEZ Leads Synced        : {total_comblopez_synced}")
    logger.info(f"Expired Tenders Auto-Pruned   : {total_pruned}")
    logger.info("=" * 68)


def parse_cli_args(argv=None):
    parser = argparse.ArgumentParser(description="Zivic Nexus Free-First GeM & Government Tender Scraper")
    parser.add_argument("--dry-run", action="store_true", help="Scrape without database writes")
    parser.add_argument("--category", type=str, default=None, help="Arbitrary business category (e.g. 'Waste Management', 'Doctors', 'Coaching')")
    parser.add_argument("--location", type=str, default=None, help="Target location ('India', 'Uttar Pradesh', 'Maharashtra', 'Lucknow', etc.)")
    parser.add_argument("--days", type=int, default=None, help="Freshness window in days (e.g. 7, 30)")
    parser.add_argument("--free-only", action="store_true", help="Use ONLY free direct portals & RSS (0 paid SERP)")
    parser.add_argument("--enable-ddgs", action="store_true", help="Allow optional local-only DuckDuckGo ddgs fallback (auto-skipped on GitHub Actions)")
    parser.add_argument("--no-ai", action="store_true", help="Disable KRSW AI query expansion for unseen categories")
    parser.add_argument("--test", action="store_true", help="Run offline unit tests and exit")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_cli_args()
    if args.test:
        import test_local
        test_local.run_unit_guards()
        sys.exit(0)
    run_scraper(
        dry_run=args.dry_run,
        category=args.category,
        location=args.location,
        days_window=args.days,
        free_only=args.free_only,
        enable_ddgs=args.enable_ddgs,
        use_ai=not args.no_ai,
    )
