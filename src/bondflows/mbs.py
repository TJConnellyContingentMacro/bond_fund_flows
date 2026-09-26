"""Agency MBS look-through, SPEC.md §6.6 (build order step 8) — narrowly
scoped, per discussion with T.J., to the 6 tickers where a genuine full
daily holdings file with real market values is confirmed available:
iShares (MBB, GNMA, AGG, IUSB) and SSGA (SPMB, SPAB).

**Scope decision**: checked all 16 tickers SPEC.md §6.6 names (the
agency_mbs and aggregate sleeves, plus BINC/PYLD/BOND/FBND/JCPB). Real
blockers found for the rest, none of them guessed around:
- Vanguard (VMBS, BND): its holdings endpoint has no market-value field at
  all (flagged in notes/vanguard.md during original discovery) — would need
  a separate bottom-up pricing project, not something this step can absorb.
- PIMCO (BOND, PYLD), JPMorgan (JCPB), Schwab (SCHZ — only ~101 reported
  holdings, likely sampled rather than full pool-level): holdings-file shape
  was never checked.
- Fidelity (FBND), Simplify (MTBA): entirely new issuers, never discovered.
- Janus Henderson (JMBS): already a confirmed dead end for even basic
  NAV/shares data.
- BINC: not even resolved in universe.csv — its `issuer` field is
  "iShares/BlackRock", which doesn't match the adapter registry's "iShares"
  key, so it's silently getting zero coverage today for basic flow data
  too — a pre-existing gap unrelated to holdings.
`mbs_weight` is simply absent for all of the above rather than guessed.

Because only 6 tickers across 2 issuers are in scope, and none of them are
the "active multisector" funds SPEC.md's flow/allocation decomposition
(§6.6's `Δ implied_mbs ≈ flow*weight(t-1) + aum(t-1)*Δweight`) is really
about — that decomposition targets *discretionary* managers changing their
MBS view, and MBB/GNMA/AGG/IUSB/SPMB/SPAB are all index funds — this module
only computes the basic `implied_mbs_flow`, not the decomposition. Building
the decomposition would need the active-fund holdings this scope excludes.

**Classification differs by what each issuer's file actually gives you**,
verified against real holdings 2026-09-26:
- **iShares** CSVs have a `Sector` column. `MBS Pass-Through` (AGG),
  `Agency Fixed Rate`, and `Hybrid Arms` (both MBB) are unambiguously agency
  MBS in every fund checked. `CMBS` is a genuinely mixed bucket — AGG's own
  CMBS rows include both agency paper (FHLMC/FHMS/FREMF-prefixed, e.g.
  Freddie Mac Multifamily K-series) and private-label conduits (BANK/WFCM/
  JPMCC/CGCMT/GSMS/MSC/etc.) — so CMBS rows get the same name-based filter
  SSGA uses below. `Agency` (general GSE debt, e.g. "FHLMC REFERENCE NOTE")
  is excluded entirely: that's a debenture, not a mortgage pool.
- **SSGA** XLSX holdings files have no Sector column at all — classification
  is by name pattern only. Confirmed a real trap in SPAB's own holdings:
  "FREDDIE MAC NOTES 07/32 6.25" matches the FREDDIE MAC issuer prefix but
  is agency *debt*, not a mortgage pool — excluded via the same
  NOTES/DEBENTURE filter applied to iShares' CMBS rows.

Weights are computed from the *prior* day (§6.6's explicit instruction, to
avoid look-ahead) — same treatment overlay.py gives the analogous §6.2 case.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import duckdb
import pandas as pd
import requests

from bondflows.db import upsert_mbs_implied_flow

USER_AGENT = "Mozilla/5.0 (compatible; bondflows/0.1; contact:tj@contingentmacro.com)"

ISHARES_MBS_TICKERS = frozenset({"MBB", "GNMA", "AGG", "IUSB"})
SSGA_MBS_TICKERS = frozenset({"SPMB", "SPAB"})
SSGA_HOLDINGS_URL = (
    "https://www.ssga.com/us/en/individual/library-content/products/fund-data/etfs/us/"
    "holdings-daily-us-en-{ticker_lower}.xlsx"
)

# iShares Sector values that are unambiguously agency MBS in every fund
# checked (MBB, AGG) — anything else needs the name-based filter below.
_ISHARES_AGENCY_SECTORS = frozenset({"MBS Pass-Through", "Agency Fixed Rate", "Hybrid Arms"})
_ISHARES_MIXED_SECTOR = "CMBS"

# Name substrings that positively identify an agency MBS pool/CMO/CMBS
# tranche, and substrings that mean "this is agency debt, not a mortgage
# pool" and must veto a positive match even if an issuer prefix is present
# (e.g. "FREDDIE MAC NOTES", "FHLMC REFERENCE NOTE").
_AGENCY_MBS_NAME_PATTERNS = ("FNMA", "FHLMC", "FED HM LN", "FREDDIE MAC", "GNMA", "GINNIE MAE", "FHMS", "FREMF")
_AGENCY_DEBT_EXCLUSION_PATTERNS = ("NOTE", "DEBENTURE")


def is_agency_mbs_by_name(name: str) -> bool:
    upper = name.upper()
    if any(term in upper for term in _AGENCY_DEBT_EXCLUSION_PATTERNS):
        return False
    return any(pattern in upper for pattern in _AGENCY_MBS_NAME_PATTERNS)


@dataclass
class MBSWeightResult:
    ticker: str
    asof_date: date | None = None
    mbs_weight_pct: Decimal | None = None
    raw_content: bytes | None = None
    error: str | None = None


def fetch_ishares_mbs_weight(ticker: str, product_url: str, *, session: requests.Session | None = None) -> MBSWeightResult:
    session = session or requests.Session()
    url = product_url.rstrip("/") + "/latest-holdings.csv"
    resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    resp.raise_for_status()
    content = resp.content

    try:
        asof_date, weight_pct = parse_ishares_holdings(content.decode("utf-8"))
    except ValueError as exc:
        return MBSWeightResult(ticker=ticker, raw_content=content, error=str(exc))

    return MBSWeightResult(ticker=ticker, asof_date=asof_date, mbs_weight_pct=weight_pct, raw_content=content)


def fetch_ssga_mbs_weight(ticker: str, *, session: requests.Session | None = None) -> MBSWeightResult:
    session = session or requests.Session()
    url = SSGA_HOLDINGS_URL.format(ticker_lower=ticker.lower())
    resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    resp.raise_for_status()
    content = resp.content

    try:
        df = pd.read_excel(io.BytesIO(content), sheet_name="holdings", header=None)
        asof_date, weight_pct = parse_ssga_holdings(df)
    except (ValueError, KeyError) as exc:
        return MBSWeightResult(ticker=ticker, raw_content=content, error=str(exc))

    return MBSWeightResult(ticker=ticker, asof_date=asof_date, mbs_weight_pct=weight_pct, raw_content=content)


def parse_ishares_holdings(csv_text: str) -> tuple[date, Decimal]:
    """Returns (asof_date, mbs_weight_pct) — the sum of Weight (%) across
    rows classified as agency MBS. Only the fund's own holdings block (the
    first one in the file — see overlay.py's docstring for the same
    two-concatenated-blocks quirk) is read."""
    rows = list(csv.reader(io.StringIO(csv_text)))

    asof_date = None
    header_idx = None
    for i, row in enumerate(rows):
        if asof_date is None and row and row[0] == "Fund Holdings as of" and len(row) > 1:
            asof_date = _parse_month_day_year(row[1])
        if header_idx is None and row and row[0] == "Name":
            header_idx = i
            break

    if asof_date is None:
        raise ValueError("could not find 'Fund Holdings as of' row")
    if header_idx is None:
        raise ValueError("could not find a holdings header row")

    header = rows[header_idx]
    try:
        sector_idx = header.index("Sector")
        weight_idx = header.index("Weight (%)")
    except ValueError as exc:
        raise ValueError("holdings header is missing 'Sector' or 'Weight (%)'") from exc

    total_weight = Decimal("0")
    for row in rows[header_idx + 1 :]:
        if not row or row[0] == "":
            break
        if len(row) <= max(sector_idx, weight_idx):
            continue
        sector = row[sector_idx]
        try:
            weight = Decimal(row[weight_idx].replace(",", ""))
        except InvalidOperation:
            continue

        if sector in _ISHARES_AGENCY_SECTORS:
            total_weight += weight
        elif sector == _ISHARES_MIXED_SECTOR and is_agency_mbs_by_name(row[0]):
            total_weight += weight

    return asof_date, total_weight


def parse_ssga_holdings(df: pd.DataFrame) -> tuple[date, Decimal]:
    """Returns (asof_date, mbs_weight_pct) from an already-loaded 'holdings'
    sheet. Kept separate from the network fetch + pd.read_excel call so
    tests can build a small DataFrame fixture directly."""
    asof_cell = str(df.iat[2, 1])
    asof_date = _parse_ssga_asof(asof_cell)
    if asof_date is None:
        raise ValueError(f"could not parse an as-of date from {asof_cell!r}")

    header = [str(v) for v in df.iloc[4].tolist()]
    try:
        name_idx = header.index("Name")
        weight_idx = header.index("Weight")
    except ValueError as exc:
        raise ValueError("holdings header is missing 'Name' or 'Weight'") from exc

    total_weight = Decimal("0")
    for i in range(5, len(df)):
        name = df.iat[i, name_idx]
        if pd.isna(name):
            continue
        weight_raw = df.iat[i, weight_idx]
        if pd.isna(weight_raw):
            continue
        if is_agency_mbs_by_name(str(name)):
            total_weight += Decimal(str(weight_raw))

    return asof_date, total_weight


def _parse_month_day_year(text: str) -> date | None:
    try:
        return datetime.strptime(text, "%b %d, %Y").date()
    except ValueError:
        return None


def _parse_ssga_asof(text: str) -> date | None:
    # e.g. "As of 24-Sep-2026"
    prefix = "As of "
    if not text.startswith(prefix):
        return None
    try:
        return datetime.strptime(text[len(prefix) :].strip(), "%d-%b-%Y").date()
    except ValueError:
        return None


def compute_mbs_implied_flows(con: duckdb.DuckDBPyConnection) -> int:
    """flow_usd(fund,t) * mbs_weight(fund,t-1)/100 (SPEC.md §6.6) for every
    ticker with mbs_weights data. Safe to rerun in full."""
    rows = con.execute(
        """
        WITH mbs_lag AS (
            SELECT ticker, asof_date,
                   LAG(mbs_weight_pct) OVER (PARTITION BY ticker ORDER BY asof_date) AS prior_weight_pct
            FROM mbs_weights
        )
        SELECT f.ticker, f.flow_date, f.flow_usd * m.prior_weight_pct / 100 AS implied_mbs_flow_usd
        FROM fund_flows f
        JOIN mbs_lag m ON m.ticker = f.ticker AND m.asof_date = f.flow_date
        WHERE m.prior_weight_pct IS NOT NULL
        """
    ).fetchall()

    for ticker, flow_date, implied in rows:
        upsert_mbs_implied_flow(con, {"ticker": ticker, "flow_date": flow_date, "implied_mbs_flow_usd": implied})
    return len(rows)
