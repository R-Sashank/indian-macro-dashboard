"""Industry & core sectors: IIP use-based indices (MoSPI eSankhyiki API) and the Index of Core
Industries by sector (PIB / Office of the Economic Adviser monthly press releases).

Base years
----------
* IIP: MoSPI rebased to 2022-23=100 (series starts April 2023); the 2011-12=100 series ends March 2026.
  The two bases are published as SEPARATE records and are never spliced.
* Core industries: DPIIT/OEA rebased to 2022-23=100 from the June 2026 release (nine industries: iron ore
  added; steel on gross production; raw coal only). The 2011-12=100 series (eight industries) was
  discontinued after May 2026. Old and new bases are separate views, never spliced.
"""

from __future__ import annotations

import re
import ssl
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from .base import EPOCH, load_seed, month_ts, record, register, series, supersedes, view

SECTION = "industry-core"

IIP_API = "https://api.mospi.gov.in/api/iip/getIIPMonthly"
PROBE_URLS = [
    IIP_API + "?base_year=2022-23&year=2023,2024,2025,2026&category_code=4,5,6,7,8,9,10&limit=200&page=1",
    IIP_API + "?base_year=2011-12&year=2012,2013,2014,2015,2016,2017,2018,2019,2020,2021,2022,2023,2024,2025,2026&category_code=4,5,6,7,8,9,10&limit=200&page=1",
    "https://www.pib.gov.in/PressReleasePage.aspx?PRID=2326038&reg=3&lang=1",   # ICI Sep 2026 (new base)
    "https://www.pib.gov.in/PressReleasePage.aspx?PRID=2286615&reg=3&lang=1",   # ICI Jun 2026
]

MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"])}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _months_old(t: int) -> float:
    d = EPOCH.fromtimestamp(t, tz=timezone.utc)
    n = _now()
    return (n.year - d.year) * 12 + (n.month - d.month)


# ----------------------------------------------------------------------------- MoSPI IIP API

def _mount(http):
    """MoSPI's API host needs legacy TLS renegotiation; the framework (sources/base.py) handles that for this one host."""
    return http


GROUPS = [  # (MoSPI category name, slug id, display name)
    ("General", "general", "Overall IIP"),
    ("Primary Goods", "primary", "Primary goods"),
    ("Capital Goods", "capital", "Capital goods"),
    ("Intermediate Goods", "intermediate", "Intermediate goods"),
    ("Infrastructure/ Construction Goods", "infra", "Infrastructure/construction goods"),
    ("Consumer Durables", "durables", "Consumer durables"),
    ("Consumer Non-durables", "nondurables", "Consumer non-durables"),
]
_NORM = lambda s: re.sub(r"[^a-z]", "", s.lower())  # noqa: E731


_IIP_CACHE: dict = {}


def _iip(http, base_year: str, years: list) -> dict:
    """{group id: {'idx': [[ts, v]], 'g': [[ts, growth]]}} for General + the six use-based groups."""
    key = (base_year, tuple(years))
    if key in _IIP_CACHE:
        return _IIP_CACHE[key]
    _mount(http)
    rows, page, total_pages = [], 1, 1
    while page <= total_pages:
        j = http.json(IIP_API, params={
            "base_year": base_year, "year": ",".join(str(y) for y in years),
            "category_code": "4,5,6,7,8,9,10", "limit": 200, "page": page})
        if not j.get("data") or "meta_data" not in j:
            raise RuntimeError(f"IIP API returned no data (base {base_year}, page {page}): {str(j)[:200]}")
        rows += j["data"]
        total_pages = int(j["meta_data"]["totalPages"])
        page += 1
        time.sleep(1)
    if total_pages > 10:
        raise RuntimeError("unexpected IIP pagination size")
    by_name = {_NORM(g[0]): g[1] for g in GROUPS}
    out = {g[1]: {"idx": {}, "g": {}} for g in GROUPS}
    for r in rows:
        if r.get("base_year") != base_year:
            raise RuntimeError(f"IIP row with unexpected base year {r.get('base_year')!r}")
        if r.get("sub_category"):
            continue
        gid = by_name.get(_NORM(r["category"]))
        if gid is None or r["type"] not in ("General", "Use-based category"):
            continue
        m = MONTHS.get(r["month"])
        if m is None:
            raise RuntimeError(f"unexpected month {r['month']!r}")
        t = month_ts(int(r["year"]), m)
        out[gid]["idx"][t] = float(r["index"])
        if r.get("growth_rate") not in (None, "", "NA"):
            out[gid]["g"][t] = float(r["growth_rate"])
    res = {}
    for gid, d in out.items():
        if not d["idx"]:
            raise RuntimeError(f"IIP group {gid} missing from API response")
        res[gid] = {"idx": [[t, d["idx"][t]] for t in sorted(d["idx"])],
                    "g": [[t, d["g"][t]] for t in sorted(d["g"])]}
    _IIP_CACHE[key] = res
    return res


def _check_iip(res: dict, lo: float, hi: float, first_ts: int, n_min: int):
    for gid, d in res.items():
        pts = d["idx"]
        if len(pts) < n_min:
            raise RuntimeError(f"IIP {gid}: only {len(pts)} months")
        if pts[0][0] != first_ts:
            raise RuntimeError(f"IIP {gid}: starts {pts[0][0]}, expected {first_ts}")
        for t, v in pts:
            if not lo <= v <= hi:
                raise RuntimeError(f"IIP {gid}: implausible index {v}")
        for (t0, _), (t1, _) in zip(pts, pts[1:]):
            d0, d1 = EPOCH.fromtimestamp(t0, tz=timezone.utc), EPOCH.fromtimestamp(t1, tz=timezone.utc)
            if (d1.year - d0.year) * 12 + d1.month - d0.month != 1:
                raise RuntimeError(f"IIP {gid}: gap in months near {d0:%Y-%m}")
        for t, g in d["g"]:
            if not -99 <= g <= 600:  # includes the Apr 2020 collapse and Apr 2021 rebound
                raise RuntimeError(f"IIP {gid}: implausible growth {g}")


_IIP_QNA = [
    {"question": "What does the use-based IIP measure?",
     "answer": "The Index of Industrial Production (IIP), compiled by MoSPI's National Statistics Office, tracks the volume of output of a fixed basket of industrial products. The use-based classification regroups the same items by how the goods are used: primary goods (basic inputs such as minerals, electricity-generation fuels, raw materials), capital goods (machinery and equipment used to produce other goods), intermediate goods (inputs used in further processing), infrastructure/construction goods (cement, steel products, paints, cables and similar), consumer durables and consumer non-durables.^Capital goods output is a read on investment demand, infrastructure/construction goods on building and public capex, and the two consumer groups on household demand, so the split is more informative for nowcasting than the headline."},
    {"question": "How should I read the index and the growth rate?",
     "answer": "Index levels are relative to the base year average (=100), so 130 means 30 per cent above the base-year average. Levels are strongly seasonal (March is typically the peak, and the festive months move with Diwali timing), so use year-on-year growth, which compares the same month, for momentum. The growth rate here is the one published by MoSPI; the year-ago index for the early months of the new series is not published, so the published growth is shown instead of recomputing it.^Base effects matter: the quick estimate for a month is revised in the next two releases, so compare latest growth with caution."},
]


def _iip_record(slug, base_year, res, first_label, extra_note, qna3, description):
    idx_series = [series(gid, name, f"Index ({base_year}=100)", res[gid]["idx"]) for _, gid, name in GROUPS]
    g_series = [series(gid, name, "Per cent", res[gid]["g"]) for _, gid, name in GROUPS]
    views = [view(f"Index ({base_year}=100)", *idx_series, vid="index"),
             view("YoY growth (as published)", *g_series, vid="yoy")]
    # per-group views so that each group can be read on its own
    qna = [_IIP_QNA[0], _IIP_QNA[1], {"question": "Definitions, revisions and caveats", "answer": qna3}]
    return record(
        slug, title=f"IIP by use-based classification (base {base_year})", description=description,
        frequency="M", views=views, source="MoSPI (eSankhyiki IIP API)",
        source_url="https://esankhyiki.mospi.gov.in/macroindicators?product=iip", qna=qna,
        notes=extra_note)


@register("iip-use-based-2022-23", section=SECTION, name="IIP use-based groups (base 2022-23)")
def iip_use_based_new(http):
    res = _iip(http, "2022-23", [2023, 2024, 2025, 2026])
    _check_iip(res, 60, 220, month_ts(2023, 4), 30)
    last = res["general"]["idx"][-1][0]
    if _months_old(last) > 4:
        raise RuntimeError(f"IIP 2022-23 latest month is stale: {last}")
    desc = ("Monthly Index of Industrial Production on the current base 2022-23=100 (series starts April 2023), "
            "split by use-based group: primary, capital, intermediate and infrastructure/construction goods, "
            "consumer durables and non-durables, plus the overall index. Index levels are shown together with the "
            "year-on-year growth rates published by MoSPI. Capital goods and infrastructure/construction goods lead "
            "investment and capex, the consumer groups proxy household demand, which makes the split a core input "
            "for nowcasting GDP and GVA in industry. Quick estimates are released about 28 days after the month end "
            "and revised once, in the following month's release.")
    note = "Base 2022-23=100 only; not comparable in level with the 2011-12=100 record. The 2011-12 series is shown separately."
    q3 = ("MoSPI rebased the IIP to 2022-23=100 with the new series (starting April 2023). Index levels of the new "
          "and old (2011-12=100) series cannot be compared or chained directly; the old series ends in March 2026 "
          "and is shown in its own record. Each month is a quick estimate (QE) at first release and "
          "gets a final revision in the following month's release.^In the 2022-23 series the use-based weights (per cent of the "
          "index) are primary 31.1, capital 8.1, intermediate 22.4, infrastructure/construction 10.9, consumer durables 11.3 and "
          "consumer non-durables 16.1 (as printed in MoSPI's press release). Data are fetched from MoSPI's public eSankhyiki API.")
    return _iip_record("iip-use-based-2022-23", "2022-23", res, "Apr 2023", note, q3, desc)


@register("iip-use-based-2011-12", section=SECTION, name="IIP use-based groups (base 2011-12, discontinued)")
def iip_use_based_old(http):
    res = _iip(http, "2011-12", list(range(2012, 2027)))
    _check_iip(res, 1, 400, month_ts(2012, 4), 150)  # low end = Apr 2020 lockdown
    last = res["general"]["idx"][-1][0]
    if last != month_ts(2026, 3):
        raise RuntimeError(f"2011-12 IIP series expected to end March 2026, ends {last}")
    desc = ("Historical monthly Index of Industrial Production on the discontinued base 2011-12=100 (April 2012 to "
            "March 2026), split by use-based group, with MoSPI's published year-on-year growth. It gives a "
            "14-year history of capital goods, infrastructure/construction goods, intermediate, primary and consumer "
            "goods output for cycle analysis. It stops where MoSPI moved to the 2022-23 base; do not append the "
            "new series to it, the levels are not comparable.")
    q3 = ("This is the old series (base 2011-12=100); MoSPI replaced it with the 2022-23=100 series from the April "
          "2026 data release, and it will not be updated. Levels of the two series are not comparable (MoSPI uses "
          "a linking factor to join them; none is applied here). Growth rates for the old series are those "
          "published at the time and may include later revisions.^Group weights differ between the old and new baskets, "
          "so shares and growth contributions are not comparable across the two records.")
    return _iip_record("iip-use-based-2011-12", "2011-12", res, "Apr 2012",
                       "Discontinued base (2011-12=100); ends March 2026.", q3, desc)


# ----------------------------------------------------------------------------- Index of Core Industries
# Source: PIB press releases of the Office of the Economic Adviser (DPIIT), monthly, ~20th of the next month.
# Every release carries tables of the last 13 months (index and YoY growth), so a handful of releases covers
# the whole new-base history. PRIDs are PIB release ids. New-base ICI (2022-23=100) was launched with the
# June 2026 release; the OEA back series (from April 2023) is only on eaindustry.nic.in, which has a broken
# TLS certificate and is therefore NOT used: the new-base history here starts in June 2025.

PIB_URL = "https://www.pib.gov.in/PressReleasePage.aspx?PRID={}&reg=3&lang=1"

# New base 2022-23=100 releases (oldest first; later releases override earlier ones for the same month).
ICI_NEW_PRIDS = [
    2286615,   # June 2026 provisional (Jun-25 .. Jun-26), first release on the new base
    2313043,   # August 2026 provisional (Aug-25 .. Aug-26; Jul-26 final)
    2326038,   # September 2026 provisional (Sep-25 .. Sep-26; Aug-26 final)
]

# Old base 2011-12=100 releases, used only by the one-off seed builder (series was discontinued).
ICI_OLD_PRIDS = sorted([
    1619590,   # March 2020  (Mar-19 .. Mar-20)
    1642586,   # June 2020   (carries final Jan-Mar 2020)
    1715093,   # March 2021
    1786593,   # November 2021 (final Jan-Mar 2021)
    1821304,   # March 2022
    1863798,   # August 2022 (final Jan-Mar 2022)
    1920543,   # March 2023
    1981162,   # October 2023 (final Jan-Mar 2023)
    2019161,   # March 2024
    2079051,   # October 2024 (final Jan-Mar 2024)
    2123185,   # March 2025
    2181272,   # September 2025 (final Jan-Mar 2025)
    2253812,   # March 2026
    2276691,   # May 2026    (Apr-25 .. May-26), last release on the old base
])  # ascending PRID == chronological, so later (more revised) releases override earlier ones

ICI_SECTORS = [  # (PIB column header, slug id, display, new base only)
    ("Coal", "coal", "Coal", False),
    ("Crude Oil", "crude-oil", "Crude oil", False),
    ("Natural Gas", "natural-gas", "Natural gas", False),
    ("Refinery Products", "refinery-products", "Refinery products", False),
    ("Fertilizers", "fertilizers", "Fertilizers", False),
    ("Steel", "steel", "Steel", False),
    ("Cement", "cement", "Cement", False),
    ("Electricity", "electricity", "Electricity", False),
    ("Iron Ore", "iron-ore", "Iron ore", True),
    ("Overall", "overall", "Combined", False),
]

_MON = {m: i + 1 for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def _num(x):
    x = x.replace(",", "").strip()
    return float(x)


def _parse_ici_release(html: str, expect_cols: list, base_label: str):
    """Return ({col: {ts: index}}, {col: {ts: yoy}}) from the two 13-month annexure tables.
    Raises unless the headers, row structure and value ranges are exactly as expected."""
    soup = BeautifulSoup(html, "lxml")
    for sc in soup(["script", "style"]):
        sc.decompose()
    monthly = []
    for t in soup.find_all("table"):
        rows = [[c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])] for r in t.find_all("tr")]
        rows = [r for r in rows if r]
        if len(rows) < 4 or rows[0][0] != "Sector" or rows[1][0] != "Weight":
            continue
        if re.fullmatch(r"[A-Z][a-z]{2}-\d\d[*\s()P]*", rows[2][0]):
            monthly.append(rows)
    if len(monthly) < 2:
        raise RuntimeError("ICI release: monthly index/growth tables not found")
    idx_t, g_t = monthly[0], monthly[1]
    for t in (idx_t, g_t):
        hdr = t[0][1:]
        if hdr[:-1] != expect_cols or not hdr[-1].startswith("Overall"):
            raise RuntimeError(f"ICI release: unexpected columns {hdr}")
        if abs(sum(_num(w) for w in t[1][1:-1]) - 100) > 0.5:
            raise RuntimeError("ICI release: weights do not sum to 100")
    cols = expect_cols + ["Overall"]

    def grab(t, kind):
        out = {c: {} for c in cols}
        for r in t[2:]:
            m = re.fullmatch(r"([A-Z][a-z]{2})-(\d\d)[*\s()P]*", r[0])
            if not m or len(r) != len(cols) + 1:
                raise RuntimeError(f"ICI release: bad {kind} row {r}")
            ts_ = month_ts(2000 + int(m.group(2)), _MON[m.group(1)])
            for c, v in zip(cols, r[1:]):
                out[c][ts_] = _num(v)
        return out

    idx, g = grab(idx_t, "index"), grab(g_t, "growth")
    for c in cols:
        if len(idx[c]) < 12:
            raise RuntimeError(f"ICI release: {c} has only {len(idx[c])} months")
        for v in idx[c].values():
            if not 1 <= v <= 600:
                raise RuntimeError(f"ICI release ({base_label}): implausible index {c}={v}")
        for v in g[c].values():
            if not -100 <= v <= 800:  # Apr 2021 rebound vs the Apr 2020 lockdown
                raise RuntimeError(f"ICI release ({base_label}): implausible growth {c}={v}")
    return idx, g


NEW_COLS = ["Coal", "Natural Gas", "Crude Oil", "Refinery Products", "Fertilizers", "Steel", "Cement",
            "Electricity", "Iron Ore"]
OLD_COLS = ["Coal", "Crude Oil", "Natural Gas", "Refinery Products", "Fertilizers", "Steel", "Cement", "Electricity"]


def _merge_dicts(parsed: list, cols: list) -> tuple:
    idx = {c: {} for c in cols}
    g = {c: {} for c in cols}
    for i, gg in parsed:  # oldest first, later overrides
        for c in cols:
            idx[c].update(i[c])
            g[c].update(gg[c])
    return idx, g


_ICI_CACHE: dict = {}


PIB_ALLREL = "https://www.pib.gov.in/allRel.aspx"  # must be the www host: the bare host 301-redirects and a POST loses its body
PIB_COMMERCE_ID = "16"  # 'Ministry of Commerce & Industry' in the allRel.aspx ministry drop-down
PROBE_URLS.append(PIB_ALLREL)
DISCOVER_MONTHS = 4  # how many recent months of the PIB listing to scan for new ICI releases
_IGNORE_HARDCODED = False  # test switch: ignore ICI_NEW_PRIDS and rely on discovery only


def _pib_listing(http, month: int, year: int) -> list:
    """[(prid, title)] of Commerce & Industry releases in a month (ASP.NET post-back on allRel.aspx)."""
    s = BeautifulSoup(http.get(PIB_ALLREL, params={"reg": "3", "lang": "1"}).text, "lxml")
    d = {i["name"]: i.get("value", "") for i in s.find_all("input", type="hidden") if i.get("name")}
    d.update({
        "ctl00$ContentPlaceHolder1$ddlMinistry": PIB_COMMERCE_ID, "ctl00$ContentPlaceHolder1$ddlday": "0",
        "ctl00$ContentPlaceHolder1$ddlMonth": str(month), "ctl00$ContentPlaceHolder1$ddlYear": str(year),
        "__EVENTTARGET": "ctl00$ContentPlaceHolder1$ddlMinistry",
    })
    s2 = BeautifulSoup(http.post(PIB_ALLREL + "?reg=3&lang=1", data=d).text, "lxml")
    out = []
    for a in s2.find_all("a", href=True):
        mm = re.search(r"PRID=(\d+)", a["href"])
        if mm:
            out.append((int(mm.group(1)), a.get_text(" ", strip=True)))
    if not out:
        raise RuntimeError("PIB listing returned no releases (layout change?)")
    return out


def _discover_ici_prids(http) -> list:
    n = _now()
    found = set()
    y, m = n.year, n.month
    for _ in range(DISCOVER_MONTHS):
        for prid, title in _pib_listing(http, m, y):
            if re.search(r"core industries", title, re.I):
                found.add(prid)
        time.sleep(1)
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return sorted(found)


def _ici_new(http):
    if "new" in _ICI_CACHE:
        return _ICI_CACHE["new"]
    prids = set() if _IGNORE_HARDCODED else set(ICI_NEW_PRIDS)
    try:
        prids |= set(_discover_ici_prids(http))
    except Exception as e:  # noqa: BLE001 - fall back to the hard-coded list, but not silently if that is empty
        if not prids:
            raise RuntimeError(f"PIB release discovery failed and no fallback list: {e}")
    parsed = []
    for prid in sorted(prids):  # ascending PRID == chronological: later releases override earlier ones
        html = http.text(PIB_URL.format(prid))
        if "2022-23" not in html or "2011-12=100" in html[:200000].split("Posted On")[0]:
            continue  # an old-base (2011-12) release found by discovery: not used here
        try:
            parsed.append(_parse_ici_release(html, NEW_COLS, "2022-23"))
        except RuntimeError:
            if prid in ICI_NEW_PRIDS:
                raise
            continue
        time.sleep(1)
    if not parsed:
        raise RuntimeError("no 2022-23 base ICI release found")
    idx, g = _merge_dicts(parsed, NEW_COLS + ["Overall"])
    _ICI_CACHE["new"] = (idx, g)
    return idx, g


def _pts(d: dict) -> list:
    return [[t, d[t]] for t in sorted(d)]


def _check_continuous(pts: list, label: str):
    for (t0, _), (t1, _) in zip(pts, pts[1:]):
        a, b = EPOCH.fromtimestamp(t0, tz=timezone.utc), EPOCH.fromtimestamp(t1, tz=timezone.utc)
        if (b.year - a.year) * 12 + b.month - a.month != 1:
            raise RuntimeError(f"{label}: gap near {a:%Y-%m}")


def _rebuild_old_seed(http):
    """ONE-OFF (not called by builders): rebuild the seed files for the discontinued 2011-12 series."""
    parsed = []
    for prid in ICI_OLD_PRIDS:
        html = http.text(PIB_URL.format(prid))
        if "2011-12" not in html:
            raise RuntimeError(f"PIB {prid} is not a 2011-12 base ICI release")
        parsed.append(_parse_ici_release(html, OLD_COLS, "2011-12"))
        time.sleep(1)
    from .base import save_seed
    idx, g = _merge_dicts(parsed, OLD_COLS + ["Overall"])
    for col, sid, _, new_only in ICI_SECTORS:
        if new_only:
            continue
        save_seed(f"ici-old-{sid}", _pts(idx[col]))
        save_seed(f"ici-old-{sid}-yoy", _pts(g[col]))


_NEW_W = {"Coal": 5.596, "Natural Gas": 3.841, "Crude Oil": 7.43, "Refinery Products": 22.572, "Fertilizers": 2.731,
          "Steel": 17.584, "Cement": 4.41, "Electricity": 30.932, "Iron Ore": 4.905, "Overall": 100.0}
_OLD_W = {"Coal": 10.33, "Crude Oil": 8.98, "Natural Gas": 6.88, "Refinery Products": 28.04, "Fertilizers": 2.63,
          "Steel": 17.92, "Cement": 5.37, "Electricity": 19.85, "Overall": 100.0}

_WHAT = {
    "Coal": "raw coal production (the new series keeps raw coal only and drops coal middling and washed coal to avoid double counting)",
    "Crude Oil": "domestic crude oil production",
    "Natural Gas": "domestic natural gas production",
    "Refinery Products": "output of petroleum refineries (refined products)",
    "Fertilizers": "fertilizer production",
    "Steel": "steel production (the new 2022-23 series uses gross production, the old one used net production, so levels and growth differ between the two series)",
    "Cement": "cement production",
    "Electricity": "electricity generation",
    "Iron Ore": "iron ore production (added to the core list only in the 2022-23 series; it has no 2011-12 history)",
    "Overall": "the combined index of all core industries",
}
_NEEDS = {
    "Coal": "Coal output feeds power generation and steel, and monthly swings follow mine despatch and monsoon disruption.",
    "Crude Oil": "Domestic crude output has been in long decline, so it is a steady drag on the combined index rather than a cyclical signal.",
    "Natural Gas": "Domestic gas output is small relative to imports, and falls mostly reflect ageing fields.",
    "Refinery Products": "Refinery throughput follows fuel demand and export margins, and with the second-largest weight it is a useful coincident read on fuel consumption.",
    "Fertilizers": "Fertilizer output is lumpy (plant shutdowns and gas supply cause large one-month moves) and has a small weight.",
    "Steel": "Steel output is a coincident indicator of construction, infrastructure and manufacturing activity.",
    "Cement": "Cement output is a direct monthly proxy for housing and infrastructure construction, and it is seasonal (strong in January-March, weak in the monsoon).",
    "Electricity": "Electricity generation tracks economic activity and weather, and it is the single largest driver of the combined index.",
    "Iron Ore": "Iron ore is an upstream input to steel and is affected by mining and monsoon conditions.",
    "Overall": "The combined index moves with industrial and infrastructure activity and arrives about three weeks after the month end, well before quarterly GDP data.",
}


def _core_record(col: str, sid: str, display: str, new_only: bool, new):
    idx_n, g_n = new
    n_pts = _pts(idx_n[col])
    _check_continuous(n_pts, f"ICI new {col}")
    if len(n_pts) < 14:
        raise RuntimeError("ICI (2022-23): fewer than 14 months parsed")
    if (_now() - EPOCH.fromtimestamp(n_pts[-1][0], tz=timezone.utc)).days > 80:  # release for month M is due ~20th of M+1 (~50 days after the 1st); 80 leaves a month of grace
        raise RuntimeError(f"ICI (2022-23) latest month for {col} is stale ({n_pts[-1][0]}): add the newest "
                           "PIB release to ICI_NEW_PRIDS")
    views = [
        view("Index (2022-23=100)", series("index_new", f"{display} index (2022-23=100)", "Index (2022-23=100)", n_pts), vid="index"),
        view("YoY % (2022-23 base)", series("yoy_new", f"{display} YoY growth", "Per cent", _pts(g_n[col])), vid="yoy"),
    ]
    old_txt = ""
    if not new_only:
        idx_o = load_seed(f"ici-old-{sid}")
        g_o = load_seed(f"ici-old-{sid}-yoy")
        if len(idx_o) < 80 or idx_o[-1][0] != month_ts(2026, 5):
            raise RuntimeError(f"seed ici-old-{sid} missing or incomplete (run _rebuild_old_seed)")
        views += [
            view("Index (2011-12=100, discontinued)", series("index_old", f"{display} index (2011-12=100)", "Index (2011-12=100)", idx_o), vid="index_old"),
            view("YoY % (2011-12 base, discontinued)", series("yoy_old", f"{display} YoY growth (old base)", "Per cent", g_o), vid="yoy_old"),
        ]
        old_txt = (" The previous 2011-12=100 series (eight industries, March 2019 to May 2026, discontinued) is "
                   "kept as separate views and is not chained to the new one.")
    w_new, w_old = _NEW_W[col], _OLD_W.get(col)
    wtxt = f"{w_new:g} per cent of the new index" + (f" (it was {w_old:g} per cent in the old 2011-12 index)" if w_old else "")
    if col == "Overall":
        wtxt = "100"
    title = "Index of Core Industries: combined index" if col == "Overall" else f"Core industries: {display}"
    desc = (f"Monthly index of {_WHAT[col]}, part of the Index of Core Industries compiled by the Office of the Economic "
            f"Adviser (DPIIT). Base 2022-23=100 (from the June 2026 release; weight {wtxt}).{old_txt} Published provisionally "
            f"about 20 days after month end. {_NEEDS[col]}")
    if col == "Cement":
        desc += (" Unit note: this is an index (2022-23=100), not million tonnes; it replaces the stalled Cement "
                 "Production page for current data.")
    qna = [
        {"question": f"What is this core industry index ({display.lower()})?",
         "answer": "The Index of Core Industries (ICI) measures physical production volumes in the core industries: coal, crude oil, "
                   "natural gas, refinery products, fertilizers, steel, cement, electricity and (new in the 2022-23 series) iron ore. "
                   f"This indicator is {_WHAT[col]}.^"
                   "Following the earlier practice, the ICI weights are derived from the weights of the corresponding items of the IIP."},
        {"question": "How should I read it?",
         "answer": "Index levels compare a month with the 2022-23 average (=100). Production is seasonal, so use the year-on-year "
                   "growth view for momentum, and treat the latest month as provisional.^"
                   f"{_NEEDS[col]} Year-on-year growth is the figure PIB publishes (one decimal)."},
        {"question": "Base year, revisions and caveats",
         "answer": "From the June 2026 release the OEA publishes the ICI on base 2022-23=100 and has discontinued the 2011-12 "
                   "series (its linking factor for the combined index between the two is 1.47). The 2022-23 series differs in "
                   "coverage: iron ore added, gross steel production, raw coal only. Old and new levels are not comparable, so they "
                   "are separate views.^"
                   "Months are provisional at first release and revised in the following month's release. The figures here come from "
                   "PIB press releases, whose tables carry the last 13 months, so the new-base history on this page starts in June 2025; "
                   "the OEA back series from April 2023 is hosted on a website with a broken TLS certificate and is not used."},
    ]
    return record(
        f"core-{sid}", title=title, description=desc, frequency="M", views=views,
        source="PIB / Office of the Economic Adviser, DPIIT", source_url=PIB_URL.format(ICI_NEW_PRIDS[-1]),
        qna=qna, notes="History on the 2022-23 base begins June 2025 (13-month tables in PIB releases).")


def _make_core(col, sid, display, new_only):
    def build(http):
        return _core_record(col, sid, display, new_only, _ici_new(http))
    build.__name__ = "core_" + sid.replace("-", "_")
    return build


for _col, _sid, _disp, _new_only in ICI_SECTORS:
    register(f"core-{_sid}", section=SECTION, name=f"Core industries: {_disp}")(_make_core(_col, _sid, _disp, _new_only))

supersedes("cement-production", "core-cement")
