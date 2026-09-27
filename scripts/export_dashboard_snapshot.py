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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bondflows import db  # noqa: E402
from bondflows.analytics import _build_ticker_cuts  # noqa: E402
from bondflows.universe import load_universe  # noqa: E402

HEADLINE_CUTS = ("total", "ex_bills", "ex_bills_ex_muni", "credit", "rates", "mixed_other")
# Longest ordinary gap between trading days: a weekend plus a Monday or Friday holiday.
PANEL_MAX_GAP_DAYS = 4
SLEEVE_WINDOW_WEEKS = 4

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


def _sum(rows, key) -> Decimal:
    return sum((r[key] or 0 for r in rows), Decimal(0))


def _flow_views(con, funds, covered_tickers: set[str]) -> dict:
    """Weekly view over in-core funds. A flow spanning more than
    PANEL_MAX_GAP_DAYS is a catch-up across missed observations: it's listed
    separately rather than landing in one week's bar."""
    in_core = {f.ticker: f for f in funds if f.in_core}
    cuts_by_ticker: dict[str, set[str]] = {}
    for ticker, cut in _build_ticker_cuts(funds):
        cuts_by_ticker.setdefault(ticker, set()).add(cut)

    cols = [
        "ticker", "prior_date", "flow_date", "flow_usd", "organic_growth_rate", "dv01_usd_per_bp",
        "spread_dv01_usd_per_bp", "spread_dv01_is_proxied", "flag", "prior_tna",
    ]
    rows = con.execute(
        """
        WITH usable AS (
            SELECT ticker, asof_date,
                   LAG(asof_date) OVER (PARTITION BY ticker ORDER BY asof_date) AS prior_date,
                   LAG(total_net_assets) OVER (PARTITION BY ticker ORDER BY asof_date) AS prior_tna
            FROM fund_daily
            WHERE NOT source_is_stale AND shares_outstanding IS NOT NULL AND nav_per_share IS NOT NULL
        )
        SELECT f.ticker, u.prior_date, f.flow_date, f.flow_usd, f.organic_growth_rate, f.dv01_usd_per_bp,
               f.spread_dv01_usd_per_bp, f.spread_dv01_is_proxied, f.flag, u.prior_tna
        FROM fund_flows f
        LEFT JOIN usable u ON u.ticker = f.ticker AND u.asof_date = f.flow_date
        WHERE f.flow_usd IS NOT NULL
        ORDER BY f.flow_date
        """
    ).fetchall()

    panel: list[dict] = []
    latest_by_ticker: dict[str, dict] = {}
    for values in rows:
        r = dict(zip(cols, values))
        fund = in_core.get(r["ticker"])
        if fund is None:
            continue
        r["issuer"], r["sleeve"] = fund.issuer, fund.sleeve
        r["in_panel"] = r["prior_date"] is not None and (r["flow_date"] - r["prior_date"]).days <= PANEL_MAX_GAP_DAYS
        if r["in_panel"]:
            r["week_start"] = r["flow_date"] - timedelta(days=r["flow_date"].weekday())
            panel.append(r)
        latest_by_ticker[r["ticker"]] = r

    week_starts = sorted({r["week_start"] for r in panel})
    weekly = []
    for ws in week_starts:
        week_rows = [r for r in panel if r["week_start"] == ws]
        entry = {"week_start": ws, "week_end": ws + timedelta(days=4), "n_funds": len({r["ticker"] for r in week_rows})}
        for cut in HEADLINE_CUTS:
            entry[cut] = _sum((r for r in week_rows if cut in cuts_by_ticker.get(r["ticker"], ())), "flow_usd")
        weekly.append(entry)

    latest_week = None
    if weekly:
        ws = week_starts[-1]
        lw = [r for r in panel if r["week_start"] == ws]
        prior = weekly[-2] if len(weekly) >= 2 else None
        headline = []
        for cut in HEADLINE_CUTS:
            members = [r for r in lw if cut in cuts_by_ticker.get(r["ticker"], ())]
            if members:
                headline.append({
                    "cut": cut,
                    "flow_usd": _sum(members, "flow_usd"),
                    "prior_week_flow_usd": prior[cut] if prior else None,
                    "dv01_usd_per_bp": _sum(members, "dv01_usd_per_bp"),
                    "spread_dv01_usd_per_bp": _sum(members, "spread_dv01_usd_per_bp"),
                    "has_proxied_spread_dv01": any(bool(r["spread_dv01_is_proxied"]) for r in members),
                    "n_funds": len({r["ticker"] for r in members}),
                })
        per_fund: dict[str, dict] = {}
        for r in lw:
            p = per_fund.setdefault(r["ticker"], {
                "ticker": r["ticker"], "issuer": r["issuer"], "sleeve": r["sleeve"], "flow_usd": Decimal(0),
                "days": 0, "start_tna": r["prior_tna"], "from_date": r["prior_date"], "to_date": r["flow_date"],
            })
            p["flow_usd"] += r["flow_usd"]
            p["days"] += 1
            p["to_date"] = r["flow_date"]
        for p in per_fund.values():
            p["ogr"] = p["flow_usd"] / p["start_tna"] if p["start_tna"] else None
        by_fund = sorted(per_fund.values(), key=lambda p: abs(p["flow_usd"]), reverse=True)
        total = next((h["flow_usd"] for h in headline if h["cut"] == "total"), None)
        latest_week = {
            "week_start": ws,
            "week_end": ws + timedelta(days=4),
            "headline": headline,
            "by_fund": by_fund,
            "last_date_by_issuer": {
                issuer: max(r["flow_date"] for r in lw if r["issuer"] == issuer) for issuer in {r["issuer"] for r in lw}
            },
            "largest_contributor": (
                {"ticker": by_fund[0]["ticker"], "flow_usd": by_fund[0]["flow_usd"],
                 "total_ex_largest": total - by_fund[0]["flow_usd"]}
                if by_fund and total is not None else None
            ),
        }

    recent = set(week_starts[-SLEEVE_WINDOW_WEEKS:])
    sleeve_rows: dict[str, list[dict]] = {}
    for r in panel:
        if r["week_start"] in recent:
            sleeve_rows.setdefault(r["sleeve"], []).append(r)
    by_sleeve = sorted(
        ({"cut": s, "flow_usd": _sum(rs, "flow_usd"), "n_funds": len({r["ticker"] for r in rs})}
         for s, rs in sleeve_rows.items()),
        key=lambda t: t["flow_usd"], reverse=True,
    )

    catch_up = sorted(
        (r for r in latest_by_ticker.values() if not r["in_panel"]),
        key=lambda r: abs(r["flow_usd"]), reverse=True,
    )
    latest_ws = week_starts[-1] if week_starts else None
    large_ogr = [
        r for r in panel + catch_up
        if r["organic_growth_rate"] is not None and abs(r["organic_growth_rate"]) > OGR_ALERT_THRESHOLD
        and (not r["in_panel"] or r["week_start"] == latest_ws)
    ]

    implied_mbs = [
        dict(zip(["ticker", "flow_date", "implied_mbs_flow_usd", "flow_usd", "mbs_weight_pct", "weight_date"], r))
        for r in con.execute(
            """
            SELECT m.ticker, m.flow_date, m.implied_mbs_flow_usd, f.flow_usd, w.mbs_weight_pct, w.asof_date
            FROM mbs_implied_flows m
            JOIN fund_flows f ON f.ticker = m.ticker AND f.flow_date = m.flow_date
            ASOF JOIN mbs_weights w ON w.ticker = m.ticker AND m.flow_date > w.asof_date
            QUALIFY ROW_NUMBER() OVER (PARTITION BY m.ticker ORDER BY m.flow_date DESC) = 1
            ORDER BY ABS(m.implied_mbs_flow_usd) DESC
            """
        ).fetchall()
    ]

    obs = {
        r[0]: (r[1], r[2])
        for r in con.execute(
            """
            SELECT ticker,
                   COUNT(*) FILTER (WHERE NOT source_is_stale),
                   COUNT(*) FILTER (WHERE NOT source_is_stale AND shares_outstanding IS NOT NULL
                                    AND nav_per_share IS NOT NULL)
            FROM fund_daily GROUP BY ticker
            """
        ).fetchall()
    }
    missing: dict[tuple[str, str], list[str]] = {}
    for ticker, fund in sorted(in_core.items()):
        if ticker not in covered_tickers or ticker in latest_by_ticker:
            continue
        clean_obs, usable_obs = obs.get(ticker, (0, 0))
        if clean_obs >= 2 and usable_obs < 2:
            reason = "no shares outstanding published on two dates yet"
        else:
            reason = "only one clean observation so far"
        missing.setdefault((fund.issuer, reason), []).append(ticker)

    strip = ("in_panel", "week_start", "prior_tna")
    return {
        "available": bool(latest_by_ticker),
        "panel_max_gap_days": PANEL_MAX_GAP_DAYS,
        "history_start": panel[0]["flow_date"] if panel else None,
        "history_end": panel[-1]["flow_date"] if panel else None,
        "n_funds_weekly": len({r["ticker"] for r in panel}),
        "weekly": weekly,
        "latest_week": latest_week,
        "sleeves_recent": {"weeks": len(recent), "start": min(recent) if recent else None, "by_sleeve": by_sleeve},
        "catch_up": [{k: v for k, v in r.items() if k not in strip} for r in catch_up],
        "large_ogr": [
            {"ticker": r["ticker"], "ogr": r["organic_growth_rate"], "prior_date": r["prior_date"],
             "flow_date": r["flow_date"], "catch_up": not r["in_panel"]}
            for r in large_ogr
        ],
        "implied_mbs": implied_mbs,
        "implied_mbs_total": _sum(implied_mbs, "implied_mbs_flow_usd"),
        "missing": [
            {"issuer": issuer, "reason": reason, "tickers": tickers}
            for (issuer, reason), tickers in sorted(missing.items(), key=lambda kv: -len(kv[1]))
        ],
    }


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

    flows = _flow_views(con, funds, covered_tickers)

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

    # Only the most recent row per ticker — both tables accumulate history
    # over time (e.g. overlay_holdings now has 2 dates for HYGW/LQDW), but
    # the dashboard's tables show a current snapshot, not a time series.
    overlay_holdings = [
        dict(zip(["ticker", "asof_date", "underlying_ticker", "weight_pct"], r))
        for r in con.execute(
            """
            SELECT o.ticker, o.asof_date, o.underlying_ticker, o.weight_pct
            FROM overlay_holdings o
            INNER JOIN (SELECT ticker, MAX(asof_date) AS max_date FROM overlay_holdings GROUP BY ticker) latest
              ON latest.ticker = o.ticker AND latest.max_date = o.asof_date
            ORDER BY o.ticker
            """
        ).fetchall()
    ]

    mbs_weights = [
        dict(zip(["ticker", "asof_date", "source", "mbs_weight_pct"], r))
        for r in con.execute(
            """
            SELECT m.ticker, m.asof_date, m.source, m.mbs_weight_pct
            FROM mbs_weights m
            INNER JOIN (SELECT ticker, MAX(asof_date) AS max_date FROM mbs_weights GROUP BY ticker) latest
              ON latest.ticker = m.ticker AND latest.max_date = m.asof_date
            ORDER BY m.mbs_weight_pct DESC
            """
        ).fetchall()
    ]

    notable: list[str] = []
    if total_expected and 100 * total_covered_today / total_expected < COVERAGE_ALERT_THRESHOLD_PCT:
        notable.append(
            f"Today's coverage {total_covered_today}/{total_expected} "
            f"({100 * total_covered_today / total_expected:.1f}%) is below the "
            f"{COVERAGE_ALERT_THRESHOLD_PCT}% threshold"
        )
    for r in flows["large_ogr"]:
        period = f"over {r['prior_date']} to {r['flow_date']} (catch-up)" if r["catch_up"] else f"on {r['flow_date']}"
        notable.append(f"{r['ticker']}: OGR {float(r['ogr']) * 100:+.1f}% {period}")
    latest_agg_date = con.execute("SELECT MAX(asof_date) FROM flow_aggregates").fetchone()[0]
    if latest_agg_date is not None:
        for cut, z in con.execute(
            "SELECT cut, zscore_252d FROM flow_aggregates WHERE asof_date = ? "
            "AND NOT has_imputed_or_suspect AND zscore_252d IS NOT NULL AND ABS(zscore_252d) > ? "
            "ORDER BY ABS(zscore_252d) DESC",
            [latest_agg_date, ZSCORE_ALERT_THRESHOLD],
        ).fetchall():
            notable.append(f"{cut}: z-score {float(z):.2f} on {latest_agg_date}")
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
        "flows": flows,
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
