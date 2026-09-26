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
    assert snapshot["flows"]["aggregates"] == []


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
