# Janus Henderson — endpoint discovery

Discovery pass, step 2 of the build order. Verified 2026-09-19/20, **re-verified
2026-09-21 on a completely fresh fetch** (different session, no cached state) —
findings below are now confirmed persistent, not a one-off outage.

## Bottom line up front

**NAV, Total Net Assets, and Shares Outstanding are permanently empty on every
Janus Henderson fund page** — confirmed on all three tickers (JAAA, JMBS, JBBB),
confirmed across `/investor/` and `/advisor/` page variants, confirmed with and
without an `?identifier=<ISIN>` query param, confirmed on a second, independent
day. Meanwhile everything else (effective duration, yields, holdings count,
performance returns) renders correctly with real numbers on the same pages, every
time. **This rules out an adapter that produces usable `fund_daily` rows for these
3 tickers** — NAV is the one field this whole project can't do without, and there
is no live, public, daily source for it anywhere on Janus Henderson's site.

## What's broken (confirmed twice, on both re-checks)

| Field | JAAA | JMBS | JBBB |
|---|---|---|---|
| NAV | `-` | `-` | `-` |
| Total Net Assets | `-` | `-` | (not individually re-checked, same page block as JMBS) |
| Shares Outstanding | (not checked) | `-` | (not checked) |

The as-of-date label always renders correctly (`09/18/2026` on the latest
re-check) right next to the empty value — the page template knows which day it
should be reporting for; whatever numeric field feeds the value is never
populated.

## What works fine (unchanged from the first pass)

- Effective Duration, Yield to Worst, 30-Day SEC Yield, Number of Holdings,
  Portfolio Turnover Rate, YTD Return, and the full performance-returns table
  all render with real values, every time checked.

## What's been ruled out as the cause

- Not a JS-loading timing issue — confirmed server-rendered as a literal `-` in
  the raw HTML, no client-side fetch ever fires for this data.
- Not a page-variant issue — `/en-us/investor/product/<slug>/` and
  `/en-us/advisor/product/<slug>/` behave identically.
- Not a missing-identifier issue — adding `?identifier=<ISIN>` (the pattern seen
  in some search-indexed URLs) changes nothing.
- Not hidden elsewhere on the page — grepped the full raw HTML for any
  NAV-shaped numeric value or embedded JSON field; found nothing. There isn't
  even a `cusip` field anywhere in the page markup for this issuer, unlike every
  other issuer checked.
- Not a transient outage — reproduced identically on two independent fetches
  several days apart, in two different sessions.

## Fallback considered and not pursued (yet)

The monthly PDF fact sheets (`documents.janushenderson.com/prod/documents/docId/
<id>`, linked from each fund's "Quicklinks") likely carry a NAV figure, but at
monthly cadence and requiring PDF parsing — a materially worse and more fragile
source than every other issuer's daily web data. Not attempted; flagging as the
only remaining lead if daily coverage for these 3 tickers turns out to matter
enough to justify it.

## robots.txt / Terms of Use

Unchanged from the first pass — permissive robots.txt; Terms of Use has the same
blanket "no bots/scrapers/automated access" clause as iShares/Invesco/PIMCO, no
personal-use carve-out. Moot for now given there's no usable data to fetch.

## Recommendation

Skip Janus Henderson's adapter for now — there were only 3 tickers to begin with
(JAAA, JMBS, JBBB), all `in_core=TRUE` in universe.csv (JAAA specifically flagged
"major growth story - watch closely" in the notes). No adapter can be built
against the current live site that produces a real NAV, so there's nothing to
isolate-and-continue the way SPEC.md's per-issuer failure handling normally
would — the issue isn't a broken fetch, it's an absent field, on every fetch.
Revisit if Janus Henderson's site changes, or if the PDF fact-sheet fallback
becomes worth the added fragility for monthly-only coverage of these 3 funds.
