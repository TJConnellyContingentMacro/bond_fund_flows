"""One-time: record a raw_fingerprints row for every adapter raw file already
under data/raw/, so staleness checks keep working once the raw files are no
longer on disk (the cloud run). Safe to re-run; it upserts.

Usage:
    python scripts/seed_raw_fingerprints.py
"""

from __future__ import annotations

import hashlib
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bondflows import db  # noqa: E402
from bondflows.ingest import RAW_DIR  # noqa: E402
from bondflows.universe import load_universe  # noqa: E402


def main() -> None:
    issuers = {f.issuer for f in load_universe()}
    con = db.connect()
    n = 0
    for path in sorted(RAW_DIR.glob("*/*/*/*/*.*")):
        issuer = path.parent.name
        if issuer not in issuers:
            continue
        run_date = date(int(path.parents[3].name), int(path.parents[2].name), int(path.parents[1].name))
        db.record_raw_fingerprint(
            con, issuer, path.stem, path.suffix.lstrip("."), run_date,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        n += 1
    con.close()
    print(f"Recorded {n} raw fingerprints")


if __name__ == "__main__":
    main()
