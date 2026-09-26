"""Recomputes mbs_implied_flows from fund_flows + mbs_weights (SPEC.md
§6.6, narrowly scoped — see mbs.py). Run after scripts/compute_flows.py and
scripts/fetch_mbs_weights.py.

Usage:
    python scripts/compute_mbs_flows.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bondflows import db  # noqa: E402
from bondflows.mbs import compute_mbs_implied_flows  # noqa: E402


def main() -> None:
    con = db.connect()
    try:
        total = compute_mbs_implied_flows(con)
    finally:
        con.close()
    print(f"Wrote {total} mbs_implied_flows rows.")


if __name__ == "__main__":
    main()
