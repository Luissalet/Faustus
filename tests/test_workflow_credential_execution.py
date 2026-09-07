import pytest
from cryptography.fernet import Fernet

from src import approval_store, secret_storage, execution_router
from src.contracts import ExecutionResult
from src.workflows import credentials
from tests.test_workflow_script_skills import project, start  # noqa: F401
from tests.test_workflow_handlers import store  # noqa: F401


@pytest.fixture
def bound(project, tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "STORE_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr(secret_storage, "_fernet", Fernet(Fernet.generate_key()))
    source = project["folder"] / "SKILL.md"
    source.write_text(source.read_text().replace("permissions_backends:", "permissions_secrets: [API_KEY]\npermissions_backends:"))
    row = credentials.put("alice", "test-provider", "synthetic-workflow-secret")
    return row


def config():
    return {"skill": "report", "script": "report.py", "secret_bindings": {"API_KEY": "test-provider"}}


def approve():
    for card in approval_store.pending(owner="alice"):
        approval_store.decide(card.id, granted=True, by="alice")


def test_only_approved_bound_secret_reaches_backend_and_output_is_redacted(store, project, bound, monkeypatch):
    captured = []
    class Backend:
        def run(self, spec, command, **kwargs):
            kwargs["on_event"]("backend.started", {})
            captured.append(kwargs["secrets"])
            return ExecutionResult.parse({"run_id": kwargs["run_id"], "backend": "docker_workspace",
                "status": "completed", "exit_code": 0, "stdout_tail": "value=synthetic-workflow-secret"})
    monkeypatch.setattr(execution_router.execution_backends, "build", lambda *a, **kw: Backend())
    run, engine, first = start(store, config=config())
    assert first["status"] == "paused"
    assert not captured
    cards = approval_store.pending(owner="alice")
    assert any("test-provider" in str(card.plan.to_dict()) for card in cards)
    assert all("synthetic-workflow-secret" not in str(card.to_dict()) for card in cards)
    approve()
    assert engine.resume(run, "script")["status"] == "completed"
    assert captured == [{"API_KEY": "synthetic-workflow-secret"}]
    assert "synthetic-workflow-secret" not in str(store.node_runs(run)["script"].result)


@pytest.mark.parametrize("action", ["rotate", "delete"])
def test_rotation_or_deletion_while_waiting_stops_execution(store, project, bound, action):
    run, engine, first = start(store, config=config())
    assert first["status"] == "paused"
    approve()
    if action == "rotate":
        credentials.put("alice", "test-provider", "rotated", expected_revision=bound["revision"])
    else:
        credentials.remove("alice", "test-provider", expected_revision=bound["revision"])
    assert engine.resume(run, "script")["status"] == "failed"
    assert not project["calls"]


def test_missing_explicit_binding_never_requests_approval(store, project, bound):
    _, _, first = start(store)
    assert first["status"] == "failed"
    assert not approval_store.pending(owner="alice")
    assert not project["calls"]
