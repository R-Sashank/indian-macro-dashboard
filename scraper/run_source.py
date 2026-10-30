"""Test harness for the official-source modules. Writes nothing to site/data.

    python scraper/run_source.py                 # list registered indicators
    python scraper/run_source.py upi-transactions gst-eway-bills
    python scraper/run_source.py --module payments   # every indicator in scraper/sources/payments.py
    python scraper/run_source.py --module payments --dump OUTDIR   # also save each record as JSON

Each indicator is built, validated and summarised: per series, point count, date range, latest value.
Exit status is 1 if any builder raised or produced an invalid record.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sources  # noqa: E402
from sources.base import Http, iso, validate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("slugs", nargs="*")
    ap.add_argument("--module", help="module name inside scraper/sources, e.g. payments")
    ap.add_argument("--dump", help="directory to write each record as JSON")
    args = ap.parse_args()

    reg = sources.load_all()
    for name, err in sources.IMPORT_ERRORS.items():
        print(f"IMPORT ERROR in sources/{name}.py: {err}")
    if not args.slugs and not args.module:
        for slug, b in sorted(reg.items(), key=lambda kv: (kv[1].module, kv[0])):
            print(f"{slug:<48} {b.section:<24} {b.module.split('.')[-1]}")
        return 1 if sources.IMPORT_ERRORS else 0

    chosen = [b for b in reg.values() if b.slug in args.slugs or (args.module and b.module.endswith("." + args.module))]
    if not chosen:
        print("no matching indicators")
        return 1
    http = Http()
    bad = 0
    for b in chosen:
        t0 = time.time()
        try:
            rec = b.fn(http)
            validate(rec)
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"FAIL {b.slug}: {type(e).__name__}: {e}")
            continue
        print(f"OK   {b.slug}  [{rec['frequency']}]  {rec['title']}  ({time.time() - t0:.1f}s)")
        print(f"       source: {rec['source']} | {rec['source_url']}")
        for v in rec["views"]:
            for s in v["series"]:
                p = s["pts"]
                print(f"       view {v['id']!r} {v['label']!r} freq={v.get('frequency') or rec['frequency']}: "
                      f"{s['name']} [{s['units']}] {len(p)} pts {iso(p[0][0])} .. {iso(p[-1][0])} latest={p[-1][1]}")
        if args.dump:
            out = Path(args.dump)
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{b.slug}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(chosen) - bad} ok, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
