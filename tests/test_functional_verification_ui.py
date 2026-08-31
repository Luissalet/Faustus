"""Source contract + node checks for the functional-verification UI:
agentHarnessUI (tests / review / checkpoint cards, restore + commit + review
controls, queue card), fileViewer (checkpoint diffs, accept/reject), chat.js
wiring, sessions.js (queue dot, interrupted toast), projects.js (agent knobs,
activity list) and slashCommands (/agents v2, /scorecard)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS_JS = Path("static/js/agentHarnessUI.js").read_text(encoding="utf-8")
VIEWER_JS = Path("static/js/fileViewer.js").read_text(encoding="utf-8")
CHAT_JS = Path("static/js/chat.js").read_text(encoding="utf-8")
SESSIONS_JS = Path("static/js/sessions.js").read_text(encoding="utf-8")
PROJECTS_JS = Path("static/js/projects.js").read_text(encoding="utf-8")
SLASH_JS = Path("static/js/slashCommands.js").read_text(encoding="utf-8")
RENDERER_JS = Path("static/js/chatRenderer.js").read_text(encoding="utf-8")
CSS = Path("static/style.css").read_text(encoding="utf-8")
ROUTES = Path("routes/chat_routes.py").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None


def test_harness_cards_cover_every_new_status():
    fn = HARNESS_JS.split("export function renderHarnessCheck", 1)[1].split("export function renderHarnessSummary", 1)[0]
    for status in ("'checkpoint'", "'tests_running'", "'tests_failed'", "'review_running'", "'review_issues'", "'verified'"):
        assert f"status === {status}" in fn, status
    verified = fn.split("status === 'verified'", 1)[1]
    assert "_testsLine(t)" in verified and "_reviewLine(rv)" in verified
    assert "checkpoint: json.checkpoint" in verified
    assert "Changed but NOT green" in verified   # tests failed / review defects → red card


def test_turn_summary_has_restore_commit_and_review_controls():
    row = HARNESS_JS.split("export function turnFilesRowHtml", 1)[1].split("function _workspaceFallback", 1)[0]
    assert "data-restore-turn=" in row and "data-revert-all=" in row and "data-commit-turn=" in row
    assert "data-review-all=\"accept\"" in row and "data-review-all=\"reject\"" in row
    assert "/api/workspace/checkpoint/restore" in HARNESS_JS
    assert "/api/workspace/commit/proposal" in HARNESS_JS and "/api/workspace/commit?workspace=" in HARNESS_JS
    assert "/api/workspace/review/" in HARNESS_JS and "'/decide'" in HARNESS_JS or "/decide`" in HARNESS_JS
    init = HARNESS_JS.split("export function init(apiBase)", 1)[1]
    for sel in ("[data-revert-all]", "[data-restore-turn]", "[data-commit-turn]", "[data-review-all]", "[data-stop-worker]", "[data-rerun-worker]"):
        assert sel in init, sel
    assert "odysseus:message-saved" in init and "odysseus:review-decided" in init


def test_queue_card_and_subagent_stop_rerun():
    assert "case 'queue_status': renderQueueStatus(json); return true;" in HARNESS_JS
    assert "In queue — position" in HARNESS_JS
    sa = HARNESS_JS.split("export function renderSubagentEvent", 1)[1].split("// ── Progress panel", 1)[0]
    assert "data-stop-worker=" in sa and "data-rerun-worker=" in sa
    assert "/api/chat/subagent/stop/" in HARNESS_JS
    assert "sc.delegateTasks([{ name: task.name" in HARNESS_JS


def test_file_viewer_diffs_and_reverts_against_the_checkpoint_and_can_decide():
    assert "checkpoint=${encodeURIComponent(_state.checkpoint)}" in VIEWER_JS
    assert VIEWER_JS.count("checkpoint=${encodeURIComponent(_state.checkpoint)}") >= 2   # diff + revert
    assert "data-fv=\"accept\"" in VIEWER_JS and "data-fv=\"reject\"" in VIEWER_JS
    assert "async function _decide(decision)" in VIEWER_JS and "/decide`" in VIEWER_JS
    assert "odysseus:review-decided" in VIEWER_JS
    assert "checkpoint: a.dataset.openCheckpoint || null" in VIEWER_JS
    assert "diff vs. before this turn" in VIEWER_JS


def test_chat_js_wiring():
    assert "_HARNESS_SPEAKS_AGAIN.has(json.status)" in CHAT_JS
    assert "'tests_failed', 'review_issues'" in CHAT_JS
    assert "json.type === 'queue_status'" in CHAT_JS
    assert "odysseus:message-saved" in CHAT_JS
    resume = CHAT_JS.split("export async function resumeStream", 1)[1].split("export function checkBackgroundStream", 1)[0]
    assert "json.type === 'queue_status'" in resume


def test_restored_history_uses_the_shared_turn_row():
    assert "window.agentHarnessUI.restoredTurnFilesRow(hz, msgId)" in RENDERER_JS
    assert "tests ✗" in RENDERER_JS and "review ✓" in RENDERER_JS
    assert "export function restoredTurnFilesRow(hz, messageId)" in HARNESS_JS


def test_sidebar_queue_and_interrupted_notice():
    sync = SESSIONS_JS.split("async function _syncActivityFromServer", 1)[1].split("\nlet _serverQueued", 1)[0]
    assert "_serverQueued = " in sync and "_noticeInterrupted(interrupted)" in sync
    assert "export function sessionQueuePosition(sessionId)" in SESSIONS_JS
    dots = SESSIONS_JS.split("function _updateResearchDots()", 1)[1].split("\n}\n", 1)[0]
    assert "star.classList.toggle('queued', isRunning && queuePos > 0)" in dots
    assert "/api/chat/interrupted/ack" in SESSIONS_JS
    assert ".session-star.queued" in CSS
    activity = ROUTES.split("async def chat_activity", 1)[1].split("@router.post(\"/api/chat/interrupted/ack\")", 1)[0]
    assert '"queued": queued' in activity and '"interrupted": interrupted' in activity


def test_projects_expose_agent_knobs_and_activity():
    for key in ("trusted", "trusted_agents", "review_mode", "checkpoints", "run_tests"):
        assert f"['{key}', " in PROJECTS_JS, key
    assert "project-test-command" in PROJECTS_JS and "project-review-model" in PROJECTS_JS
    assert "data-agent-flag" in PROJECTS_JS
    assert "export const projectAudit = (id, limit = 100) => req(`/${id}/audit?limit=${encodeURIComponent(limit)}`);" in PROJECTS_JS
    assert "async function renderAudit(project)" in PROJECTS_JS and "data-audit-session" in PROJECTS_JS
    assert "function scrollToMessage(messageId" in PROJECTS_JS and "[data-db-id=" in PROJECTS_JS
    assert "instructions_file" in PROJECTS_JS and "repo_map_chars" in PROJECTS_JS


def test_slash_commands_agents_v2_and_scorecard():
    assert "export function delegateTasks(tasks, { parallel = true, review = false } = {})" in SLASH_JS
    assert "reviewer: !!review" in SLASH_JS
    assert "--(review|reviewer|serial|sequential)" in SLASH_JS
    assert "scorecard: {" in SLASH_JS and "/api/scorecard/table?days=" in SLASH_JS
    assert "a.includes('here')" in SLASH_JS and "&workspace=${encodeURIComponent(ws)}" in SLASH_JS
    assert "agentsmd: {" in SLASH_JS and "/api/workspace/instructions/draft" in SLASH_JS and "usage: '/agentsmd [write]'" in SLASH_JS
    assert "window.slashCommandsModule = slashCommands" in SLASH_JS


@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_turn_files_row_renders_chips_and_controls_in_node(tmp_path):
    """Run the real turnFilesRowHtml (pure string building) in node."""
    src = HARNESS_JS
    # The module touches window/document only inside init()/DOM helpers and at
    # the very end (window.agentHarnessUI = …); a bare global is enough.
    script = (
        "globalThis.window = globalThis; globalThis.document = { addEventListener() {}, getElementById() { return null; } };\n"
        "globalThis.CSS = { escape: s => s };\n"
        + src.replace("export function", "function").replace("export async function", "async function").replace("export default agentHarnessUI;", "")
        + "\nconst html = turnFilesRowHtml({ mutations: ['src/a.py', 'tests/test_a.py'], workspace: 'D:/ws', checkpoint: 'abc123def456', review_mode: true }, { messageId: '42', reviewState: { pending: ['src/a.py'], accepted: ['tests/test_a.py'], rejected: [] } });\n"
        "const html2 = turnFilesRowHtml({ mutations: ['x.js'], workspace: '/w' }, {});\n"
        "console.log(JSON.stringify({ html, html2 }));\n"
    )
    proc = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    html = out["html"]
    assert 'data-open-checkpoint="abc123def456"' in html and 'data-review-msg="42"' in html
    assert 'class="harness-file is-pending"' in html and 'class="harness-file is-accepted"' in html
    assert "Restore to before this turn" in html and "Commit these changes" in html
    assert 'data-review-bar="1"' in html and '<b class="harness-review-count">1</b>' in html
    html2 = out["html2"]
    assert "Revert all 1" in html2 and "Restore to before" not in html2 and "data-review-bar" not in html2
