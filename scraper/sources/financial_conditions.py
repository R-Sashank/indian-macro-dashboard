"""Financial-conditions indicators: India VIX and effective exchange rates.

* India VIX (daily close): NSE's public daily index-closing archive
  (nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv), one small CSV per
  trading day. History is a seed file (scraper/sources/seed/india-vix.json, built once from
  those files) that each run extends with the days published since; the extension is saved back
  with save_seed so the number of fetches per run stays small if the seed directory is committed.
  The files carry the India VIX row only from mid-2014/2015; earlier history was not reachable
  (the niftyindices.com historical-data endpoint returns an error page).
* NEER / REER: BIS effective exchange rate indices for India (broad basket, 64 economies,
  2020=100) via the BIS SDMX API: daily nominal, monthly nominal and monthly real.
  These are BIS series, NOT the RBI 6-/40-currency indices.
"""

from __future__ import annotations

import csv
import io
import re
import time
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup

from .base import (
    load_seed, merge_pts, record, register, save_seed, series, ts_date, ts_iso, view, month_ts,
)

SECTION = "financial-conditions"

_NSE = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{d}.csv"
_BIS = "https://stats.bis.org/api/v1/data/WS_EER/{key}?format=csv"

PROBE_URLS = [
    _NSE.format(d="29102026"),
    _BIS.format(key="D.N.B.IN"),
    _BIS.format(key="M.N.B.IN"),
    _BIS.format(key="M.R.B.IN"),
    "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx",  # lists recent releases incl. T-bill cut-off
    "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=63876",  # a T-bill cut-off release
    "https://www.ccilindia.com/money-market-rates-and-volumes-most-liquid-tenor-",
]

_MAX_LIVE_DAYS = 100  # weekdays of catch-up fetched in one run (~2 minutes at 1 request/second)


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ------------------------------------------------------------------ India VIX

def _vix_for_day(http, d: date):
    """Closing India VIX for one trading day, None if no file (weekend/holiday/not yet published)."""
    try:
        r = http.s.get(_NSE.format(d=d.strftime("%d%m%Y")), timeout=30)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"fetch failed for {d}: {e}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    rows = list(csv.reader(io.StringIO(r.text)))
    header = [c.strip() for c in rows[0]]
    assert "Index Name" in header and "Index Date" in header and "Closing Index Value" in header, \
        f"NSE index-close layout changed: {header}"
    i_name, i_date, i_close = header.index("Index Name"), header.index("Index Date"), header.index("Closing Index Value")
    for row in rows[1:]:
        if len(row) > i_close and row[i_name].strip().upper() == "INDIA VIX":
            raw = row[i_date].strip().replace("/", "-")
            a, b, y = (int(x) for x in raw.split("-"))
            # the file name's date is authoritative; the in-file date must be that day (a few
            # files, e.g. 06-Apr-2023, have day and month swapped, so accept either order)
            assert y == d.year and {a, b} == {d.day, d.month}, f"file for {d} carries date {raw}"
            v = float(row[i_close])
            assert 5 < v < 120, f"India VIX {v} out of range on {d}"
            return v
    return None  # file without the VIX row (pre-2014 files)


@register("india-vix", section=SECTION, name="India VIX")
def india_vix(http):
    seed = load_seed("india-vix")
    assert len(seed) > 1000, f"India VIX seed missing or short ({len(seed)} points)"
    last = datetime.fromtimestamp(seed[-1][0], timezone.utc).date()
    today = _today()
    days = []
    d = last + timedelta(days=1)
    while d <= today:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    if len(days) > _MAX_LIVE_DAYS:
        raise RuntimeError(f"India VIX seed ends {last}: {len(days)} weekdays to catch up; rebuild the seed")
    new = []
    for i, d in enumerate(days):
        if i:
            time.sleep(1.0)
        v = _vix_for_day(http, d)
        if v is not None:
            new.append([ts_date(d), v])
    pts = merge_pts(seed, new)
    if new:
        save_seed("india-vix", pts)
    latest = datetime.fromtimestamp(pts[-1][0], timezone.utc).date()
    if (today - latest).days > 10:
        raise RuntimeError(f"India VIX latest date {latest} is stale")
    vals = [v for _, v in pts]
    if not (5 < min(vals) and max(vals) < 120):
        raise RuntimeError(f"India VIX out of plausible range: {min(vals)}..{max(vals)}")
    s = series("india_vix", "India VIX (daily close)", "Per cent", pts)
    return record(
        "india-vix", title="India VIX",
        description=(
            "NSE's India VIX: the market's expectation of annualised volatility of the NIFTY 50 over the "
            "next 30 calendar days, computed from NIFTY option prices (per cent). Daily closing value. "
            "It is a fast-moving gauge of risk appetite and financial stress in Indian equities: spikes "
            "coincide with sell-offs, event risk (elections, global shocks) and tightening of financial "
            "conditions, so it is useful as a high-frequency nowcasting input for sentiment and "
            "capital-flow pressure."),
        frequency="D", views=[view("Daily close", s)],
        source="NSE Indices (daily index closing files)",
        source_url="https://nsearchives.nseindia.com/content/indices/",
        notes="History starts in 2014 (earlier NSE closing files carry no India VIX row).",
        qna=[
            {"question": "What is India VIX?",
             "answer": "India VIX is a volatility index published by NSE. It is derived from the order book of NIFTY 50 options and represents the volatility investors expect over the coming 30 days, expressed as an annualised percentage.^A reading of 15 implies the market expects NIFTY to move roughly 15 / sqrt(12), i.e. about 4.3 per cent, in either direction over a month, with about 68 per cent confidence."},
            {"question": "How should I read it?",
             "answer": "Higher means more fear or uncertainty. Levels below about 13 are generally calm, while sustained readings above 20-25 mark stressed markets. It tends to rise sharply when the NIFTY falls and mean-reverts over weeks.^Changes matter as much as levels; a jump of several points in a day is a stress signal."},
            {"question": "Caveats?",
             "answer": "This is the daily close taken from NSE's index-closing archive file for each trading day. Days without a file (weekends, exchange holidays) are absent.^The series here starts in 2014 because earlier archive files do not list the index. The VIX is a market-implied measure and not a forecast of realised volatility."},
        ],
    )


# ------------------------------------------------- BIS effective exchange rates

def _bis(http, key: str, expect_title: str):
    text = http.text(_BIS.format(key=key), timeout=120)
    rdr = csv.DictReader(io.StringIO(text))
    need = {"FREQ", "EER_TYPE", "EER_BASKET", "REF_AREA", "TITLE_TS", "TIME_PERIOD", "OBS_VALUE"}
    assert rdr.fieldnames and need <= set(rdr.fieldnames), f"BIS layout changed: {rdr.fieldnames}"
    pts = []
    for x in rdr:
        assert x["REF_AREA"] == "IN", f"unexpected area {x['REF_AREA']}"
        assert expect_title in x["TITLE_TS"], f"unexpected series {x['TITLE_TS']}"
        if x["OBS_VALUE"] in ("", "NaN"):
            continue
        tp = x["TIME_PERIOD"]
        t = ts_iso(tp) if len(tp) == 10 else month_ts(int(tp[:4]), int(tp[5:7]))
        pts.append([t, float(x["OBS_VALUE"])])
    assert len(pts) > 300, f"BIS {key}: only {len(pts)} points"
    vals = [v for _, v in pts]
    assert 30 < min(vals) and max(vals) < 400, f"BIS {key}: implausible values {min(vals)}..{max(vals)}"
    return sorted(pts)


def _check_recent(pts, max_days, what):
    latest = datetime.fromtimestamp(pts[-1][0], timezone.utc).date()
    if (_today() - latest).days > max_days:
        raise RuntimeError(f"{what}: latest {latest} is stale")


_EER_QNA = [
    {"question": "What are the NEER and REER?",
     "answer": "The nominal effective exchange rate (NEER) is a trade-weighted average of the rupee's bilateral exchange rates against a basket of partner currencies. The real effective exchange rate (REER) adjusts the NEER for relative consumer-price inflation between India and its partners.^An increase means the rupee has appreciated against the basket (a loss of price competitiveness); a decrease means depreciation."},
    {"question": "How should I read them?",
     "answer": "The REER is the usual competitiveness gauge: values well above 100 indicate an overvalued rupee relative to the base year, and sustained depreciation of the REER tends to support exports and import substitution, while appreciation lowers imported inflation. The NEER is available daily and reacts quickly to the dollar and other major currencies, so it is useful for tracking real-time external conditions.^These indices complement USD/INR, which looks at a single bilateral rate."},
    {"question": "Definitions and caveats",
     "answer": "These are the Bank for International Settlements (BIS) effective exchange rate indices for India: broad basket of 64 economies, base 2020 = 100. They are NOT the RBI's 6-currency or 40-currency indices (base 2015-16 = 100), so levels differ from RBI's published figures although direction is usually similar.^BIS revises the latest months as price and trade-weight data arrive; the daily NEER is nominal only, because consumer-price deflators are available monthly. Daily dates are the BIS reference date; monthly values are stamped on the first of the month."},
]


@register("neer-bis", section=SECTION, name="Nominal Effective Exchange Rate (NEER, BIS)")
def neer_bis(http):
    daily = _bis(http, "D.N.B.IN", "Nominal - Broad")
    time.sleep(1)
    monthly = _bis(http, "M.N.B.IN", "Nominal - Broad")
    _check_recent(daily, 20, "BIS daily NEER")
    _check_recent(monthly, 75, "BIS monthly NEER")
    sd = series("neer_d", "NEER, broad (64 economies), daily, 2020=100", "Index (2020=100)", daily)
    sm = series("neer_m", "NEER, broad (64 economies), monthly average, 2020=100", "Index (2020=100)", monthly)
    return record(
        "neer-bis", title="Nominal Effective Exchange Rate (NEER, BIS broad)",
        description=(
            "Trade-weighted nominal value of the Indian rupee against 64 trading partners' currencies, BIS "
            "broad effective exchange rate index, 2020 = 100. A rise is rupee appreciation. Daily from 1996 and "
            "monthly from 1994. It summarises the external price pressure on exports, imports and inflation "
            "far better than USD/INR alone, and is a timely nowcasting input for trade competitiveness and "
            "imported inflation."),
        frequency="D",
        views=[view("Daily", sd, frequency="D"), view("Monthly (longer history)", sm, frequency="M")],
        source="Bank for International Settlements (effective exchange rate indices)",
        source_url="https://data.bis.org/topics/EER",
        qna=_EER_QNA,
    )


@register("reer-bis", section=SECTION, name="Real Effective Exchange Rate (REER, BIS)")
def reer_bis(http):
    monthly = _bis(http, "M.R.B.IN", "Real - Broad")
    _check_recent(monthly, 75, "BIS REER")
    s = series("reer_m", "REER, broad (64 economies), CPI-based, 2020=100", "Index (2020=100)", monthly)
    return record(
        "reer-bis", title="Real Effective Exchange Rate (REER, BIS broad)",
        description=(
            "CPI-deflated, trade-weighted value of the rupee against 64 trading partners, BIS broad real "
            "effective exchange rate index, 2020 = 100, monthly from 1994. A rise means the rupee has "
            "appreciated in real terms (India's goods become relatively more expensive). It is the standard "
            "gauge of external price competitiveness and a medium-term driver of exports, the current account "
            "and RBI intervention."),
        frequency="M", views=[view("Monthly", s)],
        source="Bank for International Settlements (effective exchange rate indices)",
        source_url="https://data.bis.org/topics/EER",
        qna=_EER_QNA,
    )


# ------------------------------------------------ accumulating series (seed grows each run)
#
# RBI/CCIL pages show only the latest observation(s), so these builders keep history in a seed
# file: read seed, add what the page shows (dated by the observation's own date), merge, save.

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
_RBI_PR = "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx"
_CCIL_MM = "https://www.ccilindia.com/money-market-rates-and-volumes-most-liquid-tenor-"
_TB = [("91d", "91-day"), ("182d", "182-day"), ("364d", "364-day")]


def _iso_first(pts):
    return datetime.fromtimestamp(pts[0][0], timezone.utc).strftime("%Y-%m-%d")


def _parse_tbill_release(html: str, prid: str):
    """-> (ts, [y91, y182, y364]) or None when the auction yielded no cut-off ('NA')."""
    text = " ".join(BeautifulSoup(html, "lxml").get_text(" ", strip=True).split())
    m = re.search(r"Date\s*:\s*([A-Z][a-z]{2})[a-z]*\s+(\d{1,2}),\s*(\d{4})", text)
    assert m and m.group(1) in _MONTHS, f"T-bill release {prid}: date not found"
    day = date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
    assert "91-Day" in text and "182-Day" in text and "364-Day" in text and "Cut-off" in text, \
        f"T-bill release {prid}: layout changed"
    i = text.find("Implicit Yield at Cut-Off Price")
    assert i > 0, f"T-bill release {prid}: cut-off row not found"
    seg = text[i:i + 300]
    ys = re.findall(r"YTM:\s*([0-9.]+)\s*%", seg)
    if not ys:
        assert "NA" in seg, f"T-bill release {prid}: no yields and no NA"
        return None
    assert len(ys) == 3, f"T-bill release {prid}: expected 3 yields, got {ys}"
    ys = [float(y) for y in ys]
    assert all(1 < y < 15 for y in ys), f"T-bill yields implausible: {ys}"
    return ts_date(day), ys


@register("tbill-cutoff-yields", section=SECTION, name="T-Bill Cut-off Yields (91/182/364-day)")
def tbill_cutoff(http):
    seeds = {k: load_seed(f"tbill-{k}") for k, _ in _TB}
    assert all(len(v) >= 2 for v in seeds.values()), "T-bill seed missing"
    listing = BeautifulSoup(http.text(_RBI_PR), "lxml")
    prids = []
    for a in listing.find_all("a", href=True):
        if "T-Bill Auction Result: Cut-off" in " ".join(a.get_text(" ", strip=True).split()):
            mm = re.search(r"prid=(\d+)", a["href"])
            if mm and mm.group(1) not in prids:
                prids.append(mm.group(1))
    new = {k: [] for k, _ in _TB}
    for j, prid in enumerate(prids):
        if j:
            time.sleep(1.0)
        got = _parse_tbill_release(http.text(f"{_RBI_PR}?prid={prid}"), prid)
        if got:
            t, ys = got
            for (k, _), y in zip(_TB, ys):
                new[k].append([t, y])
    merged = {}
    for k, _ in _TB:
        merged[k] = merge_pts(seeds[k], new[k])
        if new[k]:
            save_seed(f"tbill-{k}", merged[k])
        _check_recent(merged[k], 35, f"T-bill {k}")
    first = _iso_first(merged["91d"])
    ss = [series(f"tb{k}", f"{lbl} T-bill cut-off yield", "Per cent", merged[k]) for k, lbl in _TB]
    return record(
        "tbill-cutoff-yields", title="T-Bill Cut-off Yields (91/182/364-day)",
        description=(
            "Cut-off (implicit) yield at the RBI's weekly Treasury bill auctions for the 91-, 182- and 364-day "
            "bills, per cent, dated by auction day. These are the clearest market read on short-term "
            "risk-free rates and liquidity expectations, moving ahead of policy changes and money-market "
            "stress. HISTORY IS SHORT: this series is recorded from RBI press releases as they appear, "
            f"starting {first}, and grows with every weekly auction; it is not a full historical series."),
        frequency="W", views=[view("Cut-off yield", *ss)],
        source="Reserve Bank of India (T-bill auction result press releases)",
        source_url=_RBI_PR,
        notes=f"Accumulating series: history starts {first} and grows with each run. Auctions where RBI "
              "reports no cut-off (all bids rejected, shown as NA) leave a gap.",
        qna=[
            {"question": "What is the T-bill cut-off yield?",
             "answer": "At each weekly auction the RBI accepts bids from the lowest yield upward until the notified amount is filled; the highest accepted yield is the cut-off. The press release gives the cut-off price and the implied yield (YTM) for the 91-, 182- and 364-day bills.^It is a primary-market rate for risk-free government borrowing at three short maturities."},
            {"question": "How should I read it?",
             "answer": "Yields track the expected policy repo rate and system liquidity: a surplus pulls them below the repo rate, a deficit pushes them up. A wider gap between 364-day and 91-day yields signals expectations of higher rates ahead.^Compare with the repo rate and the net LAF series elsewhere on the dashboard."},
            {"question": "Caveats?",
             "answer": f"The dashboard records each auction from the RBI press release as it is published, so history starts on {first} and the series is short. Do not read it as a long-run record.^Occasionally the RBI rejects all bids or the auction is not held; the release then shows NA and no point is recorded for that week."},
        ],
    )


@register("call-money-rate", section=SECTION, name="Weighted Average Call Money Rate")
def call_money(http):
    r_seed, v_seed = load_seed("call-rate"), load_seed("call-volume")
    assert len(r_seed) >= 2 and len(v_seed) >= 2, "call money seed missing"
    sp = BeautifulSoup(http.text(_CCIL_MM, timeout=90), "lxml")
    tb = next((t for t in sp.find_all("table") if "Wtd Avg" in t.get_text()), None)
    assert tb is not None, "CCIL money-market table not found"
    rows = tb.find_all("tr")
    head = [" ".join(c.get_text(" ", strip=True).split()) for c in rows[0].find_all(["th", "td"])]
    need = ["Date", "Type", "Wtd Avg", "Volume"]
    assert all(any(h.startswith(n) for h in head) for n in need), f"CCIL layout changed: {head}"
    ix = {n: next(i for i, h in enumerate(head) if h.startswith(n)) for n in need}
    rn, vn = [], []
    for tr in rows[1:]:
        c = [" ".join(x.get_text(" ", strip=True).split()) for x in tr.find_all("td")]
        if len(c) <= max(ix.values()) or c[ix["Type"]] != "Call":
            continue
        dtxt = c[ix["Date"]]
        if re.match(r"\d{4}-\d\d-\d\d", dtxt):
            t = ts_iso(dtxt)
        else:
            assert re.fullmatch(r"\d\d-\d\d-\d{4}", dtxt), f"unrecognised CCIL date {dtxt!r}"
            t = ts_iso(f"{dtxt[6:10]}-{dtxt[3:5]}-{dtxt[0:2]}")
        rate, vol = float(c[ix["Wtd Avg"]].replace(",", "")), float(c[ix["Volume"]].replace(",", ""))
        assert 1 < rate < 15 and 0 < vol < 1e6, f"call money values implausible: {rate}, {vol}"
        rn.append([t, rate])
        vn.append([t, vol])
    assert rn, "no Call rows found on CCIL page"
    rate, vol = merge_pts(r_seed, rn), merge_pts(v_seed, vn)
    save_seed("call-rate", rate)
    save_seed("call-volume", vol)
    _check_recent(rate, 10, "call money rate")
    first = _iso_first(rate)
    return record(
        "call-money-rate", title="Weighted Average Call Money Rate",
        description=(
            "Volume-weighted average rate on overnight call money (inter-bank, uncollateralised) trades, per "
            "cent, daily, with traded volume (one leg, Rs. crore). It sits near the repo rate when liquidity "
            "is balanced, rises above it in deficit and falls toward the SDF rate in surplus, so it is a "
            "timely read on money-market tightness. HISTORY IS SHORT: recorded from CCIL's published "
            f"money-market rates (which match RBI's Money Market Operations release), starting {first}; the "
            "series grows with each run and is not a full historical series."),
        frequency="D",
        views=[view("Weighted average rate", series("call_rate", "Call money weighted average rate", "Per cent", rate)),
               view("Volume", series("call_vol", "Call money volume (one leg)", "Rs. crore", vol), vid="vol")],
        source="CCIL (Clearing Corporation of India), money-market rates; cross-checked to RBI Money Market Operations",
        source_url=_CCIL_MM,
        notes=f"Accumulating series: history starts {first} and grows with each run.",
        qna=[
            {"question": "What is the call money rate?",
             "answer": "The call market is the overnight, uncollateralised inter-bank market. The weighted average rate is the volume-weighted mean of the day's trades; the volume is the one-leg amount traded in Rs. crore.^Most overnight money-market activity is now collateralised (triparty repo), so call volumes are small relative to total overnight volume."},
            {"question": "How should I read it?",
             "answer": "Compare with the repo rate and the SDF/MSF corridor. A call rate above the repo rate points to tight liquidity; one near the SDF rate points to surplus. Daily spikes around reserve-requirement and tax-payment dates are common.^Pair with the net LAF series on this dashboard."},
            {"question": "Caveats?",
             "answer": f"History begins on {first} because the source pages show only the latest days; the dashboard adds each new day as it is published, so the series is short for now.^CCIL reports four decimals; the RBI release rounds to two (for example 5.5772 versus 5.58)."},
        ],
    )
