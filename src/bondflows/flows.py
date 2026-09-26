"""Flow calculation, SPEC.md §5: flow_usd(t) = ΔShares(t) * nav(t), plus the
split and staleness adjustments from §5.2. Populates `fund_flows` from
`fund_daily`.

Two of §5.2's four adjustments are genuinely not implemented yet, on
purpose rather than by oversight:

- **Settlement-date vs trade-date attribution.** No issuer's convention has
  been determined (see CLAUDE.md's "Discovered issuer conventions" table —
  every row says "Undetermined"). §5.2 explicitly sanctions this: "If an
  issuer's convention cannot be determined, document the ambiguity rather
  than guessing." `flow_date` is therefore whatever `asof_date` the adapter
  reported, unnormalized, for every issuer alike.
- **Launch/closure suppression.** Needs `inception_date`/`delisting_date`,
  which exist as columns on the `universe` table (db.py) but are not
  populated anywhere — universe.csv/universe.py don't carry them yet. A
  brand-new fund's first observation would currently produce one ordinary
  `clean`-flagged flow row rather than a suppressed one. Not a correctness
  bug for the current universe (no ticker in universe.csv launched inside
  the lookback window at the time of writing), but worth fixing before this
  matters — see the open item in CLAUDE.md.

Distributions need no adjustment (§5.2, verified) — nothing to implement.

**DV01 and spread DV01 (§6.4/§6.5)** are computed here too, now that enough
history exists for them to mean something. `dv01_usd_per_bp` is universal
(any ticker with a same-day `effective_duration`). `spread_dv01_usd_per_bp`
is scoped to `SPREAD_DV01_ELIGIBLE_SLEEVES` — §6.5 also names "the credit
portion of aggregate," which isn't computed: that needs a credit-vs-
government holdings decomposition for the `aggregate` sleeve (AGG/BND/SPAB/
SCHZ/IUSB) that doesn't exist, the same category of gap as the narrowed
scope in overlay.py/mbs.py. Checked the real data (2026-09-26): 27 of the
28 tickers in the eligible sleeves have no `spread_duration` at all (only
VanEck's ANGL does), so `spread_dv01_is_proxied = TRUE` — using
`effective_duration` as the stand-in §6.5 explicitly allows — is the normal
case here, not a rare fallback. 5 tickers (BKLN, CLOI, ICLO, SRLN, PCY) have
neither duration figure published at all, so `spread_dv01_usd_per_bp` stays
NULL for them rather than guessed. The optional `tenyr_equiv` unit
conversion (§6.4) isn't implemented — it needs a live on-the-run 10-year
Treasury figure, a data source this project doesn't have, and SPEC.md
explicitly marks it optional.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb

from bondflows.universe import load_universe

SPLITS_CSV = Path(__file__).resolve().parents[2] / "splits.csv"

# §6.4: dv01_flow(t) = flow_usd(t) * effective_duration(t) * 0.0001 — dollars
# of P&L per 1bp parallel move. Universal, no sleeve restriction.
DV01_BP_SCALE = Decimal("0.0001")
# §6.5: spread DV01 uses spread duration "for ig_corp, hy_corp, loans_clo,
# em_debt, and the credit portion of aggregate" — the aggregate sub-case is
# excluded here, see the module docstring.
SPREAD_DV01_ELIGIBLE_SLEEVES = frozenset({"ig_corp", "hy_corp", "loans_clo", "em_debt"})

# §5.2: "|Δshares| / shares(t-1) exceeds ~20%" is the candidate threshold.
SPLIT_CANDIDATE_SHARES_THRESHOLD = Decimal("0.20")
# How close shares_ratio * nav_ratio must be to 1 to call the two moves
# "inverse by a comparable ratio" rather than a coincidental large flow that
# happens to land near a round NAV move.
SPLIT_RATIO_PRODUCT_TOLERANCE = Decimal("0.10")
# A gap this many calendar days or less between two known observations is
# treated as an ordinary trading-day gap (handles Fri->Mon); anything longer
# (including market holidays, which this simple rule doesn't distinguish
# from real multi-day gaps) is flagged `imputed` rather than `clean` — the
# conservative direction, since `imputed` rows are excluded from z-score
# calibration per §5.2 rather than trusted as a normal daily print.
CLEAN_GAP_MAX_DAYS = 3


@dataclass(frozen=True)
class _Observation:
    asof_date: date
    shares_outstanding: int
    nav_per_share: Decimal
    total_net_assets: Decimal | None
    effective_duration: Decimal | None
    spread_duration: Decimal | None


def load_splits(path: Path = SPLITS_CSV) -> dict[tuple[str, date], Decimal]:
    """splits.csv: ticker,split_date,ratio,notes -> {(ticker, split_date): ratio}.
    `ratio` is new_shares_per_old_share (e.g. 0.25 for a 1-for-4 reverse
    split, 2 for a 2-for-1 forward split). Confirmed corporate actions only —
    §5.2: detected candidates are flagged `suspect` for a human to confirm
    here, never auto-applied."""
    splits: dict[tuple[str, date], Decimal] = {}
    if not path.exists():
        return splits
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("ticker") or not row.get("split_date"):
                continue
            splits[(row["ticker"], date.fromisoformat(row["split_date"]))] = Decimal(row["ratio"])
    return splits


def _is_split_candidate(shares_a: int, shares_b: int, nav_a: Decimal, nav_b: Decimal) -> bool:
    if shares_a == 0 or nav_a == 0:
        return False
    shares_ratio = Decimal(shares_b) / Decimal(shares_a)
    if abs(shares_ratio - 1) <= SPLIT_CANDIDATE_SHARES_THRESHOLD:
        return False
    nav_ratio = nav_b / nav_a
    return abs(shares_ratio * nav_ratio - 1) <= SPLIT_RATIO_PRODUCT_TOLERANCE


def compute_ticker_flows(
    con: duckdb.DuckDBPyConnection,
    ticker: str,
    splits: dict[tuple[str, date], Decimal],
    sleeve: str | None = None,
) -> list[dict]:
    """Walk `ticker`'s fund_daily history in order, skipping any day flagged
    `source_is_stale` (§5.2: "emit no flow for that day") and rows missing
    the fields the identity needs. A gap left by skipped/missing days means
    the next real observation's delta spans more than one day — that row
    gets `imputed` instead of `clean` rather than trying to distribute the
    change day-by-day (§5.2's stated preference).

    `sleeve` (from universe.csv) gates whether spread_dv01_usd_per_bp is
    computed at all — see SPREAD_DV01_ELIGIBLE_SLEEVES."""
    rows = con.execute(
        """
        SELECT asof_date, shares_outstanding, nav_per_share, total_net_assets,
               effective_duration, spread_duration
        FROM fund_daily
        WHERE ticker = ?
          AND shares_outstanding IS NOT NULL
          AND nav_per_share IS NOT NULL
          AND source_is_stale = FALSE
        ORDER BY asof_date
        """,
        [ticker],
    ).fetchall()
    observations = [_Observation(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]

    flow_rows: list[dict] = []
    prev: _Observation | None = None
    for obs in observations:
        if prev is None:
            prev = obs
            continue

        shares_delta = obs.shares_outstanding - prev.shares_outstanding
        gap_days = (obs.asof_date - prev.asof_date).days
        confirmed_ratio = splits.get((ticker, obs.asof_date))

        if confirmed_ratio is not None:
            prev_shares_restated = round(prev.shares_outstanding * confirmed_ratio)
            shares_delta_adjusted = obs.shares_outstanding - prev_shares_restated
            flow_usd = Decimal(shares_delta_adjusted) * obs.nav_per_share
            flag = "split_adjusted"
        elif _is_split_candidate(prev.shares_outstanding, obs.shares_outstanding, prev.nav_per_share, obs.nav_per_share):
            # §5.2: "Flag for review; do not auto-apply silently." No
            # confirmed entry in splits.csv yet, so flow_usd stays NULL
            # rather than reporting a shares_delta that's almost certainly a
            # mechanical split artifact, not a real flow.
            shares_delta_adjusted = None
            flow_usd = None
            flag = "suspect"
        else:
            shares_delta_adjusted = shares_delta
            flow_usd = Decimal(shares_delta) * obs.nav_per_share
            flag = "clean" if gap_days <= CLEAN_GAP_MAX_DAYS else "imputed"

        organic_growth_rate = None
        if flow_usd is not None and prev.total_net_assets:
            organic_growth_rate = flow_usd / prev.total_net_assets

        dv01_usd_per_bp = None
        if flow_usd is not None and obs.effective_duration is not None:
            dv01_usd_per_bp = flow_usd * obs.effective_duration * DV01_BP_SCALE

        spread_dv01_usd_per_bp = None
        spread_dv01_is_proxied = None
        if flow_usd is not None and sleeve in SPREAD_DV01_ELIGIBLE_SLEEVES:
            spread_duration_used = obs.spread_duration
            is_proxied = False
            if spread_duration_used is None:
                spread_duration_used = obs.effective_duration
                is_proxied = True
            if spread_duration_used is not None:
                spread_dv01_usd_per_bp = flow_usd * spread_duration_used * DV01_BP_SCALE
                spread_dv01_is_proxied = is_proxied

        flow_rows.append(
            {
                "ticker": ticker,
                "flow_date": obs.asof_date,
                "flow_usd": flow_usd,
                "shares_delta": shares_delta,
                "shares_delta_adjusted": shares_delta_adjusted,
                "organic_growth_rate": organic_growth_rate,
                "dv01_usd_per_bp": dv01_usd_per_bp,
                "spread_dv01_usd_per_bp": spread_dv01_usd_per_bp,
                "spread_dv01_is_proxied": spread_dv01_is_proxied,
                "flag": flag,
            }
        )
        prev = obs

    return flow_rows


def compute_all_flows(con: duckdb.DuckDBPyConnection) -> int:
    """Recomputes and upserts fund_flows for every ticker present in
    fund_daily. Cheap enough to always run in full (fund_daily is small);
    also means a newly-confirmed splits.csv entry gets applied retroactively
    just by rerunning this."""
    splits = load_splits()
    sleeve_by_ticker = {f.ticker: f.sleeve for f in load_universe()}
    tickers = [r[0] for r in con.execute("SELECT DISTINCT ticker FROM fund_daily").fetchall()]
    total = 0
    for ticker in tickers:
        sleeve = sleeve_by_ticker.get(ticker)
        for row in compute_ticker_flows(con, ticker, splits, sleeve):
            upsert_fund_flow(con, row)
            total += 1
    return total


def upsert_fund_flow(con: duckdb.DuckDBPyConnection, row: dict) -> None:
    con.execute(
        """
        INSERT INTO fund_flows (
            ticker, flow_date, flow_usd, shares_delta, shares_delta_adjusted,
            organic_growth_rate, dv01_usd_per_bp, spread_dv01_usd_per_bp,
            spread_dv01_is_proxied, flag
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ticker, flow_date) DO UPDATE SET
            flow_usd = excluded.flow_usd,
            shares_delta = excluded.shares_delta,
            shares_delta_adjusted = excluded.shares_delta_adjusted,
            organic_growth_rate = excluded.organic_growth_rate,
            dv01_usd_per_bp = excluded.dv01_usd_per_bp,
            spread_dv01_usd_per_bp = excluded.spread_dv01_usd_per_bp,
            spread_dv01_is_proxied = excluded.spread_dv01_is_proxied,
            flag = excluded.flag
        """,
        [
            row["ticker"],
            row["flow_date"],
            row["flow_usd"],
            row["shares_delta"],
            row["shares_delta_adjusted"],
            row["organic_growth_rate"],
            row["dv01_usd_per_bp"],
            row["spread_dv01_usd_per_bp"],
            row["spread_dv01_is_proxied"],
            row["flag"],
        ],
    )
