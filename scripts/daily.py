"""Cron entrypoint. Runs whichever issuer adapters have been built so far —
an issuer with no adapter module yet just shows up as 0% coverage rather than
blocking the run.

Usage:
    python scripts/daily.py                # all built adapters
    python scripts/daily.py --issuer SSGA   # just one, for testing
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bondflows.ingest import run_daily  # noqa: E402
from bondflows.sources.base import SourceAdapter  # noqa: E402

# One entry per issuer, matching the `issuer` column in universe.csv. Add a
# line here in the same commit that adds src/bondflows/sources/<issuer>.py.
_ADAPTER_MODULES: dict[str, tuple[str, str]] = {
    "iShares": ("bondflows.sources.ishares", "ISharesAdapter"),
    "Vanguard": ("bondflows.sources.vanguard", "VanguardAdapter"),
}


def _load_adapters(only: str | None) -> dict[str, SourceAdapter]:
    adapters: dict[str, SourceAdapter] = {}
    for issuer, (module_name, class_name) in _ADAPTER_MODULES.items():
        if only and issuer != only:
            continue
        try:
            module = __import__(module_name, fromlist=[class_name])
            adapter_cls = getattr(module, class_name)
        except (ImportError, AttributeError) as exc:
            logging.warning("Could not load adapter for %s: %s", issuer, exc)
            continue
        adapters[issuer] = adapter_cls()
    return adapters


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issuer", help="Run just one issuer's adapter, by universe.csv issuer name")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    adapters = _load_adapters(args.issuer)
    if not adapters:
        print("No adapters loaded — nothing to run.")
        return
    run_daily(adapters)


if __name__ == "__main__":
    main()
