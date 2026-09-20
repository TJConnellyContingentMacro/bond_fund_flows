# SSGA (SPDR) — endpoint discovery

Discovery pass, step 2 of the build order. Verified 2026-09-19/20. Sample funds:
BIL (0-3mo T-bill, near-zero duration) and SPTL (20yr+ Treasury, high duration) —
chosen deliberately to see whether fields present/absent differ by fund duration
profile. They do (see below).

## Bottom line up front

Same shape as Vanguard: **two SSGA sites, and the professional one
(`/us/en/intermediary/...`) has materially more data than the retail one
(`/us/en/individual/...`).** Unlike Vanguard, the gap here is closeable — the
intermediary page publishes shares outstanding **daily, same as-of date as NAV**,
just rounded to the nearest ~10,000 shares rather than the exact integer iShares
gives. This is the best-behaved issuer found so far after iShares.

## Two sites, one clearly better for this project

### `ssga.com/us/en/individual/etfs/<slug>` (retail) — good for NAV/AUM, missing shares outstanding and duration entirely

Server-rendered; data lives in `{"label":"...","value":"...","originalValue":"...",
"asOfDate":"..."}` JSON objects scattered through the HTML (HTML-entity encoded,
same technique as the other two issuers: unescape + regex/parse, no JS needed).

- `NAV` and `Assets Under Management` (**AUM = total net assets**) both have a
  `originalValue` field with real precision (`"91.524085"`, `"47893135224.33"`) —
  the displayed value is rounded/formatted (`"$91.52"`, `"$47,893.14 M"`), the
  `originalValue` isn't. **AUM's `originalValue` is already in whole dollars**
  despite the "M" (millions) suffix on the display string — confirmed by magnitude
  (BIL ≈ $47.9B fund, matches `47893135224.33`).
  Both dated same-day (`"as of Sep 17 2026"` when pulled Sep 19/20).
- Sector/credit-quality/maturity-ladder **weight breakdowns** (categorical, not
  aggregate stats).
- **Checked on both BIL and SPTL — zero occurrences of `sharesOutstanding`,
  `unitsOutstanding`, `effectiveDuration`, or `spreadDuration` in any form, on either
  fund.** Not a duration-profile artifact (BIL near-zero duration might plausibly
  omit it; SPTL at 13.84yr duration should be a headline stat and still isn't
  there) — the retail page just doesn't carry portfolio-characteristics or
  shares-outstanding data at all, for any fund type.
- Holdings tab has **per-position market values** (e.g. individual T-bill CUSIPs
  with dollar market values, same as-of date), which is a viable bottom-up
  cross-check on AUM if ever needed, but not required given AUM's `originalValue`
  is already precise.

### `ssga.com/us/en/intermediary/etfs/<slug>` (financial-professional site) — has what's missing

Different HTML shape — plain `<table class="tb-keyvalue">` rows under
`<h2 class="comp-title">SECTION NAME <span class="date">as of DATE</span></h2>`
sections, not JSON blobs. Also unauthenticated — no login needed, 200 on a cold curl.
Same content technique works (fetch + unescape + a small HTML-table regex; no JS
execution required here either).

Confirmed sections and fields (SPTL):

- **"Fund Net Asset Value" section, `as of Sep 17 2026`**: `NAV` ($24.94),
  **`Shares Outstanding` (442.00 M)**, `Assets Under Management` ($11,022.25 M).
  Shares outstanding is on the **same as-of date as NAV** — genuinely daily, not
  monthly like Vanguard. Precision is rounded to the nearest ~10,000 shares (2
  decimals in millions) — no hidden full-precision attribute the way the retail
  page's AUM has `originalValue`. At SPTL's ~442M-share scale that's ~0.002%
  relative rounding — immaterial for the flow calc, unlike Vanguard's monthly gap.
- **"Fund Characteristics" section, `as of Sep 17 2026`** (same day as NAV — no lag,
  unlike iShares' T-2 duration lag or Vanguard's need for a separate daily-analytics
  endpoint): `Number of Holdings`, `Average Coupon`, `Average Maturity in Years`,
  `Average Price`, `Average Yield To Worst`, **`Option Adjusted Duration`** (this is
  the effective-duration-equivalent field — 13.84 years for SPTL), `Yield to
  Maturity`. Each field appears twice in the raw HTML — once for the fund, once for
  the benchmark/index (need to disambiguate which block is which when parsing; the
  fund's own numbers came first in both samples checked, but confirm this holds
  before relying on positional ordering in the adapter).
- **"Fund Net Cash Amount" section**: `Net Cash Amount` ($108,766,201.42) — cash
  drag/creation-basket cash component, not seen on any other issuer so far. Possibly
  useful context, not required for the core flow calc.
- **No `spreadDuration`-equivalent field anywhere** — consistent with every other
  issuer checked.

## Units

Whole dollars once using the retail page's `originalValue` fields — confirmed via
magnitude on both BIL ($47.9B) and SPTL ($11.0B). The intermediary page's displayed
`AUM` and `Shares Outstanding` are in millions (`"M"` suffix, explicit) — do not
assume whole numbers there, unlike iShares.

## robots.txt / Terms of Use

- **robots.txt** (`ssga.com/robots.txt`): permissive for both `/us/en/individual/` and
  `/us/en/intermediary/` fund pages. Blocks specific legacy fund-series paths
  (`/Cash-C`, `/CIF-F`, etc.), `/search-results`, `/site_tour`, `/disclaimers/`, a
  couple of named PDF/document paths — none of which this project touches.
- **Terms and conditions** (`ssga.com/us/en/individual/about-us/terms-and-conditions`):
  **no bot/scraper/crawler/automated-access clause found** — materially different
  from iShares/BlackRock's blanket "no robot, spider, intelligent agent" prohibition.
  Only a general copyright/reproduction restriction ("may not be reproduced, copied
  or transmitted... without SSGA's express written consent"), which is standard
  boilerplate and not specifically aimed at automated retrieval of published price
  data for personal use. Meaningfully lower ToU risk than iShares for this project.

## Open questions for the next pass on SSGA

- Confirm the intermediary page's fund-vs-benchmark field ordering (fund block
  first) holds across more tickers before parsing positionally rather than by a
  more robust anchor.
- Whether the ~10,000-share rounding on `Shares Outstanding` ever matters for the
  smallest SSGA tickers in `universe.csv` (e.g. HYMB, TIPX) the way it clearly
  doesn't for a $11B fund like SPTL — worth a spot check on a smaller fund.
- Whether `ssga.com/us/en/intermediary/` requires any professional-status
  self-certification click-through in a real browser session that a plain `curl`
  bypasses incidentally — the cold curl worked here, but worth confirming this isn't
  fragile (e.g. gated by a referer check or session cookie under normal traffic
  patterns) before relying on it for the daily cron.
