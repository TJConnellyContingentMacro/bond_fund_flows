# Vanguard — endpoint discovery

Discovery pass, step 2 of the build order. Verified 2026-09-19. Sample fund checked
in depth: BND. Not yet cross-checked against a second fund (e.g. a pure-Treasury or
muni fund) — see open questions before assuming this generalizes to all 16 Vanguard
tickers in `universe.csv`.

## Bottom line up front

**There are two Vanguard sites with very different data, and the retail one
(`investor.vanguard.com`) is the wrong one to build on.** The financial-advisor site
(`advisors.vanguard.com`) exposes a full JSON REST API with daily, full-precision
NAV and duration, and exact (not rounded) shares outstanding — but shares outstanding
updates **once a month**, not daily, confirmed directly from the source rather than
inferred. This is a structural/regulatory limit (Vanguard's ETFs are a share class of
the underlying mutual fund and inherit its monthly disclosure cadence), not a missing
scraper. **The core flow identity cannot be computed daily for Vanguard from any
public source found so far** — this still needs your decision, but now with a
confirmed answer instead of an open question.

## Two sites, two different data surfaces

### `investor.vanguard.com` (retail) — page discovered first, turned out to be the weaker source

Single server-rendered JSON blob per page (`data-vgn-funds-profile="{...}"` attribute,
HTML-entity encoded). Daily NAV (`overview.navPrice`, 2-decimal string) and NAV
history (`price.historicalPrice.1m.nav`, at least 1 month embedded). **No shares
outstanding field anywhere.** Total net assets only appears in a monthly, rounded
block (`portfolioComposition.characteristics.fixedIncomeCharacteristic.fund.totalNetAssets`,
e.g. `"$398.9 billion"`, `asOfDate` monthly) — not enough precision or cadence to use.
`averageDuration` here is also monthly and rounded (`"5.7 years"`).

### `advisors.vanguard.com` (financial-professional site) — the actual data source to build on

Ticker resolves to a Vanguard-internal numeric `fundId` first (BND → `0928`; found
via the retail site's `dashboard.associatedFundIds.etfFundId`, or by loading the
advisor page directly at `/investments/products/<ticker>/<slug>` and reading network
traffic). All data lives under clean, unauthenticated REST endpoints:
`https://advisors.vanguard.com/investments/products/api/funds/<fundId>/<path>`. No
login required — all returned 200 without a session. robots.txt does not block
`/investments/products/` or any `/api/` path (it blocks `/sign-in/`,
`/content/dam/fas/`, `/engagement/`, `/assets/corp/`, `/partners/` — none of which we
need). Endpoints confirmed useful:

- **`pricing/closing`** → `{"nav":{"price":71.16,"effectiveDate":"2026-09-18",...},
  "marketPrice":{...}}` — daily NAV, full float, cleanest NAV source of the two sites.
- **`pricing/outstanding-shares`** → `{"effectiveDate":"2026-08-31",
  "outstandingShares":2261123018}` — **exact integer, not rounded.** But
  `effectiveDate` is a month-end date, confirming this updates monthly. This is the
  one field that actually gates the flow calculation, and it's the one field that's
  slow here.
- **`analytics/daily-fixed-income?dateRange=<start>:to:<end>&isMunicipalFixedIncome=
  &isInflationProtectedSecurities=&isTaxExemptBond=`** → `averageDuration`,
  `averageCoupon`, `averageEffectiveMaturity`, `yieldToMaturity`, `yieldToWorst`, each
  with its own `effectiveDate`. **This is genuinely daily** (`effectiveDate:
  "2026-09-17"` when pulled on the 19th — a T-2 lag, same shape as iShares' duration
  lag) with full float precision (`"5.70471283182729"`), despite the equivalent
  retail-site figure being monthly and rounded to 2 sig figs. The three boolean flags
  in the query string need to be set correctly per fund type (municipal /
  inflation-protected / tax-exempt) — worth confirming what they do for TIPS (VTIP)
  and muni (VTEB, VTES) tickers before assuming defaults.
- **`analytics/risk`**, **`analytics/yields`**, **`benchmark/portfolio-statistics`**,
  **`exposures/credit-quality`** — supporting analytics, monthly cadence mostly.
- **`holdings/composition`** — per-holding share quantities, no market values in the
  sample pulled — would need a separate price join to derive a bottom-up TNA, which
  is more engineering than this discovery pass justifies. Noting it exists in case
  the "keep digging" path continues later.
- **No net-assets/AUM/TNA endpoint exists anywhere in this API surface** — checked
  every `/api/funds/0928/*` call the page actually makes (confirmed exhaustively via
  two full page loads' network logs) plus several plausible sibling paths by
  analogy to `pricing/outstanding-shares` (`pricing/net-assets`,
  `pricing/total-net-assets`, `pricing/aum`, `fund/net-assets`,
  `analytics/net-assets`, `pricing/fund-facts`) — all 404. Not exhaustive (didn't
  fetch every JS chunk on the page), but this is now a reasonably well-searched
  negative result, not just an unchecked gap.

**Confirms the earlier `notes/vanguard.md` draft was directionally right but
incomplete** — shares outstanding *is* published, precisely, just monthly, and via a
completely different site than the one a first pass would naturally land on.

## What this settles

- Daily flow calc for Vanguard needs `Δshares_outstanding`, and shares_outstanding is
  only observed monthly, from any source found. There is no daily proxy available —
  computing an implied TNA as `outstandingShares(last month-end) × NAV(t)` is
  arithmetically sound but doesn't create new information: it still only reflects a
  share-count change once a month, with every day in between showing zero flow by
  construction, followed by a step change on the update day. This is the same
  structural shape flagged before, just now confirmed rather than inferred.
- Effective duration, unlike shares outstanding, **is** usable daily for Vanguard via
  `analytics/daily-fixed-income` — better than initially thought. Worth capturing
  daily per SPEC.md §6.4 regardless of what's decided about the flow calc, since it
  can't be backfilled later.

## Decision still needed (same three options as before, now on firmer ground)

1. **Monthly-only flow granularity for Vanguard's ~16 tickers** — update
   `shares_outstanding`/`flow_usd` only when `pricing/outstanding-shares`'
   `effectiveDate` advances (roughly monthly), rather than daily.
2. **NAV/duration daily, flow left NULL** — track what's genuinely daily (NAV,
   premium/discount, duration, yields) every day; leave `shares_outstanding` and
   `flow_usd` NULL between month-end prints rather than holding a stale value or
   printing a fake zero.
3. **Keep digging** — the one unexplored avenue left is deriving a bottom-up daily
   TNA from `holdings/composition` (share quantities) joined against daily security
   prices, which is a real engineering project, not a reconnaissance-pass task.

## Open questions for the next pass on Vanguard

- Confirm the `fundId` lookup and this whole API shape generalizes across the other
  15 tickers — checked BND only. In particular, verify whether the
  `isMunicipalFixedIncome`/`isInflationProtectedSecurities`/`isTaxExemptBond` query
  flags on `analytics/daily-fixed-income` change behavior for VTIP, VTEB, VTES, or
  whether they're accepted-but-ignored decoration.
- No `spreadDuration` field found anywhere in either site's data (consistent with
  every other issuer checked so far).
- Whether `advisors.vanguard.com` requires any session/consent cookie for sustained
  daily automated use, or whether these endpoints stay open long-term the same way
  they answered cold, unauthenticated curl requests during this pass.
