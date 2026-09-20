"""Loads universe.csv — the ticker list, sleeve taxonomy, and per-issuer fetch
identifiers, all version-controlled together."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

UNIVERSE_CSV = Path(__file__).resolve().parents[2] / "universe.csv"


@dataclass(frozen=True)
class Fund:
    ticker: str
    name: str
    issuer: str
    sleeve: str
    sub_sleeve: str
    in_core: bool
    confidence: str
    issuer_id: str | None  # CUSIP / internal fund ID / portfolioId, if needed
    product_url: str | None  # resolved fund-page URL, if the slug isn't derivable
    notes: str


def load_universe(path: Path = UNIVERSE_CSV) -> list[Fund]:
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [
            Fund(
                ticker=row["ticker"],
                name=row["name"],
                issuer=row["issuer"],
                sleeve=row["sleeve"],
                sub_sleeve=row["sub_sleeve"],
                in_core=row["in_core"].strip().upper() == "TRUE",
                confidence=row["confidence"],
                issuer_id=row["issuer_id"] or None,
                product_url=row["product_url"] or None,
                notes=row["notes"],
            )
            for row in reader
        ]


def by_issuer(funds: list[Fund]) -> dict[str, list[Fund]]:
    grouped: dict[str, list[Fund]] = {}
    for fund in funds:
        grouped.setdefault(fund.issuer, []).append(fund)
    return grouped
