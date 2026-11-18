"""
Quota-Protected, Free-First Search Failover Engine for GeM Procurement Tenders.
- Fixes `or` provider-chaining bug: empty [] on 0-hit HTTP 200 queries stops failover (`is not None`).
- Persistent Disk/Run Search Cache + Daily/Monthly Quota Ledger (protects SerpApi & free-tier pools).
- Pooled multi-key support for TINYFISH_API_KEYS, GOOGLE_API_KEYS/GOOGLE_KEYS, SERPERDEV_API,
  BRAVE_API_KEYS, TAVILY_API_KEYS, EXA_API_KEYS, LINKUP_API_KEYS, FIRECRAWL_API_KEYS,
  LANGSEARCH_API_KEYS, SCRAPER_API_KEYS/SCRAPERAPI_KEYS, SERPAPI_KEYS, APIFY_API_TOKEN,
  plus optional local-only SearXNG/ddgs (automatically disabled on GITHUB_ACTIONS=true).
- Supports arbitrary locations (India, any state, any city) and variable days_window (7, 30, etc.).
"""

import os
import re
import json
import time
import hashlib
import logging
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Callable
import requests

from gem_portal_client import detect_indian_location, tender_matches_keyword, STATE_TO_SWEEP_HUBS

logger = logging.getLogger("SearchFailover")
_EXHAUSTED_KEYS = set()

CACHE_DIR = os.environ.get(
    "SCRAPER_CACHE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".scraper_cache"),
)
SEARCH_CACHE_FILE = os.path.join(CACHE_DIR, "search_cache.json")
QUOTA_LEDGER_FILE = os.path.join(CACHE_DIR, "quota_ledger.json")
API_HEALTH_FILE = os.path.join(CACHE_DIR, "api_health.json")

DEFAULT_PROVIDER_CAPS: Dict[str, Dict[str, int]] = {
    "tinyfish": {"daily": 30, "monthly": 800},
    "google": {"daily": 80, "monthly": 2400},
    "serperdev": {"daily": 20, "monthly": 500},
    "brave": {"daily": 30, "monthly": 900},
    "tavily": {"daily": 25, "monthly": 750},
    "exa": {"daily": 25, "monthly": 750},
    "linkup": {"daily": 25, "monthly": 750},
    "firecrawl": {"daily": 15, "monthly": 400},
    "langsearch": {"daily": 30, "monthly": 900},
    "scraperapi": {"daily": 15, "monthly": 400},
    "serpapi": {"daily": 2, "monthly": 200},
    "apify": {"daily": 10, "monthly": 250},
}

PROVIDER_ENV_ALIASES: Dict[str, List[str]] = {
    "tinyfish": ["TINYFISH_API_KEYS", "TINYFISH_API_KEY"],
    "google": ["GOOGLE_API_KEYS", "GOOGLE_KEYS"],
    "serperdev": ["SERPERDEV_API", "SERPERDEV_API_KEYS", "SERPER_API_KEY"],
    "brave": ["BRAVE_API_KEYS", "BRAVE_API_KEY"],
    "tavily": ["TAVILY_API_KEYS", "TAVILY_API_KEY"],
    "exa": ["EXA_API_KEYS", "EXA_API_KEY"],
    "linkup": ["LINKUP_API_KEYS", "LINKUP_API_KEY"],
    "firecrawl": ["FIRECRAWL_API_KEYS", "FIRECRAWL_API_KEY"],
    "langsearch": ["LANGSEARCH_API_KEYS", "LANGSEARCH_API_KEY"],
    "scraperapi": ["SCRAPER_API_KEYS", "SCRAPERAPI_KEYS"],
    "serpapi": ["SERPAPI_KEYS", "SERPAPI_API_KEY"],
    "apify": ["APIFY_API_TOKEN", "APIFY_API_TOKENS"],
}

PROVIDER_PRIORITY = [
    "tinyfish",
    "google",
    "serperdev",
    "brave",
    "tavily",
    "exa",
    "linkup",
    "firecrawl",
    "langsearch",
    "scraperapi",
    "serpapi",
    "apify",
]

_IN_MEMORY_CACHE: Dict[str, Any] = {}
_RUN_PAID_CALLS = 0


def _read_json(path: str, default: Any) -> Any:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _write_json(path: str, payload: Any) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass


def is_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"


def is_paid_serp_disabled(free_only: bool = False) -> bool:
    if free_only:
        return True
    for env_name in ("DISABLE_PAID_SERP", "SCRAPER_FREE_ONLY"):
        if os.environ.get(env_name, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


def days_to_tbs(days_window: Optional[int]) -> str:
    d = int(days_window or 30)
    if d <= 1:
        return "qdr:d"
    if d <= 7:
        return "qdr:w"
    if d <= 31:
        return "qdr:m"
    return "qdr:y"


def _mark_key_failed(provider: str, key: str) -> None:
    _EXHAUSTED_KEYS.add((provider, key))
    health = _read_json(API_HEALTH_FILE, {})
    if not isinstance(health, dict):
        health = {}
    fp = _key_fp(key)
    prov_map = health.setdefault(provider, {})
    prov_map[fp] = {"status": "failed", "failed_at": datetime.now().isoformat()}
    prov_map.pop(key, None)
    _write_json(API_HEALTH_FILE, health)


def _is_key_healthy(provider: str, key: str) -> bool:
    if (provider, key) in _EXHAUSTED_KEYS:
        return False
    health = _read_json(API_HEALTH_FILE, {})
    prov_map = health.get(provider, {}) if isinstance(health, dict) else {}
    fp = _key_fp(key)
    lookup_k = fp if fp in prov_map else key
    rec = prov_map.get(lookup_k)
    if rec and rec.get("status") == "failed":
        try:
            if datetime.now() - datetime.fromisoformat(rec["failed_at"]) < timedelta(hours=24):
                return False
            del health[provider][lookup_k]
            _write_json(API_HEALTH_FILE, health)
        except Exception:
            return False
    return True


def _to_semantic_query(query: str) -> str:
    q = re.sub(r'-"[^"]+"', " ", query or "")
    q = re.sub(r"(?:^|\s)-\S+", " ", q)
    q = q.replace("(", " ").replace(")", " ")
    q = re.sub(r"\bOR\b", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def _key_fp(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _get_cap(provider: str, period: str) -> int:
    raw = os.environ.get(f"{provider.upper()}_{period.upper()}_CAP", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_PROVIDER_CAPS.get(provider, {}).get(period, 20 if period == "daily" else 400)


def _load_ledger() -> Dict[str, Any]:
    data = _read_json(QUOTA_LEDGER_FILE, {})
    if not isinstance(data, dict):
        return {"daily": {}, "monthly": {}}
    data.setdefault("daily", {})
    data.setdefault("monthly", {})
    return data


def _save_ledger(ledger: Dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    valid_days = {(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)}
    valid_months = {now.strftime("%Y-%m"), (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")}
    ledger["daily"] = {d: v for d, v in ledger.get("daily", {}).items() if d in valid_days}
    ledger["monthly"] = {m: v for m, v in ledger.get("monthly", {}).items() if m in valid_months}
    _write_json(QUOTA_LEDGER_FILE, ledger)


def _key_usage_today(provider: str, key: str) -> tuple[int, int]:
    ledger = _load_ledger()
    now = datetime.now(timezone.utc)
    fp = _key_fp(key)
    d = int(ledger.get("daily", {}).get(now.strftime("%Y-%m-%d"), {}).get(provider, {}).get(fp, 0))
    m = int(ledger.get("monthly", {}).get(now.strftime("%Y-%m"), {}).get(provider, {}).get(fp, 0))
    return d, m


def _can_spend(provider: str, key: str) -> bool:
    d, m = _key_usage_today(provider, key)
    return d < _get_cap(provider, "daily") and m < _get_cap(provider, "monthly")


def _record_usage(provider: str, key: str) -> None:
    global _RUN_PAID_CALLS
    _RUN_PAID_CALLS += 1
    ledger = _load_ledger()
    now = datetime.now(timezone.utc)
    day_k = now.strftime("%Y-%m-%d")
    month_k = now.strftime("%Y-%m")
    fp = _key_fp(key)
    ledger.setdefault("daily", {}).setdefault(day_k, {}).setdefault(provider, {})
    ledger.setdefault("monthly", {}).setdefault(month_k, {}).setdefault(provider, {})
    ledger["daily"][day_k][provider][fp] = int(ledger["daily"][day_k][provider].get(fp, 0)) + 1
    ledger["monthly"][month_k][provider][fp] = int(ledger["monthly"][month_k][provider].get(fp, 0)) + 1
    _save_ledger(ledger)


def _active_keys(provider: str, raw_env_names: Optional[List[str]] = None) -> List[str]:
    names = raw_env_names or PROVIDER_ENV_ALIASES.get(provider, [])
    out = []
    for name in names:
        raw = os.environ.get(name) or ""
        for k in raw.split(","):
            k = k.strip()
            if k and k not in out and _is_key_healthy(provider, k) and _can_spend(provider, k):
                out.append(k)
    out.sort(key=lambda k: _key_usage_today(provider, k)[0])
    return out


def _resolve_redirect_url(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    low = u.lower()
    if "google." in low and ("/url?" in low or "/goto?" in low):
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
            for k in ("q", "u", "url"):
                if k in qs and qs[k] and qs[k][0].startswith("http"):
                    return qs[k][0]
            if "/goto" in low:
                r = requests.get(u, allow_redirects=False, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
                loc = r.headers.get("Location") or r.headers.get("location")
                if loc and loc.startswith("http"):
                    return loc
        except Exception:
            pass
    return u


def _finalize_search_items(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for it in items or []:
        link = _resolve_redirect_url(it.get("link") or it.get("url") or "")
        if not link or "google.com/goto" in link.lower():
            continue
        if link in seen:
            continue
        seen.add(link)
        out.append({
            "title": (it.get("title") or "").strip(),
            "link": link,
            "snippet": (it.get("snippet") or "").strip(),
        })
    return out


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
        end_dt = max(parsed_dates)
        if end_dt <= now_utc:
            return None
        return end_dt.isoformat()

    return (now_utc + timedelta(days=14)).isoformat()


# ---------------- Provider Implementations ----------------

def search_tinyfish(query: str, days_window: int = 30, location_hint: str = "India") -> Optional[List[Dict[str, str]]]:
    for key in _active_keys("tinyfish"):
        try:
            _record_usage("tinyfish", key)
            recency_mins = max(60, min(5256000, int(days_window or 30) * 1440))
            res = requests.get(
                "https://api.search.tinyfish.ai",
                headers={"X-API-Key": key, "Accept": "application/json"},
                params={
                    "query": query,
                    "location": location_hint or "India",
                    "language": "en",
                    "recency_minutes": recency_mins,
                },
                timeout=20,
            )
            if res.status_code == 200:
                data = res.json()
                rows = data if isinstance(data, list) else (data.get("results") or data.get("organic") or data.get("data") or [])
                return _finalize_search_items([
                    {
                        "title": r.get("title", ""),
                        "link": r.get("url") or r.get("link", ""),
                        "snippet": r.get("snippet") or r.get("description") or r.get("content", ""),
                    }
                    for r in rows if isinstance(r, dict)
                ])
            if res.status_code in (401, 402, 403, 429):
                _mark_key_failed("tinyfish", key)
        except Exception as e:
            logger.debug(f"TinyFish error: {e}")
    return None


def search_google_cse(query: str, days_window: int = 30) -> Optional[List[Dict[str, str]]]:
    """Google Official Custom Search Engine JSON API."""
    cx = (os.environ.get("GOOGLE_CX") or os.environ.get("GOOGLE_CX_ID") or "").strip()
    if not cx:
        return None
    date_restrict = f"d{max(1, min(365, int(days_window or 30)))}"
    for key in _active_keys("google"):
        try:
            _record_usage("google", key)
            res = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": key, "cx": cx, "q": query, "dateRestrict": date_restrict, "gl": "in"},
                timeout=12,
            )
            if res.status_code == 200:
                items = res.json().get("items", []) or []
                return _finalize_search_items([
                    {"title": i.get("title", ""), "link": i.get("link", ""), "snippet": i.get("snippet", "")}
                    for i in items
                ])
            if res.status_code in (400, 401, 403, 429):
                _mark_key_failed("google", key)
        except Exception as e:
            logger.debug(f"Google CSE error: {e}")
    return None


def search_serperdev(query: str, tbs: str = "qdr:m") -> Optional[List[Dict[str, str]]]:
    """Serper.dev Google Search Engine."""
    for key in _active_keys("serperdev"):
        try:
            payload: Dict[str, Any] = {"q": query, "gl": "in", "num": 10}
            if tbs:
                payload["tbs"] = tbs
            _record_usage("serperdev", key)
            res = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json=payload,
                timeout=12,
            )
            if res.status_code == 200:
                organic = res.json().get("organic", []) or []
                return _finalize_search_items([
                    {"title": o.get("title", ""), "link": o.get("link", ""), "snippet": o.get("snippet", "")}
                    for o in organic
                ])
            if res.status_code == 400:
                if any(tok in res.text.lower() for tok in ["credit", "quota", "balance", "insufficient", "limit"]):
                    logger.warning(f"Serper key {key[:5]}... out of credits. Disabling.")
                    _mark_key_failed("serperdev", key)
                else:
                    simple_q = re.sub(r"-\S+", "", query).strip()
                    retry_payload: Dict[str, Any] = {"q": simple_q, "gl": "in", "num": 10}
                    if tbs:
                        retry_payload["tbs"] = tbs
                    _record_usage("serperdev", key)
                    r2 = requests.post(
                        "https://google.serper.dev/search",
                        headers={"X-API-KEY": key, "Content-Type": "application/json"},
                        json=retry_payload,
                        timeout=12,
                    )
                    if r2.status_code == 200:
                        organic = r2.json().get("organic", []) or []
                        return _finalize_search_items([
                            {"title": o.get("title", ""), "link": o.get("link", ""), "snippet": o.get("snippet", "")}
                            for o in organic
                        ])
            elif res.status_code in (401, 402, 403, 429):
                _mark_key_failed("serperdev", key)
        except Exception as e:
            logger.debug(f"Serper connection error: {e}")
    return None


def search_brave(query: str, days_window: int = 30) -> Optional[List[Dict[str, str]]]:
    d = int(days_window or 30)
    freshness = "pd" if d <= 1 else ("pw" if d <= 7 else ("pm" if d <= 31 else "py"))
    for key in _active_keys("brave"):
        try:
            _record_usage("brave", key)
            res = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
                params={"q": query, "country": "IN", "search_lang": "en", "count": 10, "freshness": freshness},
                timeout=15,
            )
            if res.status_code == 200:
                rows = res.json().get("web", {}).get("results", []) or []
                return _finalize_search_items([
                    {"title": r.get("title", ""), "link": r.get("url", ""), "snippet": r.get("description", "")}
                    for r in rows
                ])
            if res.status_code in (401, 402, 403, 429):
                _mark_key_failed("brave", key)
        except Exception as e:
            logger.debug(f"Brave Search error: {e}")
    return None


def search_tavily(query: str, days_window: int = 30) -> Optional[List[Dict[str, str]]]:
    clean_q = _to_semantic_query(query) or query
    for key in _active_keys("tavily"):
        try:
            _record_usage("tavily", key)
            res = requests.post(
                "https://api.tavily.com/search",
                headers={"Content-Type": "application/json"},
                json={"api_key": key, "query": clean_q, "search_depth": "basic", "max_results": 10, "days": max(1, int(days_window or 30))},
                timeout=20,
            )
            if res.status_code == 200:
                rows = res.json().get("results", []) or []
                return _finalize_search_items([
                    {"title": r.get("title", ""), "link": r.get("url", ""), "snippet": r.get("content", "")}
                    for r in rows
                ])
            if res.status_code in (401, 402, 403, 429):
                _mark_key_failed("tavily", key)
        except Exception as e:
            logger.debug(f"Tavily error: {e}")
    return None


def search_exa(query: str, days_window: int = 30) -> Optional[List[Dict[str, str]]]:
    clean_q = _to_semantic_query(query) or query
    start_iso = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days_window or 30)))).strftime("%Y-%m-%dT00:00:00.000Z")
    for key in _active_keys("exa"):
        try:
            _record_usage("exa", key)
            res = requests.post(
                "https://api.exa.ai/search",
                headers={"x-api-key": key, "Content-Type": "application/json"},
                json={"query": clean_q, "type": "auto", "numResults": 10, "startPublishedDate": start_iso, "contents": {"highlights": True}},
                timeout=20,
            )
            if res.status_code == 200:
                rows = res.json().get("results", []) or []
                out = []
                for r in rows:
                    hl = r.get("highlights")
                    snippet = " ".join(hl) if isinstance(hl, list) else (r.get("text") or "")
                    out.append({"title": r.get("title", ""), "link": r.get("url", ""), "snippet": snippet[:400]})
                return _finalize_search_items(out)
            if res.status_code in (401, 402, 403, 429):
                _mark_key_failed("exa", key)
        except Exception as e:
            logger.debug(f"Exa error: {e}")
    return None


def search_linkup(query: str, days_window: int = 30) -> Optional[List[Dict[str, str]]]:
    clean_q = _to_semantic_query(query) or query
    from_date = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days_window or 30)))).strftime("%Y-%m-%d")
    for key in _active_keys("linkup"):
        try:
            _record_usage("linkup", key)
            res = requests.post(
                "https://api.linkup.so/v1/search",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"q": clean_q, "depth": "standard", "outputType": "searchResults", "fromDate": from_date},
                timeout=20,
            )
            if res.status_code == 200:
                rows = res.json().get("results", []) or []
                return _finalize_search_items([
                    {"title": r.get("name") or r.get("title", ""), "link": r.get("url", ""), "snippet": r.get("content", "")[:400]}
                    for r in rows
                ])
            if res.status_code in (401, 402, 403, 429):
                _mark_key_failed("linkup", key)
        except Exception as e:
            logger.debug(f"Linkup error: {e}")
    return None


def search_firecrawl(query: str) -> Optional[List[Dict[str, str]]]:
    for key in _active_keys("firecrawl"):
        try:
            _record_usage("firecrawl", key)
            res = requests.post(
                "https://api.firecrawl.dev/v1/search",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"query": query, "limit": 10, "country": "IN", "lang": "en"},
                timeout=25,
            )
            if res.status_code == 200:
                raw_data = res.json().get("data")
                rows = raw_data.get("web", []) if isinstance(raw_data, dict) else (raw_data if isinstance(raw_data, list) else [])
                return _finalize_search_items([
                    {"title": r.get("title", ""), "link": r.get("url", ""), "snippet": r.get("description") or r.get("markdown", "")[:350]}
                    for r in rows if isinstance(r, dict)
                ])
            if res.status_code in (401, 402, 403, 429):
                _mark_key_failed("firecrawl", key)
        except Exception as e:
            logger.debug(f"Firecrawl error: {e}")
    return None


def search_langsearch(query: str, days_window: int = 30) -> Optional[List[Dict[str, str]]]:
    d = int(days_window or 30)
    freshness = "oneDay" if d <= 1 else ("oneWeek" if d <= 7 else ("oneMonth" if d <= 31 else "oneYear"))
    for key in _active_keys("langsearch"):
        try:
            _record_usage("langsearch", key)
            res = requests.post(
                "https://api.langsearch.com/v1/web-search",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"query": query, "count": 10, "freshness": freshness, "summary": True},
                timeout=20,
            )
            if res.status_code == 200:
                rows = res.json().get("data", {}).get("webPages", {}).get("value", []) or []
                return _finalize_search_items([
                    {"title": r.get("name") or r.get("title", ""), "link": r.get("url", ""), "snippet": r.get("summary") or r.get("text") or r.get("snippet", "")}
                    for r in rows
                ])
            if res.status_code in (401, 402, 403, 429):
                _mark_key_failed("langsearch", key)
        except Exception as e:
            logger.debug(f"LangSearch error: {e}")
    return None


def search_scraperapi(query: str) -> Optional[List[Dict[str, str]]]:
    """ScraperAPI Structured Google Search."""
    for key in _active_keys("scraperapi"):
        try:
            _record_usage("scraperapi", key)
            res = requests.get(
                "https://api.scraperapi.com/structured/google/search",
                params={"api_key": key, "query": query, "country_code": "in", "num": 15},
                timeout=25,
            )
            if res.status_code == 200:
                results = res.json().get("organic_results", []) or []
                return _finalize_search_items([
                    {"title": r.get("title", ""), "link": r.get("link", ""), "snippet": r.get("snippet", "")}
                    for r in results
                ])
            if res.status_code in (401, 402, 403, 429):
                _mark_key_failed("scraperapi", key)
        except Exception as e:
            logger.debug(f"ScraperAPI error: {e}")
    return None


def search_serpapi(query: str, tbs: str = "qdr:m") -> Optional[List[Dict[str, str]]]:
    """SerpApi.com Google Search Engine (last-resort & daily capped)."""
    for key in _active_keys("serpapi"):
        try:
            params: Dict[str, Any] = {
                "api_key": key,
                "engine": "google",
                "q": query,
                "gl": "in",
                "hl": "en",
                "google_domain": "google.co.in",
                "num": 15,
            }
            if tbs:
                params["tbs"] = tbs
            _record_usage("serpapi", key)
            res = requests.get("https://serpapi.com/search.json", params=params, timeout=20)
            if res.status_code == 200:
                data = res.json()
                if data.get("error"):
                    err = str(data.get("error")).lower()
                    if any(t in err for t in ["limit", "credit", "run out", "invalid", "key"]):
                        _mark_key_failed("serpapi", key)
                    continue
                organic = data.get("organic_results", []) or []
                return _finalize_search_items([
                    {"title": r.get("title", ""), "link": r.get("link", ""), "snippet": r.get("snippet", "")}
                    for r in organic
                ])
            if res.status_code in (401, 402, 403, 429):
                _mark_key_failed("serpapi", key)
        except Exception as e:
            logger.debug(f"SerpApi error: {e}")
    return None


def search_apify(query: str) -> Optional[List[Dict[str, str]]]:
    """Apify Google Search Results Scraper."""
    for key in _active_keys("apify"):
        try:
            _record_usage("apify", key)
            url = f"https://api.apify.com/v2/acts/apify~google-search-scraper/run-sync-get-dataset-items?token={key}"
            payload = {"queries": query, "resultsPerPage": 10, "maxPagesPerQuery": 1, "countryCode": "IN"}
            res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=45)
            if res.status_code in (200, 201):
                data = res.json()
                if isinstance(data, list) and len(data) > 0:
                    organic = data[0].get("organicResults", []) or []
                    return _finalize_search_items([
                        {"title": o.get("title", ""), "link": o.get("url", ""), "snippet": o.get("description", "")}
                        for o in organic
                    ])
            elif res.status_code in (400, 401, 402, 403, 429):
                _mark_key_failed("apify", key)
        except Exception as e:
            logger.debug(f"Apify connection error: {e}")
    return None


def search_duckduckgo(query: str, days_window: int = 30, enable_ddgs: bool = False) -> Optional[List[Dict[str, str]]]:
    """Optional local-only SearXNG / ddgs fallback (disabled automatically on GitHub Actions)."""
    if is_github_actions():
        return None

    searx_url = (os.environ.get("SEARXNG_BASE_URL") or "").strip().rstrip("/")
    if searx_url:
        try:
            time_range = "week" if int(days_window or 30) <= 7 else "month"
            res = requests.get(f"{searx_url}/search", params={"q": query, "format": "json", "language": "en-IN", "time_range": time_range}, timeout=12)
            if res.status_code == 200:
                rows = res.json().get("results", []) or []
                return _finalize_search_items([
                    {"title": r.get("title", ""), "link": r.get("url", ""), "snippet": r.get("content", "")}
                    for r in rows[:12]
                ])
        except Exception:
            pass

    use_ddgs = enable_ddgs or os.environ.get("ENABLE_LOCAL_DDGS", "").strip().lower() in ("1", "true", "yes")
    if not use_ddgs:
        return None
    try:
        try:
            from ddgs import DDGS  # type: ignore
        except ImportError:
            from duckduckgo_search import DDGS  # type: ignore
        timelimit = "d" if int(days_window or 30) <= 1 else ("w" if int(days_window or 30) <= 7 else "m")
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, region="in-en", timelimit=timelimit, max_results=10))
        return _finalize_search_items([
            {"title": r.get("title", ""), "link": r.get("href") or r.get("url", ""), "snippet": r.get("body") or r.get("snippet", "")}
            for r in raw
        ])
    except Exception:
        return None


def _dispatch_failover_query(
    query: str,
    days_window: int = 30,
    location_hint: str = "India",
    free_only: bool = False,
    enable_ddgs: bool = False,
) -> List[Dict[str, str]]:
    """
    Runs disk-cached, quota-capped search across providers using strict `is not None` stopping
    (so empty `[]` on 0-hit queries NEVER cascades to burn extra SERP providers!).
    """
    tbs = days_to_tbs(days_window)
    ttl_hours = float(os.environ.get("SEARCH_CACHE_TTL_HOURS", "12") or 12)
    ck = hashlib.sha256(f"{query.lower().strip()}|{tbs}|{days_window}|{(location_hint or '').lower().strip()}".encode("utf-8")).hexdigest()
    now_ts = time.time()

    if ttl_hours > 0:
        mem = _IN_MEMORY_CACHE.get(ck)
        if mem and (now_ts - mem.get("ts", 0)) < ttl_hours * 3600:
            return list(mem.get("results", []))
        disk = _read_json(SEARCH_CACHE_FILE, {})
        if isinstance(disk, dict) and ck in disk and (now_ts - disk[ck].get("ts", 0)) < ttl_hours * 3600:
            _IN_MEMORY_CACHE[ck] = disk[ck]
            return list(disk[ck].get("results", []))

    local_free = search_duckduckgo(query, days_window=days_window, enable_ddgs=enable_ddgs)
    if local_free is not None and len(local_free) > 0:
        if ttl_hours > 0:
            entry = {"ts": now_ts, "query": query[:140], "results": local_free}
            _IN_MEMORY_CACHE[ck] = entry
            disk = _read_json(SEARCH_CACHE_FILE, {})
            if not isinstance(disk, dict):
                disk = {}
            disk[ck] = entry
            _write_json(SEARCH_CACHE_FILE, disk)
        return local_free

    if is_paid_serp_disabled(free_only):
        return []

    max_run_calls = int(os.environ.get("MAX_PAID_SERP_CALLS_PER_RUN", "10") or 10)
    if _RUN_PAID_CALLS >= max_run_calls:
        logger.info(f"[Quota Guard] Per-run paid SERP cap ({max_run_calls}) reached; preserving quotas.")
        return []

    runners: Dict[str, Callable[[], Optional[List[Dict[str, str]]]]] = {
        "tinyfish": lambda: search_tinyfish(query, days_window=days_window, location_hint=location_hint),
        "google": lambda: search_google_cse(query, days_window=days_window),
        "serperdev": lambda: search_serperdev(query, tbs=tbs),
        "brave": lambda: search_brave(query, days_window=days_window),
        "tavily": lambda: search_tavily(query, days_window=days_window),
        "exa": lambda: search_exa(query, days_window=days_window),
        "linkup": lambda: search_linkup(query, days_window=days_window),
        "firecrawl": lambda: search_firecrawl(query),
        "langsearch": lambda: search_langsearch(query, days_window=days_window),
        "scraperapi": lambda: search_scraperapi(query),
        "serpapi": lambda: search_serpapi(query, tbs=tbs),
        "apify": lambda: search_apify(query),
    }

    for prov in PROVIDER_PRIORITY:
        runner = runners.get(prov)
        if not runner:
            continue
        res = runner()
        if res is not None:
            if ttl_hours > 0:
                entry = {"ts": now_ts, "query": query[:140], "results": res}
                _IN_MEMORY_CACHE[ck] = entry
                disk = _read_json(SEARCH_CACHE_FILE, {})
                if not isinstance(disk, dict):
                    disk = {}
                disk = {k: v for k, v in disk.items() if isinstance(v, dict) and (now_ts - v.get("ts", 0)) < ttl_hours * 3600}
                disk[ck] = entry
                _write_json(SEARCH_CACHE_FILE, disk)
            return res

    return []


def _is_official_or_aggregator_tender(link: str, full_text: str) -> bool:
    link_low = (link or "").lower()
    text_low = (full_text or "").lower()
    if any(bad in link_low for bad in [
        "naukri.com", "indeed.com", "linkedin.com", "sarkariresult", "freejobalert",
        "youtube.com", "wikipedia.org", "facebook.com", "instagram.com", "quora.com", "reddit.com",
        "tenderdetail.com/state-tenders", "tenderdetail.com/indian-tender",
        "tenderkart.in/tenders/", "tenderkart.in/state/", "tenderkart.in/authority/", "tenderkart.in/deepdive/",
        "tenders-archive", "tendersarc", "infralens.in/latest/", "pib.gov.in/press", "/pressrelease", "?page=",
    ]):
        return False
    if any(bad in text_low for bad in ["admit card", "exam date", "syllabus", "answer key", "sarkari naukri"]):
        return False
    if re.search(r"\b\d+\s+(?:live|results|tenders in)\b", text_low):
        return False
    if any(dom in link_low for dom in [
        "bidplus.gem.gov.in", "etender.up.nic.in", "eprocure.gov.in", "etenders.gov.in",
        "mahatenders.gov.in", "mptenders.gov.in", "eproc.rajasthan.gov.in",
        "avsar.app", "tenders.infralens.in", "tenderkart.in/tender/", "bideasy.in",
        "tendernews.com/tenders/latest-tender/", "tenderbook.in/",
    ]):
        return True
    if any(sfx in link_low for sfx in [
        ".gov.in", ".nic.in", ".ac.in", ".res.in", ".org.in", ".edu.in", ".bank.in",
        "sbi.co.in", "bankofbaroda.in", "pnbindia.in", "canarabank.com", "licindia.in",
        "uphousing", "ldaonline", "awasvikas", "rlda.indianrailways",
    ]):
        return any(sig in text_low for sig in [
            "tender", "nit", "rfp", "eoi", "quotation", "notice inviting", "bid", "corrigendum",
            "procurement", "empanelment", "hiring of premises", "leasing", "e-auction",
        ])
    return False


def _build_location_query_clause(location_hint: Optional[str]) -> tuple[str, str]:
    raw_loc = (location_hint or "India").strip()
    if raw_loc.lower() in ("india", "pan india", "pan-india", "all india", ""):
        return "India", "National / Central"
    st, city = detect_indian_location(raw_loc)
    if st != "National / Central":
        hubs = STATE_TO_SWEEP_HUBS.get(st, [st])
        parts = []
        if city:
            parts.append(city)
        for h in hubs[:3]:
            if h not in parts:
                parts.append(f'"{h}"' if " " in h else h)
        clause = "(" + " OR ".join(parts[:3]) + ")" if len(parts) > 1 else parts[0]
        return clause, st
    return (f'"{raw_loc}"' if " " in raw_loc else raw_loc), raw_loc


def search_gem_failover(
    keyword: str,
    category_slug: Optional[str] = None,
    location_hint: Optional[str] = None,
    days_window: int = 30,
    free_only: bool = False,
    enable_ddgs: bool = False,
) -> List[Dict[str, Any]]:
    """
    Executes multi-source open-web & GeM failover search using dynamic location & date window,
    without burning redundant providers via `or` chaining.
    """
    if is_paid_serp_disabled(free_only) and not enable_ddgs and not os.environ.get("SEARXNG_BASE_URL"):
        return []

    current_year = datetime.now(timezone.utc).year
    kw_clean = re.sub(r"[^a-zA-Z0-9\s/&+-]", " ", keyword or "").strip()
    loc_clause, target_state = _build_location_query_clause(location_hint)
    loc_simple = "India" if target_state == "National / Central" else target_state

    if any(p in kw_clean.lower() for p in ["premises", "office", "commercial space", "land allotment"]):
        queries = [
            f'("premises lease" OR "hiring of premises") {loc_clause} tender {current_year}',
            f'(site:eprocure.gov.in OR site:etenders.gov.in) (premises OR building OR office) {loc_simple}',
            f'("office space on lease" OR "bank premises") {loc_clause} tender',
        ]
    else:
        queries = [
            f'{kw_clean} {loc_clause} tender GeM bid {current_year}',
            f'{kw_clean} {loc_clause} ("Notice Inviting Tender" OR RFP OR Quotation OR NIT OR EOI)',
            f'{kw_clean} (site:eprocure.gov.in OR site:etenders.gov.in OR site:bidplus.gem.gov.in) {loc_simple}',
            f'{kw_clean} (site:avsar.app OR site:tenders.infralens.in OR site:tenderkart.in/tender/)',
        ]

    all_raw_results: List[Dict[str, str]] = []
    seen_links = set()

    for q in queries:
        logger.info(f"Running government procurement search ({days_window}d): {q}")
        res = _dispatch_failover_query(
            q,
            days_window=days_window,
            location_hint=loc_simple,
            free_only=free_only,
            enable_ddgs=enable_ddgs,
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

        if not _is_official_or_aggregator_tender(link, full_text):
            continue

        if keyword and not tender_matches_keyword(f"{title} {snippet[:220]}", "", keyword):
            continue

        bid_no_match = re.search(r"GEM/\d{4}/[BR]/\d+", full_text)
        bid_number = bid_no_match.group(0) if bid_no_match else None

        doc_num_match = re.search(r"showbidDocument/([A-Za-z0-9_]+)", link)
        if not bid_number and doc_num_match:
            bid_number = f"GEM/{current_year}/B/{doc_num_match.group(1)}"

        if not bid_number:
            agg_id_match = re.search(r"(?:tender[s]?/|id=|bid[-_]?id=)([A-Za-z0-9_\-\/]{6,30})", link, re.IGNORECASE)
            if agg_id_match:
                bid_number = f"TENDER/{current_year}/{agg_id_match.group(1).replace('/', '_')}"
            else:
                h = hashlib.md5(link.encode()).hexdigest()[:8].upper()
                bid_number = f"GOV/{current_year}/NIT/{h}"

        bid_id = re.sub(r"[^A-Za-z0-9_]", "_", bid_number)
        if bid_id in seen_ids:
            continue

        expiry_iso = parse_iso_or_default(full_text)
        if not expiry_iso:
            continue

        seen_ids.add(bid_id)

        dept_match = re.search(
            r"(?:HAL|IIM|IIT(?:\s+\w+)?|BHU|AMU|MNNIT|DFCCIL|IOCL|Indian Oil|Indian Railways|Defence|Military Affairs|Air Force|Navy|Army|Medical Education|CPWD|MES|PWD|Jal Nigam|UPPCB|UPPCL|UPSRTC|UPEIDA|GNIDA|YEIDA|LDA|Pollution Control|Municipal Corporation|Nagar Nigam|AIIMS(?:\s+\w+)?|SGPGI|KGMU|RMLIMS|Ministry of [A-Za-z &]+|Department of [A-Za-z &]+|DRDO|ISRO|NTPC|BHEL|SAIL)",
            full_text,
            re.IGNORECASE,
        )
        department = dept_match.group(0).strip() if dept_match else "Government Department / PSU"
        state, city = detect_indian_location(full_text)

        val_match = re.search(r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?\s*(?:lakh|crore|cr|k)?)", full_text, re.IGNORECASE)
        tender_val = val_match.group(0).strip() if val_match else None

        emd_match = re.search(r"EMD\s*(?:amount)?\s*:?\s*(?:₹|Rs\.?|INR)?\s*([\d,]+)", full_text, re.IGNORECASE)
        emd_val = f"₹{emd_match.group(1)}" if emd_match else None

        link_low = link.lower()
        source_label = "Government / Institutional Tender"
        if "bidplus.gem.gov.in" in link_low or bid_number.startswith("GEM/"):
            source_label = "Government e-Marketplace (GeM)"
        elif "avsar.app" in link_low:
            source_label = "Avsar (GeM & Govt Procurement)"
        elif "infralens.in" in link_low or "bideasy" in link_low:
            source_label = "BidEasy / InfraLens (Tender Radar)"
        elif "tenderkart.in" in link_low:
            source_label = "Tenderkart (Govt Tenders)"
        elif "etender.up.nic.in" in link_low:
            source_label = "Uttar Pradesh e-Procurement (etender.up.nic.in)"
        elif "eprocure.gov.in" in link_low:
            source_label = "Central Public Procurement Portal (CPPP)"
        elif "etenders.gov.in" in link_low:
            source_label = "NIC e-Tenders Portal (etenders.gov.in)"
        elif ".ac.in" in link_low or ".res.in" in link_low:
            source_label = "University / Research Institute NIT"

        doc_url = link
        if "bidplus.gem.gov.in/showbidDocument" in link_low:
            doc_url = link
        elif doc_num_match:
            doc_url = f"https://bidplus.gem.gov.in/showbidDocument/{doc_num_match.group(1)}"

        clean_title = re.sub(r"\s*\|\s*(?:GeM|Avsar|BidEasy|Tenderkart|InfraLens).*$", "", title, flags=re.IGNORECASE).strip(" /|-")
        c_low = clean_title.lower()
        is_generic_title = (
            not clean_title
            or "बड" in clean_title
            or c_low in ("additional terms & conditions", "bid document", "bid document - gem portal", "tender document", "tenders")
            or "bid number" in c_low
            or c_low.startswith("bid document")
            or len(clean_title) < 12
        )
        if is_generic_title:
            snippet_head = re.split(r"[.\n|]", snippet or "")[0].strip()
            if len(snippet_head) >= 24 and not any(x in snippet_head.lower() for x in ["page 1 of", "additional terms"]):
                clean_title = f"{snippet_head[:140]} ({bid_number})"
            else:
                clean_title = f"Government Procurement Tender — {keyword.title()} ({bid_number})"

        loc_match = (target_state != "National / Central" and state == target_state) or (target_state == "National / Central")
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
            "match_score": 88 if loc_match else 75,
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
