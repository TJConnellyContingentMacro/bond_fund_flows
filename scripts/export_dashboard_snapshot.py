"""Exports a JSON snapshot of everything the Bondflows Monitor dashboard
needs, for the cloud dashboard-refresh routine to read after this repo is
pushed. SPEC.md §10: "Connect the folder holding data/ so this session can
read the DuckDB output directly" — the cloud routine can't do that (no local
file access), so this is the deterministic bridge: all the actual numbers
are computed here, in tested Python, against the real DuckDB file. The
cloud routine's job is limited to writing commentary on what this snapshot
shows and updating the dashboard's HTML — never re-deriving figures itself.

Usage:
    python scripts/export_dashboard_snapshot.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bondflows import db  # noqa: E402
from bondflows.universe import load_universe  # noqa: E402

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "snapshot.json"

# Mirrors run_pipeline.py's alert thresholds (SPEC.md §8) — duplicated
# rather than imported to keep this script runnable standalone; if these
# drift out of sync, that's a sign to factor them into a shared module.
COVERAGE_ALERT_THRESHOLD_PCT = 90.0
OGR_ALERT_THRESHOLD = Decimal("0.10")
ZSCORE_ALERT_THRESHOLD = Decimal("4")


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"not JSON serializable: {value!r}")


def build_snapshot(con) -> dict:
    funds = load_universe()
    in_core = [f for f in funds if f.in_core]
    # "Ever covered" (has a working adapter at all) — the static Coverage
    # panel's figure, matches the dashboard's original semantics.
    covered_tickers = {
        r[0] for r in con.execute("SELECT DISTINCT ticker FROM fund_daily").fetchall()
    }
    # "Covered today" — a ticker actually fetched on this run, the figure
    # that matters for alerting (an issuer's adapter going quiet for a day
    # doesn't show up in "ever covered" once it's succeeded once).
    covered_today = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT ticker FROM fund_daily WHERE CAST(retrieved_at AS DATE) = CURRENT_DATE"
        ).fetchall()
    }
    by_issuer: dict[str, dict] = {}
    for f in in_core:
        entry = by_issuer.setdefault(f.issuer, {"expected": 0, "covered": 0, "covered_today": 0})
        entry["expected"] += 1
        if f.ticker in covered_tickers:
            entry["covered"] += 1
        if f.ticker in covered_today:
            entry["covered_today"] += 1
    total_expected = len(in_core)
    total_covered = sum(1 for f in in_core if f.ticker in covered_tickers)
    total_covered_today = sum(1 for f in in_core if f.ticker in covered_today)

    latest_fund_daily = con.execute("SELECT MAX(asof_date), MAX(retrieved_at) FROM fund_daily").fetchone()

    flow_row_count = con.execute("SELECT COUNT(*) FROM fund_flows").fetchone()[0]
    latest_aggregates = []
    latest_flow_date = None
    if flow_row_count > 0:
        latest_flow_date = con.execute("SELECT MAX(asof_date) FROM flow_aggregates").fetchone()[0]
        if latest_flow_date is not None:
            cols = [
                "cut", "flow_usd", "flow_usd_net", "ogr", "flow_5d", "flow_20d",
                "zscore_252d", "has_imputed_or_suspect", "n_funds",
            ]
            rows = con.execute(
                f"SELECT {', '.join(cols)} FROM flow_aggregates WHERE asof_date = ? "
                "AND cut IN ('total','ex_bills','ex_bills_ex_muni','credit','rates') ORDER BY cut",
                [latest_flow_date],
            ).fetchall()
            latest_aggregates = [dict(zip(cols, r)) for r in rows]

    ici_weekly = [
        dict(zip(
            ["week_ended", "total_ltf_and_etf", "bond_total", "bond_taxable", "bond_municipal"],
            r,
        ))
        for r in con.execute(
            "SELECT week_ended, total_ltf_and_etf, bond_total, bond_taxable, bond_municipal "
            "FROM ici_weekly_flows ORDER BY week_ended"
        ).fetchall()
    ]

    overlay_holdings = [
        dict(zip(["ticker", "asof_date", "underlying_ticker", "weight_pct"], r))
        for r in con.execute(
            "SELECT ticker, asof_date, underlying_ticker, weight_pct FROM overlay_holdings ORDER BY ticker"
        ).fetchall()
    ]

    mbs_weights = [
        dict(zip(["ticker", "asof_date", "source", "mbs_weight_pct"], r))
        for r in con.execute(
            "SELECT ticker, asof_date, source, mbs_weight_pct FROM mbs_weights ORDER BY mbs_weight_pct DESC"
        ).fetchall()
    ]

    notable: list[str] = []
    if total_expected and 100 * total_covered_today / total_expected < COVERAGE_ALERT_THRESHOLD_PCT:
        notable.append(
            f"Today's coverage {total_covered_today}/{total_expected} "
            f"({100 * total_covered_today / total_expected:.1f}%) is below the "
            f"{COVERAGE_ALERT_THRESHOLD_PCT}% threshold"
        )
    if latest_flow_date is not None:
        for ticker, ogr in con.execute(
            "SELECT ticker, organic_growth_rate FROM fund_flows WHERE flow_date = ? "
            "AND organic_growth_rate IS NOT NULL AND ABS(organic_growth_rate) > ? "
            "ORDER BY ABS(organic_growth_rate) DESC",
            [latest_flow_date, OGR_ALERT_THRESHOLD],
        ).fetchall():
            notable.append(f"{ticker}: |OGR| = {float(ogr) * 100:.1f}% on {latest_flow_date}")
        for cut, z in con.execute(
            "SELECT cut, zscore_252d FROM flow_aggregates WHERE asof_date = ? "
            "AND zscore_252d IS NOT NULL AND ABS(zscore_252d) > ? ORDER BY ABS(zscore_252d) DESC",
            [latest_flow_date, ZSCORE_ALERT_THRESHOLD],
        ).fetchall():
            notable.append(f"{cut}: z-score {float(z):.2f} on {latest_flow_date}")
    suspect_count = con.execute("SELECT COUNT(*) FROM fund_flows WHERE flag = 'suspect'").fetchone()[0]
    if suspect_count:
        notable.append(f"{suspect_count} split candidate(s) awaiting review in splits.csv")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "coverage": {
            "total_expected": total_expected,
            "total_covered": total_covered,
            "total_covered_today": total_covered_today,
            "by_issuer": by_issuer,
            "latest_fund_daily_asof_date": latest_fund_daily[0],
            "latest_fund_daily_retrieved_at": latest_fund_daily[1],
        },
        "flows": {
            "available": flow_row_count > 0 and bool(latest_aggregates),
            "as_of_date": latest_flow_date,
            "aggregates": latest_aggregates,
        },
        "ici_weekly": ici_weekly,
        "overlay_holdings": overlay_holdings,
        "mbs_weights": mbs_weights,
        "notable": notable,
    }


def main() -> None:
    con = db.connect()
    try:
        snapshot = build_snapshot(con)
    finally:
        con.close()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(snapshot, indent=2, default=_json_default), encoding="utf-8")
    print(f"Wrote snapshot to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
