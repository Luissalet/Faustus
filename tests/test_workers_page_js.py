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
# What a worker's own output says about it (src/output_rules.py), as the
# dispatch progress entry carries it.
DETECTED = {"id": "det", "status": "running", "title": "t", "created": 1.0, "tasks": [], "progress": {
    "w1": {"last_event": "tool", "state": "rate_limited", "why": "HTTP 429 Too Many Requests <b>"},
    "w2": {"last_event": "tool", "state": "waiting_for_input", "why": "Overwrite? [y/N]"},
    "w3": {"last_event": "tick", "round": 2}}}


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
def test_a_detected_worker_state_is_a_chip_that_says_why():
    """The board shows WHAT a worker's own output says and the line it says it
    on — and that nobody killed it for it."""
    out = _run(f"""
      console.log(JSON.stringify({{ job: jobHtml({json.dumps(DETECTED)}, true),
        chip: stateChip({{ state: 'stuck', why: 'the same line 4 times at the tail: retrying' }}),
        none: stateChip({{ last_event: 'tick' }}), empty: stateChip(null) }}));
    """)
    job, chip = out["job"], out["chip"]
    assert 'class="wk-state wk-state-rate_limited"' in job and ">rate limited<" in job
    assert 'class="wk-state wk-state-waiting_for_input"' in job and ">waiting for input<" in job
    # the matched line travels in the title, escaped, and says it was not killed
    assert "HTTP 429 Too Many Requests &lt;b&gt; (reported, not killed)" in job
    assert "<b>" not in job, "a worker's own output is never trusted as markup"
    assert "Overwrite? [y/N]" in job
    # a worker with no detected state gets no chip at all
    assert job.count('class="wk-state') == 2
    assert 'title="stuck — the same line 4 times at the tail: retrying (reported, not killed)"' in chip
    assert out["none"] == "" and out["empty"] == ""


def test_the_board_streams_live_and_falls_back_to_the_poll():
    """The list fills in from /events?stream=1 while a job runs; any failure —
    no EventSource, the setting off, a proxy — goes back to the 3 s poll."""
    assert "new EventSource(" in SRC and "/events?stream=1" in SRC
    assert "typeof EventSource !== 'undefined'" in SRC
    # every failure path latches the fallback and every stream is closed
    assert SRC.count("_noStream = true") >= 2 and "es.onerror" in SRC
    assert "addEventListener('end'" in SRC
    assert "setInterval(_refreshJobs, 3000)" in SRC and "_closeStreams()" in SRC
    # the modal is closed → nothing is left open
    close = SRC.split("export function closeWorkers()")[1]
    assert "_closeStreams()" in close and "clearInterval(_pollTimer)" in close


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_page_has_the_box_the_folder_and_an_empty_state():
    out = _run("""
      console.log(JSON.stringify({ empty: pageHtml([], new Set(), { workspace: 'D:\\\\proj' }),
                                   busy: pageHtml([], new Set(), { busy: true }) }));
    """)
    assert 'id="wk-task"' in out["empty"] and 'value="D:\\proj"' in out["empty"] and "No jobs yet" in out["empty"]
    assert 'id="wk-parallel" checked' in out["empty"] and "website/fable-workers.md" in out["empty"]
    assert 'id="wk-verify"' in out["empty"] and 'id="wk-fix"' in out["empty"] and 'id="wk-workspace"' in out["empty"] and " required" in out["empty"]
    assert "Starting…" in out["busy"] and "disabled" in out["busy"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_blank_lines_and_list_markers_split_tasks_max_four():
    out = _run("""
      console.log(JSON.stringify({
        lines: parseTasks('add a test\\n\\n2. fix the bug  \\n- write docs\\n• fourth\\n\\nfifth'),
        one: parseTasks('  just one  '), none: parseTasks(''),
      }));
    """)
    assert out["lines"] == ["add a test", "fix the bug", "write docs", "fourth"]
    assert out["one"] == ["just one"] and out["none"] == []


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_a_finished_job_shows_the_verdict_what_changed_on_disk_and_the_verification():
    job = dict(JOB, status="partial", verdict="1/1 workers done · 2 files changed on disk · verification FAILED (1 failed)",
               result=dict(JOB["result"], files_changed=["cart.py", "new.py"], claimed_only=["tests/test_cart.py"],
                           changes={"source": "checkpoint", "count": 2, "added": ["new.py"], "modified": ["cart.py"], "deleted": [], "truncated": False},
                           verification={"mode": "auto", "ran": True, "ok": False, "summary": "1 failed", "command": "python -m pytest -q",
                                         "failures": ["tests/test_cart.py::test_total — assert 0 == 3"], "pre_existing": [], "attempts": 2,
                                         "output_tail": "E  assert 0 == 3 <b>"}))
    out = _run(f"""
      const JOB = {json.dumps(job)};
      console.log(JSON.stringify({{ closed: jobHtml(JOB, false), open: jobHtml(JOB, true),
        verifying: jobHtml({{ id: 'v', status: 'verifying', title: 't', created: 1, phase: 'running the verification', ceiling_s: 1200, tasks: [], progress: {{}} }}, true) }}));
    """)
    closed, opened, verifying = out["closed"], out["open"], out["verifying"]
    assert 'wk-status-partial">partial<' in closed and "2 files changed" in closed and "verification failed" in closed
    assert "wk-verdict" in opened and "verification FAILED (1 failed)" in opened
    assert "Changed on disk" in opened and "wk-chg-added" in opened and "<code>new.py</code>" in opened
    assert "Claimed by a worker but not changed" in opened and "<code>tests/test_cart.py</code>" in opened
    assert "Verification failed" in opened and "2 attempts" in opened and "test_total — assert 0 == 3" in opened
    assert "assert 0 == 3 &lt;b&gt;" in opened and "<b>" not in opened.split("wk-tail")[1].split("</pre>")[0]
    assert "claims: <code>cart.py</code>" in opened
    assert 'wk-status-verifying">verifying<' in verifying and "running the verification" in verifying and "at most 20 min more" in verifying
    assert 'data-wk-cancel="v"' in verifying


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_a_list_row_without_a_result_reads_the_verdict_line():
    row = {"id": "r1", "status": "done", "title": "Workers · x", "created": 1.0, "duration_s": 40.1, "session_id": "s",
           "verdict": "2/2 workers done · 1 file changed on disk · verification passed (9 passed)", "tasks": []}
    out = _run(f"console.log(JSON.stringify({{ row: jobHtml({json.dumps(row)}, false), bad: jobHtml({json.dumps(dict(row, status='partial', verdict='1/1 workers done · 3 files changed on disk · verification FAILED (1 failed)'))}, false) }}));")
    assert "1 file changed" in out["row"] and 'wk-vword-ok">verified<' in out["row"]
    assert "3 files changed" in out["bad"] and 'wk-vword-bad">verification failed<' in out["bad"]
