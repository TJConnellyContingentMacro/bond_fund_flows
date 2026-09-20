"""iShares adapter. See notes/ishares.md for the full discovery writeup this
is built against.

Two sources per ticker:
  - The bulk product-screener JSON resolves every ticker's portfolioId and
    product page URL in one request per run (not one per ticker) — this is
    what universe.csv's `issuer_id`/`product_url` columns were populated
    from, but the adapter re-resolves live rather than trusting a stale CSV
    snapshot, since the screener is cheap and self-updating.
  - Each fund's own product page is server-rendered HTML with the real data
    embedded in HTML-entity-encoded JSON blobs, one per `data-componentname`
    attribute (`KeyFundFactsV3`, `PortfolioCharacteristicsV3`, `fundHeader`).

Known trap (do not use): `/us/literature/cashflows/<ticker>-etf-cash-flows.csv`
is bond cashflow *projections*, not fund flows.
"""

from __future__ import annotations

import bisect
import html as htmlmod
import json
import re
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import requests

from bondflows.sources.base import FetchResult, FundObservation, RawArtifact, SourceAdapter

BASE_URL = "https://www.ishares.com"
SCREENER_URL = (
    f"{BASE_URL}/us/product-screener/product-screener-v3.1.jsn"
    "?dcrPath=/templatedata/config/product-screener-v3/data/en/us-ishares/"
    "ishares-product-screener-backend-config&siteEntryPassthrough=true"
)
USER_AGENT = "Mozilla/5.0 (compatible; bondflows/0.1; contact:tj@contingentmacro.com)"
REQUEST_DELAY_SECONDS = 0.5  # politeness between per-fund page fetches

_COMPONENT_NAME_RE = re.compile(r'data-componentname="([^"]+)"')
_COMPONENT_PROPS_RE = re.compile(r'componentprops="([^"]*)"')


class ISharesAdapter(SourceAdapter):
    name = "iShares"

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._screener_cache: dict[str, dict] | None = None
        self._first_request = True

    def _screener(self) -> dict[str, dict]:
        if self._screener_cache is None:
            resp = self._session.get(SCREENER_URL, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            self._screener_cache = {
                rec["localExchangeTicker"]: rec for rec in data.values() if rec.get("localExchangeTicker")
            }
        return self._screener_cache

    def fetch_one(self, ticker: str, identifier: str | None) -> FetchResult:
        record = self._screener().get(ticker)
        if record is None:
            return FetchResult(ticker=ticker, error="ticker not found in iShares product screener")

        if not self._first_request:
            time.sleep(REQUEST_DELAY_SECONDS)
        self._first_request = False

        url = BASE_URL + record["productPageUrl"]
        resp = self._session.get(url, timeout=30)
        resp.raise_for_status()
        raw = RawArtifact(ticker=ticker, content=resp.content, content_type="html", source_url=url)

        components = _extract_components(resp.text)
        key_facts = components.get("KeyFundFactsV3", {}).get("dataPoints", {})
        portfolio_chars = components.get("PortfolioCharacteristicsV3", {}).get("dataPoints", {})
        nav_point = (
            components.get("fundHeader", {})
            .get("containersByNameMap", {})
            .get("fundNav", {})
            .get("dataPointsByNameMap", {})
            .get("navAmount")
        )

        nav_per_share = None
        asof_date = None
        if nav_point and nav_point.get("value") is not None:
            nav_per_share = Decimal(str(nav_point["value"]))
            asof_int = nav_point.get("asOfDate")
            if asof_int:
                asof_date = datetime.strptime(str(asof_int), "%Y%m%d").date()

        if asof_date is None:
            asof_date = _parse_month_day_year(key_facts.get("sharesOutstanding", {}).get("formattedAsOfDate"))

        if nav_per_share is None or asof_date is None:
            return FetchResult(
                ticker=ticker, raw=raw, error="could not locate NAV or as-of-date on product page"
            )

        observation = FundObservation(
            ticker=ticker,
            asof_date=asof_date,
            shares_outstanding=_parse_int(key_facts.get("sharesOutstanding", {}).get("formattedValue")),
            nav_per_share=nav_per_share,
            total_net_assets=_parse_decimal(key_facts.get("totalNetAssetsFundLevel", {}).get("formattedValue")),
            market_close=_parse_decimal(key_facts.get("closingPrice", {}).get("formattedValue")),
            effective_duration=_parse_years(portfolio_chars.get("modelOad", {}).get("formattedValue")),
            spread_duration=None,  # confirmed not published for this issuer
        )
        return FetchResult(ticker=ticker, raw=raw, observation=observation)


def _extract_components(html_text: str) -> dict[str, dict]:
    """Pairs each componentprops JSON blob with its nearest preceding
    data-componentname attribute. Keeps the first occurrence of each name —
    iShares repeats some components multiple times per page (once per
    render variant), and the first is always the populated one."""
    names = [(m.start(), m.group(1)) for m in _COMPONENT_NAME_RE.finditer(html_text)]
    name_positions = [pos for pos, _ in names]
    components: dict[str, dict] = {}
    for m in _COMPONENT_PROPS_RE.finditer(html_text):
        idx = bisect.bisect_right(name_positions, m.start()) - 1
        if idx < 0:
            continue
        component_name = names[idx][1]
        if component_name in components:
            continue
        try:
            components[component_name] = json.loads(htmlmod.unescape(m.group(1)))
        except json.JSONDecodeError:
            continue
    return components


def _parse_int(formatted: str | None) -> int | None:
    if not formatted:
        return None
    try:
        return int(formatted.replace(",", ""))
    except ValueError:
        return None


def _parse_decimal(formatted: str | None) -> Decimal | None:
    if not formatted:
        return None
    cleaned = formatted.replace(",", "").replace("$", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_years(formatted: str | None) -> Decimal | None:
    if not formatted:
        return None
    try:
        return Decimal(formatted.replace("yrs", "").strip())
    except InvalidOperation:
        return None


def _parse_month_day_year(formatted: str | None) -> date | None:
    if not formatted:
        return None
    try:
        return datetime.strptime(formatted, "%b %d, %Y").date()
    except ValueError:
        return None
