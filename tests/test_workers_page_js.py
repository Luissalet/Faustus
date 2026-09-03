"""The Workers page (static/js/workers.js): a task in plain language → the
local workers, and the job list with the compact result. The renderers are
pure and run in node; the wiring is pinned at source level."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = (REPO / "static/js/workers.js").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None

JOB = {
    "id": "0fd6e2154d1e", "status": "done", "title": "Workers · In cart.py add apply_discount", "created": 1788392365.2,
    "duration_s": 43.8, "workspace": "D:\\proj", "model": "qwen3.5:9b", "session_id": "sess-1",
    "tasks": [{"instruction": "In cart.py add apply_discount", "files": ["cart.py"]}],
    "result": {"workers": [{"name": "w1", "status": "done", "stop_reason": "complete", "rounds": 12, "tool_calls": 12,
                            "failed_calls": 4, "input_tokens": 118183, "output_tokens": 1432,
                            "files_changed": ["cart.py", "tests/test_cart.py"],
                            "summary": "All 7 tests pass. <img src=x onerror=1>"}],
               "files_changed": ["cart.py", "tests/test_cart.py"], "totals": {"errors": 0}},
}
RUNNING = {"id": "abc", "status": "running", "title": "Workers · slow <b>x</b>", "created": 1.0, "session_id": "s2",
           "tasks": [], "progress": {"w1": {"last_event": "tick", "round": 3, "last_tool": "bash", "elapsed_s": 40.2, "stalled": True, "stall_reason": "idle"}}}


def _run(script: str) -> dict:
    src = (SRC.replace("export function", "function").replace("export default workersModule;", "")
           .replace("if (typeof window !== 'undefined') window.workersModule = workersModule;", ""))
    proc = subprocess.run(["node", "--input-type=module"], input=src + "\n" + script, capture_output=True,
                          text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_module_parses_and_is_wired():
    assert subprocess.run(["node", "--check", str(REPO / "static/js/workers.js")], capture_output=True).returncode == 0
    index = (REPO / "static/index.html").read_text(encoding="utf-8")
    app = (REPO / "static/app.js").read_text(encoding="utf-8")
    assert 'id="tool-workers-btn"' in index and "Workers" in index
    assert "import workersModule from './js/workers.js" in app and "workersModule.openWorkers()" in app
    assert "'/workers':  () => document.getElementById('tool-workers-btn')?.click()" in app
    assert "'/static/js/workers.js'" in (REPO / "static/sw.js").read_text(encoding="utf-8")
    assert ".workers-modal-content" in (REPO / "static/style.css").read_text(encoding="utf-8")
    # no native dialogs, and the API is the dispatch one
    assert "window.prompt(" not in SRC and "confirm(" not in SRC and "alert(" not in SRC
    assert "/api/dispatch" in SRC


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_job_rows_collapsed_and_expanded_escape_and_link_to_the_board():
    out = _run(f"""
      const JOB = {json.dumps(JOB)}; const RUN = {json.dumps(RUNNING)};
      console.log(JSON.stringify({{ closed: jobHtml(JOB, false), open: jobHtml(JOB, true), running: jobHtml(RUN, true) }}));
    """)
    closed, opened, running = out["closed"], out["open"], out["running"]
    assert 'wk-status-done">done<' in closed and "2 files changed" in closed and 'data-wk-open="sess-1"' in closed
    assert "wk-job-body" not in closed and "Cancel" not in closed
    assert "12 rounds · 12 tools (4 failed) · 118183/1432 tok" in opened
    assert "<code>cart.py</code>" in opened and "All 7 tests pass. &lt;img src=x onerror=1&gt;" in opened
    assert "<img src=x onerror=1>" not in opened
    assert "1 task · D:\\proj · qwen3.5:9b" in opened and "[cart.py]" in opened
    # a running job: progress per worker, a Cancel button, escaped title
    assert 'data-wk-cancel="abc"' in running and "round 3 · bash · 40 s · <b>stalled</b> (idle)" in running
    assert "slow &lt;b&gt;x&lt;/b&gt;" in running and "<b>x</b>" not in running


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_page_has_the_box_the_folder_and_an_empty_state():
    out = _run("""
      console.log(JSON.stringify({ empty: pageHtml([], new Set(), { workspace: 'D:\\\\proj' }),
                                   busy: pageHtml([], new Set(), { busy: true }) }));
    """)
    assert 'id="wk-task"' in out["empty"] and 'value="D:\\proj"' in out["empty"] and "No jobs yet" in out["empty"]
    assert 'id="wk-parallel" checked' in out["empty"] and "website/fable-workers.md" in out["empty"]
    assert "Starting…" in out["busy"] and "disabled" in out["busy"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_one_task_per_line_numbered_lists_accepted_max_four():
    out = _run("""
      console.log(JSON.stringify({
        lines: parseTasks('add a test\\n\\n2. fix the bug  \\n- write docs\\n• fourth\\nfifth'),
        one: parseTasks('  just one  '), none: parseTasks(''),
      }));
    """)
    assert out["lines"] == ["add a test", "fix the bug", "write docs", "fourth"]
    assert out["one"] == ["just one"] and out["none"] == []
