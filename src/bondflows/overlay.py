"""Double-counting control for option-overlay funds, SPEC.md §6.2 (build
order step 6) — narrowly scoped to the 3 cases SPEC.md names explicitly:
"Option-overlay funds hold the underlying ETF — TLTW holds TLT, HYGW holds
HYG, LQDW holds LQD."

**Scope decision, made with T.J.**: SPEC.md §6.2 also names "some active
multisector funds [that] hold ETF positions opportunistically" as a
double-counting source. Checked BOND, TOTL, PYLD, JCPB directly: BOND has
2,044 holdings and TOTL has 1,710 — large, granular active portfolios, not
something a spot-check can rule an ETF holding in or out of on any given
day. Detecting one would need full holdings-file parsing and name/CUSIP
matching across 3-4 differently-shaped issuer formats, for a payoff SPEC.md
itself hedges as "opportunistic," not certain. Deliberately not built —
`total_flow_usd_net` only nets out the 3 overlay tickers below. See
CLAUDE.md for this decision.

Each of TLTW/HYGW/LQDW's iShares holdings CSV (confirmed live 2026-09-25)
puts its single dominant holding — the underlying ETF — as the first data
row, weighted ~100% (~99.7-100.4% observed; the rest is a small cash sleeve
and, for TLTW, a short call option position). `MIN_PLAUSIBLE_WEIGHT` guards
against silently misreading a wrong row if iShares ever changes this CSV's
row order.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import requests

USER_AGENT = "Mozilla/5.0 (compatible; bondflows/0.1; contact:tj@contingentmacro.com)"

# ticker -> (underlying ticker, a substring expected in the underlying
# holding's Name field, for a sanity check independent of row position).
OVERLAY_FUNDS: dict[str, tuple[str, str]] = {
    "TLTW": ("TLT", "TREASURY BOND"),
    "HYGW": ("HYG", "HIGH YIELD"),
    "LQDW": ("LQD", "INV GRADE"),
}

MIN_PLAUSIBLE_WEIGHT = Decimal("50")  # percent — this should always be ~100%


@dataclass
class OverlayHoldingResult:
    ticker: str
    asof_date: date | None = None
    underlying_ticker: str | None = None
    weight_pct: Decimal | None = None
    raw_content: bytes | None = None
    error: str | None = None


def fetch_overlay_holding(ticker: str, product_url: str, *, session: requests.Session | None = None) -> OverlayHoldingResult:
    if ticker not in OVERLAY_FUNDS:
        return OverlayHoldingResult(ticker=ticker, error=f"{ticker} isn't in OVERLAY_FUNDS")
    underlying_ticker, name_hint = OVERLAY_FUNDS[ticker]

    session = session or requests.Session()
    url = product_url.rstrip("/") + "/latest-holdings.csv"
    resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    content = resp.content

    try:
        asof_date, name, weight_pct = _parse_holdings_csv(content.decode("utf-8"))
    except ValueError as exc:
        return OverlayHoldingResult(ticker=ticker, raw_content=content, error=str(exc))

    if name_hint not in name.upper():
        return OverlayHoldingResult(
            ticker=ticker,
            raw_content=content,
            error=f"top holding {name!r} doesn't look like {underlying_ticker} — CSV shape may have changed",
        )
    if weight_pct < MIN_PLAUSIBLE_WEIGHT:
        return OverlayHoldingResult(
            ticker=ticker,
            raw_content=content,
            error=f"top holding weight {weight_pct}% is below the {MIN_PLAUSIBLE_WEIGHT}% sanity floor",
        )

    return OverlayHoldingResult(
        ticker=ticker,
        asof_date=asof_date,
        underlying_ticker=underlying_ticker,
        weight_pct=weight_pct,
        raw_content=content,
    )


def _parse_holdings_csv(text: str) -> tuple[date, str, Decimal]:
    """Returns (asof_date, top_holding_name, top_holding_weight_pct). The
    file is two concatenated fund-holdings blocks (the overlay fund's own
    holdings, then the underlying ETF's own look-through holdings) — only
    the first block's first data row is the underlying ETF holding itself."""
    rows = list(csv.reader(io.StringIO(text)))

    asof_date = None
    header_idx = None
    for i, row in enumerate(rows):
        if not asof_date and row and row[0] == "Fund Holdings as of" and len(row) > 1:
            asof_date = _parse_month_day_year(row[1])
        if header_idx is None and row and row[0] == "Name":
            header_idx = i
            break

    if asof_date is None:
        raise ValueError("could not find 'Fund Holdings as of' row")
    if header_idx is None or header_idx + 1 >= len(rows):
        raise ValueError("could not find a holdings header row followed by a data row")

    header = rows[header_idx]
    data_row = rows[header_idx + 1]
    try:
        weight_idx = header.index("Weight (%)")
    except ValueError as exc:
        raise ValueError("holdings header has no 'Weight (%)' column") from exc

    name = data_row[0]
    try:
        weight_pct = Decimal(data_row[weight_idx].replace(",", ""))
    except (InvalidOperation, IndexError) as exc:
        raise ValueError(f"could not parse weight from {data_row!r}") from exc

    return asof_date, name, weight_pct


def _parse_month_day_year(text: str) -> date | None:
    try:
        return datetime.strptime(text, "%b %d, %Y").date()
    except ValueError:
        return None
