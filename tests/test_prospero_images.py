"""No models or sockets: fake HTTP plus real SQLite receipts and gallery."""
import asyncio
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src import database, plugin_runtime, settings
from src import prospero_images as adapter


def png(width=3, height=2, color="blue"):
    stream = io.BytesIO()
    Image.new("RGB", (width, height), color).save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def world(tmp_path, monkeypatch):
    data, images = tmp_path / "state", tmp_path / "images"
    monkeypatch.setattr(adapter, "DATA_DIR", str(data))
    monkeypatch.setattr(adapter, "GENERATED_IMAGES_DIR", str(images))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"users": {"alice": {"is_admin": False, "privileges": {"can_generate_images": True}},
        "bob": {"is_admin": True}}}), encoding="utf-8")
    monkeypatch.setattr(adapter, "AUTH_FILE", str(auth))
    monkeypatch.setattr(settings, "get_user_setting", lambda key, owner, default: config.get("enabled", True))
    monkeypatch.setattr(adapter, "POLL_TIMEOUT_S", 0)
    engine = create_engine("sqlite:///" + str(tmp_path / "gallery.db"))
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        for sid, owner in [("session-one", "alice"), ("session-two", "bob")]:
            db.add(database.Session(id=sid, owner=owner, name="Synthetic", endpoint_url="", model=""))
        db.commit()
    monkeypatch.setattr(database, "SessionLocal", sessions)
    connection = {"id": "connection", "preset_id": "prospero", "owner": None,
        "app_url": "http://127.0.0.1:8815"}
    monkeypatch.setattr(plugin_runtime, "resolve", lambda *args, **kwargs: {"connector": connection})
    calls, jobs, projects, imports, config = [], {}, [], {}, {}
    client_options = []

    async def handler(request):
        path, method = request.url.path, request.method
        calls.append((method, path))
        assert request.url.host == "127.0.0.1" and request.url.port == 8815
        if path == "/api/health":
            return httpx.Response(config.get("health_status", 200),
                json={"service": config.get("service", "prosperos-hoard")})
        if path == "/api/projects" and method == "POST":
            project = "proj_" + str(len(projects) + 1)
            projects.append(project)
            if config.get("project_timeout"):
                raise httpx.ReadTimeout("Synthetic project write ambiguity", request=request)
            return httpx.Response(200, json={"id": project})
        if path.endswith("/import-upload"):
            assert b"\x89PNG\r\n\x1a\n" in request.content
            reference = "reference_" + str(len(imports) + 1)
            imports[reference] = path.split("/")[3]
            conn = sqlite3.connect(data / "prospero_images.db")
            try:
                assert any(row[0] in ("import_intent", "mask_import_intent")
                    for row in conn.execute("SELECT state FROM requests"))
            finally:
                conn.close()
            if config.get("import_timeout") or config.get("import_timeout_on") == len(imports):
                raise httpx.ReadTimeout("Synthetic import write ambiguity", request=request)
            imported = {"id": reference, "project_id": path.split("/")[3], "kind": "image"}
            if config.get("bad_import_on") == len(imports):
                imported[config["bad_import_field"]] = config["bad_import_value"]
            return httpx.Response(200, json=imported)
        if path.endswith("/generate") or path.endswith("/edit"):
            conn = sqlite3.connect(data / "prospero_images.db")
            try:
                states = [row[0] for row in conn.execute("SELECT state FROM requests")]
                assert "submit_intent" in states  # Durable before transport.
            finally:
                conn.close()
            if config.get("post_gate"):
                config["post_entered"].set()
                await config["post_gate"].wait()
            if config.get("post_timeout"):
                raise httpx.ReadTimeout("Synthetic ambiguous write", request=request)
            if config.get("post_cancel"):
                raise asyncio.CancelledError()
            project = imports[path.split("/")[3]] if path.endswith("/edit") else path.split("/")[3]
            job = "job_" + str(len(jobs) + 1)
            jobs[job] = {"id": job, "project_id": project, "payload": json.loads(request.content)}
            return httpx.Response(200, json={"job": {"id": job, "project_id": project}})
        if path.startswith("/api/jobs/"):
            if config.get("revoke_on_poll"):
                config["enabled"] = False
            job = jobs[path.rsplit("/", 1)[-1]]
            return httpx.Response(200, json={"id": job["id"], "project_id": config.get("job_project", job["project_id"]),
                "state": config.get("job_state", "done"), "message": config.get("job_message"),
                "outputs": {"asset_ids": ["asset_" + job["id"]]}})
        if path.startswith("/api/assets/"):
            asset_id = path.split("/")[3]
            job = jobs[asset_id.removeprefix("asset_")]
            if path.endswith("/file"):
                return httpx.Response(200, content=config.get("output", png()),
                    headers={"content-type": config.get("mime", "image/png")})
            return httpx.Response(200, json={"id": asset_id, "project_id": config.get("asset_project", job["project_id"]),
                "kind": config.get("asset_kind", "image"), "file_path": "../../never-open", "mime": "image/png"})
        raise AssertionError("Unexpected outbound request " + path)

    real_client = httpx.AsyncClient

    def client(**kwargs):
        client_options.append(kwargs)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(adapter.httpx, "AsyncClient", client)
    yield SimpleNamespace(data=data, images=images, calls=calls, jobs=jobs, projects=projects,
        imports=imports, config=config, connection=connection, sessions=sessions, client_options=client_options)
    engine.dispose()


def rows(world):
    conn = sqlite3.connect(world.data / "prospero_images.db")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM requests")]
    finally:
        conn.close()


def run(request_id="request-one", **kwargs):
    return asyncio.run(adapter.run_image("A blue bird", "session-one", "alice", request_id=request_id, **kwargs))


def test_generation_publishes_owned_png_and_repeated_id_recovers_without_http(world):
    result = run()
    assert "error" not in result, result
    assert result["image_size"] == "3x2" and result["image_model"] == "prospero"
    assert next(iter(world.jobs.values()))["payload"] == {"prompt": "A blue bird", "count": 1, "wait_s": 0}
    row = rows(world)[0]
    assert row["state"] == "done" and row["owner"] == "alice" and row["session_id"] == "session-one"
    assert row["job_id"] and row["asset_id"] and row["gallery_id"] == result["image_id"]
    assert world.images.joinpath(row["filename"]).read_bytes() == png()
    with world.sessions() as db:
        image = db.query(database.GalleryImage).one()
        assert image.owner == "alice" and image.session_id == "session-one"
        assert image.width == 3 and image.height == 2 and image.file_size == len(png())
    before = list(world.calls)
    assert run() == result and world.calls == before
    assert all(options["follow_redirects"] is False and options["trust_env"] is False for options in world.client_options)


def test_distinct_ids_regenerate_identical_prompts_and_reuse_only_private_project(world):
    first, second = run("one"), run("two")
    assert first["image_id"] != second["image_id"]
    assert len(world.projects) == 1 and len(world.jobs) == 2
    other = asyncio.run(adapter.run_image("A blue bird", "session-two", "bob", request_id="one"))
    assert "error" not in other and len(world.projects) == 2
    assert len(rows(world)) == 3


def test_no_request_id_creates_new_intent_and_returns_recovery_id(world):
    first, second = run(None), run(None)
    assert first["request_id"] != second["request_id"] and len(world.jobs) == 2


@pytest.mark.parametrize("input_kind", ["bytes", "path"])
def test_edit_uploads_resolved_reference_bytes(world, input_kind, tmp_path):
    path = tmp_path / "owned.png"
    path.write_bytes(png())
    result = run(**({"image_bytes": png()} if input_kind == "bytes" else {"image_path": str(path)}))
    assert "error" not in result, result
    assert next(iter(world.jobs.values()))["payload"]["reference_asset_id"] == "reference_1"
    assert rows(world)[0]["input_asset_id"] == "reference_1"


def test_ambiguous_job_post_is_durable_unknown_and_never_resubmitted(world):
    world.config["post_timeout"] = True
    first = run()
    assert first["state"] == "unknown" and rows(world)[0]["state"] == "unknown"
    assert first["result_status"] == "outcome_unknown" and first["uncertainty"]["reconcile_action"]
    from src.tool_result import normalize_tool_result
    typed = normalize_tool_result(first, call_id="real-call", attempt_id="real-attempt")
    assert typed.status == "outcome_unknown" and typed.uncertainty is not None
    before = list(world.calls)
    second = run()
    assert second["state"] == "unknown" and world.calls == before
    assert sum(path.endswith("/generate") for _, path in world.calls) == 1
    assert not world.images.exists()


def test_cancelled_submission_retains_unknown_receipt(world):
    world.config["post_cancel"] = True
    with pytest.raises(asyncio.CancelledError):
        run()
    assert rows(world)[0]["state"] == "unknown"
    assert run()["state"] == "unknown"


def test_pending_job_reopens_sqlite_and_resumes_without_submit(world):
    world.config["job_state"] = "queued"
    first = run()
    assert first["state"] == "pending" and first["prospero_job_id"] == "job_1"
    assert first["result_status"] == "outcome_unknown"
    world.config["job_state"] = "done"
    second = run()
    assert "error" not in second and len(world.jobs) == 1
    assert sum(path.endswith("/generate") for _, path in world.calls) == 1


def test_concurrent_same_request_has_one_submission(world):
    async def scenario():
        world.config.update(post_gate=asyncio.Event(), post_entered=asyncio.Event())
        first = asyncio.create_task(adapter.run_image("A blue bird", "session-one", "alice", request_id="same"))
        await asyncio.wait_for(world.config["post_entered"].wait(), 5)
        try:
            second = await adapter.run_image("A blue bird", "session-one", "alice", request_id="same")
            assert second["state"] == "unknown"
        finally:
            world.config["post_gate"].set()
        assert "error" not in await first
    asyncio.run(scenario())
    assert len(world.jobs) == 1 and len(rows(world)) == 1


@pytest.mark.parametrize("change", ["prompt", "input", "connection"])
def test_existing_id_rejects_changed_payload_or_bound_origin(world, change):
    run()
    before = list(world.calls)
    if change == "connection":
        world.connection["app_url"] = "http://127.0.0.1:8816"
    kwargs = {"image_bytes": png()} if change == "input" else {}
    result = asyncio.run(adapter.run_image("different" if change == "prompt" else "A blue bird",
        "session-one", "alice", request_id="request-one", **kwargs))
    assert "different input or connection" in result["error"] and world.calls == before


@pytest.mark.parametrize("url", ["http://example.com:8815", "http://169.254.169.254:8815",
    "http://127.0.0.2:8815", "https://127.0.0.1:8815", "http://user:password@127.0.0.1:8815",
    "http://127.0.0.1:8815/path", "http://127.0.0.1:8815?", "http://127.0.0.1:8815#x", "http://127.0.0.1"])
def test_unbound_or_nonloopback_urls_cannot_make_http_requests(world, url):
    world.connection["app_url"] = url
    assert "error" in run() and world.calls == [] and not world.data.exists()


def test_localhost_is_pinned_to_loopback_without_http_dns_resolution(world):
    world.connection["app_url"] = "http://localhost:8815/"
    assert "error" not in run()
    assert rows(world)[0]["origin"] == "http://127.0.0.1:8815"


def test_other_owner_connector_is_not_adopted_even_if_runtime_returns_it(world):
    world.connection["owner"] = "bob"
    assert "No visible" in run()["error"] and world.calls == []


@pytest.mark.parametrize("config", [{"service": "other-app"}, {"health_status": 302}, {"health_status": 503}])
def test_health_must_be_successful_and_identify_prospero_before_writes(world, config):
    world.config.update(config)
    assert "error" in run()
    assert world.calls == [("GET", "/api/health")]


@pytest.mark.parametrize("config", [{"job_project": "foreign"}, {"asset_project": "foreign"},
    {"asset_kind": "video"}, {"output": b"not-a-png"}, {"mime": "text/html"}])
def test_wrong_identity_or_invalid_output_never_enters_gallery(world, config):
    world.config.update(config)
    assert "error" in run()
    with world.sessions() as db:
        assert db.query(database.GalleryImage).count() == 0
    assert not world.images.exists()


def test_output_byte_limit_and_mutually_exclusive_input(world, monkeypatch):
    assert "either" in run(image_bytes=png(), image_path="unused")["error"]
    assert world.calls == []
    monkeypatch.setattr(adapter, "MAX_IMAGE_BYTES", len(png()) - 1)
    assert "exceeds" in run()["error"] and not world.images.exists()


@pytest.mark.parametrize("case", ["disabled", "denied", "unknown_user", "malformed", "foreign_session", "missing_session"])
def test_real_authorization_denies_before_filesystem_receipt_or_http(world, case):
    if case == "disabled":
        world.config["enabled"] = False
    elif case == "denied":
        Path(adapter.AUTH_FILE).write_text(json.dumps({"users": {"alice": {
            "privileges": {"can_generate_images": False}}}}), encoding="utf-8")
    elif case == "unknown_user":
        Path(adapter.AUTH_FILE).write_text('{"users": {}}', encoding="utf-8")
    elif case == "malformed":
        Path(adapter.AUTH_FILE).write_text('not-json', encoding="utf-8")
    else:
        with world.sessions() as db:
            session = db.query(database.Session).filter(database.Session.id == "session-one").one()
            if case == "foreign_session":
                session.owner = "bob"
            else:
                db.delete(session)
            db.commit()
    assert "error" in run() and world.calls == [] and not world.data.exists()


def test_admin_uses_existing_privilege_semantics(world):
    Path(adapter.AUTH_FILE).write_text(json.dumps({"users": {"alice": {
        "is_admin": True, "privileges": {"can_generate_images": False}}}}), encoding="utf-8")
    assert "error" not in run()


def test_revocation_during_poll_prevents_asset_request_and_publication(world):
    world.config["revoke_on_poll"] = True
    result = run()
    assert "disabled" in result["error"]
    assert result["result_status"] == "outcome_unknown"
    assert not any(path.startswith("/api/assets/") for _, path in world.calls)
    assert not world.images.exists()
    assert len(world.jobs) == 1
    before = list(world.calls)
    assert "disabled" in run()["error"] and world.calls == before


def test_auth_disabled_uses_local_storage_owner_for_legacy_session(world, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    Path(adapter.AUTH_FILE).write_text('not-json', encoding="utf-8")
    with world.sessions() as db:
        session = db.query(database.Session).filter(database.Session.id == "session-one").one()
        session.owner = None
        db.commit()
    result = asyncio.run(adapter.run_image("A blue bird", "session-one", None, request_id="local"))
    assert "error" not in result, result
    assert rows(world)[0]["owner"] == "__odysseus_local__"
    with world.sessions() as db:
        assert db.query(database.GalleryImage).one().owner == "__odysseus_local__"


@pytest.mark.parametrize("message", ["outcome_unknown", "outcome_unknown: interrupted image job; not resubmitted automatically"])
def test_remote_interrupted_receipt_is_typed_unknown_and_not_retried(world, message):
    world.config.update(job_state="failed", job_message=message)
    result = run()
    assert result["result_status"] == "outcome_unknown" and result["uncertainty"]
    assert rows(world)[0]["state"] == "unknown"
    assert run()["result_status"] == "outcome_unknown" and len(world.jobs) == 1


def test_resume_edit_from_persisted_prompt_without_input_or_post(world):
    world.config["job_state"] = "running"
    started = run(image_bytes=png())
    request_id = started["request_id"]
    before_posts = [call for call in world.calls if call[0] == "POST"]
    # Reopening the durable SQLite file is the entire recovery authority.
    assert rows(world)[0]["prompt"] == "A blue bird"
    pending = asyncio.run(adapter.resume_image(request_id, "session-one", "alice"))
    assert pending["state"] == "pending" and pending["result_status"] == "outcome_unknown"
    assert sum(path.startswith("/api/jobs/") for _, path in world.calls) == 2
    world.config["job_state"] = "done"
    done = asyncio.run(adapter.resume_image(request_id, "session-one", "alice"))
    assert "error" not in done and done["image_prompt"] == "A blue bird"
    assert [call for call in world.calls if call[0] == "POST"] == before_posts
    assert asyncio.run(adapter.resume_image(request_id, "session-one", "alice")) == done


def test_resume_rejects_other_owner_and_changed_connection_without_http(world):
    world.config["job_state"] = "queued"
    run()
    before = list(world.calls)
    other = asyncio.run(adapter.resume_image("request-one", "session-two", "bob"))
    assert "No image request" in other["error"] and world.calls == before
    world.connection["app_url"] = "http://127.0.0.1:8816"
    changed = asyncio.run(adapter.resume_image("request-one", "session-one", "alice"))
    assert "different Prospero connection" in changed["error"] and world.calls == before


def test_resume_rechecks_revoked_privilege_before_http(world):
    world.config["job_state"] = "queued"
    run()
    Path(adapter.AUTH_FILE).write_text(json.dumps({"users": {"alice": {
        "privileges": {"can_generate_images": False}}}}), encoding="utf-8")
    before = list(world.calls)
    assert "cannot generate" in asyncio.run(adapter.resume_image("request-one", "session-one", "alice"))["error"]
    assert world.calls == before


@pytest.mark.parametrize("stage", ["project", "import"])
def test_unresolved_project_or_import_write_is_not_repeated(world, stage):
    world.config[stage + "_timeout"] = True
    result = run(**({"image_bytes": png()} if stage == "import" else {}))
    assert result["result_status"] == "outcome_unknown"
    before = list(world.calls)
    assert asyncio.run(adapter.resume_image("request-one", "session-one", "alice"))["result_status"] == "outcome_unknown"
    assert world.calls == before and not world.jobs


def test_intent_persistence_failure_prevents_job_submission(world, monkeypatch):
    update = adapter._update

    def fail(key, **fields):
        if fields.get("state") == "submit_intent":
            raise sqlite3.OperationalError("Synthetic durable write failure")
        return update(key, **fields)

    monkeypatch.setattr(adapter, "_update", fail)
    result = run()
    assert result["result_status"] == "outcome_unknown" and not world.jobs
    assert not any(path.endswith("/generate") for _, path in world.calls)


def test_gallery_commit_then_receipt_failure_recovers_same_image_without_duplicate(world, monkeypatch):
    update = adapter._update

    def fail(key, **fields):
        if fields.get("state") == "done":
            raise sqlite3.OperationalError("Synthetic final receipt failure")
        return update(key, **fields)

    monkeypatch.setattr(adapter, "_update", fail)
    assert run()["result_status"] == "outcome_unknown"
    first = rows(world)[0]
    with world.sessions() as db:
        assert db.query(database.GalleryImage).count() == 1
    monkeypatch.setattr(adapter, "_update", update)
    result = asyncio.run(adapter.resume_image("request-one", "session-one", "alice"))
    assert result["image_id"] == first["gallery_id"]
    assert len(world.jobs) == 1
    with world.sessions() as db:
        assert db.query(database.GalleryImage).count() == 1


def test_legacy_receipt_schema_migrates_additively_without_erasing_ids(world):
    world.data.mkdir()
    conn = sqlite3.connect(world.data / "prospero_images.db")
    conn.execute("CREATE TABLE requests(key TEXT PRIMARY KEY,request_id TEXT NOT NULL,owner TEXT NOT NULL,"
        "session_id TEXT NOT NULL,connector_id TEXT NOT NULL,origin TEXT NOT NULL,fingerprint TEXT NOT NULL,"
        "state TEXT NOT NULL,project_id TEXT,job_id TEXT,input_asset_id TEXT,asset_id TEXT,gallery_id TEXT,filename TEXT)")
    conn.execute("INSERT INTO requests(key,request_id,owner,session_id,connector_id,origin,fingerprint,state,job_id)"
        " VALUES('legacy','legacy','alice','session-one','connection','http://127.0.0.1:8815','legacy','unknown','job_legacy')")
    conn.commit()
    conn.close()
    assert "error" not in run()
    legacy = next(row for row in rows(world) if row["key"] == "legacy")
    assert legacy["job_id"] == "job_legacy" and legacy["prompt"] is None


@pytest.mark.parametrize("strength", [None, 0, 1, 0.65])
def test_inpaint_imports_source_and_mask_into_private_project_and_uses_existing_edit(world, strength):
    result = run(operation="inpaint", image_bytes=png(), mask_bytes=png(color="white"), strength=strength)
    assert "error" not in result, result
    payload = next(iter(world.jobs.values()))["payload"]
    expected = {"prompt": "A blue bird", "operation": "inpaint", "asset_id": "reference_1",
        "mask_asset_id": "reference_2", "count": 1, "wait_s": 0}
    if strength is not None:
        expected["strength"] = strength
    assert payload == expected
    assert world.imports == {"reference_1": "proj_1", "reference_2": "proj_1"}
    row = rows(world)[0]
    assert row["operation"] == "inpaint" and row["input_asset_id"] == "reference_1"
    assert row["mask_asset_id"] == "reference_2" and row["owner"] == "alice"
    assert ("POST", "/api/assets/reference_1/edit") in world.calls
    assert not any(path.endswith("/generate") for _, path in world.calls)


@pytest.mark.parametrize("changes", [{"image_bytes": None}, {"mask_bytes": None},
    {"mask_bytes": b"invalid"}, {"mask_bytes": png(width=4)},
    {"strength": float("nan")}, {"strength": float("inf")}, {"strength": -0.1},
    {"strength": 1.1}, {"strength": "0.5"}, {"strength": True}, {"operation": "other"}])
def test_inpaint_invalid_input_is_rejected_before_any_receipt_or_http(world, changes):
    kwargs = {"operation": "inpaint", "image_bytes": png(), "mask_bytes": png(color="white"), "strength": 0.5}
    kwargs.update(changes)
    assert "error" in run(**kwargs)
    assert world.calls == [] and not world.data.exists()


def test_generate_fingerprint_is_unchanged_and_mask_arguments_cannot_be_ignored(world):
    assert "error" not in run(image_bytes=png())
    assert rows(world)[0]["fingerprint"] == adapter._digest("A blue bird", adapter.hashlib.sha256(png()).hexdigest())
    before = list(world.calls)
    assert "only supported for inpaint" in run("new", image_bytes=png(), mask_bytes=png())["error"]
    assert world.calls == before


@pytest.mark.parametrize("changed", ["mask", "strength", "operation"])
def test_inpaint_request_id_cannot_be_reused_with_changed_semantics(world, changed):
    kwargs = {"operation": "inpaint", "image_bytes": png(), "mask_bytes": png(color="white"), "strength": 0.5}
    assert "error" not in run(**kwargs)
    before = list(world.calls)
    if changed == "mask":
        kwargs["mask_bytes"] = png(color="black")
    elif changed == "strength":
        kwargs["strength"] = 0.6
    else:
        kwargs = {"image_bytes": png()}
    assert "different input" in run(**kwargs)["error"] and world.calls == before


def test_inpaint_resume_needs_no_source_or_mask_and_has_no_import_or_job_duplicates(world):
    world.config["job_state"] = "queued"
    pending = run(operation="inpaint", image_bytes=png(), mask_bytes=png(color="white"), strength=0.5)
    assert pending["result_status"] == "outcome_unknown"
    before = [call for call in world.calls if call[0] == "POST"]
    world.config["job_state"] = "done"
    result = asyncio.run(adapter.resume_image("request-one", "session-one", "alice"))
    assert "error" not in result and result["image_id"]
    assert [call for call in world.calls if call[0] == "POST"] == before
    assert len(world.jobs) == 1 and len(world.imports) == 2
    with world.sessions() as db:
        assert db.query(database.GalleryImage).count() == 1


@pytest.mark.parametrize("stage", ["mask_import", "edit_submit"])
def test_inpaint_ambiguous_second_import_or_edit_submission_never_retries(world, stage):
    world.config.update({"import_timeout_on": 2} if stage == "mask_import" else {"post_timeout": True})
    kwargs = {"operation": "inpaint", "image_bytes": png(), "mask_bytes": png(color="white"), "strength": 0.5}
    result = run(**kwargs)
    assert result["result_status"] == "outcome_unknown"
    row = rows(world)[0]
    assert row["state"] == "unknown" and row["input_asset_id"] == "reference_1"
    assert row["mask_asset_id"] == (None if stage == "mask_import" else "reference_2")
    before = list(world.calls)
    assert run(**kwargs)["result_status"] == "outcome_unknown"
    assert asyncio.run(adapter.resume_image("request-one", "session-one", "alice"))["result_status"] == "outcome_unknown"
    assert world.calls == before and not world.jobs


@pytest.mark.parametrize("import_number", [1, 2])
@pytest.mark.parametrize("field,value", [("project_id", "foreign"), ("kind", "video"), ("id", "../unsafe")])
def test_inpaint_import_identity_is_validated_before_edit_submission(world, import_number, field, value):
    world.config.update(bad_import_on=import_number, bad_import_field=field, bad_import_value=value)
    result = run(operation="inpaint", image_bytes=png(), mask_bytes=png(color="white"))
    assert "error" in result and result["result_status"] == "outcome_unknown"
    assert not world.jobs and not any(path.endswith("/edit") or path.endswith("/generate") for _, path in world.calls)
    assert len(world.imports) == import_number
