"""The generic-design audit of ui_smoke (radar #241): deterministic patterns
reported as warnings, never as a failure, and nothing on a plain page."""
from __future__ import annotations

import textwrap

import pytest

from src import ui_smoke

requires_playwright = pytest.mark.skipif(
    not ui_smoke.playwright_available(), reason="playwright (python) + a browser are not installed"
)

GENERIC = """
<!doctype html><html lang="en"><head><title>Generic</title>
<style>
  body { font-family: Inter, sans-serif; margin: 0; }
  .hero { background: linear-gradient(90deg, rgb(124, 58, 237), rgb(37, 99, 235)); color: white; padding: 40px; }
  .card { border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,.2); background: #fff; padding: 16px; margin: 8px; }
  .band { background: rgb(37, 99, 235); padding: 12px; }
  .band p { color: rgb(128, 128, 128); }
  .icon { width: 40px; height: 40px; border-radius: 10px; background: #eef; }
</style></head><body>
  <section class="hero"><h1>Build faster</h1></section>
  <div class="card"><div class="card">nested card text here</div></div>
  <div class="band"><p>grey text on blue background</p></div>
  <div><div class="icon"><svg width="20" height="20"></svg></div><h2>Fast</h2></div>
  <div><div class="icon"><svg width="20" height="20"></svg></div><h2>Simple</h2></div>
  <div><div class="icon"><svg width="20" height="20"></svg></div><h2>Secure</h2></div>
</body></html>
"""

PLAIN = """
<!doctype html><html lang="en"><head><title>Plain</title>
<style>body { font-family: Georgia, serif; color: #111; background: #fff; }</style></head>
<body><h1>Notes</h1><h2>One</h2><p>Plain text on white.</p><h2>Two</h2><p>More text.</p></body></html>
"""


def _run(tmp_path, html):
    (tmp_path / "index.html").write_text(textwrap.dedent(html), encoding="utf-8")
    spec = ui_smoke.detect_server(str(tmp_path))
    return ui_smoke.run_smoke(str(tmp_path), spec, timeout_s=30)


def test_design_warnings_are_listed_and_never_fail():
    out = ui_smoke._quality_warnings(None, None, {"counts_by_rule": {"nested-cards": 2, "purple-blue-gradient": 1}})
    assert out == ["design: nested-cards ×2, purple-blue-gradient ×1"]
    assert ui_smoke._quality_warnings(None, None, {"counts_by_rule": {}}) == []


@requires_playwright
def test_generic_patterns_are_found(tmp_path):
    report = _run(tmp_path, GENERIC)
    rules = (report.get("design") or {}).get("counts_by_rule") or {}
    for rule in ("purple-blue-gradient", "nested-cards", "grey-on-color", "icon-above-every-heading"):
        assert rule in rules, (rule, rules)
    assert any(w.startswith("design:") for w in report["quality_warnings"])


@requires_playwright
def test_a_plain_page_reports_no_design_pattern(tmp_path):
    report = _run(tmp_path, PLAIN)
    assert report["ok"] is True
    assert ((report.get("design") or {}).get("counts_by_rule") or {}) == {}
