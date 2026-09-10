"""Lote 54 (P1) - BENCH-01: "Espacio de trabajo persistente".

`studio/src/screens/studio/panel.ts` (reducer) and `panel-storage.ts`
(read/persist) already carried the logic - a stable per-conversation key,
drafts that block eviction, opening a file that never touches an open
doc/diff - but the real, deployed wiring in `useChatPanel.ts` persisted to
`sessionStorage`, which is cleared the moment the tab closes. That is not a
"persistent workspace" by BENCH-01's own acceptance ("abrir una fuente o
imagen no cierra el diff con cambios sin guardar ni pierde el borrador") -
a browser/tab restart is exactly the case a person expects their draft to
survive. This lote switches the wiring to `localStorage` and closes the gap
`docs/spec/v2/MAPA_P1.md` recorded for BENCH-01.

Two tiers, same pattern as the other p1-*.check.mjs files:
  1. The pure reducer/storage functions, via
     studio/checks/p1-bench01-panel-persistence.check.mjs (run below).
  2. A static check on the real deployed useChatPanel.ts (same approach as
     tests/qa/test_qa_32_preview_malicioso.py / test_p1_bench04_sidepanel_wiring.py)
     - reverting the wiring fails this.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "p1-bench01-panel-persistence.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_panel_persistence_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout


_USE_CHAT_PANEL = (_REPO / "studio" / "src" / "screens" / "studio" / "useChatPanel.ts").read_text(encoding="utf-8")


def test_the_deployed_wiring_persists_to_localstorage_not_sessionstorage():
    """The whole point of BENCH-01: the draft/doc/file has to outlive the tab
    closing, not just a navigation within it."""
    assert "readPanel(localStorage" in _USE_CHAT_PANEL
    assert "persistPanels(localStorage" in _USE_CHAT_PANEL
    # Only the explanatory comment may still say the word; no code call does.
    assert "(sessionStorage" not in _USE_CHAT_PANEL


def test_the_key_is_stable_per_conversation_independent_of_navigation():
    assert "(privateMode?'private:':'')+(sessionId||'new')" in _USE_CHAT_PANEL


def test_incognito_mode_never_persists():
    assert "if(privateMode)return {...initialPanel};" in _USE_CHAT_PANEL


def test_an_unsaved_draft_still_warns_before_the_tab_closes():
    """localStorage removes the "gone the moment the tab closes" risk, but a
    person can still lose in-flight typing to an accidental close before the
    next persist effect runs - the existing beforeunload guard stays."""
    assert "beforeunload" in _USE_CHAT_PANEL
    assert "event.preventDefault()" in _USE_CHAT_PANEL
