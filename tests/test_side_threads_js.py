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

import re
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
        "studio/src/adapters/condense.ts",
        "studio/src/screens/studio/SideThreadsPanel.tsx",
        "studio/src/screens/studio/ExploreDialog.tsx",
        "studio/src/screens/studio/CondenseDialog.tsx",
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


def test_adapter_gains_materials_and_stale_turns():
    """F1/F2 (CONTRATO_CABLES2): the materials CRUD and the stale-turns read,
    field names kept exactly as `src/side_threads.py` writes them."""
    src = _read("studio/src/adapters/sideThreads.ts")
    for symbol in ("addMaterial", "updateMaterial", "removeMaterial", "getStaleTurns"):
        assert f"export function {symbol}" in src, f"sideThreads.ts must export {symbol}()"
    for field in ("document_id", "note_text", "quotes", "ranges", "stale_turns", "last_index", "materials"):
        assert field in src, f"sideThreads.ts must keep the server's own field name {field!r}"
    assert "/materials" in src
    assert "/stale-turns" in src


def test_condense_adapter_shapes_match_lote_a_exactly():
    """F3 (CONTRATO_CABLES2): `adapters/condense.ts` mirrors `src/condense.py`
    / `routes/condense_routes.py` exactly -- same field names, same flat
    {"error","error_class"} error body as sideThreads.ts."""
    src = _read("studio/src/adapters/condense.ts")
    for symbol in ("previewCondense", "condense", "expandCondensed"):
        assert f"export function {symbol}" in src, f"condense.ts must export {symbol}()"
    for field in ("tokens_before", "tokens_after_estimate", "summary_index", "removed", "restored"):
        assert field in src, f"condense.ts must keep the server's own field name {field!r}"
    assert "/condense/preview" in src
    assert "payload.error" in src
    assert "error_class" in src


def test_chat_ts_load_history_keeps_condensed_rows_and_real_index():
    """F3: `loadHistory` keeps a `system` row only when it is a condensed
    summary, admits `'system'` on `HistoryMessage.role`, and assigns `index`
    BEFORE filtering -- the server's own history position."""
    src = _read("studio/src/adapters/chat.ts")
    assert "'user' | 'assistant' | 'system'" in src
    assert "meta.condensed" in src


def test_studio_uses_the_servers_own_history_index_not_the_filtered_arrays():
    """The bug CONTRATO_CABLES2 names by file:line -- Studio.tsx's own
    turnsFromHistory used to re-number its already-filtered array as
    historyIndex; it must read `m.index` (assigned by loadHistory before ITS
    OWN filter ran) instead, or a condensed row ahead of a later turn shifts
    every truncate/fork/explore/condense action onto the wrong row."""
    src = _read("studio/src/screens/Studio.tsx")
    assert re.search(r"historyIndex:\s*m\.index", src), (
        "Studio.tsx::turnsFromHistory must use m.index as historyIndex"
    )


def test_transcript_gains_stale_wire_banner_and_condense_actions():
    src = _read("studio/src/screens/studio/Transcript.tsx")
    assert 'data-testid="turn-stale-wire"' in src
    assert 'testId="turn-condense"' in src
    assert 'data-testid="turn-condensed"' in src
    assert 'testId="turn-expand"' in src
    assert "turn.role === 'system'" in src


def test_composer_gains_pin_button_for_document_context():
    src = _read("studio/src/screens/studio/Composer.tsx")
    assert 'data-testid="doc-context-pin"' in src
    assert "addMaterial" in src


def test_side_threads_panel_gains_materials_section_and_wire_replay():
    src = _read("studio/src/screens/studio/SideThreadsPanel.tsx")
    assert 'testId="material-add-note"' in src
    assert 'testId="wire-replay"' in src
    assert "onRegenerateTurn" in src
    assert "t('Context wires')" in src


def test_popover_label_renamed_to_context_wires():
    """CONTRATO_CABLES2: the popover trigger moves from "Side threads" to
    "Context wires" -- the panel now covers materials and condensed turns,
    not only excursos."""
    src = _read("studio/src/screens/Studio.tsx")
    assert "t('Context wires')" in src
    assert "t('Side threads')" not in src


def test_es_tsv_carries_the_new_cables2_strings():
    tsv = _read("docs/ui/i18n/es.tsv")
    for en, es in (
        ("Context wires", "Cables de contexto"),
        ("Materials", "Materiales"),
        ("Condense up to here", "Condensar hasta aquí"),
        ("Add a note", "Añadir una nota"),
    ):
        assert f"{en}\t{es}" in tsv, f"es.tsv must map {en!r} -> {es!r}"


def test_i18n_check_passes():
    proc = subprocess.run(
        ["python3", "scripts/i18n_es.py", "--check"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "missing:" not in proc.stdout
