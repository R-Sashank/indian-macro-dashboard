"""Check that every data URL the source modules depend on is reachable from this machine.

Each module in scraper/sources lists its data URLs in PROBE_URLS (a string, or a (url, kwargs)
tuple for extra headers/params). Run locally, or from the "Probe source reachability" GitHub
workflow to learn which hosts block GitHub's servers.

    python scraper/probe_urls.py
"""

import concurrent.futures as cf
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sources  # noqa: E402
from sources.base import USER_AGENT  # noqa: E402

import requests  # noqa: E402


def collect() -> list:
    sources.load_all()
    out = []
    for name, mod in sorted(sys.modules.items()):
        if name.startswith("sources.") and name not in ("sources.base",) and hasattr(mod, "PROBE_URLS"):
            for item in mod.PROBE_URLS:
                url, kw = (item, {}) if isinstance(item, str) else item
                out.append((name.split(".", 1)[1], url, kw))
    return out


def probe(args):
    module, url, kw = args
    t = time.time()
    try:
        with requests.get(url, headers={"User-Agent": USER_AGENT, **kw.get("headers", {})},
                          params=kw.get("params"), timeout=45, stream=True) as r:
            first = next(r.iter_content(2048), b"")
            note = "" if r.status_code < 400 else " <-- not OK"
            return module, url, f"{r.status_code} {r.headers.get('content-type', '?')[:28]:<28} {time.time() - t:4.1f}s{note}", first[:200]
    except Exception as e:  # noqa: BLE001
        return module, url, f"ERR {type(e).__name__}: {str(e)[:70]}", b""


def main() -> int:
    items = collect()
    for name, err in sources.IMPORT_ERRORS.items():
        print(f"IMPORT ERROR sources/{name}.py: {err}")
    if not items:
        print("no PROBE_URLS declared")
        return 1
    with cf.ThreadPoolExecutor(6) as ex:
        results = list(ex.map(probe, items))
    bad = 0
    for module, url, status, _ in results:
        if status.startswith("ERR") or "<-- not OK" in status:
            bad += 1
        print(f"{module:<22} {status:<62} {url}")
    print(f"\n{len(results) - bad} reachable, {bad} not reachable, of {len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
