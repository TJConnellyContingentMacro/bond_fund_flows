"""SSGA (SPDR) adapter. See notes/ssga.md for the full discovery writeup.

Two pages per ticker, both server-rendered (no JS execution needed):
  - retail (`/us/en/individual/etfs/<slug>`) — NAV and Assets Under
    Management (=TNA) as inline JSON objects with a full-precision
    `originalValue`, e.g. `"nav":{"value":"$91.52","originalValue":"91.524085",
    "asOfDateSimple":"Sep 17 2026"}`. No shares outstanding or duration here
    at all, on any fund — confirmed during discovery.
  - intermediary (`/us/en/intermediary/etfs/<slug>` — same slug, different
    path segment) — plain `<table class="tb-keyvalue">` rows under
    `<h2 class="comp-title">SECTION <span class="date">as of DATE</span></h2>`
    headings. "Fund Net Asset Value" has Shares Outstanding; "Fund
    Characteristics" has "Option Adjusted Duration" (= effective duration).
    Both same-day as NAV, no lag.

Product-page slugs turned out to be mechanically derivable from the fund
name (`state-street-<slugified-name>-etf-<ticker>`, where slugifying just
lowercases and replaces spaces with hyphens) for every one of the 21
tickers this adapter covers — `requests` follows the occasional 301 to a
slightly different canonical slug automatically, so exact drift in that
guess doesn't matter. The resolved URL for each ticker is stored in
universe.csv's `product_url` column rather than re-derived at runtime, so a
future new SSGA ticker needs its slug worked out once (same technique) and
added there.
"""

from __future__ import annotations

import html as htmlmod
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import requests

from bondflows.sources.base import FetchResult, FundObservation, RawArtifact, SourceAdapter

USER_AGENT = "Mozilla/5.0 (compatible; bondflows/0.1; contact:tj@contingentmacro.com)"


class SSGAAdapter(SourceAdapter):
    name = "SSGA"

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT

    def fetch_one(self, ticker: str, identifier: str | None) -> FetchResult:
        if not identifier:
            return FetchResult(
                ticker=ticker,
                error="no product_url in universe.csv — SSGA slugs need the fund name, "
                "not just the ticker, to resolve",
            )
        retail_url = identifier

        retail_resp = self._session.get(retail_url, timeout=30)
        retail_resp.raise_for_status()
        retail_text = htmlmod.unescape(retail_resp.text)

        nav_field = _extract_field(retail_text, "nav")
        aum_field = _extract_field(retail_text, "aum")
        nav_per_share = _parse_decimal(nav_field.get("originalValue"))
        asof_date = _parse_month_day_year(nav_field.get("asOfDateSimple"))
        if nav_per_share is None or asof_date is None:
            return FetchResult(
                ticker=ticker,
                raw=RawArtifact(ticker=ticker, content=retail_resp.content, content_type="html", source_url=retail_url),
                error="could not locate NAV/as-of-date on retail page",
            )
        total_net_assets = _parse_decimal(aum_field.get("originalValue"))

        intermediary_url = retail_url.replace("/individual/", "/intermediary/")
        intermediary_resp = self._session.get(intermediary_url, timeout=30)
        intermediary_resp.raise_for_status()
        intermediary_text = htmlmod.unescape(intermediary_resp.text)

        nav_section = _section_rows(intermediary_text, "Fund Net Asset Value")
        characteristics = _section_rows(intermediary_text, "Fund Characteristics")

        shares_outstanding = _parse_millions(nav_section.get("Shares Outstanding"))
        effective_duration = _parse_years(characteristics.get("Option Adjusted Duration"))

        combined_raw = retail_resp.content + b"\n<!-- intermediary -->\n" + intermediary_resp.content
        raw = RawArtifact(ticker=ticker, content=combined_raw, content_type="html", source_url=retail_url)

        observation = FundObservation(
            ticker=ticker,
            asof_date=asof_date,
            shares_outstanding=shares_outstanding,
            nav_per_share=nav_per_share,
            total_net_assets=total_net_assets,
            market_close=None,
            effective_duration=effective_duration,
            spread_duration=None,  # confirmed not published for this issuer
        )
        return FetchResult(ticker=ticker, raw=raw, observation=observation)


def _extract_field(html_text: str, key: str) -> dict[str, str]:
    """Pulls the flat {"label":...,"value":...,"originalValue":...} object
    for one inline field key, e.g. `"nav":{...}`."""
    m = re.search(r'"' + re.escape(key) + r'":\{(.*?)\}', html_text, re.S)
    if not m:
        return {}
    return dict(re.findall(r'"(\w+)":"([^"]*)"', m.group(1)))


def _section_rows(html_text: str, heading_prefix: str) -> dict[str, str]:
    """First `<table class="tb-keyvalue">` after a `<h2 class="comp-title">`
    heading starting with `heading_prefix`. Keeps the first occurrence of
    each label — some sections list the fund's row then the benchmark's
    under the same label."""
    m = re.search(re.escape(heading_prefix) + r".*?<table[^>]*>(.*?)</table>", html_text, re.S)
    if not m:
        return {}
    pattern = re.compile(
        r'<th class="label"[^>]*>\s*([^<]+?)\s*(?:<span.*?</span>)?\s*</th>\s*<td class="data">([^<]*)</td>',
        re.S,
    )
    rows: dict[str, str] = {}
    for label, value in pattern.findall(m.group(1)):
        label = re.sub(r"\s+", " ", label).strip()
        rows.setdefault(label, value.strip())
    return rows


def _parse_decimal(text: str | None) -> Decimal | None:
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_millions(text: str | None) -> int | None:
    """SSGA's intermediary page reports shares outstanding as e.g. '523.28 M'."""
    if not text:
        return None
    cleaned = text.replace(",", "").strip()
    m = re.match(r"([\d.]+)\s*M$", cleaned)
    if not m:
        return None
    try:
        return round(float(m.group(1)) * 1_000_000)
    except ValueError:
        return None


def _parse_years(text: str | None) -> Decimal | None:
    if not text:
        return None
    try:
        return Decimal(text.replace("years", "").replace("yrs", "").strip())
    except InvalidOperation:
        return None


def _parse_month_day_year(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, "%b %d %Y").date()
    except ValueError:
        return None
