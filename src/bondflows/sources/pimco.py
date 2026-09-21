"""PIMCO adapter. See notes/pimco.md for the full discovery writeup.

The richest portfolio-characteristics data of any issuer found during
discovery — full-precision NAV and total net assets, directly-published
shares outstanding, and effective duration broken out by risk factor
(bull/bear market duration) plus real spread duration by sector
(mortgage/corporate/emerging-market) — but all of it sits behind a
one-time consent gate: a role-selection tile followed by an "I agree to
be bound by these Terms and Conditions" checkbox. T.J. reviewed this
specifically and authorized the adapter completing that flow
automatically, with the resulting session persisted (see
`persists_session = True`) so it only has to run again once that session
expires, not on every fetch.

Use the full `/us/en/investments/etf/<slug>/usetf-usd` URL form — the
shorter `/en-us/investments/etf/<slug>` alias some search results surface
returns a hard 403 from an unrelated edge rule, confirmed during
discovery.

**One page has two different "Effective Duration" figures** — a compact
fund-profile summary card (e.g. "0.34 yrs") and the "Interest Rate &
Sector Exposures" section (e.g. "0.50", no unit suffix, alongside Bull/
Bear Market Duration and the spread-duration breakdown). This adapter
uses the second one specifically, since that's the one cross-verified
against the original discovery pass — extraction is scoped to that
section's text so the two never get confused.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from bondflows.sources.base import BrowserSourceAdapter, FetchResult, FundObservation, RawArtifact

_ROLE_TILE_TEXT = "Individual Investor"
_TERMS_CHECKBOX_SELECTOR = 'label[for="termsAgree"]'
_NAV_RE = re.compile(r"DAILY NAV\s*\n([\d,.\-]+)\s*USD\s*\n+As of (\d{2}/\d{2}/\d{4})")
_EXPOSURES_SECTION_RE = re.compile(
    r"Interest Rate & Sector Exposures.*?(?=Sector Allocation Duration|$)", re.S
)


class PIMCOAdapter(BrowserSourceAdapter):
    name = "PIMCO"
    persists_session = True

    def fetch_one(self, ticker: str, identifier: str | None) -> FetchResult:
        if not identifier:
            return FetchResult(ticker=ticker, error="no product_url in universe.csv for this ticker")
        url = identifier

        page = self._context.new_page()
        try:
            page.goto(url, timeout=30000)
            page.wait_for_timeout(1500)
            self._ensure_consent(page)

            try:
                page.wait_for_selector("text=Total Net Assets", timeout=15000)
            except Exception:  # noqa: BLE001 - consent may have failed; report it, don't guess
                return FetchResult(
                    ticker=ticker, error="Fund Facts never rendered — consent flow may have failed"
                )

            text = page.inner_text("body")
            raw = RawArtifact(ticker=ticker, content=text.encode("utf-8"), content_type="html", source_url=url)

            nav_m = _NAV_RE.search(text)
            if not nav_m:
                return FetchResult(ticker=ticker, raw=raw, error="could not locate DAILY NAV / as-of-date")
            nav_per_share = _parse_decimal(nav_m.group(1))
            asof_date = _parse_slash_date(nav_m.group(2))
            if nav_per_share is None or asof_date is None:
                return FetchResult(ticker=ticker, raw=raw, error="could not parse NAV/as-of-date")

            total_net_assets = _labeled_decimal(text, r"Total Net Assets \(USD\)")
            shares_outstanding = _labeled_int(text, "Shares Outstanding")

            exposures_m = _EXPOSURES_SECTION_RE.search(text)
            exposures_text = exposures_m.group(0) if exposures_m else ""
            effective_duration = _labeled_decimal(exposures_text, "Effective Duration")
            # PIMCO publishes spread duration split by sector, not one blended
            # figure — store the credit (corporate) one as `spread_duration`,
            # the closest single-number match to what SPEC.md's schema
            # expects; the mortgage/EM breakdown isn't captured in this
            # schema's single spread_duration column.
            spread_duration = _labeled_decimal(exposures_text, "Corporate Spread Duration")

            observation = FundObservation(
                ticker=ticker,
                asof_date=asof_date,
                shares_outstanding=shares_outstanding,
                nav_per_share=nav_per_share,
                total_net_assets=total_net_assets,
                market_close=None,
                effective_duration=effective_duration,
                spread_duration=spread_duration,
            )
            return FetchResult(ticker=ticker, raw=raw, observation=observation)
        finally:
            page.close()
            self.save_session()

    def _ensure_consent(self, page) -> None:
        """No-op if a persisted session already got us past the gate. If the
        role tile is showing, click it to reveal the terms checkbox. If the
        terms checkbox is (now) showing, accept it."""
        body_text = page.inner_text("body")
        if "I acknowledge" not in body_text and _ROLE_TILE_TEXT in body_text:
            try:
                page.get_by_text(_ROLE_TILE_TEXT, exact=True).first.click(timeout=5000)
                page.wait_for_timeout(1000)
                body_text = page.inner_text("body")
            except Exception:  # noqa: BLE001 - gate shape may have changed; fall through
                pass

        if "I acknowledge" in body_text:
            try:
                page.locator(_TERMS_CHECKBOX_SELECTOR).click(timeout=5000)
                page.wait_for_timeout(500)
                page.get_by_role("button", name="Accept", exact=True).click(timeout=5000)
                page.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001 - report via the caller's data-not-found check instead
                pass


def _labeled_decimal(text: str, label_pattern: str) -> Decimal | None:
    m = re.search(label_pattern + r"\s*\n([\d,.\-]+)", text)
    return _parse_decimal(m.group(1)) if m else None


def _labeled_int(text: str, label: str) -> int | None:
    m = re.search(re.escape(label) + r"\s*\n([\d,.\-]+)", text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_decimal(text: str | None) -> Decimal | None:
    if not text:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _parse_slash_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError:
        return None
