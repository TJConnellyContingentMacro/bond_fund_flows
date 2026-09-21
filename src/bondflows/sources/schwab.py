"""Schwab adapter. See notes/schwab.md for the full discovery writeup.

Ticker is the URL slug (`schwabassetmanagement.com/products/<ticker>`), so
there's no identifier to resolve — every ticker just works.

This site requires a real, visibly-rendered browser session rather than a
background/headless one — a standard headless browser gets an access error
on this domain regardless of request headers, while a normal, unmodified
Chrome window loads the page correctly with no other changes needed. T.J.
reviewed this and asked for the adapter to open Chrome normally (not
headless) to match. See `headless = False` / `channel = "chrome"` below.

Operational consequence: the daily cron must run in a session with desktop
access (a real interactive Windows session), not a bare background service.

Everything (NAV, Shares Outstanding, Total Net Assets, Effective Duration)
is already present in the initial page load's rendered DOM — no accordion
to expand, just a one-time "select a role" interstitial to click through
per browser session (not per ticker).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from bondflows.sources.base import BrowserSourceAdapter, FetchResult, FundObservation, RawArtifact

BASE_URL = "https://www.schwabassetmanagement.com/products/{ticker}"
_ROLE_LINK_PATTERN = re.compile("Personal investor")

_FIELD_PATTERN = r"{label}(?:\n|\t)(?:As of (\d{{2}}/\d{{2}}/\d{{4}})\n\t)?([^\n\t]+)"


class SchwabAdapter(BrowserSourceAdapter):
    name = "Schwab"
    headless = False
    channel = "chrome"

    def __init__(self) -> None:
        super().__init__()
        self._role_selected = False

    def fetch_one(self, ticker: str, identifier: str | None) -> FetchResult:
        page = self._context.new_page()
        try:
            url = BASE_URL.format(ticker=ticker.lower())
            page.goto(url, timeout=30000)
            page.wait_for_timeout(1500)

            if not self._role_selected:
                try:
                    page.get_by_role("link", name=_ROLE_LINK_PATTERN).click(timeout=5000)
                    page.wait_for_timeout(1500)
                except Exception:  # noqa: BLE001 - modal shape may vary; proceed either way
                    pass
                self._role_selected = True

            text = page.inner_text("body")
            raw = RawArtifact(ticker=ticker, content=text.encode("utf-8"), content_type="html", source_url=url)

            nav_value, asof_str = _extract_field(text, "NAV")
            asof_date = _parse_date(asof_str)
            nav_per_share = _parse_money(nav_value)
            if nav_per_share is None or asof_date is None:
                return FetchResult(ticker=ticker, raw=raw, error="could not locate NAV/as-of-date on page")

            shares_value, _ = _extract_field(text, "Shares Outstanding")
            tna_value, _ = _extract_field(text, "Total Net Assets")
            duration_value, _ = _extract_field(text, "Effective Duration")

            observation = FundObservation(
                ticker=ticker,
                asof_date=asof_date,
                shares_outstanding=_parse_int(shares_value),
                nav_per_share=nav_per_share,
                total_net_assets=_parse_money(tna_value),
                market_close=None,
                effective_duration=_parse_years(duration_value),
                spread_duration=None,  # confirmed not published for this issuer
            )
            return FetchResult(ticker=ticker, raw=raw, observation=observation)
        finally:
            page.close()


def _extract_field(text: str, label: str) -> tuple[str | None, str | None]:
    m = re.search(_FIELD_PATTERN.format(label=re.escape(label)), text)
    if not m:
        return None, None
    return m.group(2).strip(), m.group(1)


def _parse_money(text: str | None) -> Decimal | None:
    if not text:
        return None
    cleaned = text.replace("$", "").replace(",", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_int(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return int(text.replace(",", "").strip())
    except ValueError:
        return None


def _parse_years(text: str | None) -> Decimal | None:
    if not text:
        return None
    try:
        return Decimal(text.replace("years", "").replace("yrs", "").strip())
    except InvalidOperation:
        return None


def _parse_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError:
        return None
