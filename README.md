# GeM Government Tender Radar — Automated AI Lead Scraper (V2)

Automated category-pooled government procurement intelligence bot for **Zivic Nexus** (`/gem`). Continuously scans the **Government e-Marketplace (GeM India)** portal (`bidplus.gem.gov.in`), groups active vendors by **Business Category** + **Business Address/Location**, syncs live tenders into **Firebase Firestore (`fir-80-72586`)**, and preserves earliest `discovered_at` timestamps so Zivic Nexus enforces each vendor's subscription **Lead Delivery Speed** (`Instant`, `10 min`, `25 min`, `1 hr`, `24h`).

---

## 🌟 Key Capabilities

1. **Official GeM CSRF Session & Solr Extraction (`gem_portal_client.py`)**:
   - Negotiates `csrf_gem_cookie` / `csrf_bd_gem_nk` on `bidplus.gem.gov.in/all-bids` and queries `POST /all-bids-data` for ongoing procurement tenders.
   - Extracts Bid Number (`GEM/2026/B/...`), clean deduplicated Item Title, Ministry/Department, Quantity, State & City, Closing Deadline (`expiry_date`), calibrated AI Match Score (`65–98`), and official BOQ Document URL (`showbidDocument/{id}`).

2. **Category-Pooled & Location-Aware Discovery (`firebase_sync.py` & `gem_scraper.py`)**:
   - Resolves every subscribed / AI-enabled vendor to their canonical **Business Category** (`saree-apparel`, `wedding-event-management`, `catering-hospitality`, `digital-marketing-seo`, `web-app-development`, `medical-oxygen-equipment`, etc.), matching `lib/leads/category-lead-routing.ts` in Zivic Nexus 1-to-1.
   - Reads the vendor's physical address (`YOUNG` `vendor_profiles.address`) to prioritize state/city government tenders first.
   - Publishes tenders to both the shared category pool (`gem_vendor_feeds/__category__:{category_slug}`) and matching vendor feeds (`gem_vendor_feeds/{vendor_id}`).

3. **Lead Delivery Speed Timestamp Preservation**:
   - Preserves the earliest `found_date` / `discovered_at` whenever an existing `bid_id` is re-seen across 4-hour cron runs so plan delivery speed timers never reset.

4. **Serper Free-Tier Compatible Search Failover (`search_failover.py`)**:
   - Avoids `site:` and domain-dot operators that trigger HTTP 400 on Serper Free Tier, providing resilient fallback tender discovery via Serper.dev, Google CSE, ScraperAPI, or Apify.

5. **Surgical Auto-Pruning**:
   - Automatically deletes expired tenders (`expiry_date < now`) on every run.

---

## ⚙️ GitHub Actions Setup & Secrets

Upload all files in this directory (including `.github/workflows/gem_scraper.yml`) to your GitHub repository, then go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret Name | Value Description | Mandatory |
| :--- | :--- | :---: |
| `FIREBASE_SERVICE_ACCOUNT_KEY` | Raw JSON content of `fir-80-72586-firebase-adminsdk-*.json` | **YES** (or use `ZIVIC_BASE_URL` + `SCRAPER_API_KEY`) |
| `ZIVIC_BASE_URL` | `https://zivicnexus.com` | Recommended |
| `SCRAPER_API_KEY` | Value from `SCRAPER_API_KEYS` in `.env.local` | Recommended |
| `SERPERDEV_API` | Serper.dev API key(s) for fallback search | Optional |
| `NEXT_PUBLIC_SUPABASE_URL` | YOUNG Supabase URL (for vendor physical address lookup) | Optional |
| `SUPABASE_SERVICE_ROLE_KEY` | YOUNG Supabase service role key | Optional |
| `SENSHRADDHA_SUPABASE_PROJECT2_URL` | SENSHRADDHA Supabase URL (for diagnostic profile lookup) | Optional |
| `SENSHRADDHA_SUPABASE_PROJECT2_SERVICE_ROLE_KEY` | SENSHRADDHA service role key | Optional |

---

## 🚀 Local Verification

```bash
pip install -r requirements.txt
python test_local.py
python gem_scraper.py --dry-run
python gem_scraper.py
```
