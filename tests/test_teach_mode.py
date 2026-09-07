import os

import pytest

from src.durable_feature_store import DurableFeatureStore
from src.teach_mode.contracts import TeachError
from src.teach_mode.service import TeachService


@pytest.fixture
def teach(tmp_path):
    return TeachService(DurableFeatureStore(str(tmp_path / "teach.db")))


def test_demonstration_compiles_only_successful_semantic_steps_and_redacts(teach):
    demo = teach.start(owner="alice", request={"title": "Release", "intent": "Release safely",
                                                        "project_id": "p1", "session_id": "s1"})
    observation = teach.observe(owner="alice", demonstration_id=demo["id"], observation={
        "tool": "bash", "arguments": {"cmd": "pytest", "api_token": "top-secret"},
        "result": {"ok": True}, "classification": "required_for_core",
    })
    assert observation["arguments"]["api_token"] == "<redacted>"
    teach.observe(owner="alice", demonstration_id=demo["id"], observation={
        "tool": "web_search", "arguments": {"query": "optional"}, "result": {},
        "classification": "incidental",
    })
    teach.stop(owner="alice", demonstration_id=demo["id"])
    procedure = teach.compile(owner="alice", demonstration_id=demo["id"])
    assert [step["tool"] for step in procedure["steps"]] == ["bash"]
    assert procedure["revision_hash"]


def test_procedure_requires_simulation_and_passed_proof_before_approval(teach):
    demo = teach.start(owner="alice", request={"title": "T", "intent": "Do T"})
    teach.observe(owner="alice", demonstration_id=demo["id"],
                  observation={"tool": "read_file", "arguments": {}, "result": {}})
    teach.stop(owner="alice", demonstration_id=demo["id"])
    procedure = teach.compile(owner="alice", demonstration_id=demo["id"])
    with pytest.raises(TeachError):
        teach.transition(owner="alice", procedure_id=procedure["id"], action="approve")
    simulated = teach.transition(owner="alice", procedure_id=procedure["id"], action="simulate")
    failed = teach.transition(owner="alice", procedure_id=simulated["id"], action="validate",
                              evidence={"passed": False, "limitations": ["wrong output"]})
    assert failed["status"] == "needs_correction"
    simulated = teach.transition(owner="alice", procedure_id=failed["id"], action="simulate")
    validated = teach.transition(owner="alice", procedure_id=simulated["id"], action="validate",
                                 evidence={"passed": True, "proof_refs": ["proof:1"]})
    assert validated["status"] == "validated"


def test_owner_isolation(teach):
    demo = teach.start(owner="alice", request={"title": "T", "intent": "Do T"})
    assert teach.get(owner="bob", demonstration_id=demo["id"]) is None
    assert teach.list(owner="bob") == []


def test_capture_pause_resume_and_stop_are_idempotent(teach):
    demo = teach.start(owner="alice", request={"title": "T", "intent": "Do T",
                                                "session_id": "s1"})
    assert teach.start(owner="alice", request={"title": "duplicate", "intent": "duplicate",
                                                "session_id": "s1"})["id"] == demo["id"]
    paused = teach.pause(owner="alice", demonstration_id=demo["id"])
    assert paused["status"] == "paused"
    assert teach.pause(owner="alice", demonstration_id=demo["id"])["revision"] == paused["revision"]
    resumed = teach.resume(owner="alice", demonstration_id=demo["id"])
    assert resumed["status"] == "recording"
    stopped = teach.stop(owner="alice", demonstration_id=demo["id"])
    assert teach.stop(owner="alice", demonstration_id=demo["id"])["revision"] == stopped["revision"]


def test_install_means_a_real_published_skill_exists(teach, tmp_path, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(teach, "_register_with_immune", lambda *_args, **_kwargs: None)
    demo = teach.start(owner="alice", request={"title": "Read safely", "intent": "Read a file safely"})
    teach.observe(owner="alice", demonstration_id=demo["id"], observation={
        "tool": "read_file", "arguments": {"path": "example.txt"}, "result": {"ok": True}
    })
    teach.stop(owner="alice", demonstration_id=demo["id"])
    procedure = teach.compile(owner="alice", demonstration_id=demo["id"])
    procedure = teach.transition(owner="alice", procedure_id=procedure["id"], action="simulate")
    procedure = teach.transition(owner="alice", procedure_id=procedure["id"], action="validate",
                                 evidence={"passed": True, "proof_refs": ["proof:1"]})
    procedure = teach.transition(owner="alice", procedure_id=procedure["id"], action="approve")
    installed = teach.transition(owner="alice", procedure_id=procedure["id"], action="install")
    from services.memory.skills import SkillsManager
    skill_name = installed["installed_skill_ref"].removeprefix("skill://")
    skill = next(row for row in SkillsManager(constants.DATA_DIR).load(owner="alice")
                 if row["name"] == skill_name)
    assert skill["status"] == "published" and skill["source"] == "taught"


def test_restart_marks_an_unfinished_recording_interrupted(teach):
    demo = teach.start(owner="alice", request={"title": "T", "intent": "Do T"})
    assert teach.recover_interrupted_recordings() == 1
    recovered = teach.get(owner="alice", demonstration_id=demo["id"])
    assert recovered["status"] == "interrupted"
    assert teach.recover_interrupted_recordings() == 0
