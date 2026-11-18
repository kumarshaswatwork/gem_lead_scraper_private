"""
Free Zero-Credit Public Government Tender Channels for gem_lead_scraper:
  1. GePNIC / NIC eProcurement Public Active Tender Tables (no CAPTCHA, homepage only):
     - Uttar Pradesh (etender.up.nic.in/nicgep/app)
     - Central Public Procurement Portal (eprocure.gov.in/eprocure/app)
     - NIC Central/State Active Tenders (etenders.gov.in/eprocure/app)
     - Dynamic state portals (mahatenders.gov.in, mptenders.gov.in, eproc.rajasthan.gov.in, etc.)
  2. Bing News RSS & Google News RSS for municipal/civic/PSU NITs & tenders.
Never touches CAPTCHA-protected search pages or bypasses bot controls.
"""

import re
import time
import hashlib
import logging
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from gem_portal_client import detect_indian_location, tender_matches_keyword
from firebase_sync import classify_tender, _has_excluded_tender_noise

logger = logging.getLogger("FreeTenderSources")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

STATE_GEPNIC_PORTALS: Dict[str, List[Tuple[str, str, str]]] = {
    "Uttar Pradesh": [
        ("https://etender.up.nic.in/nicgep/app", "Uttar Pradesh e-Procurement (etender.up.nic.in)", "Uttar Pradesh"),
    ],
    "Maharashtra": [
        ("https://mahatenders.gov.in/nicgep/app", "Maharashtra e-Tenders (mahatenders.gov.in)", "Maharashtra"),
    ],
    "Madhya Pradesh": [
        ("https://mptenders.gov.in/nicgep/app", "Madhya Pradesh e-Procurement (mptenders.gov.in)", "Madhya Pradesh"),
    ],
    "Rajasthan": [
        ("https://eproc.rajasthan.gov.in/nicgep/app", "Rajasthan e-Procurement (eproc.rajasthan.gov.in)", "Rajasthan"),
    ],
    "Haryana": [
        ("https://etenders.hry.nic.in/nicgep/app", "Haryana e-Tendering (etenders.hry.nic.in)", "Haryana"),
    ],
    "Kerala": [
        ("https://etenders.kerala.gov.in/nicgep/app", "Kerala e-Tenders (etenders.kerala.gov.in)", "Kerala"),
    ],
    "West Bengal": [
        ("https://wbtenders.gov.in/nicgep/app", "West Bengal e-Tenders (wbtenders.gov.in)", "West Bengal"),
    ],
    "Tamil Nadu": [
        ("https://tntenders.gov.in/nicgep/app", "Tamil Nadu e-Tenders (tntenders.gov.in)", "Tamil Nadu"),
    ],
    "Odisha": [
        ("https://tendersodisha.gov.in/nicgep/app", "Odisha e-Tenders (tendersodisha.gov.in)", "Odisha"),
    ],
    "Punjab": [
        ("https://eproc.punjab.gov.in/nicgep/app", "Punjab e-Procurement (eproc.punjab.gov.in)", "Punjab"),
    ],
    "Delhi": [
        ("https://govtprocurement.delhi.gov.in/nicgep/app", "Delhi Govt e-Procurement (govtprocurement.delhi.gov.in)", "Delhi"),
    ],
    "Jharkhand": [
        ("https://jharkhandtenders.gov.in/nicgep/app", "Jharkhand e-Tenders (jharkhandtenders.gov.in)", "Jharkhand"),
    ],
    "Uttarakhand": [
        ("https://uktenders.gov.in/nicgep/app", "Uttarakhand e-Tenders (uktenders.gov.in)", "Uttarakhand"),
    ],
    "Assam": [
        ("https://assamtenders.gov.in/nicgep/app", "Assam e-Tenders (assamtenders.gov.in)", "Assam"),
    ],
    "Himachal Pradesh": [
        ("https://hptenders.gov.in/nicgep/app", "Himachal Pradesh e-Tenders (hptenders.gov.in)", "Himachal Pradesh"),
    ],
    "Goa": [
        ("https://eprocure.goa.gov.in/nicgep/app", "Goa e-Procurement (eprocure.goa.gov.in)", "Goa"),
    ],
    "Jammu and Kashmir": [
        ("https://jktenders.gov.in/nicgep/app", "J&K e-Tenders (jktenders.gov.in)", "Jammu and Kashmir"),
    ],
    "Chandigarh": [
        ("https://etenders.chd.nic.in/nicgep/app", "Chandigarh e-Tenders (etenders.chd.nic.in)", "Chandigarh"),
    ],
    "Puducherry": [
        ("https://pudutenders.gov.in/nicgep/app", "Puducherry e-Tenders (pudutenders.gov.in)", "Puducherry"),
    ],
}

CORE_CENTRAL_PORTALS: List[Tuple[str, str, str]] = [
    ("https://eprocure.gov.in/eprocure/app", "Central Public Procurement Portal (CPPP)", "National / Central"),
    ("https://etenders.gov.in/eprocure/app", "NIC Central/State e-Tenders Portal (etenders.gov.in)", "National / Central"),
]

_GEPNIC_PORTAL_CACHE: Dict[str, List[Dict[str, Any]]] = {}
_GNEWS_LAST_CALL_TS = 0.0
_GNEWS_MIN_INTERVAL = 2.1


def _parse_gepnic_date(raw: str) -> Optional[str]:
    s = (raw or "").strip()
    if not s:
        return None
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    for fmt in ("%d-%b-%Y %I:%M %p", "%d-%b-%Y %H:%M", "%d-%m-%Y %I:%M %p", "%d-%m-%Y %H:%M", "%d/%m/%Y %I:%M %p", "%d-%b-%Y"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=ist_tz)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def parse_gepnic_homepage_html(
    html_text: str,
    portal_url: str,
    source_label: str,
    default_state: str = "National / Central",
) -> List[Dict[str, Any]]:
    """Parses active tender rows from NIC GePNIC / eProcure homepage HTML without CAPTCHA interaction."""
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.select_one("#activeTenders") or soup.select_one("table.list_table")
    if not table:
        return []

    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    parsed_rows: List[Dict[str, Any]] = []

    for tr in table.select("tr"):
        tds = tr.select("td")
        if len(tds) < 3:
            continue
        raw_title = re.sub(r"^\d+\.\s*", "", tds[0].get_text(" ", strip=True)).strip()
        ref_no = tds[1].get_text(" ", strip=True) if len(tds) > 1 else ""
        closing_raw = tds[2].get_text(" ", strip=True) if len(tds) > 2 else ""
        if not raw_title or len(raw_title) < 6:
            continue

        expiry_iso = _parse_gepnic_date(closing_raw)
        if expiry_iso:
            try:
                if datetime.fromisoformat(expiry_iso) <= now_utc:
                    continue
            except ValueError:
                pass
        else:
            expiry_iso = (now_utc + timedelta(days=14)).isoformat()

        a_tag = tr.find("a", href=True)
        doc_url = urllib.parse.urljoin(portal_url, a_tag["href"]) if (a_tag and a_tag.get("href")) else portal_url

        bid_number = ref_no[:50] if ref_no else f"GEPNIC/{now_utc.year}/{hashlib.md5(raw_title.encode()).hexdigest()[:8].upper()}"
        bid_id = re.sub(r"[^A-Za-z0-9_]", "_", f"GEPNIC_{bid_number}")[:64]

        inferred_state, inferred_city = detect_indian_location(f"{raw_title} {ref_no}")
        state = inferred_state if inferred_state != "National / Central" else default_state
        city = inferred_city

        item = {
            "bid_id": bid_id,
            "bid_number": bid_number,
            "title": raw_title[:220],
            "category_code": "gem-services",
            "category_name": "GePNIC Government Tender",
            "matched_category_slug": None,
            "department": f"{source_label}"[:160],
            "quantity": 1,
            "state": state,
            "city": city,
            "found_date": now_iso,
            "start_date": now_iso,
            "discovered_at": now_iso,
            "expiry_date": expiry_iso,
            "status": "ACTIVE",
            "match_score": 82 if state != "National / Central" else 76,
            "doc_url": doc_url,
            "source": source_label,
            "raw_details": {
                "source": "gepnic_homepage",
                "portal_url": portal_url,
                "ref_no": ref_no,
                "closing_raw": closing_raw,
            },
        }
        inferred_cat = classify_tender(item)
        if inferred_cat:
            item["matched_category_slug"] = inferred_cat
        parsed_rows.append(item)

    return parsed_rows


def _fetch_portal_active_tenders(portal_url: str, source_label: str, default_state: str) -> List[Dict[str, Any]]:
    if portal_url in _GEPNIC_PORTAL_CACHE:
        return _GEPNIC_PORTAL_CACHE[portal_url]
    try:
        res = requests.get(portal_url, headers=_HEADERS, timeout=14)
        if res.status_code == 200:
            rows = parse_gepnic_homepage_html(res.text, portal_url, source_label, default_state)
            _GEPNIC_PORTAL_CACHE[portal_url] = rows
            logger.info(f"[GePNIC Free] {portal_url} -> {len(rows)} active tenders")
            return rows
    except Exception as e:
        logger.debug(f"GePNIC fetch warning ({portal_url}): {e}")
    _GEPNIC_PORTAL_CACHE[portal_url] = []
    return []


def get_gepnic_portals_for_location(location_hint: str = "India") -> List[Tuple[str, str, str]]:
    """Maps any Indian city, state, or Pan-India target cleanly to matching state + central GePNIC portals."""
    portals: List[Tuple[str, str, str]] = list(CORE_CENTRAL_PORTALS)
    raw_loc = (location_hint or "").strip()
    t_state, _ = detect_indian_location(raw_loc)
    if t_state != "National / Central" and t_state in STATE_GEPNIC_PORTALS:
        portals = list(STATE_GEPNIC_PORTALS[t_state]) + portals
    elif raw_loc.lower() in ("india", "pan india", "pan-india", "all india", ""):
        portals = list(STATE_GEPNIC_PORTALS["Uttar Pradesh"]) + list(STATE_GEPNIC_PORTALS["Maharashtra"]) + portals
    return portals


def fetch_gepnic_tenders_for_target(
    keywords: List[str],
    category_slug: Optional[str] = None,
    location_hint: str = "India",
) -> List[Dict[str, Any]]:
    """
    Collects active tenders from central + state GePNIC homepages matching `keywords` or `category_slug`.
    """
    portals = get_gepnic_portals_for_location(location_hint)
    t_state, _ = detect_indian_location(location_hint or "")

    matched: Dict[str, Dict[str, Any]] = {}
    for p_url, p_label, p_state in portals:
        rows = _fetch_portal_active_tenders(p_url, p_label, p_state)
        for row in rows:
            if t_state != "National / Central" and row.get("state") not in (t_state, "National / Central", "", None):
                continue
            t_check = f"{row.get('title', '')} {row.get('category_name', '')}".lower()
            if category_slug and _has_excluded_tender_noise(t_check, category_slug):
                continue
            cat_match = bool(category_slug and row.get("matched_category_slug") == category_slug)
            kw_match = any(tender_matches_keyword(row.get("title", ""), row.get("department", ""), kw) for kw in (keywords or []) if kw)
            if not keywords or cat_match or kw_match:
                copy = dict(row)
                if category_slug and not copy.get("matched_category_slug"):
                    copy["matched_category_slug"] = category_slug
                bid_id = copy["bid_id"]
                if bid_id not in matched:
                    matched[bid_id] = copy

    return list(matched.values())


# ---------------- Bing News RSS & Google News RSS (Free Civic/Municipal Tenders) ----------------

def _unwrap_bing_url(url: str) -> str:
    if "bing.com/news/apiclick" in (url or "").lower():
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            if "url" in qs and qs["url"] and qs["url"][0].startswith("http"):
                return qs["url"][0]
        except Exception:
            pass
    return url


def _parse_rss_dt(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_rss_tender_leads(
    keyword: str,
    category_slug: Optional[str] = None,
    location_hint: str = "India",
    days_window: int = 30,
) -> List[Dict[str, Any]]:
    """
    Free supplemental RSS discovery (Bing News RSS + Google News RSS) for municipal, PSU,
    and state tender/NIT notices without spending SERP API credits.
    """
    global _GNEWS_LAST_CALL_TS
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    days = max(1, int(days_window or 30))
    loc_term = (location_hint or "India").strip()
    if loc_term.lower() in ("pan india", "pan-india", "all india"):
        loc_term = "India"

    q_bing = f"{keyword} {loc_term} (tender OR NIT OR RFP OR GeM)"
    q_google = f"{keyword} {loc_term} tender OR NIT OR RFP when:{days}d"

    feeds: List[Tuple[str, Dict[str, Any], str]] = [
        ("https://www.bing.com/news/search", {"q": q_bing, "format": "rss", "cc": "IN"}, "Bing News RSS (Civic/Govt Radar)"),
        ("https://news.google.com/rss/search", {"q": q_google, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}, "Google News RSS (Civic/Govt Radar)"),
    ]

    tenders: Dict[str, Dict[str, Any]] = {}
    for feed_url, params, src_label in feeds:
        try:
            if "news.google.com" in feed_url:
                elapsed = time.time() - _GNEWS_LAST_CALL_TS
                if elapsed < _GNEWS_MIN_INTERVAL:
                    time.sleep(_GNEWS_MIN_INTERVAL - elapsed)
                _GNEWS_LAST_CALL_TS = time.time()

            res = requests.get(feed_url, params=params, headers=_HEADERS, timeout=12)
            if res.status_code != 200:
                continue
            root = ET.fromstring(res.text)
            for item in root.findall(".//item")[:12]:
                title = (item.findtext("title") or "").strip()
                link = _unwrap_bing_url((item.findtext("link") or "").strip())
                pub_raw = (item.findtext("pubDate") or "").strip()
                desc_html = (item.findtext("description") or "").strip()
                desc_txt = BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True) if desc_html else ""
                pub_dt = _parse_rss_dt(pub_raw)
                if not pub_dt or (now_utc - pub_dt).total_seconds() > days * 86400 + 3600:
                    continue
                if not title or not link:
                    continue

                hay = f"{title} {desc_txt}".lower()
                url_low = link.lower()
                if any(bad in hay or bad in url_low for bad in (
                    "freejobalert", "sarkariresult", "fresherslive", "jagranjosh",
                    "apply online", "recruitment", "vacancy", "vacancies", "admit card",
                    "posts,", "walk-in interview", "walk in interview",
                    "high court", "supreme court", "court dismisses", "petition challenging",
                    "court verdict", "court order", "quashes tender",
                )):
                    continue
                has_tender_signal = bool(
                    re.search(
                        r"\b(?:tenders?|rfp|eoi|empanelment|procurement|invites bids|quotation|notice inviting tender|bid notice)\b",
                        hay,
                    )
                    or (
                        re.search(r"\bnit\b", hay)
                        and not re.search(
                            r"\bnit\s+(?:agartala|trichy|surathkal|warangal|rourkela|calicut|durgapur|jaipur|kurukshetra|silchar|hamirpur|jalandhar|patna|raipur|bhopal|jamshedpur|srinagar|delhi|goa|manipur|meghalaya|mizoram|nagaland|puducherry|sikkim|uttarakhand|arunachal)\b",
                            hay,
                        )
                    )
                )
                if not has_tender_signal:
                    continue
                if category_slug and _has_excluded_tender_noise(hay, category_slug):
                    continue
                if keyword and not tender_matches_keyword(f"{title} {desc_txt[:180]}", "", keyword):
                    continue

                state, city = detect_indian_location(f"{title} {desc_txt}")
                t_state, _ = detect_indian_location(loc_term)
                if t_state != "National / Central" and state not in (t_state, "National / Central"):
                    continue

                h = hashlib.md5(link.encode()).hexdigest()[:8].upper()
                bid_number = f"NEWS/{now_utc.year}/NIT/{h}"
                bid_id = re.sub(r"[^A-Za-z0-9_]", "_", bid_number)
                if bid_id in tenders:
                    continue

                tenders[bid_id] = {
                    "bid_id": bid_id,
                    "bid_number": bid_number,
                    "title": title[:200],
                    "category_code": "gem-services",
                    "category_name": f"Govt/Civic {keyword.title()}",
                    "matched_category_slug": category_slug,
                    "department": "Municipal / Government Authority",
                    "quantity": 1,
                    "state": state,
                    "city": city,
                    "found_date": pub_dt.isoformat(),
                    "start_date": pub_dt.isoformat(),
                    "discovered_at": now_iso,
                    "expiry_date": (now_utc + timedelta(days=14)).isoformat(),
                    "status": "ACTIVE",
                    "match_score": 80 if (t_state != "National / Central" and state == t_state) else 72,
                    "doc_url": link,
                    "source": src_label,
                    "raw_details": {
                        "source": src_label,
                        "snippet": desc_txt[:300],
                        "link": link,
                        "pub_date": pub_dt.isoformat(),
                    },
                }
        except Exception as e:
            logger.debug(f"RSS tender warning ({src_label}): {e}")

    logger.info(f"[Free RSS] Extracted {len(tenders)} civic/govt tender notices for '{keyword}' ({days}d)")
    return list(tenders.values())
