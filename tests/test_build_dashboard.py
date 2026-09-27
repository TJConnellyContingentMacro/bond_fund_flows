from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_dashboard import DASHBOARD_DIR, main, render  # noqa: E402

TEMPLATE = (DASHBOARD_DIR / "template.html").read_text(encoding="utf-8")


def test_template_is_ascii_only():
    """Literal non-ASCII symbols rendered as mojibake when served without a
    UTF-8 charset; entities and \\u escapes render the same on any host."""
    offenders = sorted({hex(ord(c)) for c in TEMPLATE if ord(c) > 127})
    assert offenders == []


def test_render_embeds_data_that_cannot_close_the_script_tag():
    html = render(TEMPLATE, {"note": "</script><b>x"}, {"text": "a </script> b"})
    assert "</script><b>x" not in html
    assert "<\\/script><b>x" in html
    assert html.isascii()


def test_render_requires_each_placeholder_exactly_once():
    with pytest.raises(ValueError, match="__COMMENTARY_JSON__"):
        render("<p>__SNAPSHOT_JSON__</p>", {}, None)


def test_main_renders_without_commentary(tmp_path):
    (tmp_path / "template.html").write_text(TEMPLATE, encoding="utf-8")
    (tmp_path / "snapshot.json").write_text(json.dumps({"flows": {"available": False}}), encoding="utf-8")

    main(tmp_path)

    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert '<script type="application/json" id="commentary-data">null</script>' in html
