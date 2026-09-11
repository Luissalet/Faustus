"""Approved project scripts through the real router, with an inert backend."""
from pathlib import Path

import pytest

from src import approval_store, artifact_store, capability_registry as registry, execution_router
from src import skill_import_review
from src.contracts import ExecutionResult
from src.skills_runtime import bridge, discovery
from src.workflows import WorkflowEngine
from src.workflows.runtime import production_handlers
from tests.test_workflow_handlers import store, wf  # noqa: F401


@pytest.fixture
def project(tmp_path, monkeypatch):
    import services.projects as projects
    from src import tool_execution
    workspace = tmp_path / "workspace"
    folder = workspace / ".agents" / "skills" / "report"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("""---
name: report
description: Write a report
version: 1.0.0
permissions_backends: [docker_workspace]
permissions_max_seconds: 30
outputs: [report=artifact:document]
---
Run report.py to write the report.
""", encoding="utf-8")
    (folder / "report.py").write_text("from pathlib import Path\nPath('/artifacts/report.md').write_text('Verified report')\n")

    # ADP-25: src/workflows/skills.py::run now gates on skill_import_review
    # before building a bundle. Point the approvals store at a scratch dir
    # (never the real DATA_DIR) and approve this fixture's own "report"
    # skill up front, so this module's tests keep exercising the real
    # execution/approval flow rather than the review gate itself (that gate
    # is covered on its own in tests/test_adp25_skill_review.py).
    monkeypatch.setattr(skill_import_review, "APPROVALS_FILE", str(tmp_path / "skill_approvals.json"))
    monkeypatch.setattr(skill_import_review, "SNAPSHOT_DIR", str(tmp_path / "skill_approvals"))
    found = discovery.DiscoveredSkill(
        name="report", path=str(folder / "SKILL.md"), origin="agents",
        root=str(workspace), distance=0)
    manifest_text = (folder / "SKILL.md").read_text(encoding="utf-8")
    manifest = bridge.manifest_from_markdown(manifest_text, source=found.path)
    skill_import_review.approve(
        skill_id=manifest.id, manifest=manifest, manifest_text=manifest_text,
        digest=discovery.skill_digest(found), by="alice")

    class Projects:
        def get(self, project_id, owner=None):
            if project_id == "project-a" and owner == "alice":
                return {"id": project_id, "owner": owner, "workspace": str(workspace)}

    monkeypatch.setattr(projects, "get_store", lambda: Projects())
    monkeypatch.setattr(tool_execution, "vet_workspace", lambda path: path)
    monkeypatch.setattr(artifact_store, "ARTIFACT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "blobs"))
    monkeypatch.setattr(registry, "_probe_cache", {})
    monkeypatch.setattr(registry, "_probe_docker", lambda stamp: registry.Observation(
        "docker_workspace", "available", "inert fixture", stamp))
    calls = []

    class Backend:
        def run(self, spec, command, **kwargs):
            kwargs["on_event"]("backend.started", {})
            calls.append((spec, command))
            # The script actually handed to the backend is the approved copy.
            snapshot = Path(spec.artifacts_dir) / command[1].removeprefix("/artifacts/")
            assert snapshot.read_text() == (folder / "report.py").read_text()
            (Path(spec.artifacts_dir) / "report.md").write_text("Verified report", encoding="utf-8")
            return ExecutionResult.parse({"run_id": kwargs["run_id"], "backend": "docker_workspace",
                                          "status": "completed", "exit_code": 0,
                                          "artifact_filenames": ["report.md"], "stdout_tail": "done"})

    monkeypatch.setattr(execution_router.execution_backends, "build", lambda *a, **kw: Backend())
    return {"workspace": workspace, "folder": folder, "calls": calls}


def start(store, *, config=None, owner="alice", project_id="project-a"):
    definition = wf({"id": "script", "type": "skill", "config": config or
                     {"skill": "report", "script": "report.py", "version": "1.0.0"}})
    run = store.create_run(definition, owner=owner, project_id=project_id)["run_id"]
    engine = WorkflowEngine(production_handlers(), store)
    return run, engine, engine.advance(run)


def test_script_uses_container_and_publishes_downloadable_artifact(store, project):
    run, engine, first = start(store)
    assert first["status"] == "paused"
    assert not project["calls"]
    card = approval_store.get(first["approval_id"])
    assert card.owner == "alice"
    assert card.plan.action == "destructive"
    approval_store.decide(card.id, granted=True, by="alice")
    assert engine.resume(run, "script")["status"] == "completed"
    result = store.node_runs(run)["script"].result
    assert result["artifact_ids"]
    assert result["stdout"] == "done"
    assert result["source_sha256"]
    assert approval_store.get(card.id).status == "consumed"
    spec, command = project["calls"][0]
    assert spec.backend == "docker_workspace" and not spec.attended_ack
    assert command == ["python", "/artifacts/.faustus-skill/report.py"]
    assert engine.advance(run)["status"] == "completed"
    assert len(project["calls"]) == 1


def test_source_change_invalidates_an_approved_script(store, project):
    run, engine, first = start(store)
    approval_store.decide(first["approval_id"], granted=True, by="alice")
    (project["folder"] / "report.py").write_text("raise RuntimeError('changed')")
    assert engine.resume(run, "script")["status"] == "failed"
    assert not project["calls"]
    assert approval_store.get(first["approval_id"]).uses_left == 1


@pytest.mark.parametrize("config", [
    {"skill": "report", "script": "../private.py"},
    {"skill": "report", "script": "C:/private.py"},
    {"skill": "report", "script": "report.py", "version": "9.0.0"},
    {"skill": "missing", "script": "report.py"},
    {"skill": "report", "script": "report.py", "args": "--not-an-argv"},
])
def test_invalid_script_never_starts_a_backend(store, project, config):
    _, _, first = start(store, config=config)
    assert first["status"] == "failed"
    assert not project["calls"]
    assert not approval_store.pending(owner="alice")


def test_foreign_project_is_refused(store, project):
    _, _, first = start(store, owner="bob")
    assert first["status"] == "failed"
    assert not project["calls"]


def test_project_without_declared_backend_does_not_fall_back_to_host(store, project):
    source = project["folder"] / "SKILL.md"
    source.write_text(source.read_text().replace("[docker_workspace]", "[]"))
    _, _, first = start(store)
    assert first["status"] == "failed"
    assert not project["calls"]


def test_script_roundtrip_with_the_installed_container_image(store, project, monkeypatch):
    from src.execution_backends import DockerWorkspaceBackend
    backend = DockerWorkspaceBackend()
    ready = backend.probe()
    if not ready["ok"]:
        pytest.skip(ready["detail"])
    monkeypatch.setattr(execution_router.execution_backends, "build", lambda *a, **kw: backend)
    run, engine, first = start(store)
    assert first["status"] == "paused"
    approval_store.decide(first["approval_id"], granted=True, by="alice")
    outcome = engine.resume(run, "script")
    assert outcome["status"] == "completed", outcome
    assert store.node_runs(run)["script"].result["artifact_ids"]


def test_cancelling_a_real_workflow_stops_its_script(store, project, monkeypatch):
    from src.execution_backends import DockerWorkspaceBackend
    backend = DockerWorkspaceBackend()
    ready = backend.probe()
    if not ready["ok"]:
        pytest.skip(ready["detail"])
    (project["folder"] / "report.py").write_text(
        "from pathlib import Path\nimport time\n"
        "Path('/workspace/ready').touch()\n"
        "time.sleep(8)\nPath('/workspace/late').touch()\n", encoding="utf-8")
    original = backend.run
    requested = []

    def run(spec, command, **kwargs):
        check = kwargs["cancel_requested"]

        def cancel():
            if (project["workspace"] / "ready").exists() and not requested:
                requested.append(True)
                store.set_run_status(run_id, "cancelled", reason="User cancelled during script")
            return check()

        return original(spec, command, **{**kwargs, "cancel_requested": cancel})

    monkeypatch.setattr(backend, "run", run)
    monkeypatch.setattr(execution_router.execution_backends, "build", lambda *a, **kw: backend)
    run_id, engine, first = start(store)
    approval_store.decide(first["approval_id"], granted=True, by="alice")
    outcome = engine.resume(run_id, "script")
    assert requested
    assert outcome["status"] == "cancelled"
    assert not (project["workspace"] / "late").exists()
    assert store.needs_reconciliation(run_id=run_id)  # partial effects aren't silently retried
