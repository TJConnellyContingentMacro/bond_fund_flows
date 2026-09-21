"""Invesco adapter. See notes/invesco.md for the full discovery writeup.

One clean endpoint gives everything the core flow identity needs, keyed by
CUSIP: `dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/<cusip>/
prices?idType=cusip&variationType=priceListing&productType=ETF` — NAV,
shares outstanding, and total net assets (`marketValue`), all daily, full
precision, in one call.

`effective_duration`/`spread_duration` stay NULL for every ticker — the
page displays duration figures, but the source endpoint was never traced
during discovery (tried keyStats, yieldInformation, holdings/fund; none of
them return it). Documented as an open item, not silently guessed at.

The endpoint rate-limits per client, not per CUSIP: hammering it with
several requests in quick succession (confirmed with plain `curl`, not a
`requests`-library quirk) gets every request after the first one back a
cached `406 Not Acceptable` — `X-Cache: HIT` in the response headers shows
Varnish is serving that 406 from cache, so it doesn't clear until whatever
window Varnish is holding it for expires (observed anywhere from ~10s to
45s+). Spacing requests out avoids triggering it in the first place, which
is what this adapter does; the retry-with-backoff is a second line of
defense for whenever the fixed spacing isn't quite enough.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from decimal import Decimal

import requests

from bondflows.sources.base import FetchResult, FundObservation, RawArtifact, SourceAdapter

PRICES_URL = (
    "https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/{cusip}/prices"
    "?idType=cusip&variationType=priceListing&productType=ETF"
)
USER_AGENT = "Mozilla/5.0 (compatible; bondflows/0.1; contact:tj@contingentmacro.com)"
MIN_REQUEST_INTERVAL_SECONDS = 8.0  # proactive spacing to avoid the rate limit
RETRY_DELAYS_SECONDS = (10, 20, 30)  # reactive backoff if spacing wasn't enough


class InvescoAdapter(SourceAdapter):
    name = "Invesco"

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._last_request_at: float | None = None

    def fetch_one(self, ticker: str, identifier: str | None) -> FetchResult:
        if not identifier:
            return FetchResult(ticker=ticker, error="no CUSIP in universe.csv for this ticker")
        cusip = identifier

        resp = self._get_with_retry(PRICES_URL.format(cusip=cusip))
        resp.raise_for_status()
        data = resp.json()
        raw = RawArtifact(
            ticker=ticker, content=resp.content, content_type="json", source_url=resp.url
        )

        asof_date = _parse_iso_date(data.get("effectiveDate"))
        nav = data.get("nav")
        if asof_date is None or nav is None:
            return FetchResult(ticker=ticker, raw=raw, error="no NAV/effectiveDate in prices response")

        market_value = data.get("marketValue")
        shares_outstanding = data.get("sharesOutstanding")

        observation = FundObservation(
            ticker=ticker,
            asof_date=asof_date,
            shares_outstanding=int(shares_outstanding) if shares_outstanding is not None else None,
            nav_per_share=Decimal(str(nav)),
            total_net_assets=Decimal(str(market_value)) if market_value is not None else None,
            market_close=Decimal(str(data["closingPrice"])) if data.get("closingPrice") is not None else None,
            effective_duration=None,  # source endpoint never traced — see module docstring
            spread_duration=None,
        )
        return FetchResult(ticker=ticker, raw=raw, observation=observation)

    def _throttled_get(self, url: str) -> requests.Response:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
                time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()
        return self._session.get(url, timeout=30)

    def _get_with_retry(self, url: str) -> requests.Response:
        resp = self._throttled_get(url)
        for delay in RETRY_DELAYS_SECONDS:
            if resp.status_code != 406:
                break
            time.sleep(delay)
            resp = self._throttled_get(url)
        return resp


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
