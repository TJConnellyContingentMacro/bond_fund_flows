# iShares — endpoint discovery

Discovery pass, step 2 of the build order. Verified 2026-09-19. Do not treat any URL
below as stable — these are undocumented, unversioned endpoints and can change without
notice.

## Sources

### 1. Per-fund product page (primary source — has shares outstanding)

`https://www.ishares.com/us/products/<portfolioId>/<slug>`

e.g. `https://www.ishares.com/us/products/239454/ishares-20-year-treasury-bond-etf` (TLT)

Found by: loading the page in a browser and inspecting the raw HTML response (no JS
execution required — data is server-rendered, not fetched client-side). The needed
values live inside HTML-entity-encoded JSON blobs in `componentprops="{...}"`
attributes on `<walrus-render-on-client data-componentname="...">` tags. A plain
`GET` + regex/HTML-unescape + `json.loads` is sufficient; no headless browser needed
in the adapter itself.

Relevant blocks (`data-componentname` → JSON path → field):

- **`KeyFundFactsV3`** → `dataPoints.<name>`:
  - `sharesOutstanding` — formatted string only, e.g. `"575,600,000"`. **This is the
    only place iShares directly publishes shares outstanding.** No raw numeric field;
    parse the formatted string (strip commas).
  - `totalNetAssetsFundLevel` — formatted string, e.g. `"46,712,958,537"`. Whole
    dollars (confirmed against TLT $46.7B and GNMA $435M — no thousands/millions
    ambiguity for this issuer).
  - `closingPrice`, `cusip`, `distributionFrequency`, `exchange`, `indexTicker`,
    `indexSeriesName`, `launchDate`, `premiumDiscountClosingPriceNavPercent`,
    `thirtyDayAverageVolume`, `thirtyDayMedianBidAskSpread`, `consolidatedVolume`.
  - Each field has `formattedAsOfDate` (e.g. `"Sep 18, 2026"`) but no epoch/int date —
    parse with `%b %d, %Y`.

- **`PortfolioCharacteristicsV3`** → `dataPoints.<name>`:
  - `modelOad` — **this is effective duration**, e.g. `"15.05 yrs"`. Strip `" yrs"`.
  - `optionAdjustedSpread` — e.g. `"-0.05 bps"`. **No true spread-duration field
    exists anywhere in this issuer's data.** OAS is the closest available proxy;
    per SPEC.md §6.5, any use of it as a spread-duration substitute must be flagged,
    not laundered into the aggregate.
  - Also present: `numHoldings`, `convexity`, `weightedAvgLife` (weighted avg
    maturity), `weightedAvgCouponFi`, `standardDeviation3Yr`, `thirtyDaySecYield`,
    `twelveMonTrlYld`, `beta3Yr`, `fxHedgedYield` (yield to maturity, name is
    misleading). All string-formatted, `formattedAsOfDate` only.

- **`fundHeader`** → `containersByNameMap.fundNav.dataPointsByNameMap.navAmount`:
  - **Best NAV source.** Raw float with full precision (`"value": 81.155244`, not the
    rounded `"81.16"` shown on the page) plus `asOfDate` as an int `YYYYMMDD`
    (`20260918`) — cleaner to parse than the formatted-string fields elsewhere on the
    page. Sibling fields in the same container: `navAmountChange`, `percentChange`,
    `week52HighNav`, `week52LowNav`, `yearToDate`, plus Morningstar rating fields.

**Quirk:** `productPageUrl` slugs can be stale after a fund rename (e.g. AGG's slug
is `ishares-core-total-us-bond-market-etf`, PFF's is `ishares-us-preferred-stock-etf`
— both older names). Confirmed via `isin`/`fundName` that the underlying fund is
correct in both cases. **Key on `portfolioId` (or `isin`/`cusip`), never on the URL
slug text.**

### 2. Bulk product-screener JSON (secondary — no shares outstanding, but one request covers everything else for all 40 tickers)

```
https://www.ishares.com/us/product-screener/product-screener-v3.1.jsn?dcrPath=/templatedata/config/product-screener-v3/data/en/us-ishares/ishares-product-screener-backend-config&siteEntryPassthrough=true
```

Found by: watching network traffic while loading the iShares ETF screener page
(`/us/products/etf-investments`).

Single GET, ~1.9MB, returns **all 524 iShares products globally** (every asset
class/region — not just US fixed income), keyed by `portfolioId`. Every numeric field
comes as `{"d": "<formatted>", "r": <raw>}` — use `r`, it's already a clean float/int,
no string parsing needed. Confirmed fields for a fixed-income fund (TLT):
`navAmount`/`navAmountAsOf`, `totalNetAssets`/`totalNetAssetsFundAsOf`,
`effectiveDuration` (aliased identically as `cleanDuration`, `modelOad`,
`modelOad_effectiveDuration`), `optionAdjustedSpread`/`optionAdjustedSpreadAsOf`,
`cusip`, `isin`, `fees`, `inceptionDate`, `productPageUrl`.

**Confirmed absent:** `sharesOutstanding` — grepped the full response, zero matches.
This endpoint cannot replace the per-fund page for the flow calculation; it's useful
as a cheap cross-check / universe-mapping tool (used it to resolve all 40 iShares
tickers in `universe.csv` to their `portfolioId` in a single request — 100% matched,
listed below) but shares outstanding still requires the 40 individual page hits.

**Adapter implication:** ~41 requests/day total for full iShares fixed-income
coverage (1 bulk screener + 40 per-fund pages) — trivial volume, sequential is fine.

### Known trap (already in SPEC.md, reconfirmed)

`/us/literature/cashflows/<ticker>-etf-cash-flows.csv` — bond cashflow *projections*
(future scheduled interest/principal, `CALL_TYPE` of `WORST`/`MATURITY`), not fund
flows. Not touched during this discovery pass.

## Timing / lag (single observation — needs confirmation over multiple days)

On this pull, NAV / shares outstanding / TNA were all stamped **T-1** ("Sep 18, 2026"
when pulled on Sep 19), while effective duration / OAS / yield metrics were stamped
**T-2** ("Sep 17, 2026") — a full extra business day of lag on the portfolio
characteristics block versus the NAV/key-facts block. **Exact hour of update is not
yet confirmed** — the T-1 data was already present whenever this pull ran; pinning
the exact refresh hour needs same-day polling at multiple times, which is a job for
the actual cron once it's running, not this reconnaissance pass.

## Trade-date vs. settlement-date convention

**Not determinable from a single snapshot.** Needs a known creation/redemption event
and comparison of its trade date against the day shares outstanding actually moves.
Revisit once a few weeks of daily history have accumulated. Documenting the ambiguity
per SPEC.md §5.2 rather than guessing.

## robots.txt / Terms of Use

- **robots.txt**: permissive. `/us/products/` and `/us/product-screener/` are not
  disallowed. Only blocks: `/*?truepdf*`, `/*?norepdf*`, `/*.dl$`, auth/SSO paths,
  `/search/`, `/us/MYCATEGORYURL`. No crawl-delay directive.
- **Terms of Use** (iShares.com links out to BlackRock's corporate terms,
  `blackrock.com/corporate/compliance/terms-and-conditions`): contains a blanket
  prohibition —

  > "Use any robot, spider, intelligent agent, other automatic device, or manual
  > process to search, monitor or copy this Website or the reports, data,
  > information, content, software, products services, or other materials on...
  > this Website... without BlackRock's permission"

  No personal-use/research carve-out, no stated rate limit. **Flagged to T.J.
  2026-09-19; decision made: proceed anyway** for this low-volume (41 req/day),
  single-user, non-redistributed research use. Revisit if usage or distribution scope
  changes (e.g. sharing the dashboard outside personal use, scaling request volume).

## Ticker → portfolioId map (all 40 iShares tickers in universe.csv, resolved via the bulk screener, 100% match)

| ticker | portfolioId | productPageUrl |
|---|---|---|
| SGOV | 314116 | /us/products/314116/ishares-0-3-month-treasury-bond-etf |
| SHV | 239466 | /us/products/239466/ishares-short-treasury-bond-etf |
| TFLO | 260652 | /us/products/260652/ishares-treasury-floating-rate-bond-etf |
| ICSH | 258806 | /us/products/258806/ishares-liquidity-income-etf |
| NEAR | 239854 | /us/products/239854/ishares-short-maturity-bond-etf |
| SHY | 239452 | /us/products/239452/ishares-13-year-treasury-bond-etf |
| IEI | 239455 | /us/products/239455/ishares-37-year-treasury-bond-etf |
| IEF | 239456 | /us/products/239456/ishares-710-year-treasury-bond-etf |
| GOVT | 239468 | /us/products/239468/ishares-us-treasury-bond-etf |
| TLT | 239454 | /us/products/239454/ishares-20-year-treasury-bond-etf |
| TLH | 239453 | /us/products/239453/ishares-1020-year-treasury-bond-etf |
| GOVZ | 315911 | /us/products/315911/ishares-25+-year-treasury-strips-bond-etf |
| TIP | 239467 | /us/products/239467/ishares-tips-bond-etf |
| STIP | 239450 | /us/products/239450/ishares-05-year-tips-bond-etf |
| MBB | 239465 | /us/products/239465/ishares-mbs-etf |
| GNMA | 239461 | /us/products/239461/ishares-gnma-bond-etf |
| LQD | 239566 | /us/products/239566/ishares-iboxx-investment-grade-corporate-bond-etf |
| IGSB | 239451 | /us/products/239451/ishares-13-year-credit-bond-etf |
| IGIB | 239463 | /us/products/239463/ishares-intermediate-credit-bond-etf |
| IGLB | 239423 | /us/products/239423/ishares-10-year-credit-bond-etf |
| USIG | 239460 | /us/products/239460/ishares-credit-bond-etf |
| QLTA | 239431 | /us/products/239431/ishares-aaa-a-rated-corporate-bond-etf |
| HYG | 239565 | /us/products/239565/ishares-iboxx-high-yield-corporate-bond-etf |
| USHY | 291299 | /us/products/291299/ishares-broad-usd-high-yield-corporate-bond-etf |
| SHYG | 258100 | /us/products/258100/ishares-05-year-high-yield-corporate-bond-etf |
| FALN | 283855 | /us/products/283855/ishares-fallen-angels-usd-bond-etf |
| EMB | 239572 | /us/products/239572/ishares-jp-morgan-usd-emerging-markets-bond-etf |
| MUB | 239766 | /us/products/239766/ishares-national-amtfree-muni-bond-etf |
| SUB | 239772 | /us/products/239772/ishares-shortterm-national-amtfree-muni-bond-etf |
| CMF | 239731 | /us/products/239731/ishares-california-amtfree-muni-bond-etf |
| NYF | 239767 | /us/products/239767/ishares-new-york-amtfree-muni-bond-etf |
| AGG | 239458 | /us/products/239458/ishares-core-total-us-bond-market-etf |
| IUSB | 264615 | /us/products/264615/ishares-core-total-usd-bond-market-etf |
| IAGG | 279626 | /us/products/279626/ishares-international-aggregate-bond-etf |
| IGOV | 239830 | /us/products/239830/ishares-international-treasury-bond-etf |
| TLTW | 329118 | /us/products/329118/ishares-20+-year-treasury-bond-buywrite-strategy-etf |
| HYGW | 329119 | /us/products/329119/ishares-high-yield-corporate-bond-buywrite-strategy-etf |
| LQDW | 329120 | /us/products/329120/ishares-investment-grade-corporate-bond-buywrite-strategy-etf |
| ICVT | 272819 | /us/products/272819/ishares-convertible-bond-etf |
| PFF | 239826 | /us/products/239826/ishares-us-preferred-stock-etf |

## Sample raw responses

Pulled and inspected in full during this session (TLT, SHY, MUB, GNMA product pages;
the bulk screener JSON). Not saved to the repo — `data/raw/` is for the real daily
pipeline, not discovery scratch. Re-pull to re-verify if needed before building the
adapter.

## Open questions for the adapter build (not this pass)

- Exact daily update hour — needs same-day multi-point polling once the cron exists.
- Trade-date vs. settlement-date convention — needs a few weeks of accumulated
  history around a real creation/redemption event.
- Whether the bulk screener's `optionAdjustedSpread` should be wired up now as a
  flagged spread-duration proxy (per SPEC.md §6.5), or left null until phase 2 —
  defer to the adapter-build session.
