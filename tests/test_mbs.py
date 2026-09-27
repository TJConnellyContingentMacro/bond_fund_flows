"""SPEC.md §6.6, narrowly scoped to iShares (MBB/GNMA/AGG/IUSB) and SSGA
(SPMB/SPAB) — see mbs.py's docstring for why. Tests the classification
logic and both issuers' parsers against fixtures shaped like real holdings
data (confirmed live 2026-09-26)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from bondflows import db
from bondflows.mbs import (
    compute_mbs_implied_flows,
    is_agency_mbs_by_name,
    parse_ishares_holdings,
    parse_ssga_holdings,
)

NA = np.nan


def test_implied_mbs_flow_uses_most_recent_weight_strictly_before_flow_date(tmp_path):
    """Holdings files are fetched on their own cadence, so a weight usually
    won't be dated exactly on a flow date. Use the latest one strictly before
    it (SPEC.md §6.6's prior-day rule) — never a same-day or later weight."""
    con = db.connect(tmp_path / "db.duckdb")
    con.execute(
        "INSERT INTO mbs_weights (ticker, asof_date, mbs_weight_pct, source) VALUES "
        "('MBB', '2026-09-20', 90.0, 'test'), ('MBB', '2026-09-24', 94.0, 'test'), "
        "('MBB', '2026-09-25', 50.0, 'test')"
    )
    con.execute(
        "INSERT INTO fund_flows (ticker, flow_date, flow_usd, flag) VALUES ('MBB', '2026-09-25', -1000000, 'clean')"
    )

    written = compute_mbs_implied_flows(con)
    implied = con.execute("SELECT implied_mbs_flow_usd FROM mbs_implied_flows WHERE ticker = 'MBB'").fetchone()[0]
    con.close()

    assert written == 1
    assert implied == Decimal("-940000.00")  # 94% (9/24), not 50% (same-day) or 90% (older)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("FHLMC 30YR UMBS SUPER", True),
        ("FNMA POOL MA4547 FN 02/52 FIXED 2", True),
        ("FNMA TBA 30 YR 5.5 SINGLE FAMILY MORTGAGE", True),
        ("GNMA II POOL MB0424 G2 06/55 FIXED 5.5", True),
        ("FED HM LN PC POOL RQ0094 FR 02/56 FIXED 5", True),
        ("FHLMC MULTIFAMILY STRUCTURED P FHMS K 155 A2", True),
        # Agency debt, not a mortgage pool — must be excluded even though it
        # matches an issuer prefix. Confirmed real trap in SPAB's holdings.
        ("FREDDIE MAC NOTES 07/32 6.25", False),
        ("FHLMC REFERENCE NOTE", False),
        # Private-label CMBS conduits — never agency, regardless of sector.
        ("WFCM 2024-C1 A5", False),
        ("JPMCC 2023-5C1 A3", False),
        ("TREASURY NOTE", False),
        ("BANK OF AMERICA CORP", False),
    ],
)
def test_is_agency_mbs_by_name(name, expected):
    assert is_agency_mbs_by_name(name) is expected


ISHARES_FIXTURE = """iShares Core U.S. Aggregate Bond ETF
Fund Holdings as of,"Sep 24, 2026"
Inception Date,"Sep 22, 2003"
Shares Outstanding,"1,435,100,000.00"
Stock,"-"
Bond,"-"
Cash,"-"
Other,"-"

Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Par Value,CUSIP
"FNMA 30YR UMBS","MBS Pass-Through","Fixed Income","100.00","20.00","100.00","100.00","X"
"TREASURY NOTE","Treasury","Fixed Income","100.00","46.00","100.00","100.00","X"
"FHMS_K155 A2","CMBS","Fixed Income","100.00","0.50","100.00","100.00","X"
"WFCM 2024-C1 A5","CMBS","Fixed Income","100.00","0.30","100.00","100.00","X"
"FHLMC REFERENCE NOTE","Agency","Fixed Income","100.00","0.20","100.00","100.00","X"
"BLACKROCK CASH CL INST SL AGENCY","Cash and/or Derivatives","Money Market","100.00","0.55","100.00","100.00","X"

iShares Core U.S. Aggregate Bond ETF
Fund Holdings as of,"Sep 24, 2026"
Name,Sector,Asset Class,Market Value,Weight (%)
"TREASURY BOND","Treasuries","Fixed Income","1.00","99.00"
"""


def test_parse_ishares_holdings_sums_agency_mbs_sectors_and_filters_cmbs():
    asof_date, weight = parse_ishares_holdings(ISHARES_FIXTURE)
    assert asof_date == date(2026, 9, 24)
    # MBS Pass-Through (20.00) + agency-named CMBS row FHMS (0.50).
    # Excludes: Treasury, the private-label WFCM CMBS row, the "Agency"
    # sector debenture, and cash — and never reads the second block.
    assert weight == Decimal("20.50")


def _ssga_sheet(rows: list[list]) -> pd.DataFrame:
    header = ["Name", "Identifier", "SEDOL", "Weight", "Coupon", "Par Value", "Market Value", "Local Currency", "Maturity"]
    body = [
        ["Fund Name:", "SPDR Portfolio Mortgage Backed Bond ETF"] + [NA] * 7,
        ["Ticker Symbol:", "SPMB"] + [NA] * 7,
        ["Holdings:", "As of 24-Sep-2026"] + [NA] * 7,
        [NA] * 9,
        header,
        *rows,
    ]
    return pd.DataFrame(body)


def test_parse_ssga_holdings_sums_name_matches_and_excludes_agency_debt():
    df = _ssga_sheet(
        [
            ["FNMA TBA 30 YR 5.5 SINGLE FAMILY MORTGAGE", "X", "-", 1.5, 5.5, 100, 100, "USD", "10/13/2056"],
            ["FED HM LN PC POOL RQ0094 FR 02/56 FIXED 5", "X", "-", 2.0, 5, 100, 100, "USD", "02/01/2056"],
            ["FREDDIE MAC NOTES 07/32 6.25", "X", "-", 0.4, 6.25, 100, 100, "USD", "07/01/2032"],
            ["SSI US GOV MONEY MARKET CLASS", "X", "-", 3.0, 3.8, 100, 100, "USD", "12/31/2030"],
        ]
    )

    asof_date, weight = parse_ssga_holdings(df)

    assert asof_date == date(2026, 9, 24)
    assert weight == Decimal("3.5")  # 1.5 + 2.0, excludes the NOTES row and the cash sleeve


def test_parse_ssga_holdings_missing_as_of_raises():
    df = _ssga_sheet([])
    df.iat[2, 1] = "not a date"
    with pytest.raises(ValueError, match="as-of date"):
        parse_ssga_holdings(df)
