"""Lote 70a, punto A.14 (Studio half) — `Studio.tsx`'s `readDraftFor`/
`writeDraftFor` now also sync with `GET/PUT /api/sessions/{sid}/draft`
(`studio/src/adapters/session-draft.ts`, backed by `src/session_draft.py` and
tested at the route level by `tests/test_l70_a14_session_draft_route.py`):
debounced 1s on write, most-recent-wins merge on load, skipped for a chat
that has no saved session yet and for Nobody mode.

`studio/checks/l70-a14-session-draft-sync.check.mjs` exercises the adapter's
wire shape at runtime; the wiring-into-Studio.tsx checks below are a plain
static source read (same approach as
`tests/test_p1_a11y02_transcript_wiring.py`) so they run without node.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "l70-a14-session-draft-sync.check.mjs"
_STUDIO = (_REPO / "studio" / "src" / "screens" / "Studio.tsx").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_session_draft_adapter_check_js_passes():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_studio_imports_the_session_draft_adapter():
    assert "loadSessionDraft" in _STUDIO
    assert "saveSessionDraft" in _STUDIO
    assert "'../adapters/session-draft'" in _STUDIO


def test_the_server_merge_is_most_recent_wins_not_a_blind_overwrite():
    assert "remote.updatedAt * 1000 > localAt" in _STUDIO


def test_the_server_write_is_debounced_a_second():
    assert "}, 1000);" in _STUDIO
    assert "clearTimeout(timer)" in _STUDIO


def test_nobody_mode_and_an_unsaved_chat_never_reach_the_server():
    # Both the load-merge and the debounced-write effects guard on this.
    assert _STUDIO.count("if (!sessionId || knobs.incognito) return;") >= 2
