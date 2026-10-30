"""Labour-market and price-index indicators from official sources.

* CPI (new MoSPI series, base 2024=100): general index + 12 divisions, food classes/sub-classes,
  selected groups, and MoSPI's linked back series.  Source: MoSPI eSankhyiki public data API
  (api.mospi.gov.in; the same JSON/xlsx endpoints the esankhyiki.mospi.gov.in portal calls; no login).
* CPI-AL / CPI-RL (base 2019=100, plus the discontinued 1986-87 series): same API, /cpialrl.
* CPI-IW (Labour Bureau, base 2016=100): labourbureau.gov.in/centre-index (static HTML table).
* WPI primary / food / non-food articles (MoSPI API, bases 2022-23 and 2011-12, never spliced).

NOTE api.mospi.gov.in negotiates TLS with legacy renegotiation, which Python's OpenSSL 3 refuses by default.
The framework (sources/base.py) allows that for this one host only; certificate verification stays ON.

NOT built (see report): EPFO payroll (publication stopped after July 2025 data; epfindia is blocked from
GitHub, labour.gov.in payroll page is a client-rendered app) and fertiliser sales (no reachable
machine-readable monthly source).
"""

from __future__ import annotations

import io
import re
import ssl
import time
from datetime import datetime, timezone
from functools import lru_cache

import openpyxl
from bs4 import BeautifulSoup

from .base import (clean_pts, month_ts, record, register, series, view, yoy_pts, yoy_view)

SECTION = "labour-prices-inputs"
MOSPI = "https://api.mospi.gov.in/api"
LB_CENTRE = "https://labourbureau.gov.in/centre-index"

PROBE_URLS = [
    f"{MOSPI}/cpi/getCPIData?base_year=2024&level=Group&format=xlsx&series=Current&year=2025,2026&state_code=1&sector_code=1,2,3",
    f"{MOSPI}/cpialrl/getCpialrlRecords?indicator_code=1&state_code=99&limit=100&page=1",
    f"{MOSPI}/wpi/getWpiRecords?base_year=2022-23&format=xlsx&major_group_code=1000000000",
    f"{MOSPI}/wpi/getDownloadRecords?base_year=2011-12&major_group_code=1100000000",
    LB_CENTRE,
    "https://labourbureau.gov.in/consumer-price-index-numbers-for-industrial-workers",
]

MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"])}
MON3 = {k[:3]: v for k, v in MONTHS.items()}
SECTORS = ["Combined", "Rural", "Urban"]


# --------------------------------------------------------------- plumbing

def _mospi(http):
    """MoSPI's API host needs legacy TLS renegotiation; the framework (sources/base.py) handles that for this one host."""
    return http


def _xlsx_rows(http, url, params):
    """Ask the API to build an xlsx, then download it (the file appears a moment later). -> list of dicts."""
    _mospi(http)
    fp = http.get(url, params=params).json().get("file_path")
    if not fp:
        raise RuntimeError(f"no file_path from {url} {params}")
    for _ in range(8):
        time.sleep(2)
        r = http.s.get(f"{MOSPI}/download/{fp}", timeout=120)
        if r.status_code == 200 and r.content[:2] == b"PK":
            break
    else:
        raise RuntimeError(f"download of {fp} never became available")
    ws = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h) for h in rows[0]]
    return [dict(zip(hdr, r)) for r in rows[1:]]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _recent(pts, what, max_days=110):
    age = (datetime.now(timezone.utc).timestamp() - pts[-1][0]) / 86400
    if age > max_days:
        raise ValueError(f"{what}: latest period {datetime.fromtimestamp(pts[-1][0], timezone.utc):%Y-%m} is {age:.0f} days old")


def _rng(pts, lo, hi, what):
    bad = [p for p in pts if not lo <= p[1] <= hi]
    if bad:
        raise ValueError(f"{what}: implausible values {bad[:3]} (expected {lo}..{hi})")


def _years():
    y = datetime.now(timezone.utc).year
    return ",".join(str(i) for i in range(2025, y + 1))


# --------------------------------------------------------------- CPI base 2024

@lru_cache(maxsize=None)
def _cpi_level(http, key):
    """key: 'div' (general + divisions), or ('group'|'class'|'sub_class', codes)."""
    base = dict(base_year=2024, level="Group", format="xlsx", series="Current", year=_years(),
                state_code=1, sector_code="1,2,3")
    if key == "div":
        rows = _xlsx_rows(http, f"{MOSPI}/cpi/getCPIData", base)
        rows = [r for r in rows if r["group"] is None]
    else:
        kind, codes = key
        rows = _xlsx_rows(http, f"{MOSPI}/cpi/getCPIData", {**base, f"{kind}_code": codes})
        keep = {"group": lambda r: r["class"] is None,
                "class": lambda r: r["sub_class"] is None,
                "sub_class": lambda r: r["item"] is None}[kind]
        rows = [r for r in rows if r[kind] is not None and keep(r)]
    if not rows:
        raise ValueError(f"CPI 2024 {key}: no rows")
    time.sleep(1)
    return rows


def _cpi_collect(rows, namecol):
    """-> {(sector, name): ({ts: index}, {ts: inflation})}"""
    out = {}
    for r in rows:
        if r["base_year"] != "2024" or r["series"] != "Current":
            raise ValueError(f"unexpected base/series {r['base_year']}/{r['series']}")
        t = month_ts(int(r["year"]), MONTHS[r["month"]])
        idx, inf = _num(r["index"]), _num(r["inflation"])
        ix, fl = out.setdefault((r["sector"], r[namecol]), ({}, {}))
        if idx is not None:
            ix[t] = idx
        if inf is not None:
            fl[t] = inf
    return out


def _sel(data, sector, names, what, inflation=False, order=None):
    ss = []
    for n in names:
        if (sector, n) not in data:
            raise ValueError(f"{what}: {n!r} missing for {sector}")
        pts = clean_pts((data[(sector, n)][1 if inflation else 0]).items())
        if len(pts) < 2:
            raise ValueError(f"{what}: {n!r} {sector} has {len(pts)} points")
        if not inflation:
            _rng(pts, 40, 400, f"{what} {n}")
        else:
            _rng(pts, -30, 40, f"{what} {n} inflation")
        ss.append(series(f"{sector[0].lower()}_{len(ss)}", (order or {}).get(n, n),
                         "Per cent" if inflation else "Index (2024=100)", pts))
    return ss


CPI24_DIVS = ["Food and beverages", "Paan, tobacco and intoxicants", "Clothing and footwear",
              "Housing, water, electricity, gas and other fuels",
              "Furnishings, household equipment and routine household maintenance", "Health", "Transport",
              "Information and communication", "Recreation, sport and culture", "Education services",
              "Restaurants and accommodation services",
              "Personal care, social protection and miscellaneous goods and services"]
CPI24_SHORT = {"CPI (General)": "General", "Paan, tobacco and intoxicants": "Paan, tobacco & intoxicants",
               "Housing, water, electricity, gas and other fuels": "Housing, water, electricity, gas & fuels",
               "Furnishings, household equipment and routine household maintenance": "Furnishings & household maintenance",
               "Personal care, social protection and miscellaneous goods and services": "Personal care & misc. goods/services",
               "Restaurants and accommodation services": "Restaurants & accommodation"}

CPI24_NOTE = ("The new CPI (base 2024=100, MoSPI, first released 12 Feb 2026 for January 2026) replaced the 2012-base series. "
              "It uses the COICOP-2018 classification (12 divisions) and a 358-item basket weighted from the 2023-24 Household "
              "Consumption Expenditure Survey, so its groups are NOT comparable with the old Food/Fuel/Clothing/Housing/"
              "Miscellaneous groups and it has no 'core' aggregate. Index values start in January 2025 (the base year). "
              "Do not chain this series to the 2012-base series.")


@register("cpi-2024-general-divisions", section=SECTION, name="CPI (Base 2024=100): General Index and Divisions")
def cpi24_divisions(http):
    data = _cpi_collect(_cpi_level(http, "div"), "division")
    gen = "CPI (General)"
    views = []
    # general index, rural/urban/combined
    gi = []
    for sec in ["Combined", "Rural", "Urban"]:
        s = _sel(data, sec, [gen], "CPI general")[0]
        s["id"], s["name"] = f"gen_{sec.lower()}", sec
        gi.append(s)
    gi_inf = []
    for sec in ["Combined", "Rural", "Urban"]:
        s = _sel(data, sec, [gen], "CPI general inflation", inflation=True)[0]
        s["id"], s["name"] = f"gen_inf_{sec.lower()}", sec
        gi_inf.append(s)
    views.append(view("General index (All India)", *gi, vid="general"))
    views.append(view("General inflation, official YoY % (All India)", *gi_inf, vid="inflation"))
    for sec in SECTORS:
        names = CPI24_DIVS
        ss = _sel(data, sec, names, f"CPI divisions {sec}", order=CPI24_SHORT)
        for s, n in zip(ss, names):
            s["id"] = f"div_{sec.lower()}_{names.index(n)}"
        views.append(view(f"Divisions - {sec} index", *ss, vid=f"div_{sec.lower()}"))
    ss = _sel(data, "Combined", CPI24_DIVS, "CPI divisions inflation", inflation=True, order=CPI24_SHORT)
    for s, n in zip(ss, CPI24_DIVS):
        s["id"] = f"divinf_{CPI24_DIVS.index(n)}"
    views.append(view("Divisions - Combined inflation, official YoY %", *ss, vid="div_inflation"))
    last = gi[0]["pts"]
    _recent(last, "CPI 2024 general")
    if gi[0]["pts"][0][0] != month_ts(2025, 1):
        raise ValueError("CPI 2024 should start in Jan 2025")
    return record(
        "cpi-2024-general-divisions",
        title="CPI (Base 2024=100): General Index and Divisions",
        description=("India's consumer price index on MoSPI's new base (2024=100): all-India general index for rural, urban and combined, "
                     "and the 12 COICOP divisions (food & beverages, clothing, housing/utilities/fuels, transport, health, education, ...). "
                     "Monthly, index points, plus the official year-on-year inflation rate MoSPI publishes (from January 2026, "
                     "when a year-earlier index exists). This is the series that replaced the stalled 2012-base 'Miscellaneous' and "
                     "'Miscellaneous Rural' pages; the new classification has no 'Miscellaneous' group (its closest counterpart, division 12, "
                     "'Personal care, social protection and miscellaneous goods and services', is much narrower), so the series are not interchangeable. "
                     + CPI24_NOTE),
        frequency="M", views=views, source="MoSPI (eSankhyiki CPI, base 2024=100)",
        source_url="https://esankhyiki.mospi.gov.in/macroindicators?product=cpi",
        qna=[
            {"question": "What is this series and why does it start in 2025?",
             "answer": "The Consumer Price Index measures the change in retail prices of a fixed basket of goods and services bought by households. MoSPI re-based it from 2012 to 2024=100 in February 2026, updating the basket (358 items, including airfares, OTT subscriptions and online purchases) and weights from the 2023-24 household consumption survey.^Index values are published from January 2025 because 2024 is the base year; MoSPI also publishes a separate 'back series' back to 2013 (see the linked back-series page), which is a conversion of the old basket and not a like-for-like history."},
            {"question": "How should I read it for nowcasting?",
             "answer": "Use the Combined general index for the headline, and the rural/urban split to see where price pressure sits. Official inflation is the percentage change from the same month a year earlier; because the new index only begins in January 2025 it is available from January 2026 only.^The divisions let you track pass-through: food and beverages dominates month-to-month swings, while housing, transport, health and education move more slowly. Weights differ by sector (food carries a larger weight in rural India)."},
            {"question": "What are the main caveats?",
             "answer": "Groups are not comparable with the 2012-base series (for example there is no 'fuel and light' group; electricity, gas and other fuels sit within division 4 housing, and there is no 'miscellaneous' group). Do not splice this series onto the old one; ask MoSPI's linking factor if a continuous series is required.^Recent months can be revised (the figure for the latest month is provisional), and the series is short, so seasonal patterns cannot yet be estimated reliably from it alone."},
        ])


CPI24_FOOD_CLASSES = ["Cereals and cereal products", "Meat, fresh, chilled or frozen", "Fish and other seafood",
                      "Milk, other dairy products and eggs", "Oils and fats", "Fruits and nuts",
                      "Vegetables, tubers, plantains, cooking bananas and pulses", "Sugar, confectionery and desserts",
                      "Ready-made food and other food products"]
CPI24_SUBCLASSES = ["Cereals", "Pulses", "Leafy or stem vegetables, fresh or chilled",
                    "Fruit-bearing vegetables, fresh or chilled", "Green leguminous vegetables, fresh or chilled",
                    "Other vegetables, fresh or chilled", "Tubers, plantains and cooking bananas, fresh or chilled"]
CPI24_GROUPS = ["Food", "Beverages", "Clothing", "Footwear", "Actual rental payments made for housing",
                "Electricity, gas and other fuels", "Operation of personal transport equipment",
                "Passenger transport services", "Information and communication services",
                "Medicines and health products", "Outpatient care services"]
# group codes: Food 1, Beverages 2, Clothing 6, Footwear 7, rent 8, electricity/gas/fuels 11, personal transport 23,
# passenger transport 24, info & comm services 27, medicines 18, outpatient 19
_GROUP_CODES = "1,2,6,7,8,11,18,19,23,24,27"
_SUBCLASS_CODES = "1,26,27,28,29,30,31"
_CLASS_CODES = "1,2,3,4,5,6,7,8,9"


@register("cpi-2024-food-classes-groups", section=SECTION,
          name="CPI (Base 2024=100): Food Classes, Cereals, Pulses, Vegetables and Selected Groups")
def cpi24_detail(http):
    cls = _cpi_collect(_cpi_level(http, ("class", _CLASS_CODES)), "class")
    sub = _cpi_collect(_cpi_level(http, ("sub_class", _SUBCLASS_CODES)), "sub_class")
    grp = _cpi_collect(_cpi_level(http, ("group", _GROUP_CODES)), "group")
    views = []
    for tag, data, names, label in [("class", cls, CPI24_FOOD_CLASSES, "Food classes"),
                                    ("sub", sub, CPI24_SUBCLASSES, "Cereals, pulses & vegetables sub-classes"),
                                    ("grp", grp, CPI24_GROUPS, "Selected groups")]:
        for sec in SECTORS:
            ss = _sel(data, sec, names, f"CPI {label} {sec}")
            for i, s in enumerate(ss):
                s["id"] = f"{tag}_{sec.lower()}_{i}"
            views.append(view(f"{label} - {sec}", *ss, vid=f"{tag}_{sec.lower()}"))
    _recent(views[0]["series"][0]["pts"], "CPI 2024 food classes")
    return record(
        "cpi-2024-food-classes-groups",
        title="CPI (Base 2024=100): Food Classes, Cereals, Pulses, Vegetables and Selected Groups",
        description=("Detailed price indices from MoSPI's new CPI (2024=100), monthly, all-India, rural/urban/combined: the nine food classes "
                     "(cereals, meat, fish, milk & eggs, oils & fats, fruit, vegetables-and-pulses, sugar, ready-made food), the sub-classes "
                     "'Cereals', 'Pulses' and five vegetable sub-classes, and selected groups (food, beverages, clothing, footwear, "
                     "actual rent, electricity/gas/other fuels, personal transport, passenger transport, information & communication services, "
                     "medicines, outpatient care). Food-price detail matters for nowcasting because food swings drive headline CPI. "
                     "The new classification has no single 'vegetables' index: vegetables are split into five sub-classes and combined "
                     "with pulses in one class. Fuel and light is not a group any more (electricity, gas and fuels are one group inside housing). "
                     + CPI24_NOTE),
        frequency="M", views=views, source="MoSPI (eSankhyiki CPI, base 2024=100)",
        source_url="https://esankhyiki.mospi.gov.in/macroindicators?product=cpi",
        qna=[
            {"question": "What do these indices show?",
             "answer": "Each series is a price index (2024=100) for a product class or group in the new CPI, for All India. Rising values mean higher retail prices than in 2024.^Switch between Combined, Rural and Urban views: rural and urban baskets differ, and food has a larger weight in the rural index."},
            {"question": "Where are vegetables, pulses and fuel and light?",
             "answer": "In COICOP-2018 vegetables are published as five sub-classes (leafy, fruit-bearing, green leguminous, other vegetables, tubers) and the class 'Vegetables, tubers, plantains, cooking bananas and pulses' also includes pulses; 'Pulses' and 'Cereals' are published as separate sub-classes.^The old 'Fuel and light' group no longer exists: 'Electricity, gas and other fuels' is a group within division 4. These are close but not identical in coverage to the 2012-base groups, so do not splice them."},
            {"question": "Caveats?",
             "answer": "History starts in January 2025 only; year-on-year rates are available from January 2026 and are not computed here. Latest values are provisional. Individual sub-classes can be volatile (seasonal vegetables especially).^Source detail: MoSPI's eSankhyiki portal serves this data from api.mospi.gov.in; sub-class and class indices are published at All-India level for each sector."},
        ])


@register("cpi-2024-back-series-general", section=SECTION,
          name="CPI (Base 2024=100): Linked Back Series, General Index (2013-2024)")
def cpi24_back(http):
    rows = _xlsx_rows(http, f"{MOSPI}/cpi/getCPIData",
                      dict(base_year=2024, level="Group", format="xlsx", series="Back",
                           year=",".join(str(y) for y in range(2013, 2025)), state_code=1, sector_code="1,2,3"))
    rows = [r for r in rows if r["division"] == "CPI (General)" and r["group"] is None]
    d = {}
    for r in rows:
        if r["series"] != "Back":
            raise ValueError("unexpected series")
        d.setdefault(r["sector"], {})[month_ts(int(r["year"]), MONTHS[r["month"]])] = _num(r["index"])
    ss = []
    for sec in SECTORS:
        pts = clean_pts(d.get(sec, {}).items())
        if len(pts) < 100:
            raise ValueError(f"back series {sec}: {len(pts)} points")
        _rng(pts, 50, 130, f"back series {sec}")
        ss.append(series(f"back_{sec.lower()}", sec, "Index (2024=100, back series)", pts))
    if ss[0]["pts"][-1][0] != month_ts(2024, 12):
        raise ValueError("back series should end Dec 2024")
    return record(
        "cpi-2024-back-series-general",
        title="CPI (Base 2024=100): Linked Back Series, General Index (2013-2024)",
        description=("MoSPI's official 'back series' of the all-India CPI general index, expressed on the new 2024=100 base for January 2013 to December 2024 "
                     "(rural, urban, combined). It is the old (2012-base) basket re-referenced and linked by MoSPI so that long-run comparisons with the new series are possible; "
                     "it is NOT the new basket, and the first 'current series' value (January 2025) is not a continuation of its last value. "
                     "This page is kept separate from the current-series pages and the two are deliberately not joined. Useful for long-history inflation work: "
                     "the year-on-year view is computed here from the index."),
        frequency="M", views=[view("Index (2024=100, old basket)", *ss, vid="index"),
                              yoy_view(ss[0], "YoY % change (Combined)")],
        source="MoSPI (eSankhyiki CPI back series)", source_url="https://esankhyiki.mospi.gov.in/macroindicators?product=cpi",
        qna=[
            {"question": "What is the back series?",
             "answer": "When MoSPI moved to the 2024 base it published a linked back series from January 2013 to December 2024 by converting the 2012-base indices so that long-term comparison is feasible.^It carries the old basket and weights, so levels and growth rates measure the old basket's price changes."},
            {"question": "Can I join it to the current series?",
             "answer": "No. The current series is a different basket, classification and weighting; the two are not additive and January 2025 does not continue December 2024. Use the back series for 2013-2024 analysis and the current-series pages from 2025 onward, and treat any comparison across the boundary with care."},
            {"question": "Caveats?",
             "answer": "Only the general index (not groups) is available on this basis. Year-on-year rates shown here are computed from the index and can differ by a rounding digit from the official inflation rates published at the time."},
        ])


# --------------------------------------------------------------- CPI-AL / CPI-RL

@lru_cache(maxsize=None)
def _alrl_rows(http, indicator):
    _mospi(http)
    rows, pg = [], 1
    while True:
        d = http.get(f"{MOSPI}/cpialrl/getCpialrlRecords",
                     params=dict(indicator_code=indicator, state_code=99, limit=100, page=pg)).json()
        rows += d["data"]
        if pg >= d["meta_data"]["totalPages"]:
            break
        pg += 1
        time.sleep(1)
    return rows


def _alrl_ts(year, month):
    m = MONTHS[month]
    if "-" in str(year):  # financial year 'YYYY-YYYY'
        y0, y1 = (int(x) for x in str(year).split("-"))
        return month_ts(y0 if m >= 4 else y1, m)
    return month_ts(int(year), m)


@register("cpi-al-rl-2019", section=SECTION, name="CPI for Agricultural and Rural Labourers (CPI-AL / CPI-RL)")
def cpi_alrl(http):
    gen = _alrl_rows(http, 1)
    grp = _alrl_rows(http, 2)
    new = [r for r in gen if r["base_year"] == "2019"]
    old = [r for r in gen if r["base_year"] == "1986-1987"]
    if len(new) < 12 or len(old) < 200:
        raise ValueError(f"CPI-AL/RL rows: new {len(new)} old {len(old)}")

    def pts(rows, col):
        return clean_pts((_alrl_ts(r["year"], r["month"]), r[col]) for r in rows)
    al, rl = pts(new, "index_al"), pts(new, "index_rl")
    ial, irl = pts(new, "inflation_al"), pts(new, "inflation_rl")
    for p, nm in [(al, "AL"), (rl, "RL")]:
        _rng(p, 100, 200, f"CPI-{nm} 2019")
        _recent(p, f"CPI-{nm}")
    _rng(ial, -10, 25, "CPI-AL inflation")
    _rng(irl, -10, 25, "CPI-RL inflation")
    views = [view("General index, base 2019=100", series("al", "CPI-AL", "Index (2019=100)", al),
                  series("rl", "CPI-RL", "Index (2019=100)", rl), vid="general"),
             view("Inflation, official YoY % (base 2019)", series("al_inf", "CPI-AL", "Per cent", ial),
                  series("rl_inf", "CPI-RL", "Per cent", irl), vid="inflation")]
    groups = sorted({r["group"] for r in grp if r["base_year"] == "2019"})
    if len(groups) != 5:
        raise ValueError(f"CPI-AL/RL groups: {groups}")
    for col, lab in [("index_al", "CPI-AL"), ("index_rl", "CPI-RL")]:
        ss = []
        for i, g in enumerate(groups):
            p = clean_pts((_alrl_ts(r["year"], r["month"]), r[col]) for r in grp if r["group"] == g)
            _rng(p, 90, 260, f"{lab} {g}")
            ss.append(series(f"{col}_{i}", g, "Index (2019=100)", p))
        views.append(view(f"Group indices - {lab} (2019=100)", *ss, vid=f"grp_{col}"))
    oal, orl = pts(old, "index_al"), pts(old, "index_rl")
    _rng(oal, 50, 1500, "CPI-AL 1986-87")
    _rng(orl, 50, 1500, "CPI-RL 1986-87")
    views.append(view("Discontinued series, base 1986-87=100 (to Mar 2025; not linked to 2019 base)",
                      series("al_old", "CPI-AL (1986-87=100)", "Index (1986-87=100)", oal),
                      series("rl_old", "CPI-RL (1986-87=100)", "Index (1986-87=100)", orl), vid="old"))
    return record(
        "cpi-al-rl-2019",
        title="CPI for Agricultural and Rural Labourers (CPI-AL / CPI-RL)",
        description=("Labour Bureau's consumer price indices for agricultural labourers (CPI-AL) and rural labourers (CPI-RL), all-India, monthly, "
                     "as served by MoSPI's eSankhyiki. The current series has base 2019=100 and starts in June 2025; it replaced the 1986-87=100 series "
                     "(shown separately, up to March 2025; the two are not spliced). Includes the official year-on-year inflation and the five "
                     "group indices (food & non-alcoholic beverages, pan/tobacco/intoxicants, clothing & footwear, fuel & light, miscellaneous). "
                     "These indices feed rural wage indexation and minimum-wage debates and are a timely read on rural cost of living."),
        frequency="M", views=views, source="Labour Bureau via MoSPI eSankhyiki (CPI-AL/RL)",
        source_url="https://esankhyiki.mospi.gov.in/macroindicators?product=cpialrl",
        qna=[
            {"question": "What do CPI-AL and CPI-RL measure?",
             "answer": "They track retail prices of the basket consumed by agricultural labourer households (CPI-AL) and rural labourer households (CPI-RL), compiled by the Labour Bureau from village price collection. They are used to revise minimum wages and for rural cost-of-living analysis.^Inflation is the year-on-year percentage change in the all-India general index."},
            {"question": "Why are there two bases?",
             "answer": "The Labour Bureau moved from base 1986-87=100 to base 2019=100 with data from mid-2025. The old series ends in March 2025 and the new begins in June 2025; the levels (hundreds vs. ~145) are not comparable, and the page does not splice them."},
            {"question": "Caveats?",
             "answer": "The new-base series is short (since June 2025); the latest month is provisional. The old series carries financial-year labels in the source; months were mapped to calendar dates (April-December to the first year, January-March to the second)."},
        ])


# --------------------------------------------------------------- CPI-IW (Labour Bureau)

@lru_cache(maxsize=None)
def _cpi_iw_table(http):
    html = http.text(LB_CENTRE)
    soup = BeautifulSoup(html, "lxml")
    tb = soup.find("table")
    if tb is None:
        raise ValueError("CPI-IW page: no table")
    rows = tb.find_all("tr")
    hdr = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
    want = ["S.No", "Base Year", "State", "Centre", "Year", "Month", "Index"]
    if hdr != want:
        raise ValueError(f"CPI-IW table header changed: {hdr}")
    out = []
    for r in rows[1:]:
        c = [x.get_text(" ", strip=True) for x in r.find_all("td")]
        if len(c) == 7 and c[3] == "All India" and c[2].startswith("All India"):
            out.append(c)
    return out


LB_HOME_IW = "https://labourbureau.gov.in/consumer-price-index-numbers-for-industrial-workers"


@lru_cache(maxsize=None)
def _cpi_iw_last12(http):
    """The page's own 'last 12 months' bar chart: {ts: index}. Used to fill a month the table omits and to cross-check."""
    html = http.text(LB_HOME_IW)
    m1 = re.search(r"labels:\s*\[([^\]]+)\]", html)
    m2 = re.search(r"data:\s*\[([0-9.,\s]+)\]", html)
    if not (m1 and m2):
        raise ValueError("CPI-IW home chart not found")
    labs = [re.sub(r"\s+", "", x.strip(" '\"")) for x in m1.group(1).split(",")]
    vals = [float(x) for x in m2.group(1).split(",") if x.strip()]
    if len(labs) != len(vals) or len(vals) < 12:
        raise ValueError("CPI-IW home chart layout changed")
    out = {}
    for lab, v in zip(labs, vals):
        mm = re.match(r"([A-Za-z]{3})-?(\d{2})", lab)
        out[month_ts(2000 + int(mm.group(2)), MON3[mm.group(1)])] = v
    return out


@register("cpi-iw-2016", section=SECTION, name="CPI for Industrial Workers (CPI-IW), General Index")
def cpi_iw(http):
    rows = _cpi_iw_table(http)
    d = {}
    for _, base, _, _, y, m, idx in rows:
        if base != "2016":
            raise ValueError(f"unexpected CPI-IW base {base}")
        d[month_ts(int(y), MON3[m])] = _num(idx)
    last12 = _cpi_iw_last12(http)
    for t, v in last12.items():
        if t in d and abs(d[t] - v) > 0.05:
            raise ValueError(f"CPI-IW table/home chart disagree at {t}: {d[t]} vs {v}")
        d.setdefault(t, v)
    pts = clean_pts(d.items())
    if len(pts) < 60:
        raise ValueError(f"CPI-IW only {len(pts)} months")
    if pts[0][0] != month_ts(2020, 9):
        raise ValueError("CPI-IW should start Sep 2020")
    # the Labour Bureau table omits the all-India row for a few months; tolerate a handful, never fill them
    expected = (datetime.fromtimestamp(pts[-1][0], timezone.utc).year - 2020) * 12 +         datetime.fromtimestamp(pts[-1][0], timezone.utc).month - 9 + 1
    if expected - len(pts) > 5:
        raise ValueError(f"CPI-IW missing {expected - len(pts)} months")
    _rng(pts, 100, 250, "CPI-IW")
    _recent(pts, "CPI-IW", 100)
    s = series("cpi_iw", "CPI-IW general index", "Index (2016=100)", pts)
    return record(
        "cpi-iw-2016",
        title="CPI for Industrial Workers (CPI-IW), General Index",
        description=("All-India general index of the Consumer Price Index for Industrial Workers (base 2016=100), compiled monthly by the Labour Bureau from retail prices "
                     "in 317 markets across 88 industrial centres, released on the last working day of the following month. It determines dearness allowance for central "
                     "government employees and wage indexation, so it is watched for fiscal and wage-bargaining effects. History here starts in September 2020, "
                     "when the 2016-base series took effect; the older 2001-base series is not spliced on. A few months (August 2023, December 2023, January 2025) are missing because the Labour Bureau table omits the all-India row for them; they are left blank, not estimated. Year-on-year inflation is computed from the index."),
        frequency="M", views=[view("Index (2016=100)", s, vid="index"), yoy_view(s, "YoY % change")],
        source="Labour Bureau, Ministry of Labour & Employment", source_url=LB_CENTRE,
        qna=[
            {"question": "What is CPI-IW?",
             "answer": "A fixed-basket retail price index for households of industrial workers (factory, mining, plantation, railway, port and electricity workers), covering food, pan/tobacco, clothing, housing, fuel and light and miscellaneous items. The Labour Bureau publishes the All-India index and 88 centre indices.^Its main practical use is to set the dearness allowance of central government staff and pensioners."},
            {"question": "How does it differ from the national CPI?",
             "answer": "CPI-IW has a different basket (weighted to industrial workers' consumption), covers 88 industrial centres rather than all districts, and has base 2016=100, so its inflation can differ materially from MoSPI's CPI (it has a heavier food weight and no rural component). It is not a replacement for headline CPI."},
            {"question": "Caveats?",
             "answer": "Values for the latest month can be revised in the next release. The series on this page starts in September 2020 because the 2016 base took effect then. Year-on-year rates are computed from rounded one-decimal index values and may differ from the Labour Bureau's published rates by a few basis points."},
        ])


# --------------------------------------------------------------- WPI

@lru_cache(maxsize=None)
def _wpi23(http):
    rows = _xlsx_rows(http, f"{MOSPI}/wpi/getWpiRecords",
                      dict(base_year="2022-23", format="xlsx", major_group_code="1000000000,1100000000,1200000000,1300000000,1400000000"))
    if not rows or "major_group" not in rows[0]:
        raise ValueError("WPI 2022-23 layout changed")
    return rows


@lru_cache(maxsize=None)
def _wpi12(http):
    _mospi(http)
    rows = _xlsx_rows(http, f"{MOSPI}/wpi/getDownloadRecords",
                      dict(base_year="2011-12", major_group_code="1100000000"))
    if not rows or "majorgroup" not in rows[0]:
        raise ValueError("WPI 2011-12 layout changed")
    return rows


def _wpi_pts(rows, level_col, name, mgcol, mgname=None):
    d = {}
    for r in rows:
        if r[level_col] != name:
            continue
        deeper = [r[c] for c in ("sub_group", "sub_sub_group", "item", "subgroup", "sub_subgroup") if c in r]
        if any(v is not None for v in deeper):
            continue
        if mgname and r[mgcol] != mgname:
            continue
        d[month_ts(int(r["year"]), MONTHS[r["month"]])] = _num(r["index_value"])
    return clean_pts(d.items())


@register("wpi-primary-food-nonfood-articles", section=SECTION,
          name="WPI Primary Articles, Food Articles and Non-Food Articles")
def wpi_primary(http):
    r23, r12 = _wpi23(http), _wpi12(http)
    views = []
    for tag, rows, lab, mgcol, units in [("b2223", r23, "base 2022-23=100", "major_group", "Index (2022-23=100)"),
                                         ("b1112", r12, "base 2011-12=100", "majorgroup", "Index (2011-12=100)")]:
        top = "Primary Articles" if tag == "b2223" else "Primary articles"
        def grab(name, level):
            pts = _wpi_pts(rows, level, name, mgcol)
            if len(pts) < 24:
                raise ValueError(f"WPI {lab} {name}: {len(pts)} months")
            _rng(pts, 80, 400, f"WPI {lab} {name}")
            return pts
        # major-group rows have group=None and major_group==top
        pm = {}
        for r in rows:
            if r[mgcol] == top and r["group"] is None:
                pm[month_ts(int(r["year"]), MONTHS[r["month"]])] = _num(r["index_value"])
        prim = clean_pts(pm.items())
        if len(prim) < 24:
            raise ValueError(f"WPI {lab} primary: {len(prim)} months")
        _rng(prim, 80, 400, f"WPI {lab} primary")
        food = grab("Food Articles" if tag == "b2223" else "Food articles", "group")
        nonfood = grab("Non-Food Articles" if tag == "b2223" else "Non-food articles", "group")
        series_ = [series(f"{tag}_primary", "Primary articles", units, prim),
                   series(f"{tag}_food", "Food articles", units, food),
                   series(f"{tag}_nonfood", "Non-food articles", units, nonfood)]
        views.append(view(f"Index, {lab}" + (" (to Apr 2026, discontinued)" if tag == "b1112" else ""), *series_, vid=tag))
        yo = [series(f"{tag}_{n}_yoy", nm, "Per cent", yoy_pts(s["pts"]))
              for n, nm, s in zip(("primary", "food", "nonfood"), ("Primary articles", "Food articles", "Non-food articles"), series_)]
        views.append(view(f"YoY % change, {lab}", *yo, vid=f"{tag}_yoy"))
        if tag == "b2223":
            _recent(prim, "WPI primary 2022-23")
    return record(
        "wpi-primary-food-nonfood-articles",
        title="WPI Primary Articles, Food Articles and Non-Food Articles",
        description=("Wholesale Price Index (Office of the Economic Adviser, DPIIT; served via MoSPI eSankhyiki) for 'Primary Articles' (food articles, non-food articles, minerals), "
                     "'Food Articles' and 'Non-Food Articles', monthly, index points and year-on-year % change. Food articles are the main source of volatility in wholesale "
                     "prices and lead retail food CPI by a few weeks, so they are a useful early signal. Two WPI bases are shown in separate views and never joined: "
                     "the current series (2022-23=100, from April 2023; YoY from April 2024) and the previous 2011-12=100 series (April 2012 to April 2026). "
                     "A non-food manufacturing ('core') aggregate is not published in this data feed, so it is not included."),
        frequency="M", views=views, source="DPIIT Office of the Economic Adviser via MoSPI eSankhyiki (WPI)",
        source_url="https://esankhyiki.mospi.gov.in/macroindicators?product=wpi",
        qna=[
            {"question": "What do these indices measure?",
             "answer": "The WPI tracks price changes of goods at the wholesale stage, before retail margins. Primary Articles (about a fifth of the index) cover unprocessed food and non-food agricultural items and minerals; Food Articles are the cereals, pulses, vegetables, fruit, milk, eggs, meat and fish within it.^The year-on-year view is the percentage change on the same month a year earlier."},
            {"question": "Which base should I use?",
             "answer": "The 2022-23=100 series is the current one and carries the latest months; it only begins in April 2023, so YoY starts in April 2024. The 2011-12=100 series has the longer history (April 2012 to April 2026) but is discontinued. The two are not comparable in level and are shown separately."},
            {"question": "Caveats?",
             "answer": "WPI is provisional for the latest two months and is revised to a final index after about two months. Food articles are prone to large swings from vegetable prices; do not over-read one month."},
        ])


# The 2012-base "Miscellaneous" groups stopped at Dec 2025 when MoSPI moved to the 2024 base, which has no
# comparable group; the pointer sends readers to the current CPI by division.
from .base import supersedes  # noqa: E402

supersedes("consumer-price-index-cpi-inflation-miscellaneous", "cpi-2024-general-divisions")
supersedes("consumer-price-index-cpi-inflation-miscellaneous-rural", "cpi-2024-general-divisions")
