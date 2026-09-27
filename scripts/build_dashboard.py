"""Renders dashboard/index.html from dashboard/template.html, snapshot.json and
(optionally) commentary.json. Stdlib only so the cloud refresh routine can run it.

Usage:
    python scripts/build_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
PLACEHOLDERS = ("__SNAPSHOT_JSON__", "__COMMENTARY_JSON__")


def embed_json(obj) -> str:
    # "</" inside a <script> block would end it early.
    return json.dumps(obj).replace("</", "<\\/")


def render(template: str, snapshot: dict, commentary: dict | None) -> str:
    for placeholder in PLACEHOLDERS:
        if template.count(placeholder) != 1:
            raise ValueError(f"template must contain {placeholder} exactly once")
    return template.replace("__SNAPSHOT_JSON__", embed_json(snapshot)).replace(
        "__COMMENTARY_JSON__", embed_json(commentary)
    )


def main(dashboard_dir: Path = DASHBOARD_DIR) -> None:
    template = (dashboard_dir / "template.html").read_text(encoding="utf-8")
    snapshot = json.loads((dashboard_dir / "snapshot.json").read_text(encoding="utf-8"))
    commentary_path = dashboard_dir / "commentary.json"
    commentary = json.loads(commentary_path.read_text(encoding="utf-8")) if commentary_path.exists() else None
    (dashboard_dir / "index.html").write_text(render(template, snapshot, commentary), encoding="utf-8")
    print(f"Wrote {dashboard_dir / 'index.html'}")


if __name__ == "__main__":
    main()
