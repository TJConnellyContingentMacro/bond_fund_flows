"""Single daily entrypoint, SPEC.md §8 ("Operations"). Runs every pipeline
script in order, logs each step's output to logs/, and prints a fail-loudly
alert summary at the end. Exits non-zero if anything needs attention, so a
scheduler (Windows Task Scheduler, cron) can surface a failed run instead of
silently swallowing it.

**Schwab needs a visible desktop.** Its adapter (called from daily.py)
launches a real, headed Chrome window — confirmed during discovery as the
only way past Akamai's bot-blocking. This script must run in an interactive
desktop session, not a headless service account.

Usage:
    python scripts/run_pipeline.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bondflows import db  # noqa: E402

LOG_DIR = PROJECT_ROOT / "logs"

# SPEC.md §8: "Alert on: adapter exception, coverage below threshold, any
# fund with |OGR| > 10% in a day, any sleeve z-score beyond ±4, split
# candidates awaiting review." The coverage threshold itself isn't specified
# in SPEC.md — 90% is a starting point, not a figure derived from anything;
# revisit once real daily coverage variance is observed.
COVERAGE_ALERT_THRESHOLD_PCT = 90.0
OGR_ALERT_THRESHOLD = Decimal("0.10")
ZSCORE_ALERT_THRESHOLD = Decimal("4")

STEPS: list[tuple[str, str]] = [
    ("backfill_gaps", "scripts/backfill_gaps.py"),
    ("daily", "scripts/daily.py"),
    # SSGA's live page rounds shares to 10,000; its history file is exact and refills missed days.
    ("ssga_history", "scripts/backfill_history.py --issuers SSGA --days 10"),
    ("compute_flows", "scripts/compute_flows.py"),
    ("compute_analytics", "scripts/compute_analytics.py"),
    ("fetch_overlay_holdings", "scripts/fetch_overlay_holdings.py"),
    ("fetch_mbs_weights", "scripts/fetch_mbs_weights.py"),
    ("compute_mbs_flows", "scripts/compute_mbs_flows.py"),
    ("fetch_ici_weekly", "scripts/fetch_ici_weekly.py"),
    ("export_dashboard_snapshot", "scripts/export_dashboard_snapshot.py"),
]

_COVERAGE_RE = re.compile(r"Coverage:\s*(\d+)/(\d+)\s*\(([\d.]+)%\)")


def _run_step(name: str, script_path: str, log_lines: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / script_path.split()[0]), *script_path.split()[1:]],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    log_lines.append(f"\n===== {name} — {status} =====")
    log_lines.append(output.rstrip())
    print(f"[{name}] {status}")
    return result.returncode, output


def _check_alerts(con, run_date: date, daily_output: str) -> list[str]:
    alerts: list[str] = []

    coverage_match = _COVERAGE_RE.search(daily_output)
    if coverage_match:
        fetched, expected, pct = coverage_match.groups()
        if float(pct) < COVERAGE_ALERT_THRESHOLD_PCT:
            alerts.append(f"Coverage {fetched}/{expected} ({pct}%) is below the {COVERAGE_ALERT_THRESHOLD_PCT}% threshold")
    else:
        alerts.append("Could not find a 'Coverage: X/Y (Z%)' line in daily.py's output")

    ogr_rows = con.execute(
        """
        SELECT ticker, organic_growth_rate FROM fund_flows
        WHERE flow_date = ? AND organic_growth_rate IS NOT NULL
          AND ABS(organic_growth_rate) > ?
        ORDER BY ABS(organic_growth_rate) DESC
        """,
        [run_date, OGR_ALERT_THRESHOLD],
    ).fetchall()
    for ticker, ogr in ogr_rows:
        alerts.append(f"{ticker}: |OGR| = {float(ogr) * 100:.1f}% on {run_date} (> {float(OGR_ALERT_THRESHOLD) * 100:.0f}% threshold)")

    zscore_rows = con.execute(
        """
        SELECT cut, zscore_252d FROM flow_aggregates
        WHERE asof_date = ? AND zscore_252d IS NOT NULL AND ABS(zscore_252d) > ?
        ORDER BY ABS(zscore_252d) DESC
        """,
        [run_date, ZSCORE_ALERT_THRESHOLD],
    ).fetchall()
    for cut, z in zscore_rows:
        alerts.append(f"{cut}: z-score {float(z):.2f} on {run_date} (beyond ±{ZSCORE_ALERT_THRESHOLD})")

    suspect_rows = con.execute(
        "SELECT ticker, flow_date FROM fund_flows WHERE flag = 'suspect' ORDER BY flow_date DESC"
    ).fetchall()
    for ticker, flow_date in suspect_rows:
        alerts.append(f"{ticker}: split candidate awaiting review on {flow_date} — confirm or reject in splits.csv")

    return alerts


def _push_snapshot(run_date: date, log_lines: list[str]) -> None:
    """Pushes dashboard/snapshot.json to GitHub so the cloud dashboard-
    refresh routine (which has no local file access) can read it. No-ops
    cleanly if nothing changed since the last push."""
    subprocess.run(["git", "add", "dashboard/snapshot.json"], cwd=PROJECT_ROOT, check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=PROJECT_ROOT)
    if diff.returncode == 0:
        log_lines.append("\n===== push_snapshot — no changes to push =====")
        print("[push_snapshot] no changes")
        return

    subprocess.run(
        ["git", "commit", "-m", f"Automated: refresh dashboard snapshot ({run_date.isoformat()})"],
        cwd=PROJECT_ROOT,
        check=True,
    )
    push = subprocess.run(["git", "push"], cwd=PROJECT_ROOT, capture_output=True, text=True)
    status = "OK" if push.returncode == 0 else f"FAILED (exit {push.returncode})"
    log_lines.append(f"\n===== push_snapshot — {status} =====\n{(push.stdout + push.stderr).rstrip()}")
    print(f"[push_snapshot] {status}")
    if push.returncode != 0:
        raise RuntimeError("git push failed — see logs/")


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    run_date = datetime.now(timezone.utc).date()
    log_lines: list[str] = [f"bondflows pipeline run — {datetime.now(timezone.utc).isoformat()}"]

    step_outputs: dict[str, str] = {}
    failed_steps: list[str] = []
    for name, script_path in STEPS:
        returncode, output = _run_step(name, script_path, log_lines)
        step_outputs[name] = output
        if returncode != 0:
            failed_steps.append(name)

    con = db.connect()
    try:
        alerts = _check_alerts(con, run_date, step_outputs.get("daily", ""))
    finally:
        con.close()

    for name in failed_steps:
        alerts.insert(0, f"Step '{name}' exited non-zero — see logs/ for its output")

    if "export_dashboard_snapshot" not in failed_steps:
        try:
            _push_snapshot(run_date, log_lines)
        except Exception as exc:  # noqa: BLE001 - a push failure is an alert, not a crash
            alerts.append(f"Failed to push dashboard snapshot to GitHub: {exc}")

    log_path = LOG_DIR / f"pipeline_{run_date:%Y%m%d}.log"
    log_path.write_text("\n".join(log_lines), encoding="utf-8")

    print(f"\nLog written to {log_path}")
    if alerts:
        print("\nALERTS:")
        for alert in alerts:
            print(f"  - {alert}")
        sys.exit(1)
    else:
        print("\nNo alerts.")


if __name__ == "__main__":
    main()
