from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from bondflows import db
from bondflows.sources.history import (
    HistoryRow,
    ishares_historical_sheet,
    parse_ishares_historical,
    parse_ssga_navhist,
)

NA = np.nan


def _ss_row(*cells: str) -> str:
    return "<ss:Row>" + "".join(f'<ss:Cell><ss:Data ss:Type="String">{c}</ss:Data></ss:Cell>' for c in cells) + "</ss:Row>"


ISHARES_WORKBOOK = (
    '<ss:Workbook><ss:Worksheet ss:Name="Holdings"><ss:Table>'
    + _ss_row("Ticker", "Name")
    + _ss_row("X & Y", "unescaped ampersand, like the real file")
    + '</ss:Table></ss:Worksheet><ss:Worksheet ss:Name="Historical"><ss:Table>'
    + _ss_row("As Of", "NAV per Share", "Ex-Dividends", "Shares Outstanding")
    + _ss_row("Sep 25, 2026", "95.113595", "--", "1435600000")
    + _ss_row("Sep 24, 2026", "94.925223", "0.281", "1435100000")
    + _ss_row("Sep 23, 2026", "95.398171", "--", "--")
    + "</ss:Table></ss:Worksheet><ss:Worksheet ss:Name=\"Performance\"></ss:Worksheet></ss:Workbook>"
)


def test_parse_ishares_historical_reads_only_the_historical_sheet():
    rows = parse_ishares_historical(ishares_historical_sheet(ISHARES_WORKBOOK))
    assert rows[0] == HistoryRow(
        date(2026, 9, 25), Decimal("95.113595"), 1_435_600_000, Decimal("136545076982.00")
    )
    assert rows[1].shares_outstanding == 1_435_100_000
    # A missing share count stays missing, never zero.
    assert rows[2].shares_outstanding is None and rows[2].total_net_assets is None


def test_parse_ishares_historical_rejects_an_unexpected_header():
    sheet = '<ss:Worksheet ss:Name="Historical">' + _ss_row("Date", "Price") + "</ss:Worksheet>"
    with pytest.raises(ValueError, match="Historical header"):
        parse_ishares_historical(sheet)


def test_parse_ssga_navhist_skips_preamble_and_footer():
    df = pd.DataFrame([
        ["Fund Name:", "SPDR Portfolio Aggregate Bond ETF", NA, NA],
        ["Ticker Symbol:", "SPAB", NA, NA],
        [NA, NA, NA, NA],
        ["Date", "NAV", "Shares Outstanding", "Total Net Assets"],
        ["24-Sep-2026", 24.464219, 402600264, 9849301178.290001],
        ["23-Sep-2026", 24.585722, 402600264, 9898218230.42],
        [NA, NA, NA, NA],
        ["Before investing in a fund, consider its objectives...", NA, NA, NA],
    ])
    rows = parse_ssga_navhist(df)
    assert rows == [
        HistoryRow(date(2026, 9, 24), Decimal("24.464219"), 402_600_264, Decimal("9849301178.29")),
        HistoryRow(date(2026, 9, 23), Decimal("24.585722"), 402_600_264, Decimal("9898218230.42")),
    ]


def test_history_overrides_live_figures_but_keeps_live_only_fields(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    con.execute(
        "INSERT INTO fund_daily (ticker, asof_date, retrieved_at, shares_outstanding, nav_per_share, "
        "total_net_assets, effective_duration, source, source_is_stale) "
        "VALUES ('SPAB', '2026-09-24', '2026-09-24', 402600000, 24.464219, 9849301178.29, 6.1, 'SSGA', FALSE)"
    )
    row = HistoryRow(date(2026, 9, 24), Decimal("24.464219"), 402_600_264, Decimal("9849301178.29"))
    new = HistoryRow(date(2026, 9, 23), Decimal("24.585722"), 402_600_264, Decimal("9898218230.42"))

    assert db.apply_history_row(con, "SPAB", row, source="SSGA history", retrieved_at=datetime(2026, 9, 27)) is False
    assert db.apply_history_row(con, "SPAB", new, source="SSGA history", retrieved_at=datetime(2026, 9, 27)) is True

    rows = con.execute(
        "SELECT asof_date, shares_outstanding, effective_duration, source FROM fund_daily ORDER BY asof_date"
    ).fetchall()
    con.close()
    assert rows == [
        (date(2026, 9, 23), 402_600_264, None, "SSGA history"),
        (date(2026, 9, 24), 402_600_264, Decimal("6.1000"), "SSGA"),  # exact shares, live duration kept
    ]
