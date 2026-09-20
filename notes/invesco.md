# Invesco — endpoint discovery

Discovery pass, step 2 of the build order. Verified 2026-09-19/20. Sample funds:
GSY (active ultra-short, near-zero duration) and PCY (EM sovereign debt, longer
duration) — both used to confirm the API shape generalizes.

## Bottom line up front

Invesco has a real, clean, unauthenticated JSON REST API (`dng-api.invesco.com`)
serving NAV, shares outstanding, and total net assets **all in one daily call, with
full float precision** — the best single-endpoint result of any issuer checked so
far, better than needing two sites (Vanguard, SSGA) or two calls (iShares) to
assemble the same picture. One gap: couldn't isolate the source of the displayed
"Effective Duration" / "Modified Duration" / "Weighted avg life" figures despite
real effort — noted as an open question rather than guessed at.

## The fund page and its embedded API config

`https://www.invesco.com/us/en/financial-products/etfs/<slug>.html` — the individual
investor product page. First visit requires clicking through a one-time
"Confirm your role" interstitial (Individual Investor / Financial Professional /
Institutional) — cosmetic, not a login; doesn't block a plain `curl`.

The page is server-rendered with **empty-valued** JSON schemas for each data widget
(e.g. `"sharesOutstanding":{"fieldType":"compactNumber","text":"Shares
outstanding"}` with no `"value"`), plus the **API URL templates** those widgets use
to fetch real data client-side. That means a plain `curl` of the HTML won't give you
the numbers, but it hands you the exact API surface for free — found by
grep'ing the HTML for `"...Url":"https://dng-api...` patterns rather than by
reverse-engineering browser network traffic (traffic capture kept missing the
actual data calls, which fire very early in page load, before this tool's network
log buffer catches them — the URL-template approach in the HTML sidestepped that
entirely and is more reliable to redo).

Each fund needs its **CUSIP** (not ticker) to query the API — found in the same
HTML as `"cusip":"<value>"`, e.g. GSY → `46090A887`, PCY → `46138E784`. `idType=cusip`
is passed on every call.

## Confirmed endpoints (`https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/<cusip>/...`)

- **`/prices?idType=cusip&variationType=priceListing&productType=ETF`** — the one
  that matters most. Single daily call returns, all in one JSON object:
  `nav` (full float, e.g. `50.097956`), `sharesOutstanding` (integer, e.g.
  `81200000` — appears rounded to the nearest 100,000, not exact-to-the-share like
  iShares), `marketValue` (this is **total net assets**, full precision, e.g.
  `4067953996.11`), `effectiveDate` (e.g. `"2026-09-18"`), plus `closingPrice`,
  `openingPrice`, `medianBidAskSpread`, `30dayAverageTradingVolume`. Confirmed on
  both GSY and PCY — same shape, same precision. **This alone gives everything the
  core flow identity needs, from one call, no fallback math required.**
- **`/navs?idType=cusip&productType=ETF`** — NAV history, going back to fund
  inception (`startDate` in the response was the actual launch date for GSY) — free
  backfill source, more generous range than either Vanguard or SSGA offered.
- **`/keyStats?idType=cusip&productType=ETF`** — returns far less than its schema
  advertises: only `ytd` and `secYield30Day` on both funds checked, despite the
  page's schema listing `assetsUnderManagement`, `umbrellaAum`, `mgmtFee`,
  `effectiveDuration`, `esgFundRating`, `yieldToMaturity`, `totalNetAssets`,
  `sharesOutstanding`, `nav` as possible fields. Either those fields are genuinely
  unpopulated for US bond ETFs specifically, or they need a parameter this pass
  didn't find — don't rely on this endpoint for anything but yield/YTD.
- **`/shareclasses/<cusip>?expand=nav&idType=cusip&variationType=yieldInformation&productType=ETF&managementFeeWaiver=0.0`**
  — `secYield30Day`, `distributionYield`, `twelveMonthDistributionRate`. Yield
  detail, not duration.
- **`/holdings/fund?idType=cusip&productType=ETF`** — full daily per-security
  holdings (GSY: 418 positions) with `units`, `percentageOfTotalNetAssets`,
  `marketValueBase`, `coupon`, `maturityDate`, `spMoodysRating`, CUSIP per holding.
  `effectiveDate` and a separate `effectiveBusinessDate` (one day apart — worth
  understanding which is the true as-of before using this for anything
  date-sensitive). This is a genuine daily 6c-11 holdings file and could support a
  bottom-up weighted-average duration/maturity/coupon calculation if the
  page-displayed figures' source endpoint never turns up.

## What's on the page but NOT yet traced to an endpoint

The product page's "Overview" section visibly displays **Effective duration,
Modified duration, Yield to Maturity, Yield to Worst, Years to Maturity, Weighted
avg life, Weighted avg coupon** (e.g. GSY: 0.78yrs effective duration, 4.68% YTM) —
confirmed by scrolling to and screenshotting the live rendered page. Tried: static
HTML search (not there — confirmed JS-fetched), `/keyStats` (doesn't return it),
`/shareclasses/<cusip>?...variationType=yieldInformation` (yield only, no
duration), `/holdings/fund` (per-security, no precomputed portfolio-level
aggregate). None of the `dng-api` URL templates found in the page's own config
matched this data. **Left as an open question** rather than continuing to guess
undocumented parameter combinations — the holdings file is a viable fallback path
(compute weighted averages bottom-up) if this isn't resolved next pass.

## Units

Whole dollars and whole shares throughout on `/prices` — confirmed by magnitude
(GSY: 81.2M shares × $50.10 NAV ≈ $4.07B, matches `marketValue` exactly).

## robots.txt / Terms of Use

- **`invesco.com/robots.txt`**: no restriction on ETF product pages; blocks some
  legacy fund-series paths, a handful of query-string patterns (`asOfDate`,
  `startDate`, etc. — none of which this project's calls use), `/search-results`,
  and closed-end/money-market/mutual-fund performance pages not relevant here.
- **`dng-api.invesco.com/robots.txt`**: doesn't exist (404) — no crawler
  restrictions declared on the API host at all.
- **Terms of Use** (`invesco.com/us/en/resources/terms-of-use.html`), Section 10:
  same category of blanket prohibition as iShares/BlackRock —

  > "Use devices (including software) that are designed to provide repeated
  > automated access to the Website other than those made generally available by
  > Invesco" ... "use any data mining, robots, or similar data gathering and
  > extraction tools on the Website."

  No personal-use carve-out, no stated rate limit — same risk shape as iShares,
  which was already flagged and accepted for that issuer. Flagging here for
  consistency rather than re-litigating; assuming the same "proceed anyway" stance
  carries over unless told otherwise.

## Open questions for the next pass on Invesco

- Find the actual source of the Overview section's duration/maturity/coupon
  figures, or commit to computing them bottom-up from `/holdings/fund`.
- Confirm `sharesOutstanding`'s apparent 100,000-share rounding on `/prices` holds
  (or doesn't) across the other 4 Invesco tickers — not confirmed as a general
  pattern from 2 samples.
- `/holdings/fund`'s two dates (`effectiveDate` vs `effectiveBusinessDate`, one day
  apart) need disambiguating before use in the settlement-date-convention work in
  SPEC.md §5.2.
- Only 5 Invesco tickers in `universe.csv` (GSY, ICLO, BKLN, PCY, PZA) — cheap to
  re-verify all 5 once an adapter is being built, unlike the 20+ ticker issuers.
