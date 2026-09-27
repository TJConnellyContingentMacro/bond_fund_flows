"""Orchestrates adapters, writes raw snapshots, upserts fund_daily.

Isolation is the whole point of this module (SPEC.md §4.3): one ticker
failing must not stop the rest of that issuer's tickers, and one issuer's
adapter failing (or missing entirely) must not stop the others.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from bondflows import db
from bondflows.sources.base import FetchResult, SourceAdapter
from bondflows.universe import Fund, by_issuer, load_universe

logger = logging.getLogger(__name__)

RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"

_EXT_BY_CONTENT_TYPE = {"html": "html", "json": "json"}


@dataclass
class IssuerRunReport:
    issuer: str
    expected: int
    fetched: int
    errors: dict[str, str]


def _raw_path(raw_dir: Path, run_date: date, issuer: str, ticker: str, content_type: str) -> Path:
    ext = _EXT_BY_CONTENT_TYPE.get(content_type, "bin")
    day_dir = raw_dir / f"{run_date:%Y}" / f"{run_date:%m}" / f"{run_date:%d}" / issuer
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir / f"{ticker}.{ext}"


def _prior_raw_path(raw_dir: Path, run_date: date, issuer: str, ticker: str, content_type: str) -> Path | None:
    """Most recent existing raw file for this ticker before run_date, if any."""
    ext = _EXT_BY_CONTENT_TYPE.get(content_type, "bin")
    candidates = sorted(raw_dir.glob(f"*/*/*/{issuer}/{ticker}.{ext}"))
    candidates = [
        p for p in candidates
        if date(int(p.parents[3].name), int(p.parents[2].name), int(p.parents[1].name)) < run_date
    ]
    return candidates[-1] if candidates else None


def _is_stale(raw_dir: Path, result: FetchResult, run_date: date, issuer: str, con) -> bool:
    """SPEC.md §5.2 staleness, judged per (ticker, asof_date) row."""
    # A re-read of an asof_date already captured adds no new row and so no flow; keep the
    # existing flag rather than retroactively marking a real earlier observation stale.
    existing = con.execute(
        "SELECT source_is_stale FROM fund_daily WHERE ticker = ? AND asof_date = ?",
        [result.ticker, result.observation.asof_date],
    ).fetchone()
    if existing is not None:
        return existing[0]

    prior_path = _prior_raw_path(raw_dir, run_date, issuer, result.ticker, result.raw.content_type)
    if prior_path is None:
        return False
    prior_hash = hashlib.sha256(prior_path.read_bytes()).hexdigest()
    return prior_hash == hashlib.sha256(result.raw.content).hexdigest()


def _run_issuer(
    adapter: SourceAdapter, funds: list[Fund], run_date: date, con, raw_dir: Path
) -> IssuerRunReport:
    tickers = [f.ticker for f in funds]
    # issuer_id (a CUSIP/fundId/portfolioId) takes precedence when an adapter
    # has both; product_url is the fallback for adapters that need a full
    # resolved URL instead of a short identifier (e.g. SSGA).
    identifiers = {f.ticker: f.issuer_id or f.product_url for f in funds}
    errors: dict[str, str] = {}
    fetched = 0

    try:
        results = adapter.fetch_all(tickers, identifiers)
    except Exception as exc:  # noqa: BLE001 - one issuer must never stop the run
        logger.exception("%s: adapter failed entirely", adapter.name)
        return IssuerRunReport(
            issuer=adapter.name,
            expected=len(tickers),
            fetched=0,
            errors={t: f"adapter-level failure: {exc}" for t in tickers},
        )

    for result in results:
        if result.error is not None or result.observation is None:
            errors[result.ticker] = result.error or "no observation returned"
            continue

        if result.raw is not None:
            path = _raw_path(raw_dir, run_date, adapter.name, result.ticker, result.raw.content_type)
            stale = _is_stale(raw_dir, result, run_date, adapter.name, con)
            path.write_bytes(result.raw.content)
        else:
            stale = False

        db.upsert_fund_daily(
            con,
            result.observation,
            retrieved_at=result.raw.retrieved_at if result.raw else datetime.now(timezone.utc),
            source=adapter.name,
            source_is_stale=stale,
        )
        fetched += 1

    return IssuerRunReport(issuer=adapter.name, expected=len(tickers), fetched=fetched, errors=errors)


def run_daily(
    adapters: dict[str, SourceAdapter],
    run_date: date | None = None,
    *,
    funds: list[Fund] | None = None,
    raw_dir: Path = RAW_DIR,
    db_path: Path = db.DB_PATH,
) -> list[IssuerRunReport]:
    """adapters: {issuer name -> adapter instance}. Issuers in universe.csv
    with no matching adapter are reported as 0/expected, not skipped silently.

    `funds`/`raw_dir`/`db_path` are override points for tests; production
    callers (scripts/daily.py) use the defaults (real universe.csv, real
    data/raw/, real data/bondflows.duckdb)."""
    run_date = run_date or datetime.now(timezone.utc).date()
    funds_by_issuer = by_issuer(funds if funds is not None else load_universe())
    con = db.connect(db_path)
    reports: list[IssuerRunReport] = []

    for issuer, issuer_funds in funds_by_issuer.items():
        adapter = adapters.get(issuer)
        if adapter is None:
            reports.append(
                IssuerRunReport(
                    issuer=issuer,
                    expected=len(issuer_funds),
                    fetched=0,
                    errors={f.ticker: "no adapter built yet" for f in issuer_funds},
                )
            )
            continue
        reports.append(_run_issuer(adapter, issuer_funds, run_date, con, raw_dir))

    con.close()
    _print_coverage(reports)
    return reports


def _print_coverage(reports: list[IssuerRunReport]) -> None:
    total_expected = sum(r.expected for r in reports)
    total_fetched = sum(r.fetched for r in reports)
    pct = (100 * total_fetched / total_expected) if total_expected else 0.0
    print(f"\nCoverage: {total_fetched}/{total_expected} ({pct:.1f}%)")
    for r in sorted(reports, key=lambda r: r.issuer):
        issuer_pct = (100 * r.fetched / r.expected) if r.expected else 0.0
        print(f"  {r.issuer:<16} {r.fetched}/{r.expected} ({issuer_pct:.0f}%)")
        for ticker, err in sorted(r.errors.items()):
            print(f"    - {ticker}: {err}")
