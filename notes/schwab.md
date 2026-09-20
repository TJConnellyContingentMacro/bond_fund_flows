# Schwab — endpoint discovery

Discovery pass, step 2 of the build order. Verified 2026-09-19/20. Sample fund: SCHR.

## Bottom line up front

**The data itself is excellent and complete — same-day NAV, shares outstanding, and
total net assets all with full precision. The problem is access: the entire
`schwabassetmanagement.com` domain sits behind Akamai bot-management, and it blocks
plain HTTP clients outright — including a `curl` request for `robots.txt` itself.**
A real browser gets through fine. This is a different, more fundamental kind of
"awkward issuer" than anything found so far: not missing data, not a ToU clause, but
a technical barrier that rules out a lightweight `requests`-based adapter entirely.

## What's on the page (once you can reach it)

`https://www.schwabassetmanagement.com/products/<ticker-lowercase>` (e.g. `.../schr`)
— first visit shows a one-time "select your role" interstitial (Advisor / Retirement
professional / Personal investor / "Continue with a limited experience") — cosmetic,
not a login. The "Fund Details" section has per-field as-of dates, same shape as
every other issuer:

- **NAV**: `$23.92`, as of `09/18/2026` — daily.
- **Shares Outstanding**: `552,400,000`, as of `09/18/2026` — daily, same day as NAV.
- **Total Net Assets**: `$13,214,476,109.61`, as of `09/18/2026` — **daily, full
  precision to the cent**, same day as NAV. Best TNA precision/cadence combination
  seen across any issuer — better than iShares (formatted string only), Vanguard
  (monthly), or even Invesco/SSGA (rounded to ~$1K–$10K scale).
- **Effective Duration**: `4.9 years`, as of `08/31/2026` — monthly, ~18-day lag
  behind NAV. Bigger lag than iShares (T-2) or SSGA (same-day), closer in spirit to
  Vanguard's monthly cadence but only for this one field, not shares/TNA too.
- Also monthly, same `08/31/2026` as-of: `Weighted Average Coupon`, `Weighted
  Average Maturity`, `Portfolio Turnover Rate`, `Standard Deviation (3yr)`.
- `Total Holdings` (101) as of `09/17/2026` — one day behind NAV, its own cadence.
- No `spreadDuration`-equivalent field — consistent with every other issuer.
- A **"NAV History Download"** CSV link exists per fund
  (`/sites/g/files/eyrktu361/files/product_files/SCHR/SCHR_NAV_History.CSV`) — a
  free backfill source, in principle, but see the access problem below; it's
  blocked exactly like everything else on the domain.

Units: whole dollars, whole shares — confirmed by magnitude ($13.2B fund, 552.4M
shares × $23.92 ≈ $13.2B, consistent).

The page is a Drupal site using a `f2`/`f2_internal` fund-data charting library, all
self-hosted under the same domain — no separate third-party API host to route
around. Everything found (NAV, shares, TNA, duration) appears to be server-rendered
directly into the main HTML document itself, not fetched from a discrete JSON
endpoint — meaning there's no lighter-weight sibling URL to target even if the main
page were reachable a different way.

## The access problem

- A cold `curl` to `https://www.schwabassetmanagement.com/robots.txt` returns
  **HTTP 403** with an Akamai edge error page (`errors.edgesuite.net`) — not a
  normal 403 from the application, a network-edge bot-management block. This
  affects the plain-text robots.txt file itself, which is unusual — most sites
  serve robots.txt to any client by design.
- Adding a full realistic browser header set (`User-Agent`, `Accept`,
  `Accept-Language` matching a real Chrome browser) to the `curl` request **did
  not** get past the block — same 403. This rules out "just set a normal header"
  as a fix; the block is operating on a deeper signal (almost certainly TLS/JA3
  fingerprinting or similar, which a simple HTTP client can't replicate without
  browser-impersonation tooling this project shouldn't be reaching for).
- The static `NAV History Download` CSV, served from the same domain under
  `/sites/g/files/...`, is **also** blocked (confirmed via direct curl) — so this
  isn't a rule scoped to dynamic/HTML paths only; it's domain-wide.
- **A real browser (this session's browser tool) gets through with no issues at
  all** — no CAPTCHA, no challenge page, just normal page loads. So this is a
  "requires a real browser engine" problem, not a "genuinely inaccessible" one.
- **robots.txt itself, once actually reached (via the browser, not curl), is the
  standard permissive Drupal default** — no disallow on `/products/`. So the
  *policy* is fine; only the *technical enforcement layer* is the obstacle.
- Could not find a dedicated Terms of Use page on `schwabassetmanagement.com`
  itself (searched; only found Schwab's broader brokerage-account
  "Online Services Agreement" on `schwab.com`, which is a different, account-context
  document and not clearly the governing terms for this public marketing site) — not
  pursued further given the access-layer finding is the decisive one regardless of
  what the text says.

## What this means for the adapter (not this pass, but material for planning)

Every other issuer checked so far (iShares, Vanguard, SSGA, Invesco) works with a
plain HTTP client (`requests`/`curl` equivalent) — no JavaScript execution needed.
**Schwab is the first to require an actual browser engine** (e.g., Playwright or
Selenium driving headless Chromium) just to fetch the page at all, before any
parsing even starts. That's a meaningfully different and heavier operational
dependency for a daily cron job:

- Slower and more resource-intensive per fetch than an HTTP request.
- A new failure mode class (browser crashes, page-load timeouts, Akamai tightening
  its fingerprinting over time and needing the browser-driving library kept
  current) that the other adapters don't have.
- Per SPEC.md §4.3, one issuer's adapter failing must never stop the run for
  others — this isolation matters more here, since Schwab is the adapter most
  likely to break independently of the rest.

Did not attempt to find ways around the Akamai fingerprinting itself (e.g. TLS
impersonation libraries) — that crosses from "building an adapter" into actively
engineering around a site's active anti-bot security control, which is a different
kind of decision than the ToU-text judgment calls made for iShares/Invesco.

## Open questions for the next pass on Schwab

- Confirm this holds across the other 6 Schwab tickers (SCHO, SCHQ, SCHP, SCHI,
  SCHJ, SCHZ) — checked SCHR only, but the domain-wide nature of the Akamai block
  makes it very unlikely any other page on the same domain behaves differently.
- Decide whether the adapter uses a headless browser (Playwright) directly, or
  whether it's worth checking if Schwab's fund data appears through a secondary,
  less-protected channel (e.g., a data vendor that republishes it) before
  committing to the heavier dependency.
