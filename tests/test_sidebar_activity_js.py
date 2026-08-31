"""Source contract for the Cowork-style sidebar activity work (sessions.js /
chat.js): background runs keep their dot and their row, and can be stopped
from the list."""
from pathlib import Path

SESSIONS_JS = Path("static/js/sessions.js").read_text(encoding="utf-8")
CHAT_JS = Path("static/js/chat.js").read_text(encoding="utf-8")
ROUTES = Path("routes/chat_routes.py").read_text(encoding="utf-8")


def test_activity_sessions_stay_visible_above_the_fold_in_both_list_modes():
    assert "function _pushActivitySessions(all, visible, limit)" in SESSIONS_JS
    assert "_pushActivitySessions(allFlat, visible, limit)" in SESSIONS_JS          # flat sort modes
    assert "_pushActivitySessions(unfiled, visibleUnfiled, limit)" in SESSIONS_JS  # group / manual mode
    body = SESSIONS_JS.split("function _pushActivitySessions", 1)[1].split("\n}\n", 1)[0]
    assert "sessionActivityStatus(s.id)" in body and "_showAllSessions" in body


def test_activity_sync_reloads_or_rerenders_the_list():
    sync = SESSIONS_JS.split("async function _syncActivityFromServer", 1)[1].split("\nconst _localQuestionSessions", 1)[0]
    assert "_activityUnknownSeen" in sync and "loadSessions()" in sync      # unknown running id → fetch list once
    assert "renderSessionList()" in sync                                      # running set changed → re-render
    assert "_serverRunIds = " in sync                                         # run ids for Stop
    post = SESSIONS_JS.split("function _postRenderSessionList", 1)[1].split("\n}\n", 1)[0]
    assert "_updateResearchDots()" in post                                    # fresh rows get their dots


def test_stop_run_from_the_session_menu_uses_the_run_identity():
    assert "session-stop-run" in SESSIONS_JS
    stop = SESSIONS_JS.split("session-stop-run", 1)[1].split("dropdown.appendChild(stopItem)", 1)[0]
    assert "/api/chat/stop/" in stop and "'X-Odysseus-Run-Id': _serverRunIds[s.id]" in stop
    # the route hands out the ids only for runs it can actually stop
    activity = ROUTES.split("async def chat_activity", 1)[1].split("@router.get", 1)[0]
    assert '"runs": runs' in activity and "agent_runs.get_run_id(sid)" in activity


def test_resumed_runs_render_the_live_tool_timeline():
    resume = CHAT_JS.split("export async function resumeStream", 1)[1].split("export function checkBackgroundStream", 1)[0]
    for needle in ("_liveToolNode(thread, json.tool", "_settleToolNode(resumeToolNode", "agentHarnessUI.renderSubagentEvent(json)",
                   "agentHarnessUI.handleStreamEvent(json, { sessionId })", "removeLiveTimeline()"):
        assert needle in resume, needle
    # the chat route forwards live progress (the sub-agent board depended on it)
    assert '"tool_progress",' in ROUTES.split('"tool_start", "tool_output", "agent_step",', 1)[1][:1200]


def test_background_finish_toast_carries_the_harness_outcome():
    """A background chat that finished UNVERIFIED (claims not backed by tool
    evidence) must not be announced as a plain 'Response ready'."""
    stream_js = Path("static/js/chatStream.js").read_text(encoding="utf-8")
    assert "function insertStreamDoneToast(sessionId, query, outcome)" in stream_js
    assert "function notifyStreamComplete(sessionId, query, outcome)" in stream_js
    assert "Finished (UNVERIFIED) in " in stream_js and "not backed by tool evidence" in stream_js
    assert "edited ' + files + ' file'" in stream_js
    done = CHAT_JS.split("if (data === '[DONE]') {", 1)[1][:2500]
    assert "_hz.stop_reason === 'complete_unverified'" in done
    assert "_insertStreamDoneToast(streamSessionId, streamQuery, _outcome)" in done
