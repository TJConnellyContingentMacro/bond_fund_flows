"""SPEC.md §6.4/§6.5 (build order step 10): DV01 and spread DV01, computed
in flows.py using the same-day duration figures already captured in
fund_daily. Seeds fund_daily directly, same pattern as
test_validation_harness.py's _seed_fund_daily."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from bondflows import db
from bondflows.flows import SPREAD_DV01_ELIGIBLE_SLEEVES, compute_ticker_flows


def _seed(con, ticker: str, rows: list[tuple]) -> None:
    """rows: (asof_date, shares, nav, tna, effective_duration, spread_duration)."""
    for asof_date, shares, nav, tna, eff_dur, spread_dur in rows:
        con.execute(
            """
            INSERT INTO fund_daily (
                ticker, asof_date, retrieved_at, shares_outstanding, nav_per_share,
                total_net_assets, effective_duration, spread_duration, source, source_is_stale
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test', FALSE)
            """,
            [ticker, asof_date, asof_date, shares, nav, tna, eff_dur, spread_dur],
        )


def test_dv01_uses_same_day_effective_duration(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    day0, day1 = date(2026, 9, 17), date(2026, 9, 18)
    _seed(
        con,
        "AAA",
        [
            (day0, 10_000_000, Decimal("100.00"), None, Decimal("5.00"), None),
            (day1, 10_050_000, Decimal("100.10"), None, Decimal("5.10"), None),
        ],
    )

    flows = compute_ticker_flows(con, "AAA", splits={}, sleeve="ust_long")
    con.close()

    flow_usd = flows[0]["flow_usd"]
    assert flow_usd == Decimal(50_000) * Decimal("100.10")
    # Uses day1's (the observation's own day) effective_duration, not day0's.
    assert flows[0]["dv01_usd_per_bp"] == flow_usd * Decimal("5.10") * Decimal("0.0001")


def test_dv01_null_without_effective_duration(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    day0, day1 = date(2026, 9, 17), date(2026, 9, 18)
    _seed(
        con,
        "AAA",
        [
            (day0, 10_000_000, Decimal("100.00"), None, None, None),
            (day1, 10_050_000, Decimal("100.10"), None, None, None),
        ],
    )

    flows = compute_ticker_flows(con, "AAA", splits={}, sleeve="ust_long")
    con.close()

    assert flows[0]["dv01_usd_per_bp"] is None


def test_spread_dv01_uses_real_spread_duration_when_available(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    assert "hy_corp" in SPREAD_DV01_ELIGIBLE_SLEEVES
    day0, day1 = date(2026, 9, 17), date(2026, 9, 18)
    _seed(
        con,
        "ANGL",
        [
            (day0, 10_000_000, Decimal("100.00"), None, Decimal("4.50"), Decimal("4.60")),
            (day1, 10_050_000, Decimal("100.10"), None, Decimal("4.52"), Decimal("4.57")),
        ],
    )

    flows = compute_ticker_flows(con, "ANGL", splits={}, sleeve="hy_corp")
    con.close()

    flow_usd = flows[0]["flow_usd"]
    assert flows[0]["spread_dv01_usd_per_bp"] == flow_usd * Decimal("4.57") * Decimal("0.0001")
    assert flows[0]["spread_dv01_is_proxied"] is False


def test_spread_dv01_proxies_with_effective_duration_when_spread_duration_missing(tmp_path):
    """The real situation for 27 of 28 tickers in the eligible sleeves as of
    2026-09-26 — only VanEck's ANGL publishes real spread_duration."""
    con = db.connect(tmp_path / "db.duckdb")
    day0, day1 = date(2026, 9, 17), date(2026, 9, 18)
    _seed(
        con,
        "HYG",
        [
            (day0, 10_000_000, Decimal("100.00"), None, Decimal("3.05"), None),
            (day1, 10_050_000, Decimal("100.10"), None, Decimal("3.07"), None),
        ],
    )

    flows = compute_ticker_flows(con, "HYG", splits={}, sleeve="hy_corp")
    con.close()

    flow_usd = flows[0]["flow_usd"]
    assert flows[0]["spread_dv01_usd_per_bp"] == flow_usd * Decimal("3.07") * Decimal("0.0001")
    assert flows[0]["spread_dv01_is_proxied"] is True


def test_spread_dv01_not_computed_outside_eligible_sleeves(tmp_path):
    """§6.5 scopes spread DV01 to ig_corp/hy_corp/loans_clo/em_debt (and the
    unimplemented credit-portion-of-aggregate case) — a rates fund like a
    Treasury ETF gets no spread_dv01 even if it happened to have duration
    data, since spread duration isn't a meaningful concept there."""
    con = db.connect(tmp_path / "db.duckdb")
    day0, day1 = date(2026, 9, 17), date(2026, 9, 18)
    _seed(
        con,
        "TLT",
        [
            (day0, 10_000_000, Decimal("100.00"), None, Decimal("16.00"), Decimal("0.10")),
            (day1, 10_050_000, Decimal("100.10"), None, Decimal("16.05"), Decimal("0.10")),
        ],
    )

    flows = compute_ticker_flows(con, "TLT", splits={}, sleeve="ust_long")
    con.close()

    assert flows[0]["spread_dv01_usd_per_bp"] is None
    assert flows[0]["spread_dv01_is_proxied"] is None
    # DV01 (unscoped) is unaffected by the sleeve restriction.
    assert flows[0]["dv01_usd_per_bp"] is not None


def test_spread_dv01_null_when_neither_duration_available(tmp_path):
    """The real situation for BKLN/CLOI/ICLO/SRLN/PCY as of 2026-09-26 —
    neither duration figure is published, so nothing to proxy with either."""
    con = db.connect(tmp_path / "db.duckdb")
    day0, day1 = date(2026, 9, 17), date(2026, 9, 18)
    _seed(
        con,
        "BKLN",
        [
            (day0, 10_000_000, Decimal("100.00"), None, None, None),
            (day1, 10_050_000, Decimal("100.10"), None, None, None),
        ],
    )

    flows = compute_ticker_flows(con, "BKLN", splits={}, sleeve="loans_clo")
    con.close()

    assert flows[0]["spread_dv01_usd_per_bp"] is None
    assert flows[0]["spread_dv01_is_proxied"] is None
