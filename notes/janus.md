# Janus Henderson — endpoint discovery

Discovery pass, step 2 of the build order. Verified 2026-09-19/20. All three Janus
Henderson tickers checked (JAAA, JMBS, JBBB) — unusual for this stage, but the
finding below only became clear by checking more than one fund.

## Bottom line up front

**NAV, Total Net Assets, and Shares Outstanding — the three fields the core flow
calculation actually needs — are empty on every Janus Henderson fund page checked,
while everything else (effective duration, yields, holdings count, portfolio
turnover) renders correctly with real numbers.** This is precise enough to be a
real, isolated data-pipeline problem on Janus Henderson's side, not a scraping
technique issue: confirmed the values are server-rendered as literal empty
placeholders (`-`), not client-side-fetched and failing — the as-of-date labels
render correctly, only the numeric values are missing.

## Site structure (WordPress, no bot-blocking, no JS needed to read what's there)

`https://www.janushenderson.com/en-us/investor/product/<slug>/` (also
`/en-us/advisor/product/<slug>/` — checked both for JAAA, identical behavior).
Standard WordPress site (`wp-content`, `wp-json`, `admin-ajax.php` visible in
network traffic). Everything is server-rendered directly into the HTML — a plain
`curl` gets the same content a browser does, no JS execution needed, confirmed by
comparing curl output to live-rendered browser output field-for-field.

## What's broken (confirmed on all 3 tickers)

| Field | JAAA | JMBS | JBBB |
|---|---|---|---|
| NAV | `-` | `-` | `-` |
| Total Net Assets | `-` | `-` | (not individually re-checked, same page block as JMBS) |
| Shares Outstanding | (not checked) | `-` | (not checked) |

Each shows a correctly-populated `as-of` date (`09/18/2026`) right next to the
empty value — the template resolved which day it should be reporting for, but
whatever feeds the actual number came back empty at the point the page was
rendered.

## What works fine (confirmed on JMBS, spot-checked elsewhere)

- **Effective Duration**: `6.43` years (JMBS), `0.14` years (JBBB) — real values.
- **Yield to Worst**: `6.50%` (JMBS).
- **30-Day SEC Yield (With Waivers)**: `4.82%`, as of `08/31/2026` (JMBS) — monthly
  cadence, consistent with most other issuers' yield fields.
- **Number of Holdings**: `441` (JMBS).
- **Portfolio Turnover Rate**: `91.23%` (JMBS).
- **YTD Return**: `3.47%` (JAAA) — at the top summary widget, right next to the
  broken NAV field.
- Quarterly/monthly/since-inception **performance returns** (NAV return, market
  price return, benchmark return) render correctly in a chart+table on the
  Performance tab.

**The pattern is precise**: everything that looks like it comes from a live daily
pricing/AUM feed is empty; everything that looks like it comes from a
periodically-refreshed portfolio-characteristics or performance-calculation
pipeline works normally. This reads like a specific integration between Janus
Henderson's site and whatever live-pricing data source feeds NAV/AUM/shares is
broken or stalled, separate from the portfolio-analytics pipeline.

## Could not determine: transient outage vs. structural

Only one snapshot in time was available for this discovery pass. Nothing here
distinguishes "Janus Henderson's live-pricing integration happens to be down right
now" from "this is a persistent gap in how their site surfaces this data." The
clean 3-for-3 reproduction across every ticker checked, and the sharp field-level
split (pricing/AUM broken, everything else fine) both look more like a specific
integration outage than three funds' worth of coincidental missing data — but this
needs a re-check on a different day before concluding anything permanent. This is
the most important open item for the next Janus Henderson pass: **re-pull the same
three pages and see if NAV/TNA/shares populate normally.**

## robots.txt / Terms of Use

- **robots.txt**: permissive. Only meaningful disallow is a document-ID path
  (`/prod/documents/docId/`) unrelated to fund product pages.
- **Terms of Use** (`/en-us/investor/legal-information/terms-of-use/`): same
  category of blanket prohibition as iShares/Invesco —

  > "Use devices (including software) that are designed to provide repeated
  > automated access to this website, other than those made generally available
  > by us, or to probe, scan, attempt to gain unauthorized access to this website
  > through hacking, password, or data mining"

  No personal-use carve-out. Noted for consistency with the iShares/Invesco
  findings; treating the same "proceed anyway" stance as carried over unless told
  otherwise.

## Open questions for the next pass on Janus Henderson

- **Re-pull JAAA/JMBS/JBBB on a different day** to determine whether the
  NAV/TNA/shares-outstanding gap is transient or persistent. This is the one
  finding from this pass that a second data point would resolve cleanly.
- If persistent: check whether the fact sheets (PDF, monthly,
  `documents.janushenderson.com/prod/documents/docId/...`) carry NAV/AUM as a
  fallback — untested this pass, and PDF parsing plus monthly cadence would be a
  meaningfully worse source than every other issuer's daily web data, so only
  worth pursuing if the live pages don't recover.
- If it recovers on its own: nothing else needed — the page structure and field
  names (`Total Net Assets`, `Shares Outstanding`, `Effective Duration`) are
  already known and match the row-based `<table>`/`<tr>`/`<td>` layout documented
  above, ready for a normal adapter build.
