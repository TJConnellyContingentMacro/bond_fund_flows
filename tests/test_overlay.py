"""SPEC.md §6.2, narrowly scoped to TLTW/HYGW/LQDW (see overlay.py). Tests
the CSV-parsing logic against a fixture shaped like the real iShares
holdings download (two concatenated fund-holdings blocks — only the first
block's first data row is the underlying ETF holding)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bondflows.overlay import _parse_holdings_csv

TLTW_FIXTURE = """iShares 20+ Year Treasury Bond BuyWrite Strategy ETF
Fund Holdings as of,"Sep 24, 2026"
Inception Date,"Aug 18, 2022"
Shares Outstanding,"84,075,000.00"
Stock,"-"
Bond,"-"
Cash,"-"
Other,"-"

Name,Type,Sector,Asset Class,Market Value,Weight (%),Notional Value,Par Value,Price
"ISHARES 20+ YEAR TREASURY BOND ETF","EQUITY","Treasury","Fixed Income","1,734,914,016.00","100.17","1,734,914,016.00","21,844,800.00","79.42"
"BLK CSH FND TREASURY SL AGENCY","FUND","Cash and/or Derivatives","Money Market","2,810,000.00","0.16","2,810,000.00","2,810,000.00","1.00"
"USD CASH","CASH","Cash and/or Derivatives","Cash","-189,191.98","-0.01","-189,191.98","-189,192.00","100.00"
"OCT26 TLT US C @ 82","OPTION","Cash and/or Derivatives","Other Derivatives","-5,545,472.94","-0.32","-302,030,039.99","-218,448.00","0.25"

iShares 20+ Year Treasury Bond BuyWrite Strategy ETF
Fund Holdings as of,"Sep 24, 2026"
Inception Date,"Aug 18, 2022"
Shares Outstanding,"84,075,000.00"
Stock,"-"
Bond,"-"
Cash,"-"
Other,"-"

Name,Sector,Asset Class,Market Value,Weight (%)
"TREASURY BOND","Treasuries","Fixed Income","78,806,734.26","4.55"
"TREASURY BOND","Treasuries","Fixed Income","74,740,963.27","4.32"
"""


def test_parses_asof_date_and_top_holding_from_first_block_only():
    asof_date, name, weight_pct = _parse_holdings_csv(TLTW_FIXTURE)
    assert asof_date == date(2026, 9, 24)
    assert name == "ISHARES 20+ YEAR TREASURY BOND ETF"
    assert weight_pct == Decimal("100.17")
    # Confirms it stopped at the first block: the second block's much
    # smaller weights (4.55, 4.32, ...) must never be picked up as "the"
    # holding.
    assert weight_pct != Decimal("4.55")


def test_raises_loudly_if_holdings_header_is_missing():
    """A structural change to the CSV (no `Name` header row at all) must
    surface as an error, not silently return nothing/wrong data."""
    with pytest.raises(ValueError, match="holdings header"):
        _parse_holdings_csv('Fund Holdings as of,"Sep 24, 2026"\nsome,other,shape\n')
