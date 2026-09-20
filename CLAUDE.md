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
Python 3.11+, DuckDB, pandas, requests, pytest. No ORM, no framework.

## Discovered issuer conventions
(fill in as discovery completes — per issuer: endpoint, field names, units,
update time, trade-date vs settlement-date convention)
