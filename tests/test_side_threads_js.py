"""Lote B (CONTRATO_EXCURSOS.md) -- excursos (side threads), Studio side.

The adapter's pure helpers (`studio/src/adapters/sideThreads.ts`:
`layerSummaryLine`, `formatTokenCount`, `sideThreadName`, `formatAnchor`,
`indentSessions`) and the contract's own named wiring checks -- Transcript.tsx
carries `explore-selection`, Studio.tsx imports `SideThreadsPanel`, and the
adapter's sole `method: 'DELETE'` is scoped under `/references/` -- are all
exercised by `studio/checks/side_threads.check.mjs`, run the same way
`tests/test_l89_git_merge_js.py` runs lote 89's own `.check.mjs`.

The rest here is static source inspection against the files directly, same
pattern lote 89's own wiring tests use.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "side_threads.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def test_side_threads_check_js_passes():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all checks passed" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_new_studio_files_present():
    for rel in (
        "studio/src/adapters/sideThreads.ts",
        "studio/src/screens/studio/SideThreadsPanel.tsx",
        "studio/src/screens/studio/ExploreDialog.tsx",
        "studio/checks/side_threads.check.mjs",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"


def test_adapter_shapes_match_lote_a_exactly():
    """B1: field names kept exactly as routes/side_thread_routes.py /
    src/side_threads.py write them -- no re-keying, no re-shaping."""
    src = _read("studio/src/adapters/sideThreads.ts")
    for symbol in (
        "createSideThread", "getWires", "getThoughtMap", "getContextPreview",
        "addReference", "updateReference", "removeReference", "getParentsMap",
    ):
        assert f"export function {symbol}" in src, f"sideThreads.ts must export {symbol}()"
    for field in (
        "anchor_index", "anchor_passage", "source_session_id", "target_session_id",
        "context_order", "source_fingerprint", "anchor_state", "message_count",
    ):
        assert field in src, f"sideThreads.ts must keep the server's own field name {field!r}"
    assert "/side-threads" in src
    assert "/thought-map" in src
    assert "/context-preview" in src
    assert "/api/side-threads/parents" in src
    # The flat {"error","error_class"} body, not FastAPI's usual {"detail"}.
    assert "payload.error" in src
    assert "error_class" in src


def test_transcript_gains_explore_actions_beside_cite_and_fork():
    src = _read("studio/src/screens/studio/Transcript.tsx")
    assert 'data-testid="explore-selection"' in src
    assert 'testId="turn-explore"' in src
    assert "onExplore" in src
    # The floating selection group now carries both Citar and Explorar --
    # the single-button wrapper became a small group, not a second overlay.
    assert "fs-studio__quote-btn" in src


def test_studio_wires_panel_dialog_and_explore_without_touching_sidepanel():
    src = _read("studio/src/screens/Studio.tsx")
    assert "SideThreadsPanel" in src
    assert "ExploreDialog" in src
    assert "exploreAnchor" in src
    assert "onExplore={onExplore}" in src
    # Placement decision: a header Popover, not a SidePanel.tsx tab (that
    # file is not one of this lote's own -- see SideThreadsPanel.tsx's own
    # header comment for the full reasoning).
    assert "Popover" in src
    sidepanel_src = _read("studio/src/screens/studio/SidePanel.tsx")
    assert "SideThreadsPanel" not in sidepanel_src, "SidePanel.tsx is a fichero ajeno to Lote B -- must stay untouched by this feature"


def test_sessions_pane_indents_children_under_their_parent():
    src = _read("studio/src/screens/studio/SessionsPane.tsx")
    assert "getParentsMap" in src
    assert "indentSessions" in src
    # B5: a failed fetch must never block the list.
    assert ".catch(() => undefined)" in src


def test_side_threads_panel_documents_its_placement_choice():
    src = _read("studio/src/screens/studio/SideThreadsPanel.tsx")
    assert 'data-testid="side-threads-panel"' in src
    assert 'testId="bring-back"' in src
    assert 'testId="wire-remove"' in src
    assert 'testId="wire-remove-confirm"' in src
    # The contract's own placement instruction, answered in the header
    # comment (CONTRATO_EXCURSOS.md, "documenta la elección").
    assert "PLACEMENT" in src
    assert "SidePanel.tsx" in src


def test_i18n_table_carries_every_new_es_string():
    tsv = _read("docs/ui/i18n/es.tsv")
    for en, es in (
        ("Explore separately", "Explorar aparte"),
        ("Explore from here", "Explorar desde aquí"),
        ("Bring back", "Traer de vuelta"),
        ("Wire", "Cablear"),
        ("Remove wire", "Quitar cable"),
        ("Withdraw", "Retirar"),
    ):
        assert f"{en}\t{es}" in tsv, f"es.tsv must map {en!r} -> {es!r}"


def test_i18n_check_passes():
    proc = subprocess.run(
        ["python3", "scripts/i18n_es.py", "--check"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "missing:" not in proc.stdout
