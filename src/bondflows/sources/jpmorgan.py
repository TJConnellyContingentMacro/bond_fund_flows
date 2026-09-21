"""JPMorgan adapter. See notes/jpmorgan.md for the full discovery writeup.

Two endpoints, both keyed by CUSIP, both work fine without the `version`
query param the site's own browser traffic includes (that's a cache-buster
tied to a build number, not a required parameter):
  - `/FundsMarketingHandler/historicalData?cusip=<cusip>&...` —
    `historicalETFNAVMarketPriceList`, the complete daily NAV history since
    inception in one call. The last entry is today's NAV.
  - `/FundsMarketingHandler/product-data?cusip=<cusip>&...` —
    `aum.netAsset` (total net assets, full precision, same day as NAV) and
    `averageLife.data[]` (an array of `{name, value}` rows). "Duration" is
    the effective-duration equivalent; "Spread Duration" appears only for
    some funds (JPST has it, JMUB/JCPB don't) — JPMorgan is the only issuer
    found during discovery that publishes real spread duration at all,
    just not for every fund.

Shares outstanding isn't a distinct published field — arithmetically
identical to `aum.netAsset ÷ navPrice`, so this adapter derives it the same
way SPEC.md §4.1 describes as the general fallback, using full-precision
inputs rather than rounded display values.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import requests

from bondflows.sources.base import FetchResult, FundObservation, RawArtifact, SourceAdapter

PRODUCT_DATA_URL = (
    "https://am.jpmorgan.com/FundsMarketingHandler/product-data"
    "?cusip={cusip}&country=us&role=adv&language=en&userLoggedIn=false"
)
HISTORICAL_URL = (
    "https://am.jpmorgan.com/FundsMarketingHandler/historicalData"
    "?cusip={cusip}&country=us&role=adv&userLoggedIn=false&language=en"
)
USER_AGENT = "Mozilla/5.0 (compatible; bondflows/0.1; contact:tj@contingentmacro.com)"


class JPMorganAdapter(SourceAdapter):
    name = "JPMorgan"

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT

    def fetch_one(self, ticker: str, identifier: str | None) -> FetchResult:
        if not identifier:
            return FetchResult(ticker=ticker, error="no CUSIP in universe.csv for this ticker")
        cusip = identifier

        hist_resp = self._session.get(HISTORICAL_URL.format(cusip=cusip), timeout=30)
        hist_resp.raise_for_status()
        nav_list = hist_resp.json().get("historicalETFNAVMarketPriceList") or []
        if not nav_list:
            return FetchResult(ticker=ticker, error="historicalData returned no NAV history")
        latest = nav_list[-1]
        asof_date = _parse_iso_date(latest.get("date"))
        nav_value = latest.get("navPrice")
        if asof_date is None or nav_value is None:
            return FetchResult(ticker=ticker, error="latest NAV row missing date/navPrice")
        nav_per_share = Decimal(str(nav_value))
        market_close = (
            Decimal(str(latest["marketValueNavPrice"]))
            if latest.get("marketValueNavPrice") is not None
            else None
        )

        product_resp = self._session.get(PRODUCT_DATA_URL.format(cusip=cusip), timeout=30)
        product_resp.raise_for_status()
        fund_data = product_resp.json().get("fundData", {})

        aum = fund_data.get("aum") or {}
        total_net_assets = Decimal(str(aum["netAsset"])) if aum.get("netAsset") is not None else None

        shares_outstanding = None
        if total_net_assets is not None:
            shares_outstanding = round(total_net_assets / nav_per_share)

        avg_life_rows = (fund_data.get("averageLife") or {}).get("data") or []
        effective_duration = _find_value(avg_life_rows, "Duration")
        spread_duration = _find_value(avg_life_rows, "Spread Duration")

        raw = RawArtifact(
            ticker=ticker,
            content=json.dumps({"historical_last": latest, "aum": aum, "averageLife": avg_life_rows}).encode(
                "utf-8"
            ),
            content_type="json",
            source_url=hist_resp.url,
        )
        observation = FundObservation(
            ticker=ticker,
            asof_date=asof_date,
            shares_outstanding=int(shares_outstanding) if shares_outstanding is not None else None,
            nav_per_share=nav_per_share,
            total_net_assets=total_net_assets,
            market_close=market_close,
            effective_duration=effective_duration,
            spread_duration=spread_duration,
        )
        return FetchResult(ticker=ticker, raw=raw, observation=observation)


def _find_value(rows: list[dict], name: str) -> Decimal | None:
    for row in rows:
        if row.get("name") == name and row.get("value") is not None:
            return Decimal(str(row["value"]))
    return None


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
