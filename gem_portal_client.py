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
    "Manipur", "Nagaland", "Arunachal Pradesh", "Mizoram", "Sikkim", "Chandigarh",
]

MAJOR_CITIES_TO_STATE = {
    "lucknow": ("Lucknow", "Uttar Pradesh"),
    "varanasi": ("Varanasi", "Uttar Pradesh"),
    "banaras": ("Varanasi", "Uttar Pradesh"),
    "kanpur": ("Kanpur", "Uttar Pradesh"),
    "prayagraj": ("Prayagraj", "Uttar Pradesh"),
    "allahabad": ("Prayagraj", "Uttar Pradesh"),
    "agra": ("Agra", "Uttar Pradesh"),
    "noida": ("Noida", "Uttar Pradesh"),
    "ghaziabad": ("Ghaziabad", "Uttar Pradesh"),
    "gorakhpur": ("Gorakhpur", "Uttar Pradesh"),
    "meerut": ("Meerut", "Uttar Pradesh"),
    "bareilly": ("Bareilly", "Uttar Pradesh"),
    "jhansi": ("Jhansi", "Uttar Pradesh"),
    "ayodhya": ("Ayodhya", "Uttar Pradesh"),
    "new delhi": ("New Delhi", "Delhi"),
    "delhi": ("Delhi", "Delhi"),
    "mumbai": ("Mumbai", "Maharashtra"),
    "pune": ("Pune", "Maharashtra"),
    "nagpur": ("Nagpur", "Maharashtra"),
    "bengaluru": ("Bengaluru", "Karnataka"),
    "bangalore": ("Bengaluru", "Karnataka"),
    "hyderabad": ("Hyderabad", "Telangana"),
    "chennai": ("Chennai", "Tamil Nadu"),
    "kolkata": ("Kolkata", "West Bengal"),
    "ahmedabad": ("Ahmedabad", "Gujarat"),
    "surat": ("Surat", "Gujarat"),
    "jaipur": ("Jaipur", "Rajasthan"),
    "bhopal": ("Bhopal", "Madhya Pradesh"),
    "indore": ("Indore", "Madhya Pradesh"),
    "patna": ("Patna", "Bihar"),
    "guwahati": ("Guwahati", "Assam"),
    "bhubaneswar": ("Bhubaneswar", "Odisha"),
    "ranchi": ("Ranchi", "Jharkhand"),
    "raipur": ("Raipur", "Chhattisgarh"),
    "dehradun": ("Dehradun", "Uttarakhand"),
    "chandigarh": ("Chandigarh", "Chandigarh"),
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


def detect_indian_location(text: str) -> tuple[str, Optional[str]]:
    """Extracts (state, city) from department/ministry/tender text."""
    lower = (text or "").lower()
    for city_key, (city_label, state_label) in MAJOR_CITIES_TO_STATE.items():
        if re.search(rf"\b{re.escape(city_key)}\b", lower):
            return state_label, city_label
    for st in INDIAN_STATES:
        if st.lower() in lower:
            return st, None
    return "National / Central", None


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
        sort: str = "Bid-End-Date-Latest",
        category_slug: Optional[str] = None,
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

                    # Guard against GeM Solr stemmer false positives (e.g. 'Handloom' stemming to 'HAND SIGNAL FLAG')
                    if keyword:
                        kw_tokens = [
                            t for t in re.split(r"[^a-z0-9]+", keyword.lower())
                            if len(t) >= 4 and t not in {"uttar", "pradesh", "delhi", "india", "service", "services"}
                        ]
                        if kw_tokens:
                            hay_check = f"{clean_title} {department}".lower()
                            if not any(re.search(rf"\b{re.escape(tok)}", hay_check) for tok in kw_tokens):
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

                    qty_raw = _first(doc.get("b_total_quantity"), 1)
                    try:
                        quantity = max(1, int(qty_raw))
                    except (ValueError, TypeError):
                        quantity = 1

                    explicit_state = str(_first(doc.get("ba_official_details_state"), "") or "").strip()
                    explicit_city = str(_first(doc.get("ba_official_details_city"), "") or "").strip()
                    inferred_state, inferred_city = detect_indian_location(f"{department} {clean_title}")
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
                        "found_date": now_iso,
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
                            "start_date": _first(doc.get("final_start_date_sort")),
                        },
                    })

                logger.info(
                    f"[GeM AJAX] Extracted {len(extracted)} ongoing bids (page={page}, keyword='{keyword or 'ALL'}')"
                )
                return extracted
            except Exception as e:
                logger.warning(f"GeM AJAX error (attempt {attempt + 1}) for '{keyword}': {e}")

        return []

    def fetch_all_bids_html(self, page: int = 1, keyword: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Primary entry for general/page GeM bids: uses CSRF AJAX first (since bidplus.gem.gov.in
        is an AJAX SPA), then falls back to static HTML parsing if needed.
        """
        ajax_bids = self.fetch_all_bids_ajax(keyword=keyword or "", page=page)
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
    ) -> List[Dict[str, Any]]:
        """Scrapes ongoing GeM bids matching `keyword` across up to `max_pages`."""
        collected: Dict[str, Dict[str, Any]] = {}
        for p in range(1, max_pages + 1):
            batch = self.fetch_all_bids_ajax(
                keyword=keyword,
                page=p,
                category_slug=category_slug,
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
            for item in self.fetch_all_bids_html(page=1, keyword=keyword):
                bid_id = item.get("bid_id")
                if bid_id and bid_id not in collected:
                    if category_slug:
                        item["matched_category_slug"] = category_slug
                    collected[bid_id] = item

        return list(collected.values())
