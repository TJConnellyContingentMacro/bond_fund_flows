"""The adapter contract every issuer source implements (SPEC.md §4.3).

An adapter reports what it actually observed today. Every field defaults to
``None`` — nothing is ever coerced to zero. A single ticker failing must not
stop the rest of that adapter's tickers (``fetch_all``'s try/except), and a
whole adapter failing must not stop the other adapters (``ingest.py``'s job).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
STATE_DIR = DATA_DIR / ".state"


@dataclass
class FundObservation:
    """One ticker's parsed values for one day. Mirrors the `fund_daily` columns
    an adapter is responsible for."""

    ticker: str
    asof_date: date
    shares_outstanding: int | None = None
    nav_per_share: Decimal | None = None
    total_net_assets: Decimal | None = None
    market_close: Decimal | None = None
    effective_duration: Decimal | None = None
    spread_duration: Decimal | None = None


@dataclass
class RawArtifact:
    """The unparsed payload behind a `FundObservation` — what actually gets
    written under data/raw/ and hashed for staleness detection (SPEC.md §5.2).
    Kept separate from the parsed record so a parsing bug can never corrupt
    what's on disk."""

    ticker: str
    content: bytes
    content_type: str  # e.g. "html", "json"
    source_url: str
    retrieved_at: datetime | None = None


@dataclass
class FetchResult:
    """The outcome of fetching one ticker. `error` set means `observation` is
    None — ingest.py writes nothing for that ticker rather than guessing."""

    ticker: str
    raw: RawArtifact | None = None
    observation: FundObservation | None = None
    error: str | None = None


class SourceAdapter(ABC):
    """One subclass per issuer. `name` must match the `source` column value
    ingest.py writes into fund_daily."""

    name: ClassVar[str]

    @abstractmethod
    def fetch_one(self, ticker: str, identifier: str | None) -> FetchResult:
        """Fetch and parse a single ticker. May raise — fetch_all isolates it."""

    def fetch_all(
        self, tickers: list[str], identifiers: dict[str, str | None]
    ) -> list[FetchResult]:
        results: list[FetchResult] = []
        for ticker in tickers:
            try:
                result = self.fetch_one(ticker, identifiers.get(ticker))
                if result.raw is not None and result.raw.retrieved_at is None:
                    result.raw.retrieved_at = datetime.now(timezone.utc)
            except Exception as exc:  # noqa: BLE001 - isolating per SPEC.md §4.3
                logger.exception("%s: %s failed", self.name, ticker)
                result = FetchResult(ticker=ticker, error=str(exc))
            results.append(result)
        return results


class BrowserSourceAdapter(SourceAdapter):
    """For issuers that need a real browser engine (Akamai bot-blocking a
    plain HTTP client, or a consent gate that only renders after JS runs).
    Launches one headless Chromium instance for the whole `fetch_all` call,
    not one per ticker."""

    #: Set to True by adapters (e.g. PIMCO) that need a persisted login/consent
    #: session across runs, stored at `state_path`.
    persists_session: ClassVar[bool] = False

    #: Most sites are fine with a headless real browser. Set to False for a
    #: site whose bot-management specifically distinguishes headless from a
    #: genuinely displayed browser (confirmed for Schwab: standard headless
    #: Chromium/Chrome gets Access Denied from Akamai regardless of
    #: User-Agent or navigator.webdriver patching; a headed real Chrome
    #: passes with no masking needed at all). A headed adapter needs the cron
    #: to run in a session with desktop access, not a bare background service.
    headless: ClassVar[bool] = True
    #: "chrome" uses the system's real installed Chrome instead of
    #: Playwright's bundled Chromium — matters for sites that fingerprint
    #: the browser binary itself.
    channel: ClassVar[str | None] = None

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None

    @property
    def state_path(self) -> Path:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        return STATE_DIR / f"{self.name}_storage_state.json"

    def fetch_all(
        self, tickers: list[str], identifiers: dict[str, str | None]
    ) -> list[FetchResult]:
        from playwright.sync_api import sync_playwright

        state = str(self.state_path) if self.persists_session and self.state_path.exists() else None
        self._playwright = sync_playwright().start()
        try:
            launch_kwargs = {"headless": self.headless}
            if self.channel:
                launch_kwargs["channel"] = self.channel
            self._browser = self._playwright.chromium.launch(**launch_kwargs)
            self._context = self._browser.new_context(storage_state=state)
            return super().fetch_all(tickers, identifiers)
        finally:
            if self._context is not None:
                self._context.close()
            if self._browser is not None:
                self._browser.close()
            self._playwright.stop()

    def save_session(self) -> None:
        if self._context is not None:
            self._context.storage_state(path=str(self.state_path))
