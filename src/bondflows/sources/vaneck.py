"""VanEck adapter. See notes/vaneck.md for the full discovery writeup.

Uses a headless browser because the page's tab content (the "Portfolio"
section holding Effective Duration and Spread Duration) only mounts into
the DOM once that tab is clicked — a plain HTTP GET's raw HTML never
contains it, confirmed on a fresh fetch, correcting the original
discovery-pass assumption that this page had no client-side rendering step
at all. No Akamai-style headless-blocking here (unlike Schwab) — a
standard headless Chromium works fine once the navigation quirk below is
handled.

The cookie-consent redirect loop found during discovery (curl needed a
cookie jar) shows up in Playwright as `page.goto(..., wait_until="load")`
timing out — the page's "load" event apparently never fires while that
redirect dance is happening in the background. Using
`wait_until="domcontentloaded"` sidesteps it entirely; no cookie handling
needed.

**Correction to the original discovery notes**: VanEck *does* publish a
real Spread Duration figure (found once the Portfolio tab's full field
list was actually visible, not just the fields captured in a screenshot
during discovery) — update the "no spread duration" characterization
there.

No shares-outstanding field found anywhere on the page (confirmed again on
this pass) — `shares_outstanding` is derived (`total_net_assets ÷
nav_per_share`), same fallback convention as SSGA/JPMorgan.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from bondflows.sources.base import BrowserSourceAdapter, FetchResult, FundObservation, RawArtifact

_ASOF_RE = re.compile(r"as of ([A-Za-z]+ \d{1,2}, \d{4})")
_NUMBER_SUFFIX_RE = re.compile(r"([\d,.\-]+)\s*([BMK%]?)")
_SLASH_DATE_RE = re.compile(r"(\d{2}/\d{2}/\d{4})")


class VanEckAdapter(BrowserSourceAdapter):
    name = "VanEck"

    def fetch_one(self, ticker: str, identifier: str | None) -> FetchResult:
        if not identifier:
            return FetchResult(ticker=ticker, error="no product_url in universe.csv for this ticker")
        url = identifier

        page = self._context.new_page()
        try:
            try:
                page.goto(url, timeout=20000, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001 - the cookie-consent redirect loop; retry once
                page.goto(url, timeout=20000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)

            overview_text = page.inner_text("body")
            raw = RawArtifact(
                ticker=ticker, content=overview_text.encode("utf-8"), content_type="html", source_url=url
            )

            nav_value, asof_str = _extract_stat(overview_text, "NAV")
            asof_date = _parse_long_date(asof_str)
            nav_per_share = _parse_decimal(nav_value)
            if nav_per_share is None or asof_date is None:
                return FetchResult(ticker=ticker, raw=raw, error="could not locate NAV/as-of-date on page")

            tna_value, _ = _extract_stat(overview_text, "TOTAL NET ASSETS")
            total_net_assets = _parse_scaled_decimal(tna_value)
            shares_outstanding = (
                round(total_net_assets / nav_per_share) if total_net_assets is not None else None
            )

            effective_duration = None
            spread_duration = None
            try:
                page.get_by_text("Portfolio", exact=True).first.click(timeout=5000)
                page.wait_for_timeout(1200)
                portfolio_text = page.inner_text("body")
                effective_duration = _simple_value(portfolio_text, "Effective Duration (yrs)")
                spread_duration = _simple_value(portfolio_text, "Spread Duration (yrs)")
            except Exception:  # noqa: BLE001 - Portfolio tab shape may vary; NAV data still valid
                pass

            observation = FundObservation(
                ticker=ticker,
                asof_date=asof_date,
                shares_outstanding=int(shares_outstanding) if shares_outstanding is not None else None,
                nav_per_share=nav_per_share,
                total_net_assets=total_net_assets,
                market_close=None,
                effective_duration=effective_duration,
                spread_duration=spread_duration,
            )
            return FetchResult(ticker=ticker, raw=raw, observation=observation)
        finally:
            page.close()


def _extract_stat(text: str, label: str) -> tuple[str | None, str | None]:
    idx = text.find(label)
    if idx < 0:
        return None, None
    window = text[idx + len(label) : idx + len(label) + 150]
    num_m = _NUMBER_SUFFIX_RE.search(window)
    asof_m = _ASOF_RE.search(window)
    value = (num_m.group(1) + num_m.group(2)) if num_m else None
    return value, (asof_m.group(1) if asof_m else None)


def _simple_value(text: str, label: str) -> Decimal | None:
    m = re.search(re.escape(label) + r"\s*\n([\d.\-]+)", text)
    if not m:
        return None
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return None


def _parse_decimal(text: str | None) -> Decimal | None:
    if not text:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _parse_scaled_decimal(text: str | None) -> Decimal | None:
    """Parses e.g. '3.17B' -> 3,170,000,000."""
    if not text:
        return None
    m = re.match(r"([\d,.\-]+)([BMK]?)$", text)
    if not m:
        return None
    try:
        value = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    # Plain-int scale factors, not Decimal("1e9") — multiplying by an
    # exponential-notation Decimal produces a result whose string form is
    # itself exponential ("3.17E+9"), which DuckDB's parameter binding
    # silently mis-parses as 317.00. See CLAUDE.md.
    scale = {"B": 1_000_000_000, "M": 1_000_000, "K": 1_000, "": 1}[m.group(2)]
    return value * scale


def _parse_long_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, "%B %d, %Y").date()
    except ValueError:
        return None
