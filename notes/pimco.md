# PIMCO — endpoint discovery

Discovery pass, step 2 of the build order (final issuer). Verified 2026-09-19/20.
Sample fund: MINT.

## Bottom line up front

**Best portfolio-characteristics data of any issuer checked — full-precision NAV,
TNA, and directly-published shares outstanding, plus effective duration broken out
by risk factor (bull/bear market duration, and separate mortgage/corporate/EM
spread duration) — but getting to it requires completing a one-time
role-selection + "I agree to be bound by these terms" click-through before the
site will render any real data.** That consent step is a bigger deal than a
cosmetic cookie banner: I checked with T.J. before accepting it, since agreeing to
a site's terms on his behalf isn't something to do by default. He said yes, so
PIMCO's Terms and Conditions of Use are now accepted for this browsing session —
but that doesn't answer how a *daily automated* adapter handles this same gate
going forward, which needs its own decision (see below).

## The consent gate, precisely

`https://www.pimco.com/us/en/investments/etf/pimco-enhanced-short-maturity-active-exchange-traded-fund/usetf-usd`
(note: the shorter alias `www.pimco.com/en-us/investments/etf/<slug>` — the form
search engines surface first — returns a hard `403 Access Denied` from an Akamai
edge rule; **use the full `/us/en/investments/etf/<slug>/usetf-usd` form**, which
works). This is a Sitecore-based site.

- **Cold, no session**: loading the page shows a "PIMCO United States — Tell us a
  little about you" modal (Financial Advisor / Institutional Investor / Individual
  Investor), then a second gate: "Terms and Conditions and Privacy Policies —
  Please read and acknowledge the following," with a required checkbox ("I
  acknowledge that I have read and understand the Terms and Conditions, Privacy
  Policy, and Cookie Settings... and agree to be bound by them") before an
  "Accept" button activates.
- **Confirmed via `curl`**: fetching the correct URL cold (or with a fresh,
  empty cookie jar) returns a real `200`, but only an **111KB stripped shell** —
  none of `Effective Duration`, `Total Net Assets`, `Shares Outstanding`, or
  `Historical Prices` appear anywhere in it. The full page (with all portfolio
  data) only renders in-browser *after* clicking through both gates, at which
  point a `POST /ren/layout/roles/setusercookies` fires and the site becomes fully
  populated on the same URL.
- **This is a distinct mechanism from a bot-technical block.** The page does load
  cold — it's not Access Denied — it's a legitimate, deliberate consent gate that
  the site's own CMS uses to decide whether to render real content, the same way
  a paywall might. Separately, PIMCO's cookies do include Akamai Bot Manager
  markers (`_abck`, `bm_sv`, `bm_sz`), so there's likely also a background
  bot-detection layer, but it didn't visibly block anything once the consent flow
  was completed in a real browser.
- **Did not attempt to script/replicate the terms-acceptance POST request** to
  get the full page via `curl` directly. Doing that would mean building an
  automated bypass of a human "I agree to be bound" consent step, which is a
  materially different thing from parsing a page whose data is simply public —
  this is exactly the kind of terms-acceptance action that should get a specific
  decision, not get silently engineered around during a reconnaissance pass.

## What's actually on the page, once past the gate

**Fund Facts panel** (two columns, one for NAV-based figures, one for
market-price-based figures), all as of `09/17/2026` (the whole page was labeled
"Updated 09/17/2026" — a **T-1 lag** versus the "today" this pull ran, i.e. PIMCO's
site itself doesn't have same-day figures the way SSGA/VanEck do):

- `NAV`: 100.64 USD; `One Day Return (NAV %)`: 0.04%; `Premium/Discount`: 0.00%.
- **`Total Net Assets (USD)`: 17,889,151,478.52** — full precision to the cent,
  matching iShares/JPMorgan/Schwab's tier, better than SSGA/Invesco/VanEck's
  rounded-to-thousands/millions figures.
- **`Shares Outstanding`: 177,760,000** — a directly published field (not a
  derived one), though displayed rounded to the nearest 10,000 (unlike iShares'
  exact-integer figure).
- `Daily Trading Volume (dollar notional)`, `30-Day Median Bid/Ask Spread`,
  `Quarterly Average Trading Volume` (both USD and shares, quarterly cadence,
  `as of 06/30/2026`).

**Portfolio Composition tab, "Interest Rate & Sector Exposures", `as of
08/31/2026`** (monthly — a bigger lag than the NAV-linked figures above, same
general shape as every other issuer's duration-vs-NAV lag):

- `Effective Duration`: 0.50
- `Bull Market Duration`: 0.44 / `Bear Market Duration`: 0.61 — a scenario-based
  duration pair not seen from any other issuer.
- **`Mortgage Spread Duration`: 0.70**, **`Corporate Spread Duration`: 0.43**,
  **`Emerging Market Duration`: 0.04** — **genuine spread duration, broken out by
  sector.** This is richer than JPMorgan's single blended spread-duration figure
  and is exactly the sector-level granularity SPEC.md §6.5 describes wanting for
  the credit/EM sleeves specifically, not just a portfolio-wide number.
- Also: `Sector Allocation - Duration in Years` and `Maturity Distribution (%)`
  breakdowns, same `08/31/2026` date.

**"Top 10 Exposure" (holdings), `as of 09/17/2026`** — same day as NAV, with an
"All Holdings" download link (not investigated further — the summary table alone
already answered what this pass needed).

**"Historical Prices" tab** — a chart/table toggle with `1M/3M/6M/YTD/1Yr/All`
range buttons, `NAV Price` and `Market Price` per day. The `All` option implies
since-inception daily history is available directly from the page, similar in
spirit to JPMorgan's `historicalData` endpoint, though here it's a UI control
rather than a discovered raw API call (didn't find a distinct backing endpoint —
consistent with everything else on this page being one fully server-rendered
document once the consent gate is satisfied).

**Premium/Discount history chart** — a separate daily time series going back to
at least January 2025, plus a quarterly summary table of "days traded at premium"
vs "at NAV" vs "at discount."

**No shares-outstanding-style rounding concern**: given `Total Net Assets` has
full-cent precision and `Shares Outstanding` is separately, directly published,
there's no need for the fallback formula here at all for MINT.

## robots.txt / Terms of Use

- **robots.txt**: fully permissive — no disallow rules, no crawl-delay, just a
  sitemap reference.
- **Terms and Conditions of Use** (the same document just clicked through in the
  browser, at `pimco.com/us/en/general/legal-pages/terms-and-conditions-of-use-for-individuals`):
  explicitly prohibits "scraping," data mining, and prohibits using information
  obtained from the site without written permission — same category as
  iShares/Invesco/Janus Henderson's blanket clauses. **Difference from those
  three**: this one was just affirmatively accepted (checkbox + Accept button)
  in this session, with T.J.'s explicit go-ahead, rather than being a
  background fact simply noted. That's a stronger, more direct acceptance than
  "noting a ToU clause exists" — worth being clear about since it's now a
  specific action taken, not just an observation.

## Decision needed for the adapter (not this pass): how does a daily cron handle the consent gate?

This is the one PIMCO-specific question that needs an answer before building an
adapter, parallel to (but different from) Schwab's browser-engine requirement:

1. **Headless browser, run the click-through once, persist the session** — same
   general shape as the Schwab solution, and the consent flow only needs to
   happen once per session/cookie-lifetime, not every single day, if the
   resulting cookies are reused.
2. **Investigate whether the role/terms cookies, once set, are long-lived enough**
   that a real browser could complete the flow manually on a schedule (e.g.
   monthly) and hand the resulting cookie jar to a lightweight `curl`-based
   adapter the rest of the time — cheaper than driving a browser daily, but
   untested this pass.
3. **Treat MINT/ZROZ/LTPZ/MUNI/BOND/PYLD like Schwab** — bucket both issuers
   together as "needs a browser-capable adapter," accepting the shared
   operational cost rather than optimizing PIMCO specifically.

Not resolving this now — it's an adapter-architecture decision, not a discovery
one, and depends on choices already pending for Schwab.

## Open questions for the next pass on PIMCO

- Confirm the consent-gate mechanics and field set hold for the other 5 tickers
  (ZROZ, LTPZ, MUNI, BOND, PYLD) — only MINT checked.
- Confirm whether `Shares Outstanding`'s as-of date is genuinely the same as
  NAV's (`09/17/2026`) — inferred from column layout, not an explicit adjacent
  label.
- Decide the consent-gate handling approach above before adapter build.
