"""Power, coal and fuel indicators from official sources.

* CEA (Central Electricity Authority) via the National Power Portal npp.gov.in:
    - daily all-India generation by fuel (Daily Generation Report, sub-report 17)
    - daily coal stock position at thermal power stations (CEA Fuel Management Division)
  and cea.nic.in / gen-re.cea.gov.in:
    - daily renewable generation (wind / solar / other RES)
    - monthly Power Supply Position (energy and peak demand) PDFs, 2013 onwards
* Ministry of Coal "Monthly Statistics at a Glance" PDFs: coal production and despatch.
* PPAC (ppac.gov.in): consumption by product, crude production and imports, product trade.

Daily / long histories are backfilled once into scraper/sources/seed/*.json (see the
`backfill_*` functions at the bottom, which are run by hand, never by the scraper) and the
builders top them up with a short live window.  To stay correct when the seed gets old, the
builders also re-use the previously published series from site/data/i/<slug>.json, and RAISE
if the gap to refill is bigger than MAX_GAP_DAYS (the old data is then kept and the seed
should be extended with the backfill function).

grid-india.in (daily peak demand / energy met) has a broken TLS certificate and is not used;
there is therefore NO daily peak-demand series.  Monthly peak demand comes from CEA's Power
Supply Position report.
"""

from __future__ import annotations

import io
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import requests

from .base import (
    EPOCH, Http, clean_pts, iso, load_seed, merge_pts, record, register, save_seed, series,
    ts, ts_date, view, yoy_pts, yoy_view,
)

SECTION = "power-fuel"
ROOT = Path(__file__).resolve().parents[2]
ITEM_DIR = ROOT / "site" / "data" / "i"
MAX_GAP_DAYS = 60

DGR_URL = "https://npp.gov.in/public-reports/cea/daily/dgr/{d:%d-%m-%Y}/dgr17-{d:%Y-%m-%d}.xls"
COALSTOCK_URL = "https://npp.gov.in/public-reports/cea/daily/fuel/{d:%d-%m-%Y}/dailyCoal1-{d:%Y-%m-%d}.xls"
RE_URL = "https://gen-re.cea.gov.in/renewables/public/uploads/dailyReport/excel/Report-{d:%Y-%m-%d}.xlsx"
PSP_PAGE = "https://cea.nic.in/reports/dashboardView?headingId=107&subHeadingId=1003&subSubHeadingId={sid}&lang=en"
PSP_BASE = "https://cea.nic.in"
COAL_MSG_PAGE = "https://coal.nic.in/public-information/monthly-statistics-at-glance"
COAL_BASE = "https://coal.nic.in"
PPAC_AJAX = "https://ppac.gov.in/AjaxController/"
PPAC_REFERER = "https://ppac.gov.in/consumption/products-wise"

PROBE_URLS = [
    "https://npp.gov.in/public-reports/cea/daily/dgr/28-10-2026/dgr17-2026-10-28.xls",
    "https://npp.gov.in/public-reports/cea/daily/fuel/28-10-2026/dailyCoal1-2026-10-28.xls",
    "https://gen-re.cea.gov.in/renewables/public/uploads/dailyReport/excel/Report-2026-10-28.xlsx",
    "https://cea.nic.in/reports/dashboardView?headingId=107&subHeadingId=1003&subSubHeadingId=10010&lang=en",
    "https://cea.nic.in/reports/dashboardView?headingId=107&subHeadingId=1003&subSubHeadingId=10011&lang=en",
    "https://cea.nic.in/api/dashboardReports/download-pdf/en/2766e5eb-4c38-4c77-adea-119e20be73c3",
    "https://coal.nic.in/public-information/monthly-statistics-at-glance",
    "https://coal.nic.in/sites/default/files/2026-10/msg-sept26.pdf",
    "https://ppac.gov.in/consumption/products-wise",
    "https://ppac.gov.in/AjaxController/getConsumptionPetroleumProductsData  (POST financialYear=2026-2027&reportBy=1&pageId=43)",
    "https://ppac.gov.in/AjaxController/getProduction  (POST financialYear=2026-2027&reportBy=1&pageId=3)",
    "https://ppac.gov.in/AjaxController/getImportExports  (POST financialYear=2026-2027&reportBy=1&pageId=14)",
]


# ------------------------------------------------------------------ helpers

_last_req: dict = {}


def _polite(url: str, gap: float = 1.0) -> None:
    host = urlparse(url).netloc
    wait = gap - (time.time() - _last_req.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_req[host] = time.time()


def _get(http: Http, url: str, **kw) -> requests.Response:
    _polite(url)
    return http.get(url, **kw)


def _get_opt(http: Http, url: str, **kw):
    """GET returning bytes, or None when the file does not exist (HTTP 404)."""
    try:
        return _get(http, url, tries=2, **kw).content
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _pts_to_dict(pts):
    return {t: v for t, v in pts}


def _prev_series(slug: str, view_id: str, series_id: str) -> list:
    """Points of a series from the previously published item file (empty if unavailable)."""
    try:
        d = json.loads((ITEM_DIR / f"{slug}.json").read_text(encoding="utf-8"))
        for v in d["views"]:
            if view_id == "_all" or v.get("id") == view_id:
                for s in v["series"]:
                    if s["id"] == series_id:
                        return [[int(t), x] for t, x in s["pts"]]
    except Exception:  # noqa: BLE001
        pass
    return []


def _days_to_fetch(known_pts: list, today: date) -> list:
    """Dates to (re)fetch live: from 6 days before the last known day up to yesterday."""
    if not known_pts:
        raise RuntimeError("no seed history: run the backfill function first")
    last = (EPOCH + timedelta(seconds=known_pts[-1][0])).date()
    start = last - timedelta(days=6)
    end = today - timedelta(days=1)
    if (end - start).days > MAX_GAP_DAYS:
        raise RuntimeError(f"gap since {last} is too large for a live top-up; extend the seed with the backfill function")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _month_days(y: int, m: int) -> int:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return (nxt - date(y, m, 1)).days


def _monthly_from_daily(daily: list, mtd: dict) -> list:
    """Monthly totals: the report's own month-to-date figure on the month's last day when we
    have it, else the sum of the days if every day of the month is present."""
    by_month: dict = {}
    for t, v in daily:
        d = (EPOCH + timedelta(seconds=t)).date()
        by_month.setdefault((d.year, d.month), []).append(v)
    out = {}
    for (y, m), vals in by_month.items():
        t = ts(y, m, 1)
        last_ts = ts(y, m, _month_days(y, m))
        if last_ts in mtd:
            out[t] = round(mtd[last_ts], 3)
        elif len(vals) == _month_days(y, m):
            out[t] = round(sum(vals), 3)
    return [[t, out[t]] for t in sorted(out)]


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _fresh(last_ts: int, max_age_days: int, what: str) -> None:
    age = (_utc_today() - (EPOCH + timedelta(seconds=last_ts)).date()).days
    if age > max_age_days:
        raise RuntimeError(f"{what}: latest data point is {age} days old (limit {max_age_days})")



def _yoy(s: dict, label: str, vid: str):
    """yoy_view with a caller-chosen view id (so several can coexist in one record)."""
    v = yoy_view(s, label, frequency="M")
    if v:
        v["id"] = vid
    return v


QNA_NOTE_RESEARCH = "This is a flow measured over the period, so compare it with the same period a year ago (the YoY view) rather than with the previous period."


# ============================================================ CEA daily generation (DGR 17)

DGR_ROWS = {
    "COAL": "coal", "LIGNITE": "lignite", "NATURAL GAS": "gas", "TOTAL THERMAL": "thermal",
    "NUCLEAR": "nuclear", "HYDRO": "hydro", "BHUTAN IMP.": "bhutan", "TOTAL": "total",
}
DGR_NAMES = {
    "coal": "Coal", "lignite": "Lignite", "gas": "Natural gas", "thermal": "Thermal (total)",
    "nuclear": "Nuclear", "hydro": "Hydro", "bhutan": "Imports from Bhutan", "total": "Total (excl. RES)",
}


def parse_dgr17(content: bytes, d: date) -> dict:
    """-> {key: (today_actual_MU, month_to_date_actual_MU)}; raises on any surprise."""
    import xlrd

    ws = xlrd.open_workbook(file_contents=content).sheet_by_index(0)
    title = " ".join(str(c) for r in range(min(8, ws.nrows)) for c in ws.row_values(r) if isinstance(c, str))
    if "Fuel Wise Generation" not in title:
        raise ValueError("DGR17: title 'Category Wise- Fuel Wise Generation' not found")
    m = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", title)
    if not m or datetime.strptime(m.group(1), "%d-%b-%Y").date() != d:
        raise ValueError(f"DGR17: report date {m and m.group(1)} is not {d}")
    hdr = " ".join(str(c) for r in range(3, 7) for c in ws.row_values(r) if isinstance(c, str))
    if "Actual (MU)" not in hdr or "Program (MU)" not in hdr:
        raise ValueError("DGR17: unexpected column headers")
    out = {}
    for r in range(ws.nrows):
        vals = ws.row_values(r)
        strs = [c.strip() for c in vals if isinstance(c, str) and c.strip()]
        nums = [c for c in vals if _num(c)]
        if not strs or len(nums) < 7:
            continue
        key = DGR_ROWS.get(strs[0].upper())
        if key and key not in out:
            out[key] = (float(nums[2]), float(nums[4]))
    missing = set(DGR_ROWS.values()) - set(out)
    if missing:
        raise ValueError(f"DGR17: rows not found: {sorted(missing)}")
    today = {k: v[0] for k, v in out.items()}
    if not 500 <= today["total"] <= 8000:
        raise ValueError(f"DGR17: total generation {today['total']} MU outside plausible range")
    parts = today["thermal"] + today["nuclear"] + today["hydro"] + today["bhutan"]
    if abs(parts - today["total"]) > 0.01 * today["total"] + 1:
        raise ValueError("DGR17: total does not equal thermal+nuclear+hydro+Bhutan")
    if today["thermal"] + 1 < today["coal"] + today["lignite"] + today["gas"]:
        raise ValueError("DGR17: thermal smaller than coal+lignite+gas")
    for k, v in today.items():
        if v < 0 or v > 8000:
            raise ValueError(f"DGR17: {k}={v} out of range")
    return out


def _dgr_fetch(http, d):
    b = _get_opt(http, DGR_URL.format(d=d))
    return None if b is None else parse_dgr17(b, d)


def _skip_bad(fn, *a):
    """Live window: a single mislabelled/corrupt day file is skipped (the freshness check still
    raises if the latest good day gets too old, which is what a real layout change looks like)."""
    try:
        return fn(*a)
    except ValueError:
        return None


def _dgr_collect(days_data: dict):
    """days_data: {date: parsed} -> (daily {key: pts}, mtd {key: {ts: v}}) with MTD only on month-end days."""
    daily = {k: [] for k in DGR_ROWS.values()}
    mtd = {k: {} for k in DGR_ROWS.values()}
    for d in sorted(days_data):
        for k, (v, m) in days_data[d].items():
            daily[k].append([ts_date(d), round(v, 3)])
            if d.day == _month_days(d.year, d.month):
                mtd[k][ts_date(d)] = m
    return daily, mtd


@lru_cache(maxsize=1)
def _dgr_data(http_id: int = 0):
    http = _HTTP[0]
    keys = list(DGR_ROWS.values())
    seed = {k: load_seed(f"pf_dgr_{k}") for k in keys}
    seed_m = {k: load_seed(f"pf_dgrm_{k}") for k in keys}
    prev = {k: _prev_series("power-generation-daily", "daily", k) for k in keys}
    prev_m = {k: _prev_series("power-generation-daily", "monthly", k) for k in keys}
    known = merge_pts(seed["total"], prev["total"])
    live = {}
    for d in _days_to_fetch(known, _utc_today()):
        r = _skip_bad(_dgr_fetch, http, d)
        if r is not None:
            live[d] = r
    if not live:
        raise RuntimeError("DGR17: no daily report found in the live window")
    ldaily, lmtd = _dgr_collect(live)
    daily = {k: merge_pts(seed[k], prev[k], ldaily[k]) for k in keys}
    monthly = {}
    for k in keys:
        mtd_all = dict(lmtd[k])
        base = _monthly_from_daily(daily[k], mtd_all)
        monthly[k] = merge_pts(seed_m[k], prev_m[k], base)
    return daily, monthly


# ---- shared http for lru_cached data loaders (set by each builder)
_HTTP: list = [None]


@register("power-generation-daily", section=SECTION, name="Electricity Generation by Source (Daily, CEA)")
def power_generation_daily(http):
    _HTTP[0] = http
    _dgr_data.cache_clear()
    daily, monthly = _dgr_data()
    order = ["coal", "lignite", "gas", "thermal", "nuclear", "hydro", "bhutan", "total"]
    dser = [series(k, DGR_NAMES[k], "MU", daily[k]) for k in order]
    mser = [series(k, DGR_NAMES[k], "MU", monthly[k]) for k in order]
    _fresh(daily["total"][-1][0], 6, "power-generation-daily")
    if len(daily["total"]) < 1500:
        raise RuntimeError("daily generation history unexpectedly short")
    yoy_m = [series(k, DGR_NAMES[k], "Per cent", yoy_pts(monthly[k]))
             for k in ("coal", "thermal", "nuclear", "hydro", "total")]
    yoy_m = [s for s in yoy_m if s["pts"]]
    tot_d = series("total", "Total (excl. RES)", "MU", daily["total"])
    yd = yoy_pts(daily["total"])
    return record(
        "power-generation-daily", title="Electricity Generation by Source (Daily, CEA)",
        description=(
            "Daily all-India electricity generation by fuel from the Central Electricity Authority's Daily Generation "
            "Report (sub-report 17), in million units (MU = GWh) net of auxiliary use, for stations above 25 MW: coal, "
            "lignite, natural gas, nuclear, hydro, and imports from Bhutan. It EXCLUDES wind, solar and other small "
            "renewables (see the daily renewable generation indicator). Daily coal-fired output tracks industrial and "
            "household power demand almost in real time, which makes it one of the fastest read-throughs on activity; "
            "hydro and gas swing with the monsoon and fuel availability. The monthly view takes CEA's own month-to-date "
            "figure on the last day of the month (or the sum of the days where every day is present)."
        ),
        frequency="D",
        views=[
            view("Daily by source (MU)", *dser, vid="daily", frequency="D"),
            view("Daily total YoY % (same date last year)",
                 series("total", "Total (excl. RES)", "Per cent", yd), vid="daily_yoy", frequency="D") if yd else None,
            view("Monthly by source (MU)", *mser, vid="monthly", frequency="M"),
            view("Monthly YoY %", *yoy_m, vid="monthly_yoy", frequency="M") if yoy_m else None,
        ],
        source="Central Electricity Authority (National Power Portal), Daily Generation Report",
        source_url="https://npp.gov.in/publishedReports",
        qna=[
            {"question": "What does this measure?",
             "answer": "Actual generation on the day, in million units (MU), for all grid-connected thermal (coal, lignite, gas, diesel/naphtha), nuclear and hydro stations above 25 MW, plus imports from Bhutan. Rooftop and small solar/wind and other renewables are not in this file.^MU is the same as GWh; 4,000 MU a day is an average load of about 167 GW."},
            {"question": "How should I read it?",
             "answer": "Daily figures swing with weekdays, weather and the monsoon, so look at the YoY view and the monthly view for trend. Coal generation is the swing producer: when demand outruns renewables and hydro, coal output rises, so it is the cleanest daily read on underlying power demand. A cool or rainy spell lowers coal output even when activity is unchanged.^" + QNA_NOTE_RESEARCH},
            {"question": "Definitions, revisions and caveats",
             "answer": "Each day is taken from that day's CEA report ('Today's Actual'); CEA files are produced the next afternoon and some days can be revised in later cumulative figures, so very recent days are provisional. A few days are missing from the CEA archive (the file was never published) and are left blank, not filled in.^The monthly series uses the report's own month-to-date total on the last day of the month; it can differ slightly from CEA's final monthly generation statistics, which include later corrections. Because renewables are excluded, the total understates all-India generation (renewables are roughly a fifth of annual energy)."},
        ],
        notes="Seed history backfilled from CEA archive (2021-04-01 onward; the archive itself goes back to 2018); live window tops up the latest days.",
    )


# ============================================================ CEA daily coal stock

COAL_SERIES = {
    "stock_total": ("Coal stock at power stations (total)", "Thousand tonnes"),
    "stock_indigenous": ("Indigenous coal stock", "Thousand tonnes"),
    "stock_import": ("Imported coal stock", "Thousand tonnes"),
    "receipt": ("Coal receipt of the day", "Thousand tonnes"),
    "consumption": ("Coal consumption of the day", "Thousand tonnes"),
    "critical": ("Plants with critical stock", "Number of plants"),
    "pct_normative": ("Stock as % of normative requirement", "Per cent"),
}


def parse_coal_stock(content: bytes, d: date) -> dict:
    import xlrd

    ws = xlrd.open_workbook(file_contents=content).sheet_by_index(0)
    head = " ".join(str(c) for r in range(min(10, ws.nrows)) for c in ws.row_values(r) if isinstance(c, str))
    m = re.search(r"AS ON (\d{2}-\d{2}-\d{4})", head)
    if not m or datetime.strptime(m.group(1), "%d-%m-%Y").date() != d:
        raise ValueError(f"coal stock: report date {m and m.group(1)} is not {d}")
    for need in ("Actual Stock", "Indigenous", "Import", "Critical", "Receipt of the day", "Consumption of the day"):
        if need not in head:
            raise ValueError(f"coal stock: header {need!r} missing")
    row = None
    for r in range(ws.nrows):
        vals = ws.row_values(r)
        if any(isinstance(c, str) and c.strip().lower().startswith("grand total") for c in vals):
            row = vals
            break
    if row is None:
        raise ValueError("coal stock: 'Grand Total' row not found")
    nums = [c for c in row if _num(c)]
    pcts = [c for c in row if isinstance(c, str) and c.strip().endswith("%")]
    if len(nums) < 8:
        raise ValueError(f"coal stock: unexpected Grand Total row {nums} {pcts}")
    norm, ind, imp, tot, crit, rec, cons = (float(x) for x in nums[-7:])
    # some days omit the plant-load-factor % so the stock % cannot be told apart: leave it blank then
    pct = float(pcts[-1].strip().rstrip("%")) if len(pcts) >= 2 else None
    if abs(ind + imp - tot) > 1.0 + 0.001 * tot:
        raise ValueError("coal stock: total != indigenous + import (layout shifted?)")
    if not (2000 <= tot <= 120000 and 200 <= cons <= 4000 and 0 <= rec <= 6000 and 0 <= crit <= 300):
        raise ValueError(f"coal stock: implausible values total={tot} cons={cons} rec={rec} crit={crit}")
    if pct is not None and abs(tot / norm * 100 - pct) > 2.0:
        raise ValueError("coal stock: stock % of normative does not match stock/normative")
    return {"stock_total": tot, "stock_indigenous": ind, "stock_import": imp, "receipt": rec,
            "consumption": cons, "critical": crit, "pct_normative": pct}


@lru_cache(maxsize=1)
def _coal_stock_data(http_id: int = 0):
    http = _HTTP[0]
    keys = list(COAL_SERIES)
    seed = {k: load_seed(f"pf_cs_{k}") for k in keys}
    prev = {k: _prev_series("coal-stock-power-plants-daily", "_all", k) for k in keys}
    known = merge_pts(seed["stock_total"], prev["stock_total"])
    live = {k: [] for k in keys}
    n = 0
    for d in _days_to_fetch(known, _utc_today()):
        b = _get_opt(http, COALSTOCK_URL.format(d=d))
        if b is None:
            continue
        r = _skip_bad(parse_coal_stock, b, d)
        if r is None:
            continue
        n += 1
        for k in keys:
            if r[k] is not None:
                live[k].append([ts_date(d), r[k]])
    if not n:
        raise RuntimeError("coal stock: no report found in the live window")
    return {k: merge_pts(seed[k], prev[k], live[k]) for k in keys}


@register("coal-stock-power-plants-daily", section=SECTION, name="Coal Stock at Power Stations (Daily, CEA)")
def coal_stock_daily(http):
    _HTTP[0] = http
    _coal_stock_data.cache_clear()
    data = _coal_stock_data()
    _fresh(data["stock_total"][-1][0], 6, "coal stock")
    if len(data["stock_total"]) < 400:
        raise RuntimeError("coal stock history unexpectedly short")
    tot = series("stock_total", *COAL_SERIES["stock_total"], data["stock_total"])
    return record(
        "coal-stock-power-plants-daily", title="Coal Stock at Power Stations (Daily, CEA)",
        description=(
            "Daily coal stock at coal-based thermal power stations monitored by CEA's Fuel Management Division, in thousand "
            "tonnes: total, indigenous and imported, with the day's receipts and consumption, the number of plants with "
            "critical stock (below 25% of the normative requirement) and total stock as a percentage of the normative "
            "requirement. Falling stock with rising consumption is an early warning of supply constraints (and of strong "
            "power demand); rising stock signals either weak off-take or strong coal supply."
        ),
        frequency="D",
        views=[
            view("Stock position (thousand tonnes)", tot,
                 series("stock_indigenous", *COAL_SERIES["stock_indigenous"], data["stock_indigenous"]),
                 series("stock_import", *COAL_SERIES["stock_import"], data["stock_import"]), vid="daily", frequency="D"),
            view("Daily receipt and consumption", series("receipt", *COAL_SERIES["receipt"], data["receipt"]),
                 series("consumption", *COAL_SERIES["consumption"], data["consumption"]), vid="flows", frequency="D"),
            view("Stock as % of normative", series("pct_normative", *COAL_SERIES["pct_normative"], data["pct_normative"]),
                 vid="pct", frequency="D"),
            view("Plants with critical stock", series("critical", *COAL_SERIES["critical"], data["critical"]),
                 vid="critical", frequency="D"),
        ],
        source="Central Electricity Authority (Fuel Management Division), Daily Coal Report",
        source_url="https://npp.gov.in/publishedReports",
        qna=[
            {"question": "What does this measure?",
             "answer": "The coal in stock at the yard of each coal-based power station that reports to CEA, summed to an all-India Grand Total (the 'Grand Total = A+B+C+D' line of CEA's daily coal report), in thousand tonnes. It is a stock (a level on the day), split into indigenous and imported coal, with the day's receipts and consumption.^The report covers about 200 GW of coal-based capacity; imported-coal plants and captive/IPP plants are included."},
            {"question": "How should I read it?",
             "answer": "Compare the stock with the normative level (the stock each plant should hold, in days of requirement at 85% load factor). A reading below 100% of normative is not unusual; the number of plants below 25% of normative (critical) is the stress signal. Stocks normally build in the monsoon and fall in the summer demand peak.^Consumption of the day is a direct measure of coal burnt for power and moves with demand."},
            {"question": "Definitions, revisions and caveats",
             "answer": "Figures are as reported by stations the next day and are provisional; CEA does not publish revisions to old daily files. The report layout has changed over time (a daily-requirement column was added or dropped); the parser reads the Grand Total row and checks that indigenous plus imported stock equals total stock.^Daily files are available from mid-2024 only, so this series starts then. Days with no file are left blank."},
        ],
    )


# ============================================================ CEA daily renewable generation

RE_COLS = ("wind", "solar", "other", "total")


def parse_re(content: bytes, d: date) -> dict:
    import openpyxl

    ws = openpyxl.load_workbook(io.BytesIO(content), data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    head = " ".join(str(c) for r in rows[:8] for c in r if isinstance(c, str))
    if "Daily Renewable Generation Report" not in head:
        raise ValueError("RE: title not found")
    m = re.search(r"(\d{1,2})-([A-Za-z]+)-(\d{2})\b", " ".join(str(r[1]) for r in rows[:8] if r[1]))
    if not m:
        raise ValueError("RE: report date not found")
    rd = datetime.strptime(f"{m.group(1)}-{m.group(2)[:3]}-{m.group(3)}", "%d-%b-%y").date()
    if rd != d:
        raise ValueError(f"RE: report date {rd} is not {d}")
    for need in ("Wind", "Solar", "Total"):
        if need not in head:
            raise ValueError(f"RE: header {need!r} missing")
    row = None
    for r in rows:
        if r[0] and "All India" in str(r[0]):
            row = r
    if row is None:
        raise ValueError("RE: All India row not found")
    vals = [row[i] for i in range(1, 9)]
    if not all(_num(v) for v in vals):
        raise ValueError(f"RE: non-numeric All India row {vals}")
    wind, solar, other, total = (float(v) for v in vals[:4])
    mwind, msolar, mother, mtotal = (float(v) for v in vals[4:8])
    if abs(wind + solar + other - total) > 0.5 + 0.005 * total:
        raise ValueError("RE: components do not add to total")
    if not (0 <= total <= 2500 and 0 <= solar <= 2000 and 0 <= wind <= 1500):
        raise ValueError(f"RE: implausible values {vals}")
    # regional rows should sum to the all-India row
    reg = [r for r in rows if r[0] and "Region" in str(r[0]) and _num(r[4])]
    if len(reg) >= 5 and abs(sum(float(r[4]) for r in reg) - total) > 2 + 0.02 * total:
        raise ValueError("RE: regional totals do not add to All India")
    return {"wind": (wind, mwind), "solar": (solar, msolar), "other": (other, mother), "total": (total, mtotal)}


@lru_cache(maxsize=1)
def _re_data(http_id: int = 0):
    http = _HTTP[0]
    seed = {k: load_seed(f"pf_re_{k}") for k in RE_COLS}
    seed_m = {k: load_seed(f"pf_rem_{k}") for k in RE_COLS}
    prev = {k: _prev_series("renewable-generation-daily", "daily", k) for k in RE_COLS}
    prev_m = {k: _prev_series("renewable-generation-daily", "monthly", k) for k in RE_COLS}
    known = merge_pts(seed["total"], prev["total"])
    live = {}
    for d in _days_to_fetch(known, _utc_today()):
        b = _get_opt(http, RE_URL.format(d=d))
        if b is not None:
            r = _skip_bad(parse_re, b, d)
            if r is not None:
                live[d] = r
    if not live:
        raise RuntimeError("RE: no daily report found in the live window")
    daily = {k: [] for k in RE_COLS}
    mtd = {k: {} for k in RE_COLS}
    for d in sorted(live):
        for k in RE_COLS:
            daily[k].append([ts_date(d), round(live[d][k][0], 3)])
            if d.day == _month_days(d.year, d.month):
                mtd[k][ts_date(d)] = live[d][k][1]
    out_d, out_m = {}, {}
    for k in RE_COLS:
        out_d[k] = merge_pts(seed[k], prev[k], daily[k])
        out_m[k] = merge_pts(seed_m[k], prev_m[k], _monthly_from_daily(out_d[k], mtd[k]))
    return out_d, out_m


@register("renewable-generation-daily", section=SECTION, name="Wind, Solar and Other Renewable Generation (Daily, CEA)")
def renewable_daily(http):
    _HTTP[0] = http
    _re_data.cache_clear()
    daily, monthly = _re_data()
    _fresh(daily["total"][-1][0], 6, "renewable generation")
    if len(daily["total"]) < 400:
        raise RuntimeError("renewable history unexpectedly short")
    names = {"wind": "Wind", "solar": "Solar", "other": "Other RES (biomass, bagasse, small hydro)", "total": "Total renewables"}
    order = ["wind", "solar", "other", "total"]
    yoy_m = [series(k, names[k], "Per cent", yoy_pts(monthly[k])) for k in order]
    yoy_m = [s for s in yoy_m if s["pts"]]
    yd = yoy_pts(daily["total"])
    return record(
        "renewable-generation-daily", title="Wind, Solar and Other Renewable Generation (Daily, CEA)",
        description=(
            "Daily all-India generation from renewable energy sources (wind, solar, and other RES such as biomass, "
            "bagasse and small hydro) from CEA's Daily Renewable Generation Report, in million units (MU) net. Large hydro "
            "is not part of this series (it is in the daily generation-by-source indicator). Solar and wind output explains "
            "much of the daily swing in coal generation, so this series is needed to read daily power demand correctly."
        ),
        frequency="D",
        views=[
            view("Daily (MU)", *[series(k, names[k], "MU", daily[k]) for k in order], vid="daily", frequency="D"),
            view("Daily total YoY %", series("total", names["total"], "Per cent", yd), vid="daily_yoy", frequency="D") if yd else None,
            view("Monthly (MU)", *[series(k, names[k], "MU", monthly[k]) for k in order], vid="monthly", frequency="M"),
            view("Monthly YoY %", *yoy_m, vid="monthly_yoy", frequency="M") if yoy_m else None,
        ],
        source="Central Electricity Authority (Renewable Project Monitoring Division), Daily Renewable Generation Report",
        source_url="https://cea.nic.in/reports/external/daily-renewable?lang=en",
        qna=[
            {"question": "What does this measure?",
             "answer": "Energy generated on the day by grid-connected wind, solar and other renewable plants, all India, in million units (MU) net. It is the 'All India' row of CEA's daily renewable report, which also splits it by state and region.^Large hydro, nuclear and thermal are in the daily generation-by-source indicator."},
            {"question": "How should I read it?",
             "answer": "Solar output is highly seasonal (lowest in the monsoon, highest in spring) and wind peaks in the south-west monsoon, so use the YoY view. Strong YoY growth is mostly capacity additions, not demand; to read demand, add this series to the daily generation series (total generation = both).^A rise in renewables with flat coal output means falling reliance on coal, not weak demand."},
            {"question": "Definitions, revisions and caveats",
             "answer": "Data are reported by state load dispatch centres and are provisional; they cover only the renewable capacity that reports to CEA (not rooftop/behind-the-meter solar). The monthly series uses the report's cumulative month figure on the last day of the month.^The daily file archive starts on 30 June 2024; missing days are left blank. The source host is gen-re.cea.gov.in (a CEA host that is separate from cea.nic.in)."},
        ],
    )


# ============================================================ CEA monthly Power Supply Position

_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"]


def _psp_listing(http, sid: str) -> dict:
    """{ 'YYYY-MM': pdf_url } from the CEA dashboardView page."""
    from bs4 import BeautifulSoup

    html = _get(http, PSP_PAGE.format(sid=sid)).text
    soup = BeautifulSoup(html, "lxml")
    out = {}
    for a in soup.find_all("a", href=True):
        if "/api/dashboardReports/download-pdf/en/" not in a["href"]:
            continue
        row = a.find_parent("tr")
        m = re.search(r"Power Supply Position - (?:Energy|Peak)\s+(\d{4}-\d{2})", row.get_text(" ", strip=True) if row else "")
        if m:
            out[m.group(1)] = PSP_BASE + a["href"]
    if len(out) < 100:
        raise RuntimeError(f"PSP listing {sid}: only {len(out)} reports found (layout changed?)")
    return out


def parse_psp(pdf: bytes, ym: str, kind: str) -> tuple:
    """-> (requirement-or-peak-demand, supplied-or-peak-met) in MU / MW for the month `ym`."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf)) as p:
        text = "\n".join((pg.extract_text() or "") for pg in p.pages)
    y, mth = int(ym[:4]), int(ym[5:])
    hm = re.search(r"(" + "|".join(_MONTHS) + r")\s*,\s*(\d{4})", text)
    if not hm or (hm.group(1), int(hm.group(2))) != (_MONTHS[mth - 1], y):
        raise ValueError(f"PSP {kind} {ym}: heading month {hm and hm.group(0)} does not match")
    # CEA has uploaded the wrong report under some months; check the report type by its English title
    if kind == "E" and "Energy Requirement" not in text:
        raise ValueError(f"PSP {ym}: not an energy supply position report")
    if kind == "P" and "Peak Demand" not in text:
        raise ValueError(f"PSP {ym}: not a peak demand report")
    row = None
    # the numbers may sit on the line after the 'All India' label in some years
    for mm in re.finditer(r"All India\s+(-?[\d,]+)\s+(-?[\d,]+)\s+(-?[\d,]+)\s+(-?\d+\.\d)(?:\s|$)", text):
        row = mm
        break
    if row is None:
        raise ValueError(f"PSP {kind} {ym}: All India row not found")
    a, b, c = (int(row.group(i).replace(",", "")) for i in (1, 2, 3))
    if abs(abs(c) - abs(a - b)) > 3 + 0.001 * a:
        raise ValueError(f"PSP {kind} {ym}: requirement-supplied != shortage ({a}, {b}, {c})")
    if kind == "E" and not 40000 <= a <= 250000:
        raise ValueError(f"PSP E {ym}: energy requirement {a} implausible")
    if kind == "P" and not 90000 <= a <= 350000:
        raise ValueError(f"PSP P {ym}: peak demand {a} implausible")
    if b > a * 1.02 or b < a * 0.8:
        raise ValueError(f"PSP {kind} {ym}: supplied {b} implausible vs requirement {a}")
    return float(a), float(b)


PSP_KEYS = ("e_req", "e_sup", "p_dem", "p_met")


def _psp_collect(http, listings: dict, months: list) -> dict:
    out = {k: [] for k in PSP_KEYS}
    for ym in months:
        for kind, sid, kreq, ksup in (("E", "10010", "e_req", "e_sup"), ("P", "10011", "p_dem", "p_met")):
            url = listings[kind].get(ym)
            if not url:
                continue
            a, b = parse_psp(_get(http, url).content, ym, kind)
            t = ts(int(ym[:4]), int(ym[5:]), 1)
            out[kreq].append([t, a])
            out[ksup].append([t, b])
    return out


@lru_cache(maxsize=1)
def _psp_data(http_id: int = 0):
    http = _HTTP[0]
    listings = {"E": _psp_listing(http, "10010"), "P": _psp_listing(http, "10011")}
    seed = {k: load_seed(f"pf_psp_{k}") for k in PSP_KEYS}
    prev = {k: _prev_series("power-supply-position-monthly", "_all", k) for k in PSP_KEYS}
    known = {k: merge_pts(seed[k], prev[k]) for k in PSP_KEYS}
    have = set.intersection(*[set(t for t, _ in known[k]) for k in PSP_KEYS])
    avail = sorted(set(listings["E"]) & set(listings["P"]))
    # fetch every published month we do not hold, plus the latest two (they get revised)
    # (months older than a year that the seed lacks are CEA upload errors, not new data: ignore them)
    todo = [ym for ym in avail[-12:] if ts(int(ym[:4]), int(ym[5:]), 1) not in have]
    todo = sorted(set(todo) | set(avail[-2:]))
    y0, m0 = int(avail[-12][:4]), int(avail[-12][5:])
    if not have or max(have) < ts(y0, m0, 1):
        raise RuntimeError("PSP: seed/previous data older than 12 months; extend the seed with backfill_psp")
    live = _psp_collect(http, listings, todo)
    return {k: merge_pts(seed[k], prev[k], live[k]) for k in PSP_KEYS}


def _psp_pct_short(req, sup):
    r = _pts_to_dict(req)
    return [[t, round((r[t] - v) / r[t] * 100, 3)] for t, v in sup if t in r and r[t] > 0]


@register("power-supply-position-monthly", section=SECTION, name="Power Supply Position: Energy Supplied and Peak Demand (Monthly, CEA)")
def psp_monthly(http):
    _HTTP[0] = http
    _psp_data.cache_clear()
    d = _psp_data()
    _fresh(d["e_sup"][-1][0], 100, "power supply position")
    if len(d["e_sup"]) < 150 or len(d["p_met"]) < 150:
        raise RuntimeError("PSP history unexpectedly short")
    e_short = _psp_pct_short(d["e_req"], d["e_sup"])
    p_short = _psp_pct_short(d["p_dem"], d["p_met"])
    sup = series("e_sup", "Energy supplied (energy met)", "MU", d["e_sup"])
    pm = series("p_met", "Peak demand met", "MW", d["p_met"])
    pd_ = series("p_dem", "Peak demand", "MW", d["p_dem"])
    return record(
        "power-supply-position-monthly",
        title="Power Supply Position: Energy Supplied and Peak Demand (Monthly, CEA)",
        description=(
            "All-India monthly electricity supply position from CEA's Power Supply Position reports: energy supplied "
            "(energy met) in million units, peak demand and peak demand met in MW, and the shortfall of each against "
            "requirement in per cent. Peak demand is the highest all-India demand of the month, so it captures weather "
            "and activity extremes that the monthly energy total hides; energy supplied is the monthly electricity "
            "consumption proxy for nowcasting industrial and services activity. History starts January 2013."
        ),
        frequency="M",
        views=[
            view("Energy supplied (MU)", sup, vid="energy", frequency="M"),
            _yoy(sup, "Energy supplied YoY %", "energy_yoy"),
            view("Peak demand and peak met (MW)", pd_, pm, vid="peak", frequency="M"),
            _yoy(pm, "Peak demand met YoY %", "peak_yoy"),
            view("Shortage (% of requirement)",
                 series("e_short", "Energy shortage", "Per cent", e_short),
                 series("p_short", "Peak shortage", "Per cent", p_short), vid="short", frequency="M"),
        ],
        source="Central Electricity Authority, Power Supply Position (Energy / Peak) reports",
        source_url="https://cea.nic.in/reports/dashboardView?headingId=107&subHeadingId=1003&subSubHeadingId=10010&lang=en",
        qna=[
            {"question": "What does this measure?",
             "answer": "Energy supplied is the electricity actually delivered to consumers in the month, in million units (MU), net of auxiliary consumption, all India. Peak demand is the highest instantaneous all-India demand in the month (in MW) and peak demand met is the part of it that was supplied. Shortage is (requirement minus supplied) as a per cent of requirement.^Energy requirement (the matching demand figure) is already on the dashboard as Energy Requirements."},
            {"question": "How should I read it?",
             "answer": "Use the YoY view for energy (it removes the seasonal pattern). Peak demand reflects weather: it spikes in April-June and again in September when monsoon breaks, so compare the same month across years. Shortage is now below 1%, so supplied is almost equal to requirement; a rise in shortage is a stress signal."},
            {"question": "Definitions, revisions and caveats",
             "answer": "CEA publishes each month's report with a lag of about two months and revises it ('Revised' in the report title). The series is built from the all-India row of each month's PDF; the parser checks that requirement minus supplied equals the reported shortage. The sign convention of the shortage columns changed over the years, so shortage here is recomputed as requirement minus supplied.^Reports before 2015 used slightly different state coverage (for example, the Dadra & Nagar Haveli and Daman & Diu merger), and Andaman-Nicobar and Lakshadweep are shown separately and are not in regional totals; the all-India row is used as published."},
        ],
    )


# ============================================================ Ministry of Coal monthly statistics

_MON3 = {m[:3].lower(): i + 1 for i, m in enumerate(_MONTHS)}

COAL_KEYS = {
    "prod_total": ("Total coal production", "Million tonnes"),
    "prod_cil": ("Coal India Ltd (CIL)", "Million tonnes"),
    "prod_sccl": ("Singareni Collieries (SCCL)", "Million tonnes"),
    "prod_capt": ("Captive and other mines", "Million tonnes"),
    "desp_total": ("Total coal despatch", "Million tonnes"),
    "desp_cil": ("Coal India Ltd (CIL)", "Million tonnes"),
    "desp_sccl": ("Singareni Collieries (SCCL)", "Million tonnes"),
    "desp_capt": ("Captive and other mines", "Million tonnes"),
}


def _nums_in(line: str) -> list:
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*\.?\d*", line)]


def _cur_prev(nums: list):
    """Find (current, previous-year) of the monthly block: a pair followed by its growth figure
    (the PDF prints growth with an arrow, so its sign is lost; compare magnitudes, allowing for
    the 2-decimal rounding of the inputs)."""
    for i in range(len(nums) - 2):
        for k in (i + 1, i + 2):
            if k + 1 < len(nums) and nums[k] > 0.005 and nums[i] > 0:
                a, b = nums[i], nums[k]
                lo = ((a - 0.005) / (b + 0.005) - 1) * 100 - 0.03
                hi = ((a + 0.005) / (b - 0.005) - 1) * 100 + 0.03
                if lo <= nums[k + 1] <= hi or lo <= -nums[k + 1] <= hi:
                    return a, b
    return None


def parse_coal_msg(pdf: bytes) -> dict:
    """-> {'month': (y, m), 'cur': {key: v}, 'prev': {key: v}} for production and despatch."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf)) as p:
        pages = [(pg.extract_text() or "") for pg in p.pages[:5]]
    text = "\n".join(pages)
    mt = re.search(r"Statistics for\s*([A-Za-z]+)\W*(\d{4})", text)
    if not mt or mt.group(1)[:3].lower() not in _MON3:
        raise ValueError("coal msg: title month not found")
    ym = (int(mt.group(2)), _MON3[mt.group(1)[:3].lower()])
    # split into sections by their headings
    sec_re = re.compile(r"^(Coking Coal Production|Coal Production|Coal Despatch|Coal Dispatch|Coal Offtake|Non[- ]?Coking[^\n]*)", re.M | re.I)
    marks = [(m.start(), m.group(1).lower()) for m in sec_re.finditer(text)]
    sections: dict = {}
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        kind = ("coking" if name.startswith("coking") else "prod" if "production" in name
                else "desp" if name.startswith(("coal desp", "coal disp", "coal offt")) else "other")
        if kind in ("prod", "desp"):
            sections[kind] = sections.get(kind, "") + text[pos:end] + chr(10)
    if set(sections) != {"prod", "desp"}:
        raise ValueError(f"coal msg: sections found {sorted(sections)}")
    cur, prev = {}, {}
    labels = {"cil": r"^CIL\s+\d", "sccl": r"^(?:\d+\s+)?SCCL\s+\d",
              "capt": r"^(?:\d+\s+)?Captives(?:\s*/\s*Others)?\s+\d", "total": r"^Grand Total\s+\d"}
    for kind, txt in sections.items():
        for lk, rx in labels.items():
            line = next((ln for ln in txt.split("\n") if re.search(rx, ln.strip())), None)
            if line is None:
                raise ValueError(f"coal msg: row {lk} missing in {kind}")
            body = re.sub(r"^(?:\d+\s+)?(?:CIL|SCCL|Captives(?:\s*/\s*Others)?|Grand Total)", "", line.strip(), flags=re.I)
            pair = _cur_prev(_nums_in(body))
            if pair is None:
                raise ValueError(f"coal msg: cannot read {kind}/{lk}: {line!r}")
            cur[f"{kind}_{lk}"], prev[f"{kind}_{lk}"] = pair
    for kind in ("prod", "desp"):
        for dct in (cur, prev):
            parts = dct[f"{kind}_cil"] + dct[f"{kind}_sccl"] + dct[f"{kind}_capt"]
            if abs(parts - dct[f"{kind}_total"]) > 0.15 + 0.003 * dct[f"{kind}_total"]:
                raise ValueError(f"coal msg: {kind} components {parts} != total {dct[f'{kind}_total']}")
            if not 20 <= dct[f"{kind}_total"] <= 200:
                raise ValueError(f"coal msg: {kind} total {dct[f'{kind}_total']} implausible")
    return {"month": ym, "cur": cur, "prev": prev}


def _coal_msg_links(http) -> list:
    """PDF URLs on the Monthly Statistics page, newest first."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_get(http, COAL_MSG_PAGE).text, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if re.search(r"/sites/default/files/\d{4}-\d{2}/msg[^/]*\.pdf$", h, re.I):
            links.append(COAL_BASE + h if h.startswith("/") else h)
    if len(links) < 20:
        raise RuntimeError(f"coal msg listing: only {len(links)} PDFs found")
    return links


def _coal_add(res: dict, store: dict) -> None:
    y, m = res["month"]
    store.setdefault("cur", {})[(y, m)] = res["cur"]
    store.setdefault("prev", {})[(y - 1, m)] = res["prev"]


def _coal_series_from(store: dict) -> dict:
    """Prior-year values (revised) override the provisional current-month values of 12 months ago."""
    out = {k: {} for k in COAL_KEYS}
    for src in ("cur", "prev"):
        for (y, m), vals in sorted(store.get(src, {}).items()):
            for k, v in vals.items():
                out[k][ts(y, m, 1)] = v
    return {k: [[t, round(v, 3)] for t, v in sorted(d.items())] for k, d in out.items()}


@lru_cache(maxsize=1)
def _coal_data(http_id: int = 0):
    http = _HTTP[0]
    seed = {k: load_seed(f"pf_coal_{k}") for k in COAL_KEYS}
    prev = {k: _prev_series("coal-production-despatch-monthly", "_all", k) for k in COAL_KEYS}
    links = _coal_msg_links(http)
    store: dict = {}
    for url in links[:3][::-1]:
        _add = parse_coal_msg(_get(http, url).content)
        _coal_add(_add, store)
    live = _coal_series_from(store)
    return {k: merge_pts(seed[k], prev[k], live[k]) for k in COAL_KEYS}


@register("coal-production-despatch-monthly", section=SECTION, name="Coal Production and Despatch (Monthly, Ministry of Coal)")
def coal_monthly(http):
    _HTTP[0] = http
    _coal_data.cache_clear()
    d = _coal_data()
    _fresh(d["prod_total"][-1][0], 75, "coal production")
    _fresh(d["desp_total"][-1][0], 75, "coal despatch")
    if len(d["prod_total"]) < 50:
        raise RuntimeError("coal history unexpectedly short")
    def s(k):
        return series(k, *COAL_KEYS[k], d[k])
    prod, desp = s("prod_total"), s("desp_total")
    return record(
        "coal-production-despatch-monthly",
        title="Coal Production and Despatch (Monthly, Ministry of Coal)",
        description=(
            "All-India monthly raw coal production and despatch (offtake) in million tonnes from the Ministry of Coal's "
            "Monthly Statistics at a Glance, split into Coal India Ltd, Singareni Collieries and captive/other mines. "
            "Despatch is what actually leaves the mines for power plants, steel, cement and other users, so it is the "
            "better demand signal; production minus despatch shows pit-head stock build. Coal supplies about 70% of "
            "India's electricity, making this a core input for power and industrial nowcasts."
        ),
        frequency="M",
        views=[
            view("Production (MT)", prod, s("prod_cil"), s("prod_sccl"), s("prod_capt"), vid="prod", frequency="M"),
            _yoy(prod, "Production YoY %", "prod_yoy"),
            view("Despatch (MT)", desp, s("desp_cil"), s("desp_sccl"), s("desp_capt"), vid="desp", frequency="M"),
            _yoy(desp, "Despatch YoY %", "desp_yoy"),
        ],
        source="Ministry of Coal, Monthly Statistics at a Glance",
        source_url=COAL_MSG_PAGE,
        qna=[
            {"question": "What does this measure?",
             "answer": "Raw coal (coking and non-coking) produced and despatched in the month, in million tonnes (MT), by Coal India Ltd and its subsidiaries, Singareni Collieries Company Ltd and captive/commercial mines. Lignite is reported separately by the Ministry and is not included.^'Production' is output at the pit head; 'despatch' is coal moved out by rail, road, MGR and other modes to consumers."},
            {"question": "How should I read it?",
             "answer": "Use the YoY view. Growth in despatch above production means stock is being drawn down. Coal despatch to the power sector rises with electricity demand and falls when hydro and renewables are abundant (monsoon months). Captive and commercial mines are the fastest-growing segment, so compare CIL on its own for the legacy trend."},
            {"question": "Definitions, revisions and caveats",
             "answer": "Each monthly PDF is provisional and also gives the same month of the previous year (revised). The series uses the provisional figure for the latest months and the revised figure once a later PDF restates it, so recent months can be revised slightly.^Months before mid-2021 are not available from these PDFs. The parser checks that the three producers add to the grand total and that growth rates quoted in the PDF match the numbers."},
        ],
    )


# ============================================================ PPAC (petroleum)

_PPAC_MONTHS = ["april", "may", "june", "july", "august", "september", "october", "november", "december",
                "january", "february", "march"]


def _strip(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(s))).strip()


def _ppac_fys(first: int = 2020) -> list:
    today = _utc_today()
    cur = today.year if today.month >= 4 else today.year - 1
    return [f"{y}-{y + 1}" for y in range(first, cur + 1)]


def _ppac_rows(http, endpoint: str, page_id: str, report_by: str, fy: str) -> list:
    _polite(PPAC_AJAX)
    r = http.post(PPAC_AJAX + endpoint, data={"financialYear": fy, "reportBy": report_by, "pageId": page_id},
                  headers={"X-Requested-With": "XMLHttpRequest", "Referer": PPAC_REFERER})
    res = r.json().get("result")
    if not res:
        raise RuntimeError(f"PPAC {endpoint} {fy}: empty result")
    rows = list(res.values()) if isinstance(res, dict) else list(res)
    return rows


def _ppac_monthly(http, endpoint: str, page_id: str, report_by: str, wanted: dict, section_row=None, first_fy: int = 2020) -> dict:
    """{key: pts} from per-FY tables. `wanted` maps normalised title -> key; first row with
    that title (after the optional section marker row) is used."""
    out = {k: {} for k in wanted.values()}
    for fy in _ppac_fys(first_fy):
        rows = _ppac_rows(http, endpoint, page_id, report_by, fy)
        started = section_row is None
        seen = set()
        y0 = int(fy[:4])
        for row in rows:
            title = _strip(row.get("title", ""))
            if section_row is not None and not started:
                if title.upper().startswith(section_row):
                    started = True
                continue
            key = wanted.get(title.upper())
            if key is None or key in seen:
                continue
            seen.add(key)
            for i, mname in enumerate(_PPAC_MONTHS):
                v = _strip(row.get(mname, ""))
                if v == "":
                    continue
                try:
                    val = float(v.replace(",", ""))
                except ValueError:
                    raise RuntimeError(f"PPAC {endpoint} {fy} {title} {mname}: bad value {v!r}")
                y = y0 if i < 9 else y0 + 1
                out[key][ts(y, (i + 3) % 12 + 1, 1)] = val
        if not (set(wanted.values()) <= seen):
            raise RuntimeError(f"PPAC {endpoint} {fy}: rows not found {sorted(set(wanted.values()) - seen)}")
    return {k: [[t, v] for t, v in sorted(d.items())] for k, d in out.items()}


PPAC_PRODUCTS = {
    "MS": ("ppac-petrol-consumption", "Petrol (Motor Spirit)", "MS"),
    "HSD": ("ppac-diesel-consumption", "Diesel (HSD)", "HSD"),
    "LPG": ("ppac-lpg-consumption", "LPG", "LPG"),
    "ATF": ("ppac-atf-consumption", "Aviation Turbine Fuel (ATF)", "ATF"),
    "NAPHTHA": ("ppac-naphtha-consumption", "Naphtha", "Naphtha"),
    "SKO": ("ppac-kerosene-consumption", "Kerosene (SKO)", "SKO"),
    "PETROLEUM COKE": ("ppac-petcoke-consumption", "Petroleum Coke", "Petcoke"),
    "BITUMEN": ("ppac-bitumen-consumption", "Bitumen", "Bitumen"),
    "FO & LSHS": ("ppac-fuel-oil-consumption", "Fuel Oil and LSHS", "FO"),
    "LUBRICANTS & GREASES": ("ppac-lubricants-consumption", "Lubricants and Greases", "Lubes"),
}

# plausible monthly range ('000 tonnes) per product, to catch unit or layout shifts
PPAC_RANGE = {
    "MS": (800, 6000), "HSD": (2500, 11000), "LPG": (1500, 3500), "ATF": (20, 1000), "NAPHTHA": (500, 1800),
    "SKO": (5, 300), "PETROLEUM COKE": (400, 3000), "BITUMEN": (100, 1500), "FO & LSHS": (200, 1000),
    "LUBRICANTS & GREASES": (100, 700),
}


@lru_cache(maxsize=1)
def _ppac_consumption(http_id: int = 0) -> dict:
    http = _HTTP[0]
    wanted = {k: k for k in list(PPAC_PRODUCTS) + ["TOTAL"]}
    data = _ppac_monthly(http, "getConsumptionPetroleumProductsData", "43", "1", wanted)
    for k, pts in data.items():
        lo, hi = PPAC_RANGE.get(k, (8000, 30000))
        bad = [(iso(t), v) for t, v in pts if not lo <= v <= hi]
        if bad:
            raise RuntimeError(f"PPAC consumption {k}: values out of range {bad[:3]}")
        if len(pts) < 60:
            raise RuntimeError(f"PPAC consumption {k}: only {len(pts)} months")
        _fresh(pts[-1][0], 110, f"PPAC consumption {k}")
    return data


def _make_ppac_consumption(key: str, slug: str, name: str, short: str):
    @register(slug, section=SECTION, name=f"{name} Consumption (Monthly, PPAC)")
    def _builder(http):
        _HTTP[0] = http
        _ppac_consumption.cache_clear()
        pts = _ppac_consumption()[key]
        s = series("consumption", f"{name} consumption", "Thousand tonnes", pts)
        return record(
            slug, title=f"{name} Consumption (Monthly, PPAC)",
            description=(
                f"Monthly domestic consumption of {name} in India, in thousand tonnes, from PPAC (Ministry of Petroleum "
                f"and Natural Gas), compiled from oil-company sales plus private direct imports. {_PPAC_USE.get(key, '')} "
                "The latest month is provisional and is usually published within about a week of the following month."
            ),
            frequency="M",
            views=[view("Consumption ('000 tonnes)", s, vid="level", frequency="M"),
                   yoy_view(s, "YoY % change", frequency="M")],
            source="Petroleum Planning & Analysis Cell (PPAC), Ministry of Petroleum and Natural Gas",
            source_url="https://ppac.gov.in/consumption/products-wise",
            qna=[
                {"question": "What does this measure?",
                 "answer": f"Domestic consumption of {name} ({short} in PPAC tables), in thousand tonnes per month. PPAC defines consumption as the sum of oil-company sales in the domestic market and private direct imports (which are pro-rated until actual data arrive).^Total petroleum consumption is already on the dashboard as Petroleum Consumption; this page gives the product detail."},
                {"question": "How should I read it?",
                 "answer": "Use the YoY view to remove seasonality (kerosene, LPG, bitumen and petcoke have strong seasonal patterns). " + _PPAC_READ.get(key, "") + "^Months in 2020-21 are distorted by COVID lockdowns, so YoY growth for 2021-22 is mainly a base effect."},
                {"question": "Definitions, revisions and caveats",
                 "answer": "All figures are provisional and are revised: PPAC replaces each month as better oil-company and customs data arrive (the latest two to three months, especially the private direct import share, can change). The series is built from PPAC's yearly tables (April-March) back to April 2020; older years are only in PPAC's login-protected historical file and are not used.^PPAC groups some products (for example 'Others' and LDO) separately; they are not shown here."},
            ],
        )
    return _builder


_PPAC_USE = {
    "MS": "Petrol demand reflects private vehicle use and two-wheeler fleets and includes ethanol blended into petrol.",
    "HSD": "Diesel is the largest fuel (about two-fifths of consumption) and tracks freight, agriculture, construction and mining.",
    "LPG": "LPG is mostly household cooking fuel, so it is a steady consumer indicator, with growing commercial use.",
    "ATF": "Jet fuel is a direct read on air travel and is among the most cyclical products.",
    "NAPHTHA": "Naphtha is mostly a petrochemical and fertiliser feedstock, so it follows petrochemical plant runs.",
    "SKO": "Kerosene is a small, shrinking product (PDS and a little aviation/industrial use).",
    "PETROLEUM COKE": "Petcoke is used by cement, lime, refining and power and is a read on heavy industry.",
    "BITUMEN": "Bitumen is used to surface roads, so it is a read-through for road construction activity (strongly seasonal; weak in the monsoon).",
    "FO & LSHS": "Fuel oil and LSHS are industrial boiler and shipping fuels.",
    "LUBRICANTS & GREASES": "Lubricants follow vehicle parc and industrial machinery use.",
}
_PPAC_READ = {
    "MS": "Petrol growth is steady (high single digits) in normal years; a sharp slowdown usually follows price hikes or poor consumer sentiment.",
    "HSD": "Diesel growth tracks goods transport and capex; high growth with weak trade usually reflects pre-monsoon or festival stocking.",
    "ATF": "ATF growth above passenger traffic growth reflects more flights or longer routes.",
    "BITUMEN": "Because of heavy seasonality, only compare the same months across years.",
}

for _k, (_slug, _name, _short) in PPAC_PRODUCTS.items():
    _make_ppac_consumption(_k, _slug, _name, _short)


# ---- PPAC crude production and trade

@lru_cache(maxsize=1)
def _ppac_crude_prod(http_id: int = 0) -> list:
    http = _HTTP[0]
    data = _ppac_monthly(http, "getProduction", "3", "1", {"TOTAL CRUDE OIL": "crude"}, first_fy=2022)["crude"]
    bad = [(iso(t), v) for t, v in data if not 1.5 <= v <= 3.5]
    if bad:
        raise RuntimeError(f"PPAC crude production out of range {bad[:3]}")
    _fresh(data[-1][0], 110, "PPAC crude production")
    if len(data) < 40:
        raise RuntimeError("PPAC crude production history short")
    return data


@register("ppac-crude-oil-production", section=SECTION, name="Crude Oil Production (Monthly, PPAC)")
def crude_production(http):
    _HTTP[0] = http
    _ppac_crude_prod.cache_clear()
    s = series("crude", "Crude oil production", "Million tonnes", _ppac_crude_prod())
    return record(
        "ppac-crude-oil-production", title="Crude Oil Production (Monthly, PPAC)",
        description=("India's monthly domestic crude oil production (ONGC, Oil India and private/joint-venture fields) in "
                     "million tonnes from PPAC. Production has declined for over a decade and covers only about 12-15% "
                     "of refinery crude needs, so the series matters mostly as an energy-security and import-dependence input."),
        frequency="M",
        views=[view("Production (million tonnes)", s, vid="level", frequency="M"), yoy_view(s, "YoY % change", frequency="M")],
        source="Petroleum Planning & Analysis Cell (PPAC)", source_url="https://ppac.gov.in/production/indigenous-crude-oil",
        qna=[
            {"question": "What does this measure?",
             "answer": "Crude oil (including condensate) produced from Indian fields in the month, in million tonnes, as the 'Total crude oil' line of PPAC's production table (nomination blocks plus PSC/RSC regime).^A million tonnes of crude is about 7.3 million barrels."},
            {"question": "How should I read it?",
             "answer": "Month-to-month changes are small and mostly reflect field maintenance and decline rates; use the YoY view. A flat or slowly falling line is the norm. Natural gas production is on the dashboard separately."},
            {"question": "Definitions, revisions and caveats",
             "answer": "Figures are provisional and PPAC revises the latest months when companies report final numbers. The series starts in April 2022 (PPAC's production tables for earlier years use a different layout or sit behind its login). Month lengths matter: February is shorter, so seasonally unadjusted dips there are mechanical."},
        ],
    )


TRADE_WANTED = {"CRUDE OIL": "crude_imp", "PRODUCT IMPORT*": "prod_imp", "TOTAL IMPORT": "tot_imp",
                "PRODUCT EXPORT @": "prod_exp_hdr"}


@lru_cache(maxsize=1)
def _ppac_trade(http_id: int = 0) -> dict:
    http = _HTTP[0]
    out = {"crude_imp": {}, "prod_imp": {}, "prod_exp": {}}
    for fy in _ppac_fys():
        rows = _ppac_rows(http, "getImportExports", "14", "1", fy)
        y0 = int(fy[:4])
        found = set()
        for row in rows:
            title = _strip(row.get("title", "")).upper()
            key = {"CRUDE OIL": "crude_imp", "PRODUCT IMPORT*": "prod_imp"}.get(title)
            if title.startswith("TOTAL PRODUCT EXPORT"):
                key = "prod_exp"
            if key is None or key in found:
                continue
            found.add(key)
            for i, mname in enumerate(_PPAC_MONTHS):
                v = _strip(row.get(mname, ""))
                if v == "":
                    continue
                y = y0 if i < 9 else y0 + 1
                out[key][ts(y, (i + 3) % 12 + 1, 1)] = float(v.replace(",", ""))
        if found != {"crude_imp", "prod_imp", "prod_exp"}:
            raise RuntimeError(f"PPAC trade {fy}: rows found {sorted(found)}")
    res = {k: [[t, v] for t, v in sorted(d.items())] for k, d in out.items()}
    ranges = {"crude_imp": (12000, 30000), "prod_imp": (1500, 7000), "prod_exp": (2000, 7000)}
    for k, pts in res.items():
        lo, hi = ranges[k]
        bad = [(iso(t), v) for t, v in pts if not lo <= v <= hi]
        if bad:
            raise RuntimeError(f"PPAC trade {k}: out of range {bad[:3]}")
        _fresh(pts[-1][0], 110, f"PPAC trade {k}")
        if len(pts) < 60:
            raise RuntimeError(f"PPAC trade {k}: history short")
    return res


@register("ppac-crude-oil-imports", section=SECTION, name="Crude Oil Imports (Monthly, PPAC)")
def crude_imports(http):
    _HTTP[0] = http
    _ppac_trade.cache_clear()
    s = series("crude_imp", "Crude oil imports", "Thousand tonnes", _ppac_trade()["crude_imp"])
    return record(
        "ppac-crude-oil-imports", title="Crude Oil Imports (Monthly, PPAC)",
        description=("India's monthly crude oil imports in thousand tonnes from PPAC (oil-company and customs data). India "
                     "imports about 85-88% of its crude, so volumes track refinery runs and, with a lag, fuel demand; "
                     "the value of these imports is a major driver of the trade deficit and the rupee."),
        frequency="M",
        views=[view("Imports ('000 tonnes)", s, vid="level", frequency="M"), yoy_view(s, "YoY % change", frequency="M")],
        source="Petroleum Planning & Analysis Cell (PPAC)", source_url="https://ppac.gov.in/import-export",
        qna=[
            {"question": "What does this measure?",
             "answer": "Quantity of crude oil imported into India in the month, in thousand tonnes, as the 'Crude oil' line of PPAC's import table (includes RIL SEZ imports; LNG is not included). One thousand tonnes is roughly 7,300 barrels."},
            {"question": "How should I read it?",
             "answer": "Imports follow refinery throughput, so compare with product consumption and exports. Import volumes are lumpy because cargoes arrive in batches; use the YoY view and a 3-month average. Price effects are not in this series (it is quantity only)."},
            {"question": "Definitions, revisions and caveats",
             "answer": "Customs (DGCIS) data are prorated for the latest months and revised when actual data arrive. The series starts in April 2020. Quantities only; value in rupees and dollars is available on the PPAC page but is not used here."},
        ],
    )


@register("ppac-petroleum-product-trade", section=SECTION, name="Petroleum Product Imports and Exports (Monthly, PPAC)")
def product_trade(http):
    _HTTP[0] = http
    _ppac_trade.cache_clear()
    d = _ppac_trade()
    imp = series("prod_imp", "Petroleum product imports", "Thousand tonnes", d["prod_imp"])
    exp = series("prod_exp", "Petroleum product exports", "Thousand tonnes", d["prod_exp"])
    return record(
        "ppac-petroleum-product-trade", title="Petroleum Product Imports and Exports (Monthly, PPAC)",
        description=("Monthly imports and exports of refined petroleum products (LPG, petrol, naphtha, ATF, diesel, fuel "
                     "oil, bitumen, petcoke and others, excluding crude and LNG) in thousand tonnes from PPAC. India is a "
                     "net exporter of products: exports of diesel, petrol and ATF rise when domestic demand is soft or "
                     "export margins are strong, and imports are mostly LPG and petcoke."),
        frequency="M",
        views=[view("Imports and exports ('000 tonnes)", imp, exp, vid="level", frequency="M"),
               _yoy(imp, "Imports YoY %", "imp_yoy"), _yoy(exp, "Exports YoY %", "exp_yoy")],
        source="Petroleum Planning & Analysis Cell (PPAC)", source_url="https://ppac.gov.in/import-export",
        qna=[
            {"question": "What does this measure?",
             "answer": "Total refined-product imports ('Product import') and total product exports ('Total product export') in thousand tonnes per month, from PPAC's import-export table. Exports are based on refining companies' reports and exclude ATF sold to foreign-going airlines (bunker sales)."},
            {"question": "How should I read it?",
             "answer": "Compare exports with refinery throughput and domestic consumption: rising exports with falling consumption implies surplus product being sent abroad. Imports are dominated by LPG and petcoke; LPG imports follow household demand and Gulf prices."},
            {"question": "Definitions, revisions and caveats",
             "answer": "Figures are provisional and revised; DGCIS import data are prorated for the latest months. Series start April 2020. Crude oil imports are a separate indicator."},
        ],
    )


# ============================================================ backfills (run by hand)

def _polite_loop(items, fn, log):
    """Call fn(item) for each item (1 request/second/host handled inside)."""
    for i, it in enumerate(items):
        fn(it)
        if i % 100 == 0:
            log(f"{i}/{len(items)} {it}")


def backfill_dgr(start: date, end: date, cache_path: str, http=None) -> None:
    """Resumable: parsed days are cached in a JSON file; finally call finish_dgr(cache_path)."""
    http = http or Http()
    cache = json.loads(Path(cache_path).read_text()) if Path(cache_path).exists() else {}
    d = start
    n = 0
    while d <= end:
        key = d.isoformat()
        if key not in cache or (isinstance(cache[key], dict) and "error" in cache[key]):
            try:
                r = _dgr_fetch(http, d)
                cache[key] = None if r is None else {k: list(v) for k, v in r.items()}
            except Exception as e:  # noqa: BLE001
                cache[key] = {"error": str(e)[:200]}
            n += 1
            if n % 50 == 0:
                Path(cache_path).write_text(json.dumps(cache))
                print(key, n, flush=True)
        d += timedelta(days=1)
    Path(cache_path).write_text(json.dumps(cache))


def finish_dgr(cache_path: str) -> dict:
    cache = json.loads(Path(cache_path).read_text())
    ok = {date.fromisoformat(k): {kk: tuple(vv) for kk, vv in v.items()} for k, v in cache.items() if v and "error" not in v}
    errors = {k: v["error"] for k, v in cache.items() if v and "error" in v}
    daily, mtd = _dgr_collect(ok)
    for k in DGR_ROWS.values():
        save_seed(f"pf_dgr_{k}", daily[k])
        save_seed(f"pf_dgrm_{k}", _monthly_from_daily(daily[k], mtd[k]))
    return {"days": len(ok), "missing": sum(1 for v in cache.values() if v is None), "errors": errors}


def backfill_coal_stock(start: date, end: date, cache_path: str, http=None) -> None:
    http = http or Http()
    cache = json.loads(Path(cache_path).read_text()) if Path(cache_path).exists() else {}
    d, n = start, 0
    while d <= end:
        key = d.isoformat()
        if key not in cache or (isinstance(cache[key], dict) and "error" in cache[key]):
            try:
                b = _get_opt(http, COALSTOCK_URL.format(d=d))
                cache[key] = None if b is None else parse_coal_stock(b, d)
            except Exception as e:  # noqa: BLE001
                cache[key] = {"error": str(e)[:200]}
            n += 1
            if n % 50 == 0:
                Path(cache_path).write_text(json.dumps(cache))
                print(key, n, flush=True)
        d += timedelta(days=1)
    Path(cache_path).write_text(json.dumps(cache))


def finish_coal_stock(cache_path: str) -> dict:
    cache = json.loads(Path(cache_path).read_text())
    ok = {k: v for k, v in cache.items() if v and "error" not in v}
    for k in COAL_SERIES:
        save_seed(f"pf_cs_{k}", [[ts_date(date.fromisoformat(d)), v[k]] for d, v in sorted(ok.items()) if v.get(k) is not None])
    return {"days": len(ok), "missing": sum(1 for v in cache.values() if v is None),
            "errors": {k: v["error"] for k, v in cache.items() if v and "error" in v}}


def backfill_re(start: date, end: date, cache_path: str, http=None) -> None:
    http = http or Http()
    cache = json.loads(Path(cache_path).read_text()) if Path(cache_path).exists() else {}
    d, n = start, 0
    while d <= end:
        key = d.isoformat()
        if key not in cache or (isinstance(cache[key], dict) and "error" in cache[key]):
            try:
                b = _get_opt(http, RE_URL.format(d=d))
                cache[key] = None if b is None else {k: list(v) for k, v in parse_re(b, d).items()}
            except Exception as e:  # noqa: BLE001
                cache[key] = {"error": str(e)[:200]}
            n += 1
            if n % 50 == 0:
                Path(cache_path).write_text(json.dumps(cache))
                print(key, n, flush=True)
        d += timedelta(days=1)
    Path(cache_path).write_text(json.dumps(cache))


def finish_re(cache_path: str) -> dict:
    cache = json.loads(Path(cache_path).read_text())
    ok = {date.fromisoformat(k): v for k, v in cache.items() if v and "error" not in v}
    for k in RE_COLS:
        daily = [[ts_date(d), round(v[k][0], 3)] for d, v in sorted(ok.items())]
        mtd = {ts_date(d): v[k][1] for d, v in ok.items() if d.day == _month_days(d.year, d.month)}
        save_seed(f"pf_re_{k}", daily)
        save_seed(f"pf_rem_{k}", _monthly_from_daily(daily, mtd))
    return {"days": len(ok), "missing": sum(1 for v in cache.values() if v is None),
            "errors": {k: v["error"] for k, v in cache.items() if v and "error" in v}}


def backfill_psp(cache_path: str, http=None) -> dict:
    http = http or Http()
    listings = {"E": _psp_listing(http, "10010"), "P": _psp_listing(http, "10011")}
    cache = json.loads(Path(cache_path).read_text()) if Path(cache_path).exists() else {}
    months = sorted(set(listings["E"]) & set(listings["P"]))
    for ym in months:
        for kind in ("E", "P"):
            key = f"{kind}{ym}"
            if key in cache and not isinstance(cache[key], dict):
                continue
            try:
                cache[key] = list(parse_psp(_get(http, listings[kind][ym]).content, ym, kind))
            except Exception as e:  # noqa: BLE001
                cache[key] = {"error": str(e)[:200]}
        Path(cache_path).write_text(json.dumps(cache))
    return cache


def finish_psp(cache_path: str) -> dict:
    cache = json.loads(Path(cache_path).read_text())
    out = {k: [] for k in PSP_KEYS}
    errors = {}
    for key, v in sorted(cache.items(), key=lambda kv: kv[0][1:]):
        if isinstance(v, dict):
            errors[key] = v["error"]
            continue
        ym = key[1:]
        t = ts(int(ym[:4]), int(ym[5:]), 1)
        a, b = ("e_req", "e_sup") if key[0] == "E" else ("p_dem", "p_met")
        out[a].append([t, v[0]])
        out[b].append([t, v[1]])
    for k in PSP_KEYS:
        save_seed(f"pf_psp_{k}", out[k])
    return {"months": len(out["e_req"]), "errors": errors}


def backfill_coal_msg(cache_path: str, http=None) -> dict:
    http = http or Http()
    links = _coal_msg_links(http)
    cache = json.loads(Path(cache_path).read_text()) if Path(cache_path).exists() else {}
    for url in links[::-1]:
        if url in cache and "error" not in cache[url]:
            continue
        try:
            r = parse_coal_msg(_get(http, url).content)
            cache[url] = {"month": list(r["month"]), "cur": r["cur"], "prev": r["prev"]}
        except Exception as e:  # noqa: BLE001
            cache[url] = {"error": str(e)[:200]}
        Path(cache_path).write_text(json.dumps(cache))
    return cache


def finish_coal_msg(cache_path: str) -> dict:
    cache = json.loads(Path(cache_path).read_text())
    store: dict = {}
    errors = {}
    items = [(tuple(v["month"]), u, v) for u, v in cache.items() if "error" not in v]
    errors = {u: v["error"] for u, v in cache.items() if "error" in v}
    for ym, u, v in sorted(items):
        _coal_add({"month": ym, "cur": v["cur"], "prev": v["prev"]}, store)
    out = _coal_series_from(store)
    for k, pts in out.items():
        save_seed(f"pf_coal_{k}", pts)
    return {"files": len(items), "errors": errors}
