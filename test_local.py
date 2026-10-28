"""
GeM AI Lead Scraper - Local Verification & Diagnostic Script
Tests GeM Portal CSRF AJAX extraction, Category Keyword Mining (including Saree & Event Management),
Serper Free-Tier Failover, and Firebase / Zivic API connectivity.
Run with: python test_local.py
"""

import os
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
for env_path in [
    os.path.join(current_dir, ".env.local"),
    os.path.join(current_dir, ".env"),
    os.path.join(parent_dir, "digitalMarketing", ".env.local"),
]:
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                line = raw_line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        break

from gem_portal_client import GemPortalClient
from search_failover import search_gem_failover
from firebase_sync import FirebaseSyncManager, resolve_vendor_category


def run_diagnostics():
    print("=" * 65)
    print("GeM AI Lead Scraper - Local Diagnostic Test (V2)")
    print("=" * 65)

    client = GemPortalClient(timeout=15)

    # 1. Test GeM Portal Connectivity
    print("\n[1/4] Testing GeM Portal CSRF Session & All-Bids Extraction...")
    bids = client.fetch_all_bids_html(page=1)
    print(f"  [+] Scraped {len(bids)} live ongoing tenders from bidplus.gem.gov.in!")
    if bids:
        sample = bids[0]
        print(f"      - Bid Number: {sample.get('bid_number')}")
        print(f"      - Title:      {sample.get('title')[:70]}")
        print(f"      - Dept:       {sample.get('department')}")
        print(f"      - Expiry:     {sample.get('expiry_date')}")
        print(f"      - Doc URL:    {sample.get('doc_url')}")

    # 2. Test Category Keyword Mining
    print("\n[2/4] Testing Category Keyword Mining ('Saree' & 'Event Management')...")
    saree_bids = client.scrape_keyword_tenders("Saree", category_slug="saree-apparel")
    event_bids = client.scrape_keyword_tenders("Event Management", category_slug="wedding-event-management")
    print(f"  [+] 'Saree' (saree-apparel) active GeM tenders: {len(saree_bids)}")
    print(f"  [+] 'Event Management' (wedding-event-management) active GeM tenders: {len(event_bids)}")

    # 3. Test Search Failover (Serper Free-Tier Safe)
    print("\n[3/4] Testing Search Failover Engine...")
    failover_bids = search_gem_failover("Event Management", category_slug="wedding-event-management")
    print(f"  [+] Failover search extracted: {len(failover_bids)} tenders.")

    # 4. Test Category Resolver & Sync Manager
    print("\n[4/4] Testing Category Resolver & Sync Connectivity...")
    cat_slug, cat_label = resolve_vendor_category({
        "niche": "Banarasi Saree",
        "sub_niche": "Handloom Silk Sarees",
        "industry": "Apparel",
    })
    print(f"  [+] Category Resolver Test ('Banarasi Saree') -> {cat_slug} ({cat_label})")

    sync = FirebaseSyncManager()
    if sync.db:
        print("  [+] Direct Firestore client connected to fir-80-72586!")
        vendors = sync.fetch_subscribed_vendors()
        print(f"  [+] Active GeM vendors in Firestore: {len(vendors)}")
    elif sync.api_base_url and sync.scraper_api_key:
        print(f"  [+] HTTP API Sync Mode ready -> {sync.api_base_url}")
    else:
        print("  [i] Local Python does not have firebase-admin installed or ZIVIC_BASE_URL set.")
        print("      (In GitHub Actions, pip install -r requirements.txt installs firebase-admin automatically!)")

    print("\n" + "=" * 65)
    print("Diagnostic Complete.")
    print("=" * 65)


if __name__ == "__main__":
    run_diagnostics()
