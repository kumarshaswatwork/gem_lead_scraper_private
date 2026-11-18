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

from gem_portal_client import GemPortalClient, detect_indian_location, tender_matches_keyword
from search_failover import search_gem_failover
from firebase_sync import FirebaseSyncManager, resolve_vendor_category, classify_tender, get_gem_queries_for_category


def run_unit_guards():
    import tempfile, shutil
    import search_failover as _sf
    import firebase_sync as _fbs
    tmp_cache = tempfile.mkdtemp(prefix="gem_lead_test_")
    orig_state = (
        _sf.CACHE_DIR, _sf.SEARCH_CACHE_FILE, _sf.QUOTA_LEDGER_FILE,
        _sf.API_HEALTH_FILE, _sf._RUN_PAID_CALLS,
        dict(_sf._IN_MEMORY_CACHE), set(_sf._EXHAUSTED_KEYS),
        _fbs.CACHE_DIR, _fbs.GEM_AI_QUERY_CACHE_FILE,
    )
    _sf.CACHE_DIR = tmp_cache
    _sf.SEARCH_CACHE_FILE = os.path.join(tmp_cache, "search_cache.json")
    _sf.QUOTA_LEDGER_FILE = os.path.join(tmp_cache, "quota_ledger.json")
    _sf.API_HEALTH_FILE = os.path.join(tmp_cache, "api_health.json")
    _sf._RUN_PAID_CALLS = 0
    _sf._IN_MEMORY_CACHE.clear()
    _sf._EXHAUSTED_KEYS.clear()
    _fbs.CACHE_DIR = tmp_cache
    _fbs.GEM_AI_QUERY_CACHE_FILE = os.path.join(tmp_cache, "gem_ai_query_cache.json")
    try:
        _run_unit_guards_body()
    finally:
        (
            _sf.CACHE_DIR, _sf.SEARCH_CACHE_FILE, _sf.QUOTA_LEDGER_FILE,
            _sf.API_HEALTH_FILE, _sf._RUN_PAID_CALLS,
            orig_mem, orig_exh,
            _fbs.CACHE_DIR, _fbs.GEM_AI_QUERY_CACHE_FILE,
        ) = orig_state
        _sf._IN_MEMORY_CACHE.clear()
        _sf._IN_MEMORY_CACHE.update(orig_mem)
        _sf._EXHAUSTED_KEYS.clear()
        _sf._EXHAUSTED_KEYS.update(orig_exh)
        shutil.rmtree(tmp_cache, ignore_errors=True)


def _run_unit_guards_body():
    print("[0/4] Running GeM Anti-False-Positive, Free-Channel, Quota & Dynamic Geo Unit Guards...")
    assert tender_matches_keyword("HIRING OF CHARTERED AIRCRAFT", "Civil Aviation", "Chartered Accountant") is False
    assert tender_matches_keyword("Hiring of Chartered Accountant Firm for GST Audit", "UPPCL", "Chartered Accountant") is True
    assert tender_matches_keyword("Channa, Split Chickpeas", "Social Welfare Department Uttar Pradesh", "Social Media") is False
    assert tender_matches_keyword("Selection of Social Media Management Agency", "UP Tourism", "Social Media Management") is True
    assert tender_matches_keyword("Safari Suit Cloth", "Police Dept", "Saree") is False
    assert tender_matches_keyword("Banarasi Silk Saree Handloom", "Tex Ministry", "Saree") is True
    assert tender_matches_keyword("L-SHAPE MAIN TABLE, O-SHAPE CONFERENCE TABLE", "Dept", "Conference") is False
    assert tender_matches_keyword("DGMS APPROVED AUDIO VISUAL ALARM AVA", "Coal India", "Audio Visual") is False
    assert tender_matches_keyword("PMCC Panel 415V", "Atomic Energy", "PMC") is False
    assert tender_matches_keyword("Project Management Consultancy Service", "NHAI", "Project Management Consultancy") is True
    assert tender_matches_keyword("Procurement of Refrigerator Two Door for Guest House", "Powergrid", "Guest House") is False
    assert tender_matches_keyword("Premises for Lease or Rent Lucknow", "Bank of India", "Hiring of Premises") is True
    assert tender_matches_keyword("Office Space Required on Lease in Noida", "LIC", "Hiring of Premises") is True

    # New category anti-false-positive guards: Doctors vs Doctor Blade, Waste vs Waste Fittings, Coaching vs Apparel/Rail Coach
    assert tender_matches_keyword("Doctor Blade for Rotogravure Printing Press", "Security Printing Press", "Medical Consultant") is False
    assert tender_matches_keyword("Flexible Video Bronchoscope", "Medical Education Department Uttar Pradesh", "Medical Consultant") is False
    assert tender_matches_keyword("Hiring of Visiting Specialist Doctor and Medical Officer", "AIIMS", "Medical Consultant") is True
    assert tender_matches_keyword("Copper Alloy Gate Valve, Waste Fittings for Washbasin and Sinks", "Dept of Military Affairs", "Waste Management") is False
    assert tender_matches_keyword("Municipal Solid Waste Collection and Disposal Services", "Nagar Nigam Lucknow", "Waste Management") is True
    assert tender_matches_keyword("Pants, Mens Casual Shirt, Kurtis", "Skill Development Department Odisha", "Skill Training") is False
    assert tender_matches_keyword("LHB Railway Coach Cleaning and Maintenance", "Indian Railways", "Coaching") is False
    assert tender_matches_keyword("Outsourcing of Coaching Depot Assistance Material Handling Stores Operation", "Indian Railways", "Coaching") is False
    assert tender_matches_keyword("Transportation of Bogies with Wheel sets from various workshops", "Southern Railway", "Coaching") is False
    assert tender_matches_keyword("Motorized ICU Bed 4 Section with Mattress, Bedside Locker", "Health Dept", "Waste Management") is False
    assert tender_matches_keyword("Skill Training and Capacity Building Program for Youth", "UPSDM", "Skill Training") is True

    # Combined <keyword> <location> verification
    assert tender_matches_keyword("Mobile Containers For Solid Waste", "Planning and Development Department Jharkhand", "Waste Uttar Pradesh") is False
    assert tender_matches_keyword("Solid Waste Processing and Disposal Contract", "Nagar Nigam Lucknow Uttar Pradesh", "Waste Uttar Pradesh") is True

    # Category resolver alignment with digitalMarketing/lib/leads/category-lead-routing.ts
    assert resolve_vendor_category({"niche": "Event Marketing", "sub_niche": "Event Marketing"})[0] == "event-marketing"
    assert resolve_vendor_category({"niche": "Doctors", "sub_niche": "Doctors"})[0] == "doctors-medical-consultants"
    assert resolve_vendor_category({"niche": "Waste Management", "sub_niche": "Waste Management"})[0] == "waste-management"
    assert resolve_vendor_category({"niche": "Coaching", "sub_niche": "Coaching"})[0] == "coaching"
    assert resolve_vendor_category({"niche": "Banarasi Saree"})[0] == "saree-apparel"
    assert resolve_vendor_category({"niche": "Heavy Engineering", "sub_niche": "Industrial Machine Supplier and Pumps"})[0] == "heavy-engineering-machinery"
    assert tender_matches_keyword("Design, Supply, Installation, Commissioning and Maintenance of Grid Connected Roof Top Solar Power Plant Service", "ZP Nashik", "Solar Panel Installation") is True
    assert tender_matches_keyword("Custom Bid for Services - Hiring of Duty Doctor", "AIIMS", "Doctor") is True
    assert tender_matches_keyword("Custom Bid for Services - GLASS PARTITION WORK FOR NEW MEDICAL OFICER/ DOCTOR", "Military Affairs", "Doctor") is False
    assert tender_matches_keyword("CHT for Conveyance of Civ Doctors staff, Tab Cold 100 mg", "Military Affairs", "Doctor") is False
    assert tender_matches_keyword("Manpower Outsourcing Services - Fixed Remuneration - Healthcare; Peon; High School", "Medical Education Dept", "Doctor") is False
    assert classify_tender({"title": "ARC FOR STEAM NETWORK RECTIFICATION, MISCELLANEOUS HOT JOBS, AND MAINTENANCE OF WORKSHOP", "department": "IOCL"}) != "event-marketing"
    assert tender_matches_keyword("Development of 02 x Untethered four lane Infantry Weapon Training Simulator", "Military Affairs", "Training Service") is False
    assert tender_matches_keyword("Safety and Emergency Response Training Services (Version 2) - Offline", "Home Dept", "Training Service") is True
    assert detect_indian_location("Puducherry PWD") == ("Puducherry", "Puducherry")
    assert detect_indian_location("Municipal Corp Shimla") == ("Himachal Pradesh", "Shimla")

    from search_failover import _is_official_or_aggregator_tender, _dispatch_failover_query
    import search_failover as _sf
    assert _is_official_or_aggregator_tender("https://tenderkart.in/tenders/consultancy/result?page=55", "Consultancy Tenders 30467 Results") is False
    assert _is_official_or_aggregator_tender("https://www.tendernews.com/tenders/latest-tender//premises-for-lease-or-rent-lucknow.html", "Latest premises for lease or rent lucknow Tender Notice") is True
    assert detect_indian_location("SGPGI Lucknow Campus")[0] == "Uttar Pradesh"
    assert detect_indian_location("GNIDA Greater Noida Authority") == ("Uttar Pradesh", "Greater Noida")
    assert detect_indian_location("GAIL PATA Lodging and Boarding") == ("Uttar Pradesh", "Auraiya")
    assert detect_indian_location("Brihanmumbai Municipal Corporation")[0] == "Maharashtra"
    assert classify_tender({"title": "Selection of Advertising Agency and Digital Marketing", "department": "UPPCB"}) == "digital-marketing-seo"
    assert classify_tender({"title": "Channa & Cotton Mattress", "department": "Social Welfare Department"}) is None
    assert classify_tender({"title": "Solid Waste Management Contract", "department": "Nagar Nigam"}) == "waste-management"
    assert "Hiring of Premises" in get_gem_queries_for_category("real-estate", "Real Estate", [])
    assert "Guest House" in get_gem_queries_for_category("paying-guest", "Paying Guest", [])
    assert "Chartered Accountant" in get_gem_queries_for_category("gst-services", "GST Services", [])
    assert "Solid Waste" in get_gem_queries_for_category("waste-management", "Waste Management", [])
    assert "Medical Consultant" in get_gem_queries_for_category("doctors-medical-consultants", "Doctors", [])
    assert "Skill Training" in get_gem_queries_for_category("coaching", "Coaching", [])
    assert "PMC" not in get_gem_queries_for_category("gem-services", "GeM Services", [])

    # GePNIC homepage active tender parser unit check
    from free_tender_sources import parse_gepnic_homepage_html
    sample_gepnic_html = """
    <table id="activeTenders">
      <tr><td>1. Solid Waste Collection in Lucknow</td><td>NNL/SWM/2026/01</td><td>31-Dec-2027 05:00 PM</td><td>01-Jan-2028 11:00 AM</td><td><a href="/nicgep/app?sp=123">View</a></td></tr>
    </table>
    """
    gep_rows = parse_gepnic_homepage_html(sample_gepnic_html, "https://etender.up.nic.in/nicgep/app", "UP e-Procurement", "Uttar Pradesh")
    assert len(gep_rows) == 1 and gep_rows[0]["matched_category_slug"] == "waste-management" and gep_rows[0]["city"] == "Lucknow"

    # Verify default AJAX sort is Bid-Start-Date-Latest and search_failover stops on [] (non-None) without burning SerpApi!
    import inspect, unittest.mock as _mock
    sig = inspect.signature(GemPortalClient.fetch_all_bids_ajax)
    assert sig.parameters["sort"].default == "Bid-Start-Date-Latest", "Expected Bid-Start-Date-Latest default sort"

    with _mock.patch.dict(os.environ, {"SEARCH_CACHE_TTL_HOURS": "0", "SERPERDEV_API": "fake-s", "SERPAPI_KEYS": "fake-serpapi"}):
        with _mock.patch.object(_sf, "search_serperdev", return_value=[]):
            with _mock.patch.object(_sf, "search_serpapi", side_effect=AssertionError("BUG: [] fell through to SerpApi!")):
                out = _dispatch_failover_query("test 0 hit query", days_window=7, free_only=False)
                assert out == []
        with _mock.patch.object(_sf, "search_serpapi", side_effect=AssertionError("Paid SERP called in free_only!")):
            assert _dispatch_failover_query("test free only", free_only=True) == []

    # Verify build_regional_sweep_keywords supports Pan-India, Maharashtra, Uttar Pradesh dynamically
    from gem_scraper import build_regional_sweep_keywords
    assert "Mumbai" in build_regional_sweep_keywords("Maharashtra")
    assert "India" in build_regional_sweep_keywords("India")
    assert "Lucknow" in build_regional_sweep_keywords("Uttar Pradesh")

    # Verify scrape_keyword_tenders calls fetch_all_bids_ajax only ONCE on 0-hit queries (no duplicate AJAX hit)
    ajax_calls = []
    mock_client = GemPortalClient()
    with _mock.patch.object(mock_client, "fetch_all_bids_ajax", side_effect=lambda *a, **k: ajax_calls.append(1) or []):
        with _mock.patch.object(mock_client.session, "get", return_value=type("R", (), {"status_code": 404, "text": ""})()):
            mock_client.scrape_keyword_tenders("zero_hit_kw", max_pages=1)
    assert len(ajax_calls) == 1, f"Expected 1 AJAX call on 0-hit query, got {len(ajax_calls)}"

    # Verify Free RSS blocks FreeJobAlert/NIT Agartala recruitment articles disguised as 'NIT'
    from free_tender_sources import fetch_rss_tender_leads
    fake_rss = (
        "<rss><channel><item>"
        "<title>NIT Agartala Medical Officer/Campus Doctor Recruitment 2026: 2 Posts, Apply Online - FreeJobAlert</title>"
        "<link>https://www.freejobalert.com/articles/nit-agartala-recruitment</link>"
        "<pubDate>Wed, 18 Nov 2026 08:00:00 GMT</pubDate>"
        "<description>Medical Consultant recruitment at NIT Agartala</description>"
        "</item></channel></rss>"
    )
    import free_tender_sources as _fts
    with _mock.patch.object(_fts.requests, "get", return_value=type("R", (), {"status_code": 200, "text": fake_rss})()):
        rss_rows = fetch_rss_tender_leads("Medical Consultant", "doctors-medical-consultants", "India", 30)
    assert len(rss_rows) == 0, f"Expected job recruitment article to be blocked, got {rss_rows}"

    # Verify Serper HTTP 400 non-credit syntax retry preserves tbs and api_health hashes keys (never plaintext)
    posted_payloads = []
    class _FakeResp:
        def __init__(self, code, data, text=""):
            self.status_code, self._data, self.text = code, data, text
        def json(self): return self._data
    def _fake_serper_post(url, headers=None, json=None, timeout=12):
        posted_payloads.append(json)
        return _FakeResp(400, {}, "syntax error") if len(posted_payloads) == 1 else _FakeResp(200, {"organic": []})
    with _mock.patch.dict(os.environ, {"SERPERDEV_API": "fake-gem-serper-test", "SEARCH_CACHE_TTL_HOURS": "0"}):
        with _mock.patch.object(_sf.requests, "post", side_effect=_fake_serper_post):
            _sf.search_serperdev("test -bad", tbs="qdr:w")
    assert len(posted_payloads) == 2 and posted_payloads[1].get("tbs") == "qdr:w", f"Failed Serper 400 tbs preservation: {posted_payloads}"
    _sf._mark_key_failed("tinyfish", "secret-plaintext-tinyfish-key")
    health_raw = open(_sf.API_HEALTH_FILE, "r", encoding="utf-8").read()
    assert "secret-plaintext-tinyfish-key" not in health_raw, "Raw plaintext API key leaked in api_health.json!"

    # Round 2 GeM AJAX title relevance, noise guards & Kusmunda Chhattisgarh mapping
    from firebase_sync import _has_excluded_tender_noise
    assert _has_excluded_tender_noise("cypermethrin 03% smoke generator", "heavy-engineering-machinery") is True
    assert tender_matches_keyword("Cypermethrin 03% Smoke Generator", "Dept", "Generator") is False
    assert tender_matches_keyword("Diesel Power Generator (V2)", "Dept of Financial Services", "Generator") is True
    assert _has_excluded_tender_noise("motor insurance service - 4 wheeler ... ace/", "heavy-engineering-machinery") is True
    assert tender_matches_keyword("Motor Insurance Service - 4 Wheeler ... ACE/", "Coal India", "Generator") is False
    assert _has_excluded_tender_noise("avian shield ... herbicide", "solar-panel-installation") is True
    assert tender_matches_keyword("AVIAN SHIELD ... HERBICIDE", "Dept", "Solar Panel Installation") is False
    assert _has_excluded_tender_noise("solar edge indicator, chevron sign boards retro reflective sign board", "solar-panel-installation") is True
    assert tender_matches_keyword("Solar edge Indicator, Chevron Sign Boards Retro Reflective Sign board", "NHAI", "Solar Panel Installation") is False
    assert _has_excluded_tender_noise("facility management services - lumpsum based - kusmunda open cast project; o&m of electrical works", "gem-services") is True
    assert tender_matches_keyword("Facility Management Services - LumpSum Based - KUSMUNDA OPEN CAST PROJECT; O&M of Electrical Works", "Coal India", "Project Management Consultancy") is False
    assert tender_matches_keyword("Hiring of Consultants - Milestone/Deliverable Based - Project Management Consultants; AS PER RFP", "HUDCO", "Project Management Consultancy") is True
    assert classify_tender({"title": "Facility Management Services - LumpSum Based - KUSMUNDA OPEN CAST PROJECT; O&M of Electrical Works", "department": "Coal India"}) != "gem-services"
    assert detect_indian_location("KUSMUNDA OPEN CAST PROJECT") == ("Chhattisgarh", "Kusmunda")

    # Verify gem_scraper Phase 2 locally filters off-topic tenders returned by GeM Solr
    from gem_scraper import discover_category_tenders
    fake_client = GemPortalClient()
    fake_bids = [
        {"bid_id": "BAD_1", "bid_number": "GEM/2026/B/1", "title": "AVIAN SHIELD HERBICIDE", "department": "Dept", "category_name": "GeM"},
        {"bid_id": "BAD_2", "bid_number": "GEM/2026/B/2", "title": "Solar edge Indicator, Chevron Sign Boards Retro Reflective Sign board", "department": "NHAI", "category_name": "GeM"},
        {"bid_id": "OK_1", "bid_number": "GEM/2026/B/3", "title": "Off - Grid Solar Photovoltaic (PV) Power Plant (V2)", "department": "UPNEDA", "category_name": "GeM"},
    ]
    with _mock.patch.object(fake_client, "scrape_keyword_tenders", return_value=fake_bids):
        with _mock.patch("gem_scraper.fetch_gepnic_tenders_for_target", return_value=[]):
            with _mock.patch("gem_scraper.fetch_rss_tender_leads", return_value=[]):
                found_solar = discover_category_tenders(
                    fake_client, "solar-panel-installation", "Solar Panel Installation",
                    [], run_failover=False, free_only=True, use_ai=False,
                )
    assert "OK_1" in found_solar and "BAD_1" not in found_solar and "BAD_2" not in found_solar, f"Phase 2 leaked off-topic bids: {list(found_solar.keys())}"

    # Round 2 Deep Review Fixes: Court news RSS blocking, Bombay/Calcutta location mapping, Solar road stud/signboard blocking
    assert detect_indian_location("Bombay High Court") == ("Maharashtra", "Mumbai")
    assert _has_excluded_tender_noise("solar road stud retro-reflective signboard", "solar-panel-installation") is True
    assert tender_matches_keyword("Solar Road Stud Retro-Reflective Signboard", "NHAI", "Solar Panel Installation") is False
    court_rss = (
        "<rss><channel><item>"
        "<title>Bombay High Court Dismisses Petition Challenging Tender Eligibility Decision in Municipal Solid Waste Management Project</title>"
        "<link>https://www.verdictum.in/court-updates/high-courts/bombay-hc-solid-waste-tender</link>"
        "<pubDate>Wed, 18 Nov 2026 08:00:00 GMT</pubDate>"
        "<description>Bombay High Court dismissed petition on solid waste management tender.</description>"
        "</item></channel></rss>"
    )
    with _mock.patch.object(_fts.requests, "get", return_value=type("R", (), {"status_code": 200, "text": court_rss})()):
        court_rows = fetch_rss_tender_leads("Solid Waste", "waste-management", "Uttar Pradesh", 30)
    assert len(court_rows) == 0, f"Expected court news article to be blocked from RSS tenders, got {court_rows}"

    # Round 3 Dynamic Discovery & Exclusion Guards:
    # 1. Dynamic unseen query expansion ("Packaging Boxes", "Security Services", "Security Guard Services", "Solar Water Pump", "Dentist Clinic", "Catering")
    pkg_qs = get_gem_queries_for_category("packaging-boxes", "Packaging Boxes", [], location_hint="Pune")
    assert len(pkg_qs) >= 3 and "Packaging Boxes" in pkg_qs and "Packaging Box" in pkg_qs and "Packaging" in pkg_qs, f"Failed dynamic Packaging Boxes queries: {pkg_qs}"
    assert any("Pune" in q or "Maharashtra" in q for q in pkg_qs), f"Failed dynamic city/hub suffix in queries: {pkg_qs}"
    sec_qs = get_gem_queries_for_category("security-services", "Security Services", [])
    assert len(sec_qs) >= 3 and "Security Services" in sec_qs and "Security" in sec_qs, f"Failed dynamic Security Services queries: {sec_qs}"
    swp_qs = get_gem_queries_for_category("solar-water-pump", "Solar Water Pump", [])
    assert len(swp_qs) >= 3 and "Solar Water Pump" in swp_qs and "Solar Pump" in swp_qs, f"Failed dynamic Solar Water Pump queries: {swp_qs}"
    dent_qs = get_gem_queries_for_category("dentist-clinic", "Dentist Clinic", [])
    assert len(dent_qs) >= 3 and "Dentist" in dent_qs and "Dental" in dent_qs, f"Failed dynamic Dentist Clinic queries: {dent_qs}"
    cat_qs = get_gem_queries_for_category("catering", "Catering", [])
    assert len(cat_qs) >= 3 and "Catering" in cat_qs, f"Failed dynamic Catering queries: {cat_qs}"

    # 2. Plural/singular unseen keyword matching on GeM titles
    assert tender_matches_keyword("Corrugated Cardboard Box for Packaging", "Dept", "Packaging Boxes") is True, "Plural 'Packaging Boxes' missed singular 'Box'"

    # 3. Digital Marketing hardware printing-press AMC exclusion (e.g. GEM/2026/R/772737)
    dm_bad_title = "AMC/CMC for Pre-Owned 4-Colour Sheetfed Offset Printing Machine / Printing Unit"
    assert _has_excluded_tender_noise(dm_bad_title.lower(), "digital-marketing-seo") is True, "Failed digital-marketing printing-press AMC noise guard"
    assert tender_matches_keyword(dm_bad_title, "Security Printing", "Creative Agency") is False, "Failed tender_matches_keyword printing-press AMC block"
    assert tender_matches_keyword(dm_bad_title, "Security Printing", "Digital Marketing") is False, "Failed tender_matches_keyword printing-press AMC block (DM)"
    assert classify_tender({"title": dm_bad_title, "department": "SPMCIL"}) != "digital-marketing-seo", "Printing press AMC classified as Digital Marketing"

    dm_client = GemPortalClient()
    dm_fake_bids = [
        {"bid_id": "GEM_2026_R_772737", "bid_number": "GEM/2026/R/772737", "title": dm_bad_title, "department": "Press", "category_name": "GeM"},
        {"bid_id": "GEM_2026_B_999111", "bid_number": "GEM/2026/B/999111", "title": "Selection of Digital Marketing and Social Media Agency", "department": "UP Tourism", "category_name": "GeM"},
    ]
    with _mock.patch.object(dm_client, "scrape_keyword_tenders", return_value=dm_fake_bids):
        with _mock.patch("gem_scraper.fetch_gepnic_tenders_for_target", return_value=[]):
            with _mock.patch("gem_scraper.fetch_rss_tender_leads", return_value=[]):
                found_dm = discover_category_tenders(
                    dm_client, "digital-marketing-seo", "Digital Marketing & SEO",
                    [], run_failover=False, free_only=True, use_ai=False,
                )
    assert "GEM_2026_B_999111" in found_dm and "GEM_2026_R_772737" not in found_dm, f"GEM/2026/R/772737 leaked into Digital Marketing: {list(found_dm.keys())}"

    # 4. Dynamic location -> STATE_GEPNIC_PORTALS mapping (Pune -> mahatenders, Jaipur -> rajasthan, Tamil Nadu -> tntenders, Jharkhand -> jharkhandtenders)
    from free_tender_sources import get_gepnic_portals_for_location
    pune_portals = [u for u, _, _ in get_gepnic_portals_for_location("Pune")]
    jaipur_portals = [u for u, _, _ in get_gepnic_portals_for_location("Jaipur")]
    tn_portals = [u for u, _, _ in get_gepnic_portals_for_location("Tamil Nadu")]
    jhk_portals = [u for u, _, _ in get_gepnic_portals_for_location("Jharkhand")]
    guj_portals = [u for u, _, _ in get_gepnic_portals_for_location("Gujarat")]
    assert any("mahatenders.gov.in" in u for u in pune_portals), f"Pune failed to map to mahatenders: {pune_portals}"
    assert any("eproc.rajasthan.gov.in" in u for u in jaipur_portals), f"Jaipur failed to map to eproc.rajasthan: {jaipur_portals}"
    assert any("tntenders.gov.in" in u for u in tn_portals), f"Tamil Nadu failed to map to tntenders: {tn_portals}"
    assert any("jharkhandtenders.gov.in" in u for u in jhk_portals), f"Jharkhand failed to map to jharkhandtenders: {jhk_portals}"
    assert len(guj_portals) >= 2, f"Gujarat failed to return central fallback portals: {guj_portals}"

    # 5. Optional AI query expansion 24h cache test (mocked HTTP)
    import firebase_sync as _fbs_mod
    ai_calls = []
    def _fake_gem_ai_post(url, headers=None, json=None, timeout=10):
        ai_calls.append(1)
        return _FakeResp(200, {"candidates": [{"content": {"parts": [{"text": '{"queries": ["Corrugated Box", "Cardboard Carton", "Packaging Material"]}'}]}}]})
    with _mock.patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gem-ai-key", "KRSW_SUPABASE_URL": "", "KRSW_SUPABASE_PROJECT2_URL": "", "GROQ_API_KEY": "", "OPENAI_API_KEY": ""}):
        with _mock.patch.object(_fbs_mod.requests, "post", side_effect=_fake_gem_ai_post):
            qs_ai_1 = get_gem_queries_for_category("packaging-boxes", "Packaging Boxes", [], location_hint="India", use_ai=True)
            qs_ai_2 = get_gem_queries_for_category("packaging-boxes", "Packaging Boxes", [], location_hint="India", use_ai=True)
    assert len(ai_calls) == 1 and qs_ai_1 == qs_ai_2 and "Corrugated Box" in qs_ai_1, f"Failed GeM AI query cache test: calls={len(ai_calls)} qs={qs_ai_1}"

    # 6. Round 4 Deep Audit Guards:
    #    - 'Solar Pump' matches 'Solar Photovoltaic Water Pumping Systems'
    #    - Sub-niche prioritization for 'Dentist Clinic' ('Dental') & 'Security Guard Services' ('Security Guard')
    #    - Tier-2 dynamic Indian city -> state GePNIC mapping ('Warangal' -> Telangana, 'Ajmer' -> Rajasthan)
    #    - GeM AI <think> tag stripping
    assert tender_matches_keyword("Solar Photovoltaic Water Pumping Systems (V2) as per MNRE Specification", "UPNEDA", "Solar Pump") is True, "Failed 'Solar Pump' -> 'Pumping' stem match"
    assert "Water Pump" in swp_qs or "Solar Pumping" in swp_qs, f"Missing 'Water Pump'/'Solar Pumping' in Solar Water Pump queries: {swp_qs}"
    dent_focus_qs = get_gem_queries_for_category("doctors-medical-consultants", "Doctors & Medical Consultants", [{"niche": "Dentist Clinic", "sub_niche": "Dentist Clinic"}])
    assert "Dental" in dent_focus_qs[:4], f"'Dental' missing from top-4 Dentist Clinic focus queries: {dent_focus_qs}"
    sec_focus_qs = get_gem_queries_for_category("facility-security-services", "Facility Management & Security Services", [{"niche": "Security Guard Services", "sub_niche": "Security Guard Services"}], location_hint="Pune")
    assert "Security Guard" in sec_focus_qs[:4], f"'Security Guard' missing from top-4 Security Guard Services queries: {sec_focus_qs}"
    assert detect_indian_location("Warangal Municipal Corp") == ("Telangana", "Warangal")
    assert any("eproc.rajasthan.gov.in" in u for u, _, _ in get_gepnic_portals_for_location("Ajmer")), "Ajmer failed to map to Rajasthan GePNIC"

    def _fake_gem_ai_think_post(url, headers=None, json=None, timeout=10):
        return _FakeResp(200, {"choices": [{"message": {"content": '<think>internal {reasoning}</think>\n{"queries": ["Solar Pump", "Water Pumping System"]}'}}]})
    with _mock.patch.dict(os.environ, {"GROQ_API_KEY": "fake-groq-think", "KRSW_SUPABASE_URL": "", "KRSW_SUPABASE_PROJECT2_URL": "", "OPENAI_API_KEY": "", "GEMINI_API_KEY": ""}):
        with _mock.patch.object(_fbs_mod.requests, "post", side_effect=_fake_gem_ai_think_post):
            qs_think = _fbs_mod._expand_gem_queries_with_ai("solar-water-pump-think", "Solar Water Pump", location_hint="India")
    assert qs_think == ["Solar Pump", "Water Pumping System"], f"Failed GeM AI <think> stripping: {qs_think}"

    print("  [+] All GeM anti-false-positive, GePNIC, quota & dynamic location unit guards passed!")


def run_diagnostics():
    print("=" * 65)
    print("GeM AI Lead Finder - Local Diagnostic Test (V3)")
    print("=" * 65)
    run_unit_guards()

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

    # 3. Test Free GePNIC + RSS + Search Failover (Free-Only safe by default!)
    print("\n[3/4] Testing Free GePNIC & Search Failover Engine (free_only=True)...")
    failover_bids = search_gem_failover("Event Management", category_slug="wedding-event-management", free_only=("--live-serp" not in sys.argv))
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
    if "--unit" in sys.argv or "--test" in sys.argv:
        run_unit_guards()
        sys.exit(0)
    run_diagnostics()
