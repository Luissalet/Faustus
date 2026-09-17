"""tests/test_h1.py — H1 (ui_smoke): src/ui_smoke.py

Reproduces the real bug from `silhouettes_analysis.md` chat `782b7d89`
(16-09-2026): a Flask app that serves a `.mjs` module with
`Content-Type: text/plain`, which passed 170 unit tests but left every
button in the UI dead because the browser refuses to run it as an ES
module. `ui_smoke` is the harness-driven check that finds this without a
model ever opening a browser.

Uses real subprocesses (real Flask + a real `python -m http.server`), not
mocks: the whole point of this module is that it proves a server actually
answers with the right bytes over the wire.
"""
from __future__ import annotations

import os
import sys
import textwrap
import time

import pytest

from src import ui_smoke


def _write(path, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(content))


def _assert_no_leftover_process(workspace: str) -> None:
    """No process with cwd == workspace survives past the smoke run."""
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a real dependency here
        return
    ws = os.path.realpath(str(workspace))
    for p in psutil.process_iter(["pid"]):
        try:
            if os.path.realpath(p.cwd()) == ws:
                pytest.fail(f"leftover process pid={p.pid} still has cwd={ws}")
        except Exception:
            continue


# ---------------------------------------------------------------------------
# fixtures: two flask apps (one with the real bug, one healthy)
# ---------------------------------------------------------------------------

FLASK_APP_BUGGY = """
    import os
    from flask import Flask, Response

    app = Flask(__name__)

    INDEX_HTML = (
        '<!doctype html><html><body>'
        '<script type="module" src="/static/app.mjs"></script>'
        '</body></html>'
    )
    APP_MJS = "export const answer = 42;\\n"

    @app.route("/")
    def index():
        return Response(INDEX_HTML, mimetype="text/html")

    @app.route("/static/app.mjs")
    def app_mjs():
        # the real 16-09-2026 bug: an ES module served with the wrong
        # Content-Type, so the browser refuses to execute it.
        return Response(APP_MJS, mimetype="text/plain")

    if __name__ == "__main__":
        app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
"""

FLASK_APP_HEALTHY = """
    import os
    from flask import Flask, Response

    app = Flask(__name__)

    INDEX_HTML = (
        '<!doctype html><html><body>'
        '<link rel="stylesheet" href="/static/style.css">'
        '<script type="module" src="/static/app.mjs"></script>'
        '</body></html>'
    )
    APP_MJS = "export const answer = 42;\\n"
    STYLE_CSS = "body { margin: 0; }\\n"

    @app.route("/")
    def index():
        return Response(INDEX_HTML, mimetype="text/html")

    @app.route("/static/app.mjs")
    def app_mjs():
        return Response(APP_MJS, mimetype="text/javascript")

    @app.route("/static/style.css")
    def style_css():
        return Response(STYLE_CSS, mimetype="text/css")

    if __name__ == "__main__":
        app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
"""

FLASK_APP_CRASH = """
    import sys
    from flask import Flask

    app = Flask(__name__)
    print("about to crash on startup", file=sys.stderr)
    raise RuntimeError("simulated startup crash — the port is never opened")

    if __name__ == "__main__":  # pragma: no cover - unreachable, kept so
        app.run(host="127.0.0.1", port=5000)  # detect_server still sees app.run(
"""


def _flask_available() -> bool:
    try:
        import flask  # noqa: F401
        return True
    except ImportError:
        return False


requires_flask = pytest.mark.skipif(not _flask_available(), reason="Flask is not installed")


# ---------------------------------------------------------------------------
# 1. the real bug: .mjs served as text/plain -> ok=False, problem=content_type
# ---------------------------------------------------------------------------

@requires_flask
def test_mjs_served_as_text_plain_is_caught(tmp_path):
    _write(tmp_path / "app.py", FLASK_APP_BUGGY)

    spec = ui_smoke.detect_server(str(tmp_path))
    assert spec is not None
    assert spec["kind"] == "flask"

    report = ui_smoke.run_smoke(str(tmp_path), spec, timeout_s=30)

    assert report["ran"] is True
    assert report["ok"] is False
    mjs_assets = [a for a in report["assets"] if a["url"].endswith("app.mjs")]
    assert mjs_assets, f"app.mjs was never checked: {report['assets']}"
    bad = mjs_assets[0]
    assert bad["ok"] is False
    assert bad["problem"] == "content_type"
    assert (bad["content_type"] or "").split(";")[0].strip() == "text/plain"
    assert "content_type" in report["summary"] or "asset" in report["summary"]
    _assert_no_leftover_process(str(tmp_path))


# ---------------------------------------------------------------------------
# 2. a healthy project reports ok=True
# ---------------------------------------------------------------------------

@requires_flask
def test_healthy_flask_project_is_ok(tmp_path):
    _write(tmp_path / "app.py", FLASK_APP_HEALTHY)

    spec = ui_smoke.detect_server(str(tmp_path))
    assert spec is not None and spec["kind"] == "flask"

    report = ui_smoke.run_smoke(str(tmp_path), spec, timeout_s=30)

    assert report["ran"] is True
    assert report["ok"] is True, report["summary"]
    assert report["pages"] and report["pages"][0]["ok"] is True
    assert len(report["assets"]) >= 2
    assert all(a["ok"] for a in report["assets"]), report["assets"]
    _assert_no_leftover_process(str(tmp_path))


# ---------------------------------------------------------------------------
# 3. a server that crashes on startup: ran=True, ok=False, clear summary,
#    and nothing is left running.
# ---------------------------------------------------------------------------

@requires_flask
def test_server_that_fails_to_start(tmp_path):
    _write(tmp_path / "app.py", FLASK_APP_CRASH)

    spec = ui_smoke.detect_server(str(tmp_path))
    assert spec is not None and spec["kind"] == "flask"

    report = ui_smoke.run_smoke(str(tmp_path), spec, timeout_s=15)

    assert report["ran"] is True
    assert report["ok"] is False
    assert report["summary"], "a failed launch must still explain itself"
    assert "never answered" in report["summary"] or "exited" in report["summary"]
    _assert_no_leftover_process(str(tmp_path))


# ---------------------------------------------------------------------------
# 4. no UI files mutated this turn -> ran=False, nothing launched at all
# ---------------------------------------------------------------------------

@requires_flask
def test_no_ui_mutation_skips_entirely(tmp_path, monkeypatch):
    _write(tmp_path / "app.py", FLASK_APP_HEALTHY)
    monkeypatch.setattr(ui_smoke, "detect_server", lambda ws: pytest.fail("must not even look for a server"))

    report = ui_smoke.run_for_turn(str(tmp_path), ["README.md", "src/utils/math_helpers.py"])

    assert report is not None
    assert report["ran"] is False


def test_ui_mutation_but_no_server_detected(tmp_path):
    # A workspace with a UI-shaped mutation but nothing serving it.
    (tmp_path / "notes.txt").write_text("no server here")
    report = ui_smoke.run_for_turn(str(tmp_path), ["static/app.css"])
    assert report is not None
    assert report["ran"] is False
    assert "no web server" in report["summary"]


def test_agent_ui_smoke_setting_off(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_smoke, "_setting", lambda key, default: False if key == "agent_ui_smoke" else default)
    report = ui_smoke.run_for_turn(str(tmp_path), ["static/app.js"])
    assert report is None


# ---------------------------------------------------------------------------
# 5. playwright (when installed): a real console.error is captured
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not ui_smoke.playwright_available(), reason="playwright (python) + a browser are not installed")
def test_playwright_captures_console_error(tmp_path):
    _write(
        tmp_path / "index.html",
        """
        <!doctype html>
        <html><body>
        <script>console.error("boom-from-ui-smoke-test");</script>
        </body></html>
        """,
    )
    spec = ui_smoke.detect_server(str(tmp_path))
    assert spec is not None and spec["kind"] == "static"

    report = ui_smoke.run_smoke(str(tmp_path), spec, timeout_s=30)

    assert report["ran"] is True
    assert report["playwright_used"] is True
    assert any("boom-from-ui-smoke-test" in e for e in report["console_errors"])
    assert report["ok"] is False
    _assert_no_leftover_process(str(tmp_path))


# ---------------------------------------------------------------------------
# unit-level coverage that doesn't need a live server
# ---------------------------------------------------------------------------

def test_is_ui_mutation():
    assert ui_smoke._is_ui_mutation("static/editor/app.mjs")
    assert ui_smoke._is_ui_mutation("templates/index.html")
    assert ui_smoke._is_ui_mutation("app.py")
    assert not ui_smoke._is_ui_mutation("src/utils/math_helpers.py")
    assert not ui_smoke._is_ui_mutation("README.md")


def test_detect_server_static_index_html(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    spec = ui_smoke.detect_server(str(tmp_path))
    assert spec is not None
    assert spec["kind"] == "static"


def test_detect_server_npm(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"dev": "vite"}}')
    spec = ui_smoke.detect_server(str(tmp_path))
    assert spec is not None
    assert spec["kind"] == "npm"
    assert spec["script"] == "dev"


def test_detect_server_none(tmp_path):
    (tmp_path / "README.md").write_text("nothing here")
    assert ui_smoke.detect_server(str(tmp_path)) is None


def test_check_asset_js_mime_variants():
    good = ui_smoke._JS_MIME_OK
    assert "text/javascript" in good and "application/javascript" in good
    assert "text/plain" not in good


def test_detect_server_follows_a_local_import_to_find_flask(tmp_path):
    """Live 17-09: `app.py` only did `from notas.api import create_app`; the
    smoke fell through to http.server on templates/ and reported 404s on
    every asset of a working Flask app."""
    from src import ui_smoke
    (tmp_path / "notas").mkdir()
    (tmp_path / "notas" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "notas" / "api.py").write_text(
        "from flask import Flask\n\ndef create_app():\n    return Flask(__name__)\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from notas.api import create_app\n\napp = create_app()\n\nif __name__ == '__main__':\n    app.run(port=5055)\n",
        encoding="utf-8")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "index.html").write_text("<html></html>", encoding="utf-8")
    spec = ui_smoke.detect_server(str(tmp_path))
    assert spec is not None and spec["kind"] == "flask" and spec["entry"] == "app.py"
    argv, env, cwd = ui_smoke._build_launch(spec, str(tmp_path), 5099, "python")
    assert argv[-3:] == ["run", "--port", "5099"] and env["FLASK_APP"] == "app.py"
    # And a bare templates/ dir is never mistaken for a static site.
    (tmp_path / "app.py").unlink()
    assert ui_smoke.detect_server(str(tmp_path)) is None
