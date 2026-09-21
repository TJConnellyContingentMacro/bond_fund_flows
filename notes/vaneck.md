# VanEck — endpoint discovery

Discovery pass, step 2 of the build order. Verified 2026-09-19/20, **corrected
2026-09-21 while building the adapter** — two findings below turned out to be
wrong on the first pass; see the correction notes inline.

## Bottom line up front

One access quirk (needs either a cookie jar for `curl`, or
`wait_until="domcontentloaded"` for Playwright — see below) and one real,
explicit rate constraint (25-second crawl-delay) to respect. No
shares-outstanding field found — same fallback situation as SSGA/Invesco, not
as bad as Vanguard. **Correction**: the page is NOT fully server-rendered — the
Portfolio tab's content (Effective Duration, Spread Duration) only mounts into
the DOM once that tab is actually clicked, requiring a headless browser, not a
plain `curl`, to reach.

## Access quirk: cookie-consent redirect loop (not bot-blocking)

A cold `curl` (no cookie jar) to any `vaneck.com` product page gets stuck in an
infinite redirect loop: `/us/en/investments/<slug>/` → `/` → `/us/en/` →
`/us/en/?cken=true` → `/row/disabled-cookies/` → `/corp/en/disabled-cookies` → ...
repeating. This is the site checking for a "cookies enabled" flag cookie and
bouncing anyone who doesn't have one to a "please enable cookies" page, then back
around. **Fixed trivially by using a cookie jar** (`curl -c cookies.txt -b
cookies.txt ...`) — the first response sets the flag cookie, the second request
carries it, and the real page loads normally (confirmed: 200, full page, matches
live browser rendering exactly). Not a bot-detection measure like Schwab's Akamai
block — a normal browser handles this transparently via its cookie jar, and so
does a scripted client once it keeps cookies across requests within a session.

## What's on the page (all server-rendered, no JS execution needed)

Top summary block: `NAV` ($28.49), `YTD Returns`, `Total Net Assets` ($3.17B),
`Total Expense Ratio`, `Inception Date`, `30-Day SEC Yield` — all dated
`September 18, 2026` (same day).

**Total Net Assets is rounded to 2 decimals in billions** (`$3.17B` — roughly
±$5M precision at this scale, ~0.3% relative). No hidden full-precision value
found anywhere in the page source (checked for a `data-value`/raw-number
attribute the way SSGA's page has one — not present here). Same precision tier as
SSGA and Invesco; better than Vanguard's monthly figure, worse than iShares'
whole-dollar or JPMorgan/Schwab's full-cent precision.

**Portfolio tab → "Fundamentals" section, `as of 09/18/2026`** — same day as NAV,
no lag (matches SSGA's clean same-day pattern, unlike iShares' T-2 or Schwab's
~18-day lag): `Yield to Worst` (6.99%), `Years to Maturity` (9.16), `Yield to
Maturity` (7.14%), `Coupon` (5.55%), **`Effective Duration`** (4.52 yrs), and
**`Spread Duration`** (4.57 yrs). **Correction**: the original discovery pass
missed Coupon and Spread Duration — they're further down the same Fundamentals
card, past what the screenshot at the time captured. VanEck *does* publish real
spread duration, same as JPMorgan; this issuer isn't the "every issuer except
JPMorgan lacks it" case it was first written up as.

**This whole section requires clicking the "Portfolio" tab** — it isn't present
in the page's initial HTML at all (confirmed empty on a fresh `curl`, and empty
even in a full headless-browser page load before the tab is clicked). Once
clicked, the content renders immediately with no new network request visible —
consistent with the tab data already being loaded into client-side state
earlier in the page load and just not yet mounted into the DOM until that UI
action happens.

**No shares-outstanding field found anywhere on the page** — checked the hero
stats, the Portfolio tab, and grepped the full raw HTML for `shares outstanding`
as a label (the only hit was inside the NAV glossary tooltip's definition text,
not an actual data field). Same situation as SSGA/Invesco: the fallback
`shares_outstanding = total_net_assets ÷ nav` is required, and inherits the ~0.3%
imprecision from the rounded TNA figure.

No separate, independently-callable API/JSON endpoint was found — watching network
traffic while switching tabs showed no NEW request firing. **Correction**: this
does not mean everything is present in the initial page load (see above); it
means the Portfolio tab's data most likely arrives as part of the same initial
load's payload (in a client-side state store) and is simply not rendered into
visible DOM until the tab is clicked. The practical adapter implication is the
same either way — a plain HTTP GET won't show it, a headless browser click will.

## robots.txt — permissive for this project's tickers, but with a real crawl-delay

```
User-agent: *
Crawl-delay: 25
```

**A real, explicit rate limit** — the first of any issuer checked so far. 4 VanEck
tickers ⇒ a sequential daily pull respecting this delay takes ~100 seconds
(4 × 25s), trivial for an overnight cron but worth building the delay into the
adapter rather than ignoring it, since it's an explicit site directive rather than
a "be polite" inference.

Separately, robots.txt disallows crawling for a specific list of ETF tickers
(COLX, GERJ, LATM, RKH, CHLC, IDXJ, MES, ITML, ITMS, THHY, KWT, SPUN, IGEM, PRB,
GNRX, PLND) and their holdings/performance/fact-sheet pages — **none of this
project's 4 tickers (ANGL, CLOI, EMLC, HYD) are on that list**, confirmed by
checking the raw robots.txt directly. Worth re-checking this list if the universe
ever adds another VanEck ticker, since the block is ticker-specific rather than a
blanket ETF-pages rule (unclear why these specific funds are excluded — possibly
funds in registration, being wound down, or otherwise sensitive; not investigated
further since it's moot for the current universe).

## Terms of Use

`vaneck.com/us/en/legal/` — General Site Use Disclaimers / Copyrights section.
**No robot/bot/crawler/scraper/automated-access clause at all.** The only
relevant restriction is a standard copyright one, and it explicitly names a
personal-use allowance:

> "No information contained on this site may be reproduced, transmitted,
> displayed, distributed, published or otherwise used for commercial purposes
> without the prior consent of VanEck. You may, however, print or electronically
> store copies of the information for your own personal use."

Lowest-risk ToU language found of any issuer so far — explicitly permits storing
copies for personal use, silent on automation one way or the other.

## Units

Whole dollars for NAV; net assets displayed in billions (`$3.17B`) — no thousands/
millions ambiguity, just a rounding-precision one (see above).

## Open questions for the next pass on VanEck

- Re-check the ticker-specific robots.txt exclusion list if the universe changes.

## Resolved during adapter build (2026-09-21)

- Confirmed the same page structure (Portfolio tab same-day duration and spread
  duration, no shares-outstanding field anywhere) holds for CLOI, EMLC, and HYD,
  not just ANGL.
- Also caught a real bug while parsing "$3.17B"-style figures: constructing a
  `Decimal` via `Decimal("3.17") * Decimal("1e9")` produces a value whose
  string form is itself exponential (`3.17E+9`), which DuckDB's parameter
  binding silently mis-parsed as `317.00` — a 10-million-times-too-small
  number, with no error raised anywhere. Fixed by scaling with a plain `int`
  instead. See CLAUDE.md's Conventions section — this risk applies to any
  future adapter parsing an abbreviated dollar figure.
