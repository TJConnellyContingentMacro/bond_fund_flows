"""Backfills fund_daily from issuer-published daily history (iShares, SSGA; see
sources/history.py). run_pipeline.py also runs it daily for SSGA with a short
window, which keeps SSGA share counts exact and refills any missed days.

Usage:
    python scripts/backfill_history.py                         # both issuers, last 62 days
    python scripts/backfill_history.py --issuers SSGA --days 10
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bondflows import db  # noqa: E402
from bondflows.sources.history import USER_AGENT, fetch_ishares_history, fetch_ssga_history  # noqa: E402
from bondflows.universe import load_universe  # noqa: E402

RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"
SUPPORTED_ISSUERS = ("iShares", "SSGA")
REQUEST_DELAY_SECONDS = 1.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issuers", nargs="+", default=list(SUPPORTED_ISSUERS), choices=SUPPORTED_ISSUERS)
    parser.add_argument("--days", type=int, default=62, help="calendar days of history to load")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    now = datetime.now(timezone.utc)
    cutoff = now.date() - timedelta(days=args.days)
    funds = [f for f in load_universe() if f.issuer in args.issuers]
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    con = db.connect()
    inserted = updated = 0
    failed: list[str] = []
    try:
        for i, fund in enumerate(funds):
            if i:
                time.sleep(REQUEST_DELAY_SECONDS)
            try:
                if fund.issuer == "iShares":
                    sheet, rows = fetch_ishares_history(fund.issuer_id, session)
                    raw, ext = sheet.encode("utf-8"), "xml"
                else:
                    raw, rows = fetch_ssga_history(fund.ticker, session)
                    ext = "xlsx"
            except Exception as exc:  # noqa: BLE001 - one fund must not stop the rest
                logging.error("%s %s: %s", fund.issuer, fund.ticker, exc)
                failed.append(fund.ticker)
                continue

            _write_raw(now.date(), fund.issuer, fund.ticker, ext, raw)
            for row in rows:
                if row.asof_date < cutoff:
                    continue
                if db.apply_history_row(con, fund.ticker, row, source=f"{fund.issuer} history", retrieved_at=now):
                    inserted += 1
                else:
                    updated += 1
    finally:
        con.close()

    print(f"History since {cutoff}: {inserted} rows inserted, {updated} existing rows updated, "
          f"{len(funds) - len(failed)}/{len(funds)} funds loaded.")
    if failed:
        print(f"Failed: {', '.join(failed)}")
        sys.exit(1)


def _write_raw(run_date: date, issuer: str, ticker: str, ext: str, content: bytes) -> None:
    day_dir = RAW_DIR / f"{run_date:%Y}" / f"{run_date:%m}" / f"{run_date:%d}" / f"{issuer}-history"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / f"{ticker}.{ext}").write_bytes(content)


if __name__ == "__main__":
    main()
