"""
Mirror scraper for indiamacroindicators.co.in economic indicators.

Every indicator page embeds its full data as JSON inside the Next.js
server-rendered payload (self.__next_f.push chunks), so no browser is needed.

Pipeline:
  1. discover()   - read the 10 category pages, take each category's ordered
                    indicator list (JSON-LD ItemList) = exact site categorisation.
  2. scrape_*()   - fetch each indicator page and normalise it into one record:
                      views -> series -> points [unix_seconds, value, (obs_date)]
                    (or a "curve" record for the G-Sec yield curve).
  3. write        - site/data/i/<slug>.json + site/data/index.json.
                    Files whose data did not change are left untouched, so a
                    scheduled run only produces a diff when something is new.

Usage:
    python scraper/scrape.py                 # everything
    python scraper/scrape.py --only slug ... # just these slugs
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = "https://indiamacroindicators.co.in"
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "site" / "data"
ITEM_DIR = OUT_DIR / "i"
DEFAULT_SOURCE = "CMIE Economic Outlook, 1 Finance Research"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

# Category order as the site's own navigation lists them.
CATEGORY_SLUGS = [
    "services-sector-activity",
    "industrial-sector-performance",
    "agriculture-output",
    "consumer-inflation",
    "equity-market-optimism",
    "financial-sector-soundness",
    "global-economic-impact",
    "interest-rate-outlook",
    "other-hfis",
    "key-economic-indicators",
]

# Key-indicator pages reachable only via "related" links. The site links to
# government-capital-expenditure-yoy-growth but that URL returns a 404, so it
# is not mirrored; add slugs here if the source ever publishes more.
EXTRA_KEY_PAGES: dict = {}

# Hand-written series names for the key-indicator pages (payload keys are
# misleading, e.g. the mutual-fund page's `equity` key holds total net AUM).
KEY_SERIES_NAMES = {
    "consumer-price-index-headline-urban-rural": {
        "combined": "Combined (headline)",
        "urban": "Urban",
        "rural": "Rural",
    },
    "nifty-50-pe-ratio": {"nifty_50": "Nifty 50 index", "nifty_pe": "Nifty 50 P/E ratio"},
    "mutual-fund-aum": {
        "equity": "Net AUM",
        "net_aum": "Share of equity funds",
    },
}

PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
LDJSON_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
META_DESC_RE = re.compile(r'<meta name="description" content="([^"]*)"')

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ----------------------------------------------------------------- helpers

def winpath(p: Path) -> Path:
    """Extended-length path on Windows (the project folder path is very long)."""
    s = str(p)
    if sys.platform == "win32" and not s.startswith("\\\\?\\"):
        return Path("\\\\?\\" + s)
    return p


def fetch(url: str, tries: int = 4) -> str:
    last = None
    for i in range(tries):
        try:
            r = SESSION.get(url, timeout=40)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:  # noqa: PERF203
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def decode_rsc_payload(html: str) -> str:
    parts = []
    for m in PUSH_RE.finditer(html):
        try:
            parts.append(json.loads('"' + m.group(1) + '"'))
        except json.JSONDecodeError:
            continue
    return "".join(parts)


def extract_balanced(text: str, start: int) -> str:
    """Balanced {...} or [...] value starting at text[start]."""
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("unbalanced brackets")


def json_after(payload: str, key: str, start: int = 0):
    """Parse the JSON value following  "key":  (object or array)."""
    needle = f'"{key}":'
    i = payload.find(needle, start)
    if i == -1:
        return None
    j = i + len(needle)
    if payload[j] in "{[":
        return json.loads(extract_balanced(payload, j))
    return None


def iso(ts_seconds: float) -> str:
    return (EPOCH + timedelta(seconds=ts_seconds)).strftime("%Y-%m-%d")


def ld_nodes(html: str):
    for m in LDJSON_RE.finditer(html):
        try:
            o = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        for n in (o.get("@graph", [o]) if isinstance(o, dict) else o):
            if isinstance(n, dict):
                yield n


def pretty_name(key: str) -> str:
    k = re.sub(r"_abs$", "", key).replace("_", " ")
    return k[:1].upper() + k[1:]


# --------------------------------------------------------------- discovery

def discover() -> dict:
    """Categories, ordered indicator lists, and per-slug basics."""
    # graph type / frequency live in the Q&A records on the index page
    idx_html = fetch(f"{BASE}/economic-indicators")
    idx_payload = decode_rsc_payload(idx_html)
    rec_rx = re.compile(
        r'"sub_topic":"([^"]*)".*?"slug":"([^"]*)".*?"frequency":"([^"]*)".*?"graph":"([^"]*)"'
    )
    meta = {}
    for m in rec_rx.finditer(idx_payload):
        meta.setdefault(m.group(2), {"graph": m.group(4), "frequency": m.group(3)})

    categories, indicators = [], {}
    for cslug in CATEGORY_SLUGS:
        html = fetch(f"{BASE}/economic-indicators/category/{cslug}")
        name = cslug
        for n in ld_nodes(html):
            if n.get("@type") in ("CollectionPage", "WebPage") and n.get("name"):
                name = n["name"].split("|")[0].strip()
                break
        md = META_DESC_RE.search(html)
        order = []
        for n in ld_nodes(html):
            if n.get("@type") != "ItemList":
                continue
            for it in n.get("itemListElement", []):
                url = it.get("url") or ""
                slug = url.rsplit("/", 1)[-1]
                path = "key-economic-indicators" if "/key-economic-indicators/" in url else "economic-indicators"
                ind = indicators.setdefault(
                    slug, {"slug": slug, "name": it.get("name"), "path": path, "categories": []}
                )
                if cslug not in ind["categories"]:
                    ind["categories"].append(cslug)
                order.append(slug)
        if cslug == "key-economic-indicators":
            for slug, nm in EXTRA_KEY_PAGES.items():
                ind = indicators.setdefault(
                    slug, {"slug": slug, "name": nm, "path": "key-economic-indicators", "categories": []}
                )
                if cslug not in ind["categories"]:
                    ind["categories"].append(cslug)
                order.append(slug)
        categories.append(
            {
                "slug": cslug,
                "name": name,
                "description": (md.group(1) if md else "").replace("&#x27;", "'").replace("&amp;", "&"),
                "indicators": list(dict.fromkeys(order)),
            }
        )
    for slug, ind in indicators.items():
        ind.update(meta.get(slug, {}))
        ind.setdefault("graph", "line")
        ind.setdefault("frequency", None)
    return {"categories": categories, "indicators": indicators}


# ----------------------------------------------------------- page scraping

def page_common(html: str, payload: str) -> dict:
    ld = next((n for n in ld_nodes(html) if n.get("@type") == "Dataset"), {})
    title = None
    tm = re.search(r'"glance_heading","children":"([^"]+)"', payload)
    if tm:
        title = tm.group(1)
    else:
        hm = re.search(r'"h1",null,\{"className":"[^"]*","children":"([^"]+)"', payload)
        title = hm.group(1) if hm else None
    variants = {}
    vi = payload.find('"variants":{')
    if vi != -1:
        try:
            variants = json.loads(extract_balanced(payload, payload.index("{", vi + 11)))
        except (ValueError, json.JSONDecodeError):
            variants = {}
    if not variants:
        hm = re.search(r"\"children\":\"(India's [^\"]{20,300})\"", payload)
        if hm:
            variants = {"Absolute": hm.group(1)}
    md = META_DESC_RE.search(html)
    return {
        "title": title or ld.get("name"),
        "description": ld.get("description")
        or (md.group(1).replace("&#x27;", "'").replace("&amp;", "&") if md else None),
        "headline": variants,
        "source": ld.get("creditText") or DEFAULT_SOURCE,
        "source_url": ld.get("isBasedOn"),
        "date_modified": ld.get("dateModified"),
    }


def make_series(sid, name, rows, vkey="indicnum"):
    pts = []
    units = None
    for r in rows:
        v = r.get(vkey)
        if v is None:
            continue
        ts = r.get("unix_date")
        if ts is None and r.get("index_date") is not None:
            ts = r["index_date"] / 1000
        if ts is None:
            continue
        p = [int(ts), v]
        if r.get("indicdate"):
            p.append(r["indicdate"])
        pts.append(p)
        units = r.get("units") or units
    pts.sort(key=lambda p: p[0])
    return {"id": sid, "name": name, "units": units, "pts": pts}


def views_from_dict(data: dict, names: dict | None = None) -> list:
    """Group named series by unit into switchable views."""
    series = []
    keys = list(data)
    if names:  # hand-written name maps also define the display order
        keys.sort(key=lambda k: list(names).index(k) if k in names else len(names))
    for key in keys:
        rows = data[key]
        nm = (names or {}).get(key) or pretty_name(key)
        series.append(make_series(key, nm, rows))
    groups = {}
    for s in series:
        groups.setdefault(s["units"], []).append(s)
    views = []
    for units, ss in groups.items():
        label = ss[0]["name"] if len(ss) == 1 else (units or "Series")
        views.append({"id": f"v{len(views)}", "label": label, "series": ss})
    # ratio pages (P/E, P/B) are about the ratio, so lead with it
    views.sort(key=lambda v: 0 if v["series"][0]["units"] == "Times" else 1)
    for i, v in enumerate(views):
        v["id"] = f"v{i}"
    return views


def scrape_standard(slug: str, path: str) -> dict:
    url = f"{BASE}/{path}/{slug}"
    html = fetch(url)
    payload = decode_rsc_payload(html)
    anchor = payload.find('"unix_date"')
    if anchor == -1:
        raise ValueError("no series data in payload")
    start = payload.rfind('{"isToggle"', 0, anchor)
    if start == -1:
        raise ValueError("no isToggle object")
    obj = json.loads(extract_balanced(payload, start))
    data = obj["data"]

    if isinstance(data, dict):
        views = views_from_dict(data)
    elif data and "abs_indicnum" in data[0]:
        views = [
            {"id": "abs", "label": "Absolute",
             "series": [make_series("value", "Value", data, "abs_indicnum")]},
            {"id": "yoy", "label": "YoY % change",
             "series": [make_series("yoy", "YoY change", data, "yoy_indicnum")]},
        ]
        # units live under different keys on these rows
        views[0]["series"][0]["units"] = next((r.get("abs_units") for r in reversed(data) if r.get("abs_units")), None)
        views[1]["series"][0]["units"] = next((r.get("yoy_units") for r in reversed(data) if r.get("yoy_units")), "Per cent")
    else:
        views = [{"id": "v0", "label": None, "series": [make_series("value", "Value", data)]}]

    rec = {
        "slug": slug, "path": path, "kind": "timeseries", "page_url": url,
        "frequency": obj.get("frequency"),
        "views": views,
        "qna": [
            {"question": q.get("question"), "answer": q.get("answer")}
            for q in obj.get("ques_and_ans", [])
        ],
        "related": [
            {"slug": r["slug"], "title": r.get("indicator")}
            for r in (obj.get("related_hfi") or [])
            if r.get("slug")
        ],
    }
    rec.update(page_common(html, payload))
    return rec


def scrape_key(slug: str) -> dict:
    path = "key-economic-indicators"
    url = f"{BASE}/{path}/{slug}"
    html = fetch(url)
    payload = decode_rsc_payload(html)
    chart = json_after(payload, "chartData")
    if chart is None:
        raise ValueError("no chartData")
    rec = {"slug": slug, "path": path, "page_url": url, "qna": []}
    fm = re.search(r'"frequency":"([^"]*)"', payload)
    rec["frequency"] = fm.group(1) if fm else None

    if isinstance(chart, list) and chart and "label" in chart[0] and "latest_source" in chart[0]:
        rec["kind"] = "curve"
        rec["curve"] = {
            "labels": [c["label"] for c in chart],
            "snapshots": [
                {"name": "Latest", "date": chart[0]["latest_date"], "values": [c["latest_source"] for c in chart]},
                {"name": "1 month ago", "date": chart[0]["one_month_ago_date"], "values": [c["one_month_ago_source"] for c in chart]},
                {"name": "6 months ago", "date": chart[0]["six_months_ago_date"], "values": [c["six_months_ago_source"] for c in chart]},
            ],
            "units": "Per cent",
        }
        rec["views"] = []
    else:
        rec["kind"] = "timeseries"
        if isinstance(chart, dict):
            rec["views"] = views_from_dict(chart, KEY_SERIES_NAMES.get(slug))
        else:
            rec["views"] = [{"id": "v0", "label": None,
                             "series": [make_series("value", "Value", chart)]}]

    # related list
    rel = json_after(payload, "otherIndicators") or []
    rec["related"] = [{"slug": r["slug"], "title": r.get("title")} for r in rel if r.get("slug")]

    common = page_common(html, payload)
    h1 = re.search(r'"h1",null,\{"className":"[^"]*","children":"([^"]+)"', payload)
    if h1:
        common["title"] = h1.group(1)
    if not common["headline"]:
        hm = re.search(r"\"children\":\"(India's [^\"]{20,300})\"", payload)
        if hm:
            common["headline"] = {"Absolute": hm.group(1)}
    rec.update(common)
    return rec


# ------------------------------------------- indices, global P/E, calendar

INDEX_PAGES = [
    ("1-finance-macroeconomic-index", "1-finance-macroeconomic-index", "macroeconomic-index"),
    ("services-sector-activity-index", "subindices/services-sector-activity-index", "subindices"),
    ("industrial-sector-performance-index", "subindices/industrial-sector-performance-index", "subindices"),
    ("agriculture-output-index", "subindices/agriculture-output-index", "subindices"),
    ("consumer-inflation-index", "subindices/consumer-inflation-index", "subindices"),
    ("equity-market-optimism-index", "subindices/equity-market-optimism-index", "subindices"),
    ("global-economic-impact-index", "subindices/global-economic-impact-index", "subindices"),
    ("financial-sector-soundness-index", "subindices/financial-sector-soundness-index", "subindices"),
    ("interest-rate-outlook-index", "subindices/interest-rate-outlook-index", "subindices"),
]
TABLE_PAGES = [
    ("global-market-pe-ratios", "global-market-pe-ratios", "market-tools", "Global Market P/E Ratios"),
    ("india-economic-calendar", "india-economic-calendar", "market-tools", "India Economic Calendar"),
]
VALUATION = {2: "Highly undervalued", 1: "Undervalued", 0: "Fairly valued", -1: "Overvalued", -2: "Highly overvalued"}


def text_blocks(payload: str, minlen: int = 80) -> list:
    out = []
    for m in re.finditer(r'"children":"((?:[^"\\]|\\.){%d,})"' % minlen, payload):
        try:
            t = json.loads('"' + m.group(1) + '"')
        except json.JSONDecodeError:
            continue
        if t not in out and not t.startswith(("http", "$", "static/")) and " " in t:
            out.append(t)
    return out


def scrape_index_page(slug: str, path: str) -> dict:
    url = f"{BASE}/{path}"
    html = fetch(url)
    payload = decode_rsc_payload(html)
    d = json_after(payload, "data")
    if not d or "metadata" not in d:
        raise ValueError("no index metadata")
    md, dd = d["metadata"], d["data"]
    rhs = re.sub(r"\s*\(RHS\)\s*$", "", md.get("rhsLegend") or "Comparison series")
    rhs_units = "Times" if "P/E" in rhs else "Per cent"
    line = [[int(t / 1000), v] for t, v in dd["lineSeriesData"] if v is not None]
    area = [[int(t / 1000), v] for t, v in dd["areaSeriesData"] if v is not None]
    line.sort(key=lambda p: p[0])
    area.sort(key=lambda p: p[0])
    desc = md.get("descriptions") or []
    usability = md.get("usability") or {}
    qna = [{"question": q.get("question"), "answer": q.get("answer")} for q in desc]
    qna += [{"question": q.get("question"), "answer": q.get("answer")} for q in usability.get("main", [])]
    first_answer = (desc[0]["answer"].strip() if desc else "") or md.get("metaDescription") or ""
    tagline = next((t for t in text_blocks(payload, 40) if t.lower().startswith(("an index", "a composite", "the index"))), None)
    rec = {
        "slug": slug, "path": path, "kind": "timeseries", "page_url": url, "frequency": "D",
        "title": md.get("title"),
        "description": tagline or (first_answer[:240].rsplit(" ", 1)[0] + "…" if len(first_answer) > 240 else first_answer),
        "headline": {},
        "source": DEFAULT_SOURCE, "source_url": None, "date_modified": None,
        "views": [
            {"id": "v0", "label": "Index", "series": [
                {"id": "index", "name": "Index value", "units": "Index", "pts": line}]},
            {"id": "v1", "label": rhs, "series": [
                {"id": "rhs", "name": rhs, "units": rhs_units, "pts": area}]},
        ],
        "qna": qna,
        "related": [{"slug": r["slug"], "title": r.get("label")} for r in (md.get("relatedHfis") or []) if r.get("slug")],
        "index_meta": {
            "phase": md.get("currentPhase"), "phase_summary": md.get("currentPhaseSummary"),
            "success_rate": md.get("successRate"), "success_rate_description": md.get("successRateDescription"),
            "last_updated": md.get("lastUpdatedFormattedDate"),
            "interpretation": [
                {"title": md["interpretation"][k + "Title"], "description": md["interpretation"].get(k + "Description")}
                for k in ("recession", "weakRecovery", "strongRecovery", "growth")
                if (md.get("interpretation") or {}).get(k + "Title")
            ],
            "recent_updates": (md.get("details") or {}).get("recentUpdates") or [],
            "near_term_outlook": (md.get("details") or {}).get("nearTermOutlook") or [],
            "historical_events": [
                {k: v for k, v in e.items() if k not in ("imageUrl", "id")}
                for e in (md.get("historicalEvents") or [])
            ],
            "insights": usability.get("insights") or [],
        },
    }
    return rec


def scrape_global_pe(slug: str, path: str, title: str) -> dict:
    url = f"{BASE}/{path}"
    html = fetch(url)
    payload = decode_rsc_payload(html)
    dt = json_after(payload, "datatableData")
    if not dt:
        raise ValueError("no datatableData")
    rows_in = dt["rows"]
    year = next((re.search(r"\d{4}", k).group(0) for k in rows_in[0] if k.startswith("ytdReturn") and re.search(r"\d{4}", k)), "")

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    rows = []
    for r in rows_in:
        rows.append([
            r.get("countryName"), r.get("countryIndex"), VALUATION.get(r.get("category"), ""),
            num(r.get("valuationScore")), num(r.get("currentPERatio")), num(r.get("capeRatio")),
            num(r.get(f"ytdReturn{year}")), num(r.get("oneYearReturn")),
        ])
    lu = dt.get("lastUpdated") or {}
    return {
        "slug": slug, "path": path, "kind": "table", "page_url": url, "frequency": "M",
        "title": title,
        "description": next((t for t in text_blocks(payload) if t.startswith("A comparative overview")), None),
        "headline": {}, "source": DEFAULT_SOURCE, "source_url": None, "date_modified": None,
        "columns": [
            {"label": "Country"}, {"label": "Index"}, {"label": "Valuation"},
            {"label": "Valuation score", "num": True}, {"label": "Current P/E", "num": True},
            {"label": "CAPE ratio", "num": True}, {"label": f"YTD return {year} (%)", "num": True},
            {"label": "1-year return (%)", "num": True},
        ],
        "rows": rows,
        "as_of": lu.get("valuationScoreDate"),
        "notes": text_blocks(payload),
        "qna": [], "related": [], "views": [],
    }


def scrape_calendar(slug: str, path: str, title: str) -> dict:
    url = f"{BASE}/{path}"
    html = fetch(url)
    payload = decode_rsc_payload(html)
    d = json_after(payload, "data")
    if not d:
        raise ValueError("no calendar data")
    rows = []
    for month, days in d.items():
        for day, events in days.items():
            for e in events:
                rows.append([
                    e.get("date"), e.get("day"), (e.get("time_start") or "")[:5], e.get("indicator_name"),
                    e.get("category"), e.get("frequency"), e.get("impact"),
                    e.get("previous_value"), e.get("previous_value_date"),
                    e.get("actual_value"), e.get("actual_value_date"), e.get("expected_value"),
                    e.get("advisor_insights"), e.get("slug"),
                ])
    rows.sort(key=lambda r: (r[0] or "", r[2] or ""))
    return {
        "slug": slug, "path": path, "kind": "table", "page_url": url, "frequency": None,
        "title": title,
        "description": "Scheduled releases of key Indian economic data and RBI events, with previous and actual values.",
        "headline": {}, "source": DEFAULT_SOURCE, "source_url": None, "date_modified": None,
        "columns": [
            {"label": "Date"}, {"label": "Day"}, {"label": "Time (IST)"}, {"label": "Indicator"},
            {"label": "Category"}, {"label": "Frequency"}, {"label": "Impact"},
            {"label": "Previous"}, {"label": "Previous period"}, {"label": "Actual"},
            {"label": "Actual period"}, {"label": "Expected"}, {"label": "Insight"},
            {"label": "Indicator slug", "hidden": True},
        ],
        "rows": rows,
        "as_of": max((r[0] for r in rows if r[0]), default=None),
        "notes": [], "qna": [], "related": [], "views": [],
        "filterable": True,
    }


# ------------------------------------------------------------------- index

def card_summary(rec: dict, base: dict) -> dict:
    """What the home page / search needs without loading the full record."""
    out = {
        "slug": rec["slug"], "name": base["name"], "title": rec.get("title") or base["name"],
        "path": base["path"], "frequency": rec.get("frequency") or base.get("frequency"),
        "graph": base.get("graph", "line"), "categories": base["categories"],
        "kind": rec["kind"],
    }
    if rec["kind"] == "curve":
        c = rec["curve"]
        out.update(units="Per cent", latest_date=c["snapshots"][0]["date"],
                   latest_value=None, latest_yoy=None, n_points=len(c["labels"]) * 3)
        return out
    if rec["kind"] == "table":
        out.update(units=None, latest_date=None, latest_value=None, latest_yoy=None,
                   n_points=len(rec["rows"]), as_of=rec.get("as_of"))
        return out
    if rec.get("index_meta"):
        out["phase"] = rec["index_meta"].get("phase")
        out["success_rate"] = rec["index_meta"].get("success_rate")
    views = rec["views"]
    first = views[0]["series"][0]
    out["units"] = first["units"]
    out["n_points"] = max(len(s["pts"]) for s in views[0]["series"])
    out["latest_date"] = iso(first["pts"][-1][0]) if first["pts"] else None
    out["latest_value"] = first["pts"][-1][1] if first["pts"] else None
    out["spark"] = [p[1] for p in first["pts"][-40:]]
    out["latest_yoy"] = None
    for v in views:
        if v["id"] == "yoy" and v["series"][0]["pts"]:
            out["latest_yoy"] = v["series"][0]["pts"][-1][1]
    return out


def write_if_changed(path: Path, obj, stamp_key: str | None = None) -> bool:
    """Write JSON unless identical (ignoring stamp_key). Returns True if written."""
    p = winpath(path)
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            old = None
        if old is not None:
            a = {k: v for k, v in old.items() if k != stamp_key}
            b = {k: v for k, v in obj.items() if k != stamp_key}
            if a == b:
                return False
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return True


def add_extra_sections(cat: dict) -> None:
    """Add the index pages and the P/E / calendar tables as extra home sections."""
    sub_desc = ""
    try:
        md = META_DESC_RE.search(fetch(f"{BASE}/subindices"))
        sub_desc = (md.group(1) if md else "").replace("&#x27;", "'").replace("&amp;", "&")
    except RuntimeError:
        pass
    for slug, path, grp in INDEX_PAGES:
        name = "1 Finance Macroeconomic Index" if slug.startswith("1-finance") else slug.replace("-", " ").title()
        cat["indicators"][slug] = {"slug": slug, "name": name, "path": path, "categories": [grp],
                                   "graph": "line", "frequency": "D", "scraper": "index"}
    for slug, path, grp, title in TABLE_PAGES:
        cat["indicators"][slug] = {"slug": slug, "name": title, "path": path, "categories": [grp],
                                   "graph": "line", "frequency": None,
                                   "scraper": "global_pe" if "pe-ratios" in slug else "calendar"}
    macro = {"slug": "macroeconomic-index", "name": "1 Finance Macroeconomic Index",
             "description": "A composite index built from high-frequency indicators that tracks India's economic phase and has historically anticipated GDP growth.",
             "indicators": ["1-finance-macroeconomic-index"]}
    subs = {"slug": "subindices", "name": "Subindices",
            "description": sub_desc or "Sector-level indices that feed the macroeconomic index.",
            "indicators": [s for s, _, g in INDEX_PAGES if g == "subindices"]}
    tools = {"slug": "market-tools", "name": "Markets & Calendar",
             "description": "Global equity valuations and the calendar of scheduled Indian data releases.",
             "indicators": [s for s, _, _, _ in TABLE_PAGES]}
    cat["categories"] = [macro, subs] + cat["categories"] + [tools]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="only these slugs")
    args = ap.parse_args()

    winpath(ITEM_DIR).mkdir(parents=True, exist_ok=True)
    cat = discover()
    add_extra_sections(cat)
    print(f"Discovered {len(cat['categories'])} sections, {len(cat['indicators'])} pages")

    summaries, failures, changed, records = {}, [], 0, {}
    for slug, base in cat["indicators"].items():
        if args.only and slug not in args.only:
            continue
        try:
            kind = base.get("scraper")
            if kind == "index":
                rec = scrape_index_page(slug, base["path"])
            elif kind == "global_pe":
                rec = scrape_global_pe(slug, base["path"], base["name"])
            elif kind == "calendar":
                rec = scrape_calendar(slug, base["path"], base["name"])
            elif base["path"] == "key-economic-indicators":
                rec = scrape_key(slug)
            else:
                rec = scrape_standard(slug, base["path"])
            rec["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            records[slug] = rec
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {slug}: {e}")
            failures.append(slug)
        time.sleep(0.4)

    # Flag pages whose data is byte-identical to another indicator's (the
    # source site has at least one such case); mirror it, but say so.
    by_fp = {}
    for slug, rec in records.items():
        if rec["kind"] == "timeseries" and rec["views"] and rec["views"][0]["series"][0]["pts"]:
            fp = json.dumps([s["pts"] for v in rec["views"] for s in v["series"]], separators=(",", ":"))
            by_fp.setdefault(fp, []).append(slug)
    for slugs in by_fp.values():
        if len(slugs) > 1:
            for slug in slugs:
                others = [cat["indicators"][o]["name"] for o in slugs if o != slug]
                records[slug]["quality_note"] = (
                    "The source site publishes exactly the same data on this page as on: "
                    + "; ".join(others)
                    + ". It may be showing the wrong series; verify before relying on it."
                )

    for slug, rec in records.items():
        base = cat["indicators"][slug]
        wrote = write_if_changed(ITEM_DIR / f"{slug}.json", rec, stamp_key="scraped_at")
        changed += wrote
        s = card_summary(rec, base)
        if rec.get("quality_note"):
            s["quality_note"] = rec["quality_note"]
        summaries[slug] = s
        print(f"{'NEW ' if wrote else 'same'} {slug}: {s['n_points']} pts, latest {s['latest_date']} = {s['latest_value']} {s['units'] or ''}"
              + ("  [DUPLICATE DATA FLAGGED]" if rec.get("quality_note") else ""))

    if not args.only and failures:
        print("Index not rewritten because some pages failed; previous index kept.")
    if not args.only and not failures:
        index = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": DEFAULT_SOURCE,
            "categories": cat["categories"],
            "indicators": summaries,
        }
        write_if_changed(OUT_DIR / "index.json", index, stamp_key="generated_at")
        print(f"\n{len(summaries)} indicators ok, {changed} files changed, {len(failures)} failed")
    if failures:
        print("Failed:", ", ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
