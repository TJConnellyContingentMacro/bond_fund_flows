"""Recomputes flow_aggregates from fund_flows/fund_daily (SPEC.md §6). Safe
to rerun any time. Run after scripts/compute_flows.py.

Usage:
    python scripts/compute_analytics.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bondflows import db  # noqa: E402
from bondflows.analytics import compute_aggregates  # noqa: E402


def main() -> None:
    con = db.connect()
    try:
        total = compute_aggregates(con)
    finally:
        con.close()
    print(f"Wrote {total} flow_aggregates rows.")


if __name__ == "__main__":
    main()
