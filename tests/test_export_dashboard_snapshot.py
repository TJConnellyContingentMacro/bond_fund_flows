"""export_dashboard_snapshot.py feeds the cloud dashboard-refresh routine
(SPEC.md §10) — these tests check the coverage-today vs. ever-covered
distinction and the notable-items thresholds, since those drive the
routine's alerting."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from export_dashboard_snapshot import build_snapshot  # noqa: E402

from bondflows import db
from bondflows.universe import Fund


def make_fund(ticker: str, issuer: str) -> Fund:
    return Fund(
        ticker=ticker, name=f"{ticker} fund", issuer=issuer, sleeve="ig_corp", sub_sleeve="broad",
        in_core=True, confidence="high", issuer_id=None, product_url=None, notes="",
    )


def test_covered_today_distinguishes_from_ever_covered(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "db.duckdb")
    yesterday = date.today() - timedelta(days=1)
    today = date.today()
    # AAA fetched yesterday only (an adapter that went quiet today).
    con.execute(
        "INSERT INTO fund_daily (ticker, asof_date, retrieved_at, source, source_is_stale) VALUES (?, ?, ?, 'test', FALSE)",
        ["AAA", yesterday, yesterday],
    )
    # BBB fetched today.
    con.execute(
        "INSERT INTO fund_daily (ticker, asof_date, retrieved_at, source, source_is_stale) VALUES (?, ?, ?, 'test', FALSE)",
        ["BBB", today, today],
    )
    monkeypatch.setattr(
        "export_dashboard_snapshot.load_universe",
        lambda: [make_fund("AAA", "issuer_a"), make_fund("BBB", "issuer_b")],
    )

    snapshot = build_snapshot(con)
    con.close()

    assert snapshot["coverage"]["total_covered"] == 2  # both have ever reported
    assert snapshot["coverage"]["total_covered_today"] == 1  # only BBB today
    assert snapshot["coverage"]["by_issuer"]["issuer_a"]["covered_today"] == 0
    assert snapshot["coverage"]["by_issuer"]["issuer_b"]["covered_today"] == 1


def test_flows_not_available_when_fund_flows_empty(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "db.duckdb")
    monkeypatch.setattr("export_dashboard_snapshot.load_universe", lambda: [make_fund("AAA", "issuer_a")])

    snapshot = build_snapshot(con)
    con.close()

    assert snapshot["flows"]["available"] is False
    assert snapshot["flows"]["by_fund"] == []
    assert snapshot["flows"]["headline"] == []


def _seed_daily(con, ticker: str, asof: str, shares: int | None, nav: str = "100") -> None:
    con.execute(
        "INSERT INTO fund_daily (ticker, asof_date, retrieved_at, shares_outstanding, nav_per_share, source, "
        "source_is_stale) VALUES (?, ?, ?, ?, ?, 'test', FALSE)",
        [ticker, asof, asof, shares, Decimal(nav)],
    )


def test_latest_period_flows_use_each_funds_own_latest_date(tmp_path, monkeypatch):
    """Issuers report on different lags, so each fund's latest flow can land
    on a different date — the headline totals sum each fund's own latest
    period rather than whatever single date happens to be newest."""
    con = db.connect(tmp_path / "db.duckdb")
    funds = [
        Fund(ticker="AAA", name="a", issuer="issuer_a", sleeve="ig_corp", sub_sleeve="broad", in_core=True,
             confidence="high", issuer_id=None, product_url=None, notes=""),
        Fund(ticker="BBB", name="b", issuer="issuer_b", sleeve="ust_long", sub_sleeve="long", in_core=True,
             confidence="high", issuer_id=None, product_url=None, notes=""),
        Fund(ticker="CCC", name="c", issuer="issuer_c", sleeve="ig_corp", sub_sleeve="broad", in_core=True,
             confidence="high", issuer_id=None, product_url=None, notes=""),
        Fund(ticker="DDD", name="d", issuer="issuer_c", sleeve="ig_corp", sub_sleeve="broad", in_core=True,
             confidence="high", issuer_id=None, product_url=None, notes=""),
    ]
    monkeypatch.setattr("export_dashboard_snapshot.load_universe", lambda: funds)
    for asof in ("2026-09-11", "2026-09-18", "2026-09-25"):
        _seed_daily(con, "AAA", asof, 1_000_000)
    for asof in ("2026-09-17", "2026-09-24"):
        _seed_daily(con, "BBB", asof, 1_000_000)
        _seed_daily(con, "CCC", asof, None)  # e.g. Vanguard: shares only published monthly
    _seed_daily(con, "DDD", "2026-09-18", 1_000_000)
    con.execute(
        "INSERT INTO fund_flows (ticker, flow_date, flow_usd, flag) VALUES "
        "('AAA', '2026-09-18', 100, 'clean'), ('AAA', '2026-09-25', 300, 'imputed'), "
        "('BBB', '2026-09-24', -50, 'imputed')"
    )

    flows = build_snapshot(con)["flows"]
    con.close()

    headline = {t["cut"]: t["flow_usd"] for t in flows["headline"]}
    assert headline["total"] == Decimal("250")  # AAA's 9/25 flow + BBB's 9/24 flow
    assert headline["credit"] == Decimal("300")
    assert headline["rates"] == Decimal("-50")
    assert flows["window_start"] == date(2026, 9, 17)
    assert flows["window_end"] == date(2026, 9, 25)
    aaa = next(r for r in flows["by_fund"] if r["ticker"] == "AAA")
    assert aaa["prior_date"] == date(2026, 9, 18)
    assert flows["largest_contributor"] == {
        "ticker": "AAA", "flow_usd": Decimal("300"), "total_ex_largest": Decimal("-50"),
    }
    reasons = {m["reason"]: m["tickers"] for m in flows["missing"]}
    assert reasons["no shares outstanding published on two dates yet"] == ["CCC"]
    assert reasons["only one clean observation so far"] == ["DDD"]


def test_overlay_and_mbs_tables_show_only_the_latest_date_per_ticker(tmp_path, monkeypatch):
    """overlay_holdings/mbs_weights accumulate history over time (e.g. a
    ticker fetched on consecutive days) -- the dashboard's tables are a
    current snapshot, not a time series, so only the most recent row per
    ticker should surface."""
    con = db.connect(tmp_path / "db.duckdb")
    monkeypatch.setattr("export_dashboard_snapshot.load_universe", lambda: [make_fund("AAA", "issuer_a")])
    con.execute(
        "INSERT INTO overlay_holdings (ticker, asof_date, underlying_ticker, weight_pct, source) VALUES "
        "('HYGW', '2026-09-23', 'HYG', 100.28, 'test'), ('HYGW', '2026-09-24', 'HYG', 100.15, 'test')"
    )
    con.execute(
        "INSERT INTO mbs_weights (ticker, asof_date, mbs_weight_pct, source) VALUES "
        "('MBB', '2026-09-23', 93.0, 'test'), ('MBB', '2026-09-24', 94.09, 'test')"
    )

    snapshot = build_snapshot(con)
    con.close()

    assert len(snapshot["overlay_holdings"]) == 1
    assert snapshot["overlay_holdings"][0]["asof_date"] == date(2026, 9, 24)
    assert snapshot["overlay_holdings"][0]["weight_pct"] == Decimal("100.15")
    assert len(snapshot["mbs_weights"]) == 1
    assert snapshot["mbs_weights"][0]["asof_date"] == date(2026, 9, 24)
    assert snapshot["mbs_weights"][0]["mbs_weight_pct"] == Decimal("94.09")


def test_large_ogr_and_suspect_flows_are_flagged_notable(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "db.duckdb")
    day = date.today()
    monkeypatch.setattr("export_dashboard_snapshot.load_universe", lambda: [make_fund("AAA", "issuer_a")])
    con.execute(
        "INSERT INTO fund_flows (ticker, flow_date, organic_growth_rate, flag) VALUES (?, ?, ?, 'clean')",
        ["AAA", day, Decimal("0.20")],
    )
    con.execute(
        "INSERT INTO flow_aggregates (cut, asof_date, has_imputed_or_suspect, n_funds) VALUES ('total', ?, FALSE, 1)",
        [day],
    )
    con.execute("INSERT INTO fund_flows (ticker, flow_date, flag) VALUES ('BBB', ?, 'suspect')", [day])

    snapshot = build_snapshot(con)
    con.close()

    assert any("AAA" in n and "OGR" in n for n in snapshot["notable"])
    assert any("suspect" in n.lower() or "review" in n.lower() for n in snapshot["notable"])
