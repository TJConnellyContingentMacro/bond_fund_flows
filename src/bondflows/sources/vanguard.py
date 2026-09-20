"""Vanguard adapter. See notes/vanguard.md for the full discovery writeup.

Two sites, very different data:
  - investor.vanguard.com (retail) — used only to resolve each ticker's
    internal `fundId` (embedded in the page's `data-vgn-funds-profile` JSON
    blob at `dashboard.associatedFundIds.etfFundId`), cached into
    universe.csv's `issuer_id` column so this only has to happen once.
  - advisors.vanguard.com — the real data source. Unauthenticated JSON REST
    API keyed by fundId: daily NAV (full precision), a shares-outstanding
    figure that only updates monthly (confirmed structural — Vanguard's
    ETFs are a share class of the underlying mutual fund, not a scraping
    gap), and daily effective duration.

`total_net_assets` is never populated for Vanguard — confirmed not
published anywhere, at any cadence, during discovery. Not a bug to fix
later; see CLAUDE.md.

Shares outstanding cadence: Vanguard's own figure only moves roughly once a
month. Reporting the *same* value every day between prints would make
flows.py compute a fake `shares_delta = 0` for weeks at a time — a zero
standing in for "we don't actually know," which is exactly what CLAUDE.md's
no-silent-zeros rule forbids. So this adapter reports `shares_outstanding`
only on the day its `effectiveDate` actually advances to something new
(tracked in a small local state file, `data/.state/vanguard_shares_seen.json`)
and `None` every other day.
"""

from __future__ import annotations

import html as htmlmod
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import requests

from bondflows.sources.base import STATE_DIR, FetchResult, FundObservation, RawArtifact, SourceAdapter
from bondflows.universe import load_universe

RETAIL_URL = "https://investor.vanguard.com/investment-products/etfs/profile/{ticker}"
ADVISOR_BASE = "https://advisors.vanguard.com/investments/products/api/funds/{fund_id}"
USER_AGENT = "Mozilla/5.0 (compatible; bondflows/0.1; contact:tj@contingentmacro.com)"
SHARES_SEEN_PATH = STATE_DIR / "vanguard_shares_seen.json"

_PROFILE_ATTR_MARKER = 'data-vgn-funds-profile="'


class VanguardAdapter(SourceAdapter):
    name = "Vanguard"

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._fund_id_cache: dict[str, str] = {}
        self._sleeve_by_ticker = {
            f.ticker: f.sleeve for f in load_universe() if f.issuer == "Vanguard"
        }
        self._shares_seen = _load_shares_seen()

    def _resolve_fund_id(self, ticker: str) -> str | None:
        if ticker in self._fund_id_cache:
            return self._fund_id_cache[ticker]
        resp = self._session.get(RETAIL_URL.format(ticker=ticker.lower()), timeout=30)
        resp.raise_for_status()
        html_text = resp.text
        idx = html_text.find(_PROFILE_ATTR_MARKER)
        if idx < 0:
            return None
        blob = htmlmod.unescape(html_text[idx + len(_PROFILE_ATTR_MARKER) :])
        try:
            data, _ = json.JSONDecoder().raw_decode(blob)
        except json.JSONDecodeError:
            return None
        fund_id = data.get("dashboard", {}).get("associatedFundIds", {}).get("etfFundId")
        if fund_id:
            self._fund_id_cache[ticker] = fund_id
        return fund_id

    def fetch_one(self, ticker: str, identifier: str | None) -> FetchResult:
        fund_id = identifier or self._resolve_fund_id(ticker)
        if not fund_id:
            return FetchResult(ticker=ticker, error="could not resolve Vanguard fundId for this ticker")

        base = ADVISOR_BASE.format(fund_id=fund_id)

        closing_resp = self._session.get(f"{base}/pricing/closing", timeout=30)
        closing_resp.raise_for_status()
        closing = closing_resp.json()
        nav = closing.get("nav") or {}
        asof_date = _parse_iso_date(nav.get("effectiveDate"))
        if nav.get("price") is None or asof_date is None:
            return FetchResult(ticker=ticker, error="no NAV/effectiveDate returned by pricing/closing")

        shares_resp = self._session.get(f"{base}/pricing/outstanding-shares", timeout=30)
        shares_resp.raise_for_status()
        shares_data = shares_resp.json()
        shares_outstanding = self._shares_if_new(ticker, shares_data)

        sleeve = self._sleeve_by_ticker.get(ticker, "")
        duration_params = {
            "dateRange": f"{(asof_date - timedelta(days=10)).isoformat()}:to:{asof_date.isoformat()}",
            "isMunicipalFixedIncome": str(sleeve == "muni").lower(),
            "isInflationProtectedSecurities": str(sleeve == "tips").lower(),
            "isTaxExemptBond": str(sleeve == "muni").lower(),
        }
        duration_resp = self._session.get(
            f"{base}/analytics/daily-fixed-income", params=duration_params, timeout=30
        )
        effective_duration = None
        duration_data = {}
        if duration_resp.ok:
            duration_data = duration_resp.json()
            avg_duration = duration_data.get("averageDuration") or {}
            if avg_duration.get("value") is not None:
                effective_duration = Decimal(str(avg_duration["value"]))

        market_price = closing.get("marketPrice") or {}
        market_close = Decimal(str(market_price["price"])) if market_price.get("price") is not None else None

        raw = RawArtifact(
            ticker=ticker,
            content=json.dumps(
                {"closing": closing, "outstanding_shares": shares_data, "duration": duration_data}
            ).encode("utf-8"),
            content_type="json",
            source_url=f"{base}/pricing/closing",
        )
        observation = FundObservation(
            ticker=ticker,
            asof_date=asof_date,
            shares_outstanding=shares_outstanding,
            nav_per_share=Decimal(str(nav["price"])),
            total_net_assets=None,  # confirmed not published anywhere for this issuer
            market_close=market_close,
            effective_duration=effective_duration,
            spread_duration=None,
        )
        return FetchResult(ticker=ticker, raw=raw, observation=observation)

    def _shares_if_new(self, ticker: str, shares_data: dict) -> int | None:
        effective = shares_data.get("effectiveDate")
        raw_value = shares_data.get("outstandingShares")
        if not effective or raw_value is None:
            return None
        if self._shares_seen.get(ticker) == effective:
            return None  # same monthly print as last time we saw it — not new information
        self._shares_seen[ticker] = effective
        _save_shares_seen(self._shares_seen)
        return round(raw_value)  # Vanguard's own API returns a non-integer share count


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _load_shares_seen() -> dict[str, str]:
    if SHARES_SEEN_PATH.exists():
        return json.loads(SHARES_SEEN_PATH.read_text())
    return {}


def _save_shares_seen(data: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SHARES_SEEN_PATH.write_text(json.dumps(data))
