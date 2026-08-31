"""Static contracts for the first-class Projects workspace."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_projects_uses_the_chat_canvas_not_the_legacy_memory_modal():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'class="modal-content projects-modal-content"' in html
    assert 'id="projects-active-tab"' in html
    assert 'id="projects-archived-tab"' in html
    assert 'id="project-active-pill"' in html


def test_project_hub_exposes_the_core_claude_style_workflow():
    js = (ROOT / "static" / "js" / "projects.js").read_text(encoding="utf-8")
    for contract in (
        'id="project-chat-input"',
        'id="project-start-chat"',
        'id="project-model-select"',
        "form.append('endpoint_id', choice.endpointId)",
        'id="project-memory-list"',
        'id="project-context-preview"',
        'id="project-edit-instructions"',
        'id="project-add-context"',
        'includeFiles: true',
        'read and modify every file or folder',
        'archived: false',
        'pinned: false',
    ):
        assert contract in js


def test_existing_project_folder_is_stable_when_renamed():
    js = (ROOT / "static" / "js" / "projects.js").read_text(encoding="utf-8")
    assert "folder: isNew ? name : _draft.folder" in js
