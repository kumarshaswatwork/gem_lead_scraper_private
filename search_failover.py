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


def search_gem_failover(
    keyword: str,
    category_slug: Optional[str] = None,
    location_hint: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Executes Serper-Free-Tier-safe search failover for official GeM tenders.
    """
    current_year = datetime.now(timezone.utc).year
    loc_clause = f' "{location_hint}"' if location_hint else ""
    query = f'showbidDocument "{keyword}"{loc_clause} GeM ("GEM/{current_year}" OR "Bid")'
    logger.info(f"Running GeM search failover: {query}")

    raw_results = (
        search_serperdev(query)
        or search_google_cse(query)
        or search_scraperapi(query)
        or search_apify(query)
        or []
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    tenders: List[Dict[str, Any]] = []
    seen_ids = set()

    for r in raw_results:
        title = (r.get("title") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        link = (r.get("link") or "").strip()
        full_text = f"{title} {snippet}"

        bid_no_match = re.search(r"GEM/\d{4}/[BR]/\d+", full_text)
        bid_number = bid_no_match.group(0) if bid_no_match else None

        doc_num_match = re.search(r"showbidDocument/([A-Za-z0-9_]+)", link)
        if not bid_number:
            if doc_num_match:
                bid_number = f"GEM/{current_year}/B/{doc_num_match.group(1)}"
            else:
                continue

        bid_id = re.sub(r"[^A-Za-z0-9_]", "_", bid_number)
        if bid_id in seen_ids:
            continue

        expiry_iso = parse_iso_or_default(full_text)
        if not expiry_iso:
            continue

        seen_ids.add(bid_id)
        dept_match = re.search(
            r"(?:Ministry of [A-Za-z &]+|Department of [A-Za-z &]+|AIIMS|DRDO|ISRO|NTPC|BHEL|Indian Railways|Municipal Corporation [A-Za-z ]*)",
            full_text,
            re.IGNORECASE,
        )
        department = dept_match.group(0).strip() if dept_match else "Government Department / PSU"
        state, city = detect_indian_location(full_text)

        doc_url = (
            link
            if "bidplus.gem.gov.in/showbidDocument" in link
            else (
                f"https://bidplus.gem.gov.in/showbidDocument/{doc_num_match.group(1)}"
                if doc_num_match
                else f"https://bidplus.gem.gov.in/showbidDocument/{bid_id}"
            )
        )

        clean_title = re.sub(r"\s*\|\s*GeM.*$", "", title, flags=re.IGNORECASE).strip()
        if not clean_title or clean_title.lower().startswith("bid document"):
            clean_title = f"GeM Procurement Tender — {keyword.title()} ({bid_number})"

        tenders.append({
            "bid_id": bid_id,
            "bid_number": bid_number,
            "title": clean_title[:200],
            "category_code": "gem-services",
            "category_name": f"GeM {keyword.title()}",
            "matched_category_slug": category_slug,
            "department": department[:150],
            "quantity": 1,
            "state": state,
            "city": city,
            "found_date": now_iso,
            "discovered_at": now_iso,
            "expiry_date": expiry_iso,
            "status": "ACTIVE",
            "match_score": 78,
            "doc_url": doc_url,
            "raw_details": {
                "source": "search_failover",
                "snippet": snippet[:300],
                "link": link,
            },
        })

    logger.info(f"Search failover extracted {len(tenders)} active tenders for '{keyword}'")
    return tenders
