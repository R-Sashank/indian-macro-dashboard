# India Macro Data

A research mirror of [indiamacroindicators.co.in](https://indiamacroindicators.co.in)
(1 Finance), with the one thing that site lacks: **downloadable data**.
Same categories, same data, same explanatory text; every page also has
**Excel** and **CSV** downloads, and every section has a one-click workbook
of all its indicators.

## What is mirrored (143 pages)

| Section | Pages |
|---|---|
| 1 Finance Macroeconomic Index | 1 (daily index since 1991, current phase, interpretation, outlook, historical events) |
| Subindices | 8 |
| Economic Indicators, 9 categories exactly as the site groups them | 127 (shared indicators appear under every category that lists them) |
| Key Economic Indicators | 5 (CPI urban/rural/combined, Nifty 50 P/E, mutual fund AUM, debt-to-GDP, G-Sec yield curve) |
| Global Market P/E Ratios, India Economic Calendar | 2 tables |

Not mirrored: the dashboard pages (they regroup indicators already here) and
the editorial reports/blogs/authors pages (text, not data).

## How it works

- `scraper/scrape.py` reads the site's category pages for the exact
  categorisation, then fetches every page. Each page embeds its full dataset
  as JSON in its server-rendered payload, so no browser is needed. Output:
  `site/data/index.json` plus one `site/data/i/<slug>.json` per page.
  Files whose data did not change are not rewritten, so a scheduled run only
  produces a diff when something is new.
- Pages whose data is identical to another page's are flagged on-screen
  (currently the source's *NIFTY Bank P/E* and *NIFTY IT P/E* pages).
- `site/` is a static site (no build step): Chart.js for charts, SheetJS to
  build Excel files in the browser so downloads always match the data shown.

## Two layers of data

1. **The mirrored pages** (143): scraped by `scraper/scrape.py` from the page payloads of the source site, categorised as that
   site categorises them. They carry no source credit on screen.
2. **Added indicators from primary sources**: payments and administrative data, power and fuel, IIP and core sectors, vehicles,
   aviation, rail and ports, surveys, financial conditions, trade detail, labour and prices. Each lives in a module under
   `scraper/sources/` that registers one builder per indicator (`@register(...)`, see `scraper/sources/base.py`). Each page names
   its own source beside the chart. Rules for every builder: finest available frequency (daily, then weekly, then monthly; a
   coarser long series is added as a second view); raise instead of returning partial data; keep the previous data if a source
   fails (a warning, never a broken site); TLS verification is never disabled.

Useful commands:

```
python scraper/run_source.py                       # list registered indicators
python scraper/run_source.py --module payments     # build, validate and summarise one module (writes nothing)
python scraper/probe_urls.py                       # is every source URL reachable from here?
python scraper/scrape.py --no-added                # only the mirrored pages
```

The "Probe source reachability" workflow (Actions tab) runs the same URL check from GitHub's servers, because some Indian
government hosts block overseas cloud servers. A source that works on a PC can still fail in the scheduled job.

## Run locally

```
pip install -r requirements.txt
python scraper/scrape.py                      # refresh all data (~2-3 min)
python scraper/scrape.py --only gold-price    # refresh just some pages
python -m http.server 8043 --directory site   # then open http://localhost:8043
```

`update.ps1` does the scrape step; schedule it with Windows Task Scheduler
if you want local auto-refresh.

## Staying current (and deploying to Netlify)

1. The project lives in a public GitHub repository.
2. `.github/workflows/update-data.yml` re-scrapes every 2 hours and commits
   only real changes (e.g. the day GST for the next month appears).
3. Netlify deploys `site/` once (publish directory `site`, no build command).
   The deployed site reads its data from the repo through
   `raw.githubusercontent.com` (see `site/config.js`), so a data refresh never
   needs a redeploy. `netlify.toml` skips deploys for commits that only touch
   `site/data`, which keeps usage inside Netlify's free plan; the bundled data
   is only a fallback if GitHub is unreachable.

Only code changes (anything in `site/` outside `site/data`) redeploy the site.

## Use

Private research dashboard (it is behind a Netlify login). Indicators from primary sources name their source on the page and in
the Info sheet of every download.
