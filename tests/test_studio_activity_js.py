"""The live-run signals: the dot in the lists, the rejoin, and Stop.

Seen live (06-09-2026): a turn stopped at a permission gate, the person
answered it and walked to the project root while the continuation ran. The
run kept going server-side — as designed — but nothing in the interface said
so, and coming back showed the saved message ("Allow this task to continue?")
with no card under it. The chat looked hung; it was working.

The backend already had every answer: `/api/chat/activity` (the dots),
`/api/chat/resume` (rejoin a detached run) and `/api/chat/stop`. None of them
had a caller — the failure mode PARIDAD_FUNCIONAL §7 warns about: an endpoint
with no caller is not half a feature, it is a missing one, and it looks
exactly like a feature that never existed. These tests keep the callers.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "studio" / "src"
_CHECK = _REPO / "studio" / "checks" / "activity.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()


def _sources() -> dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for path in _SRC.rglob("*.ts*")
        if path.suffix in (".ts", ".tsx")
    }


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_studio_activity_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout


@pytest.mark.parametrize(
    "endpoint",
    ["/api/chat/activity", "/api/chat/resume/", "/api/chat/stop/"],
)
def test_the_live_run_endpoints_have_a_caller(endpoint):
    """Each of these was built, wired to nothing, and forgotten for weeks."""
    callers = [p.name for p, text in _sources().items() if endpoint in text]
    assert callers, f"{endpoint} has no caller in studio/src: the feature is gone"


def test_stop_sends_the_run_id_back():
    """`agent_runs.stop()` is fail-closed: no `X-Odysseus-Run-Id`, no cancel.

    Stop used to POST without it, so it always answered `stopped: false`. The
    stream closed in the browser and the model kept generating — a button
    that only hid the evidence."""
    chat = (_SRC / "adapters" / "chat.ts").read_text(encoding="utf-8")
    assert "X-Odysseus-Run-Id" in chat
    body = chat[chat.index("export async function stopChat"):]
    body = body[: body.index("\n}\n") + 3]
    assert "RUN_ID_HEADER" in body, "stopChat must send the run id header"
    assert "runId" in body


def test_the_screen_captures_the_run_id_and_rejoins():
    studio = (_SRC / "screens" / "Studio.tsx").read_text(encoding="utf-8")
    assert "onRunId" in studio, "without the header the screen has no run id to stop"
    assert "resumeTurn" in studio, "opening a session must rejoin a run still going"
    # The rejoin has to happen after the history load, not instead of it.
    assert studio.index("turnsFromHistory(sessionId") < studio.index("rejoinRef.current(sessionId)")


def test_open_studio_follows_external_activity_without_reload():
    """MCP can start a turn on a conversation already open in the browser.

    The sidebar poll already saw the run (#941 repro): Thinking on, Send
    disabled. The transcript stayed on the previous messages until a full
    reload, because rejoin only ran from the history-load effect and
    sessionId never changed. Studio must subscribe to the shared activity
    snapshot and rejoin on idle→live for the open session — resume only,
    no new Send/POST. Marks carry runId + session watermarks so a whole
    turn between idle polls (or a new run id while still running) still
    refreshes; external rejoin is adoptOnly (no outbox Send).
    """
    studio = (_SRC / "screens" / "Studio.tsx").read_text(encoding="utf-8")
    assert "useChatActivity" in studio
    assert "sessionActivity" in studio
    assert "externalFollowPrevRef" in studio
    assert "mayApplyExternalFollow" in studio
    assert "externalFollowGateRef" in studio
    # CAS5+ plans follow transitions (defer while a controller drains) instead of
    # calling decideExternalFollowAction directly from the effect.
    assert "planExternalFollowTransition" in studio
    assert "externalFollowKick" in studio or "bumpExternalFollow" in studio
    assert "openFollowMark" in studio
    assert "adoptOnly: true" in studio
    assert studio.count("rejoinRef.current(") >= 2
    shell = (_SRC / "shell" / "activity.ts").read_text(encoding="utf-8")
    assert "#941" in shell or "external MCP" in shell


_EXT_FOLLOW_CHECK = _REPO / "studio" / "checks" / "external-follow.check.mjs"
_EXT_ADOPT_CHECK = _REPO / "studio" / "checks" / "external-follow-adopt.check.mjs"


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_external_follow_generation_gate():
    """Deferred idle→live must not overwrite a later live→idle refresh."""
    proc = subprocess.run(
        ["node", str(_EXT_FOLLOW_CHECK)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(_REPO),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_external_follow_adopt_only_empty_resume():
    """External empty resume must not reconcileOutbox→sendTurn (CAS2)."""
    proc = subprocess.run(
        ["node", str(_EXT_ADOPT_CHECK)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(_REPO),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
    assert "adoptOnly empty resume must sendTurn 0 times" in proc.stdout


def test_an_answered_permission_is_not_a_question():
    """The gate's question is saved as the assistant message's whole text.

    Rendered as prose it reads as a question with no buttons — which is what
    "it froze" looked like. Once the gate is answered the turn says so."""
    model = (_SRC / "screens" / "studio" / "model.ts").read_text(encoding="utf-8")
    assert "approval = { question:" in model
    assert re.search(r"turn\.text\.trim\(\)\s*===\s*approval\.question\.trim\(\)", model)
    transcript = (_SRC / "screens" / "studio" / "Transcript.tsx").read_text(encoding="utf-8")
    assert "AnsweredCard" in transcript


def test_only_one_poller_for_the_dots():
    """One shared store, not a request per row: the lists show dozens."""
    shell = (_SRC / "shell" / "activity.ts").read_text(encoding="utf-8")
    assert "watchers" in shell and "visibilitychange" in shell
    users = [p.name for p, text in _sources().items() if "useChatActivity" in text and p.name != "activity.ts"]
    assert len(users) >= 3, f"the dot should be in the three lists, found: {users}"
    # Nobody but the store may call the endpoint's adapter.
    callers = [p.name for p, text in _sources().items() if "chatActivity(" in text]
    assert set(callers) <= {"activity.ts", "chat.ts"}, callers
