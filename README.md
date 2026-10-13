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

## Credit and use

Data credit on every page and in every download: **CMIE Economic Outlook,
1 Finance Research**. This mirror is for personal research; check the source
site's terms before making it public.
