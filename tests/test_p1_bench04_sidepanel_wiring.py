"""Lote 49b (P1) - BENCH-04: a 409 from `PUT /api/workspace/file`
(routes/workspace_routes.py::save_workspace_file, the base_revision conflict
check from lote 19/EDIT-01) must open a three-way view in the workbench's
FileTab, never a bare error that drops the draft.

Static source check on the real deployed SidePanel.tsx (same approach as
tests/qa/test_qa_32_preview_malicioso.py) — reverting the wiring fails this.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SIDE_PANEL = (REPO_ROOT / "studio" / "src" / "screens" / "studio" / "SidePanel.tsx").read_text(encoding="utf-8")


def test_a_409_opens_the_conflict_view_instead_of_a_bare_error():
    assert "e.status===409" in SIDE_PANEL or "e.status === 409" in SIDE_PANEL
    assert "setConflict(" in SIDE_PANEL


def test_the_draft_text_about_to_be_saved_is_preserved_in_the_conflict_not_discarded():
    # `conflict.mine` is set from the exact `text` that was about to be
    # written — never reset to the server's content on a 409.
    assert "setConflict({mine:text" in SIDE_PANEL


def test_both_resolutions_are_offered_not_a_forced_overwrite():
    assert "Keep mine (overwrite)" in SIDE_PANEL
    assert "Take the newer version" in SIDE_PANEL


def test_conflict_view_replaces_the_editor_not_the_draft_state():
    """While a conflict is open, the plain textarea editor is hidden (so the
    person resolves it deliberately) but nothing about the draft itself is
    cleared until they pick a resolution."""
    assert "editable&&!conflict&&(preview?" in SIDE_PANEL
