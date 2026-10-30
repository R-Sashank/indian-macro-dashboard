"""Monthly external-trade detail: merchandise trade by commodity group (Commerce Ministry quick
estimates, via PIB), services trade (RBI press release), crude oil imports.

History is a one-off backfill stored in sources/seed/*.json and extended on every run from the
latest monthly releases (a fresh value always overrides the seed for the same month).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from functools import lru_cache

from bs4 import BeautifulSoup

from .base import (
    load_seed, month_ts, record, register, series, view, yoy_pts, yoy_view,
)

SECTION = "trade-detail"

# Exact URLs requested (GET). Also POSTed with form data: the RBI and PIB archive pages above (month / day
# listings) and https://ppac.gov.in/AjaxController/getImportExports (PPAC import/export tables, JSON).
PROBE_URLS = [
    "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx",
    "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=63699",  # monthly services trade (Aug 2026)
    "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=63493",  # quarterly BoP (Q1 2026-27)
    "https://www.pib.gov.in/allRel.aspx?reg=3&lang=1",
    "https://www.pib.gov.in/PressReleasePage.aspx?PRID=2310636",
    "https://static.pib.gov.in/WriteReadData/specificdocs/documents/2026/sep/doc_9876_20260915_19512001.pdf",
    "https://ppac.gov.in/import-export",
]

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
_MON3 = {m[:3]: i + 1 for i, m in enumerate(MONTHS)}


def _sleep():
    time.sleep(1.0)  # ~1 request/second per host


# ============================================================ RBI: services trade

_RBI = "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx"


def _rbi_month_list(http, y: int, m: int) -> list:
    """(prid, title) of every RBI press release in month y-m (RBI archive form post)."""
    s = BeautifulSoup(http.text(_RBI), "lxml")
    d = {i["name"]: i.get("value", "") for i in s.find_all("input", {"type": "hidden"}) if i.get("name")}
    d.update({"hdnYear": str(y), "hdnMonth": str(m)})
    t = http.post(_RBI, data=d).text
    _sleep()
    return re.findall(r"BS_PressReleaseDisplay\.aspx\?prid=(\d+)'?\"?[^>]*>([^<]*)", t)


def parse_rbi_services(html: str) -> dict:
    """{(year, month): (exports, imports)} in US$ million from one RBI services release."""
    s = BeautifulSoup(html, "lxml")
    tb = s.find("table", class_="tablebg") or s
    lines = [x.strip() for x in tb.get_text("\n", strip=True).split("\n") if x.strip()]
    if not any("Receipts" in x for x in lines):
        raise ValueError("RBI services release: 'Receipts' header not found")
    mon = re.compile(r"^([A-Za-z]{3,9})\s*[-–—]?\s*(\d{2,4})$")
    out, cur = {}, None
    for ln in lines:
        mm = mon.match(ln)
        if mm and mm.group(1).lower()[:3] in _MON3 and mm.group(1).lower() in MONTHS + [k for k in _MON3]:
            y = int(mm.group(2))
            y = y + 2000 if y < 100 else y
            cur = (y, _MON3[mm.group(1).lower()[:3]])
            out[cur] = []
            continue
        if cur is not None:
            if re.match(r"^\d{1,3}(,\d{3})+$|^\d{3,6}$", ln):
                out[cur].append(int(ln.replace(",", "")))
            elif ln.lower().startswith("note"):
                cur = None
    res = {}
    for k, v in out.items():
        if len(v) != 2:
            raise ValueError(f"RBI services release: month {k} has values {v}")
        if not (3000 < v[0] < 60000 and 1500 < v[1] < 40000):
            raise ValueError(f"RBI services release: implausible values {k} {v}")
        res[k] = (v[0], v[1])
    if not res:
        raise ValueError("RBI services release: no months parsed")
    return res


def _services_points(http):
    """({ts: exports}, {ts: imports}) = seed history merged with the latest RBI release."""
    ex = {t: v for t, v in load_seed("trade-services-exports")}
    im = {t: v for t, v in load_seed("trade-services-imports")}
    now = datetime.now(timezone.utc)
    cands = []
    for back in (0, 1):
        y, m = now.year, now.month - back
        if m < 1:
            y, m = y - 1, m + 12
        cands += [(int(p), t) for p, t in _rbi_month_list(http, y, m) if re.search(r"(?i)trade in services", t)]
    if not cands:
        raise RuntimeError("no RBI 'International Trade in Services' release found in the last two months")
    prid = max(p for p, _ in cands)
    html = http.text(f"{_RBI}?prid={prid}")
    got = parse_rbi_services(html)
    for (y, m), (e, i) in got.items():
        ex[month_ts(y, m)] = e
        im[month_ts(y, m)] = i
    last = max(got)
    age = (now.year - last[0]) * 12 + now.month - last[1]
    if age > 3:
        raise RuntimeError(f"RBI services data stale: latest month {last}")
    return ex, im


# ============================================== Commerce quick estimates (PIB release + PDF)

import io  # noqa: E402

import pdfplumber  # noqa: E402

_PIB_LIST = "https://www.pib.gov.in/allRel.aspx?reg=3&lang=1"
_PIB_REL = "https://www.pib.gov.in/PressReleasePage.aspx?PRID="
_PIB_COMMERCE = "16"  # 'Ministry of Commerce & Industry' in the PIB ministry drop-down

# canonical key -> regex on the start of the (lower-cased, and->&) row label
_EXPORT_ROWS = {
    "total": r"(grand total|total export)\s",
    "engineering": r"engineering goods",
    "petroleum": r"petroleum products",
    "electronics": r"electronic goods",
    "gems": r"gems & jewellery",
    "pharma": r"drugs & pharmaceuticals",
    "chemicals": r"organic & inorganic",
    "rmg": r"rmg of all",
    "rice": r"rice\s",
    "marine": r"marine products",
}
_IMPORT_ROWS = {
    "total": r"(grand total|india.s total import)\s",
    "oil": r"petroleum, crude",
    "gold": r"gold\s",
    "silver": r"silver\s",
    "pearls": r"pearls, precious",
    "electronics": r"electronic goods",
    "machinery": r"machinery, electrical",
    "coal": r"coal, coke",
    "fertiliser": r"fertilisers",
    "vegoil": r"vegetable oil",
}
_NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def _close(growth, new, old) -> bool:
    if not old:
        return False
    return abs((new / old - 1) * 100 - growth) <= 0.3 + 50.0 / abs(old) * (1 + abs(new / old))


def _row(line: str, table: dict):
    """-> (key, [numbers]) if `line` is one of the wanted rows."""
    low = re.sub(r"\band\b", "&", line.lower())
    low = re.sub(r"^[\d\s]+(?=[a-z])", "", low)  # serial number (may be split: "3 0 Silver")
    for key, rx in table.items():
        m = re.match(rx, low + " ")
        if m:
            nums = [float(x.replace(",", "")) for x in _NUM.findall(low[m.end() - 1:])]
            return key, nums
    return None


def parse_qe_pdf(data: bytes):
    """Parse a Commerce 'quick estimates' PDF (both layouts used since 2021).

    Returns (releases_month, {'export': {key: {(y, m): value}}, 'import': {...}}) in US$ million;
    `releases_month` is the (year, month) the estimates are for."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [(p.extract_text() or "") for p in pdf.pages]
    out = {"export": {}, "import": {}}
    cur = None
    seen = set()
    layouts = set()
    for txt in pages:
        head = txt[:700]
        kind = None
        layout = None
        mm = re.search(r"QUICK ESTIMATES FOR SELECTED MAJOR COMMODITIES FOR\s+([A-Z]+)\s*,?\s*(\d{4})", head)
        if mm and "Million USD" in head.replace("MillionUSD", "Million USD"):
            layout = "old"
            kind = "export" if "TRADE: EXPORT" in head else "import" if "TRADE: IMPORT" in head else None
            mon, yr = mm.group(1).lower(), int(mm.group(2))
        elif re.search(r"Table \d: India.s Merchandise (Export|Import) of QE Groups", head):
            layout = "new"
            kind = "export" if "Merchandise Export" in head else "import"
            m2 = re.search(r"Quick Estimate\s+([A-Za-z]+)\s*,?\s*(\d{4})", txt)
            if not m2:
                continue
            mon, yr = m2.group(1).lower(), int(m2.group(2))
        if not kind or mon not in MONTHS:
            continue
        month = (yr, MONTHS.index(mon) + 1)
        seen.add(kind)
        layouts.add(layout)
        if cur and cur != month:
            raise ValueError(f"QE pdf mixes months {cur} and {month}")
        cur = month
        py = (month[0] - 1, month[1])
        prev = (month[0], month[1] - 1) if month[1] > 1 else (month[0] - 1, 12)
        table = _EXPORT_ROWS if kind == "export" else _IMPORT_ROWS
        lines = txt.splitlines()
        for li, line in enumerate(lines):
            cands = [line]
            # numbers may sit on a line whose label wrapped onto the line before and/or after:
            #   "Pearls, precious & Semi-precious" / "19 stones 1414.23 ..."
            ms = re.match(r"^(\d+)\s+([A-Za-z][^\d]*?)?\s*(-?\d[\d.,\s-]*)$", line.strip())
            if ms:
                own = (ms.group(2) or "").strip()
                for j in (li - 1, li + 1):
                    if 0 <= j < len(lines) and not re.search(r"\d", lines[j]):
                        nb = lines[j].strip()
                        # a wrapped label only ever joins to the numbers line it belongs to: before + own,
                        # own + after, or (when the numbers line has no text of its own) the neighbour alone
                        lab = (nb if not own else (nb + " " + own) if j < li else (own + " " + nb))
                        cands.append(f"{ms.group(1)} {lab.strip()} {ms.group(3)}")
            r = None
            for c in cands:
                r = _row(c, table)
                if r and r[1]:  # a label-only line (numbers on the next line) is skipped
                    break
                r = None
            if not r:
                continue
            key, n = r
            if out[kind].get(key):
                continue  # first occurrence only
            if layout == "old":
                if len(n) == 3 and _close(n[2], n[1], n[0]):  # April: month = cumulative, so only 3 columns
                    out[kind][key] = {py: n[0], month: n[1]}
                    continue
                if len(n) != 6 or not _close(n[4], n[2], n[0]):
                    raise ValueError(f"QE pdf: unexpected numbers for {kind}/{key}: {n}")
                out[kind][key] = {py: n[0], month: n[2]}
            else:
                if len(n) < 11 or not _close(n[5], n[4], n[3]) or not _close(n[7], n[4], n[6]):
                    raise ValueError(f"QE pdf: unexpected numbers for {kind}/{key}: {n}")
                out[kind][key] = {py: n[3], month: n[4], prev: n[6]}
    if cur is None:
        raise ValueError("QE pdf: no commodity tables found")
    for kind, tbl in (("export", _EXPORT_ROWS), ("import", _IMPORT_ROWS)):
        if kind not in seen:
            continue
        miss = [k for k in tbl if k not in out[kind]]
        if miss:
            raise ValueError(f"QE pdf ({cur}): missing {kind} rows {miss}")
    return cur, out, ("old" if layouts == {"old"} else "new")


_PIB_TITLE_KW = re.compile(r"(?i)export|import|trade|merchandise|overall|foreign|cumulative")
_STATIC = re.compile(r'href="(https://static\.pib\.gov\.in/WriteReadData/specificdocs/documents/[^"]+\.(?:pdf|PDF))"')


def _pib_list(http, y: int, m: int, day: int) -> list:
    """(prid, title) of Ministry of Commerce press releases on one day (PIB archive form post)."""
    t0 = http.text(_PIB_LIST)
    _sleep()
    s = BeautifulSoup(t0, "lxml")
    d = {i["name"]: i.get("value", "") for i in s.find_all("input", {"type": "hidden"}) if i.get("name")}
    d.update({
        "ctl00$ContentPlaceHolder1$ddlMinistry": _PIB_COMMERCE,
        "ctl00$ContentPlaceHolder1$ddlday": str(day),
        "ctl00$ContentPlaceHolder1$ddlMonth": str(m),
        "ctl00$ContentPlaceHolder1$ddlYear": str(y),
        "__EVENTTARGET": "ctl00$ContentPlaceHolder1$ddlYear",
    })
    t = http.post(_PIB_LIST, data=d).text
    _sleep()
    return [(p, ti) for ti, p in re.findall(r"<a title='([^']*)' href='/PressReleaseDetail\.aspx\?PRID=(\d+)'", t)]


def parse_qe_release_page(http, prid: str):
    """Download the PDFs attached to one PIB release and merge what parses as quick estimates.

    The 2-decimal 'QUICK ESTIMATES FOR SELECTED MAJOR COMMODITIES' PDF is preferred; the newer 'Exim
    summary' PDFs (integers, but with the previous month too) only fill months it does not have.
    -> (month, {'export': {...}, 'import': {...}}) or None if the page has no quick-estimate PDF."""
    page = http.text(_PIB_REL + str(prid))
    _sleep()
    parsed, errors = [], []
    for url in dict.fromkeys(_STATIC.findall(page)):
        data = http.bytes(url)
        _sleep()
        try:
            parsed.append(parse_qe_pdf(data))
        except ValueError as e:
            if "no commodity tables" not in str(e):  # country-wise tables etc. are expected to fail
                errors.append(f"{url.rsplit('/', 1)[-1]}: {e}")
    if not parsed:
        if errors:
            raise ValueError(f"PIB release {prid}: {'; '.join(errors)}")
        return None
    months = {mo for mo, _, _ in parsed}
    if len(months) != 1:
        raise ValueError(f"PIB release {prid}: PDFs for different months {sorted(months)}")
    month = months.pop()
    merged = {"export": {}, "import": {}}
    for _, out, layout in sorted(parsed, key=lambda x: x[2] != "old"):  # old layout first
        for kind in merged:
            for key, vals in out[kind].items():
                tgt = merged[kind].setdefault(key, {})
                for mon, v in vals.items():
                    tgt.setdefault(mon, v)
    for kind, tbl in (("export", _EXPORT_ROWS), ("import", _IMPORT_ROWS)):
        if any(k not in merged[kind] for k in tbl):
            raise ValueError(f"PIB release {prid} ({month}): {kind} table incomplete; {'; '.join(errors)}")
    return month, merged


def find_qe_release(http, y: int, m: int):
    """Quick-estimate data released in month y-m (usually on the 15th), or None if not out yet."""
    for day in (15, 14, 16, 17, 13, 18, 12, 19, 20):
        for prid, title in _pib_list(http, y, m, day):
            if not _PIB_TITLE_KW.search(title):
                continue
            got = parse_qe_release_page(http, prid)
            if got:
                return got
    return None


_CACHE: dict = {}


def _qe_all(http) -> dict:
    """{('export'|'import', key): {ts: US$ million}} = seed history + the latest two releases."""
    if "qe" in _CACHE:
        return _CACHE["qe"]
    seed = load_seed("trade-qe")
    data = {(k, key): {t: v for t, v in pts} for k, d in seed.items() for key, pts in d.items()}
    now = datetime.now(timezone.utc)
    found = []
    for back in (1, 0):
        y, m = now.year, now.month - back
        if m < 1:
            y, m = y - 1, m + 12
        if back == 0 and now.day < 14:
            continue  # this month's release is not out yet
        got = find_qe_release(http, y, m)
        if got:
            found.append(got)
    if not found:
        raise RuntimeError("no Commerce quick-estimate release found on PIB in the last two months")
    for month, out in found:  # oldest first, so the newest release wins
        for kind, d in out.items():
            for key, vals in d.items():
                tgt = data.setdefault((kind, key), {})
                for (y, m), v in vals.items():
                    tgt[month_ts(y, m)] = v
    latest = max(mo for mo, _ in found)
    age = (now.year - latest[0]) * 12 + now.month - latest[1]
    if age > 3:
        raise RuntimeError(f"Commerce quick estimates stale: latest month {latest}")
    _CACHE["qe"] = data
    return data


def _pts(d: dict) -> list:
    return [[t, d[t]] for t in sorted(d)]


def _check(name: str, pts: list, lo: float, hi: float, max_age_months: int = 3, min_pts: int = 60,
           jump: float = 0) -> list:
    if len(pts) < min_pts:
        raise ValueError(f"{name}: only {len(pts)} points")
    bad = [p for p in pts if not lo <= p[1] <= hi]
    if bad:
        raise ValueError(f"{name}: implausible values {bad[:3]}")
    last = datetime.fromtimestamp(pts[-1][0], timezone.utc)
    now = datetime.now(timezone.utc)
    if (now.year - last.year) * 12 + now.month - last.month > max_age_months:
        raise ValueError(f"{name}: latest point {last:%Y-%m} is stale")
    if jump:  # catches a row picked up from the wrong line of a PDF table (checked from mid-2021, after the Covid trough)
        for (_, v0), (t1, v1) in zip(pts, pts[1:]):
            if t1 >= 1622505600 and v0 > 0 and not (1 / jump <= v1 / v0 <= jump):
                raise ValueError(f"{name}: implausible jump to {v1} in {datetime.fromtimestamp(t1, timezone.utc):%Y-%m} (from {v0})")
    # a month missing in the middle means a failed merge, not a real gap
    for a, b in zip(pts, pts[1:]):
        da, db = datetime.fromtimestamp(a[0], timezone.utc), datetime.fromtimestamp(b[0], timezone.utc)
        if (db.year - da.year) * 12 + db.month - da.month != 1:
            raise ValueError(f"{name}: gap between {da:%Y-%m} and {db:%Y-%m}")
    return pts


def _yoy_multi(label, items, vid="yoy"):
    """One 'YoY %' view with a growth series per (id, name, pts)."""
    ss = []
    for sid, name, pts in items:
        y = yoy_pts(pts)
        if y:
            ss.append(series(sid, name, "Per cent", y))
    return view(label, *ss, vid=vid) if ss else None


_QE_SOURCE = "Ministry of Commerce & Industry (DGCI&S quick estimates), via PIB"
_QE_URL = "https://www.pib.gov.in/allRel.aspx"
_QE_CAVEAT = (
    "The latest month is a quick estimate and is revised when final data come in about a month later; "
    "earlier months on this page use the revised figure published in the next year's release."
)


# ------------------------------------------------------------ builders

@register("merchandise-exports-imports-total", section=SECTION, name="Merchandise Exports and Imports (Total)")
def merchandise_totals(http):
    d = _qe_all(http)
    ex = _check("exports", _pts(d[("export", "total")]), 5000, 80000, jump=1.6)
    im = _check("imports", _pts(d[("import", "total")]), 8000, 110000, jump=1.6)
    sx = series("exports", "Merchandise exports", "US$ million", ex)
    si = series("imports", "Merchandise imports", "US$ million", im)
    return record(
        "merchandise-exports-imports-total",
        title="Merchandise Exports and Imports (Total)",
        description=(
            "Total value of India's goods exports (including re-exports) and imports each month, in US$ million, "
            "from the Commerce Ministry's DGCI&S quick estimates. The monthly export and import levels behind the "
            "trade balance are the starting point for tracking external demand (exports) and domestic demand plus "
            "oil and gold prices (imports), and they arrive about two weeks after the month ends, well ahead of GDP."
        ),
        frequency="M",
        views=[view("US$ million", sx, si), _yoy_multi("YoY % change", [("yoy_exports", "Exports", ex), ("yoy_imports", "Imports", im)])],
        source=_QE_SOURCE, source_url=_QE_URL, notes=_QE_CAVEAT,
        qna=[
            {"question": "What does this series measure?", "answer": (
                "The value of merchandise (goods) exports and imports in US dollars, as compiled by DGCI&S and released by the "
                "Commerce Ministry around the 15th of each month for the previous month.^"
                "It differs from the Merchandise Trade Balance page only in showing the two levels separately rather than their difference.")},
            {"question": "How should I read it?", "answer": (
                "Compare each month with the same month a year earlier (see the YoY view), since exports and imports are seasonal "
                "and swing with oil prices and gold imports.^"
                "Imports are heavily influenced by crude prices and gold; for the underlying domestic demand signal look at the non-oil non-gold import series on the dashboard.")},
            {"question": "What are the caveats?", "answer": (
                "The latest month is a quick estimate and is usually revised; this page keeps the revised value for older months where a later release shows it.^"
                "Values are in current US dollars, so rupee depreciation or commodity prices change them without any change in volumes. "
                "Figures here are goods only; services trade is on a separate page.")},
        ],
    )


@register("oil-imports", section=SECTION, name="Petroleum (Crude and Products) Imports")
def oil_imports(http):
    d = _qe_all(http)
    oil = _check("oil imports", _pts(d[("import", "oil")]), 2000, 40000, jump=2)
    s = series("oil", "Petroleum, crude and products imports", "US$ million", oil)
    return record(
        "oil-imports",
        title="Petroleum (Crude and Products) Imports",
        description=(
            "Monthly value of India's imports of petroleum, crude and products, in US$ million, from the Commerce Ministry quick "
            "estimates. Oil is about a quarter of the import bill, so this series separates the price-driven swings in imports "
            "from the rest and is a quick read on the oil import burden for the current account and inflation."
        ),
        frequency="M",
        views=[view("US$ million", s), yoy_view(s)],
        source=_QE_SOURCE, source_url=_QE_URL, notes=_QE_CAVEAT,
        qna=[
            {"question": "What is included?", "answer": (
                "The DGCI&S group 'Petroleum, Crude & products': crude oil plus imported refined products and petroleum gases such as LNG and LPG classified there.^"
                "For crude oil alone (value and tonnes) see the PPAC crude oil imports page.")},
            {"question": "How do I read it?", "answer": (
                "Value is price times volume. A rise with flat volumes reflects dearer crude (or a weaker rupee), not more consumption; compare with the crude oil volume series.^"
                "Month-to-month swings also reflect cargo timing.")},
            {"question": "Caveats", "answer": (
                "The latest month is a quick estimate and is revised later. Values are in US dollars, so the rupee cost differs.")},
        ],
    )


@register("gold-silver-imports", section=SECTION, name="Gold and Silver Imports")
def gold_imports(http):
    d = _qe_all(http)
    gold = _check("gold imports", _pts(d[("import", "gold")]), 0, 15000, jump=6)
    silver = _check("silver imports", _pts(d[("import", "silver")]), 0, 5000)
    pearls = _check("pearls & stones imports", _pts(d[("import", "pearls")]), 0, 6000, jump=8)
    sg = series("gold", "Gold", "US$ million", gold)
    ss = series("silver", "Silver", "US$ million", silver)
    sp = series("pearls", "Pearls, precious and semi-precious stones", "US$ million", pearls)
    return record(
        "gold-silver-imports",
        title="Gold and Silver Imports",
        description=(
            "Monthly value of India's gold and silver imports, plus pearls and precious stones, in US$ million, from the Commerce "
            "Ministry quick estimates. Gold imports are lumpy and driven by price, festival and wedding demand and import duty changes; "
            "stripping them out (as the dashboard's non-oil non-gold import series does) gives a cleaner read of trade, "
            "while the gold series itself tracks household savings behaviour and a swing factor in the trade deficit."
        ),
        frequency="M",
        views=[view("Gold and silver", sg, ss), view("Gems inputs", sp, vid="stones"), yoy_view(sg, "Gold YoY % change")],
        source=_QE_SOURCE, source_url=_QE_URL, notes=_QE_CAVEAT + " Gold volumes (tonnes) are not published in the quick-estimate tables.",
        qna=[
            {"question": "What does it show?", "answer": (
                "The value in US dollars of gold and silver imported each month (DGCI&S quick estimates), together with pearls, precious and semi-precious stones, which feed the gems and jewellery export industry.^"
                "Volumes are not published in these tables, so a price rise raises the value even if tonnes fall.")},
            {"question": "How do I read it?", "answer": (
                "Gold imports respond to the international gold price, the domestic price premium, import duty and festival demand; they can double or halve within a month.^"
                "A fall in gold imports narrows the trade deficit but is not a sign of weaker domestic demand in the same way a fall in non-gold imports would be.")},
            {"question": "Caveats", "answer": (
                "The latest month is a quick estimate and is revised later. The gold group is as defined by DGCI&S; it can differ from RBI or customs-duty based measures of gold inflows.")},
        ],
    )


_EXPORT_NAMES = {
    "engineering": "Engineering goods", "petroleum": "Petroleum products", "electronics": "Electronic goods",
    "gems": "Gems and jewellery", "pharma": "Drugs and pharmaceuticals", "chemicals": "Organic and inorganic chemicals",
    "rmg": "Readymade garments (all textiles)", "rice": "Rice", "marine": "Marine products",
}
_IMPORT_NAMES = {
    "electronics": "Electronic goods", "machinery": "Machinery (electrical and non-electrical)",
    "coal": "Coal, coke and briquettes", "fertiliser": "Fertilisers", "vegoil": "Vegetable oil",
}


@register("exports-by-commodity-group", section=SECTION, name="Exports by Commodity Group")
def exports_by_group(http):
    d = _qe_all(http)
    ser = {}
    for k in _EXPORT_NAMES:
        ser[k] = _check(f"exports {k}", _pts(d[("export", k)]), 20, 25000, jump=3)
    S = {k: series(k, nm, "US$ million", ser[k]) for k, nm in _EXPORT_NAMES.items()}
    big = ["engineering", "petroleum", "electronics", "gems"]
    rest = ["pharma", "chemicals", "rmg", "marine", "rice"]
    return record(
        "exports-by-commodity-group",
        title="Exports by Commodity Group",
        description=(
            "Monthly merchandise exports of the principal commodity groups (engineering goods, petroleum products, electronics, gems "
            "and jewellery, pharmaceuticals, chemicals, garments, marine products, rice), in US$ million, from the Commerce Ministry "
            "quick estimates. The commodity mix shows which sectors are driving export growth, for example the electronics "
            "(mobile phone) boom or oil-price-driven petroleum product exports, which a total hides."
        ),
        frequency="M",
        views=[
            view("Large groups", *[S[k] for k in big]),
            view("Other groups", *[S[k] for k in rest], vid="other"),
            _yoy_multi("YoY % change (large groups)", [(k + "_yoy", _EXPORT_NAMES[k], ser[k]) for k in big], vid="yoy"),
            _yoy_multi("YoY % change (other groups)", [(k + "_yoy", _EXPORT_NAMES[k], ser[k]) for k in rest], vid="yoy_other"),
        ],
        source=_QE_SOURCE, source_url=_QE_URL, notes=_QE_CAVEAT,
        qna=[
            {"question": "What is covered?", "answer": (
                "Nine of the 30 principal commodity groups in the DGCI&S quick estimates, in US$ million per month.^"
                "These groups are a subset; they do not add to total exports (see the total merchandise page). Existing dashboard pages cover manufactured, agricultural and non-oil exports as broader aggregates.")},
            {"question": "How should I read it?", "answer": (
                "Use the YoY view: base effects, exchange rates and tariffs move individual groups sharply. Petroleum product exports track oil prices and refinery margins; engineering goods and electronics track external demand and tariff shifts, notably the US.^"
                "Gems and jewellery also depends on gold and diamond prices.")},
            {"question": "Caveats", "answer": (
                "Group definitions follow the DGCI&S quick-estimate groups; the latest month is a quick estimate that is revised. Values are nominal US dollars.")},
        ],
    )


@register("imports-by-commodity-group", section=SECTION, name="Imports by Commodity Group")
def imports_by_group(http):
    d = _qe_all(http)
    ser = {k: _check(f"imports {k}", _pts(d[("import", k)]), 20, 25000, jump=3) for k in _IMPORT_NAMES}
    S = {k: series(k, nm, "US$ million", ser[k]) for k, nm in _IMPORT_NAMES.items()}
    return record(
        "imports-by-commodity-group",
        title="Imports by Commodity Group",
        description=(
            "Monthly merchandise imports of selected principal commodity groups (electronic goods, machinery, coal, fertilisers, "
            "vegetable oil), in US$ million, from the Commerce Ministry quick estimates. Machinery and electronics imports are a "
            "read on investment and manufacturing demand; coal, fertiliser and edible oil show energy and farm input needs."
        ),
        frequency="M",
        views=[view("US$ million", *S.values()),
               _yoy_multi("YoY % change", [(k + "_yoy", _IMPORT_NAMES[k], ser[k]) for k in _IMPORT_NAMES])],
        source=_QE_SOURCE, source_url=_QE_URL, notes=_QE_CAVEAT,
        qna=[
            {"question": "What is covered?", "answer": (
                "Five of the 30 import groups in the DGCI&S quick estimates. Oil and gold have their own pages on this dashboard.^"
                "Values are in US$ million and groups do not add to total imports.")},
            {"question": "How do I read it?", "answer": (
                "Capital-goods type imports (machinery, electronics) are an indicator of investment and electronics-manufacturing activity (much of which is imported components), but they are also affected by exchange rates and price changes.^"
                "Fertiliser and coal imports are seasonal and policy dependent.")},
            {"question": "Caveats", "answer": (
                "The latest month is a quick estimate and is revised. Nominal US dollar values; no volume adjustment.")},
        ],
    )


@register("services-exports-imports", section=SECTION, name="Services Exports and Imports")
def services_trade(http):
    ex, im = _services_points(http)
    ex, im = _pts(ex), _pts(im)
    _check("services exports", ex, 3000, 60000, min_pts=100)
    _check("services imports", im, 1500, 40000, min_pts=100)
    sx = series("exports", "Services exports (receipts)", "US$ million", ex)
    si = series("imports", "Services imports (payments)", "US$ million", im)
    return record(
        "services-exports-imports",
        title="Services Exports and Imports",
        description=(
            "India's monthly services exports (receipts) and imports (payments) in US$ million, from the RBI's monthly press "
            "release on international trade in services. Services exports (IT, business services, transport, travel) are the "
            "main offset to the goods trade deficit, and the monthly data arrive about a month after the reference month, "
            "months before the quarterly balance of payments."
        ),
        frequency="M",
        views=[view("US$ million", sx, si), _yoy_multi("YoY % change", [("yoy_exports", "Exports", ex), ("yoy_imports", "Imports", im)])],
        source="Reserve Bank of India (monthly data on India's international trade in services)",
        source_url="https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx",
        notes=("The latest two months are provisional; RBI revises them pro rata when each quarter's balance of payments is published. "
               "Earlier months here are as first published in the RBI releases (provisional, or revised where a later release showed them), so old months may differ from the final BoP figures."),
        qna=[
            {"question": "What does it measure?", "answer": (
                "Receipts (exports) and payments (imports) on services account, in US$ million, from the RBI. The Services Trade Net Inflows page on this dashboard is the difference of these two.^"
                "Coverage follows the balance of payments definition (software, business, transport, travel, financial and other services).")},
            {"question": "How should I read it?", "answer": (
                "Look at year-on-year growth in the second view; monthly values are lumpy, with quarter-end months often larger.^"
                "Services exports have been the main offset to the goods trade deficit.")},
            {"question": "Revisions and caveats", "answer": (
                "The most recent months are provisional and later re-based to the quarterly balance of payments. Values on this page for months published long ago are the figures from the RBI release that last showed them.^"
                "The Commerce Ministry's own figure for the latest services month (in its press release) is a projection, not RBI data, and is not used here.")},
        ],
    )


# ============================================================ PPAC: crude oil imports

_PPAC_API = "https://ppac.gov.in/AjaxController/getImportExports"
_PPAC_PAGE = "https://ppac.gov.in/import-export"
_FY_MONTHS = ["april", "may", "june", "july", "august", "september", "october", "november", "december",
              "january", "february", "march"]
_HTML = re.compile(r"<[^>]+>")


def _ppac_crude(http, fy_start: int, report_by: int, page_id: str) -> dict:
    """{ts: value} of CRUDE OIL imports for one financial year (report_by 1 = '000 tonnes, 3 = US$ million)."""
    j = http.post(_PPAC_API, data={"financialYear": f"{fy_start}-{fy_start + 1}", "reportBy": report_by, "pageId": page_id},
                  headers={"X-Requested-With": "XMLHttpRequest"}).json()
    _sleep()
    res = j.get("result")
    if not isinstance(res, dict):
        return {}
    rows = [v for v in res.values() if isinstance(v, dict) and _HTML.sub("", v.get("title", "")).strip().upper() == "CRUDE OIL"]
    if len(rows) != 1:
        raise ValueError(f"PPAC import table {fy_start}: expected one CRUDE OIL row, found {len(rows)}")
    out = {}
    for i, mname in enumerate(_FY_MONTHS):
        txt = _HTML.sub("", str(rows[0].get(mname, ""))).strip().replace(",", "")
        if not txt:
            continue
        y, mo = (fy_start, i + 4) if i < 9 else (fy_start + 1, i - 8)
        out[month_ts(y, mo)] = float(txt)
    return out


@register("crude-oil-imports", section=SECTION, name="Crude Oil Imports (Volume and Value)")
def crude_oil_imports(http):
    page = http.text(_PPAC_PAGE)
    _sleep()
    m = re.search(r'id="page_id"[^>]*value="(\d+)"', page) or re.search(r'value="(\d+)"[^>]*id="page_id"', page)
    if not m:
        raise ValueError("PPAC import/export page: page_id not found")
    pid = m.group(1)
    now = datetime.now(timezone.utc)
    cur_fy = now.year if now.month >= 4 else now.year - 1
    qty, val = {}, {}
    for fy in range(2020, cur_fy + 1):
        qty.update(_ppac_crude(http, fy, 1, pid))
        val.update(_ppac_crude(http, fy, 3, pid))
    qty, val = _pts(qty), _pts(val)
    _check("crude volume", qty, 5000, 30000, min_pts=60)
    _check("crude value", val, 1500, 30000, min_pts=60)
    if [t for t, _ in qty] != [t for t, _ in val]:
        raise ValueError("PPAC crude: volume and value cover different months")
    sq = series("volume", "Crude oil imports (volume)", "'000 tonnes", qty)
    sv = series("value", "Crude oil imports (value)", "US$ million", val)
    return record(
        "crude-oil-imports",
        title="Crude Oil Imports (Volume and Value)",
        description=(
            "India's monthly crude oil imports in thousand tonnes and in US$ million, from the Petroleum Planning & Analysis Cell "
            "(PPAC). Volume tracks refinery runs and fuel demand; value adds the crude price and the exchange rate. Together they "
            "split the oil import bill into quantity and price, which matters for the current account and for fuel inflation."
        ),
        frequency="M",
        views=[view("Volume ('000 tonnes)", sq), view("Value (US$ million)", sv, vid="value"),
               yoy_view(sq, "Volume YoY % change"),
               view("Value YoY % change", series("value_yoy", "Value YoY change", "Per cent", yoy_pts(val)), vid="value_yoy")],
        source="Petroleum Planning & Analysis Cell (Ministry of Petroleum & Natural Gas)",
        source_url=_PPAC_PAGE,
        notes="PPAC data for the latest month can be revised. The Commerce Ministry's 'Petroleum, crude & products' series is broader (it includes refined products and gas).",
        qna=[
            {"question": "What does it measure?", "answer": (
                "Imports of crude oil (not refined products) by quantity in thousand metric tonnes and by value in US$ million, compiled by PPAC. "
                "India imports the large majority of the crude it refines.^"
                "The oil-imports page (Commerce Ministry) covers crude plus products in value only.")},
            {"question": "How should I read it?", "answer": (
                "Volume moves with refinery throughput and fuel demand, with refinery maintenance and cargo timing causing monthly noise; use the year-on-year view.^"
                "Dividing value by volume gives the average landed price per tonne, which follows the crude price with a lag.")},
            {"question": "Caveats", "answer": (
                "PPAC's most recent month is provisional and revisions occur. Data for each financial year are read from PPAC's import/export report, here from April 2020 onwards.")},
        ],
    )


# ============================================== RBI balance of payments: remittances (quarterly)

_QNAME = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_REMIT_RX = re.compile(
    r"(?:Private|Personal) transfer receipts[^.]{0,120}?remittances by Indians employed overseas[^$]{0,160}?"
    r"US\$\s*([\d.]+)\s*billion(.{0,160})", re.S)
_TITLE_RX = re.compile(
    r"Balance of Payments during the\s+(First|Second|Third|Fourth)\s+Quarter\s*\([A-Za-z]+-[A-Za-z]+\)\s+of\s+(\d{4})-(\d{2})", re.I)


def _fy_quarter_end(fy_start: int, q: int) -> int:
    """Quarter-end timestamp of financial-year quarter q (Q1 = Apr-Jun) for FY starting in fy_start."""
    from .base import quarter_end_ts
    return quarter_end_ts(fy_start + (1 if q == 4 else 0), (q + 1) if q < 4 else 1)


def parse_remittances_text(text: str) -> dict:
    """{quarter_end_ts: US$ billion} from the text of one RBI quarterly BoP press release.

    Gives the release's own quarter and, when the text states it, the revised year-earlier quarter."""
    t = re.sub(r"\s+", " ", text)
    mt = _TITLE_RX.search(t)
    if not mt:
        raise ValueError("RBI BoP release: quarter title not found")
    q, fy = _QNAME[mt.group(1).lower()], int(mt.group(2))
    mr = _REMIT_RX.search(t)
    if not mr:
        raise ValueError("RBI BoP release: remittances sentence not found")
    val = float(mr.group(1))
    if not 8 <= val <= 80:
        raise ValueError(f"RBI BoP release: implausible remittances {val}")
    out = {_fy_quarter_end(fy, q): val}
    # newer wording: "... rose to US$ 42.9 billion in Q1:2026-27 from US$ 33.2 billion in Q1:2025-26"
    mp = re.match(r"\s*in Q(\d):\s?(\d{4})-\d{2}\s+from\s+US\$\s*([\d.]+)\s*billion\s+in Q(\d):\s?(\d{4})-\d{2}", mr.group(2))
    if mp and int(mp.group(1)) == q and int(mp.group(2)) == fy and int(mp.group(4)) == q and int(mp.group(5)) == fy - 1:
        out[_fy_quarter_end(fy - 1, q)] = float(mp.group(3))  # revised year-earlier quarter
    return out


def _remittances_points(http) -> list:
    pts = {t: v for t, v in load_seed("trade-remittances")}
    now = datetime.now(timezone.utc)
    cands = []
    for back in range(0, 4):
        y, m = now.year, now.month - back
        if m < 1:
            y, m = y - 1, m + 12
        cands += [int(p) for p, t in _rbi_month_list(http, y, m)
                  if re.search(r"(?i)balance of payments during the .*quarter", t)]
        if cands:
            break
    if not cands:
        raise RuntimeError("no RBI quarterly balance-of-payments release found in the last four months")
    html = http.text(f"{_RBI}?prid={max(cands)}")
    s = BeautifulSoup(html, "lxml")
    tb = s.find("table", class_="tablebg") or s
    pts.update(parse_remittances_text(tb.get_text(" ", strip=True)))
    return _pts(pts)


@register("remittances-private-transfers", section=SECTION, name="Remittances (Private Transfer Receipts, Quarterly)")
def remittances(http):
    pts = _remittances_points(http)
    last = datetime.fromtimestamp(pts[-1][0], timezone.utc)
    now = datetime.now(timezone.utc)
    if (now.year - last.year) * 12 + now.month - last.month > 8:
        raise ValueError(f"remittances: latest quarter {last:%Y-%m} is stale")
    if len(pts) < 20:
        raise ValueError(f"remittances: only {len(pts)} quarters")
    for a, b in zip(pts, pts[1:]):
        da, db = datetime.fromtimestamp(a[0], timezone.utc), datetime.fromtimestamp(b[0], timezone.utc)
        if (db.year - da.year) * 12 + db.month - da.month != 3:
            raise ValueError(f"remittances: gap between {da:%Y-%m} and {db:%Y-%m}")
    s = series("remittances", "Private transfer receipts (remittances)", "US$ billion", pts)
    return record(
        "remittances-private-transfers",
        title="Remittances (Private Transfer Receipts, Quarterly)",
        description=(
            "Private (personal) transfer receipts in India's balance of payments, mainly remittances by Indians working overseas, "
            "in US$ billion per quarter. QUARTERLY data from the RBI's balance of payments press release. Remittances finance "
            "a large part of the goods trade deficit and support household consumption, especially in Kerala, Punjab and Uttar Pradesh; "
            "they respond to Gulf and US labour markets and the rupee."
        ),
        frequency="Q",
        views=[view("US$ billion", s), yoy_view(s)],
        source="Reserve Bank of India (Developments in India's Balance of Payments, quarterly press release)",
        source_url="https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx",
        notes=("Quarterly only. Each quarter is taken from the RBI press release for that quarter (preliminary); where a later release "
               "states a revised figure for the year-earlier quarter, that revision replaces it. Other quarters keep their first-published values, "
               "so older quarters can differ from the RBI's latest BoP tables."),
        qna=[
            {"question": "What does this measure?", "answer": (
                "Private transfer receipts (called personal transfer receipts since the BPM6 re-labelling), the largest part of secondary income in the current account, mainly money sent home by Indians employed abroad.^"
                "This is quarterly data, in US$ billion, for the quarter ending on the date shown.")},
            {"question": "How should I read it?", "answer": (
                "Use the year-on-year view: remittances grow with the number of overseas workers, wages abroad and the rupee, and fell in 2020 during the pandemic.^"
                "Because they are about 3 per cent of GDP they matter for the current account balance: a rise in remittances narrows the current account deficit.")},
            {"question": "Caveats", "answer": (
                "Figures come from the narrative of RBI's press release (US$ billion, one decimal) rather than the detailed BoP tables, which are published as files on a host that blocks automated downloads. Each quarter's first release is preliminary and is revised later.^"
                "Remittances are net of nothing: they are gross receipts, while outward remittances are a separate (smaller) payments item.")},
        ],
    )
