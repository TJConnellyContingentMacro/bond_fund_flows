# bondflows

Daily US bond ETF flow model. Read SPEC.md before doing anything.

## Conventions — do not change without asking

- flow = (shares_outstanding(t) - shares_outstanding(t-1)) * nav_per_share(t)
- NAV only. Never market close.
- data/raw/ is append-only. Never edit or delete a raw snapshot.
- All derived output must be rebuildable from data/raw/ alone.
- A missing observation is NULL, never 0. Zero is a real value in this dataset.
- One adapter per issuer. An adapter failure must not stop the run for other issuers.
- Capture effective_duration and spread_duration daily even though nothing uses
  them yet. Issuers publish current values only; this cannot be backfilled.
- **Never construct a `Decimal` via multiplication by `Decimal("1eN")`** (e.g.
  `Decimal("3.17") * Decimal("1e9")`). The result's internal representation is
  itself exponential (`3.17E+9`), and DuckDB's Python parameter binding
  silently mis-parses that as `317.00` — a real, confirmed, silent data
  corruption found while building the VanEck adapter (a $3.17B fund landed in
  `fund_daily` as $317). Scale by a plain Python `int` instead
  (`Decimal("3.17") * 1_000_000_000`), which stays in normal notation and
  binds correctly. Applies to any adapter parsing an abbreviated figure like
  "$3.17B" or "523.28 M".

## Stack
Python 3.11+, DuckDB, pandas, requests, pytest, Playwright (Schwab + PIMCO only —
`playwright install chromium` is a one-time manual step, not covered by
`pip install`). No ORM, no framework.

## Decisions from discovery — do not re-litigate these

- **Vanguard flow granularity is monthly, not daily.** Vanguard's ETFs are a
  share class of the underlying mutual fund and only disclose shares
  outstanding monthly, confirmed via both the retail and advisor sites — not a
  scraping gap. The adapter reports `shares_outstanding = NULL` on days the
  monthly figure hasn't advanced; this is not a bug to "fix" by interpolating
  or holding a stale value. NAV and duration are daily and reported normally.
- **Schwab and PIMCO adapters use Playwright (`BrowserSourceAdapter`).**
  Schwab: the whole `schwabassetmanagement.com` domain returns HTTP 403 from
  Akamai bot-management for any plain HTTP client (even `robots.txt` itself),
  confirmed a real browser gets through with zero friction — this is a "needs
  a browser engine" problem, not a data-availability one. PIMCO: a one-time
  role-select + "I agree to be bound by these terms" click-through gates all
  real data; the adapter persists Playwright session state
  (`data/.state/pimco_storage_state.json`, gitignored) so the consent flow
  only has to run again when that session expires, not every single day.
- **iShares' shares outstanding, and every issuer's shares outstanding where
  it isn't directly published, may be a derived value** (`total_net_assets /
  nav_per_share`) — flagged in the adapter per SPEC.md §4.1, not silently
  presented as issuer-reported.
- **ToU risk stance, decided per issuer** (see `notes/<issuer>.md` for the
  exact clause quoted): iShares, Invesco, Janus Henderson, and PIMCO all have
  a blanket "no bots/scrapers/automated access" clause with no personal-use
  carve-out — proceeding anyway was an explicit, informed call for this
  project's low-volume personal use, not an oversight. SSGA, JPMorgan, and
  VanEck have no such clause (only generic copyright language) — meaningfully
  lower risk. Vanguard and Schwab weren't fully resolved (Vanguard: two
  different sites, didn't check the advisor site's ToU separately; Schwab:
  Akamai blocking meant ToU text wasn't the operative constraint anyway).

## Flow calculation (flows.py, step 4) — decisions and open gaps

- **Only 2 of §5.2's 4 adjustments are implemented.** Split detection/flagging
  and staleness handling are done. Settlement-date normalization and
  launch/closure suppression are not — both documented below rather than
  silently skipped, per §5.2's own instruction to document ambiguity rather
  than guess.
- **Settlement-date vs trade-date is unresolved for every issuer** (see the
  conventions table below — every row says "Undetermined"). `flow_date` is
  therefore just the adapter-reported `asof_date`, unnormalized. Revisit once
  enough `fund_daily` history has accumulated to actually determine each
  issuer's convention empirically (the thing §5.2 says to wait for).
- **Launch/closure suppression is not implemented.** It needs
  `inception_date`/`delisting_date`, which exist as columns on the `universe`
  DB table but are never populated — `universe.csv`/`universe.py` don't carry
  them. Not a live correctness problem for the current universe (nothing in
  it launched inside the lookback window as of 2026-09), but a real gap:
  populate those two columns and wire launch-suppression into
  `compute_ticker_flows` before relying on flow data for a fund that's newly
  listed.
- **Split candidates are never auto-applied.** `compute_ticker_flows` flags
  `suspect` (flow_usd left NULL) when `|Δshares|/shares(t-1) > 20%` and
  `shares_ratio * nav_ratio ≈ 1` (within 10%) — a human confirms the
  corporate action by adding a row to the version-controlled `splits.csv`
  (columns: `ticker,split_date,ratio,notes`; `ratio` = new shares per old
  share, e.g. `0.25` for a 1-for-4 reverse split), then reruns
  `scripts/compute_flows.py`, which recomputes the full history and picks up
  the confirmed entry retroactively.
- **Staleness**: `fund_daily.source_is_stale` rows (set by ingest.py) are
  skipped entirely when walking a ticker's history, so the next fresh
  observation's delta spans the whole gap and gets flagged `imputed` — the
  "preferred" approach §5.2 describes, rather than trying to distribute the
  change day-by-day. The `stale_prior` flag value (in the `fund_flows.flag`
  enum) is intentionally unused as a result.
- **`organic_growth_rate` is populated in flows.py**, not deferred to the
  §6.3 analytics layer — it's a trivial `flow_usd / total_net_assets(t-1)`
  using data already in hand while computing flow_usd. `dv01_usd_per_bp` and
  `spread_dv01_usd_per_bp` stay NULL here — SPEC.md §6.4 explicitly calls
  those out as phase-2, built later against accumulated history.
- **The §9 "spot check against a public source" test is not automated** — it
  inherently means comparing one real fund's real flow against a real
  published figure, which no fixture can stand in for. It's a manual check to
  run once enough history has accumulated, not part of `pytest`.

## Dollar aggregates (analytics.py, step 5) — decisions and open gaps

- **Gross only — no double-counting correction yet.** §6.2 (net of
  fund-of-fund holdings, e.g. TLTW holding TLT) is build-order step 6 and
  needs a holdings-file ingestion path that doesn't exist yet.
  `flow_aggregates` has no `_net` column. Every number in it is a gross sum.
- **Every cut filters to `in_core = TRUE`**, applied consistently across the
  headline total, ex-bills, credit/rates, and every by-sleeve cut — not just
  the top-line number §6.1 explicitly calls out core-only.
- **Credit-vs-rates sleeve assignment** (`analytics.py`'s `RATES_SLEEVES` /
  `CREDIT_SLEEVES` / `MIXED_OTHER_SLEEVES`): `levered_inverse` (TMF/TMV/TBT/
  TBF/UBT) is all leveraged/inverse long-Treasury products, so it's grouped
  with rates rather than left as "mixed" despite the name. `intl_bond`
  (BNDX/IAGG/BWX/IGOV) is developed-market sovereign debt — duration-driven,
  grouped with rates despite the FX dimension. `aggregate`, `active_multisector`,
  and `overlay` are genuinely mixed and deliberately excluded from the
  credit/rates split rather than force-assigned — `overlay`'s 3 tickers
  (TLTW/HYGW/LQDW) mix a rates underlying and two credit underlyings inside
  one sleeve, and they're exactly the §6.2 double-counting candidates
  anyway, so they wait for that phase. `analytics.py` asserts every sleeve
  in `universe.csv` lands in exactly one of the three groups — a newly added
  sleeve that isn't assigned fails loudly rather than silently landing
  nowhere or double-counting.
- **Rolling sums (5-day/20-day) are windows over each cut's own observed
  trading days**, not calendar-day windows — there's no market holiday
  calendar in this project, and none is needed if the window is just "the
  last N rows" of that cut's own daily series. MTD/QTD reset at the calendar
  month/quarter boundary.
- **z-score methodology (§6.7)**: trailing 252-*observed-day* window,
  computed as of and including the day being scored (the scored day's own
  value is part of its own calibration set — only days flagged `imputed` or
  `suspect` are excluded from the mean/stdev calibration, exactly matching
  §6.7's wording, not the day being scored itself). Requires at least 20
  clean days in the trailing window or `zscore_252d` is `NULL` — a stated
  threshold to avoid a meaningless z-score early in the series, not a silent
  guess.
- **Aggregate OGR** = `SUM(flow_usd) / SUM(prior-day total_net_assets)`
  across a cut's constituent tickers. A ticker with no `total_net_assets` at
  all (e.g. Vanguard) simply drops out of that sum via normal SQL NULL
  handling — the denominator is a partial sum across issuers that publish
  TNA, not a fabricated total-universe AUM figure.

## Double-counting control (overlay.py, step 6) — narrowly scoped, by design

- **§6.2 is only partly built.** SPEC.md names two double-counting sources:
  option-overlay funds holding their underlying ETF (TLTW→TLT, HYGW→HYG,
  LQDW→LQD), and active multisector funds that "hold ETF positions
  opportunistically." Only the first is implemented.
- **Why the second isn't**: checked BOND, TOTL, PYLD, JCPB directly (the
  active_multisector tickers whose issuers we have adapters for — FBND and
  DFCF have no adapter at all, different issuers never discovered). BOND has
  2,044 holdings, TOTL has 1,710 — large, granular active portfolios.
  Detecting an ETF holding among them would need full holdings-file parsing
  and name/CUSIP matching across 3-4 differently-shaped issuer formats
  (iShares, PIMCO, SSGA, JPMorgan each publish holdings differently), for a
  payoff SPEC.md itself hedges as "opportunistic" — not a certain effect.
  Decided with T.J. to skip this for now rather than build a large,
  uncertain-payoff pipeline. `flow_aggregates.flow_usd_net` therefore only
  nets out TLTW/HYGW/LQDW; it is **not** a complete §6.2 implementation.
- **How the overlay correction works**: `overlay.py` fetches each of the 3
  funds' iShares holdings CSV directly (no auth, `<product_url>/latest-
  holdings.csv`) via `scripts/fetch_overlay_holdings.py`. Each fund's
  dominant holding (~100% weight, confirmed live 2026-09-25: TLTW 100.17%,
  HYGW 100.28%, LQDW 100.44%) is its underlying ETF; the rest is a small
  cash sleeve plus (for TLTW) a short call option position. A row is
  rejected (logged, not written) if the top holding's name doesn't match the
  expected underlying or its weight drops below a 50% sanity floor — both
  guard against silently misreading a wrong row if iShares changes the CSV's
  shape.
- **Uses the prior day's weight**, matching §6.6's "computed from the prior
  day to avoid look-ahead" principle for the same kind of holdings-weight
  calculation, even though §6.2's own text doesn't restate that timing
  explicitly.
- Run `scripts/fetch_overlay_holdings.py` before `scripts/compute_analytics.py`
  for `flow_usd_net` to reflect that day's overlay weight; if it hasn't run
  yet, `flow_usd_net` just equals `flow_usd` for that day (no adjustment
  available yet), which is a silent-but-honest degrade, not a wrong number.

## ICI weekly overlay (sources/ici.py, step 7)

- **Real URL, confirmed live 2026-09-26**: the release page
  (ici.org/research/statistics/etfs/weekly-combined-estimated-etf-and-longterm-flows)
  links to `ici.org/combined_flows_data_<year>.xls` — a legacy binary `.xls`
  (OLE2/BIFF), not `.xlsx`. Needs the `xlrd` package (added to
  pyproject.toml) — `openpyxl` cannot read this format.
- **The sheet has both a monthly and a weekly section** ("Weekly MF & ETF
  Public Report" sheet: a monthly-flows block first, then an "Estimated
  weekly fund flows" block). Only the weekly section is parsed into
  `ici_weekly_flows` — SPEC.md §7 only asks for the weekly overlay. The
  monthly section isn't ingested but is still recoverable from the raw
  `.xls` snapshot under `data/raw/.../ici/`, same rebuildability guarantee
  as everything else.
- **Publish-day observed as Wednesday, not Tuesday** — SPEC.md's own
  research pass said "published Tuesdays"; the live site's most recent
  posting (checked 2026-09-26) was dated Wednesday Sep 23, covering the week
  ended Wednesday Sep 16 (matches SPEC.md's "week ended the prior Wednesday"
  exactly). Not worth changing anything over — the loader doesn't assume a
  posting day, it just fetches whatever's live — but noted here since it's a
  factual correction to the spec's own text.
- **Units converted from the file's native millions to raw USD** (`*
  1_000_000`, a plain int) for consistency with the rest of the schema,
  which stores raw dollars everywhere else.
- Stored in its own `ici_weekly_flows` table, `week_ended`-keyed, upserted on
  every fetch — safe to run `scripts/fetch_ici_weekly.py` daily even though
  ICI only posts weekly. Never blended into the daily `fund_flows`/
  `flow_aggregates` tables, per SPEC.md §7's explicit instruction.
- **Not yet consumed anywhere** — this step only builds the loader. Whatever
  eventually displays this data (the dashboard, step 9) must show the lag
  explicitly per §7 ("never align an ICI week to a current ETF day without
  explicitly showing the lag") rather than plotting it flush against the
  daily series.

## Agency MBS look-through (mbs.py, step 8) — narrowly scoped, by design

- **Only 6 of the 16 §6.6 tickers are covered**: iShares (MBB, GNMA, AGG,
  IUSB) and SSGA (SPMB, SPAB) — the only issuers confirmed to publish a
  genuine full daily holdings file with real market values. Checked all 16
  directly (the agency_mbs and aggregate sleeves, plus BINC/PYLD/BOND/FBND/
  JCPB) before narrowing: Vanguard's holdings endpoint has no market-value
  field at all (a pre-existing gap from original discovery, needs a separate
  bottom-up pricing project); PIMCO/JPMorgan/Schwab's holdings-file shapes
  were never checked; Fidelity and Simplify are entirely new, never-
  discovered issuers; Janus Henderson is an already-confirmed dead end; and
  BINC isn't even resolved in universe.csv — its `issuer` field
  ("iShares/BlackRock") doesn't match the adapter registry's "iShares" key,
  so it's silently getting zero coverage today for basic flow data too, a
  pre-existing bug unrelated to holdings. `mbs_weight` is simply absent for
  all of these rather than guessed.
- **No flow/allocation decomposition** (§6.6's `Δ implied_mbs ≈
  flow*weight(t-1) + aum(t-1)*Δweight`). That decomposition is about
  discretionary managers changing their MBS view — it only makes sense for
  the active multisector funds, none of which made the cut above (all 6
  in-scope tickers are index funds). Only the basic `implied_mbs_flow` is
  computed.
- **Classification is genuinely different per issuer's actual fields**,
  confirmed against real holdings 2026-09-25/26: iShares' CSV has a `Sector`
  column (`MBS Pass-Through`, `Agency Fixed Rate`, `Hybrid Arms` are always
  agency MBS; `CMBS` is a mixed bucket needing a name filter — AGG's own
  CMBS rows include both FHLMC/FHMS/FREMF-prefixed agency paper and private
  conduits like WFCM/JPMCC/BANK; `Agency` — e.g. "FHLMC REFERENCE NOTE" — is
  general GSE debt, not a mortgage pool, and is excluded entirely). SSGA's
  XLSX has no Sector column, so classification is name-pattern-only.
- **A real trap, found in SPAB's actual holdings**: "FREDDIE MAC NOTES
  07/32 6.25" matches the FREDDIE MAC issuer prefix but is agency *debt*,
  not a mortgage-backed security — excluded via a NOTE/DEBENTURE veto that
  applies regardless of an issuer-prefix match. Same veto covers iShares'
  "Agency" sector and its CMBS rows.
- **Cross-issuer sanity check, live 2026-09-25**: MBB 94.09%, GNMA 98.61%,
  and SSGA's SPMB 98.61% (independently classified via a completely
  different code path) all land in the same range for pure-play agency MBS
  funds — a good sign the classifier isn't issuer-specific by accident.
  AGG 21.90% and SPAB 23.37% (both blended aggregate funds) are in the same
  ballpark too.
- Uses the *prior* day's weight for `implied_mbs_flow`, same principle as
  overlay.py's §6.2 treatment. Run `scripts/fetch_mbs_weights.py` then
  `scripts/compute_mbs_flows.py`, after `scripts/compute_flows.py`.

## Discovered issuer conventions

Full detail — sample endpoints, exact field names, raw response shapes — lives
in `notes/<issuer>.md` per issuer. This table is the fast-reference summary;
go to the notes file before writing or debugging an adapter.

| Issuer | Access | Shares O/S | Total Net Assets | Effective Duration | Spread Duration | Settlement convention |
|---|---|---|---|---|---|---|
| iShares | HTTP, bulk screener resolves all tickers | Direct, daily, T-1 | Direct (string), T-1 | Direct, T-2 | Not published | Undetermined — needs accumulated history |
| Vanguard | HTTP, advisor API (`advisors.vanguard.com`), needs per-ticker `fundId` | Direct, **monthly only** (structural) | Not published anywhere | Direct, daily (via `analytics/daily-fixed-income`) | Not published | Undetermined |
| SSGA | HTTP, retail + intermediary pages (intermediary has the fields retail lacks) | Direct, daily, same-day as NAV | Direct (full precision via retail `originalValue`) | Direct, same-day, no lag | Not published | Undetermined |
| Invesco | HTTP, `dng-api.invesco.com`, needs per-ticker CUSIP | Direct, daily, full precision | Direct, daily, full precision | On-page but source endpoint never traced — open item | Not published | Undetermined |
| Schwab | **Playwright** (Akamai blocks plain HTTP) | Direct, daily, full precision | Direct, daily, full precision | Direct, **monthly**, ~18-day lag | Not published | Undetermined |
| JPMorgan | HTTP, `am.jpmorgan.com/FundsMarketingHandler`, needs per-ticker CUSIP | Derived (TNA÷NAV, not a distinct field) | Direct, daily, full precision | Direct, ~7-week lag (confirm this isn't JPST-specific) | **Direct** — the only issuer that publishes real spread duration | Undetermined |
| Janus Henderson | HTTP, server-rendered, no distinct API | **Empty at last check** on all 3 tickers — re-verify before trusting | **Empty at last check** — re-verify | Direct, same-day, works fine | Not published | Undetermined |
| VanEck | HTTP, needs a cookie jar (consent-redirect loop, not bot-blocking); robots.txt has a real 25s crawl-delay | Not published — use TNA÷NAV fallback | Direct, daily, rounded to ~$5M precision | Direct, same-day, no lag | Not published | Undetermined |
| PIMCO | **Playwright** + persisted consent session (see above) | Direct, daily, rounded to nearest 10k shares | Direct, daily, full precision | Direct, monthly; **broken out by mortgage/corporate/EM sector** — richest duration data of any issuer | Not published as a single figure, but sector-level spread duration is | Undetermined |

**No issuer's settlement-date (trade vs. settlement) convention has been
determined yet** — SPEC.md §5.2 says to document the ambiguity rather than
guess, and that's exactly the state here. This needs a few weeks of
accumulated daily history per issuer (watching a known creation/redemption
event) before it can be resolved — not something discovery-from-a-single-pull
can answer.
