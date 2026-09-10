"""MEDIA-02 — a recipe update that introduces new executable nodes has to be
reviewed again before it can render anything.

`config/media_workflows/*.json` being under code review answers "was this
file's TEXT looked at". It does not answer "does this render only invoke
node types somebody has actually reviewed" — a template edited (by a bad
merge, a compromised dependency, or a rushed change) to add a node that can
read or write arbitrary files on the render machine would otherwise take
effect the moment the process next reads the directory.

`src.media_workflows.review_status()` is the check; `src.media_runs.plan()`
and `.start()` are where it is enforced, before a job ever reaches a GPU.

Revert-and-fail check (documented per COMUN.md rule 5): with the two
`review_status(...)`/`if not review["reviewed"]` blocks removed from
`src/media_runs.py` (and the file otherwise unchanged), every test in this
module that talks to `plan()`/`start()` fails — the "poisoned" template that
introduces a fake node renders and even queues against the fake engine
exactly as if it had been reviewed. Restoring the blocks makes the suite
green again. Verified by hand (copy of `media_runs.py`, temporary edit, run,
restore) rather than left as a permanent second code path in the module.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src import media_runs
from src import media_workflows as mw
from tests.test_comfyui_backend import FakeComfy

SOURCE_TEMPLATE = mw.WORKFLOWS_DIR + "/image.quick-draft.v1.json"


def _template(*, poisoned: bool) -> dict:
    payload = json.loads(open(SOURCE_TEMPLATE, encoding="utf-8").read())
    payload["id"] = "test.p1.media02"
    payload["version"] = "1.0.0"
    if poisoned:
        # A node type this recipe never used before — the thing a review has
        # to actually see before it runs, e.g. one that could shell out or
        # read arbitrary paths on the render machine.
        payload["graph"]["8"] = {"class_type": "ArbitraryPythonNode",
                                 "inputs": {"code": "import os; os.system('echo hi')"}}
    return payload


@pytest.fixture()
def library(tmp_path):
    """A private template directory AND a private review registry, so this
    test proves the gate without touching the shipped
    `config/media_workflows/reviews/approved_recipes.json`."""
    (tmp_path / "reviews").mkdir()
    return tmp_path


def _write(library, payload):
    (library / "test.p1.media02.v1.json").write_text(json.dumps(payload), encoding="utf-8")


def _registry(library, *, node_types):
    path = library / "reviews" / "approved_recipes.json"
    path.write_text(json.dumps({
        "test.p1.media02": {"approved": [{
            "version": "1.0.0", "fingerprint": "not-the-real-one-on-purpose",
            "node_types": node_types, "approved_by": "test", "approved_at": "2026-01-01T00:00:00Z",
        }]}
    }), encoding="utf-8")
    return str(path)


# ── review_status() itself — pure, no engine, no database ─────────────────

def test_a_template_with_no_registry_entry_at_all_is_not_reviewed(library):
    _write(library, _template(poisoned=False))
    workflow = mw.load("test.p1.media02", directory=str(library))
    status = mw.review_status(workflow, registry={})
    assert status["reviewed"] is False
    assert status["reason"] == "no_review_record"
    assert "CheckpointLoaderSimple" in status["new_nodes"]


def test_an_exact_fingerprint_match_is_reviewed(library):
    _write(library, _template(poisoned=False))
    workflow = mw.load("test.p1.media02", directory=str(library))
    registry = {"test.p1.media02": {"approved": [{
        "version": "1.0.0", "fingerprint": workflow.fingerprint(),
        "node_types": sorted(mw.graph_node_types(workflow))}]}}
    assert mw.review_status(workflow, registry=registry) == {
        "reviewed": True, "reason": "", "new_nodes": []}


def test_a_changed_fingerprint_with_no_new_node_types_stays_reviewed(library):
    """A prompt/default tweak changes the fingerprint (the graph is part of
    it) but calls no node nobody has reviewed — not a re-review event."""
    original = _template(poisoned=False)
    workflow_before = mw.parse(original)
    registry = {"test.p1.media02": {"approved": [{
        "version": "1.0.0", "fingerprint": workflow_before.fingerprint(),
        "node_types": sorted(mw.graph_node_types(workflow_before))}]}}

    tweaked = json.loads(json.dumps(original))
    tweaked["inputs"]["prompt"]["max_len"] = 850  # same nodes, different fingerprint
    _write(library, tweaked)
    workflow_after = mw.load("test.p1.media02", directory=str(library))
    assert workflow_after.fingerprint() != workflow_before.fingerprint()
    assert mw.review_status(workflow_after, registry=registry)["reviewed"] is True


def test_a_new_node_type_is_not_covered_by_an_old_review(library):
    poisoned = _template(poisoned=True)
    clean_fingerprint = mw.parse(_template(poisoned=False)).fingerprint()
    registry = {"test.p1.media02": {"approved": [{
        "version": "1.0.0", "fingerprint": clean_fingerprint,
        "node_types": sorted(mw.graph_node_types(mw.parse(_template(poisoned=False))))}]}}
    _write(library, poisoned)
    workflow = mw.load("test.p1.media02", directory=str(library))
    status = mw.review_status(workflow, registry=registry)
    assert status["reviewed"] is False
    assert status["reason"] == "new_nodes_introduced"
    assert status["new_nodes"] == ["ArbitraryPythonNode"]


# ── enforced by plan() and start(), against a real database + fake engine ──

@pytest.fixture()
def world(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "media.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    from src import artifact_store
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(artifact_store, "ARTIFACT_RUNS_DIR", str(tmp_path / "runs"))
    fake = FakeComfy()
    base_url = fake.start()
    monkeypatch.setenv("COMFYUI_URL", base_url)
    try:
        yield fake
    finally:
        fake.stop()
        engine.dispose()


_ORIGINAL_NODES = ["CheckpointLoaderSimple", "CLIPTextEncode", "EmptyLatentImage",
                   "KSampler", "SaveImage", "VAEDecode"]


def test_plan_refuses_an_unreviewed_template_before_touching_the_engine(world, library, monkeypatch):
    _write(library, _template(poisoned=True))
    monkeypatch.setattr(mw, "WORKFLOWS_DIR", str(library))
    monkeypatch.setattr(mw, "REVIEW_REGISTRY_PATH", _registry(library, node_types=_ORIGINAL_NODES))
    out = media_runs.plan("test.p1.media02", {"prompt": "a mug"}, check_engine=False)
    assert out["ok"] is False and out["reason"] == "needs_review"
    assert out["new_nodes"] == ["ArbitraryPythonNode"]
    assert world.submitted == [], "an unreviewed recipe must never reach the engine"


def test_plan_allows_the_same_template_once_the_registry_covers_its_nodes(world, library, monkeypatch):
    _write(library, _template(poisoned=True))
    monkeypatch.setattr(mw, "WORKFLOWS_DIR", str(library))
    monkeypatch.setattr(mw, "REVIEW_REGISTRY_PATH",
                        _registry(library, node_types=["ArbitraryPythonNode", *_ORIGINAL_NODES]))
    out = media_runs.plan("test.p1.media02", {"prompt": "a mug"}, check_engine=False)
    assert out["ok"] is True


def test_start_refuses_an_unreviewed_template_and_writes_no_row(world, library, monkeypatch):
    _write(library, _template(poisoned=True))
    monkeypatch.setattr(mw, "WORKFLOWS_DIR", str(library))
    monkeypatch.setattr(mw, "REVIEW_REGISTRY_PATH", _registry(library, node_types=["CheckpointLoaderSimple"]))
    before = media_runs.recent(limit=200)
    out = media_runs.start("test.p1.media02", {"prompt": "a mug"})
    assert out["ok"] is False and out["reason"] == "needs_review"
    assert world.submitted == []
    assert media_runs.recent(limit=200) == before, "a refused recipe must not leave a row behind"


def test_the_shipped_templates_are_all_reviewed_as_shipped():
    """The registry actually shipped in the repo covers every real template —
    otherwise turning this gate on would have silently disabled MEDIA-02 for
    everything the app really uses."""
    catalogue = mw.catalogue()
    assert catalogue["broken"] == []
    for workflow in catalogue["workflows"]:
        status = mw.review_status(workflow)
        assert status["reviewed"], f"{workflow.id} {workflow.version}: {status}"
