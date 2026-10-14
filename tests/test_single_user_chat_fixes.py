"""Single-user installs: images reach the turn, projects get a folder, Unity
projects get the CLI, closed family apps are detected, bare «Done.» is caught."""
import io
import os

from starlette.datastructures import UploadFile

from src.owner_identity import DEFAULT_LOCAL_OWNER, owners_match
from src.upload_handler import UploadHandler


def _save_image(handler, owner):
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
           b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")
    return handler.save_upload(UploadFile(io.BytesIO(png), filename="ref.png"), "127.0.0.1", owner=owner)


def test_session_owned_by_admin_reads_an_upload_stored_for_the_local_owner(tmp_path, monkeypatch):
    # The desktop app stores uploads as the local owner while old sessions
    # belong to «admin»: every image used to vanish from the turn.
    monkeypatch.setenv("AUTH_ENABLED", "false")
    handler = UploadHandler(str(tmp_path), str(tmp_path / "uploads"))
    saved = _save_image(handler, DEFAULT_LOCAL_OWNER)
    assert handler.resolve_upload(saved["id"], owner="admin") is not None
    assert handler.resolve_upload(saved["id"], owner=None) is not None


def test_with_login_on_another_user_still_cannot_read_the_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    handler = UploadHandler(str(tmp_path), str(tmp_path / "uploads"))
    saved = _save_image(handler, "alice")
    assert handler.resolve_upload(saved["id"], owner="bob") is None
    assert handler.resolve_upload(saved["id"], owner="alice") is not None


def test_owners_match_follows_the_login_mode(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    assert owners_match("admin", DEFAULT_LOCAL_OWNER)
    assert owners_match(None, "admin")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    assert owners_match("alice", "alice")
    assert not owners_match("alice", "bob")
    assert not owners_match("alice", None)


def test_project_without_workspace_gets_a_folder_under_the_projects_root(tmp_path, monkeypatch):
    import services.projects as projects
    root = tmp_path / "Proyectos"
    root.mkdir()
    store = projects.ProjectStore(str(tmp_path / "data"))
    monkeypatch.setattr(projects, "get_store", lambda: store)
    monkeypatch.setattr(projects, "default_projects_root", lambda rows=None: str(root))
    row = store.create("3d terrain tiling", folder="terrain", scaffold_memory=False)
    assert row["workspace"] == ""
    path = projects.ensure_project_workspace(dict(row))
    assert os.path.isdir(path)
    assert os.path.normcase(os.path.dirname(path)) == os.path.normcase(os.path.realpath(str(root)))
    assert os.path.basename(path) == "3d terrain tiling"
    assert store.get(row["id"])["workspace"] == path


def test_default_projects_root_is_the_parent_most_projects_share(tmp_path, monkeypatch):
    import services.projects as projects
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    parent = tmp_path / "Proyectos"
    (parent / "a").mkdir(parents=True)
    (parent / "b").mkdir()
    rows = [{"workspace": str(parent / "a")}, {"workspace": str(parent / "b")}, {"workspace": str(tmp_path)}]
    assert projects.default_projects_root(rows) == str(parent)


def test_engine_block_points_a_unity_workspace_at_the_cli(tmp_path):
    from src import project_instructions
    game = tmp_path / "Game" / "Game"
    (game / "Assets").mkdir(parents=True)
    (game / "ProjectSettings").mkdir()
    (tmp_path / "Game" / "Library").mkdir()
    assert project_instructions.unity_projects(str(tmp_path)) == [os.path.realpath(str(game))]
    block = project_instructions.engine_block(str(tmp_path))
    assert "Unity project in this workspace" in block
    assert "unity status" in block and "unity command eval" in block
    assert project_instructions.engine_block(str(tmp_path / "Game" / "Library")) == ""


def test_closed_family_app_answers_are_recognised():
    from src.mcp_manager import _app_closed_result
    assert _app_closed_result({"output": '{"error":"Abre Ledger\'s Hoard (npm start)","code":"not_running"}'})
    assert _app_closed_result({"output": '{"error": "Writers Hoard is not running, so its data is unreachable.", "code": "app-closed"}'})
    assert not _app_closed_result({"output": '{"subscriptions": []}'})


def test_bare_acknowledgements_are_recognised():
    from src.agent_loop import _is_bare_ack
    for text in ("Done.", "done", "Hecho.", "Listo", "OK", "¡Hecho!", "Ya está."):
        assert _is_bare_ack(text), text
    for text in ("Done: the project is in C:\\x", "Hecho, lo he movido a la carpeta del proyecto."):
        assert not _is_bare_ack(text), text
