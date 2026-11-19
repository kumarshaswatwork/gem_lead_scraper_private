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
import time
import hashlib
import logging
import urllib.parse
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
import requests

logger = logging.getLogger("FirebaseSync")

CACHE_DIR = os.environ.get(
    "SCRAPER_CACHE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".scraper_cache"),
)
GEM_AI_QUERY_CACHE_FILE = os.path.join(CACHE_DIR, "gem_ai_query_cache.json")
GEM_AI_QUERY_CACHE_TTL_SECONDS = 24 * 3600

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
        "keywords": ["photographer", "photography", "videography", "cinematography", "pre-wedding shoot", "wedding photographer", "event photographer"],
        "gem_queries": ["Photography", "Videography", "Audio Visual"],
    },
    {
        "slug": "wedding-event-management",
        "label": "Wedding & Event Management",
        "keywords": [
            "event-management", "event management", "event manager", "event marketer", "wedding planner", "wedding planning",
            "event planner", "orchestra", "live band", "wedding decorator", "tent house",
            "birthday party", "birthday parties", "party planner", "corporate event", "performer", "musician", "sangeet", "anchor", "dj",
        ],
        "gem_queries": ["Event Management", "Exhibition or Expo Management", "Seminar or Workshop", "Cultural Event", "Tentage"],
    },
    {
        "slug": "catering-hospitality",
        "label": "Catering & Hospitality",
        "keywords": ["catering", "caterer", "banquet", "halwai", "bakery", "mithai", "sweets", "corporate catering", "wedding catering"],
        "gem_queries": ["Catering", "Food Service", "Refreshment"],
    },
    {
        "slug": "makeup-salon-beauty",
        "label": "Bridal Makeup & Salon",
        "keywords": ["bridal makeup", "makeup artist", "mua", "mehendi", "mehndi", "salon", "beauty parlour"],
        "gem_queries": ["Cosmetics", "Hygiene Kit", "Personal Care"],
    },
    {
        "slug": "doctors-medical-consultants",
        "label": "Doctors & Medical Consultants",
        "keywords": [
            "doctor", "clinic", "physician", "hospital consultant", "medical officer",
            "specialist doctor", "visiting consultant", "medical consultant", "pediatrician",
            "cardiologist", "gynecologist", "telemedicine",
        ],
        "gem_queries": ["Doctor", "Medical Consultant", "Specialist Doctor", "Medical Officer", "Visiting Doctor", "Telemedicine"],
    },
    {
        "slug": "business-consultants",
        "label": "Business & Management Consultants",
        "keywords": [
            "business consultant", "management consultant", "strategy consultant", "crm consultant",
            "financial advisor", "msme consultant", "business advisor", "growth consultant",
        ],
        "gem_queries": ["Management Consultancy", "Business Advisory", "Financial Advisory", "Consultancy Service"],
    },
    {
        "slug": "digital-marketing-seo",
        "label": "Digital Marketing & SEO",
        "keywords": ["digital marketing", "social media marketing", "seo agency", "performance marketing", "meta ads", "google ads", "lead generation"],
        "gem_queries": ["Digital Marketing", "Social Media Management", "Publicity Campaign", "Advertising Agency", "Creative Agency"],
    },
    {
        "slug": "web-app-development",
        "label": "Web & App Development",
        "keywords": ["web development", "website design", "app development", "mobile app", "ecommerce website"],
        "gem_queries": ["Website Development", "Software Development", "IT Portal"],
    },
    {
        "slug": "civil-contractor-interior",
        "label": "Civil Contractors & Interior Designers",
        "keywords": ["civil contractor", "interior designer", "interior design", "office fitout", "renovation", "commercial fitout", "architect", "false ceiling"],
        "gem_queries": ["Interior Work", "Civil Construction", "Renovation", "Office Fitout"],
    },
    {
        "slug": "medical-oxygen-equipment",
        "label": "Medical Oxygen & Hospital Equipment",
        "keywords": ["oxygen plant", "psa plant", "psa", "oxyygen", "medical oxygen", "oxygen concentrator", "medical gas pipeline", "mgps", "hospital equipment"],
        "gem_queries": ["Oxygen Plant", "PSA Oxygen", "Oxygen Generator", "Oxygen Concentrator", "Medical Gas Pipeline", "Liquid Medical Oxygen"],
    },
    {
        "slug": "water-treatment-etp-stp",
        "label": "Water Treatment, STP & ETP Plants",
        "keywords": [
            "waste water", "wastewater", "etp", "stp", "etp-stp",
            "effluent treatment", "sewage treatment", "water treatment",
            "water treatment plant", "ro plant", "dm plant",
            "water purification", "water quality", "pollution control",
        ],
        "gem_queries": [
            "Sewage Treatment", "Effluent Treatment", "Water Treatment", "STP", "ETP",
            "Operation and Maintenance of Effluent", "Effluent/Sewage Treatment Plant", "RO Plant"
        ],
    },
    {
        "slug": "heavy-engineering-machinery",
        "label": "Heavy Machinery & Electrical Engineering",
        "keywords": ["machinery", "machine supplier", "industrial machine", "excavator", "heavy machine", "power cable", "jetting machine", "transformer", "dg set", "generator", "industrial pumps", "cnc machine", "packaging machine"],
        "gem_queries": ["Excavator", "Power Cable", "Generator", "Industrial Machinery"],
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

CUSTOM_VERTICAL_QUERY_PROFILES: Dict[str, Dict[str, Any]] = {
    "real_estate": {
        "triggers": ["real estate", "property", "flat", "plot", "apartment", "premises", "commercial space", "warehouse", "godown", "building lease"],
        "gem_queries": ["Hiring of Premises", "Leasing of Premises", "Office Premises", "Commercial Space", "Land Allotment"],
        "include": ["hiring of premises", "leasing of premises", "leasing of office", "office space", "office premises", "commercial space", "residential accommodation", "staff quarters", "renting of building", "land allotment", "e-auction"],
        "exclude": ["software", "laptop", "printer", "camera", "stationery", "sanitation", "furniture", "food grain storage"],
    },
    "paying_guest": {
        "triggers": ["paying guest", "hostel", "guest house", "pg ", "boarding", "lodging", "dormitory", "accommodation"],
        "gem_queries": ["Guest House", "Hostel Accommodation", "Transit Camp", "Boarding and Lodging", "Accommodation"],
        "include": ["guest house", "hostel", "accommodation", "transit camp", "boarding and lodging", "dormitory", "barrack", "hostel mess"],
        "exclude": ["pg diploma", "post graduate", "laptop", "computer", "furniture", "refrigerator", "two door", "washing machine", "air conditioner", "plc systems", "siemens"],
    },
    "gem_services": {
        "triggers": ["gem service", "gem consultancy", "gem registration", "bid consultancy", "tender consultancy", "e-procurement", "procurement support", "pmc"],
        "gem_queries": ["Tender Consultancy", "Procurement Consultancy", "Project Management Consultancy", "Transaction Advisory", "Bid Management"],
        "include": ["tender consultancy", "procurement support", "procurement consultancy", "project management consultancy", "project management consultant", "bid preparation", "manpower outsourcing consultancy", "transaction advisory"],
        "exclude": ["gemstone", "saree", "mattress", "pmcc", "pmcxmc", "mcc panel", "vme module", "flash point", "switchgear", "facility management services", "facility management", "housekeeping", "o&m of electrical", "electrical work", "electrical works", "security manpower", "kusmunda"],
    },
    "gst_services": {
        "triggers": ["gst", "chartered accountant", "tax consultancy", "tax audit", "statutory audit", "internal audit", "accounting", "ca ", "itr", "tds"],
        "gem_queries": ["Chartered Accountant", "GST Audit", "Tax Consultancy", "Internal Audit", "Statutory Audit", "Accounting Services"],
        "include": ["chartered accountant", "gst audit", "gst return", "gst ", "tax consultancy", "internal audit", "statutory audit", "accounting service", "taxation", "financial audit"],
        "exclude": ["chartered aircraft", "air charter", "helicopter", "airline", "ambulance", "passenger transport"],
    },
    "waste_management": {
        "triggers": ["waste management", "solid waste", "biomedical waste", "e-waste", "ewaste", "garbage", "recycling", "waste disposal", "scrap disposal"],
        "gem_queries": ["Solid Waste", "Waste Management", "Biomedical Waste", "E-Waste", "Waste Collection", "Garbage"],
        "include": ["solid waste", "waste management", "waste collection", "waste disposal", "biomedical waste", "bio-medical waste", "e-waste", "ewaste", "garbage", "waste container", "municipal waste", "hazardous waste"],
        "exclude": ["waste fittings", "waste pipe", "sink waste", "washbasin", "waste coupling", "cotton waste", "gate valve", "stop valve", "pillar cock"],
    },
    "doctors": {
        "triggers": ["doctor", "doctors", "medical consultant", "visiting consultant", "specialist doctor", "medical officer", "physician", "telemedicine"],
        "gem_queries": ["Doctor", "Medical Consultant", "Specialist Doctor", "Medical Officer", "Visiting Doctor", "Telemedicine"],
        "include": ["hiring of duty doctor", "hiring of doctor", "deputation of doctors", "medical consultant", "visiting consultant", "specialist doctor", "medical officer", "physician", "telemedicine", "doctor service", "surgeon"],
        "exclude": ["doctor blade", "doctor's blade", "rotogravure", "spectrophotometer", "bronchoscope", "anaesthesia workstation", "syringe", "reagent", "wheelchair", "stretcher", "glass partition", "partition work", "conveyance", "tab cold", "syp ", "hostel of", "central heating", "peon", "unskilled", "head teacher", "ward boy", "sweeper", "high school"],
    },
    "coaching": {
        "triggers": ["coaching", "skill training", "capacity building", "vocational training", "tuition", "tutor", "faculty", "test prep"],
        "gem_queries": ["Skill Training", "Capacity Building", "Coaching", "Training Service", "Vocational Training", "Faculty"],
        "include": ["coaching", "skill training", "capacity building", "vocational training", "training program", "training service", "faculty", "upskilling", "test preparation", "placement training"],
        "exclude": ["pants", "shirt", "kurti", "kurta", "shoes", "rope light", "led ", "maplitho", "catering", "railway coach", "lhb coach", "icf coach", "coach cleaning", "coach attendant", "passenger coach", "coaching depot", "bogie", "bogies", "wheel set", "wheel sets", "rail coach", "loco", "weapon training simulator", "thematic murals", "drone training", "energy audit", "sewing machine"],
    },
    "event_marketing": {
        "triggers": ["event marketing", "corporate event", "brand activation", "exhibition"],
        "gem_queries": ["Event Management", "Exhibition or Expo Management", "Seminar or Workshop", "Cultural Event", "Tentage"],
        "include": ["event management", "exhibition", "seminar", "seminar or workshop", "training workshop", "cultural event", "tentage", "brand activation"],
        "exclude": ["conference table", "conference chair", "conference room", "audio visual alarm", "led display", "steam network", "rectification", "hot jobs", "maintenance of workshop"],
    },
}


def _slugify(val: str) -> str:
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", (val or "").lower()))[:64]


def _vertical_family(slug_or_text: str) -> str:
    low = (slug_or_text or "").lower().replace("-", " ")
    for family, cfg in CUSTOM_VERTICAL_QUERY_PROFILES.items():
        if any(tr in low for tr in cfg["triggers"]):
            return family
    return ""


def _has_excluded_tender_noise(text_low: str, category_hint: str = "") -> bool:
    """Shared negative guard preventing absurd GeM false positives across classification & vendor matching."""
    cat_low = (category_hint or "").lower()
    if any(k in cat_low for k in ["gst", "tax", "audit", "chartered", "account", "ca-"]):
        if any(bad in text_low for bad in ["chartered aircraft", "air charter", "helicopter", "ambulance"]):
            if not any(ok in text_low for ok in ["chartered accountant", "gst", "tax", "audit", "accounting"]):
                return True
    if any(k in cat_low for k in ["doctor", "medical-consultant", "physician"]):
        if any(bad in text_low for bad in [
            "doctor blade", "doctor's blade", "rotogravure", "spectrophotometer",
            "bronchoscope", "anaesthesia workstation", "syringe", "reagent", "wheelchair", "stretcher",
            "glass partition", "partition work", "conveyance", "tab cold", "syp ", "hostel of", "central heating",
            "peon", "unskilled", "head teacher", "ward boy", "sweeper", "high school",
        ]):
            return True
    if any(k in cat_low for k in ["waste-management", "waste", "solid-waste", "recycling"]):
        if any(bad in text_low for bad in [
            "waste fittings", "waste pipe", "sink waste", "washbasin", "waste coupling",
            "cotton waste", "gate valve", "stop valve", "pillar cock", "health faucet",
        ]):
            return True
        if any(furn in text_low for furn in ["icu bed", "motorized bed", "manual bed", "bedside locker", "fowler bed"]):
            if not any(ok in text_low for ok in ["waste management", "waste disposal", "waste collection"]):
                return True
    if any(k in cat_low for k in ["coaching", "training", "tuition"]):
        if any(bad in text_low for bad in [
            "pants", "shirt", "kurti", "kurta", "shoes", "rope light", "led ",
            "maplitho", "catering", "railway coach", "lhb coach", "icf coach",
            "coach cleaning", "coach attendant", "passenger coach", "coach watering",
            "coaching depot", "bogie", "bogies", "wheel set", "wheel sets", "rail coach", "loco",
            "weapon training simulator", "thematic murals", "drone training", "energy audit", "sewing machine",
        ]):
            return True
    if any(k in cat_low for k in ["event", "wedding-event"]):
        if any(bad in text_low for bad in [
            "conference table", "conference chair", "conference room", "fel -3 stage",
            "audio visual alarm", "haulage", "dummy core", "l-shape", "workstation table", "led display",
        ]):
            return True
    if any(k in cat_low for k in ["gem-service", "gem-registration", "procurement", "tender-bid"]):
        if any(bad in text_low for bad in [
            "pmcc", "pmcxmc", "mcc panel", "vme module", "flash point", "switchgear",
            "facility management services", "facility management", "housekeeping",
            "o&m of electrical", "electrical work", "electrical works", "security manpower", "kusmunda",
        ]):
            if not any(ok in text_low for ok in ["tender consultancy", "procurement consultancy", "bid management"]):
                return True
    if any(k in cat_low for k in ["heavy-engineering", "machinery", "machine", "electrical-engineering", "generator", "excavator", "dg-set", "power-cable"]):
        if any(bad in text_low for bad in [
            "motor insurance", "vehicle insurance", "insurance service",
            "smoke generator", "cypermethrin", "herbicide", "insecticide", "pesticide", "fogging",
        ]):
            return True
    if any(k in cat_low for k in ["solar", "photovoltaic"]):
        has_solar_pv = any(ok in text_low for ok in ["photovoltaic", "solar power plant", "rooftop solar", "roof top solar", "solar pv", "solar panel"])
        if not has_solar_pv and any(bad in text_low for bad in [
            "herbicide", "insecticide", "pesticide", "chevron sign", "retro reflective", "retro-reflective",
            "sign board", "signboard", "road stud", "cat eye", "reflective stud",
        ]):
            return True
    if any(k in cat_low for k in ["paying-guest", "pg-", "hostel"]):
        if any(bad in text_low for bad in ["refrigerator", "two door", "washing machine", "air conditioner", "plc systems", "siemens"]):
            return True
    if any(k in cat_low for k in ["real-estate", "flats", "plots", "premises"]):
        if "food grain storage" in text_low:
            return True
    if any(k in cat_low for k in ["digital-marketing", "social", "seo", "advertising"]):
        if any(bad in text_low for bad in [
            "chickpea", "channa", "coconut grater", "cotton mattress", "ration", "mid day meal",
            "amc/cmc for pre-owned", "amc / cmc for pre-owned", "for pre-owned",
            "printing unit", "offset printing", "printing press", "printing machine",
            "sheetfed offset", "colour offset", "color offset", "web offset",
        ]):
            return True
    if "saree" in cat_low:
        if "safari" in text_low and "saree" not in text_low and "sari" not in text_low:
            return True
    return False


def classify_tender(tender: Dict[str, Any]) -> Optional[str]:
    """
    Classifies an untagged tender strictly from its work item title / category name
    (NEVER department name!) so 'Social Welfare Department' food items don't pollute Digital Marketing.
    """
    title_blob = f"{tender.get('title', '')} {tender.get('category_name', '')}".lower()
    for bucket in CANONICAL_CATEGORY_BUCKETS:
        if any(kw in title_blob for kw in bucket["keywords"]):
            if not _has_excluded_tender_noise(title_blob, bucket["slug"]):
                return bucket["slug"]

    for slug_key, cfg in CUSTOM_VERTICAL_QUERY_PROFILES.items():
        if any(inc in title_blob for inc in cfg["include"]) and not any(exc in title_blob for exc in cfg["exclude"]):
            mapped = slug_key.replace("_", "-")
            if not _has_excluded_tender_noise(title_blob, mapped):
                return mapped
    return None


def resolve_vendor_category(vendor: Dict[str, Any]) -> Tuple[str, str]:
    """
    Deterministically resolves `(category_slug, category_label)` for a vendor,
    matching `resolveVendorCategory` in `lib/leads/category-lead-routing.ts`.
    """
    sub_niche = str(vendor.get("sub_niche") or "").strip()
    niche = str(vendor.get("niche") or "").strip()
    what_sell = str(vendor.get("what_sell") or "").strip()
    industry = str(vendor.get("industry") or "").strip()
    vp_slug = str(vendor.get("vp_category_slug") or "").strip()

    # 1. Check vendor's explicit niche / sub-niche first (e.g. Oxygen Plant, Waste Water Treatment)
    niche_blob = f"{sub_niche} {niche}".lower()
    for bucket in CANONICAL_CATEGORY_BUCKETS:
        if any(kw in niche_blob for kw in bucket["keywords"]):
            return bucket["slug"], bucket["label"]

    # 2. Check full primary blob including what_sell
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

    # 3. Check secondary signals (vendor_profile category / industry)
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

    # 4. Fallback to existing valid slug if present
    existing_slug = str(vendor.get("category_slug") or "").strip()
    existing_label = str(vendor.get("category_label") or "").strip()
    if existing_slug and existing_slug != "general-marketplace":
        if existing_slug in BUCKET_BY_SLUG:
            return existing_slug, BUCKET_BY_SLUG[existing_slug]["label"]
        if existing_label:
            return existing_slug, existing_label

    return "general-marketplace", "General Marketplace"



_DYNAMIC_GENERIC_MODIFIERS = {
    "service", "services", "solution", "solutions", "agency", "agencies",
    "management", "company", "companies", "provider", "providers", "firm",
    "consultancy", "consulting", "center", "centre", "shop", "store",
    "work", "works", "supply", "supplier", "suppliers", "contractor", "contractors",
    "system", "systems", "india", "indian", "and", "for", "of", "the",
}

_WEAK_STANDALONE_MODIFIERS = {
    "water", "cold", "hot", "air", "raw", "high", "mini", "auto", "smart",
    "home", "office", "commercial", "industrial", "general", "private", "public",
    "box", "boxes", "pump", "pumps", "guard", "guards", "clinic", "panel", "panels",
}


def _singular_plural_variants(phrase: str) -> List[str]:
    words = [w for w in re.split(r"\s+", (phrase or "").strip()) if w]
    if not words:
        return []
    last = words[-1]
    low = last.lower()
    prefix = " ".join(words[:-1])
    variants: List[str] = []

    def _join(tail: str) -> str:
        return f"{prefix} {tail}".strip() if prefix else tail

    if low.endswith("xes") and len(last) >= 4:
        variants.append(_join(last[:-2]))
    elif low.endswith("ies") and len(last) >= 5:
        variants.append(_join(last[:-3] + "y"))
    elif low.endswith("s") and not low.endswith(("ss", "us", "is", "ics")) and len(last) >= 4:
        variants.append(_join(last[:-1]))
    elif not low.endswith(("s", "ing", "ment", "tion", "sion", "ity", "ry", "ic", "al", "ship")):
        sfx = "es" if low.endswith(("x", "ch", "sh")) else "s"
        variants.append(_join(last + sfx))
    return variants


def _synthesize_dynamic_gem_queries(
    category_slug: str,
    category_label: str,
    vendors_in_cat: List[Dict[str, Any]],
    location_hint: Optional[str] = None,
) -> List[str]:
    """Synthesizes 3-5 clean GeM-friendly root queries for any unseen category/location."""
    from gem_portal_client import detect_indian_location, STATE_TO_SWEEP_HUBS

    raw_label = re.sub(r"[^a-zA-Z0-9\s/&+-]", " ", (category_label or "").strip())
    raw_label = re.sub(r"\s+", " ", raw_label).strip()
    if not raw_label and category_slug:
        raw_label = category_slug.replace("-", " ").title()

    queries: List[str] = []
    seen = set()

    def _add_q(q_val: str) -> None:
        q_clean = re.sub(r"\s+", " ", (q_val or "").strip())
        if len(q_clean) < 2:
            return
        ql = q_clean.lower()
        if ql not in seen:
            seen.add(ql)
            queries.append(q_clean)

    # 1. Full clean phrase
    _add_q(raw_label)

    # 2. Strip generic suffix modifiers if another domain noun remains
    words = [w for w in re.split(r"[\s/&+-]+", raw_label) if w]
    domain_words = [w for w in words if w.lower() not in _DYNAMIC_GENERIC_MODIFIERS]
    stripped_phrase = " ".join(domain_words) if domain_words else raw_label
    _add_q(stripped_phrase)

    # 3. Core 1-2 word noun phrases + singular/plural clean variants
    for v in _singular_plural_variants(stripped_phrase):
        _add_q(v)
    if stripped_phrase.lower() != raw_label.lower():
        for v in _singular_plural_variants(raw_label):
            _add_q(v)

    if len(domain_words) >= 3:
        _add_q(f"{domain_words[0]} {domain_words[-1]}")
        _add_q(" ".join(domain_words[-2:]))
        _add_q(" ".join(domain_words[:2]))
        if len(domain_words[0]) >= 4 and domain_words[0].lower() not in _WEAK_STANDALONE_MODIFIERS:
            _add_q(domain_words[0])
    elif len(domain_words) == 2:
        if len(domain_words[0]) >= 4 and domain_words[0].lower() not in _WEAK_STANDALONE_MODIFIERS:
            _add_q(domain_words[0])
        elif len(domain_words[1]) >= 4 and domain_words[1].lower() not in _WEAK_STANDALONE_MODIFIERS:
            _add_q(domain_words[1])
    elif len(domain_words) == 1:
        root = domain_words[0]
        if "dentist" in root.lower():
            _add_q("Dental")
        if not any(m in raw_label.lower() for m in ("service", "services")):
            _add_q(f"{root} Services")
            _add_q(f"{root} Service")

    # Lightweight morphological helpers for common clinical/product variants
    low_label = raw_label.lower()
    if "dentist" in low_label:
        _add_q("Dental")
        _add_q(re.sub(r"\bdentist\b", "Dental", raw_label, flags=re.IGNORECASE))
    if re.search(r"\bpumps?\b", low_label):
        _add_q(f"{domain_words[0]} Pumping" if domain_words else "Pumping")

    # 4. Append vendor-specific niche hints if available
    for v in vendors_in_cat or []:
        for field in ["sub_niche", "niche"]:
            val = str(v.get(field) or "").strip()
            if 3 <= len(val) <= 35 and val.lower() not in GENERIC_CATEGORY_STOPWORDS:
                _add_q(val)

    # 5. Location hub / city suffix combinations when targeting a specific Indian state or city
    loc_raw = (location_hint or "").strip()
    if loc_raw and loc_raw.lower() not in ("india", "pan india", "pan-india", "all india"):
        t_st, t_city = detect_indian_location(loc_raw)
        core_root = (
            domain_words[0]
            if (domain_words and len(domain_words[0]) >= 4 and domain_words[0].lower() not in _WEAK_STANDALONE_MODIFIERS)
            else stripped_phrase
        )
        loc_suffixes: List[str] = []
        if t_city:
            loc_suffixes.append(t_city)
        if t_st != "National / Central":
            if t_st not in loc_suffixes:
                loc_suffixes.append(t_st)
            for h in STATE_TO_SWEEP_HUBS.get(t_st, [])[:2]:
                if h not in loc_suffixes:
                    loc_suffixes.append(h)
        elif loc_raw:
            loc_suffixes.append(loc_raw)
        for ls in loc_suffixes[:2]:
            _add_q(f"{core_root} {ls}")

    return queries[:6]


def _decrypt_krsw_key(stored: str, secret: str) -> str:
    if not stored:
        return ""
    if not stored.startswith("enc:v1:"):
        return stored
    if not secret:
        return ""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        _, _, iv_h, tag_h, ct_h = stored.split(":")
        aes_key = hashlib.sha256(secret.encode("utf-8")).digest()
        ct = bytes.fromhex(ct_h) + bytes.fromhex(tag_h)
        return AESGCM(aes_key).decrypt(bytes.fromhex(iv_h), ct, None).decode("utf-8")
    except Exception:
        return ""


def _expand_gem_queries_with_ai(
    category_slug: str,
    category_label: str,
    location_hint: Optional[str] = None,
) -> List[str]:
    """Optional KRSW/LLM query expansion for unseen GeM categories, cached 24h on disk."""
    cache_key = f"{(category_slug or '').lower()}|{(category_label or '').lower()}|{(location_hint or 'india').lower()}"
    now_ts = time.time()
    cache_data: Dict[str, Any] = {}
    if os.path.exists(GEM_AI_QUERY_CACHE_FILE):
        try:
            with open(GEM_AI_QUERY_CACHE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    cache_data = loaded
        except Exception:
            cache_data = {}

    entry = cache_data.get(cache_key)
    if isinstance(entry, dict) and (now_ts - float(entry.get("ts") or 0)) < GEM_AI_QUERY_CACHE_TTL_SECONDS:
        qs = entry.get("queries")
        if isinstance(qs, list):
            return [str(x).strip() for x in qs if isinstance(x, str) and x.strip()]

    candidates: List[Dict[str, str]] = []
    krsw_url = (os.environ.get("KRSW_SUPABASE_URL") or os.environ.get("KRSW_SUPABASE_PROJECT2_URL") or "").rstrip("/")
    krsw_key = (os.environ.get("KRSW_SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("KRSW_SUPABASE_PROJECT2_SERVICE_ROLE_KEY") or "").strip()
    enc_secret = (os.environ.get("AI_KEY_ENCRYPTION_SECRET") or "").strip()

    if krsw_url and krsw_key and enc_secret:
        try:
            hdr = {"apikey": krsw_key, "Authorization": f"Bearer {krsw_key}"}
            provs = requests.get(f"{krsw_url}/rest/v1/ai_providers?select=*&enabled=eq.true", headers=hdr, timeout=6).json()
            keys = requests.get(f"{krsw_url}/rest/v1/ai_provider_keys?select=*&active=eq.true", headers=hdr, timeout=6).json()
            routes = requests.get(f"{krsw_url}/rest/v1/ai_model_routes?select=*&enabled=eq.true", headers=hdr, timeout=6).json()
            prov_by_id = {p["id"]: p for p in provs if isinstance(p, dict)}
            key_by_prov = {}
            for krow in (keys if isinstance(keys, list) else []):
                if isinstance(krow, dict):
                    plain = _decrypt_krsw_key(str(krow.get("api_key") or ""), enc_secret)
                    if plain and krow.get("provider_id") not in key_by_prov:
                        key_by_prov[krow["provider_id"]] = plain
            for rt in sorted([r for r in (routes if isinstance(routes, list) else []) if isinstance(r, dict)], key=lambda x: int(x.get("priority") or 99)):
                pid = rt.get("provider_id")
                p = prov_by_id.get(pid)
                plain = key_by_prov.get(pid)
                if p and plain:
                    candidates.append({
                        "provider": str(p.get("provider_name") or "").lower(),
                        "base_url": str(p.get("base_url") or "").rstrip("/"),
                        "api_key": plain,
                        "model": str(rt.get("model_name") or p.get("default_model") or ""),
                    })
        except Exception:
            pass

    if os.environ.get("GROQ_API_KEY"):
        candidates.append({"provider": "groq", "base_url": "https://api.groq.com/openai/v1", "api_key": os.environ["GROQ_API_KEY"].strip(), "model": "openai/gpt-oss-120b"})
    if os.environ.get("OPENAI_API_KEY"):
        candidates.append({"provider": "openai", "base_url": "https://api.openai.com/v1", "api_key": os.environ["OPENAI_API_KEY"].strip(), "model": "gpt-4o-mini"})
    if os.environ.get("GEMINI_API_KEY"):
        candidates.append({"provider": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta", "api_key": os.environ["GEMINI_API_KEY"].strip(), "model": "gemini-2.5-flash"})

    if not candidates:
        return []

    prompt = (
        f"Return ONLY valid JSON: {{\"queries\": [\"3 to 5 concise 1-3 word Government e-Marketplace (GeM India) "
        f"portal search keywords for category '{category_label}' (slug '{category_slug}'). No quotes or boolean operators.\"]}}"
    )
    extracted: List[str] = []
    for cfg in candidates[:3]:
        try:
            txt = ""
            if cfg["provider"] == "gemini":
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{cfg['model']}:generateContent?key={cfg['api_key']}",
                    json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1}},
                    timeout=10,
                )
                if r.status_code == 200:
                    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            else:
                base = cfg["base_url"] or ("https://api.groq.com/openai/v1" if cfg["provider"] == "groq" else "https://api.openai.com/v1")
                r = requests.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
                    json={"model": cfg["model"], "messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 250},
                    timeout=10,
                )
                if r.status_code == 200:
                    txt = r.json()["choices"][0]["message"]["content"]
            txt = re.sub(r"<think>[\s\S]*?</think>", "", txt, flags=re.IGNORECASE).strip()
            txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.IGNORECASE).strip()
            s, e = txt.find("{"), txt.rfind("}")
            if s != -1 and e != -1 and e >= s:
                parsed = json.loads(txt[s : e + 1])
                if isinstance(parsed.get("queries"), list):
                    extracted = [re.sub(r'["()]', "", str(x)).strip() for x in parsed["queries"] if isinstance(x, str) and x.strip()][:5]
                    if extracted:
                        break
        except Exception:
            continue

    if extracted:
        try:
            parent = os.path.dirname(GEM_AI_QUERY_CACHE_FILE)
            if parent:
                os.makedirs(parent, exist_ok=True)
            cache_data = {k: v for k, v in cache_data.items() if isinstance(v, dict) and (now_ts - float(v.get("ts") or 0)) < GEM_AI_QUERY_CACHE_TTL_SECONDS}
            cache_data[cache_key] = {"ts": now_ts, "queries": extracted}
            with open(GEM_AI_QUERY_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache_data, f)
        except Exception:
            pass
    return extracted


def get_gem_queries_for_category(
    category_slug: str,
    category_label: str,
    vendors_in_cat: List[Dict[str, Any]],
    location_hint: Optional[str] = None,
    use_ai: bool = False,
) -> List[str]:
    """Returns concise, GeM-taxonomy-friendly search terms for a Business Category or vertical."""
    queries: List[str] = []
    if category_slug in BUCKET_BY_SLUG:
        queries.extend(BUCKET_BY_SLUG[category_slug]["gem_queries"])

    # Match custom procurement verticals (Real Estate/Premises, PG/Hostel, GeM Consultancy, GST/CA Audit)
    context_blob = " ".join(
        [(category_slug or "").replace("-", " "), category_label or ""]
        + [f"{v.get('niche','')} {v.get('sub_niche','')} {v.get('what_sell','')}" for v in vendors_in_cat]
    ).lower()
    for cfg in CUSTOM_VERTICAL_QUERY_PROFILES.values():
        if any(tr in context_blob for tr in cfg["triggers"]):
            for q in cfg["gem_queries"]:
                if q not in queries:
                    queries.append(q)

    if not queries and category_slug != "general-marketplace":
        queries.extend(_synthesize_dynamic_gem_queries(category_slug, category_label, vendors_in_cat, location_hint=location_hint))
        if use_ai:
            for ai_q in _expand_gem_queries_with_ai(category_slug, category_label, location_hint=location_hint):
                if ai_q and ai_q.lower() not in {q.lower() for q in queries}:
                    queries.append(ai_q)

    # If a specific sub-niche/focus (e.g. 'Dentist Clinic' or 'Security Guard Services') maps into a broad
    # bucket whose default queries don't cover that sub-niche, synthesize & prioritize sub-niche queries FIRST.
    focus_queries: List[str] = []
    if queries and vendors_in_cat:
        q_lows = [q.lower() for q in queries]
        for v in vendors_in_cat:
            for field in ("sub_niche", "niche"):
                val = str(v.get(field) or "").strip()
                v_low = val.lower()
                if not val or len(val) < 3 or v_low in GENERIC_CATEGORY_STOPWORDS:
                    continue
                if not any(ql in v_low for ql in q_lows):
                    dyn = _synthesize_dynamic_gem_queries(_slugify(val), val, [], location_hint=location_hint)
                    for dq in dyn:
                        if dq.lower() not in {fq.lower() for fq in focus_queries}:
                            focus_queries.append(dq)
                    break

    if focus_queries:
        is_cli_synthetic = all(not v.get("vendor_id") for v in vendors_in_cat)
        if is_cli_synthetic or not queries:
            combined_q: List[str] = list(focus_queries[:4])
            seen_cq = {q.lower() for q in combined_q}
            for q in queries:
                if q.lower() not in seen_cq:
                    seen_cq.add(q.lower())
                    combined_q.append(q)
            queries = combined_q
        else:
            combined_q = list(queries[:4])
            seen_cq = {q.lower() for q in combined_q}
            for fq in focus_queries[:2]:
                if fq.lower() not in seen_cq:
                    seen_cq.add(fq.lower())
                    combined_q.append(fq)
            queries = combined_q

    for v in vendors_in_cat:
        for field in ["sub_niche", "niche"]:
            val = str(v.get(field) or "").strip()
            if 3 <= len(val) <= 35 and val.lower() not in GENERIC_CATEGORY_STOPWORDS:
                if val.lower() not in {q.lower() for q in queries}:
                    queries.append(val)

    return queries[:6] if queries else ["Event Management", "Chartered Accountant", "Catering"]


class FirebaseSyncManager:
    def __init__(self):
        self.db = None
        raw_base = (
            os.environ.get("ZIVIC_BASE_URL")
            or os.environ.get("NEXT_PUBLIC_APP_URL")
            or os.environ.get("NEXT_PUBLIC_SITE_URL")
            or ""
        ).rstrip("/")
        # Default to production domain so HTTP API fallback works reliably everywhere
        if not raw_base or ("localhost" in raw_base and not os.environ.get("ZIVIC_BASE_URL")):
            self.api_base_url = "https://zivicnexus.com"
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
                                v["business_name"] = ident.get("businessName") or bp.get("business_name") or v.get("business_name")
                                v["niche"] = ident.get("niche") or v.get("niche")
                                v["sub_niche"] = ident.get("subNiche") or v.get("sub_niche")
                                v["what_sell"] = ident.get("whatYouSell") or v.get("what_sell")
                                v["industry"] = ident.get("industry") or bp.get("industry") or v.get("industry")
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
        Matches category tenders to a vendor, strictly rejecting unrelated items/departments and
        boosting tenders whose city/state/department matches the vendor's physical region!
        """
        from gem_portal_client import detect_indian_location
        v_cat = vendor.get("category_slug") or "general-marketplace"
        addr_low = f"{vendor.get('address') or ''} {vendor.get('state') or ''} {vendor.get('city') or ''}".lower()
        addr_tokens = [
            tok for tok in re.split(r"[^a-z0-9]+", addr_low)
            if len(tok) >= 3 and tok not in {"india", "road", "near", "nagar", "colony", "sector"}
        ]
        v_state, v_city = detect_indian_location(addr_low)
        has_specific_state = bool(v_state and v_state != "National / Central")

        VENDOR_KEYWORD_STOPWORDS = {
            "with", "from", "services", "service", "products", "product", "also",
            "provide", "provides", "providing", "plant", "plants", "new", "deal",
            "deals", "dealing", "supply", "supplies", "supplying", "maintenance",
            "mantainence", "repair", "work", "works", "near", "good", "best",
            "contract", "have", "need", "make", "more", "some", "than", "they",
            "this", "that", "hospital", "hospitals", "private", "public", "online",
            "social", "media", "chartered", "real", "estate", "guest", "management",
            "digital", "marketing", "consultancy", "consultant", "agency", "system",
        }

        keywords = set()
        for field in ["niche", "sub_niche", "what_sell", "industry"]:
            val = str(vendor.get(field) or "").strip().lower()
            if val:
                keywords.add(val)
                for w in re.split(r"[^a-z0-9]+", val):
                    if len(w) >= 4 and w not in VENDOR_KEYWORD_STOPWORDS:
                        keywords.add(w)

        is_local_space = any(k in v_cat for k in ["real-estate", "paying-guest", "hostel", "premises"])
        matched: List[Dict[str, Any]] = []
        for t in tenders:
            t_cat = t.get("matched_category_slug")
            # Match keywords strictly on title + category_name (NEVER department name!)
            t_title_only = f"{t.get('title', '')} {t.get('category_name', '')}".lower()
            loc_text = f"{t.get('city', '')} {t.get('state', '')} {t.get('department', '')}".lower()

            if _has_excluded_tender_noise(t_title_only, v_cat):
                continue

            v_fam = _vertical_family(v_cat)
            t_fam = _vertical_family(t_cat or "")
            same_family = bool(v_fam and t_fam and v_fam == t_fam)

            if t_cat and v_cat != "general-marketplace" and t_cat != v_cat and not same_family:
                continue

            same_category = bool((t_cat and t_cat == v_cat) or same_family)
            kw_hit = any(re.search(rf"\b{re.escape(kw)}\b", t_title_only) for kw in keywords if kw)

            if not (same_category or kw_hit or v_cat == "general-marketplace"):
                continue

            t_state = str(t.get("state") or "")
            if has_specific_state and t_state not in (v_state, "National / Central", ""):
                continue

            item_copy = dict(t)
            base_score = int(item_copy.get("match_score") or 76)
            if has_specific_state:
                if t_state == v_state:
                    base_score = min(98, base_score + 10)
                elif t_state == "National / Central":
                    base_score = min(95, base_score + 2)
                else:
                    base_score = max(50, base_score - 14)
            if addr_tokens and any(tok in loc_text for tok in addr_tokens):
                base_score = min(98, base_score + 6)
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
        is_pool = target_doc_id.startswith("__category__:")
        if not new_inquiries and is_pool:
            return {"synced": 0, "pruned": 0}

        if self.db:
            try:
                doc_ref = self.db.collection("gem_vendor_feeds").document(target_doc_id)
                snap = doc_ref.get()
                existing_data = (snap.to_dict() or {}) if snap.exists else {}
                existing_inquiries: Dict[str, Any] = existing_data.get("inquiries") or {}
                now_iso = datetime.now(timezone.utc).isoformat()

                if not is_pool and category_slug:
                    existing_cat = existing_data.get("category_slug")
                    if existing_cat and existing_cat != category_slug:
                        logger.info(f"Vendor '{target_doc_id}' changed category from '{existing_cat}' to '{category_slug}'. Resetting stale inquiries.")
                        existing_inquiries = {}
                    else:
                        # Clean out any mismatched category inquiries from previous runs
                        existing_inquiries = {
                            bid_id: inq
                            for bid_id, inq in existing_inquiries.items()
                            if isinstance(inq, dict) and (not inq.get("matched_category_slug") or inq.get("matched_category_slug") == category_slug)
                        }

                merged_inquiries: Dict[str, Any] = {}
                synced_count = 0

                for inq in (new_inquiries or []):
                    bid_id = inq.get("bid_id")
                    if not bid_id:
                        continue
                    prev = existing_inquiries.get(bid_id)
                    if not isinstance(prev, dict):
                        prev = {}
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

                if snap.exists:
                    doc_ref.update(payload)
                else:
                    doc_ref.set(payload)
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
                    "inquiries": new_inquiries or [],
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

    def sync_to_comblopez(
        self,
        target_key: str,
        tenders: List[Dict[str, Any]],
        category_slug: Optional[str] = None,
        category_label: Optional[str] = None,
    ) -> int:
        """
        Syncs live government procurement tenders into COMBLOPEZ `scraped_leads` table,
        so that vendors instantly view live tenders in their Zivic Nexus dashboard
        (`zivicnexus.com/yourbusiness/dashboard`) under 'Live AI Lead Finder'.
        """
        comblopez_url = os.environ.get("COMBLOPEZ_SUPABASE_PROJECT2_URL", "").rstrip("/")
        comblopez_key = os.environ.get("COMBLOPEZ_SUPABASE_PROJECT2_SERVICE_ROLE_KEY", "").strip()
        if not comblopez_url or not comblopez_key or not tenders:
            return 0

        headers = {
            "apikey": comblopez_key,
            "Authorization": f"Bearer {comblopez_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }
        encoded_key = urllib.parse.quote(target_key)
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. Fetch existing leads for this target key to preserve earliest discovered_at
        existing_leads = []
        try:
            get_res = requests.get(
                f"{comblopez_url}/rest/v1/scraped_leads?select=metadata&email=eq.{encoded_key}",
                headers=headers,
                timeout=12,
            )
            if get_res.status_code == 200:
                data = get_res.json()
                if data and len(data) > 0:
                    raw_existing = data[0].get("metadata", {}).get("leads", [])
                    for l in raw_existing:
                        if isinstance(l, dict):
                            existing_leads.append(l)
        except Exception as e:
            logger.warning(f"[COMBLOPEZ] Could not fetch existing leads for {target_key}: {e}")

        # 2. Format tenders into standard COMBLOPEZ leads
        new_formatted_leads = []
        for t in tenders:
            bid_id = t.get("bid_id") or t.get("bid_number") or ""
            bid_no = t.get("bid_number") or bid_id
            title = t.get("title") or f"Government Procurement Tender — {bid_no}"
            dept = t.get("department") or "Government Department / PSU"
            city = t.get("city") or ""
            state = t.get("state") or "India"
            loc = f"{city}, {state}".strip(", ") if city else state
            score = t.get("match_score", 85)
            doc_url = t.get("doc_url") or f"https://bidplus.gem.gov.in/showbidDocument/{bid_id}"
            expiry = t.get("expiry_date") or ""
            source = t.get("source") or "Government e-Marketplace (GeM)"

            lead_item = {
                "name": f"{title} ({bid_no})",
                "title": title,
                "social_url": doc_url,
                "score": score,
                "status": "high_confidence",
                "category_slug": category_slug or t.get("matched_category_slug") or "general-marketplace",
                "category_label": category_label or t.get("category_name") or "Government Tenders",
                "source": source,
                "author": dept,
                "location": loc,
                "city": city,
                "state": state,
                "discovered_at": t.get("discovered_at") or t.get("found_date") or now_iso,
                "post_date": t.get("found_date") or now_iso,
                "expiry_date": expiry,
                "original_post_text": f"Official GeM Government Tender ({bid_no}) issued by {dept} ({loc}). Requirement: {title}. Closing Date: {expiry[:10] if expiry else 'Active'}.",
                "matched_intent": f"Govt Tender: {title[:90]} | Closes: {expiry[:10] if expiry else 'Open'}",
                "contacts": f"Department: {dept} | Bid: {bid_no}",
                "raw_details": t,
            }
            new_formatted_leads.append(lead_item)

        # 3. Preserve non-GeM AI web leads from existing_leads; for GeM tenders keep only active new_formatted_leads
        #    while preserving earliest discovered_at from existing_leads.
        existing_disc_by_key = {}
        retained_non_gem_leads = []
        for l in existing_leads:
            u = (l.get("social_url") or "").strip().lower()
            bid_key = (l.get("raw_details", {}).get("bid_number") or "").strip().lower()
            lead_key = bid_key or u
            if lead_key and l.get("discovered_at"):
                existing_disc_by_key[lead_key] = l.get("discovered_at")
            is_gov = (
                bool(bid_key)
                or "gem" in str(l.get("source") or "").lower()
                or str(l.get("matched_intent") or "").startswith("Govt Tender:")
                or "gem.gov.in" in u
                or "eprocure.gov.in" in u
                or "etenders.gov.in" in u
                or ".nic.in" in u
            )
            if not is_gov:
                retained_non_gem_leads.append(l)

        merged_by_key = {}
        for l in retained_non_gem_leads + new_formatted_leads:
            u = (l.get("social_url") or "").strip().lower()
            bid_key = (l.get("raw_details", {}).get("bid_number") or "").strip().lower()
            lead_key = bid_key or u
            if not lead_key:
                continue

            item_copy = dict(l)
            prev_disc = existing_disc_by_key.get(lead_key)
            if prev_disc and item_copy.get("discovered_at"):
                item_copy["discovered_at"] = min(prev_disc, item_copy["discovered_at"])
            elif prev_disc:
                item_copy["discovered_at"] = prev_disc

            if lead_key not in merged_by_key:
                merged_by_key[lead_key] = item_copy
            else:
                prev = merged_by_key[lead_key]
                earliest_disc = prev.get("discovered_at") or item_copy.get("discovered_at")
                if prev.get("discovered_at") and item_copy.get("discovered_at"):
                    earliest_disc = min(prev["discovered_at"], item_copy["discovered_at"])
                winner = item_copy if (item_copy.get("score") or 0) >= (prev.get("score") or 0) else dict(prev)
                winner["discovered_at"] = earliest_disc
                merged_by_key[lead_key] = winner

        all_leads = sorted(merged_by_key.values(), key=lambda x: x.get("score") or 0, reverse=True)

        # 4. Upsert into COMBLOPEZ table `scraped_leads`
        payload = {
            "email": target_key,
            "metadata": {
                "leads": all_leads,
                "category_slug": category_slug,
                "category_label": category_label,
                "is_government_source": True,
                "last_updated": now_iso,
            },
            "status": "bundled",
            "original_post_text": "Live Government Procurement & GeM Bids",
            "source": "gem_government_radar",
            "updated_at": now_iso,
        }

        try:
            post_res = requests.post(
                f"{comblopez_url}/rest/v1/scraped_leads?on_conflict=email",
                headers=headers,
                json=payload,
                timeout=15,
            )
            if post_res.status_code in [200, 201, 204]:
                logger.info(f"[COMBLOPEZ] Synced {len(all_leads)} government leads to '{target_key}'")
                return len(all_leads)
            else:
                logger.warning(f"[COMBLOPEZ] Upsert returned HTTP {post_res.status_code}: {post_res.text[:150]}")
        except Exception as e:
            logger.error(f"[COMBLOPEZ] Upsert failed for '{target_key}': {e}")

        return 0

    def prune_expired_inquiries(self, vendor_id: str) -> int:
        """Removes expired inquiries from `gem_vendor_feeds/{vendor_id}` where `expiry_date < now`."""
        if not self.db:
            return 0

        try:
            from firebase_admin import firestore
            doc_ref = self.db.collection("gem_vendor_feeds").document(vendor_id)
            snap = doc_ref.get()
            if not snap.exists:
                return 0

            data = snap.to_dict() or {}
            inquiries = data.get("inquiries", {})
            now_utc = datetime.now(timezone.utc)

            deletes = {}
            pruned_count = 0
            for bid_id, inq in inquiries.items():
                if not isinstance(inq, dict):
                    deletes[f"inquiries.{bid_id}"] = firestore.DELETE_FIELD
                    continue
                expiry = inq.get("expiry_date") or inq.get("expiry")
                if expiry:
                    try:
                        exp_dt = datetime.fromisoformat(expiry)
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                        if exp_dt < now_utc:
                            deletes[f"inquiries.{bid_id}"] = firestore.DELETE_FIELD
                            pruned_count += 1
                    except (ValueError, TypeError):
                        pass

            if deletes:
                doc_ref.update(deletes)
            return pruned_count
        except Exception as e:
            logger.error(f"Error pruning expired inquiries for {vendor_id}: {e}")
            return 0

