# Universe & Taxonomy — Design Notes

~125 tickers. `in_core=TRUE` is the primary universe; `FALSE` is tracked but excluded from sleeve aggregates.

## Sleeves

`ust_ultrashort` · `ultrashort_credit` · `ust_short` · `ust_intermediate` · `ust_broad` · `ust_long` · `tips` · `agency_mbs` · `ig_corp` · `hy_corp` · `loans_clo` · `em_debt` · `muni` · `aggregate` · `active_multisector` · `intl_bond` · `levered_inverse` · `overlay` · `hybrid_credit`

## Decisions worth your review

- **Levered/inverse excluded from core.** TMF/TMV/TBT flows are trading noise, not allocation, and they're the main source of reverse splits. Kept in the file as a separate sentiment sleeve.
- **Option-overlay funds excluded.** TLTW/HYGW/LQDW flows aren't clean duration demand.
- **`ust_broad` split out for GOVT.** All-maturity — would double-count against the tenor buckets.
- **Ultrashort split into bills vs credit.** SGOV and JPST behave nothing alike. FRN funds (TFLO/USFR) flagged separately — near-zero duration, so DV01 contribution is ~0 regardless of dollar size.
- **STRIPS funds flagged** (EDV/ZROZ/GOVZ). Duration ~24+, so small dollar flows are large DV01 flows.
- **Intl bond in-core but hedged only.** BNDX/IAGG are USD-hedged — they carry foreign duration, not US. Keep them out of the US DV01 aggregate; separate line.
- **State munis excluded from core** to avoid double-counting against national funds.
- **Converts/preferreds optional.** Useful credit sentiment, not really bond duration.

## Deliberately omitted

- **Target-maturity ladders** (iBonds `IBDx`/`IBTx`, BulletShares `BSCx`). ~100+ tickers, individually small. They also terminate at maturity and return capital, which prints as a huge phantom outflow. If added later, handle as a group with explicit maturity-event suppression.
- **Single-tenor Treasuries** (UTWO/UTEN/UTHY) — in file, `in_core=FALSE`. Small, but the cleanest possible tenor-specific duration read if they grow.

## Before first run

- `confidence=med` rows need a ticker check — especially VBIL, MTBA, HYLB, CLOA/ICLO.
- The adapters will fail loudly on a bad ticker, so this validates itself on day one.
- No AUM figures included on purpose. Rank by AUM from your own first pull rather than a stale published list.

## MBS look-through targets

AGG, BND, SPAB, SCHZ, IUSB — plus the active multisector funds (BINC, PYLD, BOND, FBND, JCPB), which carry meaningful and *variable* MBS weight. Those are the interesting ones: weight changes there are an active manager's agency MBS view, separate from the flow.
