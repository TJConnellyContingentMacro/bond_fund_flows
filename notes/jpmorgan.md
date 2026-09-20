# JPMorgan — endpoint discovery

Discovery pass, step 2 of the build order. Verified 2026-09-19/20. Sample fund: JPST.

## Bottom line up front

Clean, well-behaved JSON API, keyed by CUSIP, no bot-blocking. **First issuer found
that directly publishes actual spread duration**, not just an OAS proxy — a real
answer to the SPEC.md §6.5 open question. One gap: shares outstanding isn't a
distinct field anywhere found; it's most likely derived client-side from TNA ÷ NAV,
same as SPEC.md's own fallback formula — meaning the adapter can just compute it
directly rather than needing to reverse-engineer where the page gets it.

## Endpoints (`https://am.jpmorgan.com/FundsMarketingHandler/...`, keyed by CUSIP)

Found via network traffic on the product page — fires early enough in page load
that (unlike Invesco) this session's network log actually caught it directly, no
need to reverse-engineer from embedded config.

- **`/product-data?cusip=<cusip>&country=us&role=adv&language=en&userLoggedIn=false&version=<v>`**
  — one enormous JSON object (nearly 1MB, ~250 top-level keys covering every fund
  type JPMorgan offers, most irrelevant to a given fund) containing, for the bond
  ETF fields that matter:
  - `aum.netAsset` — **total net assets, full precision to the cent**
    (`41712141777.91`), `aum.date` same-day as NAV (`2026-09-18`).
  - `averageLife.data[]` (array of `{name, value, displayValue}` rows,
    `averageLife.effectiveDate` applies to the whole array) — contains `Duration`
    (0.78 yrs), `Average Life` (1.03 yrs), and **`Spread Duration`** (0.93 yrs).
    **This `effectiveDate` lags NAV by ~7 weeks** (`2026-07-31` vs NAV's
    `2026-09-18`) — a much bigger lag than any other issuer's duration figure
    (iShares: T-2 days; SSGA: same-day; Schwab: ~18 days; Vanguard: same-day via a
    separate endpoint). Worth confirming this generalizes rather than being a
    JPST-specific reporting quirk before assuming it for JMUB/JCPB.
  - `numberOfHoldings` (789) / `numberOfHoldingsEffectiveDate` — same day as NAV.
  - `fundValuationDate` — the fund-level as-of date, matches NAV's date.
  - `shareClass.tradingInfo` has real-time-quote-vendor ticker symbols
    (`"nav": "JPST.NV"`, `"sharesOutstanding": "JPST.SO"`,
    `"underlyingTradingValue": "JPST.IV"`) rather than values — these look like
    symbols meant for a live market-data feed (the vendor/API that resolves them
    wasn't chased down; `marketNav` and `currentSharePrice` fields elsewhere in the
    same payload are `null`, consistent with this data living outside this
    particular endpoint).
  - `bondYTM`, `bondYTW` were `null` in this payload despite existing as fields —
    check the page's displayed "Yield to Maturity Net"/"Yield to Worst" values
    against `shareClass.secYield`/`etfSecYield`/`yieldMonthEnd` sub-objects instead,
    which are populated (30-day SEC yield daily via `etfSecYield`, monthly via
    `yieldMonthEnd`).

- **`/historicalData?cusip=<cusip>&country=us&role=adv&userLoggedIn=false&language=en&version=<v>`**
  — `historicalETFNAVMarketPriceList`: **complete daily NAV history since fund
  inception** (JPST: 2296 rows back to 2017-05-17) in one call, each row
  `{date, navPrice (full precision, e.g. 50.3374667), marketValueNavPrice
  (rounded to cents), epochDate}`. Best backfill source of any issuer checked —
  no pagination, no date-range parameter needed, just the whole history in one
  response.

**Shares Outstanding**: displayed on the page as `828,650,000` (as of `09/18/2026`,
same day as NAV) but **not found as a distinct field in either endpoint** despite
checking thoroughly (grepped both raw JSON payloads for the exact figure — no
match). It's arithmetically identical to `aum.netAsset ÷ navPrice`
(41,712,141,777.91 ÷ 50.3374667 ≈ 828,650,000), which strongly suggests the
page itself derives and displays this client-side rather than sourcing it as a
distinct published figure — i.e., **JPMorgan's own frontend is doing exactly the
fallback calculation SPEC.md §4.1 describes**, just with genuinely high-precision
inputs (not rounded ones), so replicating it in the adapter carries none of the
"carries the issuer's own rounding" caveat the spec warns about generically.
Flag it as `derived` per the spec's convention anyway, for consistency with other
issuers where the distinction matters more.

## Units

Whole dollars throughout — confirmed by magnitude ($41.7B fund, matches known JPST
AUM).

## robots.txt — a real ambiguity, not a clean permissive/restrictive read

The raw rules for `User-agent: *`:

```
Allow: /*/*/asset-management/
Disallow: /asset-management/
Disallow: /*/asset-management/
```

The product page path is `/us/en/asset-management/adv/products/...`. Robots.txt
wildcards (`*`) match across `/` characters, so **both** the `Allow` rule (treating
`*` `*` as matching `us` then `en`) **and** the second `Disallow` rule (treating a
single `*` as matching `us/en` together) technically match this URL. Under the
standard robots.txt precedence rule (most specific — i.e. longest — matching
pattern wins, which is what Google's spec and most modern parsers implement), the
`Allow` pattern (`/*/*/asset-management/`, 22 characters) is longer than the
matching `Disallow` pattern (`/*/asset-management/`, 20 characters), so **the
product page should be considered allowed**. But this is a real ambiguity in the
site's own robots.txt authoring, not a clean-cut answer, and not every parser
implements longest-match-wins identically. Noting the reasoning explicitly rather
than just asserting an answer.

Separately: `Allow: /` with a 10-second crawl-delay is explicitly granted to a long
list of AI crawlers (GPTBot, ClaudeBot, PerplexityBot, etc.) and to Bingbot/Slurp/
DuckDuckBot/ia_archiver — none of which is what this project's daily cron would
identify as, but it signals the site operator is generally comfortable with
automated access at a reasonable rate, for whatever that's worth alongside the
literal rule-matching analysis above.

## Terms of Use

`am.jpmorgan.com/us/en/asset-management/adv/terms-of-use` — read in full. **No
robot/bot/crawler/scraper/automated-access clause anywhere.** Only a general
copyright/reproduction restriction on copying or redistributing site content
without written consent — the same low-risk category as SSGA, not the
BlackRock/Invesco-style blanket automation prohibition.

## Open questions for the next pass on JPMorgan

- Confirm the `/product-data` and `/historicalData` endpoints work the same way,
  keyed by CUSIP, for JMUB and JCPB (only JPST checked).
- Confirm whether the ~7-week duration/spread-duration reporting lag is a JPST
  quirk (it's an ultra-short active fund; monthly characteristics reporting is
  plausible for that category) or holds for JMUB/JCPB too.
- Never resolved what vendor/endpoint the `JPST.NV`/`JPST.SO`/`JPST.IV` ticker
  symbols in `tradingInfo` point to — not pursued further since `historicalData`
  already gives a better (full-precision, complete-history) NAV source directly.
