"""Shared helpers for the official-source indicator modules (scraper/sources/*.py).

A module registers one builder per indicator:

    from .base import register, series, view, record, month_ts, clean_pts

    @register("upi-transactions", section="digital-administrative", name="UPI Transactions")
    def upi(http):
        ...                      # download + parse
        return record("upi-transactions", title=..., description=..., frequency="M",
                      views=[view("Monthly", series("volume", "Volume", "Million", pts))],
                      source="NPCI", source_url="https://...")

The framework runs every builder on its own, validates what it returns, and keeps the
previous saved data if a builder raises. So: raise on anything suspicious (a parse
that finds no rows, a layout change) instead of returning partial or empty data.

Conventions
-----------
* Points are [unix_seconds_UTC, value]. Monthly data is stamped on the FIRST day of the
  month, quarterly on the quarter's last day, daily/weekly on the actual date (for a
  weekly or fortnightly period, the period END date).
* Prefer the finest frequency the source publishes. If the fine series is short and a
  coarser one is longer, return both as separate views (view(..., frequency="D"/"M")).
* Units are plain strings ("Rs. crore", "Million", "Per cent", "Index", "MW", ...).
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

import requests
import requests.adapters

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
FREQUENCIES = {"D", "W", "F", "M", "B", "Q", "A"}  # F fortnightly, B bi-monthly
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


# ------------------------------------------------------------------ HTTP

MOSPI_API_PREFIX = "https://api.mospi.gov.in"


class _LegacyRenegotiationAdapter(requests.adapters.HTTPAdapter):
    """Approved exception for ONE host (MoSPI's data API, which only supports legacy TLS
    renegotiation). Certificate and hostname verification stay fully on."""

    def init_poolmanager(self, *args, **kwargs):
        import ssl

        ctx = ssl.create_default_context()  # CERT_REQUIRED + check_hostname
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


class _StrictSession(requests.Session):
    """requests.Session that refuses custom transport adapters (e.g. ones that weaken TLS).
    The one approved exception, MoSPI's API host, is mounted by Http itself; a module that
    tries to mount its own adapter for that host is ignored, any other custom adapter raises."""

    def mount(self, prefix, adapter):
        if type(adapter) is requests.adapters.HTTPAdapter:
            super().mount(prefix, adapter)
        elif prefix.rstrip("/") == MOSPI_API_PREFIX:
            return  # framework adapter already in place
        else:
            raise ValueError("custom transport adapters (e.g. weakened TLS) are not allowed")


class Http:
    """requests.Session with browser-like headers and retries.

    Every method takes the usual requests keyword arguments (params, headers, timeout,
    data, json...). TLS certificate verification can NOT be disabled: a host with a
    broken certificate must be skipped in favour of another official route. The only
    approved exception is legacy TLS renegotiation for api.mospi.gov.in (verification stays on).
    """

    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self):
        self.s = _StrictSession()
        requests.Session.mount(self.s, MOSPI_API_PREFIX, _LegacyRenegotiationAdapter())
        self.s.headers.update(
            {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Language": "en-IN,en;q=0.9"}
        )

    def request(self, method: str, url: str, *, tries: int = 3, timeout: int = 60, **kw) -> requests.Response:
        if kw.get("verify") is False:
            raise ValueError("disabling TLS certificate verification is not allowed; use another source")
        last: Optional[Exception] = None
        for i in range(tries):
            try:
                r = self.s.request(method, url, timeout=timeout, **kw)
                if r.status_code in self.RETRY_STATUS and i < tries - 1:
                    time.sleep(2 * (i + 1))
                    continue
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
                last = e
                time.sleep(2 * (i + 1))
        raise RuntimeError(f"{method} {url} failed: {last}")

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        return self.request("POST", url, **kw)

    def text(self, url: str, **kw) -> str:
        return self.get(url, **kw).text

    def json(self, url: str, **kw):
        return self.get(url, **kw).json()

    def bytes(self, url: str, **kw) -> bytes:
        return self.get(url, **kw).content


# -------------------------------------------------------------- registry

SECTIONS: dict = {
    "digital-administrative": {
        "name": "Digital & Administrative Data",
        "description": "Payments and administrative records that arrive weeks before official GDP data: UPI and other payment systems, e-way bills.",
    },
    "power-fuel": {
        "name": "Power & Fuel",
        "description": "Electricity demand and generation, coal, and consumption of petrol, diesel, LPG and other fuels.",
    },
    "industry-core": {
        "name": "Industry & Core Sectors",
        "description": "IIP by use-based group and the eight core industries, plus cement and other industrial output.",
    },
    "transport-vehicles": {
        "name": "Transport & Vehicles",
        "description": "Vehicle sales and registrations, air cargo and passengers, rail freight and passengers, port and container traffic.",
    },
    "trade-detail": {
        "name": "Trade Detail",
        "description": "Total goods exports and imports, gold and oil imports, services trade and remittances.",
    },
    "surveys-sentiment": {
        "name": "Surveys & Sentiment",
        "description": "RBI consumer confidence, industrial outlook and business expectations surveys, forecaster surveys and PMI detail.",
    },
    "financial-conditions": {
        "name": "Financial Conditions",
        "description": "India volatility index, effective exchange rates, T-bill yields and money-market rates.",
    },
    "labour-prices-inputs": {
        "name": "Labour, Prices & Agri Inputs",
        "description": "Payroll data, price indices and farm inputs; includes up-to-date replacements for series that stalled elsewhere.",
    },
}


@dataclass
class Builder:
    slug: str
    section: str
    name: str
    graph: str
    fn: Callable
    module: str


REGISTRY: dict = {}

# legacy page slug -> slug of a newer indicator that supersedes it (shown as a pointer on the old page)
SUPERSEDES: dict = {}


def supersedes(old_slug: str, new_slug: str) -> None:
    """Declare that indicator `new_slug` carries newer data than the legacy page `old_slug`."""
    SUPERSEDES[old_slug] = new_slug


def register(slug: str, *, section: str, name: str, graph: str = "line"):
    """Register fn(http) -> record(...) as the builder for indicator `slug`."""
    if section not in SECTIONS:
        raise ValueError(f"unknown section {section!r}; add it to SECTIONS in sources/base.py")

    def deco(fn: Callable) -> Callable:
        if slug in REGISTRY:
            raise ValueError(f"duplicate indicator slug {slug!r}")
        REGISTRY[slug] = Builder(slug, section, name, graph, fn, fn.__module__)
        return fn

    return deco


# ------------------------------------------------------- seed history

SEED_DIR = Path(__file__).resolve().parent / "seed"


def load_seed(name: str) -> list:
    """History saved earlier by a one-off backfill: [[ts, value], ...] (empty if none)."""
    p = SEED_DIR / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def save_seed(name: str, pts: list) -> None:
    """Save history so a long backfill (many files/pages) does not repeat on every run."""
    SEED_DIR.mkdir(exist_ok=True)
    (SEED_DIR / f"{name}.json").write_text(json.dumps(pts, separators=(",", ":")), encoding="utf-8")


def merge_pts(*lists: list) -> list:
    """Merge point lists; on a repeated timestamp the later list wins. Returns sorted points."""
    d = {}
    for pts in lists:
        for t, v in pts:
            d[t] = v
    return [[t, d[t]] for t in sorted(d)]


# --------------------------------------------------------------- helpers

def ts(y: int, m: int = 1, d: int = 1) -> int:
    """Unix seconds for a UTC calendar date."""
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def ts_date(d) -> int:
    """Unix seconds (UTC midnight) for a date/datetime."""
    return ts(d.year, d.month, d.day)


def ts_iso(s: str) -> int:
    """'YYYY-MM-DD' (or longer ISO string) -> unix seconds."""
    return ts(int(s[0:4]), int(s[5:7]), int(s[8:10]))


def month_ts(y: int, m: int) -> int:
    return ts(y, m, 1)


def quarter_end_ts(y: int, q: int) -> int:
    m = q * 3
    last = (date(y + (m // 12), (m % 12) + 1, 1) - timedelta(days=1)).day
    return ts(y, m, last)


def iso(ts_seconds: float) -> str:
    return (EPOCH + timedelta(seconds=ts_seconds)).strftime("%Y-%m-%d")


def _num(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.replace(",", "").replace("%", "").strip()
        if v in ("", "-", "--", "NA", "N.A.", "N/A", "nan", "NaN"):
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    f = round(f, 6)
    return int(f) if f.is_integer() and abs(f) < 1e15 else f


def clean_pts(pairs: Iterable) -> list:
    """(ts, value) pairs -> sorted [[ts, value], ...]; drops missing/NaN, last duplicate wins."""
    d = {}
    for t, v in pairs:
        n = _num(v)
        if n is None or t is None:
            continue
        d[int(t)] = n
    return [[t, d[t]] for t in sorted(d)]


def series(sid: str, name: str, units: Optional[str], pts: list) -> dict:
    return {"id": sid, "name": name, "units": units, "pts": pts}


def view(label: Optional[str], *series_list: dict, vid: Optional[str] = None, frequency: Optional[str] = None) -> dict:
    """A switchable view. Series in one view share a chart; give differing units separate views.
    `frequency` overrides the record frequency for date formatting (e.g. 'D' for a daily view)."""
    v = {"id": vid, "label": label, "series": list(series_list)}
    if frequency:
        v["frequency"] = frequency
    return v


def yoy_pts(pts: list) -> list:
    """Year-on-year % change, matching each point to the same calendar date one year earlier."""
    by = {t: v for t, v in pts}
    out = []
    for t, v in pts:
        dt = EPOCH + timedelta(seconds=t)
        try:
            prev = by.get(ts(dt.year - 1, dt.month, dt.day))
        except ValueError:  # 29 Feb
            prev = by.get(ts(dt.year - 1, dt.month, 28))
        if prev:
            out.append([t, round((v / prev - 1) * 100, 2)])
    return out


def yoy_view(s: dict, label: str = "YoY % change", frequency: Optional[str] = None) -> Optional[dict]:
    """A 'yoy' view computed from a series (None if there is under a year of data)."""
    pts = yoy_pts(s["pts"])
    if not pts:
        return None
    return view(label, series("yoy", "YoY change", "Per cent", pts), vid="yoy", frequency=frequency)


def record(
    slug: str, *, title: str, description: str, frequency: str, views: list,
    source: str, source_url: str, graph: str = "line", qna: Optional[list] = None,
    related: Optional[list] = None, notes: Optional[str] = None, headline: Optional[dict] = None,
) -> dict:
    """Build a record in the dashboard's schema. views may include None (ignored)."""
    views = [v for v in views if v]
    for i, v in enumerate(views):
        v["id"] = v.get("id") or f"v{i}"
    return {
        "slug": slug, "path": "official", "kind": "timeseries", "page_url": source_url,
        "frequency": frequency, "graph": graph, "title": title, "description": description,
        "headline": headline or {}, "source": source, "source_url": source_url, "date_modified": None,
        "views": views, "qna": qna or [], "related": related or [], "layer": "added",
        **({"note": notes} if notes else {}),
    }


def validate(rec: dict) -> None:
    """Raise ValueError describing the first problem found in a record."""
    for k in ("slug", "title", "description", "frequency", "views", "source", "source_url"):
        if not rec.get(k):
            raise ValueError(f"missing {k}")
    if rec["frequency"] not in FREQUENCIES:
        raise ValueError(f"frequency {rec['frequency']!r} not in {sorted(FREQUENCIES)}")
    seen_ids = set()
    now = datetime.now(timezone.utc).timestamp()
    for v in rec["views"]:
        if v["id"] in seen_ids:
            raise ValueError(f"duplicate view id {v['id']}")
        seen_ids.add(v["id"])
        if v.get("frequency") and v["frequency"] not in FREQUENCIES:
            raise ValueError(f"view frequency {v['frequency']!r} invalid")
        if not v["series"]:
            raise ValueError(f"view {v['id']} has no series")
        for s in v["series"]:
            pts = s["pts"]
            if len(pts) < 2:
                raise ValueError(f"series {s['id']} has {len(pts)} points")
            last = None
            for t, val in pts:
                if not isinstance(t, int) or isinstance(val, bool) or not isinstance(val, (int, float)):
                    raise ValueError(f"series {s['id']}: bad point {[t, val]}")
                if math.isnan(val) or math.isinf(val):
                    raise ValueError(f"series {s['id']}: NaN/inf value")
                if last is not None and t <= last:
                    raise ValueError(f"series {s['id']}: timestamps not strictly ascending at {iso(t)}")
                last = t
            if pts[0][0] < ts(1900) or pts[-1][0] > now + 366 * 86400:
                raise ValueError(f"series {s['id']}: dates out of range ({iso(pts[0][0])}..{iso(pts[-1][0])})")
