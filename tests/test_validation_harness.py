"""The validation harness, SPEC.md §9 — written before the analytics layer.

Three of the five test ingest.py alone, with fake adapters and no real
network calls. Two more — the identity reconciliation and the split
regression — now that flows.py exists (step 4), test flows.py against
fund_daily fixtures written directly into a throwaway DuckDB file.

The fifth, "spot check against a public source," is inherently not a unit
test (it means comparing a real fund's real flow against a real published
figure) and stays out of the automated suite — see CLAUDE.md for where
that check is expected to happen instead.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bondflows import db, ingest
from bondflows.flows import compute_ticker_flows
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
    """An unchanged file relabeled with a new asof_date (byte-identical
    payload) must be flagged stale on the new date, not treated as a fresh
    (and phantom-zero-flow-inducing) observation."""
    funds = [make_fund("BBB", "good_issuer")]
    same_content = b'{"nav": 100.0}'
    raw_dir = tmp_path / "raw"
    db_path = tmp_path / "db.duckdb"

    for run_date in (date(2026, 9, 18), date(2026, 9, 19)):
        ingest.run_daily(
            {"good_issuer": GoodAdapter(same_content, asof_date=run_date)},
            run_date=run_date,
            funds=funds,
            raw_dir=raw_dir,
            db_path=db_path,
        )

    con = db.connect(db_path)
    rows = con.execute(
        "SELECT asof_date, source_is_stale FROM fund_daily WHERE ticker = 'BBB' ORDER BY asof_date"
    ).fetchall()
    con.close()
    assert rows == [(date(2026, 9, 18), False), (date(2026, 9, 19), True)]


def test_rereading_an_unadvanced_date_does_not_poison_the_original_row(tmp_path):
    """If the issuer hasn't posted a newer date yet, a later run re-reads the
    same asof_date. That adds no new row (so no flow), and must not flip the
    earlier, genuinely fresh capture to stale — otherwise it can never anchor
    a flow once a newer date does arrive."""
    funds = [make_fund("BBB", "good_issuer")]
    content = b'{"nav": 100.0, "asof": "2026-09-18"}'
    unchanged_asof = date(2026, 9, 18)
    raw_dir = tmp_path / "raw"
    db_path = tmp_path / "db.duckdb"

    for run_date in (date(2026, 9, 18), date(2026, 9, 19), date(2026, 9, 20)):
        ingest.run_daily(
            {"good_issuer": GoodAdapter(content, asof_date=unchanged_asof)},
            run_date=run_date,
            funds=funds,
            raw_dir=raw_dir,
            db_path=db_path,
        )

    con = db.connect(db_path)
    rows = con.execute("SELECT asof_date, source_is_stale FROM fund_daily WHERE ticker = 'BBB'").fetchall()
    con.close()
    assert rows == [(unchanged_asof, False)]


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


def _seed_fund_daily(con, ticker: str, rows: list[tuple]) -> None:
    """rows: (asof_date, shares_outstanding, nav_per_share, total_net_assets,
    source_is_stale). Writes fund_daily directly — this is a flows.py unit
    test, not an ingest.py one, so it skips the adapter/raw-file machinery
    entirely."""
    for asof_date, shares, nav, tna, stale in rows:
        con.execute(
            """
            INSERT INTO fund_daily (
                ticker, asof_date, retrieved_at, shares_outstanding, nav_per_share,
                total_net_assets, source, source_is_stale
            ) VALUES (?, ?, ?, ?, ?, ?, 'test', ?)
            """,
            [ticker, asof_date, asof_date, shares, nav, tna, stale],
        )


def test_identity_reconciles_tna_to_flow_plus_return(tmp_path):
    """SPEC.md §9/§5.3: ΔTNA should reconcile to flow_usd + market return
    within a tolerance implying a plausible daily return. For a clean
    (non-split, non-gap) day this holds exactly by construction:
    ΔTNA = ΔShares*nav(t) + shares(t-1)*ΔNAV(t) = flow_usd + market_return."""
    con = db.connect(tmp_path / "db.duckdb")
    ticker = "AAA"
    day0, day1 = date(2026, 9, 17), date(2026, 9, 18)
    shares0, nav0 = 10_000_000, Decimal("100.00")
    shares1, nav1 = 10_050_000, Decimal("100.10")
    tna0, tna1 = shares0 * nav0, shares1 * nav1
    _seed_fund_daily(
        con,
        ticker,
        [
            (day0, shares0, nav0, tna0, False),
            (day1, shares1, nav1, tna1, False),
        ],
    )

    flows = compute_ticker_flows(con, ticker, splits={})
    con.close()

    assert len(flows) == 1
    assert flows[0]["flag"] == "clean"
    flow_usd = flows[0]["flow_usd"]

    market_return = shares0 * (nav1 - nav0)
    delta_tna = tna1 - tna0
    assert delta_tna == flow_usd + market_return


@pytest.mark.skip(
    reason="inherently manual — compares a real fund's flow against a real "
    "published figure, not something a fixture can stand in for; see "
    "CLAUDE.md for where this check happens instead of the automated suite"
)
def test_spot_check_against_public_source():
    """SPEC.md §9: pick a handful of large, unambiguous flow days and compare
    sign/magnitude against a published figure (ETF.com, issuer press
    release). A systematic one-day offset is the settlement-vs-trade-date
    signature from §5.2."""


def test_split_regression_no_phantom_flow(tmp_path):
    """SPEC.md §9: a fixture containing a known reverse split must show no
    phantom flow in the adjusted series — the raw ΔShares looks like a ~75%
    redemption, but with the split confirmed in splits.csv the adjusted flow
    reflects only the small real change."""
    con = db.connect(tmp_path / "db.duckdb")
    ticker = "BBB"
    day0, day1 = date(2026, 9, 17), date(2026, 9, 18)
    # 1-for-4 reverse split: shares drop ~75%, NAV rises ~4x, small real flow
    # (+50,000 post-split shares) layered on top.
    shares0, nav0 = 40_000_000, Decimal("25.00")
    shares1, nav1 = 10_050_000, Decimal("100.00")
    _seed_fund_daily(
        con,
        ticker,
        [
            (day0, shares0, nav0, shares0 * nav0, False),
            (day1, shares1, nav1, shares1 * nav1, False),
        ],
    )

    # Without a confirmed split entry, this must be flagged suspect, not
    # reported as a phantom ~$775M outflow.
    unconfirmed = compute_ticker_flows(con, ticker, splits={})
    assert unconfirmed[0]["flag"] == "suspect"
    assert unconfirmed[0]["flow_usd"] is None

    # Once confirmed in splits.csv (ratio = new_shares_per_old_share = 0.25),
    # the adjusted series shows the real, small flow.
    confirmed = compute_ticker_flows(con, ticker, splits={(ticker, day1): Decimal("0.25")})
    con.close()

    assert confirmed[0]["flag"] == "split_adjusted"
    assert confirmed[0]["shares_delta_adjusted"] == 50_000
    assert confirmed[0]["flow_usd"] == Decimal(50_000) * nav1
    assert abs(confirmed[0]["flow_usd"]) < Decimal("10000000")  # nowhere near the phantom $775M
