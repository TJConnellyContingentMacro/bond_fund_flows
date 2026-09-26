"""Fetches ICI's weekly estimated flows release (SPEC.md §7) and upserts
ici_weekly_flows. Posted weekly by ICI (not daily) — safe to run daily
anyway, since a week with no new data just re-upserts the same rows.

Usage:
    python scripts/fetch_ici_weekly.py
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bondflows import db  # noqa: E402
from bondflows.sources.ici import fetch_ici_weekly  # noqa: E402

RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    run_date = datetime.now(timezone.utc).date()

    result = fetch_ici_weekly()
    if result.raw_content is not None:
        _write_raw(run_date, result.source_file, result.raw_content)

    if result.error is not None:
        logging.error("ICI: %s", result.error)
        print("Wrote 0 ici_weekly_flows rows.")
        return

    con = db.connect()
    try:
        retrieved_at = datetime.now(timezone.utc)
        for row in result.rows:
            db.upsert_ici_weekly_flow(con, {**row, "retrieved_at": retrieved_at, "source_file": result.source_file})
    finally:
        con.close()

    print(f"Wrote {len(result.rows)} ici_weekly_flows rows.")


def _write_raw(run_date: date, source_file: str, content: bytes) -> None:
    day_dir = RAW_DIR / f"{run_date:%Y}" / f"{run_date:%m}" / f"{run_date:%d}" / "ici"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / source_file).write_bytes(content)


if __name__ == "__main__":
    main()
