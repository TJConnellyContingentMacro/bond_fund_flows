"""ICI weekly overlay, SPEC.md §7 — a single market-wide weekly release, not
a per-ticker adapter, so this deliberately doesn't implement `SourceAdapter`
(whose contract is one `FundObservation` per ticker per day).

Verified live 2026-09-26: the release page
(ici.org/research/statistics/etfs/weekly-combined-estimated-etf-and-longterm-flows)
links to `ici.org/combined_flows_data_<year>.xls` — a legacy binary `.xls`
(OLE2/BIFF, not `.xlsx`), needing the `xlrd` package rather than openpyxl.
The sheet ("Weekly MF & ETF Public Report") has a **monthly** section
followed by an **"Estimated weekly fund flows"** section; only the weekly
section is parsed here — the monthly section isn't part of SPEC.md §7's ask,
though it's still preserved in the raw `.xls` snapshot for rebuildability.

Two properties from SPEC.md §7 that matter wherever this data is *used*,
not just here:
- It's an estimate — ICI's own label — not an actual.
- The lag is real (6-12 calendar days). Never align an ICI week to a
  current ETF day without showing that lag explicitly.

Stored in its own `ici_weekly_flows` table (db.py), never blended into the
daily `fund_flows` series.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime, timezone

import pandas as pd
import requests

BASE_URL = "https://www.ici.org"
USER_AGENT = "Mozilla/5.0 (compatible; bondflows/0.1; contact:tj@contingentmacro.com)"
SHEET_NAME = "Weekly MF & ETF Public Report"
WEEKLY_SECTION_LABEL = "Estimated weekly fund flows"

# Column indices in the raw sheet (header=None) for the weekly section,
# confirmed against a live download 2026-09-26. Odd indices in between are
# blank spacer columns in ICI's own layout, not gaps in this mapping.
_COLUMNS = {
    "total_ltf_and_etf": 1,
    "equity_total": 3,
    "equity_domestic": 5,
    "equity_world": 7,
    "hybrid": 9,
    "bond_total": 11,
    "bond_taxable": 13,
    "bond_municipal": 15,
    "commodity": 17,
}
WEEK_ENDED_COLUMN = 0
# The source file's own units ("Millions, U.S. dollars"); scaled by a plain
# int per CLAUDE.md's Decimal-scaling rule, for consistency with the rest of
# the schema, which stores raw USD.
MILLIONS_TO_USD = 1_000_000


@dataclass
class ICIFetchResult:
    rows: list[dict]
    raw_content: bytes | None = None
    source_file: str | None = None
    error: str | None = None


def fetch_ici_weekly(year: int | None = None, *, session: requests.Session | None = None) -> ICIFetchResult:
    year = year or datetime.now(timezone.utc).year
    filename = f"combined_flows_data_{year}.xls"
    url = f"{BASE_URL}/{filename}"

    session = session or requests.Session()
    resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    content = resp.content

    try:
        df = pd.read_excel(io.BytesIO(content), sheet_name=SHEET_NAME, header=None)
        rows = extract_weekly_rows(df)
    except ValueError as exc:
        return ICIFetchResult(rows=[], raw_content=content, source_file=filename, error=str(exc))

    return ICIFetchResult(rows=rows, raw_content=content, source_file=filename)


def extract_weekly_rows(df: pd.DataFrame) -> list[dict]:
    """Pure function over an already-loaded sheet — kept separate from the
    network fetch + `pd.read_excel` call so tests can build a small
    DataFrame fixture directly instead of a real (legacy binary) .xls file."""
    label_col = df[0].astype(str).str.strip()
    section_rows = label_col[label_col == WEEKLY_SECTION_LABEL].index
    if len(section_rows) == 0:
        raise ValueError(f"could not find the {WEEKLY_SECTION_LABEL!r} section header")
    start = section_rows[0] + 1

    rows: list[dict] = []
    for i in range(start, len(df)):
        week_ended = _parse_date(df.iat[i, WEEK_ENDED_COLUMN])
        if week_ended is None:
            break  # blank row / trailing footnote marks the end of the section

        row: dict = {"week_ended": week_ended}
        for name, col in _COLUMNS.items():
            row[name] = round(float(df.iat[i, col]) * MILLIONS_TO_USD)
        rows.append(row)

    if not rows:
        raise ValueError(f"found the {WEEKLY_SECTION_LABEL!r} header but no data rows after it")
    return rows


def _parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%m/%d/%Y").date()
        except ValueError:
            return None
    return None
