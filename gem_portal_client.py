"""
GeM Portal Scraper Client (V2 — CSRF AJAX + Solr Array Unwrapping + HTML Fallback)
Extracts live ongoing procurement tenders from Government e-Marketplace (bidplus.gem.gov.in).
"""

import re
import json
import time
import logging
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("GemPortalClient")

GEM_ALL_BIDS_URL = "https://bidplus.gem.gov.in/all-bids"
GEM_BIDS_DATA_URL = "https://bidplus.gem.gov.in/all-bids-data"
GEM_DOC_BASE_URL = "https://bidplus.gem.gov.in/showbidDocument"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
    "Referer": GEM_ALL_BIDS_URL,
    "Connection": "keep-alive",
}

INDIAN_STATES = [
    "Uttar Pradesh", "Maharashtra", "Delhi", "Karnataka", "Tamil Nadu",
    "Gujarat", "Rajasthan", "Madhya Pradesh", "West Bengal", "Bihar",
    "Telangana", "Andhra Pradesh", "Kerala", "Punjab", "Haryana",
    "Odisha", "Assam", "Jharkhand", "Chhattisgarh", "Uttarakhand",
    "Himachal Pradesh", "Jammu and Kashmir", "Goa", "Tripura", "Meghalaya",
    "Manipur", "Nagaland", "Arunachal Pradesh", "Mizoram", "Sikkim", "Chandigarh", "Puducherry",
]

MAJOR_CITIES_TO_STATE = {
    "lucknow": ("Lucknow", "Uttar Pradesh"),
    "varanasi": ("Varanasi", "Uttar Pradesh"),
    "banaras": ("Varanasi", "Uttar Pradesh"),
    "kashi": ("Varanasi", "Uttar Pradesh"),
    "kanpur": ("Kanpur", "Uttar Pradesh"),
    "prayagraj": ("Prayagraj", "Uttar Pradesh"),
    "allahabad": ("Prayagraj", "Uttar Pradesh"),
    "agra": ("Agra", "Uttar Pradesh"),
    "noida": ("Noida", "Uttar Pradesh"),
    "greater noida": ("Greater Noida", "Uttar Pradesh"),
    "ghaziabad": ("Ghaziabad", "Uttar Pradesh"),
    "gorakhpur": ("Gorakhpur", "Uttar Pradesh"),
    "meerut": ("Meerut", "Uttar Pradesh"),
    "bareilly": ("Bareilly", "Uttar Pradesh"),
    "jhansi": ("Jhansi", "Uttar Pradesh"),
    "ayodhya": ("Ayodhya", "Uttar Pradesh"),
    "aligarh": ("Aligarh", "Uttar Pradesh"),
    "moradabad": ("Moradabad", "Uttar Pradesh"),
    "mathura": ("Mathura", "Uttar Pradesh"),
    "saharanpur": ("Saharanpur", "Uttar Pradesh"),
    "muzaffarnagar": ("Muzaffarnagar", "Uttar Pradesh"),
    "firozabad": ("Firozabad", "Uttar Pradesh"),
    "raebareli": ("Raebareli", "Uttar Pradesh"),
    "rae bareli": ("Raebareli", "Uttar Pradesh"),
    "sultanpur": ("Sultanpur", "Uttar Pradesh"),
    "mirzapur": ("Mirzapur", "Uttar Pradesh"),
    "bhadohi": ("Bhadohi", "Uttar Pradesh"),
    "azamgarh": ("Azamgarh", "Uttar Pradesh"),
    "jaunpur": ("Jaunpur", "Uttar Pradesh"),
    "barabanki": ("Barabanki", "Uttar Pradesh"),
    "unnao": ("Unnao", "Uttar Pradesh"),
    "gautam buddha nagar": ("Noida", "Uttar Pradesh"),
    "new delhi": ("New Delhi", "Delhi"),
    "delhi": ("Delhi", "Delhi"),
    "gurugram": ("Gurugram", "Haryana"),
    "gurgaon": ("Gurugram", "Haryana"),
    "faridabad": ("Faridabad", "Haryana"),
    "mumbai": ("Mumbai", "Maharashtra"),
    "bombay": ("Mumbai", "Maharashtra"),
    "navi mumbai": ("Navi Mumbai", "Maharashtra"),
    "thane": ("Thane", "Maharashtra"),
    "pune": ("Pune", "Maharashtra"),
    "nagpur": ("Nagpur", "Maharashtra"),
    "nashik": ("Nashik", "Maharashtra"),
    "aurangabad": ("Aurangabad", "Maharashtra"),
    "bengaluru": ("Bengaluru", "Karnataka"),
    "bangalore": ("Bengaluru", "Karnataka"),
    "mysuru": ("Mysuru", "Karnataka"),
    "mysore": ("Mysuru", "Karnataka"),
    "mangaluru": ("Mangaluru", "Karnataka"),
    "mangalore": ("Mangaluru", "Karnataka"),
    "hubballi": ("Hubballi", "Karnataka"),
    "hyderabad": ("Hyderabad", "Telangana"),
    "secunderabad": ("Secunderabad", "Telangana"),
    "warangal": ("Warangal", "Telangana"),
    "khammam": ("Khammam", "Telangana"),
    "visakhapatnam": ("Visakhapatnam", "Andhra Pradesh"),
    "vijayawada": ("Vijayawada", "Andhra Pradesh"),
    "tirupati": ("Tirupati", "Andhra Pradesh"),
    "guntur": ("Guntur", "Andhra Pradesh"),
    "chennai": ("Chennai", "Tamil Nadu"),
    "madras": ("Chennai", "Tamil Nadu"),
    "coimbatore": ("Coimbatore", "Tamil Nadu"),
    "madurai": ("Madurai", "Tamil Nadu"),
    "salem": ("Salem", "Tamil Nadu"),
    "trichy": ("Trichy", "Tamil Nadu"),
    "kolkata": ("Kolkata", "West Bengal"),
    "calcutta": ("Kolkata", "West Bengal"),
    "howrah": ("Howrah", "West Bengal"),
    "siliguri": ("Siliguri", "West Bengal"),
    "durgapur": ("Durgapur", "West Bengal"),
    "ahmedabad": ("Ahmedabad", "Gujarat"),
    "surat": ("Surat", "Gujarat"),
    "vadodara": ("Vadodara", "Gujarat"),
    "baroda": ("Vadodara", "Gujarat"),
    "rajkot": ("Rajkot", "Gujarat"),
    "gandhinagar": ("Gandhinagar", "Gujarat"),
    "jaipur": ("Jaipur", "Rajasthan"),
    "udaipur": ("Udaipur", "Rajasthan"),
    "jodhpur": ("Jodhpur", "Rajasthan"),
    "kota": ("Kota", "Rajasthan"),
    "ajmer": ("Ajmer", "Rajasthan"),
    "bhopal": ("Bhopal", "Madhya Pradesh"),
    "indore": ("Indore", "Madhya Pradesh"),
    "gwalior": ("Gwalior", "Madhya Pradesh"),
    "jabalpur": ("Jabalpur", "Madhya Pradesh"),
    "patna": ("Patna", "Bihar"),
    "gaya": ("Gaya", "Bihar"),
    "muzaffarpur": ("Muzaffarpur", "Bihar"),
    "guwahati": ("Guwahati", "Assam"),
    "bhubaneswar": ("Bhubaneswar", "Odisha"),
    "cuttack": ("Cuttack", "Odisha"),
    "rourkela": ("Rourkela", "Odisha"),
    "ranchi": ("Ranchi", "Jharkhand"),
    "jamshedpur": ("Jamshedpur", "Jharkhand"),
    "dhanbad": ("Dhanbad", "Jharkhand"),
    "raipur": ("Raipur", "Chhattisgarh"),
    "kusmunda": ("Kusmunda", "Chhattisgarh"),
    "bilaspur": ("Bilaspur", "Chhattisgarh"),
    "korba": ("Korba", "Chhattisgarh"),
    "dehradun": ("Dehradun", "Uttarakhand"),
    "haridwar": ("Haridwar", "Uttarakhand"),
    "rishikesh": ("Rishikesh", "Uttarakhand"),
    "kochi": ("Kochi", "Kerala"),
    "thiruvananthapuram": ("Thiruvananthapuram", "Kerala"),
    "kozhikode": ("Kozhikode", "Kerala"),
    "ludhiana": ("Ludhiana", "Punjab"),
    "amritsar": ("Amritsar", "Punjab"),
    "jalandhar": ("Jalandhar", "Punjab"),
    "patiala": ("Patiala", "Punjab"),
    "mohali": ("Mohali", "Punjab"),
    "panipat": ("Panipat", "Haryana"),
    "ambala": ("Ambala", "Haryana"),
    "rohtak": ("Rohtak", "Haryana"),
    "karnal": ("Karnal", "Haryana"),
    "chandigarh": ("Chandigarh", "Chandigarh"),
    "shimla": ("Shimla", "Himachal Pradesh"),
    "jammu": ("Jammu", "Jammu and Kashmir"),
    "srinagar": ("Srinagar", "Jammu and Kashmir"),
    "panaji": ("Panaji", "Goa"),
    "puducherry": ("Puducherry", "Puducherry"),
    "pondicherry": ("Puducherry", "Puducherry"),
}

STATE_TO_SWEEP_HUBS = {
    "Uttar Pradesh": ["Uttar Pradesh", "Lucknow", "Noida", "Kanpur", "Varanasi", "Greater Noida", "Ghaziabad", "Prayagraj"],
    "Maharashtra": ["Maharashtra", "Mumbai", "Pune", "Nagpur", "Thane", "Nashik"],
    "Karnataka": ["Karnataka", "Bengaluru", "Mysuru", "Mangaluru"],
    "Delhi": ["Delhi", "New Delhi"],
    "Telangana": ["Telangana", "Hyderabad"],
    "Tamil Nadu": ["Tamil Nadu", "Chennai", "Coimbatore", "Madurai"],
    "West Bengal": ["West Bengal", "Kolkata", "Howrah"],
    "Gujarat": ["Gujarat", "Ahmedabad", "Surat", "Vadodara"],
    "Rajasthan": ["Rajasthan", "Jaipur", "Udaipur", "Jodhpur"],
    "Madhya Pradesh": ["Madhya Pradesh", "Indore", "Bhopal"],
    "Bihar": ["Bihar", "Patna"],
    "Haryana": ["Haryana", "Gurugram", "Faridabad"],
    "Punjab": ["Punjab", "Chandigarh", "Ludhiana", "Amritsar", "Mohali"],
    "Kerala": ["Kerala", "Kochi", "Thiruvananthapuram"],
    "Odisha": ["Odisha", "Bhubaneswar"],
    "Jharkhand": ["Jharkhand", "Ranchi"],
    "Uttarakhand": ["Uttarakhand", "Dehradun", "Haridwar"],
    "Chhattisgarh": ["Chhattisgarh", "Raipur"],
    "Assam": ["Assam", "Guwahati"],
    "Himachal Pradesh": ["Himachal Pradesh", "Shimla"],
    "Goa": ["Goa", "Panaji"],
    "Jammu and Kashmir": ["Jammu and Kashmir", "Srinagar", "Jammu"],
    "Puducherry": ["Puducherry"],
}

NATIONAL_INSTITUTION_TO_LOC = {
    "brihanmumbai": ("Maharashtra", "Mumbai"),
    "bmc": ("Maharashtra", "Mumbai"),
    "cidco": ("Maharashtra", "Navi Mumbai"),
    "midc": ("Maharashtra", "Mumbai"),
    "maharashtra": ("Maharashtra", None),
    "bbmp": ("Karnataka", "Bengaluru"),
    "bescom": ("Karnataka", "Bengaluru"),
    "ghmc": ("Telangana", "Hyderabad"),
    "mcd": ("Delhi", "Delhi"),
    "municipal corporation of delhi": ("Delhi", "Delhi"),
    "dda": ("Delhi", "Delhi"),
    "ndmc": ("Delhi", "New Delhi"),
    "gidc": ("Gujarat", None),
    "riico": ("Rajasthan", None),
    "gmada": ("Punjab", None),
    "puda": ("Punjab", None),
    "kmc": ("West Bengal", "Kolkata"),
    "aiims nagpur": ("Maharashtra", "Nagpur"),
    "aiims patna": ("Bihar", "Patna"),
    "aiims bhopal": ("Madhya Pradesh", "Bhopal"),
    "aiims jodhpur": ("Rajasthan", "Jodhpur"),
    "aiims rishikesh": ("Uttarakhand", "Rishikesh"),
    "aiims bhubaneswar": ("Odisha", "Bhubaneswar"),
    "aiims raipur": ("Chhattisgarh", "Raipur"),
    "aiims delhi": ("Delhi", "New Delhi"),
    "nagar nigam lucknow": ("Uttar Pradesh", "Lucknow"),
    "nagar nigam kanpur": ("Uttar Pradesh", "Kanpur"),
    "nagar nigam varanasi": ("Uttar Pradesh", "Varanasi"),
    "noida authority": ("Uttar Pradesh", "Noida"),
    "upsida": ("Uttar Pradesh", None),
    "upeida": ("Uttar Pradesh", None),
}

UP_INSTITUTION_TO_CITY = {
    "sgpgi": "Lucknow",
    "kgmu": "Lucknow",
    "king george": "Lucknow",
    "rmlims": "Lucknow",
    "babasaheb bhimrao": "Lucknow",
    "bbau": "Lucknow",
    "cdri": "Lucknow",
    "nbri": "Lucknow",
    "iitr": "Lucknow",
    "iim lucknow": "Lucknow",
    "upsrtc": "Lucknow",
    "uppcl": "Lucknow",
    "uppcb": "Lucknow",
    "uprvunl": "Lucknow",
    "up jal nigam": "Lucknow",
    "updesco": "Lucknow",
    "uprnn": "Lucknow",
    "iit kanpur": "Kanpur",
    "hbtu": "Kanpur",
    "hal kanpur": "Kanpur",
    "ordnance factory kanpur": "Kanpur",
    "dmsrde": "Kanpur",
    "csjmu": "Kanpur",
    "bhu": "Varanasi",
    "banaras hindu": "Varanasi",
    "iit bhu": "Varanasi",
    "dlw": "Varanasi",
    "blw": "Varanasi",
    "mnnit": "Prayagraj",
    "motilal nehru national institute": "Prayagraj",
    "high court allahabad": "Prayagraj",
    "gnida": "Greater Noida",
    "yeida": "Greater Noida",
    "aligarh muslim": "Aligarh",
    "amu": "Aligarh",
    "aiims gorakhpur": "Gorakhpur",
    "aiims raebareli": "Raebareli",
    "gail pata": "Auraiya",
    "gail, pata": "Auraiya",
    "auraiya": "Auraiya",
    "unchahar": "Raebareli",
    "anpara": "Sonbhadra",
    "obra": "Sonbhadra",
    "rihand": "Sonbhadra",
    "dadri": "Gautam Buddha Nagar",
    "narora": "Bulandshahr",
    "etender.up.nic.in": None,
    "up.gov.in": None,
}


def _first(val: Any, default: Any = None) -> Any:
    """Safely unwraps GeM Solr single-element arrays e.g. ['GEM/2026/B/8165931'] -> 'GEM/2026/B/8165931'."""
    if isinstance(val, list):
        return val[0] if len(val) > 0 else default
    return val if val is not None else default


def _dedupe_comma_items(raw_title: str) -> str:
    """Cleans repeated Solr item strings like 'Pants,Pants,Saree (V1),Saree (V1)' -> 'Pants, Saree (V1)'."""
    if not raw_title:
        return ""
    parts = [p.strip() for p in str(raw_title).split(",") if p.strip()]
    seen = set()
    unique_parts = []
    for p in parts:
        key = p.lower()
        if key not in seen:
            seen.add(key)
            unique_parts.append(p)
    cleaned = ", ".join(unique_parts)
    return cleaned[:220]


UP_REGION_KEYWORDS = {
    "uttar pradesh", "lucknow", "noida", "greater noida", "kanpur", "varanasi",
    "banaras", "ghaziabad", "prayagraj", "allahabad", "agra", "gorakhpur",
    "aligarh", "meerut", "bareilly", "ayodhya", "jhansi", "mathura", "raebareli",
}


def detect_indian_location(text: str) -> tuple[str, Optional[str]]:
    """Extracts (state, city) from department/ministry/tender/institution text."""
    lower = (text or "").lower()
    for city_key, (city_label, state_label) in sorted(MAJOR_CITIES_TO_STATE.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(city_key)}\b", lower):
            return state_label, city_label
    for inst_key, mapped_city in UP_INSTITUTION_TO_CITY.items():
        if re.search(rf"\b{re.escape(inst_key)}\b", lower):
            return "Uttar Pradesh", mapped_city
    for inst_key, (st_lbl, c_lbl) in sorted(NATIONAL_INSTITUTION_TO_LOC.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(inst_key)}\b", lower):
            return st_lbl, c_lbl
    if re.search(r"\b(uttar pradesh|u\.p\.)\b", lower):
        return "Uttar Pradesh", None
    for st in INDIAN_STATES:
        if re.search(rf"\b{re.escape(st.lower())}\b", lower):
            return st, None
    return "National / Central", None


def _split_combined_keyword_location(keyword: str) -> tuple[str, str]:
    """Splits combined GeM queries like 'Waste Uttar Pradesh' or 'Medical Consultant Pune' -> ('Waste', 'Uttar Pradesh')."""
    kw_low = (keyword or "").lower().strip()
    if not kw_low:
        return "", ""
    loc_candidates = sorted(
        [st.lower() for st in INDIAN_STATES] + list(MAJOR_CITIES_TO_STATE.keys()) + ["india"],
        key=len,
        reverse=True,
    )
    for loc in loc_candidates:
        if kw_low == loc:
            return "", loc
        if kw_low.endswith(" " + loc):
            base = kw_low[: -(len(loc) + 1)].strip()
            if base:
                return base, loc
    return kw_low, ""


def tender_matches_keyword(clean_title: str, department: str, keyword: str) -> bool:
    """
    Guards against GeM Solr stemmer and ministry-name false positives.
    - Pure regional sweeps (e.g. 'Uttar Pradesh', 'Maharashtra', 'Pune') match `clean_title + department`.
    - Combined '<service> <location>' queries (e.g. 'Waste Uttar Pradesh') verify BOTH base service on
      `clean_title` AND target location on `clean_title + department`.
    - Product/service keywords match strictly against `clean_title` (NEVER ministry/department)
      with domain-specific anti-false-positive blockers (e.g. Chartered Aircraft vs CA/GST,
      Doctor Blade vs Doctors, Waste Fittings vs Waste Management, Railway Coach vs Coaching).
    """
    if not keyword or not keyword.strip():
        return True
    kw_low = keyword.lower().strip()
    title_low = (clean_title or "").lower()

    base_kw, loc_suffix = _split_combined_keyword_location(kw_low)
    if loc_suffix and not base_kw:
        if loc_suffix == "india":
            return True
        full_hay = f"{title_low} {(department or '').lower()}"
        state, city = detect_indian_location(full_hay)
        return (
            state.lower() == loc_suffix
            or (city or "").lower() == loc_suffix
            or bool(re.search(rf"\b{re.escape(loc_suffix)}\b", full_hay))
        )
    if loc_suffix and base_kw:
        if not tender_matches_keyword(clean_title, department, base_kw):
            return False
        if loc_suffix == "india":
            return True
        full_hay = f"{title_low} {(department or '').lower()}"
        state, city = detect_indian_location(full_hay)
        return (
            state.lower() == loc_suffix
            or (city or "").lower() == loc_suffix
            or bool(re.search(rf"\b{re.escape(loc_suffix)}\b", full_hay))
        )

    if kw_low in UP_REGION_KEYWORDS:
        full_hay = f"{title_low} {(department or '').lower()}"
        state, _ = detect_indian_location(full_hay)
        return state == "Uttar Pradesh" or bool(re.search(rf"\b{re.escape(kw_low)}\b", full_hay))

    # Service-specific negative blockers on item title
    if any(t in kw_low for t in ["gst", "chartered accountant", "tax", "audit", "accounting"]):
        if any(bad in title_low for bad in ["chartered aircraft", "air charter", "helicopter", "ambulance", "passenger transport"]):
            if not any(ok in title_low for ok in ["chartered accountant", "gst", "tax", "statutory audit", "internal audit", "accounting"]):
                return False

    if any(t in kw_low for t in ["doctor", "medical consultant", "visiting consultant", "specialist doctor", "medical officer", "physician", "telemedicine"]):
        if any(bad in title_low for bad in [
            "doctor blade", "doctor's blade", "rotogravure", "spectrophotometer",
            "bronchoscope", "anaesthesia workstation", "syringe", "reagent", "bed sheet",
            "wheelchair", "stretcher", "patient monitor", "tablet", "capsule",
            "glass partition", "partition work", "conveyance", "tab cold", "syp ",
            "hostel of", "central heating", "peon", "unskilled", "head teacher",
            "ward boy", "sweeper", "high school",
        ]):
            return False
        doc_markers = [
            "medical consultant", "visiting consultant", "specialist doctor", "medical officer",
            "visiting doctor", "duty doctor", "hiring of doctor", "hiring of duty doctor",
            "deputation of doctors", "physician", "surgeon", "gynaecologist", "gynecologist",
            "cardiologist", "pediatrician", "pathologist", "radiologist", "anaesthetist",
            "telemedicine", "medical practitioner", "clinical service", "doctor service", "doctor hiring",
        ]
        if any(m in title_low for m in doc_markers):
            return True
        return bool(re.search(r"\bdoctors?\b", title_low)) and not any(
            w in title_low for w in ("blade", "conveyance", "partition", "hostel", "heating")
        )

    if any(t in kw_low for t in ["waste", "solid waste", "biomedical waste", "e-waste", "ewaste", "garbage", "recycling"]):
        if any(bad in title_low for bad in [
            "waste fittings", "waste pipe", "sink waste", "washbasin", "waste coupling",
            "cotton waste", "gate valve", "stop valve", "pillar cock", "health faucet",
        ]):
            return False
        if any(furn in title_low for furn in ["icu bed", "motorized bed", "manual bed", "bedside locker", "fowler bed"]):
            if not any(ok in title_low for ok in ["waste management", "waste disposal", "waste collection"]):
                return False
        waste_markers = [
            "solid waste", "waste management", "waste collection", "waste disposal",
            "biomedical waste", "bio-medical waste", "bio medical waste", "e-waste", "ewaste",
            "garbage", "waste container", "municipal waste", "hazardous waste", "recycling",
            "waste processing", "plastic waste", "waste segregation", "compost",
        ]
        return any(m in title_low for m in waste_markers)

    if any(t in kw_low for t in ["coaching", "skill training", "capacity building", "tuition", "vocational training", "faculty", "test prep", "training service"]):
        if any(bad in title_low for bad in [
            "pants", "shirt", "kurti", "kurta", "shoes", "rope light", "led ",
            "maplitho", "catering", "railway coach", "lhb coach", "icf coach",
            "coach cleaning", "coach attendant", "coach watering", "coach maintenance",
            "passenger coach", "ac coach", "coaching depot", "bogie", "bogies",
            "wheel set", "wheel sets", "rail coach", "loco",
            "weapon training simulator", "thematic murals", "drone training", "energy audit", "sewing machine",
        ]):
            return False
        coach_markers = [
            "coaching", "skill training", "capacity building", "vocational training",
            "training program", "training service", "training services", "faculty", "upskilling",
            "test preparation", "tuition", "mentor", "placement training", "computer training",
        ]
        return any(m in title_low for m in coach_markers)

    if any(t in kw_low for t in ["event", "exhibition", "conference", "stage", "audio visual", "tentage", "seminar"]):
        if any(bad in title_low for bad in [
            "conference table", "conference chair", "conference room", "fel -3 stage",
            "audio visual alarm", "haulage", "dummy core", "l-shape", "workstation table", "led display",
            "steam network", "rectification", "hot jobs", "maintenance of workshop",
        ]):
            return False

    if any(t in kw_low for t in ["pmc", "tender consultancy", "procurement", "bid management", "transaction advisory", "consultancy", "project management"]):
        if any(bad in title_low for bad in [
            "pmcc", "pmcxmc", "mcc panel", "vme module", "flash point", "switchgear",
            "facility management services", "facility management", "housekeeping",
            "o&m of electrical", "electrical work", "electrical works", "security manpower", "kusmunda",
        ]):
            if not any(ok in title_low for ok in ["tender consultancy", "procurement consultancy", "bid management"]):
                return False

    if any(t in kw_low for t in ["generator", "excavator", "machinery", "machine", "power cable", "transformer", "dg set"]):
        if any(bad in title_low for bad in [
            "motor insurance", "vehicle insurance", "insurance service",
            "smoke generator", "cypermethrin", "herbicide", "insecticide", "pesticide", "fogging",
        ]):
            return False

    if any(t in kw_low for t in ["solar", "photovoltaic"]):
        has_solar_pv = any(ok in title_low for ok in ["photovoltaic", "solar power plant", "rooftop solar", "roof top solar", "solar pv", "solar panel"])
        if not has_solar_pv and any(bad in title_low for bad in [
            "herbicide", "insecticide", "pesticide", "chevron sign", "retro reflective", "retro-reflective",
            "sign board", "signboard", "road stud", "cat eye", "reflective stud",
        ]):
            return False

    if any(t in kw_low for t in ["digital marketing", "social media", "seo", "advertising", "publicity", "iec", "creative agency"]):
        if any(bad in title_low for bad in [
            "amc/cmc for pre-owned", "amc / cmc for pre-owned", "for pre-owned",
            "printing unit", "offset printing", "printing press", "printing machine",
            "sheetfed offset", "colour offset", "color offset", "web offset",
        ]):
            return False
        media_markers = ["social media", "digital marketing", "advertising", "publicity", "media campaign", "branding", "content creation", "pr agency", "creative"]
        if not any(m in title_low for m in media_markers) and not re.search(r"\b(?:iec|seo)\b", title_low):
            return False

    if any(t in kw_low for t in ["premises", "office space", "real estate", "warehouse", "godown", "hostel", "guest house", "paying guest", "accommodation", "commercial space", "land allotment"]):
        if any(bad in title_low for bad in ["refrigerator", "two door", "washing machine", "air conditioner", "plc systems", "siemens", "laptop", "printer", "canteen"]):
            return False
        space_markers = ["premises", "building", "office space", "accommodation", "guest house", "hostel", "warehouse", "godown", "flat", "residential", "commercial space", "transit camp", "boarding", "land allotment", "e-auction"]
        if any(p in kw_low for p in ["premises", "office space", "commercial space", "real estate", "land allotment"]):
            return any(m in title_low for m in ["premises", "office space", "commercial space", "leasing of office", "building", "land allotment", "e-auction", "flat", "residential"])
        return any(m in title_low for m in space_markers)

    if "saree" in kw_low or "sari" in kw_low:
        return bool(re.search(r"\b(sarees?|saris?)\b", title_low))

    if re.search(rf"\b{re.escape(kw_low)}\b", title_low):
        return True

    stopwords = {
        "of", "for", "and", "in", "on", "to", "the", "india", "uttar", "pradesh",
        "services", "service", "works", "work", "hiring", "leasing", "selection",
        "installation", "maintenance", "repair", "supply", "commissioning", "design",
        "equipment", "equipments", "system", "systems", "plant", "panel",
    }
    tokens = [t for t in re.split(r"[^a-z0-9]+", kw_low) if t and t not in stopwords and (len(t) >= 3 or t in {"ca", "pg", "ro", "it", "dm"})]
    if not tokens:
        return kw_low in title_low

    def _tok_matches(tok: str) -> bool:
        if tok in ("consultancy", "consultant", "consultants"):
            return bool(re.search(r"\b(?:consultancy|consultants?)\b", title_low))
        if tok in ("box", "boxes"):
            return bool(re.search(r"\bbox(?:es)?\b", title_low))
        if tok.endswith("ies") and len(tok) >= 5:
            stem = tok[:-3]
            return bool(re.search(rf"\b{re.escape(stem)}(?:y|ies)\b", title_low))
        if tok.endswith("s") and len(tok) >= 4 and not tok.endswith(("ss", "us", "is")):
            stem = tok[:-1]
            return bool(re.search(rf"\b{re.escape(stem)}(?:s|es)?\b", title_low))
        if len(tok) == 4:
            pat = rf"\b{re.escape(tok)}(?:s|es|ing|ed|er)?\b"
        elif len(tok) < 4:
            pat = rf"\b{re.escape(tok)}(?:s|es)?\b"
        else:
            pat = rf"\b{re.escape(tok)}"
        return bool(re.search(pat, title_low))

    if len(tokens) == 1:
        return _tok_matches(tokens[0])
    return all(_tok_matches(tok) for tok in tokens[:4])


class GemPortalClient:
    def __init__(self, timeout: int = 18):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.csrf_token: Optional[str] = None
        self._csrf_fetched_at: float = 0.0

    def _parse_gem_date(self, date_val: Any) -> Optional[str]:
        """Converts GeM ISO or DD-MM-YYYY date strings to ISO 8601 UTC string."""
        raw = str(_first(date_val, "") or "").strip()
        if not raw:
            return None

        # GeM Solr returns ISO UTC strings like "2026-11-10T12:00:00Z"
        if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", raw):
            try:
                iso_norm = raw.replace("Z", "+00:00")
                dt = datetime.fromisoformat(iso_norm)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).isoformat()
            except ValueError:
                pass

        formats = [
            "%d-%m-%Y %I:%M %p",
            "%d-%m-%Y %H:%M:%S",
            "%d-%m-%Y %H:%M",
            "%d/%m/%Y %I:%M %p",
            "%Y-%m-%d %H:%M:%S",
            "%d-%b-%Y %I:%M %p",
            "%d-%m-%Y",
        ]
        ist_offset = timezone(timedelta(hours=5, minutes=30))
        for fmt in formats:
            try:
                dt = datetime.strptime(raw, fmt).replace(tzinfo=ist_offset)
                return dt.astimezone(timezone.utc).isoformat()
            except ValueError:
                continue
        return None

    def _clean_bid_id(self, bid_no: Any) -> str:
        """Normalizes bid number e.g. 'GEM/2026/B/8912301' -> 'GEM_2026_B_8912301'."""
        s = str(_first(bid_no, "") or "").strip()
        return re.sub(r"[^A-Za-z0-9_]", "_", s)

    def _ensure_csrf_token(self, force_refresh: bool = False) -> Optional[str]:
        """Initializes GeM session cookies & extracts `csrf_gem_cookie` / `csrf_bd_gem_nk`."""
        now = time.time()
        if not force_refresh and self.csrf_token and (now - self._csrf_fetched_at) < 600:
            return self.csrf_token

        try:
            res = self.session.get(GEM_ALL_BIDS_URL, timeout=self.timeout)
            cookie_tok = self.session.cookies.get("csrf_gem_cookie")
            if not cookie_tok and res.status_code == 200:
                m = re.search(r"csrf_bd_gem_nk['\"]?\s*[:=]\s*['\"]([a-f0-9]+)['\"]", res.text)
                if m:
                    cookie_tok = m.group(1)

            if cookie_tok:
                self.csrf_token = cookie_tok
                self._csrf_fetched_at = now
                return self.csrf_token
        except Exception as e:
            logger.warning(f"Failed to initialize GeM CSRF session: {e}")

        return None

    def fetch_all_bids_ajax(
        self,
        keyword: str = "",
        page: int = 1,
        sort: str = "Bid-Start-Date-Latest",
        category_slug: Optional[str] = None,
        days_window: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Queries GeM's official `/all-bids-data` endpoint using form-encoded payload + CSRF token.
        """
        csrf = self._ensure_csrf_token()
        if not csrf:
            return []

        payload_obj = {
            "page": page,
            "param": {
                "searchBid": (keyword or "").strip(),
                "searchType": "fullText",
            },
            "filter": {
                "bidStatusType": "ongoing_bids",
                "byType": "all",
                "highBidValue": "",
                "byEndDate": {"from": "", "to": ""},
                "sort": sort,
            },
        }

        post_headers = {
            **HEADERS,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://bidplus.gem.gov.in",
            "Referer": GEM_ALL_BIDS_URL,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }

        for attempt in range(2):
            try:
                form_data = {
                    "payload": json.dumps(payload_obj),
                    "csrf_bd_gem_nk": csrf,
                }
                res = self.session.post(
                    GEM_BIDS_DATA_URL,
                    data=form_data,
                    headers=post_headers,
                    timeout=self.timeout,
                )

                if res.status_code == 403 and attempt == 0:
                    csrf = self._ensure_csrf_token(force_refresh=True)
                    if not csrf:
                        return []
                    continue

                if res.status_code != 200:
                    logger.debug(f"GeM AJAX returned status {res.status_code} for keyword='{keyword}'")
                    return []

                data = res.json()
                docs = data.get("response", {}).get("response", {}).get("docs", []) or []
                now_utc = datetime.now(timezone.utc)
                now_iso = now_utc.isoformat()
                extracted: List[Dict[str, Any]] = []

                for doc in docs:
                    bid_number = str(_first(doc.get("b_bid_number"), "") or "").strip()
                    if not bid_number:
                        continue

                    bid_id = self._clean_bid_id(bid_number)
                    numeric_id = str(_first(doc.get("b_id")) or doc.get("id") or "").strip()

                    raw_cat_title = (
                        _first(doc.get("bd_category_name"))
                        or _first(doc.get("b_category_name"))
                        or _first(doc.get("title"))
                        or ""
                    )
                    clean_title = _dedupe_comma_items(str(raw_cat_title)) or f"GeM Procurement: {bid_number}"

                    min_name = str(_first(doc.get("ba_official_details_minName"), "") or "").strip()
                    dept_name = str(_first(doc.get("ba_official_details_deptName"), "") or "").strip()
                    if min_name and dept_name and min_name.lower() not in dept_name.lower():
                        department = f"{dept_name} ({min_name})"[:160]
                    else:
                        department = (dept_name or min_name or "Government Ministry / PSU")[:160]

                    # Guard against GeM Solr stemmer & ministry-name false positives
                    if keyword and not tender_matches_keyword(clean_title, department, keyword):
                        continue

                    expiry_raw = (
                        _first(doc.get("final_end_date_sort"))
                        or _first(doc.get("final_end_date"))
                        or _first(doc.get("b_bid_end_date"))
                    )
                    expiry_iso = self._parse_gem_date(expiry_raw)
                    if expiry_iso:
                        try:
                            if datetime.fromisoformat(expiry_iso) <= now_utc:
                                # Skip already expired tenders at extraction time
                                continue
                        except ValueError:
                            pass
                    else:
                        expiry_iso = (now_utc + timedelta(days=14)).isoformat()

                    start_raw = _first(doc.get("final_start_date_sort")) or _first(doc.get("b_bid_start_date"))
                    start_iso = self._parse_gem_date(start_raw)
                    if days_window and start_iso:
                        try:
                            st_dt = datetime.fromisoformat(start_iso)
                            if (now_utc - st_dt).total_seconds() > int(days_window) * 86400 + 3600:
                                continue
                        except ValueError:
                            pass

                    qty_raw = _first(doc.get("b_total_quantity"), 1)
                    try:
                        quantity = max(1, int(qty_raw))
                    except (ValueError, TypeError):
                        quantity = 1

                    # Note: ba_official_details_state is absent in live Solr docs; location lives in deptName/minName/title
                    explicit_state = str(_first(doc.get("ba_official_details_state"), "") or "").strip()
                    explicit_city = str(_first(doc.get("ba_official_details_city"), "") or "").strip()
                    inferred_state, inferred_city = detect_indian_location(f"{dept_name} {min_name} {clean_title}")
                    state = explicit_state or inferred_state
                    city = explicit_city or inferred_city

                    raw_cat_code = str(_first(doc.get("b_cat_id"), "") or "").split(",")[0].strip()
                    is_high_val = bool(_first(doc.get("is_high_value"), False))

                    # Calibrate procurement relevance score (68 - 96)
                    score = 76
                    if keyword:
                        kw_low = keyword.lower()
                        if kw_low in clean_title.lower():
                            score += 12
                        elif any(tok in clean_title.lower() for tok in kw_low.split() if len(tok) >= 4):
                            score += 7
                    if is_high_val or quantity >= 10:
                        score += 4
                    if city or state != "National / Central":
                        score += 3
                    score = min(96, max(65, score))

                    doc_target = numeric_id if numeric_id else bid_id
                    doc_url = f"{GEM_DOC_BASE_URL}/{doc_target}"

                    extracted.append({
                        "bid_id": bid_id,
                        "bid_number": bid_number,
                        "title": clean_title,
                        "category_code": raw_cat_code or "gem-services",
                        "category_name": f"GeM {keyword.title()}" if keyword else "GeM Government Procurement",
                        "matched_category_slug": category_slug,
                        "department": department,
                        "quantity": quantity,
                        "state": state,
                        "city": city,
                        "found_date": start_iso or now_iso,
                        "start_date": start_iso or now_iso,
                        "discovered_at": now_iso,
                        "expiry_date": expiry_iso,
                        "status": "ACTIVE",
                        "match_score": score,
                        "doc_url": doc_url,
                        "raw_details": {
                            "source": "gem_portal_ajax",
                            "gem_numeric_id": numeric_id,
                            "search_keyword": keyword or "all_bids",
                            "is_high_value": is_high_val,
                            "start_date": start_raw,
                            "start_date_iso": start_iso,
                        },
                    })

                logger.info(
                    f"[GeM AJAX] Extracted {len(extracted)} ongoing bids (page={page}, keyword='{keyword or 'ALL'}')"
                )
                return extracted
            except Exception as e:
                logger.warning(f"GeM AJAX error (attempt {attempt + 1}) for '{keyword}': {e}")

        return []

    def fetch_all_bids_html(
        self,
        page: int = 1,
        keyword: Optional[str] = None,
        sort: str = "Bid-Start-Date-Latest",
        days_window: Optional[int] = None,
        try_ajax: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Primary entry for general/page GeM bids: uses CSRF AJAX first (since bidplus.gem.gov.in
        is an AJAX SPA), then falls back to static HTML parsing if needed.
        """
        if try_ajax:
            ajax_bids = self.fetch_all_bids_ajax(keyword=keyword or "", page=page, sort=sort, days_window=days_window)
            if ajax_bids:
                return ajax_bids

        params: Dict[str, Any] = {}
        if page > 1:
            params["page"] = page
        if keyword:
            params["search"] = keyword

        try:
            response = self.session.get(GEM_ALL_BIDS_URL, params=params, timeout=self.timeout)
            if response.status_code != 200:
                return []

            soup = BeautifulSoup(response.text, "html.parser")
            bids: List[Dict[str, Any]] = []
            card_elements = soup.select(".card, .border.block, .bid-card, .col-md-12.border")
            if not card_elements:
                card_elements = soup.find_all(
                    lambda tag: tag.name in ["div", "li"] and "GEM/" in tag.get_text()
                )

            now_iso = datetime.now(timezone.utc).isoformat()
            for card in card_elements:
                text = card.get_text(separator=" ", strip=True)
                bid_no_match = re.search(r"GEM/\d{4}/[BR]/\d+", text)
                if not bid_no_match:
                    continue

                bid_number = bid_no_match.group(0)
                bid_id = self._clean_bid_id(bid_number)
                items_match = re.search(r"Items?:\s*([^–\n\r|]+)", text, re.IGNORECASE)
                title = items_match.group(1).strip() if items_match else f"Procurement Contract: {bid_number}"
                dept_match = re.search(r"(?:Department|Ministry/State):\s*([^–\n\r|]+)", text, re.IGNORECASE)
                department = dept_match.group(1).strip() if dept_match else "Government Department"
                qty_match = re.search(r"Quantity:\s*(\d+)", text, re.IGNORECASE)
                quantity = int(qty_match.group(1)) if qty_match else 1

                end_date_match = re.search(
                    r"(?:End Date|Closing Date|Bid End Date):\s*(\d{2}[-/]\d{2}[-/]\d{4}\s*(?:\d{2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)?)",
                    text,
                    re.IGNORECASE,
                )
                expiry_iso = self._parse_gem_date(end_date_match.group(1)) if end_date_match else None
                if not expiry_iso:
                    expiry_iso = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()

                a_tag = card.find("a", href=re.compile(r"showbidDocument|download", re.IGNORECASE))
                doc_link = (
                    urllib.parse.urljoin("https://bidplus.gem.gov.in", a_tag.get("href"))
                    if a_tag and a_tag.get("href")
                    else f"{GEM_DOC_BASE_URL}/{bid_id}"
                )

                state, city = detect_indian_location(text)
                bids.append({
                    "bid_id": bid_id,
                    "bid_number": bid_number,
                    "title": title[:200],
                    "category_code": "gem-services",
                    "category_name": "GeM Government Procurement",
                    "department": department[:150],
                    "quantity": quantity,
                    "state": state,
                    "city": city,
                    "found_date": now_iso,
                    "discovered_at": now_iso,
                    "expiry_date": expiry_iso,
                    "status": "ACTIVE",
                    "match_score": 75,
                    "doc_url": doc_link,
                    "raw_details": {"source": "gem_portal_html", "raw_snippet": text[:300]},
                })

            return bids
        except Exception as e:
            logger.error(f"Error scraping GeM portal HTML: {e}")
            return []

    def scrape_keyword_tenders(
        self,
        keyword: str,
        category_slug: Optional[str] = None,
        max_pages: int = 1,
        sort: str = "Bid-Start-Date-Latest",
        days_window: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Scrapes ongoing GeM bids matching `keyword` across up to `max_pages`."""
        collected: Dict[str, Dict[str, Any]] = {}
        for p in range(1, max_pages + 1):
            batch = self.fetch_all_bids_ajax(
                keyword=keyword,
                page=p,
                sort=sort,
                category_slug=category_slug,
                days_window=days_window,
            )
            for item in batch:
                bid_id = item.get("bid_id")
                if bid_id and bid_id not in collected:
                    collected[bid_id] = item
            if len(batch) < 10:
                break
            if p < max_pages:
                time.sleep(0.5)

        if not collected:
            for item in self.fetch_all_bids_html(page=1, keyword=keyword, sort=sort, days_window=days_window, try_ajax=False):
                bid_id = item.get("bid_id")
                if bid_id and bid_id not in collected:
                    if category_slug:
                        item["matched_category_slug"] = category_slug
                    collected[bid_id] = item

        return list(collected.values())
