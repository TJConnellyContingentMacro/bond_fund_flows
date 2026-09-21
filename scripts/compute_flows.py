"""Recomputes fund_flows from fund_daily (SPEC.md §5). Safe to rerun any
time — e.g. after confirming a new entry in splits.csv.

Usage:
    python scripts/compute_flows.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bondflows import db  # noqa: E402
from bondflows.flows import compute_all_flows  # noqa: E402


def main() -> None:
    con = db.connect()
    try:
        total = compute_all_flows(con)
    finally:
        con.close()
    print(f"Wrote {total} fund_flows rows.")


if __name__ == "__main__":
    main()
