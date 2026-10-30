"""Transport & vehicles: FADA retail registrations, AAI air traffic (cargo/passengers/movements),
Indian Railways freight loading, major-port container traffic.

Everything here comes from official / association press material fetched over plain HTTPS.
Long histories are stored as seeds (scraper/sources/seed/*.json, built once by the helper
functions at the bottom of each section) and merged with a live fetch of the latest periods.
"""

from __future__ import annotations

import io
import re
import time
from functools import lru_cache
from urllib.parse import quote

from .base import (
    EPOCH, clean_pts, iso, load_seed, merge_pts, month_ts, record, register, series,
    supersedes, ts, view, yoy_view,
)

SECTION = "transport-vehicles"

PROBE_URLS: list = [
    "https://fada.in/press-release-list.php?page=1",
    "https://www.aai.aero/en/business-opportunities/aai-traffic-news",
    "https://www.aai.aero/sites/default/files/traffic-news/TRSep2k_26.pdf",
    "https://fada.in/images/press-release/16ac46ad2e0388FADA%20Releases%20September%202026%20Vehicle%20Retail%20Data.pdf",
    "https://shipmin.gov.in/sites/default/files/Cargo%20handled%20at%20Major%20Ports%20Sep%202026.pdf",
]

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_MON_RE = re.compile(r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\s*['’‘`]\s*(\d{2})\b", re.I)
_INT_RE = re.compile(r"^-?\d[\d,]*$")


def _pdf_pages(data: bytes, max_pages: int | None = None):
    import logging
    import pdfplumber

    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, pg in enumerate(pdf.pages):
            if max_pages is not None and i >= max_pages:
                return
            yield pg.extract_text() or ""


def _month_tokens(line: str) -> list:
    out = []
    for m in _MON_RE.finditer(line):
        t = month_ts(2000 + int(m.group(2)), _MONTHS[m.group(1).upper()[:3]])
        if t not in out:
            out.append(t)
    return out


# ============================================================ FADA retail registrations

FADA_BASE = "https://fada.in/"
FADA_LIST = FADA_BASE + "press-release-list.php?page={p}"
_FADA_SKIP = re.compile(r"YTD|FY|CY|Q\d|Festiv|Navratri|APR\s*-", re.I)
_FADA_LABELS = {"2W": "2w", "3W": "3w", "PV": "pv", "CV": "cv", "TRAC": "trac", "TRACTOR": "trac", "TOTAL": "total"}
FADA_SERIES = [
    ("total", "All vehicles"), ("2w", "Two-wheelers"), ("pv", "Passenger vehicles"),
    ("cv", "Commercial vehicles"), ("3w", "Three-wheelers"), ("trac", "Tractors"),
]


def fada_parse(data: bytes):
    """One FADA press release PDF -> (lead_month_ts, {cat: {month_ts: units}}).

    Reads the first monthly 'CATEGORY <month> <month> ...' table (units columns only). The
    first month in the header is the release month; the other columns (year-ago, previous
    month, ...) are kept as fall-back values. Raises if the table is not found."""
    lines = []
    for txt in _pdf_pages(data, max_pages=7):
        lines += txt.split("\n")
    for i, line in enumerate(lines):
        if not re.match(r"\s*CATEGORY\b", line, re.I) or _FADA_SKIP.search(line):
            continue
        toks = _month_tokens((lines[i - 1] if i else "") + " " + line)
        if not toks:
            continue
        rows: dict = {}
        for r in lines[i + 1:i + 40]:
            parts = r.split()
            if not parts:
                continue
            lab = _FADA_LABELS.get(parts[0].upper())
            if lab and lab not in rows:
                ints = [int(p.replace(",", "")) for p in parts[1:] if _INT_RE.match(p)]
                if len(ints) >= len(toks):
                    rows[lab] = dict(zip(toks, ints))
            if parts[0].upper() == "TOTAL":
                break
        if {"2w", "3w", "pv", "cv", "total"} <= set(rows):
            if toks[0] != max(toks):
                raise ValueError("FADA: header month order unexpected: " + line[:80])
            return toks[0], rows
    raise ValueError("FADA: monthly CATEGORY table not found / columns changed")


def _fada_check(lead: int, rows: dict) -> None:
    parts = sum(rows[k][lead] for k in ("2w", "3w", "pv", "cv", "trac") if k in rows)
    tot = rows["total"][lead]
    if not (0.97 <= parts / tot <= 1.001 + 0.01):
        raise ValueError(f"FADA {iso(lead)}: segments sum {parts} vs total {tot}")
    if not (200_000 < tot < 6_000_000):
        raise ValueError(f"FADA {iso(lead)}: implausible total {tot}")


def fada_merge(parsed: list, fill: bool = True) -> dict:
    """[(lead_ts, rows)] -> {cat: pts}.

    A month's own-release figure is replaced by the year-ago column of the following year's release (FADA restates
    upward as more RTOs are covered, and its published YoY growth is computed on the restated base). Other columns
    (previous month, two years ago) only fill gaps, and only when fill=True."""
    import datetime as _dt

    got: dict = {}
    for lead, rows in sorted(parsed, key=lambda r: r[0]):
        d = _dt.datetime.fromtimestamp(lead, _dt.timezone.utc)
        ya = month_ts(d.year - 1, d.month)
        for cat, cols in rows.items():
            dd = got.setdefault(cat, {})
            for t, v in cols.items():
                if t == lead or t == ya:
                    dd[t] = v
                elif fill and t not in dd:
                    dd[t] = v
    return {c: clean_pts(d.items()) for c, d in got.items()}


@lru_cache(maxsize=1)
def _fada_live() -> dict:
    """{cat: pts} from the most recent releases on fada.in (release month and its restated year-ago month)."""
    from bs4 import BeautifulSoup
    from .base import Http

    http = Http()
    soup = BeautifulSoup(http.text(FADA_LIST.format(p=1)), "lxml")
    urls = [a["href"] for a in soup.find_all("a", href=True)
            if "press-release/" in a["href"] and re.search(r"Vehicle Retail Data|Registration Data|Retail Sales", a["href"], re.I)]
    if not urls:
        raise ValueError("FADA: no retail-data releases listed")
    parsed = []
    for u in urls[:4]:
        time.sleep(1)
        lead, rows = fada_parse(http.bytes(FADA_BASE + quote(u, safe="/%")))
        _fada_check(lead, rows)
        parsed.append((lead, rows))
    return fada_merge(parsed, fill=False)


def _fada_series(cat: str) -> list:
    live = _fada_live().get(cat, [])
    pts = merge_pts(load_seed(f"fada_{cat}"), live)
    if len(pts) < 24:
        raise ValueError(f"FADA {cat}: only {len(pts)} points (seed missing?)")
    return pts


def _check_recent(pts: list, max_age_days: int, what: str) -> None:
    import datetime as _dt
    age = (_dt.datetime.now(_dt.timezone.utc).timestamp() - pts[-1][0]) / 86400
    if age > max_age_days:
        raise ValueError(f"{what}: latest period {iso(pts[-1][0])} is stale")


_FADA_SRC = "FADA (Federation of Automobile Dealers Associations), monthly vehicle retail data press releases (VAHAN-based)"
_FADA_URL = "https://fada.in/press-release-list.php"
_FADA_NOTE = (
    "Values are VAHAN-based registrations ('retail') collated by FADA for the RTOs it can access; "
    "coverage of states changed over time (e.g. Telangana, Lakshadweep and some others were missing early on, "
    "Kerala added Dec 2019, Wheeled construction equipment added to the total in 2026), and FADA restates "
    "earlier months (by up to ~5-8 per cent in 2025-26) in later releases. To keep growth rates like-for-like, each month's own-release figure is "
    "replaced by the restated year-ago figure printed in the following year's release when one exists, so the latest 12 months are as first "
    "released and are likely to be revised up. The 'All vehicles' total includes tractors from Jan 2020 only."
)


@register("fada-vehicle-retail-total", section=SECTION, name="Vehicle Retail Registrations - All Segments (FADA)")
def fada_total(http):
    pts = _fada_series("total")
    pts = [p for p in pts if p[0] >= ts(2020, 1, 1)]  # tractors included in total from Jan 2020
    _check_recent(pts, 75, "FADA total")
    s = series("total", "All vehicles (retail registrations)", "Units", pts)
    return record(
        "fada-vehicle-retail-total", title="Vehicle Retail Registrations - All Segments (FADA)",
        description=(
            "Monthly retail registrations (units) of all new vehicles in India - two-wheelers, three-wheelers, passenger "
            "vehicles, commercial vehicles and tractors - as compiled by the dealers' federation FADA from VAHAN. "
            "Unlike manufacturers' dispatches (SIAM), this is sales to end customers, so it is a cleaner read of "
            "consumer and fleet demand and is used in activity monitors as a timely proxy for discretionary spending, "
            "rural demand and credit conditions. FADA publishes in the first week after month-end."),
        frequency="M", views=[view("Monthly registrations", s), yoy_view(s)],
        source=_FADA_SRC, source_url=_FADA_URL, notes=_FADA_NOTE,
        qna=[
            {"question": "What does this series measure?",
             "answer": "Number of new vehicles registered at regional transport offices in a month (all five segments together), as reported in FADA's monthly press release.^It counts registrations (retail to the end customer) and not factory dispatches to dealers, so it avoids channel stuffing and inventory swings that affect dispatch numbers."},
            {"question": "How should I read it?",
             "answer": "Registrations are strongly seasonal: peaks around Navratri/Diwali (Oct-Nov), the March financial-year end and wedding/harvest months, and troughs in monsoon and Shraadh periods. Compare with the same month a year earlier (YoY view) and watch festival timing shifts between months.^Two-wheelers are roughly 70 per cent of units, so the total mostly tracks rural and low-income demand; passenger vehicles dominate in value."},
            {"question": "What are the caveats and revisions?",
             "answer": "FADA's coverage of RTOs was incomplete in 2019-2022 and the latest month is collated a few days after month-end, so FADA restates earlier months upward in later releases (by roughly 5-8 per cent for Sep 2025, for instance). This series uses the restated year-ago figure from the next year's release where available, so YoY growth matches FADA's own published growth rates; the latest 12 months are as first released.^Tax/regulatory changes (BS-VI, GST changes, subsidy rules) and festival calendars cause large base effects, so single-month growth rates can mislead."},
        ])


@register("fada-vehicle-retail-segments", section=SECTION, name="Vehicle Retail Registrations by Segment (FADA)")
def fada_segments(http):
    ss, ys = [], []
    for cat, name in FADA_SERIES[1:]:
        pts = _fada_series(cat)
        if cat == "trac":
            pts = [p for p in pts if p[0] >= ts(2020, 1, 1)]
        _check_recent(pts, 75, f"FADA {cat}")
        s = series(cat, name, "Units", pts)
        ss.append(s)
        y = yoy_view(s)
        ys.append(series(cat, name, "Per cent", y["series"][0]["pts"]))
    return record(
        "fada-vehicle-retail-segments", title="Vehicle Retail Registrations by Segment (FADA)",
        description=(
            "Monthly retail registrations (units) by vehicle segment - two-wheelers, passenger vehicles, commercial "
            "vehicles, three-wheelers and tractors - from FADA's monthly releases (VAHAN data). Segments read different "
            "parts of the economy: two-wheelers and tractors the rural and low-income consumer, passenger vehicles "
            "urban discretionary demand and credit, commercial vehicles freight/infrastructure activity, three-wheelers "
            "last-mile transport and the e-rickshaw boom."),
        frequency="M",
        views=[view("Registrations", *ss), view("YoY % change", *ys, vid="yoy")],
        source=_FADA_SRC, source_url=_FADA_URL, notes=_FADA_NOTE,
        qna=[
            {"question": "What is in each segment?",
             "answer": "2W: motorcycles, scooters and mopeds (including electric). 3W: goods and passenger three-wheelers including e-rickshaws. PV: cars, utility vehicles and vans. CV: light, medium and heavy commercial vehicles and buses. Tractors: agricultural tractors (series starts January 2020 when FADA began reporting them)."},
            {"question": "How is this different from SIAM sales on this dashboard?",
             "answer": "SIAM figures are manufacturer dispatches to dealers; FADA numbers are registrations to end customers. Over a few months they converge, but month to month dealer stocking (e.g. ahead of festivals or price changes) can make dispatch and retail diverge sharply - FADA's PV inventory-days commentary is the usual reconciling story."},
            {"question": "Caveats?",
             "answer": "Months before 2022 had incomplete state coverage in FADA's releases, and the most recent month is preliminary and later releases restate earlier months upward (this series uses the restated year-ago figure from the following year's release where available, and fills gaps from the other comparison columns).^Gaps may exist where FADA published no release for a month and no later release carried it."},
        ])


# ============================================================ AAI air traffic

AAI_PAGE = "https://www.aai.aero/en/business-opportunities/aai-traffic-news"
_AAI_HDR = re.compile(r"AIR\s+TRAFFIC\s+REPORT\s*[–—\-]*\s*([A-Za-z]+)\s*,?\s*(\d{4})", re.I)
_AAI_ROW = re.compile(r"\b(International|Domestic|Total)\s+([\d,.\s\-]+)$")
_AAI_GROUPS = ["mov", "pax", "frt"]  # aircraft movements (thousand), passengers (million), freight (thousand tonnes)


def _aai_split(rest: str):
    """'33.30 39.84 -16.4' (possibly with stray spaces inside numbers) -> (current, year_ago) or None."""
    rest = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", rest).strip()
    toks = rest.split()
    if len(toks) < 3:
        return None
    chg = toks[-1]
    mid = toks[:-1]
    chg_v = float(chg.replace(",", "")) if re.match(r"^-?[\d,]+\.\d+$", chg) else None
    cands = []
    for k in range(1, len(mid)):
        a, b = "".join(mid[:k]).replace(",", ""), "".join(mid[k:]).replace(",", "")
        try:
            fa, fb = float(a), float(b)
        except ValueError:
            continue
        if fb == 0:
            cands.append((fa, fb))
        elif chg_v is None or abs((fa / fb - 1) * 100 - chg_v) < 0.25:
            cands.append((fa, fb))
    if len(cands) == 1 or (len(cands) > 1 and len(mid) == 2):
        return cands[0]
    return None


def aai_parse(data: bytes):
    """AAI 'Traffic News Summary' PDF -> (month_ts, {key: (current, year_ago)}), keys like 'frt_dom'.

    Units: movements thousand, passengers million, freight thousand tonnes. Raises on an unexpected layout."""
    pages = list(_pdf_pages(data, max_pages=1))
    text = pages[0] if pages else ""
    m = _AAI_HDR.search(text)
    if not m:
        raise ValueError("AAI: report header not found")
    mon = _MONTHS.get(m.group(1).upper()[:3])
    if not mon:
        raise ValueError("AAI: bad month " + m.group(1))
    month = month_ts(int(m.group(2)), mon)
    vals = []
    for line in text.split("\n"):
        mm = _AAI_ROW.search(line)
        if mm:
            vals.append((mm.group(1).lower(), _aai_split(mm.group(2))))
        if len(vals) == 9:
            break
    if len(vals) < 9 or any(v is None for _, v in vals):
        raise ValueError(f"AAI {iso(month)}: could not read the 9 monthly rows (got {len(vals)})")
    out = {}
    for gi, g in enumerate(_AAI_GROUPS):
        trio = vals[gi * 3:gi * 3 + 3]
        if [t for t, _ in trio] != ["international", "domestic", "total"]:
            raise ValueError(f"AAI {iso(month)}: row order unexpected")
        i, d, t = [v for _, v in trio]
        for col in (0, 1):
            if abs(i[col] + d[col] - t[col]) > 0.02 * max(t[col], 1) + 0.02:
                raise ValueError(f"AAI {iso(month)} {g}: intl+dom != total")
        out[f"{g}_intl"], out[f"{g}_dom"], out[f"{g}_total"] = i, d, t
    return month, out


def _aai_merge(parsed: list) -> dict:
    """[(month, {key:(cur, prev)})] -> {key: pts}. Own-release value first; the year-ago column of the
    following year's release (revised) then overrides it."""
    import datetime as _dt

    lead: dict = {}
    prev: dict = {}
    for month, rows in parsed:
        d = _dt.datetime.fromtimestamp(month, _dt.timezone.utc)
        t_prev = month_ts(d.year - 1, d.month)
        for k, (cur, pv) in rows.items():
            lead.setdefault(k, {})[month] = cur
            prev.setdefault(k, {})[t_prev] = pv
    out = {}
    for k in lead:
        merged = dict(lead[k])
        merged.update(prev.get(k, {}))
        out[k] = clean_pts(merged.items())
    return out


@lru_cache(maxsize=1)
def _aai_live() -> dict:
    """Parse the AAI summary PDFs of the current and previous calendar year (live part)."""
    import datetime as _dt
    from .base import Http

    http = Http()
    yr = _dt.datetime.now(_dt.timezone.utc).year
    urls = []
    for y in (yr, yr - 1):
        html = http.text(AAI_PAGE, params={"field_news_date_value[value][year]": y})
        for u in re.findall(r"(https://www\.aai\.aero/sites/default/files/traffic-news/[^\"'<>]+?\.pdf)", html, re.I):
            n = u.rsplit("/", 1)[1]
            if not re.search(r"annex|anex", n, re.I) and u not in urls:
                urls.append(u)
        time.sleep(1)
    if not urls:
        raise ValueError("AAI: no traffic summary PDFs listed")
    parsed = []
    for u in urls[:11]:
        time.sleep(1)
        try:
            parsed.append(aai_parse(http.bytes(u.replace(" ", "%20"))))
        except ValueError:
            continue
    if len(parsed) < 3:
        raise ValueError("AAI: could not parse the recent summary PDFs")
    return _aai_merge(parsed)


def _aai_series(key: str) -> list:
    pts = merge_pts(load_seed(f"aai_{key}"), _aai_live().get(key, []))
    if len(pts) < 36:
        raise ValueError(f"AAI {key}: only {len(pts)} points (seed missing?)")
    return pts


_AAI_NOTE = (
    "Taken from AAI's monthly 'Traffic News Summary' (all operational Indian airports). Each month's figure is the "
    "provisional number from its own release, replaced by the revised year-ago figure printed in the following year's "
    "release once available; the latest 12 months are therefore provisional. A few months may be missing where AAI's "
    "summary could not be read."
)


def _yoy_series(ss: list) -> list:
    out = []
    for s in ss:
        v = yoy_view(s)
        if v:
            out.append(series(s["id"], s["name"], "Per cent", v["series"][0]["pts"]))
    return out


@register("air-cargo-traffic-aai", section=SECTION, name="Air Cargo Traffic at Indian Airports (AAI)")
def aai_cargo(http):
    keys = [("frt_total", "Total freight"), ("frt_dom", "Domestic freight"), ("frt_intl", "International freight")]
    ss = [series(k, n, "Thousand tonnes", _aai_series(k)) for k, n in keys]
    for s in ss:
        _check_recent(s["pts"], 100, "AAI freight")
        if not all(3 < p[1] < 600 for p in s["pts"] if p[0] >= ts(2021, 1, 1)):
            raise ValueError("AAI freight: implausible value")
    return record(
        "air-cargo-traffic-aai", title="Air Cargo Traffic at Indian Airports (AAI)",
        description=(
            "Freight (cargo and mail) handled at all Indian airports each month, in thousand tonnes, split into domestic "
            "and international, from the Airports Authority of India. Air cargo carries high-value, time-sensitive goods "
            "(electronics, pharmaceuticals, e-commerce parcels), so it tracks merchandise trade values and e-commerce "
            "better than volumes of bulk freight, and is available about 2-3 weeks after the month ends."),
        frequency="M", views=[view("Thousand tonnes", *ss), view("YoY % change", *_yoy_series(ss), vid="yoy")],
        source="Airports Authority of India (AAI), Traffic News", source_url=AAI_PAGE, notes=_AAI_NOTE,
        qna=[
            {"question": "What does this measure?",
             "answer": "Tonnes of freight (including mail) loaded and unloaded at all operational Indian airports in the month, in thousands of tonnes. International cargo is the larger share (about two thirds); domestic cargo is mostly e-commerce, express and perishables."},
            {"question": "How should I read it?",
             "answer": "Use year-on-year growth rather than levels because of seasonality (festive e-commerce, pharma exports). Weakness in international cargo has tended to coincide with softer merchandise exports by value; the large swings in 2020-21 reflect the pandemic collapse of belly-hold capacity on passenger flights.^A domestic tonne is counted at both origin and destination airport, so domestic freight is not comparable with rail or port tonnage."},
            {"question": "What are the caveats?",
             "answer": "AAI compiles the data from airports and states that it is subject to revision; this series replaces each month with the revised year-ago figure from the following year's release. Some months may be absent where AAI's summary could not be read."},
        ])


@register("aircraft-movements-aai", section=SECTION, name="Aircraft Movements at Indian Airports (AAI)")
def aai_movements(http):
    keys = [("mov_total", "Total movements"), ("mov_dom", "Domestic movements"), ("mov_intl", "International movements")]
    ss = [series(k, n, "Thousand", _aai_series(k)) for k, n in keys]
    for s in ss:
        _check_recent(s["pts"], 100, "AAI movements")
    return record(
        "aircraft-movements-aai", title="Aircraft Movements at Indian Airports (AAI)",
        description=(
            "Landings and take-offs of commercial aircraft (excluding general aviation) at all Indian airports, in "
            "thousands per month, domestic and international, from AAI. Movements follow passenger demand but also fleet "
            "constraints and airline capacity decisions, so set against passenger traffic they show load-factor and "
            "capacity trends."),
        frequency="M", views=[view("Thousand movements", *ss), view("YoY % change", *_yoy_series(ss), vid="yoy")],
        source="Airports Authority of India (AAI), Traffic News", source_url=AAI_PAGE, notes=_AAI_NOTE,
        qna=[
            {"question": "What is counted?",
             "answer": "Aircraft movements (each landing or take-off) of commercial aircraft at all operational airports, excluding general aviation (private, charter and training flights), which AAI reports separately."},
            {"question": "How do I use it for nowcasting?",
             "answer": "Together with the Air Passenger Traffic pages it gives passengers per movement: falling movements with stable passengers mean higher load factors; falling passengers with stable movements mean slack demand.^Capacity shocks (grounded aircraft, engine issues, airline failures) can reduce movements without any change in demand."},
            {"question": "Revisions and caveats?",
             "answer": "AAI data is provisional when first published and revised in the following year's release; the series uses the revised year-ago values where available. Totals are domestic plus international and exclude general aviation."},
        ])


# ============================================================ Major ports: container traffic (TEU)

SHIPMIN_LIST_URLS = [
    "https://shipmin.gov.in/division/transport-research",
    "https://shipmin.gov.in/division/archieve/transport-research",
]
PROBE_URLS.extend(SHIPMIN_LIST_URLS)
_PORT_HDR = re.compile(r"MAJOR\s+PORTS?\s+DURING\s+(?:AND\s+UP\s*TO\s+)?([A-Z]{3,9})\s*,?\s*(\d{4})", re.I)
_NUM_CELL = re.compile(r"^-?[\d,]+(?:\.\d+)?$")


def port_parse(data: bytes):
    """Ministry of Ports 'Cargo handled at Major Ports' monthly PDF -> (month_ts, teu_current, teu_year_ago) in
    thousand TEU. Handles the old layout (thousand TEU, rounded) and the 2024+ layout (TEU)."""
    import logging
    import pdfplumber

    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    month = None
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        head = (pdf.pages[0].extract_text() or "").replace("\n", " ")
        m = _PORT_HDR.search(head)
        if not m or m.group(1).upper()[:3] not in _MONTHS:
            raise ValueError("ports: report header not found")
        month = month_ts(int(m.group(2)), _MONTHS[m.group(1).upper()[:3]])
        for pg in pdf.pages:
            for tb in pg.extract_tables():
                for row in tb:
                    cells = [str(c).strip() if c is not None else "" for c in row]
                    lab = next((c for c in cells if re.search(r"TEU", c, re.I)), None)
                    if lab is None:
                        continue
                    nums = [float(c.replace(",", "")) for c in cells if c and _NUM_CELL.match(c)]
                    if len(nums) < 3:
                        continue
                    thousands = bool(re.search(r"000", lab))
                    prev, cur = nums[0], nums[1]
                    share_layout = thousands and len(nums) >= 4 and nums[1] < 50 < nums[2]  # 2022 reports: value, share, value, share
                    if share_layout:
                        cur = nums[2]
                    if not thousands:
                        prev, cur = prev / 1000.0, cur / 1000.0
                    if not (300 < prev < 2500 and 300 < cur < 2500):
                        raise ValueError(f"ports {iso(month)}: implausible TEU {prev}, {cur}")
                    if thousands and len(nums) == 3 and not share_layout:
                        if abs((cur / prev - 1) * 100 - nums[2]) > 0.6:
                            raise ValueError(f"ports {iso(month)}: TEU growth check failed")
                    if not thousands and len(nums) >= 4 and abs((cur / prev - 1) * 100 - nums[3]) > 0.15:
                        raise ValueError(f"ports {iso(month)}: TEU growth check failed")
                    return month, round(cur, 3), round(prev, 3)
    raise ValueError(f"ports {iso(month)}: TEU row not found")


def _port_merge(parsed: list) -> list:
    import datetime as _dt

    lead, prev = {}, {}
    for month, cur, pv in parsed:
        d = _dt.datetime.fromtimestamp(month, _dt.timezone.utc)
        lead[month] = cur
        prev[month_ts(d.year - 1, d.month)] = pv
    merged = dict(lead)
    merged.update(prev)
    return clean_pts(merged.items())


@lru_cache(maxsize=1)
def _port_live() -> list:
    from .base import Http

    http = Http()
    urls = []
    html = http.text(SHIPMIN_LIST_URLS[0])
    for u in re.findall(r"(https://shipmin\.gov\.in/sites/default/files/[^\"'<>]+?\.pdf)", html):
        if re.search(r"major", u, re.I) and not re.search(r"non|hindi", u, re.I) and u not in urls:
            urls.append(u)
    if not urls:
        raise ValueError("ports: no Major Ports reports listed")
    parsed = []
    for u in urls[:4]:
        time.sleep(1)
        parsed.append(port_parse(http.bytes(u)))
    return _port_merge(parsed)


@register("port-container-traffic-teu", section=SECTION, name="Container Traffic at Major Ports (TEU)")
def port_teu(http):
    pts = merge_pts(load_seed("port_teu"), _port_live())
    if len(pts) < 36:
        raise ValueError(f"ports TEU: only {len(pts)} points (seed missing?)")
    _check_recent(pts, 100, "major ports TEU")
    s = series("teu", "Container traffic at 12 Major Ports", "Thousand TEU", pts)
    return record(
        "port-container-traffic-teu", title="Container Traffic at Major Ports (TEU)",
        description=(
            "Containers handled by the 12 Major (government-owned) ports each month, in thousand twenty-foot equivalent units "
            "(TEU), from the Ministry of Ports, Shipping and Waterways' monthly cargo reports. Container volumes track "
            "manufactured goods trade (exports of engineering goods, textiles, chemicals and imports of electronics and "
            "machinery) more closely than bulk cargo, so this complements the Port Cargo Traffic page, which is total tonnes "
            "dominated by coal, crude oil and iron ore."),
        frequency="M", views=[view("Thousand TEU", s), yoy_view(s)],
        source="Ministry of Ports, Shipping and Waterways, Cargo handled at Major Ports (monthly)",
        source_url="https://shipmin.gov.in/division/transport-research",
        notes=("12 Major Ports only (Mundra, Pipavav and other non-major ports are excluded, so this is not all-India container traffic; "
               "JNPA and Deendayal are the largest). Latest month is an advance estimate. Each month's figure is the one in its own report, "
               "replaced by the revised year-ago figure printed in the following year's report. Reports before 2024 give TEU rounded to the "
               "nearest thousand."),
        qna=[
            {"question": "What does this series measure?",
             "answer": "Total containers handled (loaded and discharged, imports, exports, transhipment and coastal) at India's 12 Major Ports, expressed in TEU - a 20-foot container is 1 TEU, a 40-foot container 2. The unit here is thousand TEU."},
            {"question": "How should I read it?",
             "answer": "It is a physical indicator of merchandise trade in manufactured goods. Use year-on-year growth: seasonality is moderate (peaks around Oct-Mar, with export shipping ahead of the holiday season), and shipping disruptions (Red Sea diversions, port congestion) can shift volumes between months or between major and non-major ports.^Compare with the exports and imports values on the Trade pages: container volume growth above value growth suggests falling unit values."},
            {"question": "What are the caveats?",
             "answer": "Coverage is the 12 Major Ports only; private non-major ports (notably Mundra) handle a large and growing share of containers, so share shifts can move this series independently of trade. The latest month is an 'advance estimate' compared with provisional estimates of the previous year, and the Ministry revises figures in later reports.^Pre-2024 reports gave TEU in thousands (rounded), so earlier history is slightly coarser."},
        ])


# ============================================================ Indian Railways freight loading (PIB releases)

PIB_ALLREL = "https://www.pib.gov.in/allRel.aspx"
PIB_RELEASE = "https://www.pib.gov.in/PressReleasePage.aspx?PRID={prid}"
PIB_RAILWAYS_ID = "23"  # 'Ministry of Railways' in the allRel.aspx ministry drop-down
PROBE_URLS.extend([PIB_ALLREL, PIB_RELEASE.format(prid=2322673)])

_MONTH_NAMES = "January|February|March|April|May|June|July|August|September|October|November|December"
_MN = {n.lower(): i + 1 for i, n in enumerate(_MONTH_NAMES.split("|"))}
_QTY = r"([\d,]+(?:\.\d+)?)\s*(?:million\s+tonnes|MT|mt|Million\s+Tonnes|Million\s+tonnes)"
_MY = r"(" + _MONTH_NAMES + r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)[a-z]*\s*[,’‘']?\s*(\d{4}|\d{2})\b"
_RE_LEAD = re.compile(
    r"(?:loading|loaded|loads|loaded a total of|achieves?|achieved|recorded|records)\s+(?:of\s+|a\s+)?(?:freight\s+)?(?:loading\s+)?(?:of\s+)?"
    + _QTY + r"(?:\s+of\s+freight)?\s*,?\s*(?:in|during|for|\(for)\s+(?:the\s+month\s+of\s+)?" + _MY, re.I)
_RE_LEAD_B = re.compile(
    _QTY + r"\s+(?:of\s+)?(?:freight\s+)?(?:loading|freight)\s*,?\s*(?:in|during|for)\s+(?:the\s+month\s+of\s+)?" + _MY, re.I)
_RE_LEAD_C = re.compile(_MY + r"[^.0-9]{0,80}?(?:loading|loaded)\s+(?:of\s+)?" + _QTY, re.I)
_RE_PREV = re.compile(
    r"(?:compared\s+(?:to|with)|as\s+against|against|from|vs\.?)\s+(?:the\s+)?(?:corresponding\s+period\s+of\s+)?(?:" + _QTY + r")?", re.I)


def _mon_idx(s: str) -> int:
    k = s.lower()[:3]
    return {n[:3].lower(): v for n, v in _MN.items()}[k]


def _yr(s: str) -> int:
    y = int(s)
    return y + 2000 if y < 100 else y


def rail_parse(text: str):
    """PIB Railways release text -> list of (month_ts, million_tonnes) for the month named in the release.

    Returns the current month and, when the release gives the same month last year with its
    tonnage, that year-ago month too. Empty list if the release is not a monthly loading report."""
    x = re.sub(r"\s+", " ", text)
    out = {}
    hits = [(m, m.group(1), m.group(2), m.group(3)) for m in list(_RE_LEAD.finditer(x)) + list(_RE_LEAD_B.finditer(x))]
    hits += [(m, m.group(3), m.group(1), m.group(2)) for m in _RE_LEAD_C.finditer(x)]
    for m, q_s, mon_s, yr_s in hits:
        qty = float(q_s.replace(",", ""))
        mon, yr = _mon_idx(mon_s), _yr(yr_s)
        if not (60 < qty < 200):
            continue
        t = month_ts(yr, mon)
        out.setdefault(t, qty)
        tail = x[m.end():m.end() + 160]
        mm = re.search(r"(?:compared\s+(?:to|with)|as\s+against|against|over)[^0-9]{0,60}" + _QTY + r"[^.]{0,60}?(?:in\s+|during\s+)?(?:" + _MY + r")?", tail, re.I)
        if mm:
            pq = float(mm.group(1).replace(",", ""))
            if 60 < pq < 200 and abs((qty / pq - 1) * 100) < 40:
                if mm.group(2):
                    out.setdefault(month_ts(_yr(mm.group(3)), _mon_idx(mm.group(2))), pq)
                else:
                    out.setdefault(month_ts(yr - 1, mon), pq)
    return sorted(out.items())


def _pib_listing(http, month: int, year: int) -> list:
    """[(prid, title)] of Ministry of Railways releases in a month (ASP.NET post-back on allRel.aspx)."""
    from bs4 import BeautifulSoup

    s = BeautifulSoup(http.get(PIB_ALLREL, params={"reg": "3", "lang": "1"}).text, "lxml")
    d = {i["name"]: i.get("value", "") for i in s.find_all("input", type="hidden") if i.get("name")}
    d.update({
        "ctl00$ContentPlaceHolder1$ddlMinistry": PIB_RAILWAYS_ID, "ctl00$ContentPlaceHolder1$ddlday": "0",
        "ctl00$ContentPlaceHolder1$ddlMonth": str(month), "ctl00$ContentPlaceHolder1$ddlYear": str(year),
        "__EVENTTARGET": "ctl00$ContentPlaceHolder1$ddlMinistry",
    })
    s2 = BeautifulSoup(http.post(PIB_ALLREL + "?reg=3&lang=1", data=d).text, "lxml")
    out = []
    for a in s2.find_all("a", href=True):
        mm = re.search(r"PRID=(\d+)", a["href"])
        if mm:
            out.append((mm.group(1), a.get_text(" ", strip=True)))
    if not out:
        raise ValueError("PIB listing returned no releases (layout change?)")
    return out


def _pib_text(http, prid: str) -> str:
    from bs4 import BeautifulSoup

    x = re.sub(r"\s+", " ", BeautifulSoup(http.text(PIB_RELEASE.format(prid=prid)), "lxml").get_text(" ", strip=True))
    i = x.find("Posted On")
    return x[i:i + 4000] if i >= 0 else ""


@lru_cache(maxsize=1)
def _rail_live() -> list:
    import datetime as _dt
    from .base import Http

    http = Http()
    now = _dt.datetime.now(_dt.timezone.utc)
    got: dict = {}
    for back in (0, 1, 2):
        y, m = now.year, now.month - back
        while m < 1:
            y, m = y - 1, m + 12
        time.sleep(1)
        for prid, title in _pib_listing(http, m, y):
            if not re.search(r"(?i)freight|loading", title) or re.search(r"(?i)oxygen|corridor|crore|revenue|till |first (two|four)", title):
                continue
            time.sleep(1)
            for t, v in rail_parse(_pib_text(http, prid)):
                got.setdefault(t, v)
    return clean_pts(got.items())


@register("rail-freight-loading", section=SECTION, name="Indian Railways Freight Loading (Monthly)")
def rail_loading(http):
    pts = merge_pts(load_seed("rail_loading"), _rail_live())
    if len(pts) < 15:
        raise ValueError(f"rail loading: only {len(pts)} points (seed missing?)")
    for t, v in pts:
        if not 60 < v < 200:
            raise ValueError(f"rail loading: implausible {iso(t)} {v}")
    _check_recent(pts, 200, "rail freight loading")
    s = series("loading", "Freight loading (originating)", "Million tonnes", pts)
    return record(
        "rail-freight-loading", title="Indian Railways Freight Loading (Monthly)",
        description=(
            "Total revenue-earning freight originating on Indian Railways each month, in million tonnes, as announced by the "
            "Ministry of Railways in monthly press releases carried by PIB. Rail moves coal, iron ore, steel, cement, foodgrains, "
            "fertiliser, containers and petroleum, so loading is a timely volume read on mining, power, construction and "
            "manufacturing activity - available about two weeks after month-end, months before the IIP. "
            "It is a volume series (not revenue) and complements the older Railway Freight Traffic - Revenue page, which stops in Dec 2024."),
        frequency="M", views=[view("Million tonnes", s), yoy_view(s)],
        source="Ministry of Railways, monthly press releases (via PIB)", source_url="https://www.pib.gov.in/allRel.aspx",
        notes=("Not every month has a monthly press release, so the series has gaps (no values are filled or interpolated). "
               "Where a release states the same month of the previous year, that year-ago figure is also used. "
               "Figures are those announced in the releases and may differ slightly from later Railway Board statistics."),
        qna=[
            {"question": "What does freight loading measure?",
             "answer": "Million tonnes of revenue-earning goods loaded (originating) on Indian Railways in the month. Coal is about half of the total, followed by iron ore, cement and clinker, foodgrains, fertiliser, steel, containers and POL."},
            {"question": "How should I read it?",
             "answer": "Use year-on-year growth: loading is seasonal (monsoon months are weaker for coal and iron ore; Q4 and March are strong). Coal loading tracks power demand and mine output; containers, steel and cement track construction and manufacturing demand.^Rail loading has been rising in structural steps (dedicated freight corridors), so growth rates run above those of industrial output."},
            {"question": "What are the caveats?",
             "answer": "The data come from monthly press releases whose wording and commodity detail vary, and some months were never announced, so the series has gaps. Loading counts tonnes at origin only, and revisions in Railway Board's final statistics can differ slightly from the figures announced."},
        ])


supersedes("revenue-earnings-railway-freight-traffic-major-commodities", "rail-freight-loading")
