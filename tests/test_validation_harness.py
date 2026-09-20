"""The validation harness, SPEC.md §9 — written before the analytics layer.

Three of the five are testable now, against ingest.py alone, with fake
adapters and no real network calls. The other two ("identity test" and
"spot check against a public source") plus the split regression test need
flows.py's flow_usd to exist (step 4) and are stubbed below so the full
five-item structure stays visible.
"""

from __future__ import annotations

from datetime import date

import pytest

from bondflows import db, ingest
from bondflows.sources.base import FetchResult, FundObservation, RawArtifact, SourceAdapter
from bondflows.universe import Fund


def make_fund(ticker: str, issuer: str) -> Fund:
    return Fund(
        ticker=ticker,
        name=f"{ticker} test fund",
        issuer=issuer,
        sleeve="ust_short",
        sub_sleeve="index",
        in_core=True,
        confidence="high",
        issuer_id=None,
        product_url=None,
        notes="",
    )


class GoodAdapter(SourceAdapter):
    name = "good_issuer"

    def __init__(self, content: bytes = b'{"nav": 100.0}', asof_date: date = date(2026, 9, 19)):
        self.content = content
        self.asof_date = asof_date

    def fetch_one(self, ticker: str, identifier: str | None) -> FetchResult:
        raw = RawArtifact(
            ticker=ticker, content=self.content, content_type="json", source_url="http://example.test"
        )
        observation = FundObservation(
            ticker=ticker,
            asof_date=self.asof_date,
            shares_outstanding=1_000_000,
            nav_per_share=100,
        )
        return FetchResult(ticker=ticker, raw=raw, observation=observation)


class NoDataAdapter(SourceAdapter):
    """Simulates a ticker the site genuinely has nothing for today —
    e.g. Janus Henderson's empty NAV field found during discovery."""

    name = "no_data_issuer"

    def fetch_one(self, ticker: str, identifier: str | None) -> FetchResult:
        return FetchResult(ticker=ticker, error="site returned an empty NAV field")


class RaisingAdapter(SourceAdapter):
    """Simulates a whole adapter breaking (e.g. the site changed shape) —
    not just one ticker."""

    name = "raising_issuer"

    def fetch_one(self, ticker: str, identifier: str | None) -> FetchResult:
        raise AssertionError("should never be reached — fetch_all itself fails first")

    def fetch_all(self, tickers, identifiers):
        raise RuntimeError("site changed shape, nothing parseable")


def test_no_silent_zeros(tmp_path):
    """A ticker the adapter couldn't fetch gets no fund_daily row at all —
    never a row of zeros or NULLs pretending to be a real observation."""
    funds = [make_fund("AAA", "no_data_issuer")]
    db_path = tmp_path / "db.duckdb"

    reports = ingest.run_daily(
        {"no_data_issuer": NoDataAdapter()},
        run_date=date(2026, 9, 19),
        funds=funds,
        raw_dir=tmp_path / "raw",
        db_path=db_path,
    )

    assert reports[0].fetched == 0
    assert reports[0].errors["AAA"] == "site returned an empty NAV field"

    con = db.connect(db_path)
    rows = con.execute("SELECT * FROM fund_daily WHERE ticker = 'AAA'").fetchall()
    con.close()
    assert rows == []


def test_staleness_fixture(tmp_path):
    """Two identical consecutive payloads (same bytes, same issuer-labeled
    asof_date) must produce a stale flag on the second day, not a fresh
    (and phantom-flow-inducing) observation."""
    funds = [make_fund("BBB", "good_issuer")]
    same_content = b'{"nav": 100.0, "asof": "2026-09-18"}'
    unchanged_asof = date(2026, 9, 18)  # the issuer's file didn't move forward
    raw_dir = tmp_path / "raw"
    db_path = tmp_path / "db.duckdb"

    ingest.run_daily(
        {"good_issuer": GoodAdapter(same_content, asof_date=unchanged_asof)},
        run_date=date(2026, 9, 18),
        funds=funds,
        raw_dir=raw_dir,
        db_path=db_path,
    )
    con = db.connect(db_path)
    day1_stale = con.execute(
        "SELECT source_is_stale FROM fund_daily WHERE ticker = 'BBB' AND asof_date = ?", [unchanged_asof]
    ).fetchone()[0]
    con.close()
    assert day1_stale is False  # nothing to compare against yet

    ingest.run_daily(
        {"good_issuer": GoodAdapter(same_content, asof_date=unchanged_asof)},
        run_date=date(2026, 9, 19),
        funds=funds,
        raw_dir=raw_dir,
        db_path=db_path,
    )
    con = db.connect(db_path)
    rows = con.execute("SELECT asof_date, source_is_stale FROM fund_daily WHERE ticker = 'BBB'").fetchall()
    con.close()

    # The issuer's file never advanced past 2026-09-18, so there's still only
    # one row for it (the upsert re-confirms the same day) — now flagged stale.
    assert rows == [(unchanged_asof, True)]


def test_adapter_isolation(tmp_path):
    """One issuer's adapter breaking entirely must not stop another issuer's
    tickers from being fetched and written."""
    funds = [make_fund("CCC", "good_issuer"), make_fund("DDD", "raising_issuer")]

    reports = ingest.run_daily(
        {"good_issuer": GoodAdapter(), "raising_issuer": RaisingAdapter()},
        run_date=date(2026, 9, 19),
        funds=funds,
        raw_dir=tmp_path / "raw",
        db_path=tmp_path / "db.duckdb",
    )

    by_issuer = {r.issuer: r for r in reports}
    assert by_issuer["good_issuer"].fetched == 1
    assert by_issuer["raising_issuer"].fetched == 0
    assert "DDD" in by_issuer["raising_issuer"].errors


@pytest.mark.skip(reason="needs flows.py — step 4")
def test_identity_reconciles_tna_to_flow_plus_return():
    """SPEC.md §9: for a sample of funds/days, ΔTNA should reconcile to
    flow_usd + market return within a tolerance implying a plausible daily
    return."""


@pytest.mark.skip(reason="needs flows.py — step 4")
def test_spot_check_against_public_source():
    """SPEC.md §9: pick a handful of large, unambiguous flow days and compare
    sign/magnitude against a published figure (ETF.com, issuer press
    release). A systematic one-day offset is the settlement-vs-trade-date
    signature from §5.2."""


@pytest.mark.skip(reason="needs flows.py — step 4")
def test_split_regression_no_phantom_flow():
    """SPEC.md §9: a fixture containing a known reverse split must show no
    phantom flow in the adjusted series."""
