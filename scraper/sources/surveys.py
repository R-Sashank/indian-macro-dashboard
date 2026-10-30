"""RBI surveys: Urban Consumer Confidence, Households' Inflation Expectations, Survey of
Professional Forecasters, Industrial Outlook, Services & Infrastructure Outlook.

All come from RBI press-release PDFs (rbi.org.in listing page -> PublicationsView page -> PDF on
rbidocs.rbi.org.in). The press-release tables carry only the last ~5-7 rounds, so the full
history was backfilled once from every release (seed/*.json, see `_backfill_*` helpers at the
bottom) and each builder merges the seed with a live parse of the newest releases.
"""

from __future__ import annotations

import io
import re
import time
from functools import lru_cache

from bs4 import BeautifulSoup

from .base import (
    clean_pts, iso, load_seed, merge_pts, month_ts, quarter_end_ts, record, register, save_seed, series, view,
    yoy_view,
)

SECTION = "surveys-sentiment"
RBI = "https://www.rbi.org.in/scripts/"

PROBE_URLS = [
    RBI + "BimonthlyPublications.aspx?head=Urban+Consumer+Confidence+Survey+-+Bi-monthly",
    RBI + "BimonthlyPublications.aspx?head=Inflation+Expectations+Survey+of+Households+-+Bi-monthly",
    RBI + "BimonthlyPublications.aspx?head=Survey+of+Professional+Forecasters+-+Bi-monthly",
    RBI + "QuarterlyPublications.aspx?head=Quarterly+Industrial+Outlook+Survey",
    RBI + "QuarterlyPublications.aspx?head=Services+and+Infrastructure+Outlook+Survey",
    RBI + "PublicationsView.aspx?id=24093",
    "https://rbidocs.rbi.org.in/rdocs/Publications/PDFs/SPF10207126LSSS0000OPPPP.PDF",
]

MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
NUM = r"-?\d+(?:\.\d+)?"


# ------------------------------------------------------------------ fetching

_last = [0.0]


def _polite():
    wait = 1.0 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()


@lru_cache(maxsize=None)
def _listing(http, kind: str, head: str) -> tuple:
    """Newest-first [(pub_id, title, date_text)] from the first page of an RBI publications head."""
    _polite()
    s = BeautifulSoup(http.text(f"{RBI}{kind}Publications.aspx?head={head}"), "lxml")
    out = []
    for tr in s.find_all("tr"):
        a = tr.find("a", href=re.compile("PublicationsView"))
        if not a:
            continue
        prev = tr.find_previous("tr")
        out.append((a["href"].split("id=")[-1], a.get_text(" ", strip=True), prev.get_text(" ", strip=True)[:14] if prev else ""))
    if not out:
        raise RuntimeError(f"no publications found on RBI listing {head!r}; layout changed?")
    return tuple(out)


@lru_cache(maxsize=None)
def _pdf_url(http, pub_id: str) -> str:
    _polite()
    s = BeautifulSoup(http.text(f"{RBI}PublicationsView.aspx?id={pub_id}"), "lxml")
    for a in s.find_all("a", href=True):
        if re.search(r"rdocs/Publications/PDFs/.*\.pdf$", a["href"], re.I):
            return a["href"]
    raise RuntimeError(f"no PDF link on RBI publication {pub_id}")


@lru_cache(maxsize=None)
def _pages(http, pub_id: str) -> tuple:
    import pdfplumber

    url = _pdf_url(http, pub_id)
    for attempt in range(4):
        _polite()
        data = http.bytes(url)
        if data[:5] == b"%PDF-":
            break
        time.sleep(5 * (attempt + 1))  # RBI sometimes answers with an HTML error page when hit quickly
    else:
        raise RuntimeError(f"RBI PDF {url} did not return a PDF")
    with pdfplumber.open(io.BytesIO(data)) as p:
        return tuple((pg.extract_text() or "") for pg in p.pages)


def _fulltext(http, pub_id) -> str:
    return "\n".join(_pages(http, pub_id))


# ------------------------------------------------------------------ parsing

def _month_of(label: str):
    m = re.match(r"^([A-Za-z]{3,9})\W*(\d{2})$", label.strip())
    if not m or m.group(1)[:3].lower() not in MONTHS:
        raise ValueError(f"bad round label {label!r}")
    return 2000 + int(m.group(2)), MONTHS[m.group(1)[:3].lower()]


def _table_rows(text: str, title_re: str, ncols, label_re=r"[A-Za-z]{3,9}[-\s]\d{2}") -> dict:
    """Rows 'Mon-YY n n n ...' of the table whose 'Table N: <title>' line matches title_re."""
    lines = text.splitlines()
    idx = [i for i, l in enumerate(lines) if re.match(r"^Table\s*\d+\w*\s*[:.]", l) and re.search(title_re, l)]
    if not idx:
        return {}
    out = {}
    for l in lines[idx[0] + 1:]:
        if re.match(r"^Table\s*\d+", l):
            break
        m = re.match(rf"^({label_re})\s+((?:{NUM}\s*)+)$", l.strip())
        if m:
            vals = [float(x) for x in m.group(2).split()]
            if len(vals) != ncols:
                raise ValueError(f"row {l!r} has {len(vals)} numbers, expected {ncols}")
            out[_month_of(m.group(1).replace(" ", "-"))] = vals
    return out


def _mts(ym) -> int:
    return month_ts(ym[0], ym[1])


# ---- Urban Consumer Confidence Survey

UCCS_VARS = [
    ("econ", "General economic situation", r"on (the )?(General )?Economic (Situation|Conditions)"),
    ("employment", "Employment", r"on Employment"),
    ("price", "Price level", r"on Price Level\s*$"),
    ("income", "Income", r"on Income"),
    ("spending", "Spending", r"on Spending\s*$"),
]


def parse_uccs(text: str) -> dict:
    """{series_id: {ts: value}} for one release: CSI/FEI (2 rounds) and net responses (all rows)."""
    out: dict = {}
    # summary block: header with two round labels, then the 4-number Consumer Confidence Index line
    i = text.find("Summary based on Net Response")
    if i < 0:
        raise ValueError("UCCS summary block not found")
    block = text[i:i + 2500]
    hdr = re.search(r"((?:[A-Za-z]{3,5}-\d{2}\s+){1,2}Change\s+(?:[A-Za-z]{3,5}-\d{2}\s+)?[A-Za-z]{3,5}-\d{2}\s+[A-Za-z]{3,5}-\d{2}\s+Change)", block)
    if not hdr:
        raise ValueError("UCCS summary header not found")
    labels = re.findall(r"[A-Za-z]{3,5}-\d{2}", hdr.group(1))
    prev, cur = _month_of(labels[0]), _month_of(labels[1])
    four = rf"({NUM})[ ]+({NUM})[ ]+({NUM})[ ]+({NUM})"
    m = re.search(r"Consumer(?:\s+Confidence)?(?:\s+Index)?\s*" + four, block)
    if not m:
        raise ValueError("UCCS CSI/FEI line not found")
    c0, c1, f0, f1 = map(float, m.groups())
    out["csi"] = {_mts(prev): c0, _mts(cur): c1}
    out["fei"] = {_mts(prev): f0, _mts(cur): f1}
    for sid, _name, tre in UCCS_VARS:
        rows = _table_rows(text, tre, 8)
        if not rows:
            raise ValueError(f"UCCS table for {sid} not found")
        out[f"cur_{sid}"] = {_mts(k): v[3] for k, v in rows.items()}
        out[f"fut_{sid}"] = {_mts(k): v[7] for k, v in rows.items()}
    # cross-check: published index = 100 + average of the five net responses (rounding aside)
    for key, pre in (("csi", "cur_"), ("fei", "fut_")):
        for t, val in out[key].items():
            nets = [out[f"{pre}{v[0]}"].get(t) for v in UCCS_VARS]
            if None in nets:
                continue
            calc = 100 + sum(nets) / 5
            if abs(calc - val) > 1.0:
                raise ValueError(f"UCCS {key} {val} at {iso(t)} disagrees with net responses ({calc:.1f})")
    return out


# ---- Households' Inflation Expectations Survey

def parse_ies(text: str) -> dict:
    """Table 3 rows: Mean/SE/Median/SE for current perception, 3-month ahead and 1-year ahead."""
    lines = text.splitlines()
    idx = [i for i, l in enumerate(lines) if re.match(r"^Table\s*3\s*:", l) and "nflation" in l]
    if not idx:
        raise ValueError("IES Table 3 not found")
    out: dict = {k: {} for k in ("cur_med", "m3_med", "y1_med", "cur_mean", "m3_mean", "y1_mean")}
    n = 0
    for l in lines[idx[0] + 1:]:
        if re.match(r"^(Table\s*\d+|Note)", l):
            break
        m = re.match(rf"^([A-Za-z]{{3,9}}-\d{{2}})\s+((?:{NUM}\s*)+)$", l.strip())
        if not m:
            continue
        v = [float(x) for x in m.group(2).split()]
        if len(v) != 12:
            raise ValueError(f"IES row {l!r} has {len(v)} numbers")
        t = _mts(_month_of(m.group(1)))
        for k, j in (("cur_mean", 0), ("cur_med", 2), ("m3_mean", 4), ("m3_med", 6), ("y1_mean", 8), ("y1_med", 10)):
            out[k][t] = v[j]
        n += 1
    if not n:
        raise ValueError("IES Table 3 has no rows")
    return out


def _fold(parsed_list: list) -> dict:
    """Combine per-release dicts; later releases (listed last) win."""
    acc: dict = {}
    for p in parsed_list:
        for sid, d in p.items():
            acc.setdefault(sid, {}).update(d)
    return {sid: [[t, d[t]] for t in sorted(d)] for sid, d in acc.items()}


def _live_merge(seed_name: str, http, kind, head, parser, n_live=2, min_len=5):
    seed = load_seed(seed_name) or {}
    items = list(_listing(http, kind, head))[:n_live]
    parsed = [parser(_fulltext(http, pid)) for pid, _t, _d in reversed(items)]
    live = _fold(parsed)
    ids = set(seed) | set(live)
    merged = {sid: merge_pts(seed.get(sid, []), live.get(sid, [])) for sid in ids}
    for sid, pts in merged.items():
        if len(pts) < min_len:
            raise ValueError(f"{seed_name}/{sid}: only {len(pts)} points")
    return merged


# ------------------------------------------------------------------ helpers for builders

def _check(pts, sid, lo, hi, max_age_days, now=None):
    now = now or time.time()
    if not pts:
        raise ValueError(f"{sid}: no points")
    bad = [p for p in pts if not (lo <= p[1] <= hi)]
    if bad:
        raise ValueError(f"{sid}: implausible values {bad[:3]}")
    if now - pts[-1][0] > max_age_days * 86400:
        raise ValueError(f"{sid}: latest point {pts[-1]} is older than {max_age_days} days")


# ------------------------------------------------------------------ Consumer Confidence

UCCS_HEAD = "Urban+Consumer+Confidence+Survey+-+Bi-monthly"


@register("rbi-consumer-confidence-survey", section=SECTION, name="RBI Consumer Confidence Survey")
def uccs(http):
    d = _live_merge("uccs", http, "Bimonthly", UCCS_HEAD, parse_uccs, n_live=3)
    for sid in ("csi", "fei"):
        _check(d[sid], sid, 20, 160, 160)
    for sid, pts in d.items():
        if sid not in ("csi", "fei"):
            _check(pts, sid, -100, 100, 160)
    names = {sid: nm for sid, nm, _ in UCCS_VARS}
    unit = "Net response (% points)"
    cur = [series(f"cur_{s}", f"{n}: current perception", unit, d[f"cur_{s}"]) for s, n in names.items()]
    fut = [series(f"fut_{s}", f"{n}: one year ahead", unit, d[f"fut_{s}"]) for s, n in names.items()]
    idx = "Index (100 = neutral)"
    return record(
        "rbi-consumer-confidence-survey", title="RBI Consumer Confidence Survey (CSI and FEI)",
        description=(
            "Reserve Bank of India household survey (bi-monthly since 2017; urban households in 6, then 13, now 19 "
            "cities). The Current Situation Index (CSI) summarises how households rate today against a year ago, and the "
            "Future Expectations Index (FEI) how they expect the year ahead to compare with now. Both equal 100 plus the "
            "average net response on the economy, employment, prices, income and spending; above 100 is optimism. "
            "Useful as a timely read on private consumption and as a cross-check on inflation sentiment. Each point is "
            "stamped on the first day of the survey month; results are released about 4-6 weeks later."
        ),
        frequency="B",
        views=[
            view("Confidence indices", series("csi", "Current Situation Index", idx, d["csi"]),
                 series("fei", "Future Expectations Index", idx, d["fei"]), vid="indices"),
            view("Net responses: current perception", *cur, vid="current"),
            view("Net responses: one year ahead", *fut, vid="future"),
        ],
        source="Reserve Bank of India (Consumer Confidence Survey press releases)",
        source_url="https://www.rbi.org.in/scripts/BimonthlyPublications.aspx?head=Urban+Consumer+Confidence+Survey+-+Bi-monthly",
        qna=[
            {"question": "What does the survey measure?",
             "answer": "Households are asked whether the general economic situation, employment, the price level, their own income and their spending are better, same or worse than a year ago (current perception) and whether they will be better, same or worse a year from now (expectation).^"
                       "The net response is the percentage reporting improvement minus the percentage reporting deterioration, so it lies between -100 and +100. For the price level a rise counts as a negative, so its net responses are typically deeply negative."},
            {"question": "How should the indices be read?",
             "answer": "CSI and FEI are 100 plus the simple average of the five net responses (current and one-year-ahead respectively). Above 100 means optimism on balance; below 100 pessimism. The FEI is structurally higher than the CSI because households are generally more optimistic about the future than about the present.^"
                       "Look at changes and at the components: a fall in the CSI driven by employment and the economic situation is a different signal from one driven by the price level."},
            {"question": "What are the caveats?",
             "answer": "Coverage changed over time: 6 metropolitan cities until early 2017, 13 cities afterwards, and 19 cities under the name Urban Consumer Confidence Survey from 2025. The survey was irregular in 2016-17 and bi-monthly from May 2017.^"
                       "The results reflect respondents' views, not the Reserve Bank's. This series is assembled from the tables in each press release (net responses from July 2020, the indices from May 2021); the press releases refer to an Excel time series that this dashboard does not use."},
        ],
    )


# ------------------------------------------------------------------ Households' Inflation Expectations

IES_HEAD = "Inflation+Expectations+Survey+of+Households+-+Bi-monthly"


@register("rbi-household-inflation-expectations", section=SECTION, name="RBI Households' Inflation Expectations")
def ies(http):
    d = _live_merge("ies", http, "Bimonthly", IES_HEAD, parse_ies, n_live=3)
    for sid, pts in d.items():
        _check(pts, sid, 0, 30, 160)
    u = "Per cent"
    return record(
        "rbi-household-inflation-expectations", title="RBI Households' Inflation Expectations Survey",
        description=(
            "Median inflation perceived by households over the past year, and their expected inflation three months and one "
            "year ahead, from the Reserve Bank's bi-monthly survey of urban households (19 cities). Households' own "
            "price experience runs well above CPI, so use the series for direction and for the gap between perception "
            "and expectation, not as a forecast of CPI. Stamped on the first day of the survey month; results are released "
            "about 4-6 weeks later. Mean series are also shown; they are higher than medians because of a long right tail of replies."
        ),
        frequency="B",
        views=[
            view("Median", series("cur_med", "Perceived current inflation (median)", u, d["cur_med"]),
                 series("m3_med", "Expected inflation, 3 months ahead (median)", u, d["m3_med"]),
                 series("y1_med", "Expected inflation, 1 year ahead (median)", u, d["y1_med"]), vid="median"),
            view("Mean", series("cur_mean", "Perceived current inflation (mean)", u, d["cur_mean"]),
                 series("m3_mean", "Expected inflation, 3 months ahead (mean)", u, d["m3_mean"]),
                 series("y1_mean", "Expected inflation, 1 year ahead (mean)", u, d["y1_mean"]), vid="mean"),
        ],
        source="Reserve Bank of India (Inflation Expectations Survey of Households press releases)",
        source_url="https://www.rbi.org.in/scripts/BimonthlyPublications.aspx?head=Inflation+Expectations+Survey+of+Households+-+Bi-monthly",
        qna=[
            {"question": "What is this survey?",
             "answer": "A Reserve Bank survey that asks households for a number: how much did prices rise over the last year (perception) and by how much will they rise over the next three months and the next year (expectations). Quantitative responses are summarised by their mean and median, with standard errors in the press release.^"
                       "It is a household-sentiment gauge, not a forecast by the Reserve Bank, and reflects respondents' own consumption baskets."},
            {"question": "How should I read it?",
             "answer": "Medians are the headline the Reserve Bank quotes. Households typically report inflation well above measured CPI, so the level matters less than the change between rounds and the gap between three-month and one-year expectations.^"
                       "Rising expectations feed wage and price setting and can pre-empt tightening of policy; falling expectations are consistent with disinflation."},
            {"question": "What are the caveats?",
             "answer": "The survey covers urban households in major cities (13 cities earlier, 19 now), so it under-represents rural India. The series starts in September 2018 here because earlier press releases used a different table layout.^"
                       "Each point comes from Table 3 of the press release for that round, as first published."},
        ],
    )


# ------------------------------------------------------------------ Survey of Professional Forecasters

SPF_HEAD = "Survey+of+Professional+Forecasters+-+Bi-monthly"
SPF_ITEMS = [
    ("gdp", r"GDP at constant (?:\([\d\-]+\)\s+)?prices: Annual Growth"),
    ("cpi", r"Inflation based on CPI Combined: Headline"),
]


def _six(block: str, label_re: str):
    m = re.search(label_re, block)
    if not m:
        return None
    n = re.search(rf"(?:{NUM}\s+){{5}}{NUM}", block[m.end():m.end() + 400])
    return [float(x) for x in n.group(0).split()] if n else None


def parse_spf(text: str) -> dict:
    """One SPF round -> {series_id: {survey_month_ts: median}} for the current-FY and next-FY annual forecasts.
    series ids: gdp_fy0/gdp_fy1/cpi_fy0/cpi_fy1; fy0 = fiscal year containing the survey month."""
    m = re.search(r"(?:conducted|received)\s+(?:during|in)\s+(?:the\s+month\s+of\s+)?([A-Za-z]+)\.?\s*(?:\d{1,2}\s*-\s*\d{1,2}\s*,?\s*)?,?\s*(\d{4})", text)
    if not m or m.group(1)[:3].lower() not in MONTHS:
        raise ValueError("SPF survey month not found")
    sy, sm = int(m.group(2)), MONTHS[m.group(1)[:3].lower()]
    t = month_ts(sy, sm)
    fy0 = sy if sm >= 4 else sy - 1
    heads = list(re.finditer(r"Annex\s*(\d):\s*Annual Forecasts for (\d{4})-(\d{2})", text))
    # keep the first occurrence of each annex (headers repeat on the same page)
    seen, annex = set(), []
    for h in heads:
        if h.group(1) not in seen:
            seen.add(h.group(1))
            annex.append(h)
    if len(annex) < 2:
        raise ValueError("SPF annual annexes not found")
    out: dict = {}
    for k, h in enumerate(annex[:2]):
        end = text.find("Annex", h.end()) if False else None
        nxt = [x.start() for x in re.finditer(r"Annex\s*\d:", text) if x.start() > h.end()]
        block = text[h.end(): nxt[0] if nxt else len(text)]
        fy = int(h.group(2))
        if fy != fy0 + k:
            raise ValueError(f"SPF annex {h.group(1)} is FY{fy}, expected FY{fy0 + k}")
        for sid, lre in SPF_ITEMS:
            v = _six(block, lre)
            if v is None:
                raise ValueError(f"SPF item {sid} not found in annex {h.group(1)}")
            mean, med, mx, mn = v[0], v[1], v[2], v[3]
            if not (mn <= med <= mx):
                raise ValueError(f"SPF {sid}: median {med} outside min/max {mn}/{mx}")
            out[f"{sid}_fy{k}"] = {t: med}
    return out


def _spf_record(slug, title, key, unit_note, http, desc, qna):
    d = _live_merge("spf", http, "Bimonthly", SPF_HEAD, parse_spf, n_live=3, min_len=5)
    fy0, fy1 = d[f"{key}_fy0"], d[f"{key}_fy1"]
    lo, hi = (-2, 15) if key == "gdp" else (0, 20)
    for sid in (f"{key}_fy0", f"{key}_fy1"):
        _check(d[sid], sid, lo, hi, 130)
    u = "Per cent"
    return record(
        slug, title=title, description=desc, frequency="B",
        views=[view("By survey round", series(f"{key}_fy0", f"{unit_note}: current fiscal year", u, fy0),
                    series(f"{key}_fy1", f"{unit_note}: next fiscal year", u, fy1), vid="rounds")],
        source="Reserve Bank of India (Survey of Professional Forecasters press releases)",
        source_url="https://www.rbi.org.in/scripts/BimonthlyPublications.aspx?head=Survey+of+Professional+Forecasters+-+Bi-monthly",
        qna=qna,
    )


_SPF_COMMON = (
    "Each point is the median forecast of the panel in one survey round, stamped on the first day of the month in "
    "which the survey was conducted (the press release follows 4-6 weeks later). The 'current fiscal year' series is the "
    "forecast for the April-March year that contains the survey month (e.g. a September 2026 round gives 2026-27); "
    "'next fiscal year' is the following year. So the series is the evolution of expectations, not a time series of outcomes: "
    "the forecast target changes each April. "
)


@register("rbi-spf-gdp-growth-forecast", section=SECTION, name="RBI SPF: Real GDP Growth Forecast")
def spf_gdp(http):
    return _spf_record(
        "rbi-spf-gdp-growth-forecast", "RBI Survey of Professional Forecasters: Real GDP Growth", "gdp",
        "Real GDP growth, median forecast", http,
        "Median forecast of annual real GDP growth (per cent, constant prices) by professional forecasters in the Reserve Bank's Survey of Professional Forecasters. " + _SPF_COMMON +
        "Useful as the consensus benchmark against which to judge nowcasts and official estimates.",
        [{"question": "What is the Survey of Professional Forecasters?",
          "answer": "A Reserve Bank survey of about 30-50 economists from banks, research houses and rating agencies, who forecast growth, inflation, external and financial variables for the current and following fiscal years and the next few quarters. The Reserve Bank has run it since September 2007.^"
                    "The press release reports the median forecast (with mean, range and quartiles in annexes). This series takes the median from the annual-forecast annexes."},
         {"question": "How do I read the points?",
          "answer": "Each point is one survey round: the date is the survey month and the value is the median forecast of growth for the fiscal year named in the series (current or next). Moves between rounds show how forecasters are revising in response to data.^"
                    "Watch the gap between current- and next-year forecasts, and the size of revisions after GDP releases or policy shocks."},
         {"question": "What are the caveats?",
          "answer": "The fiscal year is the April-March year, and GDP is real GDP at constant prices. The base year and definition of GDP change when the national accounts are rebased, so forecasts across a rebasing are not strictly comparable.^"
                    "The panel membership varies by round and views are the forecasters' own, not the Reserve Bank's."}])


@register("rbi-spf-cpi-inflation-forecast", section=SECTION, name="RBI SPF: CPI Inflation Forecast")
def spf_cpi(http):
    return _spf_record(
        "rbi-spf-cpi-inflation-forecast", "RBI Survey of Professional Forecasters: CPI Inflation", "cpi",
        "CPI headline inflation, median forecast", http,
        "Median forecast of annual headline CPI (Combined) inflation (per cent) by professional forecasters in the Reserve Bank's Survey of Professional Forecasters. " + _SPF_COMMON +
        "Compare with the RBI's own projection and the inflation target (4 per cent, with a 2-6 per cent tolerance band).",
        [{"question": "What does this series show?",
          "answer": "The panel's median forecast for average headline CPI inflation (CPI Combined, general index) over the fiscal year, from each round of the Survey of Professional Forecasters.^"
                    "The first series is the forecast for the year in which the survey was conducted, the second for the next year."},
         {"question": "How do I read the points?",
          "answer": "The date is the survey month, the value the forecast. Rising forecasts across rounds mean forecasters are marking up inflation; a gap between current-year and next-year forecasts signals the expected path.^"
                    "The press release also gives quarterly CPI forecasts, core CPI and WPI forecasts, not included here."},
         {"question": "What are the caveats?",
          "answer": "CPI was rebased (2024=100) recently, so forecasts made on the old and new index are not strictly comparable. Fiscal-year inflation is an annual average, so a forecast for the current year partly reflects months already published.^"
                    "Forecasts are the panellists' own and the panel changes from round to round."}])


# ------------------------------------------------------------------ Industrial Outlook Survey

IOS_HEAD = "Quarterly+Industrial+Outlook+Survey"


def _fq_end(label: str) -> int:
    m = re.match(r"^Q([1-4]):\s*(\d{4})", label)
    if not m:
        raise ValueError(f"bad quarter label {label!r}")
    q, y = int(m.group(1)), int(m.group(2))
    return quarter_end_ts(y, q + 1) if q < 4 else quarter_end_ts(y + 1, 1)


def parse_ios(text: str) -> dict:
    """'Business Sentiments' table: Quarter, BAI, BEI (the newest quarter carries only a BEI)."""
    lines = text.splitlines()
    idx = [i for i, l in enumerate(lines) if re.match(r"^Table\s*\d+\s*:\s*Business Sentiments", l)]
    if not idx:
        raise ValueError("IOS Business Sentiments table not found")
    bai, bei = {}, {}
    for l in lines[idx[0] + 1:]:
        m = re.match(rf"^(Q[1-4]:\s*\d{{4}}\s*-\s*\d{{2}})\s+({NUM})(?:\s+({NUM}))?\s*$", l.strip())
        if m:
            t = _fq_end(m.group(1))
            if m.group(3) is not None:
                bai[t], bei[t] = float(m.group(2)), float(m.group(3))
            else:
                bei[t] = float(m.group(2))
        elif bai:
            break
    if len(bai) < 3:
        raise ValueError("IOS table has too few rows")
    return {"bai": bai, "bei": bei}


@register("rbi-industrial-outlook-survey", section=SECTION, name="RBI Industrial Outlook Survey")
def ios(http):
    d = _live_merge("ios", http, "Quarterly", IOS_HEAD, parse_ios, n_live=2, min_len=8)
    for sid, pts in d.items():
        _check(pts, sid, 40, 180, 330)
    idx = "Index (100 = neutral)"
    return record(
        "rbi-industrial-outlook-survey", title="RBI Industrial Outlook Survey: Business Assessment and Expectations Indices",
        description=(
            "Quarterly Reserve Bank survey of Indian manufacturing companies. The Business Assessment Index (BAI) summarises "
            "firms' assessment of the quarter just ended (production, order books, capacity utilisation, inventories, employment, "
            "exports, finances and profit margins), and the Business Expectations Index (BEI) their expectation for the quarter ahead. "
            "Each index is a simple average of nine weighted net responses and ranges from 0 to 200; 100 separates expansion from "
            "contraction. Points are stamped on the last day of the calendar quarter they refer to, so the latest BEI point (the coming "
            "quarter) is dated one quarter after the latest BAI. Complements capacity utilisation (OBICUS) and PMIs for industrial nowcasting."
        ),
        frequency="Q",
        views=[view("BAI and BEI", series("bai", "Business Assessment Index (BAI)", idx, d["bai"]),
                    series("bei", "Business Expectations Index (BEI)", idx, d["bei"]))],
        source="Reserve Bank of India (Industrial Outlook Survey press releases)",
        source_url="https://www.rbi.org.in/scripts/QuarterlyPublications.aspx?head=Quarterly+Industrial+Outlook+Survey",
        qna=[
            {"question": "What do BAI and BEI measure?",
             "answer": "The survey asks manufacturers whether production, order books, capacity utilisation, employment, exports, finances and profit margins increased, stayed the same or decreased this quarter (assessment) and are expected to in the next. Net response = percentage reporting optimism minus percentage reporting pessimism.^"
                       "BAI is built from the assessment answers and BEI from the expectation answers. Both are composites of nine parameters, scaled around 100."},
            {"question": "How should I read them?",
             "answer": "Above 100 is expansion and below 100 is contraction. Expectations typically run well above assessments (firms are structurally optimistic), so compare BEI with its own history and track BAI as the realised sentiment.^"
                       "Quarter labels follow the fiscal year (Q1 = April-June); this dashboard dates each point at the end of the corresponding calendar quarter."},
            {"question": "What are the caveats?",
             "answer": "The survey is qualitative, covers roughly 1,000-1,300 manufacturing companies, and the Reserve Bank has revised the index methodology from time to time. Values are as published in each press release; later releases can restate recent quarters slightly (the latest published value wins here).^"
                       "History is limited to what the press releases carry and parse; the series starts with the oldest release successfully parsed."},
        ],
    )
