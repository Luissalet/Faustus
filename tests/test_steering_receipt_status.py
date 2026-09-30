"""Owner-checked receipt status from bounded reopened real JSONL journals."""
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core import database as d
from routes import chat_routes, session_routes
from src import agent_runs, constants
from src.agent_tools import subagent_tools as st


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "owners.db"), connect_args={"check_same_thread": False})
    d.Session.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add_all([d.Session(id=sid, name="fixture", owner=owner, endpoint_url="http://unused", model="fixture") for sid, owner in
                    [("parent", "alice"), ("child", "alice"), ("foreign", "bob"), ("other", "alice")]])
        db.commit()
    monkeypatch.setattr(session_routes, "SessionLocal", factory)
    monkeypatch.setattr(session_routes, "effective_user", lambda request: request.headers.get("x-fixture-user", "alice"))
    monkeypatch.setattr(session_routes, "_auth_disabled", lambda: False)
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    monkeypatch.setattr(st, "_ACTIVE_WORKERS", {})
    monkeypatch.setattr(st, "_WORKER_RUNS", {})
    app = FastAPI()
    app.include_router(chat_routes.setup_chat_routes(*[SimpleNamespace(sessions={}) for _ in range(6)]))
    run = agent_runs._Run()
    run.log = agent_runs._RunLog("parent", run)
    worker = st.SubagentRun(0, {"name": "fixture", "instruction": "Synthetic"})
    worker.session_id = "child"
    worker.parent_run_id = run.run_id
    worker._steering_parent_session_id = "parent"
    worker.delegation_id = "delegation"
    worker._steering_attempt_id = uuid.uuid4().hex
    worker._steering_recorder = agent_runs._EffectRecorder(run)
    worker.accepts_steers = True
    st._ACTIVE_WORKERS["child"] = SimpleNamespace(done=lambda: False)
    st._WORKER_RUNS["child"] = worker
    yield TestClient(app), worker, run
    run.log.orphan()
    engine.dispose()


def submit(client):
    response = client.post("/api/chat/subagent/steer/child", json={"text": "Private synthetic body", "return_receipt": True})
    assert response.status_code == 200, response.text
    receipt = response.json()["receipt"]
    assert receipt["parent_session_id"] == "parent" and receipt["parent_run_id"]
    return receipt


def status(client, receipt, child="child", **overrides):
    params = {"parent_session_id": receipt["parent_session_id"], "parent_run_id": receipt["parent_run_id"]}
    params.update(overrides)
    return client.get("/api/chat/subagent/steering-receipts/" + child + "/" + receipt["receipt_id"], params=params)


@pytest.mark.parametrize("state", ["queued", "drained", "applied", "dropped"])
def test_real_api_reopens_old_journal_without_live_registry(api, monkeypatch, state):
    client, worker, run = api
    receipt = submit(client)
    if state in {"drained", "applied"}:
        st.pending_steers("child", _include_receipt_ids=True)
    if state == "applied":
        st._observe_steering_applied(worker, {"steering_receipt_id": receipt["receipt_id"]})
    if state == "dropped":
        st._drop_pending_steering(worker)
    run.log.orphan()
    st._ACTIVE_WORKERS.clear()
    st._WORKER_RUNS.clear()
    class NoRegistry(dict):
        def get(self, *a):
            pytest.fail("status must never consult the current run slot")
    monkeypatch.setattr(agent_runs, "_RUNS", NoRegistry())
    response = status(client, receipt)
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == state and body["durability"] == "unknown"
    assert body["evidence"] == "journal" and body["journal_present"] is True
    assert "Private synthetic body" not in response.text


def test_legacy_post_response_unchanged(api):
    client, _, _ = api
    assert client.post("/api/chat/subagent/steer/child", json={"text": "Legacy"}).json() == {"ok": True}


@pytest.mark.parametrize("foreign_scope", ["child", "parent"])
def test_owner_checks_both_scopes_before_any_journal_access(api, monkeypatch, foreign_scope):
    client, _, _ = api
    receipt = submit(client)
    monkeypatch.setattr(agent_runs, "read_steering_receipt", lambda *a: pytest.fail("filesystem reader before authorization"))
    response = status(client, receipt, child="foreign" if foreign_scope == "child" else "child",
                      **({"parent_session_id": "foreign"} if foreign_scope == "parent" else {}))
    assert response.status_code == 404


@pytest.mark.parametrize("foreign", ["receipt", "child"])
def test_foreign_receipt_404_only_after_validated_complete_journal(api, foreign):
    client, _, _ = api
    receipt = submit(client)
    if foreign == "receipt":
        receipt["receipt_id"] = uuid.uuid4().hex
    response = status(client, receipt, child="other" if foreign == "child" else "child")
    assert response.status_code == 404


@pytest.mark.parametrize("field,value", [("receipt_id", "../secret"), ("parent_run_id", "../secret"),
                                         ("receipt_id", "F" * 32), ("parent_run_id", "short")])
def test_invalid_locator_rejected_without_listing_files(api, monkeypatch, field, value):
    client, _, _ = api
    receipt = submit(client)
    receipt[field] = value
    monkeypatch.setattr(agent_runs, "_session_log_names", lambda *a: pytest.fail("invalid id must not list files"))
    assert status(client, receipt).status_code == (404 if field == "receipt_id" and "/" in value else 400)


@pytest.mark.parametrize("damage", ["missing", "tail", "malformed", "header", "transition", "scope", "too_large", "too_many_lines"])
def test_missing_corrupt_or_bounded_journal_is_unknown_never_prefix_authority(api, monkeypatch, damage):
    client, worker, run = api
    receipt = submit(client)
    run.log.orphan()
    path = __import__("pathlib").Path(run.log.path)
    if damage == "missing":
        path.unlink()
    elif damage == "tail":
        with path.open("ab") as handle:
            handle.write(b'{"partial":')
    elif damage == "malformed":
        with path.open("ab") as handle:
            handle.write(b'not-json\n')
    elif damage == "header":
        lines = path.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0]); header["run_id"] = uuid.uuid4().hex
        path.write_text(json.dumps(header) + "\n" + "\n".join(lines[1:]) + "\n", encoding="utf-8")
    elif damage in {"transition", "scope"}:
        lines = path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[1]); payload = json.loads(event["ev"][6:])
        if damage == "transition":
            payload["state"] = "applied"
        else:
            payload["parent_run_id"] = uuid.uuid4().hex
        event["ev"] = "data: " + json.dumps(payload) + "\n\n"
        lines[1] = json.dumps(event)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif damage == "too_large":
        monkeypatch.setattr(agent_runs, "_STEERING_READ_MAX_BYTES", 100)
    else:
        monkeypatch.setattr(agent_runs, "_STEERING_READ_MAX_LINES", 1)
    response = status(client, receipt)
    assert response.status_code == 200
    assert response.json()["state"] == "unknown"
    assert response.json()["durability"] == "unknown"


def test_missing_other_run_never_adopts_latest_registry_or_journal(api):
    client, _, _ = api
    receipt = submit(client)
    receipt["parent_run_id"] = uuid.uuid4().hex
    assert status(client, receipt).json()["state"] == "unknown"


@pytest.mark.parametrize("run_id,receipt_id", [("../secret", "f" * 32), ("f" * 32, "../secret")])
def test_direct_reader_rejects_traversal_before_filesystem(monkeypatch, run_id, receipt_id):
    monkeypatch.setattr(agent_runs, "_session_log_names", lambda *a: pytest.fail("must not list paths"))
    with pytest.raises(ValueError):
        agent_runs.read_steering_receipt("parent", run_id, "child", receipt_id)


def test_duplicate_same_run_journal_is_unknown_not_latest_guess(api):
    from pathlib import Path
    client, _, run = api
    receipt = submit(client)
    run.log.orphan()
    alternate = Path(agent_runs._run_log_path("parent", run.run_id, run.started_at + 1))
    alternate.write_bytes(Path(run.log.path).read_bytes())
    assert status(client, receipt).json()["state"] == "unknown"


def test_get_with_parent_owner_not_child_owner_rejected(api, monkeypatch):
    client, _, _ = api
    receipt = submit(client)
    monkeypatch.setattr(agent_runs, "read_steering_receipt", lambda *a: pytest.fail("no authorized child"))
    response = client.get("/api/chat/subagent/steering-receipts/child/" + receipt["receipt_id"],
        params={"parent_session_id": "foreign", "parent_run_id": receipt["parent_run_id"]},
        headers={"x-fixture-user": "bob"})
    assert response.status_code == 404



def test_actual_over_8mib_journal_does_not_report_queued_prefix(api):
    from pathlib import Path
    client, _, run = api
    receipt = submit(client)
    run.log.orphan()
    with Path(run.log.path).open("ab") as handle:
        handle.write(b" " * (8 * 1024 * 1024) + b"\n")
    assert status(client, receipt).json()["state"] == "unknown"


def test_unreadable_journal_directory_is_unknown(api, monkeypatch):
    client, _, _ = api
    receipt = submit(client)
    monkeypatch.setattr(agent_runs, "_session_log_names", lambda *a: (_ for _ in ()).throw(OSError("synthetic")))
    assert status(client, receipt).json()["state"] == "unknown"


@pytest.mark.parametrize("run_id,receipt_id", [(None, "f" * 32), ("f" * 32, 1)])
def test_nonstring_locator_rejected_before_any_read(monkeypatch, run_id, receipt_id):
    monkeypatch.setattr(agent_runs, "_session_log_names", lambda *a: pytest.fail("must not read"))
    with pytest.raises(ValueError):
        agent_runs.read_steering_receipt("parent", run_id, "child", receipt_id)
