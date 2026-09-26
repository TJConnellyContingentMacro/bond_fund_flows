"""Fetches agency-MBS weights (SPEC.md §6.6, narrowly scoped — see mbs.py's
docstring) for the 6 in-scope tickers and writes mbs_weights. Run
scripts/compute_flows.py first, then this, then scripts/compute_mbs_flows.py
for implied_mbs_flow to use that day's weight.

Usage:
    python scripts/fetch_mbs_weights.py
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bondflows import db  # noqa: E402
from bondflows.mbs import (  # noqa: E402
    ISHARES_MBS_TICKERS,
    SSGA_MBS_TICKERS,
    fetch_ishares_mbs_weight,
    fetch_ssga_mbs_weight,
)
from bondflows.universe import load_universe  # noqa: E402

RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    funds_by_ticker = {f.ticker: f for f in load_universe()}
    run_date = datetime.now(timezone.utc).date()

    con = db.connect()
    written = 0
    total = len(ISHARES_MBS_TICKERS) + len(SSGA_MBS_TICKERS)
    try:
        for ticker in sorted(ISHARES_MBS_TICKERS):
            fund = funds_by_ticker.get(ticker)
            if fund is None or not fund.product_url:
                logging.warning("%s: no product_url in universe.csv", ticker)
                continue
            result = fetch_ishares_mbs_weight(ticker, fund.product_url)
            written += _handle_result(con, run_date, "iShares", result)

        for ticker in sorted(SSGA_MBS_TICKERS):
            result = fetch_ssga_mbs_weight(ticker)
            written += _handle_result(con, run_date, "SSGA", result)
    finally:
        con.close()

    print(f"Wrote {written}/{total} mbs_weights rows.")


def _handle_result(con, run_date: date, source: str, result) -> int:
    if result.error is not None:
        logging.error("%s: %s", result.ticker, result.error)
        return 0
    if result.raw_content is not None:
        _write_raw(run_date, source, result.ticker, result.raw_content)
    db.upsert_mbs_weight(
        con,
        {
            "ticker": result.ticker,
            "asof_date": result.asof_date,
            "mbs_weight_pct": result.mbs_weight_pct,
            "source": source,
        },
    )
    return 1


def _write_raw(run_date: date, source: str, ticker: str, content: bytes) -> None:
    ext = "csv" if source == "iShares" else "xlsx"
    day_dir = RAW_DIR / f"{run_date:%Y}" / f"{run_date:%m}" / f"{run_date:%d}" / "mbs_weights"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / f"{ticker}.{ext}").write_bytes(content)


if __name__ == "__main__":
    main()
