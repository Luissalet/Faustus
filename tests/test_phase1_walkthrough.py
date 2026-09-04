"""Phase 1 acceptance, end to end: a manifest becomes a container, and what
the container wrote becomes typed artifacts with provenance in the database.

Phase 0's walkthrough proved the vocabulary with no execution behind it. This
one runs. It is deliberately the same shape — manifest → run → events →
artifact — so the diff between the two files is exactly what Phase 1 added.

Skips, loudly, when there is no Docker: the point of the test is the isolation,
and a version of it that mocked the daemon would assert that a list of flags
was built rather than that a container obeyed them.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import ArtifactRow, Base
from src import artifact_store, capability_registry as registry, execution_router as router
from src.contracts import SkillManifest
from src.execution_backends import DockerWorkspaceBackend

TEST_IMAGE = "python:3.12-slim"
_READY = DockerWorkspaceBackend(image=TEST_IMAGE).probe()

needs_docker = pytest.mark.skipif(
    not _READY["ok"], reason=f"needs docker and {TEST_IMAGE}: {_READY.get('detail')}")

MANIFEST = SkillManifest.parse({
    "id": "document.report", "version": "1.0.0", "title": "Write a report from notes",
    "inputs": {"notes": "text"},
    "outputs": {"report": "artifact:document"},
    "memory": {"read_scopes": ["project"], "write_scopes": ["run"]},
    "permissions": {"backends": ["docker_workspace"], "max_seconds": 120},
})

SCRIPT = (
    "import pathlib\n"
    "notes = pathlib.Path('/workspace/notes.txt').read_text()\n"
    "pathlib.Path('/artifacts/report.md').write_text('# Report\\n\\n' + notes)\n"
    "print('done')\n"
)


@pytest.fixture()
def stage(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "phase1.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    work = tmp_path / "ws"
    work.mkdir()
    (work / "notes.txt").write_text("- one\n- two\n", encoding="utf-8")
    yield {"workspace": str(work), "artifacts_root": str(tmp_path / "runs"),
           "store": str(tmp_path / "store")}
    engine.dispose()


@needs_docker
def test_a_manifest_becomes_a_container_and_the_output_becomes_a_row(stage):
    events = []
    decision, result = router.execute(
        MANIFEST, ["python", "-c", SCRIPT],
        workspace=stage["workspace"], artifacts_root=stage["artifacts_root"],
        run_id="phase1-1", image=TEST_IMAGE,
        on_event=lambda name, data: events.append((name, data)))

    assert decision.ok and decision.backend == "docker_workspace"
    assert result.status == "completed" and result.exit_code == 0
    assert result.artifact_filenames == ("report.md",)
    assert [n for n, _ in events] == ["backend.started", "backend.finished"]
    assert events[0][1]["isolation"] == "container"
    assert events[0][1]["network"] is False

    collected = artifact_store.collect(
        result, source_dir=decision.spec.artifacts_dir,
        owner="luis", project_id="book",
        skill_id=MANIFEST.id, skill_version=MANIFEST.version,
        provenance={"recipe": "report.v1", "recipe_version": "1.0.0"},
        store_dir=stage["store"])
    assert len(collected.artifacts) == 1
    art = collected.artifacts[0]
    assert art.kind == "document" and art.label == "report.md"
    assert art.filename == f"{art.sha256}.md"

    assert artifact_store.persist(collected.artifacts) == {"created": 1, "already_there": 0}
    db = db_mod.SessionLocal()
    try:
        row = db.query(ArtifactRow).one()
        assert (row.run_id, row.backend, row.skill_id) == \
               ("phase1-1", "docker_workspace", "document.report")
        assert row.model is None            # nothing here knew a model; it says so
    finally:
        db.close()

    stored = artifact_store.path_of(art.filename, store_dir=stage["store"])
    assert "- one" in open(stored, encoding="utf-8").read()


@needs_docker
def test_the_same_walk_is_refused_when_the_manifest_asks_for_more(stage):
    """One extra permission and the same run does not happen: the manifest
    wants the host, and no backend in this build both isolates and offers it."""
    greedy = SkillManifest.parse({
        **MANIFEST.to_dict(),
        "permissions": {**MANIFEST.permissions.to_dict(), "host_access": True},
    })
    decision = router.choose(greedy, workspace=stage["workspace"],
                             artifacts_root=stage["artifacts_root"], run_id="phase1-2")
    assert decision.ok is False
    assert "host" in str(decision.detail)
    assert not os.listdir(artifact_store.run_dir("phase1-2", root=stage["artifacts_root"]))


@needs_docker
def test_the_registry_now_answers_from_a_probe_and_not_from_a_promise():
    observed = registry.observe("docker_workspace", fresh=True)
    assert observed.state == "available"
    assert "docker " in observed.evidence and "image" in observed.evidence
    assert registry.observe("media_worker").state == "unavailable"
    assert "not implemented" in registry.observe("media_worker").evidence
