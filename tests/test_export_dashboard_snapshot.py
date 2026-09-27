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
    assert snapshot["flows"]["weekly"] == []
    assert snapshot["flows"]["latest_week"] is None


def _seed_daily(con, ticker: str, asof: str, shares: int | None, nav: str = "100") -> None:
    con.execute(
        "INSERT INTO fund_daily (ticker, asof_date, retrieved_at, shares_outstanding, nav_per_share, source, "
        "source_is_stale) VALUES (?, ?, ?, ?, ?, 'test', FALSE)",
        [ticker, asof, asof, shares, Decimal(nav)],
    )


def _fund(ticker: str, issuer: str, sleeve: str) -> Fund:
    return Fund(ticker=ticker, name=ticker, issuer=issuer, sleeve=sleeve, sub_sleeve="broad", in_core=True,
                confidence="high", issuer_id=None, product_url=None, notes="")


def test_weekly_view_separates_catch_ups_and_sums_each_week(tmp_path, monkeypatch):
    """Daily flows (including one across a holiday weekend) land in their
    week; a flow spanning a week of missed observations is a catch-up, listed
    on its own instead of inflating the week it happens to end in."""
    con = db.connect(tmp_path / "db.duckdb")
    funds = [
        _fund("AAA", "issuer_a", "ig_corp"),
        _fund("BBB", "issuer_a", "ust_long"),
        _fund("CCC", "issuer_b", "ig_corp"),
        _fund("DDD", "issuer_c", "ig_corp"),
        _fund("EEE", "issuer_c", "ig_corp"),
    ]
    monkeypatch.setattr("export_dashboard_snapshot.load_universe", lambda: funds)
    # AAA daily across Labor Day (Fri 9/4 -> Tue 9/8 is 4 days, still a daily flow) and into the next week.
    for asof in ("2026-09-03", "2026-09-04", "2026-09-08", "2026-09-14", "2026-09-15"):
        _seed_daily(con, "AAA", asof, 1_000_000)
    for asof in ("2026-09-11", "2026-09-14", "2026-09-15"):
        _seed_daily(con, "BBB", asof, 1_000_000)
    for asof in ("2026-09-08", "2026-09-15"):  # one reading a week apart: a catch-up
        _seed_daily(con, "CCC", asof, 1_000_000)
    for asof in ("2026-09-08", "2026-09-15"):
        _seed_daily(con, "DDD", asof, None)  # e.g. Vanguard: shares only published monthly
    _seed_daily(con, "EEE", "2026-09-15", 1_000_000)
    con.execute(
        "INSERT INTO fund_flows (ticker, flow_date, flow_usd, flag) VALUES "
        "('AAA', '2026-09-04', 10, 'clean'), ('AAA', '2026-09-08', 20, 'imputed'), "
        "('AAA', '2026-09-14', 30, 'imputed'), ('AAA', '2026-09-15', 40, 'clean'), "
        "('BBB', '2026-09-14', -5, 'clean'), ('BBB', '2026-09-15', -7, 'clean'), "
        "('CCC', '2026-09-15', 999, 'imputed')"
    )

    flows = build_snapshot(con)["flows"]
    con.close()

    weeks = {w["week_start"]: w for w in flows["weekly"]}
    assert weeks[date(2026, 8, 31)]["total"] == Decimal("10")
    assert weeks[date(2026, 9, 7)]["total"] == Decimal("20")  # the Labor Day Tuesday
    # AAA's 9/8 -> 9/14 flow spans 6 days, so it's out of the weekly series.
    assert weeks[date(2026, 9, 14)]["total"] == Decimal("28")  # 40 - 5 - 7, CCC's 999 excluded
    assert weeks[date(2026, 9, 14)]["credit"] == Decimal("40")
    assert weeks[date(2026, 9, 14)]["rates"] == Decimal("-12")

    lw = flows["latest_week"]
    assert lw["week_start"] == date(2026, 9, 14)
    headline = {h["cut"]: h for h in lw["headline"]}
    assert headline["total"]["prior_week_flow_usd"] == Decimal("20")
    bbb = next(f for f in lw["by_fund"] if f["ticker"] == "BBB")
    assert (bbb["flow_usd"], bbb["days"]) == (Decimal("-12"), 2)
    assert [c["ticker"] for c in flows["catch_up"]] == ["CCC"]

    reasons = {m["reason"]: m["tickers"] for m in flows["missing"]}
    assert reasons["no shares outstanding published on two dates yet"] == ["DDD"]
    assert reasons["only one clean observation so far"] == ["EEE"]


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
    _seed_daily(con, "AAA", str(day - timedelta(days=1)), 1_000_000)
    _seed_daily(con, "AAA", str(day), 1_200_000)
    con.execute(
        "INSERT INTO fund_flows (ticker, flow_date, flow_usd, organic_growth_rate, flag) VALUES (?, ?, 1, ?, 'clean')",
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
