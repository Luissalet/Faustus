"""tests/test_ui_smoke_audit.py — the a11y + perf audit added to
`src/ui_smoke.py`'s Playwright pass.

Two layers:
1. Pure-Python unit tests for the summary/policy logic (`_quality_warnings`,
   `_perf_warnings`, `_summarize`, the `agent_ui_smoke_a11y_blocking` setting) that
   need no browser at all.
2. Real Playwright + Chromium tests that serve tiny static HTML fixtures
   (via `ui_smoke`'s own static-server path) and run the actual in-page
   audit, asserting each deliberately-broken rule is caught with the right
   severity, and that a clean page reports nothing. Skipped cleanly when
   Playwright/Chromium are unavailable in this environment.
"""
from __future__ import annotations

import textwrap

import pytest

from src import ui_smoke


def _write(path, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(content))


# ---------------------------------------------------------------------------
# 1. pure-Python unit tests — no browser
# ---------------------------------------------------------------------------

def test_perf_warnings_thresholds():
    assert ui_smoke._perf_warnings({}) == []
    assert ui_smoke._perf_warnings({"lcp_ms": 1000, "load_ms": 1000, "js_bytes": 1000}) == []

    w = ui_smoke._perf_warnings({"lcp_ms": 3000})
    assert len(w) == 1 and "LCP" in w[0]

    w = ui_smoke._perf_warnings({"load_ms": 5000})
    assert len(w) == 1 and "load" in w[0]

    w = ui_smoke._perf_warnings({"js_bytes": 2_000_000})
    assert len(w) == 1 and "JS transfer" in w[0]

    w = ui_smoke._perf_warnings({"lcp_ms": 3000, "load_ms": 5000, "js_bytes": 2_000_000})
    assert len(w) == 3


def test_quality_warnings_combines_a11y_and_perf():
    a11y = {"counts_by_severity": {"serious": 2, "moderate": 1, "minor": 0}}
    perf = {"warnings": ["LCP 3000ms exceeds 2500ms"]}
    out = ui_smoke._quality_warnings(a11y, perf)
    assert any("2 serious" in w for w in out)
    assert any("LCP" in w for w in out)


def test_quality_warnings_empty_when_nothing_to_report():
    a11y = {"counts_by_severity": {"serious": 0, "moderate": 0, "minor": 0}}
    perf = {"warnings": []}
    assert ui_smoke._quality_warnings(a11y, perf) == []
    assert ui_smoke._quality_warnings(None, None) == []


def test_summarize_never_fails_on_perf_alone():
    """Perf warnings must never turn `ok`/failure-shaped summary on their
    own — only `problems`/`console_errors`/`a11y_blocking_failed` do."""
    perf = {"warnings": ["LCP 3000ms exceeds 2500ms", "load 5000ms exceeds 4000ms"]}
    summary = ui_smoke._summarize([], [], [], [], a11y=None, perf=perf, a11y_blocking_failed=False)
    assert summary.startswith("ui_smoke ok")
    assert "LCP" in summary


def test_summarize_surfaces_a11y_as_informational_by_default():
    a11y = {"counts_by_severity": {"serious": 3, "moderate": 0, "minor": 0}, "findings": []}
    summary = ui_smoke._summarize([], [], [], [], a11y=a11y, perf=None, a11y_blocking_failed=False)
    assert summary.startswith("ui_smoke ok")
    assert "3 serious" in summary


def test_summarize_fails_when_a11y_blocking_flag_set():
    a11y = {
        "counts_by_severity": {"serious": 1, "moderate": 0, "minor": 0},
        "findings": [{"rule": "img-alt", "severity": "serious", "selector": "img", "detail": "no alt"}],
    }
    summary = ui_smoke._summarize([], [], [], [], a11y=a11y, perf=None, a11y_blocking_failed=True)
    assert summary.startswith("ui_smoke FAILED")
    assert "serious a11y finding" in summary


def test_ui_smoke_a11y_blocking_setting_default_false():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS.get("agent_ui_smoke_a11y_blocking") is False


def test_compact_carries_quality_warnings_and_perf():
    report = ui_smoke._report(
        ran=True, ok=True, summary="ui_smoke ok",
        a11y={"counts_by_severity": {"serious": 1, "moderate": 0, "minor": 0}},
        perf={"lcp_ms": 100, "load_ms": 200, "js_bytes": 500, "warnings": []},
        quality_warnings=["a11y: 1 serious, 0 moderate, 0 minor finding(s)"],
    )
    out = ui_smoke.compact(report)
    assert out["quality_warnings"] == ["a11y: 1 serious, 0 moderate, 0 minor finding(s)"]
    assert out["a11y_counts"] == {"serious": 1, "moderate": 0, "minor": 0}
    assert out["perf"]["lcp_ms"] == 100


# ---------------------------------------------------------------------------
# 2. real Playwright + Chromium tests against tiny static fixtures
# ---------------------------------------------------------------------------

requires_playwright = pytest.mark.skipif(
    not ui_smoke.playwright_available(), reason="playwright (python) + a browser are not installed"
)

BROKEN_PAGE = """
    <!doctype html>
    <html>
    <head></head>
    <body>
      <img src="x.png">
      <button></button>
      <input type="text" placeholder="only a placeholder">
      <input type="text" id="dup">
      <h1>Title</h1>
      <h4>Skipped heading</h4>
      <div id="dup">duplicate id target</div>
      <p style="color:#999999; background-color:#ffffff;">low contrast body copy that is long enough to render</p>
    </body>
    </html>
"""

CLEAN_PAGE = """
    <!doctype html>
    <html lang="en">
    <head><title>Clean Page</title></head>
    <body>
      <img src="x.png" alt="a descriptive alt text">
      <button aria-label="Close dialog">X</button>
      <label for="f1">Full name</label>
      <input id="f1" type="text">
      <h1>Title</h1>
      <h2>Subheading</h2>
      <p style="color:#111111; background-color:#ffffff;">high contrast body copy</p>
    </body>
    </html>
"""


def _findings_by_rule(a11y, rule):
    return [f for f in a11y["findings"] if f["rule"] == rule]


# 1x1 transparent GIF — served so `<img src="x.png">` doesn't 404 and pollute
# the console-error / network-response check with an unrelated failure.
_TINY_GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
)


def _write_tiny_image(tmp_path):
    (tmp_path / "x.png").write_bytes(_TINY_GIF)


@requires_playwright
def test_a11y_audit_catches_broken_page_rules(tmp_path):
    _write_tiny_image(tmp_path)
    _write(tmp_path / "index.html", BROKEN_PAGE)
    spec = ui_smoke.detect_server(str(tmp_path))
    assert spec is not None and spec["kind"] == "static"

    report = ui_smoke.run_smoke(str(tmp_path), spec, timeout_s=30)

    assert report["ran"] is True
    assert report["playwright_used"] is True
    a11y = report["a11y"]
    assert a11y is not None

    img_alt = _findings_by_rule(a11y, "img-alt")
    assert img_alt and img_alt[0]["severity"] == "serious"

    name_missing = _findings_by_rule(a11y, "name-missing")
    assert name_missing and name_missing[0]["severity"] == "serious"

    input_label = _findings_by_rule(a11y, "input-label")
    severities = {f["severity"] for f in input_label}
    assert "minor" in severities  # placeholder-only input
    assert "serious" in severities  # no label at all

    html_lang = _findings_by_rule(a11y, "html-lang")
    assert html_lang and html_lang[0]["severity"] == "moderate"

    page_title = _findings_by_rule(a11y, "page-title")
    assert page_title and page_title[0]["severity"] == "moderate"

    dup_id = _findings_by_rule(a11y, "duplicate-id")
    assert dup_id and dup_id[0]["severity"] == "moderate"

    heading_skip = _findings_by_rule(a11y, "heading-skip")
    assert heading_skip and heading_skip[0]["severity"] == "minor"

    contrast = _findings_by_rule(a11y, "color-contrast")
    assert contrast, "low-contrast paragraph must be flagged"
    assert contrast[0]["severity"] in ("serious", "moderate")

    counts = a11y["counts_by_severity"]
    assert counts["serious"] >= 3
    assert counts["moderate"] >= 3
    assert counts["minor"] >= 2

    # by default a11y is informational only: it must not fail the smoke
    assert report["ok"] is True, report["summary"]
    assert report["quality_warnings"], "serious findings must still surface as quality warnings"

    # perf keys are present regardless of a11y outcome
    perf = report["perf"]
    assert perf is not None
    for key in ("dom_content_loaded_ms", "load_ms", "lcp_ms", "js_bytes", "css_bytes", "request_count", "warnings"):
        assert key in perf


@requires_playwright
def test_a11y_audit_clean_page_has_no_findings(tmp_path):
    _write_tiny_image(tmp_path)
    _write(tmp_path / "index.html", CLEAN_PAGE)
    spec = ui_smoke.detect_server(str(tmp_path))
    assert spec is not None and spec["kind"] == "static"

    report = ui_smoke.run_smoke(str(tmp_path), spec, timeout_s=30)

    assert report["ran"] is True
    assert report["playwright_used"] is True
    a11y = report["a11y"]
    assert a11y is not None
    assert a11y["findings"] == []
    assert a11y["counts_by_severity"] == {"serious": 0, "moderate": 0, "minor": 0}
    assert report["ok"] is True
    assert report["quality_warnings"] == [] or all(
        "a11y" not in w for w in report["quality_warnings"]
    )


@requires_playwright
def test_a11y_blocking_setting_flips_result(tmp_path, monkeypatch):
    _write_tiny_image(tmp_path)
    _write(tmp_path / "index.html", BROKEN_PAGE)
    spec = ui_smoke.detect_server(str(tmp_path))
    assert spec is not None and spec["kind"] == "static"

    orig_setting = ui_smoke._setting

    def _patched(key, default):
        if key == "agent_ui_smoke_a11y_blocking":
            return True
        return orig_setting(key, default)

    monkeypatch.setattr(ui_smoke, "_setting", _patched)

    report = ui_smoke.run_smoke(str(tmp_path), spec, timeout_s=30)

    assert report["ran"] is True
    assert report["playwright_used"] is True
    assert report["a11y"]["counts_by_severity"]["serious"] > 0
    assert report["ok"] is False
    assert "serious a11y finding" in report["summary"]


@requires_playwright
def test_a11y_blocking_off_by_default_even_with_serious_findings(tmp_path):
    _write_tiny_image(tmp_path)
    _write(tmp_path / "index.html", BROKEN_PAGE)
    spec = ui_smoke.detect_server(str(tmp_path))
    report = ui_smoke.run_smoke(str(tmp_path), spec, timeout_s=30)
    assert report["playwright_used"] is True
    assert report["a11y"]["counts_by_severity"]["serious"] > 0
    assert report["ok"] is True


# ---------------------------------------------------------------------------
# HTTP-only degradation path stays unchanged when Playwright is unavailable
# ---------------------------------------------------------------------------

def test_no_playwright_leaves_a11y_and_perf_none(tmp_path, monkeypatch):
    _write(tmp_path / "index.html", CLEAN_PAGE)
    spec = ui_smoke.detect_server(str(tmp_path))
    monkeypatch.setattr(ui_smoke, "playwright_available", lambda: False)

    report = ui_smoke.run_smoke(str(tmp_path), spec, timeout_s=15)

    assert report["ran"] is True
    assert report["playwright_used"] is False
    assert report["a11y"] is None
    assert report["perf"] is None
    assert report["quality_warnings"] == []
