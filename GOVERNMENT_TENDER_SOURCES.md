# Master Government Tender & Procurement Sources Directory
**Zivic Nexus Platform — Dedicated Government Procurement Scraping Engine (`gem_lead_scraper`)**

> **Strict Operational Separation Rule**:
> This repository (`gem_lead_scraper`) is **exclusively dedicated** to government-associated tenders, public sector procurements, municipal bids, statutory authority notices, and public institution contracts.
> Non-government, commercial, and retail social leads MUST NEVER be scraped by this engine (those belong strictly to `ai_lead_scraper`).

---

## 1. Central Government & National Public Procurement Portals

| Portal / Organization | Primary URL | API / Data Feed Endpoint | Coverage & Focus |
|---|---|---|---|
| **GeM (Government e-Marketplace)** | `https://gem.gov.in` | `https://bidplus.gem.gov.in/showbidDocument/{id}` | All Central Ministries, PSUs, Autonomous Bodies, Defence, and State departments buying goods/services |
| **CPPP (Central Public Procurement Portal)** | `https://eprocure.gov.in/eprocure/app` | `https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata` | All high-value central government works, EPC infrastructure, health, and civil contracts |
| **IREPS (Indian Railways e-Procurement)** | `https://ireps.gov.in` | `https://ireps.gov.in/eps/search.do` | Railway zones, production units (RDSO Lucknow, DLW, CLW), STP/ETP, coach maintenance |
| **Defence Procurement Portal (MoD)** | `https://defproc.gov.in` | `https://defproc.gov.in/nicgep/app` | Army, Navy, Air Force, Military Engineer Services (MES), Ordnance Factory Board |
| **MSTC E-Commerce & Tenders** | `https://mstcecommerce.com` | `https://mstcecommerce.com/eproc/` | PSU procurement, mineral/scrap sales, heavy engineering tenders |
| **Coal India Limited e-Procurement** | `https://coalindiatenders.nic.in` | `https://coalindiatenders.nic.in/nicgep/app` | CIL subsidiaries (NCL, SECL, BCCL, ECL) environmental and heavy machinery bids |
| **NTPC Limited e-Tendering** | `https://eprocurentpc.nic.in` | `https://eprocurentpc.nic.in/nicgep/app` | Thermal power stations, ash pond wastewater, ETP water treatment, turbine AMC |
| **IOCL (Indian Oil Corporation)** | `https://iocletenders.nic.in` | `https://iocletenders.nic.in/nicgep/app` | Refineries, pipelines, retail marketing, wastewater effluent treatment and biochemicals |
| **ONGC Procurement Portal** | `https://tenders.ongc.co.in` | `https://tenders.ongc.co.in` | Oil and gas exploration, plant engineering, industrial safety and water utilities |
| **BHEL (Bharat Heavy Electricals)** | `https://bhel.com/tenders` | `https://bhel.com/tenders` | Industrial systems, power plants, manufacturing tooling and AMC services |
| **National Tenders Portal (NIC)** | `https://tenders.gov.in` | `https://tenders.gov.in` | Aggregated government tender notices across all ministries and states |

---

## 2. State e-Procurement Portals (Priority: Uttar Pradesh & Northern Hubs)

| State / Department | Official Portal | Focus & Key Buyer Bodies |
|---|---|---|
| **Uttar Pradesh e-Procurement** | `https://etender.up.nic.in` | Primary UP portal: UP PWD, UP Jal Nigam, Medical Education, Irrigation Dept |
| **UP Jal Nigam (Urban & Rural)** | `https://upjalnigam.co.in` | Municipal sewage treatment plants (STP), drinking water purification, effluent infrastructure |
| **UP Pollution Control Board (UPPCB)** | `https://uppcb.com/tenders.htm` | Environmental compliance audits, monitoring stations, laboratory supplies |
| **UP State Industrial Development (UPSIDA)**| `https://onlineupsidc.com` | Industrial parks in Lucknow (Amausi, Nadarganj, Talkatora, Chinhat) CETP plants |
| **SGPGI Lucknow & KGMU Tenders** | `https://sgpgims.org.in`, `https://kgmu.org` | Medical gas pipelines (MGPS), PSA Oxygen Plants, liquid oxygen, biomedical ETP |
| **Delhi Government e-Procurement** | `https://govtprocurement.delhi.gov.in` | Delhi Jal Board, PWD Delhi, Directorate of Health Services |
| **Haryana e-Tendering** | `https://etenders.hry.nic.in` | Haryana Urban Development (HSVP), Irrigation, Health Department |
| **Rajasthan e-Procurement** | `https://eproc.rajasthan.gov.in` | PHED Rajasthan, RIICO industrial areas, SMS Hospital Jaipur |
| **Madhya Pradesh e-Procurement** | `https://mptenders.gov.in` | MP Urban Development, Public Health Engineering, MP Jal Nigam |
| **Bihar e-Procurement (eProc 2.0)** | `https://eproc2.bihar.gov.in` | Bihar Rajya Jal Parishad, BUIDCO, Health Department Bihar |
| **Maharashtra e-Tenders** | `https://mahatenders.gov.in` | CIDCO, MIDC, Brihanmumbai Municipal Corporation (BMC), MJP |
| **Gujarat e-Procurement (nProcure)** | `https://tender.nprocure.com` | Gujarat Water Supply (GWSSB), GIDC industrial estates |

---

## 3. High-Liquidity Open Government Tender Aggregators

These aggregators ingest real-time Solr/XML feeds from GeM and CPPP, bypass Cloudflare bot challenges, and provide indexed, search-engine-crawlable tender records:

| Aggregator Platform | Index Domain | Why It Finds Leads Faster |
|---|---|---|
| **Avsar App** | `https://avsar.app` | Indexes 100% of live GeM bids daily, with clean URLs `https://avsar.app/tender/{gem-bid-id}`, exact closing dates, EMD, and buyer department |
| **BidEasy / InfraLens** | `https://tenders.infralens.in`, `https://bideasy.in` | Full historical and live tracking of GeM, CPPP, and state tenders with contract values and bidder rankings |
| **Tenderkart** | `https://tenderkart.in` | Categorizes tenders by CPV code, states (UP, Delhi, etc.), and industrial niches |
| **TendersOnTime** | `https://tendersontime.com` | Global and national government tenders search |
| **TenderTiger / Tender247** | `https://tendertiger.com`, `https://tender247.com` | Multi-source aggregator across Indian municipal corporations, defence, and PSUs |
| **BidDetail** | `https://biddetail.com` | Daily government notifications and downloadable RFP documents |

---

## 4. Query & Scraping Strategies in `gem_lead_scraper`

1. **Direct GeM Keyword Search**: Queries `https://bidplus.gem.gov.in` API with vendor-specific and category-pooled queries.
2. **Aggregator-Powered Failover**: When direct GeM returns Cloudflare Captcha or 403, `search_failover.py` immediately queries:
   - `site:avsar.app ("<niche>" OR "<sub-niche>") "<city/state>"`
   - `(site:bideasy.in OR site:tenders.infralens.in) ("<niche>") ("Tender" OR "GEM")`
   - `(site:etender.up.nic.in OR site:eprocure.gov.in) ("<niche>") "<city/state>"`
3. **Execution Budget**: Strictly enforced at **14 minutes** safety trigger (< 15-minute budget) to guarantee atomic write to Firestore and COMBLOPEZ `scraped_leads`.
