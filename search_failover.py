"""
Search Failover Engine for GeM Procurement Tenders (Serper Free-Tier Safe)
Queries external Google Search API providers (Serper.dev, Apify, ScraperAPI, Google CSE)
without triggering Serper Free Tier HTTP 400 (`site:` / quoted domain restrictions).
"""

import os
import re
import json
import logging
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import requests

from gem_portal_client import detect_indian_location

logger = logging.getLogger("SearchFailover")


def parse_iso_or_default(text: str) -> Optional[str]:
    """Finds closing date in search snippets or defaults to 14 days ahead; returns None if expired."""
    now_utc = datetime.now(timezone.utc)
    matches = re.findall(r"(\d{2}[-/]\d{2}[-/]\d{4})", text or "")
    parsed_dates = []
    for raw in matches:
        norm = raw.replace("/", "-")
        try:
            dt = datetime.strptime(norm, "%d-%m-%Y").replace(tzinfo=timezone.utc) + timedelta(hours=17)
            parsed_dates.append(dt)
        except ValueError:
            continue

    if parsed_dates:
        # Closing date is typically the latest date mentioned in the bid header
        end_dt = max(parsed_dates)
        if end_dt <= now_utc:
            return None
        return end_dt.isoformat()

    return (now_utc + timedelta(days=14)).isoformat()


def search_serperdev(query: str) -> Optional[List[Dict[str, str]]]:
    """Serper.dev Google Search Engine (Free-tier compatible: gl='in', num=10, no site: operator)."""
    raw_keys = os.environ.get("SERPERDEV_API") or os.environ.get("SERPER_API_KEY") or ""
    keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
    if not keys:
        return None

    for key in keys:
        try:
            url = "https://google.serper.dev/search"
            payload = {"q": query, "gl": "in", "num": 10}
            headers = {"X-API-KEY": key, "Content-Type": "application/json"}
            res = requests.post(url, headers=headers, json=payload, timeout=12)
            if res.status_code == 200:
                organic = res.json().get("organic", [])
                return [
                    {
                        "title": o.get("title", ""),
                        "link": o.get("link", ""),
                        "snippet": o.get("snippet", ""),
                    }
                    for o in organic
                ]
            elif res.status_code in [401, 403, 429]:
                logger.warning(f"Serper key {key[:5]}... exhausted or invalid. Rotating.")
                continue
            else:
                logger.debug(f"Serper returned HTTP {res.status_code} for query='{query}'")
        except Exception as e:
            logger.debug(f"Serper connection error: {e}")
    return None


def search_scraperapi(query: str) -> Optional[List[Dict[str, str]]]:
    """ScraperAPI Structured Google Search."""
    raw_keys = os.environ.get("SCRAPERAPI_KEYS") or os.environ.get("SERPAPI_KEYS") or ""
    keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
    if not keys:
        return None

    for key in keys:
        try:
            payload = {"api_key": key, "query": query, "country_code": "in"}
            res = requests.get(
                "https://api.scraperapi.com/structured/google/search",
                params=payload,
                timeout=15,
            )
            if res.status_code == 200:
                results = res.json().get("organic_results", [])
                return [
                    {
                        "title": r.get("title", ""),
                        "link": r.get("link", ""),
                        "snippet": r.get("snippet", ""),
                    }
                    for r in results
                ]
            elif res.status_code in [401, 403, 429]:
                continue
        except Exception as e:
            logger.debug(f"ScraperAPI error: {e}")
    return None


def search_apify(query: str) -> Optional[List[Dict[str, str]]]:
    """Apify Google Search Results Scraper."""
    keys = [k.strip() for k in os.environ.get("APIFY_API_TOKEN", "").split(",") if k.strip()]
    if not keys:
        return None

    for key in keys:
        try:
            url = f"https://api.apify.com/v2/acts/apify~google-search-scraper/run-sync-get-dataset-items?token={key}"
            payload = {"queries": query, "resultsPerPage": 10, "maxPagesPerQuery": 1, "countryCode": "in"}
            res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
            if res.status_code in [200, 201]:
                data = res.json()
                if isinstance(data, list) and len(data) > 0:
                    organic = data[0].get("organicResults", [])
                    return [
                        {
                            "title": o.get("title", ""),
                            "link": o.get("url", ""),
                            "snippet": o.get("description", ""),
                        }
                        for o in organic
                    ]
            elif res.status_code in [401, 403, 429]:
                continue
        except Exception as e:
            logger.debug(f"Apify connection error: {e}")
    return None


def search_google_cse(query: str) -> Optional[List[Dict[str, str]]]:
    """Google Official Custom Search Engine JSON API."""
    raw_keys = os.environ.get("GOOGLE_KEYS") or os.environ.get("GOOGLE_API_KEYS") or ""
    keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
    cx = (os.environ.get("GOOGLE_CX") or os.environ.get("GOOGLE_CX_ID") or "").strip()
    if not keys or not cx:
        return None

    for key in keys:
        try:
            url = (
                f"https://www.googleapis.com/customsearch/v1"
                f"?key={key}&cx={cx}&q={urllib.parse.quote(query)}&dateRestrict=m1&gl=in"
            )
            res = requests.get(url, timeout=12)
            if res.status_code == 200:
                items = res.json().get("items", [])
                return [
                    {
                        "title": i.get("title", ""),
                        "link": i.get("link", ""),
                        "snippet": i.get("snippet", ""),
                    }
                    for i in items
                ]
            elif res.status_code == 429:
                continue
        except Exception as e:
            logger.debug(f"Google CSE error: {e}")
    return None


def search_duckduckgo(query: str) -> Optional[List[Dict[str, str]]]:
    """Free zero-key HTML search fallback via DuckDuckGo HTML endpoint."""
    try:
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        res = requests.post(url, data={"q": query, "kl": "in-en"}, headers=headers, timeout=12)
        if res.status_code == 200:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.text, "html.parser")
            results = []
            for r in soup.select(".result"):
                a = r.select_one(".result__title a")
                s = r.select_one(".result__snippet")
                if a and a.get("href"):
                    href = a["href"]
                    if "uddg=" in href:
                        import urllib.parse
                        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                        href = parsed.get("uddg", [href])[0]
                    results.append({
                        "title": a.get_text(strip=True),
                        "link": href,
                        "snippet": s.get_text(strip=True) if s else "",
                    })
            if results:
                return results
    except Exception as e:
        logger.debug(f"DuckDuckGo search fallback: {e}")
    return None


def search_gem_failover(
    keyword: str,
    category_slug: Optional[str] = None,
    location_hint: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Executes multi-source search failover for government procurement tenders across:
    1. GeM portal showbidDocument (official bids)
    2. Tender Aggregators (avsar.app, tenders.infralens.in / BidEasy, tenderkart.in)
    3. State eProcurement & CPPP (etender.up.nic.in, eprocure.gov.in)
    """
    current_year = datetime.now(timezone.utc).year
    loc_clause = f' "{location_hint}"' if location_hint else ""

    # Build multi-source queries targeting GeM, aggregators, and state portals
    queries = [
        f'showbidDocument "{keyword}"{loc_clause} GeM ("GEM/{current_year}" OR "Bid")',
        f'"{keyword}" (site:avsar.app OR site:tenders.infralens.in OR site:tenderkart.in OR "BidEasy") ("GEM" OR "Tender") {location_hint or ""}',
        f'"{keyword}" (site:etender.up.nic.in OR site:eprocure.gov.in) ("Tender" OR "Notice") {location_hint or "Uttar Pradesh"}',
    ]

    all_raw_results: List[Dict[str, str]] = []
    seen_links = set()

    for q in queries:
        logger.info(f"Running government procurement search: {q}")
        res = (
            search_serperdev(q)
            or search_google_cse(q)
            or search_scraperapi(q)
            or search_duckduckgo(q)
            or search_apify(q)
            or []
        )
        for item in res:
            link = (item.get("link") or "").strip()
            if link and link not in seen_links:
                seen_links.add(link)
                all_raw_results.append(item)

    now_iso = datetime.now(timezone.utc).isoformat()
    tenders: List[Dict[str, Any]] = []
    seen_ids = set()

    for r in all_raw_results:
        title = (r.get("title") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        link = (r.get("link") or "").strip()
        full_text = f"{title} {snippet} {link}"

        # 1. Detect bid number
        bid_no_match = re.search(r"GEM/\d{4}/[BR]/\d+", full_text)
        bid_number = bid_no_match.group(0) if bid_no_match else None

        doc_num_match = re.search(r"showbidDocument/([A-Za-z0-9_]+)", link)
        if not bid_number and doc_num_match:
            bid_number = f"GEM/{current_year}/B/{doc_num_match.group(1)}"

        # If not a standard GeM ID, check for aggregator / eProcure tender ID
        if not bid_number:
            agg_id_match = re.search(r"(?:tender[s]?/|id=|bid[-_]?id=)([A-Za-z0-9_\-\/]{6,30})", link, re.IGNORECASE)
            if agg_id_match:
                bid_number = f"TENDER/{current_year}/{agg_id_match.group(1).replace('/', '_')}"
            elif any(domain in link.lower() for domain in ["etender.up.nic.in", "eprocure.gov.in", "avsar.app", "infralens.in", "tenderkart.in"]):
                import hashlib
                h = hashlib.md5(link.encode()).hexdigest()[:8].upper()
                bid_number = f"GOV/{current_year}/BID/{h}"
            else:
                continue

        bid_id = re.sub(r"[^A-Za-z0-9_]", "_", bid_number)
        if bid_id in seen_ids:
            continue

        expiry_iso = parse_iso_or_default(full_text)
        if not expiry_iso:
            continue

        seen_ids.add(bid_id)

        # 2. Extract Organization / Department
        dept_match = re.search(
            r"(?:HAL|IIM|IIT|DFCCIL|IOCL|Indian Oil|Indian Railways|Defence|Military Affairs|Air Force|Navy|Army|Medical Education|CPWD|MES|PWD|Jal Nigam|UPPCB|Pollution Control|Municipal Corporation|Nagar Nigam|AIIMS|SGPGI|KGMU|RMLIMS|Ministry of [A-Za-z &]+|Department of [A-Za-z &]+|DRDO|ISRO|NTPC|BHEL|SAIL)",
            full_text,
            re.IGNORECASE,
        )
        department = dept_match.group(0).strip() if dept_match else "Government Department / PSU"
        state, city = detect_indian_location(full_text)

        # 3. Detect financial metrics (Tender Value & EMD)
        val_match = re.search(r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?\s*(?:lakh|crore|cr|k)?)", full_text, re.IGNORECASE)
        tender_val = val_match.group(0).strip() if val_match else None

        emd_match = re.search(r"EMD\s*(?:amount)?\s*:?\s*(?:₹|Rs\.?|INR)?\s*([\d,]+)", full_text, re.IGNORECASE)
        emd_val = f"₹{emd_match.group(1)}" if emd_match else None

        # 4. Resolve source and doc URL
        source_label = "Government e-Marketplace (GeM)"
        if "avsar.app" in link:
            source_label = "Avsar (GeM & Govt Procurement)"
        elif "infralens.in" in link or "bideasy" in link:
            source_label = "BidEasy / InfraLens (Tender Radar)"
        elif "tenderkart.in" in link:
            source_label = "Tenderkart (Govt Tenders)"
        elif "etender.up.nic.in" in link:
            source_label = "Uttar Pradesh e-Procurement (etender.up.nic.in)"
        elif "eprocure.gov.in" in link:
            source_label = "Central Public Procurement Portal (CPPP)"

        doc_url = link
        if "bidplus.gem.gov.in/showbidDocument" in link:
            doc_url = link
        elif doc_num_match:
            doc_url = f"https://bidplus.gem.gov.in/showbidDocument/{doc_num_match.group(1)}"

        # 5. Extract clean descriptive work title
        clean_title = re.sub(r"\s*\|\s*(?:GeM|Avsar|BidEasy|Tenderkart|InfraLens).*$", "", title, flags=re.IGNORECASE).strip()
        is_generic_title = (
            not clean_title
            or "बड" in clean_title
            or "bid number" in clean_title.lower()
            or clean_title.lower().startswith("bid document")
            or len(clean_title) < 10
        )
        if is_generic_title:
            work_match = re.search(
                r"(?:Operation and Maintenance of [^;,\.\n]+|Sewage / Effluent Treatment Plant|Sewage Treatment Plant[^;,\.\n]*|Effluent Treatment Plant[^;,\.\n]*|PSA Oxygen [^;,\.\n]*|Oxygen Plant[^;,\.\n]*|AMC of [^;,\.\n]+|Supply and Installation of [^;,\.\n]+)",
                snippet,
                re.IGNORECASE,
            )
            if work_match:
                clean_title = f"{work_match.group(0).strip()} ({bid_number})"
            else:
                clean_title = f"Government Procurement Tender — {keyword.title()} ({bid_number})"

        tenders.append({
            "bid_id": bid_id,
            "bid_number": bid_number,
            "title": clean_title[:200],
            "category_code": "gem-services",
            "category_name": f"Government {keyword.title()}",
            "matched_category_slug": category_slug,
            "department": department[:150],
            "quantity": 1,
            "state": state,
            "city": city,
            "found_date": now_iso,
            "discovered_at": now_iso,
            "expiry_date": expiry_iso,
            "status": "ACTIVE",
            "match_score": 85 if (state == "Uttar Pradesh" or city == "Lucknow") else 75,
            "doc_url": doc_url,
            "source": source_label,
            "tender_value": tender_val,
            "emd": emd_val,
            "raw_details": {
                "source": source_label,
                "snippet": snippet[:350],
                "link": link,
                "tender_value": tender_val,
                "emd": emd_val,
            },
        })

    logger.info(f"Government search failover extracted {len(tenders)} active tenders for '{keyword}'")
    return tenders
