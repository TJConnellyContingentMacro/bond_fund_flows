"""Issuer-published daily history (NAV and shares outstanding), used to backfill
fund_daily and to refill gaps — SPEC.md §8's "re-fetch where the issuer offers
history". Only iShares and SSGA publish this; see CLAUDE.md for the others.

- iShares: the product page's "Data Download" workbook (SpreadsheetML, not
  well-formed XML, so it's scanned with regexes) has a "Historical" sheet back
  to inception: As Of, NAV per Share, Ex-Dividends, Shares Outstanding. It
  matched the live adapter to the last digit on every date checked. No TNA
  column; iShares' published TNA equals shares x NAV exactly, so that's used.
- SSGA: navhist-us-en-<ticker>.xlsx has Date, NAV, Shares Outstanding, Total
  Net Assets. Its share counts are exact, where the live page rounds to 10,000.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pandas as pd
import requests

USER_AGENT = "Mozilla/5.0 (compatible; bondflows/0.1; contact:tj@contingentmacro.com)"
ISHARES_DOWNLOAD_URL = (
    "https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document"
    "?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US"
    "&portfolioId={portfolio_id}&component=fundDownload&userType=individual"
)
SSGA_NAVHIST_URL = (
    "https://www.ssga.com/us/en/individual/library-content/products/fund-data/etfs/us/"
    "navhist-us-en-{ticker_lower}.xlsx"
)
CENT = Decimal("0.01")

_ROW_RE = re.compile(r"<ss:Row[^>]*>(.*?)</ss:Row>", re.S)
_CELL_RE = re.compile(r"<ss:Data[^>]*>(.*?)</ss:Data>", re.S)


@dataclass(frozen=True)
class HistoryRow:
    asof_date: date
    nav_per_share: Decimal
    shares_outstanding: int | None
    total_net_assets: Decimal | None


def ishares_historical_sheet(workbook_text: str) -> str:
    """The verbatim "Historical" worksheet section — what gets kept as the raw
    snapshot, since the full workbook is ~28 MB of mostly holdings."""
    start = workbook_text.find('<ss:Worksheet ss:Name="Historical"')
    if start == -1:
        raise ValueError("no 'Historical' worksheet in the iShares download")
    end = workbook_text.find("</ss:Worksheet>", start)
    if end == -1:
        raise ValueError("'Historical' worksheet is truncated")
    return workbook_text[start : end + len("</ss:Worksheet>")]


def parse_ishares_historical(sheet: str) -> list[HistoryRow]:
    rows = [_CELL_RE.findall(r) for r in _ROW_RE.findall(sheet)]
    if not rows or rows[0][:4] != ["As Of", "NAV per Share", "Ex-Dividends", "Shares Outstanding"]:
        raise ValueError(f"unexpected Historical header: {rows[0] if rows else None!r}")
    out = []
    for cells in rows[1:]:
        if len(cells) < 4:
            continue
        asof = _parse_date(cells[0], "%b %d, %Y")
        nav = _parse_decimal(cells[1])
        if asof is None or nav is None:
            continue
        shares = _parse_int(cells[3])
        tna = (Decimal(shares) * nav).quantize(CENT) if shares is not None else None
        out.append(HistoryRow(asof, nav, shares, tna))
    return out


def parse_ssga_navhist(df: pd.DataFrame) -> list[HistoryRow]:
    header_idx = next((i for i in range(len(df)) if str(df.iat[i, 0]).strip() == "Date"), None)
    if header_idx is None:
        raise ValueError("no 'Date' header row in the SSGA NAV history")
    header = [str(v).strip() for v in df.iloc[header_idx].tolist()]
    try:
        nav_col, shares_col, tna_col = (header.index(h) for h in ("NAV", "Shares Outstanding", "Total Net Assets"))
    except ValueError as exc:
        raise ValueError(f"unexpected SSGA NAV history header: {header!r}") from exc
    out = []
    for i in range(header_idx + 1, len(df)):
        asof = _parse_date(str(df.iat[i, 0]).strip(), "%d-%b-%Y")
        nav = _parse_decimal(df.iat[i, nav_col])
        if asof is None or nav is None:
            continue
        shares = _parse_int(df.iat[i, shares_col])
        tna = _parse_decimal(df.iat[i, tna_col])
        out.append(HistoryRow(asof, nav, shares, tna.quantize(CENT) if tna is not None else None))
    return out


def fetch_ishares_history(portfolio_id: str, session: requests.Session) -> tuple[str, list[HistoryRow]]:
    resp = session.get(ISHARES_DOWNLOAD_URL.format(portfolio_id=portfolio_id), timeout=120)
    resp.raise_for_status()
    sheet = ishares_historical_sheet(resp.content.decode("utf-8", errors="replace"))
    return sheet, parse_ishares_historical(sheet)


def fetch_ssga_history(ticker: str, session: requests.Session) -> tuple[bytes, list[HistoryRow]]:
    resp = session.get(SSGA_NAVHIST_URL.format(ticker_lower=ticker.lower()), timeout=60)
    resp.raise_for_status()
    df = pd.read_excel(io.BytesIO(resp.content), header=None)
    return resp.content, parse_ssga_navhist(df)


def _parse_date(text: str, fmt: str) -> date | None:
    try:
        return datetime.strptime(text, fmt).date()
    except ValueError:
        return None


def _parse_decimal(value) -> Decimal | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).replace(",", "").strip()
    if text in ("", "--", "-", "nan"):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_int(value) -> int | None:
    d = _parse_decimal(value)
    return int(d.to_integral_value()) if d is not None else None
