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
    flag                    VARCHAR NOT NULL,
    PRIMARY KEY (ticker, flow_date)
);

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
