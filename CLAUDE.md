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
