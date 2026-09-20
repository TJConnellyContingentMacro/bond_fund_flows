# VanEck — endpoint discovery

Discovery pass, step 2 of the build order. Verified 2026-09-19/20. Sample fund: ANGL.

## Bottom line up front

Straightforward, fully server-rendered site — one `curl` per fund page gets NAV,
Total Net Assets, and same-day Effective Duration, no separate API calls needed.
One access quirk (needs a cookie jar, not a bot-blocking issue) and one real,
explicit rate constraint (25-second crawl-delay) to respect. No shares-outstanding
field found — same fallback situation as SSGA/Invesco, not as bad as Vanguard.

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
Maturity` (7.14%), **`Effective Duration`** (4.52 yrs). No spread-duration field —
consistent with every issuer except JPMorgan.

**No shares-outstanding field found anywhere on the page** — checked the hero
stats, the Portfolio tab, and grepped the full raw HTML for `shares outstanding`
as a label (the only hit was inside the NAV glossary tooltip's definition text,
not an actual data field). Same situation as SSGA/Invesco: the fallback
`shares_outstanding = total_net_assets ÷ nav` is required, and inherits the ~0.3%
imprecision from the rounded TNA figure.

No separate API/JSON endpoint exists — confirmed by watching network traffic while
loading the live page and switching tabs: zero XHR/fetch calls fired. Everything
(including the Portfolio tab's Fundamentals) is present in the single initial page
load.

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

- Confirm the same page structure (single SSR page, Portfolio tab same-day
  duration, no shares-outstanding field) holds for CLOI, EMLC, and HYD — only ANGL
  checked in depth.
- Re-check the ticker-specific robots.txt exclusion list if the universe changes.
- No spread duration found — matches every issuer except JPMorgan; nothing further
  to check here.
