"""DuckDB schema (SPEC.md §3) and loaders. Fully rebuildable from data/raw/ —
this file is disposable, data/raw/ is the asset."""

from __future__ import annotations

from pathlib import Path

import duckdb

from bondflows.sources.base import FundObservation

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "bondflows.duckdb"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fund_daily (
    ticker              VARCHAR NOT NULL,
    asof_date           DATE NOT NULL,
    retrieved_at        TIMESTAMP NOT NULL,
    shares_outstanding  BIGINT,
    nav_per_share       DECIMAL(18,6),
    total_net_assets    DECIMAL(18,2),
    market_close        DECIMAL(18,6),
    effective_duration  DECIMAL(8,4),
    spread_duration     DECIMAL(8,4),
    source              VARCHAR NOT NULL,
    source_is_stale     BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (ticker, asof_date)
);

CREATE TABLE IF NOT EXISTS fund_flows (
    ticker                  VARCHAR NOT NULL,
    flow_date               DATE NOT NULL,
    flow_usd                DECIMAL(18,2),
    shares_delta            BIGINT,
    shares_delta_adjusted   BIGINT,
    organic_growth_rate     DECIMAL(10,6),
    dv01_usd_per_bp         DECIMAL(18,2),
    spread_dv01_usd_per_bp  DECIMAL(18,2),
    -- SPEC.md §6.5: TRUE when spread_dv01_usd_per_bp used effective_duration
    -- as a stand-in for a missing spread_duration, FALSE when a real
    -- spread_duration was used, NULL when spread_dv01 wasn't computed at all
    -- (out-of-scope sleeve, or neither duration figure was available).
    spread_dv01_is_proxied BOOLEAN,
    flag                    VARCHAR NOT NULL,
    PRIMARY KEY (ticker, flow_date)
);

CREATE TABLE IF NOT EXISTS flow_aggregates (
    cut                     VARCHAR NOT NULL,
    asof_date               DATE NOT NULL,
    flow_usd                DECIMAL(18,2),
    flow_usd_net            DECIMAL(18,2),
    ogr                     DECIMAL(10,6),
    flow_5d                 DECIMAL(18,2),
    flow_20d                DECIMAL(18,2),
    flow_mtd                DECIMAL(18,2),
    flow_qtd                DECIMAL(18,2),
    zscore_252d             DECIMAL(10,4),
    has_imputed_or_suspect  BOOLEAN NOT NULL,
    n_funds                 INTEGER NOT NULL,
    dv01_usd_per_bp         DECIMAL(20,2),
    spread_dv01_usd_per_bp  DECIMAL(20,2),
    -- SPEC.md §6.5: "a proxied spread DV01 is an estimate, and the column
    -- should make that visible rather than laundering it into the
    -- aggregate" — TRUE if any constituent ticker's spread_dv01 for this
    -- cut/day used the effective-duration proxy.
    has_proxied_spread_dv01 BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (cut, asof_date)
);

-- SPEC.md §6.2, narrowly scoped to the option-overlay funds (TLTW/HYGW/
-- LQDW) — see overlay.py. `weight_pct` is the underlying ETF's share of
-- portfolio market value as published in that day's holdings file (e.g.
-- 100.17 for 100.17%), not a fraction.
CREATE TABLE IF NOT EXISTS overlay_holdings (
    ticker              VARCHAR NOT NULL,
    asof_date           DATE NOT NULL,
    underlying_ticker   VARCHAR NOT NULL,
    weight_pct          DECIMAL(7,4) NOT NULL,
    source              VARCHAR NOT NULL,
    PRIMARY KEY (ticker, asof_date)
);

-- SPEC.md §7: ICI's weekly estimated flows, its own table, never blended
-- into the daily fund_flows series. All dollar columns converted from the
-- source file's native millions to raw USD (`* 1_000_000`, a plain int per
-- CLAUDE.md's Decimal-scaling rule) for consistency with the rest of the
-- schema. `week_ended` is the Wednesday the file itself reports, not the
-- (later) date ICI posted the release.
CREATE TABLE IF NOT EXISTS ici_weekly_flows (
    week_ended          DATE NOT NULL,
    total_ltf_and_etf   DECIMAL(20,2) NOT NULL,
    equity_total        DECIMAL(20,2) NOT NULL,
    equity_domestic     DECIMAL(20,2) NOT NULL,
    equity_world        DECIMAL(20,2) NOT NULL,
    hybrid              DECIMAL(20,2) NOT NULL,
    bond_total          DECIMAL(20,2) NOT NULL,
    bond_taxable        DECIMAL(20,2) NOT NULL,
    bond_municipal      DECIMAL(20,2) NOT NULL,
    commodity           DECIMAL(20,2) NOT NULL,
    retrieved_at        TIMESTAMP NOT NULL,
    source_file         VARCHAR NOT NULL,
    PRIMARY KEY (week_ended)
);

-- SPEC.md §6.6, narrowly scoped (see mbs.py): agency-MBS share of portfolio
-- market value, for the 6 tickers with a confirmed full daily holdings file
-- with real market values (iShares: MBB/GNMA/AGG/IUSB; SSGA: SPMB/SPAB).
CREATE TABLE IF NOT EXISTS mbs_weights (
    ticker          VARCHAR NOT NULL,
    asof_date       DATE NOT NULL,
    mbs_weight_pct  DECIMAL(7,4) NOT NULL,
    source          VARCHAR NOT NULL,
    PRIMARY KEY (ticker, asof_date)
);

-- flow_usd(fund,t) * mbs_weight(fund,t-1)/100, SPEC.md §6.6. `SUM(...)
-- GROUP BY flow_date` over this table is total_etf_mbs_flow(t) — not
-- separately materialized, to avoid a second source of truth that could
-- go stale if this table is recomputed but a cached total isn't.
CREATE TABLE IF NOT EXISTS mbs_implied_flows (
    ticker                  VARCHAR NOT NULL,
    flow_date               DATE NOT NULL,
    implied_mbs_flow_usd    DECIMAL(18,2),
    PRIMARY KEY (ticker, flow_date)
);

-- Migrations for databases created before a column existed on an
-- already-existing table — CREATE TABLE IF NOT EXISTS is a no-op in that
-- case, so a pre-existing data/bondflows.duckdb needs these added
-- explicitly. Safe to run every connect(): IF NOT EXISTS makes each ADD
-- COLUMN a no-op once applied. `flow_usd_net` was a latent gap from §6.2
-- (step 6) that only surfaced once fund_flows had rows to aggregate.
ALTER TABLE flow_aggregates ADD COLUMN IF NOT EXISTS flow_usd_net DECIMAL(18,2);
ALTER TABLE fund_flows ADD COLUMN IF NOT EXISTS spread_dv01_is_proxied BOOLEAN;
ALTER TABLE flow_aggregates ADD COLUMN IF NOT EXISTS dv01_usd_per_bp DECIMAL(20,2);
ALTER TABLE flow_aggregates ADD COLUMN IF NOT EXISTS spread_dv01_usd_per_bp DECIMAL(20,2);
ALTER TABLE flow_aggregates ADD COLUMN IF NOT EXISTS has_proxied_spread_dv01 BOOLEAN DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS universe (
    ticker          VARCHAR NOT NULL,
    name            VARCHAR NOT NULL,
    issuer          VARCHAR NOT NULL,
    sleeve          VARCHAR NOT NULL,
    sub_sleeve      VARCHAR,
    inception_date  DATE,
    delisting_date  DATE,
    valid_from      DATE NOT NULL,
    valid_to        DATE,
    PRIMARY KEY (ticker, valid_from)
);
"""


def connect(db_path: Path = DB_PATH) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA)
    return con


def upsert_fund_daily(
    con: duckdb.DuckDBPyConnection,
    observation: FundObservation,
    *,
    retrieved_at,
    source: str,
    source_is_stale: bool,
) -> None:
    """Insert or replace one (ticker, asof_date) row. Never called with a
    None observation — ingest.py writes nothing for a ticker it couldn't
    fetch, rather than a row full of NULLs pretending to be an observation."""
    con.execute(
        """
        INSERT INTO fund_daily (
            ticker, asof_date, retrieved_at, shares_outstanding, nav_per_share,
            total_net_assets, market_close, effective_duration, spread_duration,
            source, source_is_stale
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ticker, asof_date) DO UPDATE SET
            retrieved_at = excluded.retrieved_at,
            shares_outstanding = excluded.shares_outstanding,
            nav_per_share = excluded.nav_per_share,
            total_net_assets = excluded.total_net_assets,
            market_close = excluded.market_close,
            effective_duration = excluded.effective_duration,
            spread_duration = excluded.spread_duration,
            source = excluded.source,
            source_is_stale = excluded.source_is_stale
        """,
        [
            observation.ticker,
            observation.asof_date,
            retrieved_at,
            observation.shares_outstanding,
            observation.nav_per_share,
            observation.total_net_assets,
            observation.market_close,
            observation.effective_duration,
            observation.spread_duration,
            source,
            source_is_stale,
        ],
    )


def apply_history_row(con: duckdb.DuckDBPyConnection, ticker: str, row, *, source: str, retrieved_at) -> bool:
    """Writes one issuer-history row (sources/history.py). History is the
    authority for shares/NAV/TNA on its dates, so it overrides those on an
    existing live row, keeping fields only the live adapter captures (durations,
    market close). Returns True if a new row was inserted."""
    exists = con.execute(
        "SELECT 1 FROM fund_daily WHERE ticker = ? AND asof_date = ?", [ticker, row.asof_date]
    ).fetchone()
    if exists:
        con.execute(
            """
            UPDATE fund_daily SET
                shares_outstanding = COALESCE(?, shares_outstanding),
                nav_per_share = COALESCE(?, nav_per_share),
                total_net_assets = COALESCE(?, total_net_assets),
                source_is_stale = FALSE
            WHERE ticker = ? AND asof_date = ?
            """,
            [row.shares_outstanding, row.nav_per_share, row.total_net_assets, ticker, row.asof_date],
        )
        return False
    con.execute(
        """
        INSERT INTO fund_daily (
            ticker, asof_date, retrieved_at, shares_outstanding, nav_per_share,
            total_net_assets, source, source_is_stale
        ) VALUES (?, ?, ?, ?, ?, ?, ?, FALSE)
        """,
        [ticker, row.asof_date, retrieved_at, row.shares_outstanding, row.nav_per_share, row.total_net_assets, source],
    )
    return True


def upsert_flow_aggregate(con: duckdb.DuckDBPyConnection, row: dict) -> None:
    con.execute(
        """
        INSERT INTO flow_aggregates (
            cut, asof_date, flow_usd, flow_usd_net, ogr, flow_5d, flow_20d, flow_mtd,
            flow_qtd, zscore_252d, has_imputed_or_suspect, n_funds,
            dv01_usd_per_bp, spread_dv01_usd_per_bp, has_proxied_spread_dv01
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (cut, asof_date) DO UPDATE SET
            flow_usd = excluded.flow_usd,
            flow_usd_net = excluded.flow_usd_net,
            ogr = excluded.ogr,
            flow_5d = excluded.flow_5d,
            flow_20d = excluded.flow_20d,
            flow_mtd = excluded.flow_mtd,
            flow_qtd = excluded.flow_qtd,
            zscore_252d = excluded.zscore_252d,
            has_imputed_or_suspect = excluded.has_imputed_or_suspect,
            n_funds = excluded.n_funds,
            dv01_usd_per_bp = excluded.dv01_usd_per_bp,
            spread_dv01_usd_per_bp = excluded.spread_dv01_usd_per_bp,
            has_proxied_spread_dv01 = excluded.has_proxied_spread_dv01
        """,
        [
            row["cut"],
            row["asof_date"],
            row["flow_usd"],
            row["flow_usd_net"],
            row["ogr"],
            row["flow_5d"],
            row["flow_20d"],
            row["flow_mtd"],
            row["flow_qtd"],
            row["zscore_252d"],
            row["has_imputed_or_suspect"],
            row["n_funds"],
            row["dv01_usd_per_bp"],
            row["spread_dv01_usd_per_bp"],
            row["has_proxied_spread_dv01"],
        ],
    )


def upsert_ici_weekly_flow(con: duckdb.DuckDBPyConnection, row: dict) -> None:
    con.execute(
        """
        INSERT INTO ici_weekly_flows (
            week_ended, total_ltf_and_etf, equity_total, equity_domestic, equity_world,
            hybrid, bond_total, bond_taxable, bond_municipal, commodity, retrieved_at, source_file
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (week_ended) DO UPDATE SET
            total_ltf_and_etf = excluded.total_ltf_and_etf,
            equity_total = excluded.equity_total,
            equity_domestic = excluded.equity_domestic,
            equity_world = excluded.equity_world,
            hybrid = excluded.hybrid,
            bond_total = excluded.bond_total,
            bond_taxable = excluded.bond_taxable,
            bond_municipal = excluded.bond_municipal,
            commodity = excluded.commodity,
            retrieved_at = excluded.retrieved_at,
            source_file = excluded.source_file
        """,
        [
            row["week_ended"],
            row["total_ltf_and_etf"],
            row["equity_total"],
            row["equity_domestic"],
            row["equity_world"],
            row["hybrid"],
            row["bond_total"],
            row["bond_taxable"],
            row["bond_municipal"],
            row["commodity"],
            row["retrieved_at"],
            row["source_file"],
        ],
    )


def upsert_mbs_weight(con: duckdb.DuckDBPyConnection, row: dict) -> None:
    con.execute(
        """
        INSERT INTO mbs_weights (ticker, asof_date, mbs_weight_pct, source)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (ticker, asof_date) DO UPDATE SET
            mbs_weight_pct = excluded.mbs_weight_pct,
            source = excluded.source
        """,
        [row["ticker"], row["asof_date"], row["mbs_weight_pct"], row["source"]],
    )


def upsert_mbs_implied_flow(con: duckdb.DuckDBPyConnection, row: dict) -> None:
    con.execute(
        """
        INSERT INTO mbs_implied_flows (ticker, flow_date, implied_mbs_flow_usd)
        VALUES (?, ?, ?)
        ON CONFLICT (ticker, flow_date) DO UPDATE SET
            implied_mbs_flow_usd = excluded.implied_mbs_flow_usd
        """,
        [row["ticker"], row["flow_date"], row["implied_mbs_flow_usd"]],
    )


def upsert_overlay_holding(con: duckdb.DuckDBPyConnection, row: dict) -> None:
    con.execute(
        """
        INSERT INTO overlay_holdings (ticker, asof_date, underlying_ticker, weight_pct, source)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (ticker, asof_date) DO UPDATE SET
            underlying_ticker = excluded.underlying_ticker,
            weight_pct = excluded.weight_pct,
            source = excluded.source
        """,
        [row["ticker"], row["asof_date"], row["underlying_ticker"], row["weight_pct"], row["source"]],
    )
