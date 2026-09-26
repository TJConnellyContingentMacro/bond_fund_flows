"""Dollar aggregates, SPEC.md §6.1/§6.3/§6.7 (build order step 5): total,
ex-bills, by-sleeve, and a credit-vs-rates cut, each with OGR, rolling sums,
and a z-score. Populates `flow_aggregates` from `fund_flows` + `fund_daily`.

Also computes `flow_usd_net` (§6.2, build order step 6) — `flow_usd` minus
the portion of TLTW/HYGW/LQDW's own flow attributable to their underlying
ETF holding (see `overlay.py`). This is a **narrower** double-counting
correction than §6.2 fully describes: the other candidate it names — active
multisector funds holding ETF positions "opportunistically" — is not netted
out here (see `overlay.py`'s docstring for why). `flow_usd_net == flow_usd`
for every cut that doesn't include TLTW/HYGW/LQDW, and for `credit`/`rates`
specifically, which exclude the `overlay` sleeve entirely (see
`MIXED_OTHER_SLEEVES` below).

Scope: every cut is computed over `in_core = TRUE` tickers only, matching
§6.1's "all core funds" wording for the headline and applied consistently to
every other cut for the same reason.
"""

from __future__ import annotations

import duckdb

from bondflows.db import upsert_flow_aggregate
from bondflows.universe import Fund, load_universe

# §6.1: "SGOV, BIL, SHV and the FRN funds" are the cash-management funds
# excluded from `ex_bills`. `sub_sleeve` cleanly captures both categories.
BILL_LIKE_SUBSLEEVES = frozenset({"bills", "frn"})

# Credit-vs-rates ("the top-line rotation read", §6.1) sleeve assignment.
# Every sleeve in universe.csv must land in exactly one of these three sets —
# checked by `_check_sleeve_coverage` so a newly added sleeve fails loudly
# instead of silently falling through or double-counting.
RATES_SLEEVES = frozenset(
    {
        "ust_ultrashort",
        "ust_short",
        "ust_intermediate",
        "ust_long",
        "ust_broad",
        "tips",
        "agency_mbs",
        "levered_inverse",  # TMF/TMV/TBT/TBF/UBT — all leveraged/inverse UST
        "intl_bond",  # BNDX/IAGG/BWX/IGOV — sovereign, duration-driven
    }
)
CREDIT_SLEEVES = frozenset(
    {
        "ig_corp",
        "hy_corp",
        "loans_clo",
        "em_debt",
        "muni",
        "hybrid_credit",
        "ultrashort_credit",
    }
)
# Genuinely mixed — reported as their own bucket, not folded into either side.
# `aggregate` (AGG/BND-style) blends government and credit; `active_multisector`
# funds rotate by mandate; `overlay` (TLTW/HYGW/LQDW) mixes a rates underlying
# and two credit underlyings inside one 3-ticker sleeve with no clean
# sleeve-level split.
MIXED_OTHER_SLEEVES = frozenset({"aggregate", "active_multisector", "overlay"})

ROLLING_WINDOWS = {"flow_5d": 5, "flow_20d": 20}
ZSCORE_WINDOW_DAYS = 252
ZSCORE_MIN_CLEAN_DAYS = 20


def _check_sleeve_coverage(funds: list[Fund]) -> None:
    all_groups = RATES_SLEEVES | CREDIT_SLEEVES | MIXED_OTHER_SLEEVES
    sleeves = {f.sleeve for f in funds}
    unassigned = sleeves - all_groups
    if unassigned:
        raise ValueError(
            f"Sleeve(s) {sorted(unassigned)} aren't in RATES_SLEEVES, CREDIT_SLEEVES, "
            "or MIXED_OTHER_SLEEVES — assign them in analytics.py before computing "
            "the credit/rates aggregates, rather than silently leaving them out."
        )


def _build_ticker_cuts(funds: list[Fund]) -> list[tuple[str, str]]:
    """Every (ticker, cut) membership row. A ticker can belong to several
    cuts at once (e.g. AGG is in `total`, `ex_bills`, `ex_bills_ex_muni`,
    `mixed_other`, and its own sleeve `aggregate`)."""
    _check_sleeve_coverage(funds)
    rows: list[tuple[str, str]] = []
    for f in funds:
        if not f.in_core:
            continue
        rows.append((f.ticker, "total"))

        is_bill_like = f.sub_sleeve in BILL_LIKE_SUBSLEEVES
        if not is_bill_like:
            rows.append((f.ticker, "ex_bills"))
            if f.sleeve != "muni":
                rows.append((f.ticker, "ex_bills_ex_muni"))

        if f.sleeve in RATES_SLEEVES:
            rows.append((f.ticker, "rates"))
        elif f.sleeve in CREDIT_SLEEVES:
            rows.append((f.ticker, "credit"))
        else:
            rows.append((f.ticker, "mixed_other"))

        rows.append((f.ticker, f.sleeve))
    return rows


_AGGREGATE_SQL = """
WITH tna_lag AS (
    SELECT ticker, asof_date,
           LAG(total_net_assets) OVER (PARTITION BY ticker ORDER BY asof_date) AS prior_tna
    FROM fund_daily
),
-- SPEC.md §6.2, narrowly scoped (see overlay.py): the prior day's weight is
-- used, not the current day's, matching §6.6's "computed from the prior day
-- to avoid look-ahead" principle for the same kind of holdings-weight
-- calculation.
overlay_lag AS (
    SELECT ticker, asof_date,
           LAG(weight_pct) OVER (PARTITION BY ticker ORDER BY asof_date) AS prior_weight_pct
    FROM overlay_holdings
),
flow_with_tna AS (
    SELECT
        f.ticker, f.flow_date, f.flow_usd, f.flag, t.prior_tna,
        COALESCE(f.flow_usd * o.prior_weight_pct / 100, 0) AS overlay_adjustment
    FROM fund_flows f
    LEFT JOIN tna_lag t ON t.ticker = f.ticker AND t.asof_date = f.flow_date
    LEFT JOIN overlay_lag o ON o.ticker = f.ticker AND o.asof_date = f.flow_date
),
cut_daily AS (
    SELECT
        c.cut,
        f.flow_date AS asof_date,
        SUM(f.flow_usd) AS flow_usd,
        SUM(f.flow_usd) - SUM(f.overlay_adjustment) AS flow_usd_net,
        SUM(f.prior_tna) AS total_prior_tna,
        BOOL_OR(f.flag IN ('imputed', 'suspect')) AS has_imputed_or_suspect,
        COUNT(*) AS n_funds
    FROM flow_with_tna f
    JOIN ticker_cuts c ON c.ticker = f.ticker
    GROUP BY c.cut, f.flow_date
),
windowed AS (
    SELECT
        cut, asof_date, flow_usd, flow_usd_net, has_imputed_or_suspect, n_funds,
        CASE WHEN total_prior_tna IS NOT NULL AND total_prior_tna != 0
             THEN flow_usd / total_prior_tna ELSE NULL END AS ogr,
        SUM(flow_usd) OVER (
            PARTITION BY cut ORDER BY asof_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
        ) AS flow_5d,
        SUM(flow_usd) OVER (
            PARTITION BY cut ORDER BY asof_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS flow_20d,
        SUM(flow_usd) OVER (
            PARTITION BY cut, DATE_TRUNC('month', asof_date) ORDER BY asof_date
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS flow_mtd,
        SUM(flow_usd) OVER (
            PARTITION BY cut, DATE_TRUNC('quarter', asof_date) ORDER BY asof_date
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS flow_qtd,
        AVG(CASE WHEN NOT has_imputed_or_suspect THEN flow_usd END) OVER (
            PARTITION BY cut ORDER BY asof_date
            ROWS BETWEEN {zscore_window_minus_1} PRECEDING AND CURRENT ROW
        ) AS calib_mean,
        STDDEV_SAMP(CASE WHEN NOT has_imputed_or_suspect THEN flow_usd END) OVER (
            PARTITION BY cut ORDER BY asof_date
            ROWS BETWEEN {zscore_window_minus_1} PRECEDING AND CURRENT ROW
        ) AS calib_stdev,
        COUNT(CASE WHEN NOT has_imputed_or_suspect THEN 1 END) OVER (
            PARTITION BY cut ORDER BY asof_date
            ROWS BETWEEN {zscore_window_minus_1} PRECEDING AND CURRENT ROW
        ) AS calib_n
    FROM cut_daily
)
SELECT
    cut, asof_date, flow_usd, flow_usd_net, ogr, flow_5d, flow_20d, flow_mtd, flow_qtd,
    CASE WHEN calib_n >= {min_clean_days} AND calib_stdev IS NOT NULL AND calib_stdev != 0
         THEN (flow_usd - calib_mean) / calib_stdev ELSE NULL END AS zscore_252d,
    has_imputed_or_suspect, n_funds
FROM windowed
ORDER BY cut, asof_date
""".format(
    zscore_window_minus_1=ZSCORE_WINDOW_DAYS - 1,
    min_clean_days=ZSCORE_MIN_CLEAN_DAYS,
)


def compute_aggregates(con: duckdb.DuckDBPyConnection, funds: list[Fund] | None = None) -> int:
    """Recomputes and upserts flow_aggregates for every cut. Cheap enough to
    always run in full, same rationale as flows.compute_all_flows."""
    funds = funds if funds is not None else load_universe()
    ticker_cuts = _build_ticker_cuts(funds)

    con.execute("CREATE OR REPLACE TEMP TABLE ticker_cuts (ticker VARCHAR, cut VARCHAR)")
    con.executemany("INSERT INTO ticker_cuts VALUES (?, ?)", ticker_cuts)

    rows = con.execute(_AGGREGATE_SQL).fetchall()
    columns = [d[0] for d in con.description]
    total = 0
    for values in rows:
        row = dict(zip(columns, values))
        upsert_flow_aggregate(con, row)
        total += 1
    return total
