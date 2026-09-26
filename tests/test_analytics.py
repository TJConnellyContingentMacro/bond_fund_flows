"""SPEC.md §6.1/§6.3/§6.7 (build order step 5) — dollar aggregates.

Seeds fund_daily/fund_flows directly (same pattern as
tests/test_validation_harness.py's _seed_fund_daily) rather than going
through ingest.py/flows.py — these tests are about analytics.py's
aggregation SQL, not the upstream pipeline.
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta
from decimal import Decimal

from bondflows import db
from bondflows.analytics import compute_aggregates
from bondflows.universe import Fund


def make_fund(ticker: str, sleeve: str, sub_sleeve: str, in_core: bool = True) -> Fund:
    return Fund(
        ticker=ticker,
        name=f"{ticker} test fund",
        issuer="test_issuer",
        sleeve=sleeve,
        sub_sleeve=sub_sleeve,
        in_core=in_core,
        confidence="high",
        issuer_id=None,
        product_url=None,
        notes="",
    )


def seed_fund_flow(
    con,
    ticker: str,
    flow_date: date,
    flow_usd: Decimal | None,
    flag: str = "clean",
    *,
    dv01_usd_per_bp: Decimal | None = None,
    spread_dv01_usd_per_bp: Decimal | None = None,
    spread_dv01_is_proxied: bool | None = None,
) -> None:
    con.execute(
        """
        INSERT INTO fund_flows (
            ticker, flow_date, flow_usd, flag,
            dv01_usd_per_bp, spread_dv01_usd_per_bp, spread_dv01_is_proxied
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [ticker, flow_date, flow_usd, flag, dv01_usd_per_bp, spread_dv01_usd_per_bp, spread_dv01_is_proxied],
    )


def seed_fund_daily_tna(con, ticker: str, asof_date: date, total_net_assets: Decimal) -> None:
    con.execute(
        """
        INSERT INTO fund_daily (ticker, asof_date, retrieved_at, total_net_assets, source, source_is_stale)
        VALUES (?, ?, ?, ?, 'test', FALSE)
        """,
        [ticker, asof_date, asof_date, total_net_assets],
    )


def seed_overlay_holding(
    con, ticker: str, asof_date: date, underlying_ticker: str, weight_pct: Decimal
) -> None:
    con.execute(
        """
        INSERT INTO overlay_holdings (ticker, asof_date, underlying_ticker, weight_pct, source)
        VALUES (?, ?, ?, ?, 'test')
        """,
        [ticker, asof_date, underlying_ticker, weight_pct],
    )


def get_row(con, cut: str, asof_date: date) -> dict:
    cols = [
        "cut", "asof_date", "flow_usd", "flow_usd_net", "ogr", "flow_5d", "flow_20d",
        "flow_mtd", "flow_qtd", "zscore_252d", "has_imputed_or_suspect", "n_funds",
        "dv01_usd_per_bp", "spread_dv01_usd_per_bp", "has_proxied_spread_dv01",
    ]
    row = con.execute(
        f"SELECT {', '.join(cols)} FROM flow_aggregates WHERE cut = ? AND asof_date = ?",
        [cut, asof_date],
    ).fetchone()
    assert row is not None, f"no flow_aggregates row for cut={cut} date={asof_date}"
    return dict(zip(cols, row))


def test_total_and_ex_bills_exclude_bill_like_tickers(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    funds = [
        make_fund("AAA", "ig_corp", "broad"),
        make_fund("BILL1", "ust_ultrashort", "bills"),
    ]
    day = date(2026, 9, 18)
    seed_fund_flow(con, "AAA", day, Decimal("100"))
    seed_fund_flow(con, "BILL1", day, Decimal("50"))

    compute_aggregates(con, funds=funds)

    total = get_row(con, "total", day)
    assert total["flow_usd"] == Decimal("150")
    assert total["n_funds"] == 2

    ex_bills = get_row(con, "ex_bills", day)
    assert ex_bills["flow_usd"] == Decimal("100")
    assert ex_bills["n_funds"] == 1
    con.close()


def test_muni_excluded_only_from_ex_bills_ex_muni(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    funds = [
        make_fund("AAA", "ig_corp", "broad"),
        make_fund("MUNI1", "muni", "broad"),
    ]
    day = date(2026, 9, 18)
    seed_fund_flow(con, "AAA", day, Decimal("100"))
    seed_fund_flow(con, "MUNI1", day, Decimal("30"))

    compute_aggregates(con, funds=funds)

    ex_bills = get_row(con, "ex_bills", day)
    assert ex_bills["flow_usd"] == Decimal("130")  # muni isn't bill-like

    ex_bills_ex_muni = get_row(con, "ex_bills_ex_muni", day)
    assert ex_bills_ex_muni["flow_usd"] == Decimal("100")
    con.close()


def test_credit_and_rates_buckets(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    funds = [
        make_fund("AAA", "ig_corp", "broad"),  # credit
        make_fund("RATE1", "ust_long", "long"),  # rates
        make_fund("MIX1", "aggregate", "index"),  # mixed_other, excluded from both
    ]
    day = date(2026, 9, 18)
    seed_fund_flow(con, "AAA", day, Decimal("100"))
    seed_fund_flow(con, "RATE1", day, Decimal("200"))
    seed_fund_flow(con, "MIX1", day, Decimal("300"))

    compute_aggregates(con, funds=funds)

    assert get_row(con, "credit", day)["flow_usd"] == Decimal("100")
    assert get_row(con, "rates", day)["flow_usd"] == Decimal("200")
    assert get_row(con, "mixed_other", day)["flow_usd"] == Decimal("300")

    total = con.execute(
        "SELECT COUNT(*) FROM flow_aggregates WHERE cut IN ('credit', 'rates') AND asof_date = ?",
        [day],
    ).fetchone()[0]
    assert total == 2  # MIX1 contributes to neither
    con.close()


def test_rolling_sums_are_cumulative_over_observed_days(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    funds = [make_fund("AAA", "ig_corp", "broad")]
    day1, day2, day3 = date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)
    seed_fund_flow(con, "AAA", day1, Decimal("10"))
    seed_fund_flow(con, "AAA", day2, Decimal("20"))
    seed_fund_flow(con, "AAA", day3, Decimal("30"))

    compute_aggregates(con, funds=funds)

    # Only 3 observed days exist for this cut, so both the 5-day and 20-day
    # windows are just the full cumulative sum by day 3.
    row = get_row(con, "total", day3)
    assert row["flow_5d"] == Decimal("60")
    assert row["flow_20d"] == Decimal("60")
    assert row["flow_mtd"] == Decimal("60")  # all 3 days are in September

    row2 = get_row(con, "total", day2)
    assert row2["flow_5d"] == Decimal("30")
    con.close()


def test_ogr_uses_prior_day_total_net_assets(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    funds = [make_fund("AAA", "ig_corp", "broad")]
    day0, day1 = date(2026, 9, 17), date(2026, 9, 18)
    seed_fund_daily_tna(con, "AAA", day0, Decimal("1000000000.00"))
    # flows.py always writes a fund_daily row for the flow's own date too
    # (that's where nav_per_share(t) comes from) — the LAG in analytics.py's
    # SQL reads *that* row's predecessor, so day1 needs a fund_daily row of
    # its own for the join to find anything.
    seed_fund_daily_tna(con, "AAA", day1, Decimal("1005000000.00"))
    seed_fund_flow(con, "AAA", day1, Decimal("5000000"))

    compute_aggregates(con, funds=funds)

    row = get_row(con, "total", day1)
    assert row["ogr"] == Decimal("5000000") / Decimal("1000000000.00")
    con.close()


def test_flow_usd_net_backs_out_overlay_funds_underlying_etf_flow(tmp_path):
    """SPEC.md §6.2, narrowly scoped to TLTW/HYGW/LQDW (overlay.py): a
    dollar into TLTW is (almost) a dollar into TLT, so it should net out of
    flow_usd_net using the *prior* day's holdings weight. A ticker with no
    overlay_holdings row (AAA) must be completely unaffected."""
    con = db.connect(tmp_path / "db.duckdb")
    funds = [
        make_fund("TLTW", "overlay", "buywrite"),
        make_fund("AAA", "ig_corp", "broad"),
    ]
    day0, day1 = date(2026, 9, 17), date(2026, 9, 18)
    seed_overlay_holding(con, "TLTW", day0, "TLT", Decimal("100.00"))
    # fetch_overlay_holdings.py writes a row every day it runs (including
    # day1) — the LAG in analytics.py's SQL reads *that* row's predecessor,
    # same reason the OGR test needs a fund_daily row on the flow's own date.
    seed_overlay_holding(con, "TLTW", day1, "TLT", Decimal("99.50"))
    seed_fund_flow(con, "TLTW", day1, Decimal("1000000"))
    seed_fund_flow(con, "AAA", day1, Decimal("500"))

    compute_aggregates(con, funds=funds)

    total = get_row(con, "total", day1)
    assert total["flow_usd"] == Decimal("1000500")
    assert total["flow_usd_net"] == Decimal("500")  # TLTW's $1mm nets out entirely

    overlay_cut = get_row(con, "overlay", day1)
    assert overlay_cut["flow_usd"] == Decimal("1000000")
    assert overlay_cut["flow_usd_net"] == Decimal("0")

    credit = get_row(con, "credit", day1)
    assert credit["flow_usd"] == Decimal("500")
    assert credit["flow_usd_net"] == Decimal("500")  # unaffected, no overlay ticker here
    con.close()


def test_dv01_aggregates_by_sum_and_surfaces_proxy_flag(tmp_path):
    """SPEC.md §6.4/§6.5: dv01/spread_dv01 aggregate by simple sum, and a cut
    shows has_proxied_spread_dv01 = TRUE if any constituent ticker's spread
    DV01 used the effective-duration stand-in — never silently blended in."""
    con = db.connect(tmp_path / "db.duckdb")
    funds = [
        make_fund("HYG", "hy_corp", "broad"),
        make_fund("ANGL", "hy_corp", "fallen_angel"),
    ]
    day = date(2026, 9, 18)
    seed_fund_flow(
        con, "HYG", day, Decimal("1000000"),
        dv01_usd_per_bp=Decimal("3000"), spread_dv01_usd_per_bp=Decimal("3000"),
        spread_dv01_is_proxied=True,
    )
    seed_fund_flow(
        con, "ANGL", day, Decimal("500000"),
        dv01_usd_per_bp=Decimal("2000"), spread_dv01_usd_per_bp=Decimal("2100"),
        spread_dv01_is_proxied=False,
    )

    compute_aggregates(con, funds=funds)

    row = get_row(con, "hy_corp", day)
    assert row["dv01_usd_per_bp"] == Decimal("5000")
    assert row["spread_dv01_usd_per_bp"] == Decimal("5100")
    assert row["has_proxied_spread_dv01"] is True  # HYG's proxy, even though ANGL's wasn't
    con.close()


def test_imputed_day_counts_toward_flow_but_not_zscore_calibration(tmp_path):
    """A day with a huge `imputed`-flagged flow must still show up in that
    day's raw flow_usd (it's real, if lumpy, dollar movement), but must not
    distort the trailing-252-day mean/stdev used to z-score a later day."""
    con = db.connect(tmp_path / "db.duckdb")
    funds = [make_fund("AAA", "ig_corp", "broad")]
    start = date(2026, 1, 5)

    clean_values = []
    d = start
    for i in range(25):
        value = Decimal(1_000_000 + (i % 5) * 10_000)
        seed_fund_flow(con, "AAA", d, value, flag="clean")
        clean_values.append(value)
        d += timedelta(days=1)

    imputed_day = d
    seed_fund_flow(con, "AAA", imputed_day, Decimal("500000000"), flag="imputed")
    d += timedelta(days=1)

    final_day = d
    final_value = Decimal("1005000")
    seed_fund_flow(con, "AAA", final_day, final_value, flag="clean")

    compute_aggregates(con, funds=funds)

    imputed_row = get_row(con, "total", imputed_day)
    assert imputed_row["flow_usd"] == Decimal("500000000")  # raw dollars: included
    assert imputed_row["has_imputed_or_suspect"] is True

    final_row = get_row(con, "total", final_day)
    assert final_row["has_imputed_or_suspect"] is False

    # The trailing window is "as of and including" the day being scored, so
    # final_day's own value is part of its own calibration set — only the
    # imputed day is excluded. Mirror that here rather than excluding
    # final_value from the expected calibration set too.
    calibration_floats = [float(v) for v in clean_values] + [float(final_value)]
    expected_mean = statistics.mean(calibration_floats)
    expected_stdev = statistics.stdev(calibration_floats)
    expected_z = (float(final_value) - expected_mean) / expected_stdev

    assert final_row["zscore_252d"] is not None
    assert abs(float(final_row["zscore_252d"]) - expected_z) < 0.01
    # If the $500mm imputed day had leaked into calibration, the mean/stdev
    # would be dominated by it and this z-score would come out near zero
    # instead of reflecting final_value's position among the small clean
    # values — bound it away from that failure mode explicitly.
    assert abs(float(final_row["zscore_252d"])) > 0.5
    con.close()
