# US Bond Fund & ETF Flow Model — Build Spec

**Owner:** T.J. Connelly
**Drafted:** 2026-09-19
**Target runtime:** Claude Code repo on an always-on machine, daily cron
**Data tier:** Free issuer sources, accumulating forward from day one

---

## 1. Purpose

A daily, fund-level dataset of flows into and out of US fixed income ETFs, with a weekly
mutual fund overlay and a look-through view of agency MBS demand through the ETF channel.

**Primary lens (v1): total dollars into and out of fixed income** — headline, by sleeve,
and normalized by fund size. The question is *how much money is moving into the asset
class and where within it*, not yet how much duration that represents.

DV01 and spread-DV01 (§6.4) are built later, against accumulated data. They are the
natural second act, not the v1 deliverable.

### Explicit non-goals

- This is not a measure of total bond market demand. US fixed income ETFs are a small
  single-digit share of the roughly $60T US bond market. Treat every output as a
  marginal-demand and sentiment indicator, never as the demand picture.
- This is not a daily mutual fund flow model. That data does not exist publicly at any
  granularity (see §7). Anything at fund level and daily frequency is ETF-only.

---

## 2. Repo layout

```
bondflows/
  CLAUDE.md                 # project conventions, read by Claude Code every session
  SPEC.md                   # this file
  pyproject.toml
  src/bondflows/
    sources/                # one adapter per issuer, uniform interface
      base.py               # SourceAdapter ABC
      ishares.py
      vanguard.py
      ssga.py
      invesco.py
      schwab.py
      jpmorgan.py
      vaneck.py
      ici.py                # weekly xls, different cadence
    universe.py             # ticker list + sleeve taxonomy, version-controlled
    ingest.py               # orchestrates adapters, writes raw parquet
    flows.py                # ΔSO × NAV, all adjustments
    analytics.py            # OGR, DV01, z-scores, MBS look-through
    validate.py             # the harness in §9
    db.py                   # DuckDB schema + loaders
  data/
    raw/YYYY/MM/DD/         # immutable snapshots, never overwritten
    bondflows.duckdb        # derived, fully rebuildable from raw/
  tests/
  scripts/
    daily.py                # cron entrypoint
    backfill_gaps.py
```

**Hard rule: `data/raw/` is append-only and never edited.** Every derived number must be
reproducible from raw by rerunning the transform. When you find a bug in the split
adjustment logic in month nine, you rebuild the whole history rather than discovering
that you silently corrupted it in month three.

---

## 3. Data model

### `fund_daily` — one row per ticker per day, raw observations

| column | type | notes |
|---|---|---|
| `ticker` | VARCHAR | |
| `asof_date` | DATE | the date the issuer labels the observation |
| `retrieved_at` | TIMESTAMP | when we actually pulled it |
| `shares_outstanding` | BIGINT | |
| `nav_per_share` | DECIMAL(18,6) | |
| `total_net_assets` | DECIMAL(18,2) | issuer-reported, used as a cross-check |
| `market_close` | DECIMAL(18,6) | for premium/discount only, never for flow |
| `effective_duration` | DECIMAL(8,4) | nullable, issuer-reported |
| `spread_duration` | DECIMAL(8,4) | nullable, rarely published; may need proxying |
| `source` | VARCHAR | adapter name |
| `source_is_stale` | BOOLEAN | see §5 staleness detection |

### `fund_flows` — derived

`ticker`, `flow_date`, `flow_usd`, `shares_delta`, `shares_delta_adjusted`,
`organic_growth_rate`, `dv01_usd_per_bp`, `spread_dv01_usd_per_bp`, `flag` (enum:
`clean`, `stale_prior`, `split_adjusted`, `imputed`, `suspect`).

### `universe` — slowly changing dimension

`ticker`, `name`, `issuer`, `sleeve`, `sub_sleeve`, `inception_date`, `delisting_date`,
`valid_from`, `valid_to`. Versioned so that a reclassification does not silently rewrite
history.

### Sleeve taxonomy (v1)

`ust_ultrashort`, `ust_short` (1-3y), `ust_intermediate` (3-10y), `ust_long` (10y+),
`tips`, `agency_mbs`, `ig_corp`, `hy_corp`, `loans_clo`, `em_debt`, `muni`,
`aggregate` (multi-sector index), `active_multisector`, `other`.

Keep `aggregate` separate rather than decomposing it in the primary table. The
decomposition happens in the look-through layer (§6.4) so both views stay available.

---

## 4. Ingestion

### 4.1 The regulatory floor

SEC Rule 6c-11 (the ETF Rule) requires ETFs to publish daily on their websites: NAV,
market closing price, premium/discount, bid-ask spread, and portfolio holdings. This is
the reason a free daily pipeline is viable at all.

*Inference, verify before relying on it:* I believe shares outstanding is published daily
by every major issuer as a matter of practice, but I have not confirmed that 6c-11
mandates it specifically. If any issuer turns out not to publish it, the fallback is
`total_net_assets / nav_per_share`, which is arithmetically equivalent but carries the
issuer's own rounding. Flag any fund sourced that way.

### 4.2 Task one of the build: endpoint discovery

Do not take endpoint URLs from this spec or from any blog post — issuer endpoints are
undocumented, unversioned, and change without notice. The first work item is a
reconnaissance pass, per issuer, that:

1. Identifies the actual daily-updating source (product screener JSON, per-fund AJAX
   CSV, or the fund page itself).
2. Records the field names and their units. Units are not consistent across issuers —
   net assets may be in whole dollars, thousands, or millions.
3. Confirms update timing: what hour the file changes, and whether the `asof` date in the
   file is the prior business day.
4. Checks the site's terms of use and robots.txt. Rate-limit politely (one request per
   fund per day, sequential, identified user agent). This is a one-request-per-day-per-fund
   workload, which should be unobjectionable, but confirm rather than assume.

**Known trap:** iShares publishes CSVs at
`/us/literature/cashflows/<ticker>-etf-cash-flows.csv`. These are **bond cashflow
projections** — scheduled interest and principal by future date, with `CALL_TYPE` of
`WORST` or `MATURITY` — not fund flows. Verified 2026-09-19. Do not wire these up.

### 4.3 Adapter contract

Every issuer adapter implements the same interface and returns the same normalized
record. An adapter failure must be isolated: if Invesco's site changes shape, the job
logs a hard error for Invesco tickers, writes nothing for them, marks the day as
incomplete for those funds, and **still completes normally for everyone else.** One
broken issuer must never poison the run or, worse, silently write zeros.

Zero is a real value in this dataset. A missing observation must never be coerced to zero
anywhere in the pipeline.

---

## 5. Flow calculation and adjustments

### 5.1 Core identity

```
flow_usd(t) = (shares_outstanding(t) - shares_outstanding(t-1)) * nav_per_share(t)
```

Use NAV, never market close. In fixed income the premium/discount is large enough and
persistent enough that using price materially corrupts the series.

The convention choice — `nav(t)`, `nav(t-1)`, or the average — is second-order but must be
documented and constant. Use `nav(t)`. Note it in CLAUDE.md so it does not drift.

### 5.2 Adjustments, in order of how much damage they do if skipped

**Share splits and reverse splits.** A 1-for-4 reverse split produces an apparent 75%
redemption. This is the single most destructive unhandled case. Detect candidates
automatically: any day where `|Δshares| / shares(t-1)` exceeds ~20% **and**
`nav(t) / nav(t-1)` moves inversely by a comparable ratio is almost certainly a split, not
a flow. Flag for review; do not auto-apply silently. Maintain a version-controlled
`splits.csv` of confirmed corporate actions.

**Settlement-date vs trade-date attribution.** Issuers differ on whether posted shares
outstanding reflects the trade date or the settlement date of creations. Since the move to
T+1 settlement, a one-day misattribution across issuers will systematically smear your
sleeve aggregates. Determine the convention per issuer during discovery, record it in the
adapter, and normalize everything to trade date. If an issuer's convention cannot be
determined, document the ambiguity rather than guessing.

**Stale files.** An issuer posting an unchanged file produces a false zero flow followed
by a lumpy catch-up that looks like a large one-day flow. Detection: if the raw payload
hash is identical to the prior day, or `asof_date` did not advance, set
`source_is_stale = TRUE`, emit no flow for that day, and on the next fresh observation
distribute the multi-day change across the gap — or, preferably, mark the whole span
`imputed` and exclude it from z-score calibration. Never let a catch-up print become a
signal.

**Launches, closures, delistings.** Seed capital at launch is not a flow. Suppress flows
for the first 5 business days after inception. On delisting, terminate the series rather
than printing the final liquidation as a massive outflow, which would otherwise drag the
sleeve aggregate. Survivorship bias is real here: keep dead funds in the history.

**Distributions.** Reinvested distributions do not change shares outstanding for ETFs, and
the ex-date NAV drop is captured correctly by the identity above. No adjustment needed —
but verify this holds for any fund with an unusual distribution mechanism.

### 5.3 Cross-check

Reconcile derived `flow_usd` against `Δ total_net_assets` net of estimated market return.
They will not match exactly — that residual is the fund's return. A residual that implies
an implausible daily return for the sleeve is a data error, not a discovery. Log it.

---

## 6. Analytics layer

### 6.1 Headline dollar aggregates — the v1 output

- `total_flow_usd(t)` — all core funds. The headline number.
- `total_flow_ex_bills(t)` — **report this alongside the headline, always.**
- By sleeve, plus 5-day, 20-day and MTD/QTD rolling sums.

**Why ex-bills matters more than anything else here.** SGOV, BIL, SHV and the FRN funds are
enormous and their flows are cash-management decisions — a money-market substitute choice,
not a fixed income allocation choice. A $10bn week into bills and a $10bn week into IG mean
opposite things about risk appetite. If the headline blends them, the series will be
dominated by cash parking and tell you very little. Ex-bills is arguably the *real*
headline; report both and let them diverge visibly.

Suggested cut levels: **total**, **ex-bills**, **ex-bills-ex-muni** (munis are a
tax-driven, largely separate investor base), and **credit vs. rates** as the top-line
rotation read.

### 6.2 Double-counting control — do this before trusting any aggregate

A total-dollars view has a failure mode that a duration view mostly dodges: **funds that
hold other funds double-count.**

- Option-overlay funds hold the underlying ETF — TLTW holds TLT, HYGW holds HYG, LQDW
  holds LQD. A dollar into TLTW becomes a dollar into TLT. Counting both inflates the total.
- Some active multisector and model-portfolio funds hold ETF positions opportunistically,
  and the weight moves.

Handling:

1. From each fund's daily holdings file, compute `etf_holding_weight(fund, t)` — the share
   of portfolio MV held in other ETFs in the universe.
2. Maintain `total_flow_usd_net` = gross total minus flows attributable to
   fund-of-fund positions, alongside the gross number.
3. Report gross and net. The gap is small in normal periods and informative when it isn't.

This is cheap to build (the holdings files are already being pulled for §6.6) and it is
the difference between a headline you can quote and one you can't.

### 6.3 Organic growth rate

```
ogr(t) = flow_usd(t) / total_net_assets(t-1)
```

Essential for cross-sectional comparison. Without it, AGG and BND swamp everything and
you never see a 4% one-day organic inflow into a CLO fund.

### 6.4 DV01-weighted flow — phase 2, not v1

```
dv01_flow(t) = flow_usd(t) * effective_duration(t) * 0.0001
```

Units: dollars of P&L per 1bp parallel move. A $500mm inflow into TLT and a $500mm inflow
into SHY are the same dollar flow and wildly different duration demand; this is the
number that makes the model a rates tool rather than a flows table.

Aggregate by sleeve and total. Optionally express in 10-year equivalents:

```
tenyr_equiv(t) = dv01_flow(t) / dv01_per_mm_10y
```

where `dv01_per_mm_10y` is computed from the current on-the-run 10y, not hardcoded. As a
rough order of magnitude at recent yield levels it has been in the neighborhood of
$800/bp per $1mm notional — *that figure is an approximation for sanity-checking only and
should be recomputed daily from actual on-the-run data, not relied on as a constant.*

**Critical even though the analytics are deferred:** keep capturing `effective_duration`
and `spread_duration` in `fund_daily` from day one. Issuers publish current duration, not
history — if you skip the column now you can never build this retroactively. Capture the
input immediately; build the math whenever.

### 6.5 Spread DV01 — phase 2

Same construction using spread duration for `ig_corp`, `hy_corp`, `loans_clo`, `em_debt`,
and the credit portion of `aggregate`. Spread duration is published less consistently than
effective duration. Where unavailable, proxy with effective duration and **flag the row**
— a proxied spread DV01 is an estimate, and the column should make that visible rather
than laundering it into the aggregate.

### 6.6 Agency MBS look-through

Pure-play agency MBS ETFs (MBB, VMBS, SPMB, GNMA and peers) understate ETF-channel agency
demand, because broad aggregate funds hold a large MBS weight.

Using the daily holdings files that 6c-11 already requires:

```
mbs_weight(fund, t)      = Σ(market value of agency MBS holdings) / total portfolio MV
implied_mbs_flow(fund,t) = flow_usd(fund, t) * mbs_weight(fund, t-1)
total_etf_mbs_flow(t)    = Σ over all funds of implied_mbs_flow
```

Classify a holding as agency MBS by issuer (FNMA / FHLMC / GNMA) from the holdings file's
sector and issuer fields. Pools, CMOs and agency CMBS should be separable.

**Include the active multisector funds** (BINC, PYLD, BOND, FBND, JCPB) in the
look-through set, not just the index aggregates. Their MBS weight is discretionary and
moves, so the implied flow blends two effects: money arriving, and a manager changing his
agency MBS view. Report the decomposition rather than just the product —

```
Δ implied_mbs(fund) ≈ flow * weight(t-1)  +  aum(t-1) * Δweight
                       └─ flow effect ─┘     └─ allocation effect ─┘
```

The allocation-effect term for the actives is arguably the more interesting series for
your seat: it's a read on what discretionary managers are doing in agency MBS,
independent of whether they're getting money.

Two caveats to carry in the output, not just the code:

- Weights are computed from the prior day to avoid look-ahead within the calculation.
- This measures the MBS *share* of a flow, which assumes creations are in proportion to
  the portfolio. Fixed income creations are frequently cash or custom-basket, so the
  actual securities bought may not match the portfolio weights on any given day. Over a
  week or month the approximation is reasonable; on a single day it can be quite wrong.

### 6.7 Normalization

Report every sleeve aggregate as: raw dollars, OGR, 5-day and 20-day rolling sums,
and a z-score against a trailing 252-day window. Exclude `imputed` and `suspect` days from
z-score calibration or your volatility estimate absorbs your own data errors.

---

## 7. ICI weekly overlay

Source: ICI's **Combined Estimated Long-Term Flows and ETF Net Issuance** release.
Verified 2026-09-19: published **Tuesdays**, covering the week ended the **prior
Wednesday**, downloadable as an Excel file (`combined_flows_data_YYYY.xls`). Categories
include Bond split into **Taxable** and **Municipal**, alongside Equity, Hybrid and
Commodity.

Two properties to respect:

1. **It is estimated, not actual.** ICI's own label. It is a survey-based estimate.
2. **The lag is real.** Six to twelve calendar days depending on where you are in the
   week. Never align an ICI week to a current ETF day without explicitly showing the lag,
   or you will manufacture a spurious lead-lag relationship in your own dashboard.

Its value is that it is the only free window into the mutual fund complex, which is far
larger than the ETF complex. Store it in its own weekly table. Do not blend it into the
daily series.

If daily fund-level mutual fund flows ever become a requirement, that is a vendor
purchase — LSEG Lipper fund flows or EPFR are the buy-side standards — not an
engineering problem. Check Dynex's existing Bloomberg entitlements before buying anything;
some flow content may already be licensed.

---

## 8. Operations

**Schedule.** Cron once daily, timed after the last issuer updates (determine empirically
during discovery; issuer file update times vary and some post late evening ET). Build in a
second catch-up run a few hours later for issuers that were stale on the first pass.

**Gap detection.** `backfill_gaps.py` runs at the start of every job: identify every
(ticker, business day) with no clean observation, attempt re-fetch where the issuer offers
history, and mark the rest permanently `imputed`. Print a coverage percentage every run.
Coverage is the health metric for this dataset.

**Alerting.** Fail loudly. A silent pipeline producing zeros is far worse than a broken
one. Alert on: adapter exception, coverage below threshold, any fund with
`|OGR| > 10%` in a day, any sleeve z-score beyond ±4, split candidates awaiting review.

**Backups.** `data/raw/` is the asset — the DuckDB file is disposable. Back raw up
off-machine. Losing it means losing the compounding value of the whole project.

---

## 9. Validation harness

Non-negotiable, and written before the analytics layer:

- **Identity test.** For a sample of funds and days, `Δ TNA` reconciles to
  `flow + market return` within a tolerance implying a plausible daily return.
- **Spot check against a public source.** Pick a handful of large, unambiguous flow days
  and compare against a published figure from ETF.com or an issuer press release. They
  will not match exactly — vendors use different conventions — but they should agree in
  sign and rough magnitude. A systematic one-day offset against a vendor is the signature
  of the trade-date/settlement-date problem in §5.2.
- **Split regression test.** A fixture containing a known reverse split, asserting the
  adjusted series shows no phantom flow.
- **Staleness fixture.** Two identical consecutive payloads must produce zero emitted
  flows and a stale flag, not a zero flow and then a catch-up spike.
- **No-silent-zeros test.** Assert that no code path converts a missing observation to 0.

---

## 10. Claude tooling plan

**Claude Code, in the repo — the primary tool.** It holds the whole codebase in context,
runs the tests, and iterates. Maintain a `CLAUDE.md` at the repo root recording: the NAV
convention, each issuer's settlement convention as discovered, the raw-is-immutable rule,
and the no-silent-zeros rule. That file is what keeps a future session from cheerfully
"fixing" a deliberate choice.

- Use **plan mode** before the discovery pass and before the analytics layer. Both are
  cases where the wrong structure is expensive to unwind later.
- Use **subagents** for the per-issuer adapter work — each adapter is an independent,
  well-specified unit, which is exactly the shape that parallelizes well.
- Use a **git commit per working adapter** so a site change can be bisected.

**This Cowork session — the research and presentation layer.**

- Scoping and source reconnaissance like today's, where web fetching and reasoning about
  tradeoffs matter more than code.
- A **scheduled task** for the daily read: pull the latest numbers, write the commentary,
  flag what is unusual. This runs in the cloud whether or not your machine is awake, and
  is a different job from the ETL — the ETL must be reliable, the commentary must be
  thoughtful.
- An **artifact** for the dashboard, republished daily to the same URL so it is a live
  page rather than a file you have to find. Shareable with the Dynex team when you want
  it to be.
- Connect the folder holding `data/` so this session can read the DuckDB output directly.

**What not to use Claude for.** Do not have a model in the loop on the daily ETL
transform. The extraction and flow math should be deterministic, tested Python. Model
judgment belongs in the interpretation layer, above the data, where a mistake is visible
rather than silently written into a series you will trust for years.

---

## 11. Build order

1. ~~Universe definition and sleeve taxonomy~~ — done, see `universe.csv`.
2. Endpoint discovery, one issuer at a time, starting with iShares (largest coverage).
3. Ingest + raw storage + validation harness. **Get this running daily before building any
   analytics.** Capture duration columns now even though nothing consumes them yet.
4. Flow calculation with split, staleness and settlement adjustments.
5. Dollar aggregates: total, ex-bills, by sleeve, plus OGR.
6. Double-counting control (§6.2).
7. ICI weekly loader.
8. MBS look-through, including the flow/allocation decomposition.
9. Dashboard.
10. *Later:* DV01 and spread-DV01 layer.

Steps 2-3 are the only time-sensitive ones. Everything after builds against accumulating
data.

---

## 12. Honest limitations to carry into any use of this

- Fixed income ETF creations are frequently cash-based or use custom baskets, and
  authorized participants use these funds to warehouse inventory. A given day's flow can
  reflect dealer balance-sheet mechanics rather than end-investor conviction. Weekly and
  monthly aggregates are far more interpretable than daily prints.
- Building forward from free sources means roughly a year before the series supports any
  statistical claim. Until then it is a monitoring tool, not a research dataset. Resist
  the temptation to backtest on ten months of data.
- The ETF channel is a small and non-representative slice of bond demand, skewed toward
  retail, advisory and tactical institutional money. It tells you little directly about
  bank, insurance, pension or foreign official demand — the flows that matter most to an
  agency MBS book.
