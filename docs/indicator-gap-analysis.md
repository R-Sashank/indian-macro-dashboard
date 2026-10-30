# What is missing: India's nowcasting indicators vs. the dashboard

Prepared 30 Oct 2026. The dashboard currently has 143 pages (see `site/data/index.json`).

## 1. Bottom line

- The dashboard already covers the **core activity block** well: IIP, core sector aggregate, PMIs, GST, credit,
  auto (cars, two-wheelers, tractors), cement, steel, air passengers, port cargo, trade headline items, markets and rates.
  Against **RBI's own 27-indicator Economic Activity Index**, it has **16 fully, 5 partly, 6 missing**.
  Against the **28 candidate indicators in RBI's GDP nowcasting paper**: **11 fully, 6 partly, 11 missing**.
- What is missing falls into five groups: **digital and administrative data** (UPI, e-way bills), **power and fuel detail**,
  **use-based IIP and core-sector components**, **surveys and sentiment** (RBI consumer and industry surveys),
  and the **global and financial-conditions block** (US/OECD indicators, NEER, VIX, T-bill and call rates).
- Several indicators the dashboard shows are **stale at the source** (railway freight stops Dec 2024, fertiliser Feb 2025,
  EPFO Jul 2025, CPI-Miscellaneous Dec 2025, cement Mar 2026), which matters for nowcasting.
- Almost everything missing can be obtained from **official, free primary sources** (RBI, MoSPI, NPCI, GSTN, PPAC, AAI, FRED/OECD/EIA),
  not from 1 Finance/CMIE. That is also the cleaner basis for a public site (see section 7).

## 2. Who uses what (evidence)

| Source | What it uses | How I got it |
|---|---|---|
| **RBI Economic Activity Index** (Bulletin, "An Economic Activity Index for India") | 27 indicators in four groups: Industry (8), Services (6), Global (8), Miscellaneous (5). Exact list in section 3. | Read from the RBI Bulletin article page. |
| **RBI working paper 03/2020**, "Nowcasting Indian GDP growth using a Dynamic Factor Model" | 28 candidates; three models (DF-6, DF-9, DF-12) built from 6, 9 and 12 of them. Section 4. | Read from the RBI publication page. |
| **RBI Bulletin, Nov 2025**, "Nowcasting GDP in India: A New Approach" | Dynamic factor model plus residual information | Press release only; I did not read the article. |
| **NCAER** nowcasting model | Sector-level dynamic factors; PMI services, bank credit, electricity, railway freight are named | A search summary only. NCAER's own page did not list them. |
| **2026 MPRA paper**, "Digital Public Infrastructure and Bayesian Nowcasting of India's GDP" | 13 indicators incl. GST, UPI, electricity, bank credit; adding **UPI transaction volume** gave the lowest out-of-sample error | Abstract only. The 13-indicator list is not in the abstract. |
| **ICRA Business Activity Monitor** | Auto production (2W, PV), vehicle registrations, Coal India output, power generation, rail freight, non-oil exports; later 14-16 indicators incl. deposits, non-food credit, air traffic | Press coverage. The component list has changed between editions. |
| **Finance Ministry Monthly Economic Review** | E-way bills, UPI value, auto and tractor sales, petrol/diesel consumption, PMIs, air passenger traffic | Press coverage of recent reviews. |
| Goldman Sachs, Nomura and similar broker indices | Not verified. I found nothing reliable on their components, and they are proprietary. | n/a |

## 3. Scorecard vs RBI's 27-indicator Economic Activity Index

Legend: ✔ have, ◐ partly (see note), ✘ missing.

**Industry (8)**: IIP ✔ · Automobile sales (total) ◐ *we have passenger cars and two-wheelers only* · Tractor sales ✔ ·
Cement production ✔ *(stale: Mar 2026)* · Steel consumption ✔ · Power supply ✔ *(as "Energy Requirements")* ·
Fuel consumption ✔ *(total petroleum only)* · PMI manufacturing ✔

**Services (6)**: Domestic air passengers ✔ · Domestic air cargo ✘ · Port cargo ✔ · Railway freight ◐ *(revenue series, stale since Dec 2024)* ·
Foreign tourist arrivals ✔ · PMI services ✔

**Global (8)**: US industrial production ✘ · Baltic Dry Index ✔ · US PMI manufacturing ✘ · OECD Composite Leading Indicator ✘ ·
US payrolls ✘ · Crude price (average of Brent, Dubai, WTI) ◐ *(Indian basket only)* · Non-oil exports ✔ · Non-oil non-gold imports ✔

**Miscellaneous (5)**: Gross taxes ✔ · Naukri JobSpeak Index ✘ · Non-food credit ◐ *(total and sector credit, not non-food)* ·
Broad money M3 ✔ · CPI non-food ◐ *(derivable from components)*

## 4. Scorecard vs RBI nowcasting paper (28 candidates)

✔ IIP-Core, Auto-Passenger, Air-Passenger, Non-oil non-gold imports, Foreign tourist arrivals, Bank credit, Power demand, Steel production,
Cement production, Forex reserves, Crude (Indian basket) (**11**)

◐ IIP-Consumer goods (we split durables/non-durables), Auto-Total, Rail freight, Exports (non-oil/sector only),
Oil consumption petrol & diesel (total only), Sensex (we have Nifty 50) (**6**)

✘ Auto-Commercial, Agricultural wages, Air cargo, Rail passenger, CPI-Industrial Workers, Government total receipts, NEER,
Treasury bill 91-day yield, CRB commodity index, OECD industrial production, CBOE VIX (**11**)

The paper's final models (the DF-6, DF-9 and DF-12 sets) used: IIP consumer goods, non-oil non-gold imports, auto sales total, rail freight,
air cargo, government total receipts; then IIP core, exports, foreign tourist arrivals; then bank credit, NEER, Sensex.

## 5. What to add

Effort is a rough guess until each source is actually tested. "Free" means no licence fee, not that reuse is unrestricted.

### Tier 1: used by RBI, NCAER, ICRA or the Finance Ministry

| # | Indicator | Why it matters | Likely free source | Freq | Effort |
|---|---|---|---|---|---|
| 1 | **UPI transactions, volume and value** (plus other payment systems) | Best single add in the 2026 paper; in the Finance Ministry's reviews | NPCI "UPI product statistics" page; RBI payment-system indicators | Monthly | Low |
| 2 | **GST e-way bills** (volume, value, by state) | Real-time freight and trade proxy; used by RBI, MoF, brokers | GST statistics on gst.gov.in | Monthly | Medium |
| 3 | **Power: peak demand, daily demand, generation by source** | NCAER, ICRA, MPRA all use electricity | Grid-India daily reports; CEA monthly | Daily/monthly | Medium-high |
| 4 | **Coal production and despatch** (Coal India output) | ICRA input; power and industry signal | Ministry of Coal / Coal India monthly releases | Monthly | Medium |
| 5 | **Fuel consumption split**: petrol, diesel, LPG, ATF | RBI uses petrol and diesel; diesel tracks freight and farm demand | PPAC | Monthly | Low-medium |
| 6 | **Total auto sales** incl. commercial vehicles and three-wheelers; **vehicle registrations** | RBI uses total auto sales; ICRA uses registrations | SIAM monthly; FADA; VAHAN dashboard | Monthly | Medium |
| 7 | **IIP by use-based group**: capital, intermediate, infrastructure/construction, primary, consumer goods | Capital goods is the investment signal; RBI uses consumer goods | MoSPI IIP release; eSankhyiki portal | Monthly | Low-medium |
| 8 | **Core sector, each of the 8 industries** (coal, crude, gas, refinery products, fertilisers, steel, cement, electricity) | We only have the aggregate | Office of the Economic Adviser, DPIIT | Monthly | Low-medium |
| 9 | **Air cargo; rail freight loading (tonnes) and rail passengers; container traffic** | RBI index and paper both use air cargo and rail freight | AAI monthly traffic; Indian Railways releases | Monthly | Low-medium |
| 10 | **Trade detail**: total goods exports and imports, gold imports, oil imports, services exports and imports, remittances | We have only net services and non-oil items | Commerce Ministry quick estimates; RBI monthly services data | Monthly | Low-medium |
| 11 | **Central government monthly accounts**: total receipts, direct taxes, expenditure, deficit vs budget estimate | RBI uses government receipts; fiscal impulse | Controller General of Accounts | Monthly | Medium (PDFs) |
| 12 | **Surveys and sentiment**: RBI Consumer Confidence Survey, Industrial Outlook Survey, Business Expectations, Survey of Professional Forecasters; PMI composite and sub-indices | Forward-looking; the only expectations data in the set | RBI publications and releases | Bi-monthly/quarterly | Medium |
| 13 | **Labour**: PLFS monthly, Naukri JobSpeak, agricultural wages | RBI uses JobSpeak and agricultural wages; PLFS is the official jobs series | MoSPI (PLFS); Info Edge press releases; Labour Bureau | Monthly | Medium; JobSpeak is low (press only) |
| 14 | **Financial conditions**: NEER/REER, India VIX, 91-day T-bill, call money rate/MIBOR, CP/CD rates | NEER, VIX, T-bill are in RBI's candidate set; policy transmission | RBI DBIE; NSE (VIX) | Daily/monthly | Medium |
| 15 | **Global block**: US industrial production, US payrolls, US PMI, OECD CLI, Brent/Dubai/WTI, CRB, Fed funds, DXY, US 10Y | RBI's index has 8 global inputs, we cover 3 | FRED, OECD, EIA (stable APIs). ISM PMI is licensed, so a proxy may be needed | Daily/monthly | Low |
| 16 | **Price detail**: CPI-IW, CPI sub-groups (vegetables, cereals, pulses, fuel and light, housing), WPI primary articles, daily retail prices (tomato, onion, potato, pulses) | CPI-IW in RBI paper; food drives CPI surprises | MoSPI / Labour Bureau; Dept of Consumer Affairs | Monthly/daily | Low-medium |

### Tier 2: used in research, or specialised

- **GDP detail**: GVA by the eight sectors and the expenditure components (exports, imports, change in stocks). Quarterly, MoSPI. Our nowcast targets are covered, but not the breakdown.
- **Housing**: RBI House Price Index (quarterly); private sales data (PropEquity, Knight Frank) has unclear reuse terms.
- **State level**: state GST, e-way bills, power demand.
- **Other payments**: NEFT, IMPS, cards, AePS (RBI payment-system indicators, fortnightly). FASTag is covered by our E-Toll series.
- **Satellite night-lights (VIIRS)**: research use; heavy processing, not a simple download.
- **Search and mobility**: Google Trends is possible but unofficial; Google's mobility reports were discontinued, so they are not an option.
- **Granular bank credit**: RBI's sectoral deployment of credit and NBFC credit.
- **Corporate results aggregates**: CMIE/Capitaline are paid, so excluded.

## 6. Existing pages that need attention

| Page | Latest data | Note |
|---|---|---|
| Railway Freight Traffic, Revenue | Dec 2024 | Stopped at source. Replace with loading in tonnes (item 9). |
| Fertiliser Sales | Feb 2025 | Stopped at source. |
| EPFO Payroll Additions | Jul 2025 | Lagging by over a year. |
| CPI Miscellaneous and Miscellaneous Rural | Dec 2025 | Probably the CPI base-year revision at MoSPI; needs confirming. |
| Cement Production | Mar 2026 | Stopped at source. Core-sector release (item 8) would cover it. |
| NIFTY Bank P/E and NIFTY IT P/E | n/a | Already flagged: the source serves identical data on both. |

## 7. Decisions needed before building

1. **Public or private, and licensing.** The current 143 pages come from 1 Finance and CMIE. That is fine for private research.
   "A one-stop site for everyone" means publishing, and redistributing CMIE-derived numbers publicly needs the owners' permission.
   Official sources are generally easier, but each has its own terms (for example RBI's website terms and the Government Open Data
   Licence for data.gov.in), so each must be checked. **Recommendation:** build every new indicator from official primary sources, and
   gradually replace the 1 Finance-derived pages with primary-source equivalents (IIP, CPI, GDP, trade, credit and others are all
   published by MoSPI, RBI or the Commerce Ministry). That also removes the dependency risk we hit when the source redesigned.
2. **Source discipline.** Every new indicator needs its own scraper with the same safeguards (fail loudly on layout change, never overwrite
   good data with truncated data). The data model already supports multiple series, units and a per-indicator source credit.
3. **A nowcasting layer.** Once the inputs exist, we could publish our own activity index built the way RBI's is (a dynamic factor model on
   these inputs). That would be the differentiator, but it needs care and honest back-testing. Treat it as a later phase.

## 8. Suggested order

- **Phase 1 (low effort, high value):** UPI; IIP use-based groups; core sector by industry; PPAC fuel split; air cargo and passengers (AAI);
  the global block from FRED/OECD/EIA; India VIX; NEER/REER.
- **Phase 2:** e-way bills; power and coal; auto sales and registrations (SIAM/FADA); central government accounts; RBI surveys;
  trade detail; call money, T-bill and CP/CD rates.
- **Phase 3:** labour (PLFS, JobSpeak, agricultural wages); housing; state-level data; our own composite index.

## 9. What I could not verify

- I did **not** test any of the new sources for download format or scrapability. The efforts above are estimates, and the first
  step of each build is checking the actual page or API.
- Some research came from press summaries or abstracts, noted in section 2. Verify the specific list in any paper before citing it.
- I could not find Goldman Sachs or Nomura indicator definitions.
