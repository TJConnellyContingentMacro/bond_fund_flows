"""Gap detection, SPEC.md §8: "identify every (ticker, business day) with no
clean observation, attempt re-fetch where the issuer offers history, and
mark the rest permanently imputed. Print a coverage percentage every run."

**Re-fetch is not implemented.** None of the 8 adapters built so far can
fetch a *historical* date — every one of them only reads the issuer's
current live page/API response for today's value (confirmed while building
each one; this is a real, structural limitation of scraping public product
pages rather than a paid historical-data feed). So every gap found here is
already effectively permanent, and this script is a pure report: it never
writes to fund_daily. The "mark the rest permanently imputed" half of §8's
instruction already happens naturally in flows.py — when compute_ticker_
flows walks past a gap to the next real observation, that row gets flagged
`imputed` on its own, without this script's help.

This script's value is longitudinal and grows with accumulated history: on
a database with only 1-2 days of history (like this project has today) it
reports close to 100% coverage almost by definition, since there's been
almost no time in which a gap could open up. Run daily so a real gap (an
issuer's adapter silently breaking for a week, say) becomes visible instead
of invisible.

Usage:
    python scripts/backfill_gaps.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bondflows import db  # noqa: E402

_GAP_QUERY = """
WITH ticker_start AS (
    SELECT ticker, MIN(asof_date) AS start_date
    FROM fund_daily
    GROUP BY ticker
),
expected_business_days AS (
    SELECT t.ticker, CAST(gs.d AS DATE) AS business_day
    FROM ticker_start t, generate_series(t.start_date, CAST(? AS DATE), INTERVAL 1 DAY) AS gs(d)
    WHERE isodow(CAST(gs.d AS DATE)) < 6
),
clean_days AS (
    SELECT DISTINCT ticker, asof_date
    FROM fund_daily
    WHERE source_is_stale = FALSE
)
SELECT e.ticker, e.business_day
FROM expected_business_days e
LEFT JOIN clean_days c ON c.ticker = e.ticker AND c.asof_date = e.business_day
WHERE c.asof_date IS NULL
ORDER BY e.ticker, e.business_day
"""

_EXPECTED_COUNT_QUERY = """
WITH ticker_start AS (
    SELECT ticker, MIN(asof_date) AS start_date
    FROM fund_daily
    GROUP BY ticker
)
SELECT COUNT(*)
FROM ticker_start t, generate_series(t.start_date, CAST(? AS DATE), INTERVAL 1 DAY) AS gs(d)
WHERE isodow(CAST(gs.d AS DATE)) < 6
"""


@dataclass
class GapReport:
    expected_business_days: int
    gaps: list[tuple[str, date]]

    @property
    def clean_business_days(self) -> int:
        return self.expected_business_days - len(self.gaps)

    @property
    def coverage_pct(self) -> float:
        if self.expected_business_days == 0:
            return 100.0
        return 100 * self.clean_business_days / self.expected_business_days

    @property
    def gaps_by_ticker(self) -> dict[str, list[date]]:
        by_ticker: dict[str, list[date]] = {}
        for ticker, day in self.gaps:
            by_ticker.setdefault(ticker, []).append(day)
        return by_ticker


def find_gaps(con, as_of: date | None = None) -> GapReport:
    as_of = as_of or datetime.now(timezone.utc).date()
    expected = con.execute(_EXPECTED_COUNT_QUERY, [as_of]).fetchone()[0]
    gaps = con.execute(_GAP_QUERY, [as_of]).fetchall()
    return GapReport(expected_business_days=expected, gaps=gaps)


def print_report(report: GapReport) -> None:
    print(
        f"\nHistorical coverage: {report.clean_business_days}/{report.expected_business_days} "
        f"business-days ({report.coverage_pct:.1f}%)"
    )
    for ticker, days in sorted(report.gaps_by_ticker.items()):
        preview = ", ".join(d.isoformat() for d in days[:5])
        more = f" (+{len(days) - 5} more)" if len(days) > 5 else ""
        print(f"  - {ticker}: {len(days)} gap day(s) — {preview}{more}")


def main() -> None:
    con = db.connect()
    try:
        report = find_gaps(con)
    finally:
        con.close()
    print_report(report)


if __name__ == "__main__":
    main()
