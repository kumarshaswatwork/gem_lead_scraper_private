"""
Firebase Firestore & Zivic Nexus Category-Pooled Synchronization Engine (V2)
- Maps subscribed vendors to canonical Business Categories (`__category__:<slug>`),
  matching `lib/leads/category-lead-routing.ts` 1-to-1.
- Enriches vendor profiles with physical address (`YOUNG.vendor_profiles.address`)
  and diagnostic identity (`SENSHRADDHA.business_profiles`).
- Preserves earliest `found_date` / `discovered_at` on merges so per-plan
  `Lead Delivery Speed` timers never reset across scraper runs!
"""

import os
import re
import json
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
import requests

logger = logging.getLogger("FirebaseSync")

GENERIC_CATEGORY_STOPWORDS = {
    "other", "others", "general", "general business", "local business",
    "local clothing/apparel market", "clothing", "apparel", "retail",
    "services", "service", "product", "products", "saas & software",
    "saas", "software", "ai", "business", "company", "agency", "store", "shop",
}

CANONICAL_CATEGORY_BUCKETS: List[Dict[str, Any]] = [
    {
        "slug": "saree-apparel",
        "label": "Saree & Ethnic Apparel",
        "keywords": ["banarasi", "benarasi", "banarasee", "saree", "sari", "sarri", "handloom silk", "katan silk"],
        "gem_queries": ["Saree", "Handloom Saree", "Silk Saree"],
    },
    {
        "slug": "lehenga-bridal-wear",
        "label": "Lehenga & Bridal Wear",
        "keywords": ["lehenga", "bridal wear", "bridal couture", "sherwani", "gown"],
        "gem_queries": ["Apparel", "Garments", "Uniform"],
    },
    {
        "slug": "chikankari-ethnic-wear",
        "label": "Chikankari & Kurta Apparel",
        "keywords": ["chikankari", "chikan", "kurti", "kurta", "salwar", "suit set"],
        "gem_queries": ["Kurta", "Handloom", "Cotton Garments"],
    },
    {
        "slug": "jewellery-accessories",
        "label": "Jewellery & Bridal Accessories",
        "keywords": ["jewellery", "jewelry", "kundan", "polki", "gold ornament", "imitation jewellery"],
        "gem_queries": ["Memento", "Handicraft", "Trophy"],
    },
    {
        "slug": "photography-videography",
        "label": "Photography & Videography",
        "keywords": ["photographer", "photography", "videography", "cinematography", "pre-wedding shoot"],
        "gem_queries": ["Photography", "Videography", "Audio Visual"],
    },
    {
        "slug": "wedding-event-management",
        "label": "Wedding & Event Management",
        "keywords": [
            "event-management", "event management", "wedding planner", "wedding planning",
            "event planner", "orchestra", "live band", "wedding decorator", "tent house",
        ],
        "gem_queries": ["Event Management", "Stage", "Audio Visual", "Tent"],
    },
    {
        "slug": "catering-hospitality",
        "label": "Catering & Hospitality",
        "keywords": ["catering", "caterer", "banquet", "halwai", "bakery", "mithai", "sweets"],
        "gem_queries": ["Catering", "Food Service", "Refreshment"],
    },
    {
        "slug": "makeup-salon-beauty",
        "label": "Bridal Makeup & Salon",
        "keywords": ["bridal makeup", "makeup artist", "mua", "mehendi", "mehndi", "salon", "beauty parlour"],
        "gem_queries": ["Cosmetics", "Hygiene Kit", "Personal Care"],
    },
    {
        "slug": "digital-marketing-seo",
        "label": "Digital Marketing & SEO",
        "keywords": ["digital marketing", "social media marketing", "seo agency", "performance marketing", "meta ads", "google ads"],
        "gem_queries": ["Social Media", "Digital Marketing", "Advertising"],
    },
    {
        "slug": "web-app-development",
        "label": "Web & App Development",
        "keywords": ["web development", "website design", "app development", "mobile app", "ecommerce website"],
        "gem_queries": ["Website Development", "Software Development", "IT Portal"],
    },
    {
        "slug": "medical-oxygen-equipment",
        "label": "Medical Oxygen & Hospital Equipment",
        "keywords": ["oxygen plant", "psa plant", "medical oxygen", "oxygen concentrator", "medical gas pipeline", "mgps", "hospital equipment"],
        "gem_queries": ["Oxygen Plant", "Oxygen Concentrator", "Medical Gas Pipeline"],
    },
    {
        "slug": "heavy-engineering-machinery",
        "label": "Heavy Machinery & Electrical Engineering",
        "keywords": ["excavator", "heavy machine", "power cable", "jetting machine", "transformer", "dg set", "generator"],
        "gem_queries": ["Excavator", "Power Cable", "Generator"],
    },
    {
        "slug": "facility-security-services",
        "label": "Facility Management & Security Services",
        "keywords": ["facility management", "housekeeping", "manpower supply", "security guard", "sanitation service"],
        "gem_queries": ["Facility Management", "Housekeeping", "Security Manpower"],
    },
    {
        "slug": "printing-signage-advertising",
        "label": "Printing, Signage & Outdoor Advertising",
        "keywords": ["printing", "flex banner", "hoarding", "signage", "offset print", "brochure printing"],
        "gem_queries": ["Printing", "Signage", "Brochure"],
    },
]

BUCKET_BY_SLUG = {b["slug"]: b for b in CANONICAL_CATEGORY_BUCKETS}


def _slugify(val: str) -> str:
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", (val or "").lower()))[:64]


def resolve_vendor_category(vendor: Dict[str, Any]) -> Tuple[str, str]:
    """
    Deterministically resolves `(category_slug, category_label)` for a vendor,
    matching `resolveVendorCategory` in `lib/leads/category-lead-routing.ts`.
    """
    existing_slug = str(vendor.get("category_slug") or "").strip()
    existing_label = str(vendor.get("category_label") or "").strip()
    if existing_slug and existing_slug != "general-marketplace":
        if existing_slug in BUCKET_BY_SLUG:
            return existing_slug, BUCKET_BY_SLUG[existing_slug]["label"]
        if existing_label:
            return existing_slug, existing_label

    sub_niche = str(vendor.get("sub_niche") or "").strip()
    niche = str(vendor.get("niche") or "").strip()
    what_sell = str(vendor.get("what_sell") or "").strip()
    industry = str(vendor.get("industry") or "").strip()
    vp_slug = str(vendor.get("vp_category_slug") or "").strip()

    primary_blob = f"{sub_niche} {niche} {what_sell}".lower()
    for bucket in CANONICAL_CATEGORY_BUCKETS:
        if any(kw in primary_blob for kw in bucket["keywords"]):
            return bucket["slug"], bucket["label"]

    for cand in [sub_niche, niche]:
        clean = cand.strip()
        if clean and clean.lower() not in GENERIC_CATEGORY_STOPWORDS:
            slug = _slugify(clean)
            if len(slug) >= 2:
                return slug, clean

    secondary_blob = f"{vp_slug} {industry}".lower()
    for bucket in CANONICAL_CATEGORY_BUCKETS:
        if any(kw in secondary_blob for kw in bucket["keywords"]):
            return bucket["slug"], bucket["label"]

    for cand in [vp_slug, industry]:
        clean = cand.strip()
        if clean and clean.lower() not in GENERIC_CATEGORY_STOPWORDS:
            slug = _slugify(clean)
            if len(slug) >= 2:
                return slug, clean.replace("-", " ").title()

    return "general-marketplace", "General Marketplace"


def get_gem_queries_for_category(category_slug: str, category_label: str, vendors_in_cat: List[Dict[str, Any]]) -> List[str]:
    """Returns concise, GeM-taxonomy-friendly search terms for a Business Category."""
    queries: List[str] = []
    if category_slug in BUCKET_BY_SLUG:
        queries.extend(BUCKET_BY_SLUG[category_slug]["gem_queries"])
    elif category_slug != "general-marketplace":
        queries.append(category_label)

    for v in vendors_in_cat:
        for field in ["sub_niche", "niche"]:
            val = str(v.get(field) or "").strip()
            if 3 <= len(val) <= 35 and val.lower() not in GENERIC_CATEGORY_STOPWORDS:
                if val not in queries:
                    queries.append(val)

    return queries[:4] if queries else ["Event Management", "Computer", "Catering"]


class FirebaseSyncManager:
    def __init__(self):
        self.db = None
        raw_base = (
            os.environ.get("ZIVIC_BASE_URL")
            or os.environ.get("NEXT_PUBLIC_APP_URL")
            or os.environ.get("NEXT_PUBLIC_SITE_URL")
            or ""
        ).rstrip("/")
        # Avoid using localhost in non-running local CLI unless explicitly set via ZIVIC_BASE_URL
        if "localhost" in raw_base and not os.environ.get("ZIVIC_BASE_URL"):
            self.api_base_url = ""
        else:
            self.api_base_url = raw_base

        raw_scraper_keys = os.environ.get("SCRAPER_API_KEY") or os.environ.get("SCRAPER_API_KEYS") or ""
        self.scraper_api_key = raw_scraper_keys.split(",")[0].strip() if raw_scraper_keys else ""
        self._init_firestore()

    def _init_firestore(self):
        """Initializes direct Firestore client (`fir-80-72586`) if `firebase_admin` + credentials are present."""
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore

            if firebase_admin._apps:
                self.db = firestore.client()
                return

            cred = None
            raw_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_KEY") or os.environ.get("FIREBASE_GEM_SERVICE_ACCOUNT_KEY")
            if raw_json:
                try:
                    cred = credentials.Certificate(json.loads(raw_json))
                    logger.info("Loaded Firebase credentials from FIREBASE_SERVICE_ACCOUNT_KEY")
                except Exception as e:
                    logger.error(f"Failed to parse FIREBASE_SERVICE_ACCOUNT_KEY JSON: {e}")

            file_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH")
            if not cred and file_path and os.path.exists(file_path):
                cred = credentials.Certificate(file_path)
                logger.info(f"Loaded Firebase credentials from: {file_path}")

            if not cred:
                secrets_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "digitalMarketing",
                    "secrets",
                )
                if os.path.isdir(secrets_dir):
                    for fname in os.listdir(secrets_dir):
                        if fname.endswith(".json") and "fir-80-72586" in fname:
                            cred = credentials.Certificate(os.path.join(secrets_dir, fname))
                            logger.info(f"Discovered local secret: {fname}")
                            break

            if cred:
                firebase_admin.initialize_app(cred, {"projectId": "fir-80-72586"})
                self.db = firestore.client()
                logger.info("Connected to Firestore project fir-80-72586")
            else:
                logger.warning("No direct Firestore credentials found. Will use Zivic HTTP API if configured.")
        except Exception as e:
            logger.warning(f"Direct Firestore client unavailable ({e}). Will rely on HTTP API if configured.")

    def _enrich_vendors_from_supabase(self, vendors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Enriches vendor rows with physical address (YOUNG) & diagnostic profile (SENSHRADDHA) if env vars exist."""
        if not vendors:
            return vendors

        young_url = (os.environ.get("NEXT_PUBLIC_SUPABASE_URL") or "").rstrip("/")
        young_key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        sens_url = (os.environ.get("SENSHRADDHA_SUPABASE_PROJECT2_URL") or "").rstrip("/")
        sens_key = (os.environ.get("SENSHRADDHA_SUPABASE_PROJECT2_SERVICE_ROLE_KEY") or "").strip()

        if young_url and young_key:
            try:
                vids = [v["vendor_id"] for v in vendors if v.get("vendor_id")]
                if vids:
                    in_list = ",".join(f'"{vid}"' for vid in vids[:100])
                    resp = requests.get(
                        f"{young_url}/rest/v1/vendor_profiles?select=user_id,category_slug,address&user_id=in.({in_list})",
                        headers={"apikey": young_key, "Authorization": f"Bearer {young_key}"},
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        vp_by_id = {row["user_id"]: row for row in resp.json() if row.get("user_id")}
                        for v in vendors:
                            vp = vp_by_id.get(v.get("vendor_id"))
                            if vp:
                                if not v.get("address") and vp.get("address"):
                                    v["address"] = vp["address"]
                                if vp.get("category_slug"):
                                    v["vp_category_slug"] = vp["category_slug"]
            except Exception as e:
                logger.debug(f"YOUNG vendor_profiles enrichment skipped: {e}")

        if sens_url and sens_key:
            try:
                emails = [str(v.get("email") or "").strip().lower() for v in vendors if v.get("email")]
                if emails:
                    in_list = ",".join(f'"{em}"' for em in emails[:100])
                    resp = requests.get(
                        f"{sens_url}/rest/v1/business_profiles?select=email,business_name,industry,raw_inputs&email=in.({in_list})",
                        headers={"apikey": sens_key, "Authorization": f"Bearer {sens_key}"},
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        bp_by_em = {str(r["email"]).lower(): r for r in resp.json() if r.get("email")}
                        for v in vendors:
                            em = str(v.get("email") or "").strip().lower()
                            bp = bp_by_em.get(em)
                            if bp:
                                ident = (bp.get("raw_inputs") or {}).get("identity") or {}
                                v["business_name"] = v.get("business_name") or bp.get("business_name") or ident.get("businessName")
                                v["niche"] = v.get("niche") or ident.get("niche")
                                v["sub_niche"] = v.get("sub_niche") or ident.get("subNiche")
                                v["what_sell"] = v.get("what_sell") or ident.get("whatYouSell")
                                v["industry"] = v.get("industry") or bp.get("industry") or ident.get("industry")
            except Exception as e:
                logger.debug(f"SENSHRADDHA business_profiles enrichment skipped: {e}")

        for v in vendors:
            cat_slug, cat_label = resolve_vendor_category(v)
            v["category_slug"] = cat_slug
            v["category_label"] = cat_label

        return vendors

    def fetch_subscribed_vendors(self) -> List[Dict[str, Any]]:
        """
        Fetches all vendors with `subscribed == True` or `ai_lead_gen_enabled == True`
        (excluding `__category__:*` pool docs) and enriches their Category + Address.
        """
        by_id: Dict[str, Dict[str, Any]] = {}

        if self.db:
            try:
                col_ref = self.db.collection("gem_vendor_feeds")
                for field_name in ["subscribed", "ai_lead_gen_enabled"]:
                    for doc in col_ref.where(field_name, "==", True).stream():
                        if doc.id.startswith("__category__:"):
                            continue
                        d = doc.to_dict() or {}
                        if d.get("is_category_pool"):
                            continue
                        vid = d.get("vendor_id") or doc.id
                        by_id[vid] = {**d, "vendor_id": vid}

                vendors = self._enrich_vendors_from_supabase(list(by_id.values()))
                logger.info(f"[Firestore] Discovered {len(vendors)} active GeM vendors")
                return vendors
            except Exception as e:
                logger.error(f"Firestore query for subscribed vendors failed: {e}")

        if self.api_base_url and self.scraper_api_key:
            try:
                url = f"{self.api_base_url}/api/gem/subscribed-vendors"
                headers = {"Authorization": f"Bearer {self.scraper_api_key}"}
                res = requests.get(url, headers=headers, timeout=15)
                if res.status_code == 200:
                    vendors = res.json().get("vendors", [])
                    for v in vendors:
                        cat_slug, cat_label = resolve_vendor_category(v)
                        v["category_slug"] = cat_slug
                        v["category_label"] = cat_label
                    logger.info(f"[API] Discovered {len(vendors)} active GeM vendors")
                    return vendors
            except Exception as e:
                logger.error(f"Failed to fetch subscribed vendors via API: {e}")

        return []

    def match_tenders_to_vendor(self, vendor: Dict[str, Any], tenders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Matches category tenders to a vendor, boosting tenders whose city/state/department
        matches the vendor's physical address!
        """
        v_cat = vendor.get("category_slug") or "general-marketplace"
        addr_tokens = [
            tok for tok in re.split(r"[^a-z0-9]+", str(vendor.get("address") or "").lower())
            if len(tok) >= 3 and tok not in {"india", "road", "near", "nagar", "colony", "sector"}
        ]

        keywords = set()
        for field in ["niche", "sub_niche", "what_sell", "industry"]:
            val = str(vendor.get(field) or "").strip().lower()
            if val:
                keywords.add(val)
                for w in re.split(r"[^a-z0-9]+", val):
                    if len(w) >= 4 and w not in {"with", "from", "services", "service", "products"}:
                        keywords.add(w)

        matched: List[Dict[str, Any]] = []
        for t in tenders:
            t_cat = t.get("matched_category_slug")
            t_text = f"{t.get('title', '')} {t.get('department', '')} {t.get('category_name', '')}".lower()
            loc_text = f"{t.get('city', '')} {t.get('state', '')} {t.get('department', '')}".lower()

            same_category = bool(t_cat and t_cat == v_cat)
            kw_hit = any(kw in t_text for kw in keywords)

            if not (same_category or kw_hit or v_cat == "general-marketplace"):
                continue

            item_copy = dict(t)
            base_score = int(item_copy.get("match_score") or 76)
            if addr_tokens and any(tok in loc_text for tok in addr_tokens):
                base_score = min(98, base_score + 10)
            item_copy["match_score"] = base_score
            matched.append(item_copy)

        matched.sort(key=lambda x: (-int(x.get("match_score") or 0), str(x.get("expiry_date") or "")))
        return matched[:50]

    def sync_target_inquiries(
        self,
        target_doc_id: str,
        new_inquiries: List[Dict[str, Any]],
        category_slug: Optional[str] = None,
        category_label: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Upserts GeM inquiries into `gem_vendor_feeds/{target_doc_id}` (either `__category__:<slug>`
        or `{vendor_id}`), strictly preserving earliest `found_date` / `discovered_at` so
        Lead Delivery Speed timers never reset!
        """
        if not new_inquiries:
            return {"synced": 0, "pruned": 0}

        is_pool = target_doc_id.startswith("__category__:")

        if self.db:
            try:
                doc_ref = self.db.collection("gem_vendor_feeds").doc(target_doc_id)
                snap = doc_ref.get()
                existing_data = (snap.to_dict() or {}) if snap.exists else {}
                existing_inquiries: Dict[str, Any] = existing_data.get("inquiries") or {}
                now_iso = datetime.now(timezone.utc).isoformat()

                merged_inquiries = dict(existing_inquiries)
                synced_count = 0

                for inq in new_inquiries:
                    bid_id = inq.get("bid_id")
                    if not bid_id:
                        continue
                    prev = existing_inquiries.get(bid_id) or {}
                    prev_disc = prev.get("discovered_at") or prev.get("found_date")
                    cand_disc = inq.get("discovered_at") or inq.get("found_date") or now_iso
                    earliest_disc = prev_disc if prev_disc and prev_disc <= cand_disc else cand_disc

                    merged_inquiries[bid_id] = {
                        **prev,
                        **inq,
                        "found_date": earliest_disc,
                        "discovered_at": earliest_disc,
                    }
                    synced_count += 1

                payload: Dict[str, Any] = {
                    "inquiries": merged_inquiries,
                    "last_synced_at": now_iso,
                }
                if is_pool:
                    payload["vendor_id"] = target_doc_id
                    payload["is_category_pool"] = True
                    payload["subscribed"] = False
                if category_slug:
                    payload["category_slug"] = category_slug
                if category_label:
                    payload["category_label"] = category_label

                doc_ref.set(payload, merge=True)
                pruned = self.prune_expired_inquiries(target_doc_id)
                logger.info(
                    f"[Firestore] Target '{target_doc_id}': Synced {synced_count} tenders, pruned {pruned} expired."
                )
                return {"synced": synced_count, "pruned": pruned}
            except Exception as e:
                logger.error(f"Firestore update failed for '{target_doc_id}': {e}")

        if self.api_base_url and self.scraper_api_key:
            try:
                url = f"{self.api_base_url}/api/gem/sync-bids"
                headers = {
                    "Authorization": f"Bearer {self.scraper_api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "vendorId": target_doc_id,
                    "inquiries": new_inquiries,
                    "categorySlug": category_slug,
                    "categoryLabel": category_label,
                }
                res = requests.post(url, json=payload, headers=headers, timeout=20)
                if res.status_code == 200:
                    data = res.json()
                    return {
                        "synced": data.get("syncedCount", 0),
                        "pruned": data.get("prunedCount", 0),
                    }
                else:
                    logger.warning(f"API /api/gem/sync-bids returned HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Failed to sync '{target_doc_id}' via API: {e}")

        return {"synced": 0, "pruned": 0}

    def sync_vendor_inquiries(self, vendor_id: str, new_inquiries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Backwards-compatible alias for `sync_target_inquiries`."""
        return self.sync_target_inquiries(vendor_id, new_inquiries)

    def prune_expired_inquiries(self, vendor_id: str) -> int:
        """Removes expired inquiries from `gem_vendor_feeds/{vendor_id}` where `expiry_date < now`."""
        if not self.db:
            return 0

        try:
            from firebase_admin import firestore
            doc_ref = self.db.collection("gem_vendor_feeds").doc(vendor_id)
            snap = doc_ref.get()
            if not snap.exists:
                return 0

            data = snap.to_dict() or {}
            inquiries = data.get("inquiries", {})
            now_iso = datetime.now(timezone.utc).isoformat()

            deletes = {}
            pruned_count = 0
            for bid_id, inq in inquiries.items():
                expiry = (inq or {}).get("expiry_date")
                if expiry and expiry < now_iso:
                    deletes[f"inquiries.{bid_id}"] = firestore.DELETE_FIELD
                    pruned_count += 1

            if deletes:
                doc_ref.update(deletes)
            return pruned_count
        except Exception as e:
            logger.error(f"Error pruning expired inquiries for {vendor_id}: {e}")
            return 0
