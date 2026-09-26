"""SPEC.md §8: gap detection. Re-fetch isn't implemented (no adapter can
pull a historical date — see backfill_gaps.py's docstring), so these tests
cover the reporting logic only."""

from __future__ import annotations

from datetime import date

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backfill_gaps import find_gaps  # noqa: E402

from bondflows import db


def _seed(con, ticker: str, clean_dates: list[date], stale_dates: list[date] | None = None) -> None:
    for d in clean_dates:
        con.execute(
            "INSERT INTO fund_daily (ticker, asof_date, retrieved_at, source, source_is_stale) VALUES (?, ?, ?, 'test', FALSE)",
            [ticker, d, d],
        )
    for d in stale_dates or []:
        con.execute(
            "INSERT INTO fund_daily (ticker, asof_date, retrieved_at, source, source_is_stale) VALUES (?, ?, ?, 'test', TRUE)",
            [ticker, d, d],
        )


def test_no_gaps_when_every_business_day_has_a_clean_observation(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    # Mon 9/14 through Fri 9/18 — a full business week, no gaps.
    _seed(con, "AAA", [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)])

    report = find_gaps(con, as_of=date(2026, 9, 18))
    con.close()

    assert report.expected_business_days == 5
    assert report.gaps == []
    assert report.coverage_pct == 100.0


def test_missing_weekday_is_a_gap_but_weekend_is_not(tmp_path):
    con = db.connect(tmp_path / "db.duckdb")
    # Seed Mon 9/14 and Fri 9/18 only. Tue-Thu (9/15-9/17) are gaps; the
    # weekend (9/19-9/20) is never "expected" at all.
    _seed(con, "AAA", [date(2026, 9, 14), date(2026, 9, 18)])

    report = find_gaps(con, as_of=date(2026, 9, 20))
    con.close()

    assert report.expected_business_days == 5  # Mon-Fri only
    assert report.gaps == [
        ("AAA", date(2026, 9, 15)),
        ("AAA", date(2026, 9, 16)),
        ("AAA", date(2026, 9, 17)),
    ]
    assert report.coverage_pct == 40.0  # 2 of 5 expected days are clean


def test_stale_flagged_day_counts_as_a_gap_not_a_clean_observation(tmp_path):
    """A row exists but is flagged source_is_stale — that's not a clean
    observation, so it must still show up as a gap."""
    con = db.connect(tmp_path / "db.duckdb")
    _seed(con, "AAA", clean_dates=[date(2026, 9, 14)], stale_dates=[date(2026, 9, 15)])

    report = find_gaps(con, as_of=date(2026, 9, 15))
    con.close()

    assert report.gaps == [("AAA", date(2026, 9, 15))]


def test_each_ticker_only_expected_from_its_own_first_observation(tmp_path):
    """A ticker that starts reporting later (e.g. a new adapter, or a fund
    that just launched) shouldn't be penalized for days before it existed
    in fund_daily at all."""
    con = db.connect(tmp_path / "db.duckdb")
    _seed(con, "AAA", [date(2026, 9, 14), date(2026, 9, 15)])
    _seed(con, "BBB", [date(2026, 9, 17), date(2026, 9, 18)])  # starts later, no gaps of its own

    report = find_gaps(con, as_of=date(2026, 9, 18))
    con.close()

    # AAA: Mon-Fri (9/14-9/18) = 5 expected, missing 9/16-9/18 = 3 gaps.
    # BBB: only expected from 9/17 (its own start) = 2 expected, 0 gaps.
    assert report.expected_business_days == 7
    assert report.gaps == [
        ("AAA", date(2026, 9, 16)),
        ("AAA", date(2026, 9, 17)),
        ("AAA", date(2026, 9, 18)),
    ]
    assert "BBB" not in report.gaps_by_ticker
