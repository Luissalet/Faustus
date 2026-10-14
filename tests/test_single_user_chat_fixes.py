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


def test_app_owned_copilot_projects_never_get_a_folder(tmp_path, monkeypatch):
    import services.projects as projects
    monkeypatch.setattr(projects, "default_projects_root", lambda rows=None: str(tmp_path))
    copilot = {"id": "x", "name": "QA Dragon", "folder": "gepetto · QA Dragon · 81c36b8f", "workspace": ""}
    assert projects.is_app_owned_project(copilot)
    assert projects.ensure_project_workspace(dict(copilot)) == ""
    assert not (tmp_path / "QA Dragon").exists()
    assert not projects.is_app_owned_project({"name": "Blood Arrows", "folder": "Blood Arrows"})


def test_codex_direct_calls_get_real_query_defaults():
    # /api/codex/emails answered 500: the email list endpoint was called as a
    # plain coroutine and `since` arrived as the Query object.
    import asyncio

    from fastapi import APIRouter, Query

    from routes.codex_routes import _find_endpoint

    router = APIRouter()

    @router.get("/api/email/list")
    async def list_emails(folder: str = Query("INBOX"), since: str | None = Query(None), owner: str = "x"):
        return {"folder": folder, "since": since, "owner": owner}

    endpoint = _find_endpoint(router, "GET", "/api/email/list")
    out = asyncio.run(endpoint(owner="me"))
    assert out == {"folder": "INBOX", "since": None, "owner": "me"}
    assert asyncio.run(endpoint(folder="Sent", owner="me"))["folder"] == "Sent"


def test_codex_finds_routes_inside_included_routers():
    # Newer FastAPI nests every include_router: /api/codex/cookbook/cached and
    # the serve wrappers answered 503 «endpoint unavailable» forever.
    import asyncio

    from fastapi import APIRouter, FastAPI

    from routes.codex_routes import _find_app_endpoint

    app = FastAPI()
    flat = APIRouter()
    nested = APIRouter()

    @flat.get("/api/model/cached")
    async def cached(host: str | None = None):
        return {"host": host}

    @nested.post("/serve")
    async def serve():
        return "served"

    app.include_router(flat)
    app.include_router(nested, prefix="/api/model")
    endpoint = _find_app_endpoint(app, "GET", "/api/model/cached")
    assert endpoint is not None and asyncio.run(endpoint(host="h")) == {"host": "h"}
    assert asyncio.run(_find_app_endpoint(app, "POST", "/api/model/serve")()) == "served"
    assert _find_app_endpoint(app, "GET", "/api/model/serve") is None


def test_single_user_desktop_reaches_cookbook_packages_and_push(monkeypatch):
    from types import SimpleNamespace

    import pytest
    from fastapi import HTTPException

    from routes.push_routes import _push_owner
    from routes.shell_routes import _require_admin

    class _Auth:
        is_configured = True

        def is_admin(self, user):
            return user == "admin"

    def _request(user=None):
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(auth_manager=_Auth())),
                               state=SimpleNamespace(current_user=user), headers={})

    monkeypatch.setenv("AUTH_ENABLED", "false")
    _require_admin(_request())  # no login: the person at the desktop is the owner
    assert _push_owner(_request()) == DEFAULT_LOCAL_OWNER
    with pytest.raises(HTTPException):
        _require_admin(_request("api"))  # bearer pseudo-user stays out
    monkeypatch.setenv("AUTH_ENABLED", "true")
    with pytest.raises(HTTPException):
        _require_admin(_request())
    with pytest.raises(HTTPException):
        _require_admin(_request("bob"))
    _require_admin(_request("admin"))


def test_single_user_push_events_reach_the_desktop_devices(monkeypatch):
    from src import push

    seen = []
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(push, "list_subscriptions", lambda owner: seen.append(owner) or [])
    push._push_sink({"owner": "admin", "kind": "x"})
    push._push_sink({"kind": "x"})
    assert seen == [DEFAULT_LOCAL_OWNER, DEFAULT_LOCAL_OWNER]
    monkeypatch.setenv("AUTH_ENABLED", "true")
    seen.clear()
    push._push_sink({"owner": "alice", "kind": "x"})
    push._push_sink({"kind": "x"})
    assert seen == ["alice"]


def test_closed_local_ollama_is_started_before_the_call(monkeypatch):
    import asyncio

    from src import engine_swap, ollama_autostart

    monkeypatch.setenv("FAUSTUS_TEST_OLLAMA_AUTOSTART", "1")
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setattr(ollama_autostart, "_last_failure", 0.0)
    monkeypatch.setattr(ollama_autostart, "find_binary", lambda: "ollama")
    state = {"open": False, "spawned": 0}
    monkeypatch.setattr(ollama_autostart, "_port_open", lambda port, timeout=0.3: state["open"])

    def _spawn(binary):
        state["spawned"] += 1
        state["open"] = True

    monkeypatch.setattr(ollama_autostart, "_spawn", _spawn)
    monkeypatch.setattr(engine_swap, "engine_for_url", lambda url: None)

    assert ollama_autostart.is_local_ollama_url("http://127.0.0.1:11434/v1")
    assert not ollama_autostart.is_local_ollama_url("http://192.168.1.5:11434/v1")
    assert not ollama_autostart.is_local_ollama_url("http://127.0.0.1:8080/v1")
    assert asyncio.run(engine_swap.ensure_ready("http://127.0.0.1:11434/v1"))["action"] == "started"
    assert asyncio.run(engine_swap.ensure_ready("http://127.0.0.1:11434/v1"))["action"] == "none"
    assert state["spawned"] == 1
    assert engine_swap.recoverable_url("http://127.0.0.1:11434/v1")
    assert not engine_swap.recoverable_url("https://api.example.com/v1")


def test_ollama_is_never_started_from_a_test_run_by_default(monkeypatch):
    import asyncio

    from src import ollama_autostart

    monkeypatch.delenv("FAUSTUS_TEST_OLLAMA_AUTOSTART", raising=False)
    monkeypatch.setattr(ollama_autostart, "_spawn", lambda binary: (_ for _ in ()).throw(AssertionError("spawned")))
    assert asyncio.run(ollama_autostart.ensure("http://127.0.0.1:11434/v1")) == {"action": "none"}


def test_an_emptied_local_ollama_is_believed(monkeypatch):
    # The models folder was emptied and the picker kept offering eight deleted
    # models (and one as the automatic vision model) because an empty list
    # was always read as a failed probe.
    from types import SimpleNamespace

    import routes.model_routes as mr

    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    answers = {"models": []}
    seen = []

    def _get(url, timeout=None):
        seen.append(url)
        return SimpleNamespace(status_code=200, json=lambda: answers)

    monkeypatch.setattr(mr.httpx, "get", _get)
    assert mr._local_ollama_confirms_empty("http://127.0.0.1:11434/v1")
    assert seen == ["http://127.0.0.1:11434/api/tags"]
    answers["models"] = [{"name": "qwen"}]
    assert not mr._local_ollama_confirms_empty("http://127.0.0.1:11434/v1")
    assert not mr._local_ollama_confirms_empty("http://192.168.0.5:11434/v1")
    assert not mr._local_ollama_confirms_empty("http://127.0.0.1:8081/v1")


def test_an_engine_whose_model_file_moved_says_so_and_does_not_start(tmp_path, monkeypatch):
    # The 27B engine pointed at a deleted model-store blob: it only failed
    # inside llama-server. Now the engine reports it and refuses to start.
    import asyncio

    from src import engines

    real = tmp_path / "model.gguf"
    real.write_bytes(b"GGUF")
    gone = str(tmp_path / "blobs" / "sha256-gone")
    monkeypatch.setattr(engines, "_parse_fields", lambda profile: dict(profile))
    monkeypatch.setattr(engines, "mtp_supported_for", lambda path: None)
    monkeypatch.setattr(engines, "find_mmproj", lambda path: None)
    moved = engines._decorate({"model_path": gone, "mmproj_path": str(real), "port": 8081})
    assert moved["model_missing"] is True and moved["mmproj_missing"] is False
    fine = engines._decorate({"model_path": str(real), "mmproj_path": "", "port": 8081})
    assert fine["model_missing"] is False and fine["mmproj_missing"] is False

    monkeypatch.setattr(engines, "get_engine", lambda engine_id: moved)
    out = asyncio.run(engines.start_engine("x"))
    assert out["started"] is False and out["code"] == "model_missing"
    assert gone in out["error"]
