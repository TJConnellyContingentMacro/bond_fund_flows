"""Fetches the option-overlay funds' underlying-ETF weight (SPEC.md §6.2,
narrowly scoped — see overlay.py's docstring) and writes overlay_holdings.
Raw CSVs land under data/raw/ alongside the rest of the pipeline's raw
snapshots, same append-only convention.

Usage:
    python scripts/fetch_overlay_holdings.py
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bondflows import db  # noqa: E402
from bondflows.overlay import OVERLAY_FUNDS, fetch_overlay_holding  # noqa: E402
from bondflows.universe import load_universe  # noqa: E402

RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    funds_by_ticker = {f.ticker: f for f in load_universe()}
    run_date = datetime.now(timezone.utc).date()

    con = db.connect()
    written = 0
    try:
        for ticker in OVERLAY_FUNDS:
            fund = funds_by_ticker.get(ticker)
            if fund is None or not fund.product_url:
                logging.warning("%s: no product_url in universe.csv", ticker)
                continue

            result = fetch_overlay_holding(ticker, fund.product_url)
            if result.error is not None:
                logging.error("%s: %s", ticker, result.error)
                continue

            if result.raw_content is not None:
                _write_raw(run_date, ticker, result.raw_content)

            db.upsert_overlay_holding(
                con,
                {
                    "ticker": result.ticker,
                    "asof_date": result.asof_date,
                    "underlying_ticker": result.underlying_ticker,
                    "weight_pct": result.weight_pct,
                    "source": "iShares",
                },
            )
            written += 1
    finally:
        con.close()

    print(f"Wrote {written}/{len(OVERLAY_FUNDS)} overlay_holdings rows.")


def _write_raw(run_date: date, ticker: str, content: bytes) -> None:
    day_dir = RAW_DIR / f"{run_date:%Y}" / f"{run_date:%m}" / f"{run_date:%d}" / "overlay_holdings"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / f"{ticker}.csv").write_bytes(content)


if __name__ == "__main__":
    main()
