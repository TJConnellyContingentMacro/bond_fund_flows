"""SPEC.md §8 alerting: "adapter exception, coverage below threshold, any
fund with |OGR| > 10% in a day, any sleeve z-score beyond ±4, split
candidates awaiting review." Tests _check_alerts against seeded fixtures —
run_pipeline.py's subprocess orchestration itself isn't covered here (it's
a thin wrapper around scripts already tested individually)."""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_pipeline import _check_alerts  # noqa: E402

from bondflows import db


def test_clean_run_has_no_alerts(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    alerts = _check_alerts(con, date(2026, 9, 18), "Coverage: 100/100 (100.0%)")
    con.close()
    assert alerts == []


def test_coverage_below_threshold_alerts(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    alerts = _check_alerts(con, date(2026, 9, 18), "Coverage: 50/100 (50.0%)")
    con.close()
    assert any("50.0%" in a for a in alerts)


def test_missing_coverage_line_alerts(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    alerts = _check_alerts(con, date(2026, 9, 18), "some unrelated output, no coverage line")
    con.close()
    assert any("Coverage" in a for a in alerts)


def test_large_ogr_alerts(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    day = date(2026, 9, 18)
    con.execute(
        "INSERT INTO fund_flows (ticker, flow_date, organic_growth_rate, flag) VALUES (?, ?, ?, 'clean')",
        ["AAA", day, Decimal("0.15")],
    )
    alerts = _check_alerts(con, day, "Coverage: 100/100 (100.0%)")
    con.close()
    assert any("AAA" in a and "OGR" in a for a in alerts)


def test_small_ogr_does_not_alert(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    day = date(2026, 9, 18)
    con.execute(
        "INSERT INTO fund_flows (ticker, flow_date, organic_growth_rate, flag) VALUES (?, ?, ?, 'clean')",
        ["AAA", day, Decimal("0.02")],
    )
    alerts = _check_alerts(con, day, "Coverage: 100/100 (100.0%)")
    con.close()
    assert alerts == []


def test_extreme_zscore_alerts(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    day = date(2026, 9, 18)
    con.execute(
        """
        INSERT INTO flow_aggregates (cut, asof_date, zscore_252d, has_imputed_or_suspect, n_funds)
        VALUES (?, ?, ?, FALSE, 5)
        """,
        ["ig_corp", day, Decimal("4.5")],
    )
    alerts = _check_alerts(con, day, "Coverage: 100/100 (100.0%)")
    con.close()
    assert any("ig_corp" in a and "z-score" in a for a in alerts)


def test_suspect_flow_alerts_regardless_of_date(tmp_path):
    """Split candidates awaiting review accumulate until a human resolves
    them via splits.csv — they should surface even if flagged on an earlier
    day, not just today's run."""
    con = db.connect(tmp_path / "db.duckdb")
    con.execute(
        "INSERT INTO fund_flows (ticker, flow_date, flag) VALUES (?, ?, 'suspect')",
        ["AAA", date(2026, 9, 10)],
    )
    alerts = _check_alerts(con, date(2026, 9, 18), "Coverage: 100/100 (100.0%)")
    con.close()
    assert any("AAA" in a and "review" in a for a in alerts)
