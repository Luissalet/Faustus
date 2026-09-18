"""Projects: the store, its guards, and the two chat-path hooks.

The hooks are what actually matter — a project that does not reach the model's
system prompt, or that fails to confine the file tools, is decoration. Those are
asserted against the real functions in routes/, not re-implemented here.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.projects import ProjectError, ProjectStore  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    return ProjectStore(str(tmp_path / "data"))


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "covernet"
    ws.mkdir()
    return str(ws)


# ── store basics ──────────────────────────────────────────────────────


def test_create_round_trips_through_json(store, workspace):
    store.create("Covernet", workspace=workspace, instructions="Reglas.")
    fresh = ProjectStore(os.path.dirname(store.path))
    assert [p["name"] for p in fresh.list()] == ["Covernet"]
    assert fresh.list()[0]["instructions"] == "Reglas."


def test_folder_defaults_to_name(store, workspace):
    p = store.create("Covernet", workspace=workspace)
    assert p["folder"] == "Covernet"


def test_folder_lookup_is_case_insensitive(store, workspace):
    # Folder names are typed by hand in the sidebar; 'Covernet' and 'covernet'
    # must not resolve to two different projects.
    store.create("Covernet", folder="Covernet", workspace=workspace)
    assert store.get_by_folder("COVERNET")["name"] == "Covernet"


def test_one_folder_cannot_belong_to_two_projects(store, workspace):
    store.create("Covernet", folder="Work", workspace=workspace)
    with pytest.raises(ProjectError):
        store.create("Otro", folder="work", workspace=workspace)


def test_workspace_is_vetted_like_a_manual_bind(store, tmp_path):
    root = os.path.abspath(os.sep)
    with pytest.raises(ProjectError):
        store.create("Root", workspace=root)          # filesystem root
    with pytest.raises(ProjectError):
        store.create("Ghost", workspace=str(tmp_path / "does-not-exist"))
    f = tmp_path / "a-file.txt"
    f.write_text("x")
    with pytest.raises(ProjectError):
        store.create("File", workspace=str(f))        # not a directory


def test_folder_name_rejects_path_separators(store, workspace):
    for bad in ("a/b", "a\\b", "a:b"):
        with pytest.raises(ProjectError):
            store.create("X", folder=bad, workspace=workspace)


def test_delete_forgets_the_binding_not_the_files(store, workspace):
    p = store.create("Covernet", workspace=workspace)
    store.write_memory_file(p, "notas.md", "contenido")
    assert store.delete(p["id"]) is True
    assert store.list() == []
    # The folder and its memory are the user's work, not ours to remove.
    assert os.path.isfile(os.path.join(workspace, ".odysseus", "notas.md"))


def test_legacy_rows_gain_project_organisation_defaults(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "projects.json").write_text(
        '[{"id":"old","name":"Legacy","folder":"Legacy"}]',
        encoding="utf-8",
    )
    project = ProjectStore(str(data)).list()[0]
    assert project["pinned"] is False
    assert project["archived"] is False


def test_archiving_unpins_and_pinning_restores(store, workspace):
    project = store.create("Covernet", workspace=workspace)
    project = store.update(project["id"], {"pinned": True})
    assert project["pinned"] is True
    assert project["archived"] is False

    project = store.update(project["id"], {"archived": True})
    assert project["archived"] is True
    assert project["pinned"] is False

    project = store.update(project["id"], {"pinned": True})
    assert project["pinned"] is True
    assert project["archived"] is False


def test_archived_project_still_provides_context(store, workspace):
    project = store.create("Covernet", workspace=workspace, instructions="Keep this.")
    project = store.update(project["id"], {"archived": True})
    assert "Keep this." in store.system_block(project)


def test_touch_refreshes_project_activity(store, workspace, monkeypatch):
    project = store.create("Covernet", workspace=workspace)
    monkeypatch.setattr("services.projects._now", lambda: project["updated_at"] + 20)
    touched = store.touch(project["id"])
    assert touched["updated_at"] == project["updated_at"] + 20


def test_project_work_roots_round_trip_and_cannot_escape(store, workspace, tmp_path):
    project = store.create("Covernet", workspace=workspace)
    extra = tmp_path / "reference"
    extra.mkdir()
    source = extra / "brief.txt"
    source.write_text("first\nimportant decision\nthird", encoding="utf-8")

    item = store.add_context_item(project["id"], str(extra))
    project = store.get(project["id"])
    assert item["kind"] == "folder"
    assert project["context_items"][0]["path"] == str(extra)
    assert "important decision" in store.read_context_file(project, item["id"], "brief.txt")["content"]
    assert store.search_context(project, "decision")["matches"][0]["path"] == "brief.txt"
    with pytest.raises(ProjectError):
        store.read_context_file(project, item["id"], "../outside.txt")


def test_system_block_explains_editable_work_roots_and_project_chat_search(store, workspace, tmp_path):
    project = store.create("Covernet", workspace=workspace)
    extra = tmp_path / "docs"
    extra.mkdir()
    store.add_context_item(project["id"], str(extra))
    block = store.system_block(store.get(project["id"]))
    assert "Project work roots" in block
    assert "normal file tools" in block
    assert "search_project_chats" in block


# ── memory files ──────────────────────────────────────────────────────


def test_memory_is_scaffolded_inside_the_workspace(store, workspace):
    store.create("Covernet", workspace=workspace)
    assert os.path.isfile(os.path.join(workspace, ".odysseus", "MEMORY.md"))


def test_memory_filenames_cannot_escape_the_memory_dir(store, workspace):
    p = store.create("Covernet", workspace=workspace)
    for bad in ("../escape.md", "sub/x.md", "..\\escape.md", "notes.txt", ""):
        with pytest.raises(ProjectError):
            store.write_memory_file(p, bad, "pwned")
    assert not os.path.exists(os.path.join(os.path.dirname(workspace), "escape.md"))


def test_memory_round_trips(store, workspace):
    p = store.create("Covernet", workspace=workspace)
    store.write_memory_file(p, "decisiones.md", "# Decisiones\nqwen3-coder-next.\n")
    assert "qwen3-coder-next" in store.read_memory_file(p, "decisiones.md")
    names = [f["name"] for f in store.list_memory_files(p)]
    assert names[0] == "MEMORY.md"            # index first
    assert "decisiones.md" in names
    assert "](decisiones.md)" in store.read_memory_file(p, "MEMORY.md")


def test_reading_a_missing_memory_file_is_empty_not_an_error(store, workspace):
    p = store.create("Covernet", workspace=workspace)
    assert store.read_memory_file(p, "nope.md") == ""


# ── the injected block ────────────────────────────────────────────────


def test_system_block_carries_folder_instructions_and_index(store, workspace):
    p = store.create("Covernet", workspace=workspace, instructions="Responde en castellano.")
    store.write_memory_file(p, "MEMORY.md", "- [Decisiones](decisiones.md) - por que qwen")
    block = store.system_block(p)
    assert workspace in block
    assert "Responde en castellano." in block
    assert "decisiones.md" in block
    assert block.startswith("<project_context>") and block.endswith("</project_context>")


def test_system_block_is_stable_across_calls(store, workspace):
    """The block sits in the system prompt, and local backends key their KV
    cache off a byte-identical prefix. Anything per-turn in here would
    invalidate that cache on every message."""
    p = store.create("Covernet", workspace=workspace, instructions="Reglas.")
    assert store.system_block(p) == store.system_block(p)


def test_disabled_project_contributes_nothing(store, workspace):
    p = store.create("Covernet", workspace=workspace, instructions="Reglas.")
    p = store.update(p["id"], {"enabled": False})
    assert store.system_block(p) == ""


def test_long_index_is_truncated_with_a_marker(store, workspace):
    from services.projects import MAX_INDEX_INJECT

    p = store.create("Covernet", workspace=workspace)
    store.write_memory_file(p, "MEMORY.md", "x" * (MAX_INDEX_INJECT + 5000))
    block = store.system_block(p)
    assert "truncated here" in block
    assert len(block) < MAX_INDEX_INJECT + 4000


# ── the chat-path hooks ───────────────────────────────────────────────


def test_project_instructions_are_prepended_to_the_preset():
    """Project first, preset second: the project is ground truth about the
    work, the preset is a style knob."""
    from routes.chat_helpers import project_system_prompt

    class _Sess:
        id = "sess-1"

    import services.projects as projects_mod
    original = projects_mod.instructions_for_session
    projects_mod.instructions_for_session = lambda sid, owner=None: "<project_context>P</project_context>"
    try:
        out = project_system_prompt(_Sess(), "luis", "PRESET")
        assert out.startswith("<project_context>P</project_context>")
        assert out.endswith("PRESET")
        # No preset at all -> just the project block, no stray blank lines.
        assert project_system_prompt(_Sess(), "luis", None) == "<project_context>P</project_context>"
    finally:
        projects_mod.instructions_for_session = original


def test_chat_without_a_project_is_left_exactly_as_before():
    from routes.chat_helpers import project_system_prompt

    class _Sess:
        id = "orphan"

    import services.projects as projects_mod
    original = projects_mod.instructions_for_session
    projects_mod.instructions_for_session = lambda sid, owner=None: ""
    try:
        assert project_system_prompt(_Sess(), "luis", "PRESET") == "PRESET"
        assert project_system_prompt(_Sess(), "luis", None) is None
    finally:
        projects_mod.instructions_for_session = original


def test_a_broken_store_costs_context_not_the_message():
    """A corrupt projects.json must degrade to 'no project', never raise into
    the chat handler."""
    from routes.chat_helpers import project_system_prompt

    class _Sess:
        id = "sess-1"

    import services.projects as projects_mod
    original = projects_mod.instructions_for_session

    def _boom(sid, owner=None):
        raise RuntimeError("projects.json is a banana")

    projects_mod.instructions_for_session = _boom
    try:
        assert project_system_prompt(_Sess(), "luis", "PRESET") == "PRESET"
    finally:
        projects_mod.instructions_for_session = original


def test_project_workspace_is_revetted_at_use_time(monkeypatch, tmp_path):
    """The stored path was vetted when the project was saved, but a folder can
    be deleted (or replaced by a symlink) afterwards. A stale path must not be
    handed to the tool layer as a live confinement root."""
    from routes import chat_routes

    gone = str(tmp_path / "deleted-since")
    monkeypatch.setattr(
        "src.tool_security.owner_is_admin_or_single_user", lambda owner: True
    )
    monkeypatch.setattr(chat_routes, "get_current_user", lambda request: "luis")
    monkeypatch.setattr(
        "services.projects.workspace_for_session", lambda sid, owner=None: gone
    )
    assert chat_routes._project_workspace(object(), "sess-1") == ""


def test_non_admin_gets_no_project_workspace(monkeypatch, tmp_path):
    from routes import chat_routes

    ws = tmp_path / "real"
    ws.mkdir()
    monkeypatch.setattr(
        "src.tool_security.owner_is_admin_or_single_user", lambda owner: False
    )
    monkeypatch.setattr(chat_routes, "get_current_user", lambda request: "someone")
    monkeypatch.setattr(
        "services.projects.workspace_for_session", lambda sid, owner=None: str(ws)
    )
    assert chat_routes._project_workspace(object(), "sess-1") == ""


def test_no_session_means_no_project_workspace():
    from routes import chat_routes

    assert chat_routes._project_workspace(object(), "") == ""
    assert chat_routes._project_workspace(object(), None) == ""
