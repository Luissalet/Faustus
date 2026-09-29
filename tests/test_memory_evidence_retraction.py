"""Source withdrawal keeps only claims supported by remaining evidence."""

import pytest

from src import memory_engine as engine


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    yield
    engine.reset_vector_store()


def _fact(owner, evidence, project="atlas"):
    return engine.add_item("Threshold is 42.", owner=owner, project=project,
                           trust_class="agent_assertion", evidence=evidence)


def test_retraction_forgets_unsupported_claim_and_preserves_other_owner(store):
    source = {"kind": "file", "ref": "src/config.py", "excerpt": "Threshold is 42."}
    mine = _fact("luis", [source])
    other = _fact("ana", [source])

    report = engine.retract_evidence("src/config.py", owner="luis", kind="file",
                                     project="atlas")

    assert report["forgotten_ids"] == [mine["id"]]
    assert engine.get_item(mine["id"]) is None
    assert engine.get_item(other["id"]) is not None
    with pytest.raises(engine.MemoryEngineError):
        _fact("luis", [source])


def test_retraction_keeps_independently_supported_claim(store):
    item = _fact("luis", [
        {"kind": "file", "ref": "src/config.py", "excerpt": "Threshold is 42."},
        {"kind": "chat", "ref": "session-2", "excerpt": "Threshold is 42."},
    ])

    report = engine.retract_evidence("src/config.py", owner="luis", kind="file")

    assert report["retained_ids"] == [item["id"]]
    assert report["forgotten_ids"] == []
    surviving = engine.get_item(item["id"])
    assert [span["ref"] for span in surviving["evidence"]] == ["session-2"]


def test_retraction_does_not_touch_other_project_or_evidence_kind(store):
    source = {"kind": "file", "ref": "src/config.py", "excerpt": "Threshold is 42."}
    other_project = _fact("luis", [source], project="zephyr")
    other_kind = _fact("luis", [
        {"kind": "dispatch", "ref": "src/config.py", "excerpt": "Threshold is 42."}
    ])

    report = engine.retract_evidence("src/config.py", owner="luis", kind="file",
                                     project="atlas")
    assert report["forgotten_ids"] == []
    assert engine.get_item(other_project["id"]) is not None
    assert engine.get_item(other_kind["id"]) is not None


def test_retraction_forgets_claim_when_remaining_excerpt_does_not_support_it(store):
    item = _fact("luis", [
        {"kind": "file", "ref": "src/config.py", "excerpt": "Threshold is 42."},
        {"kind": "chat", "ref": "session-2", "excerpt": "The threshold changed."},
    ])

    report = engine.retract_evidence("src/config.py", owner="luis", kind="file")
    assert report["forgotten_ids"] == [item["id"]]
    assert engine.get_item(item["id"]) is None


def test_retraction_does_not_treat_unrelated_excerpt_as_generic_support(store):
    item = engine.add_item("Use the reliable workflow.", owner="luis",
                           project="atlas", trust_class="agent_assertion",
                           evidence=[
                               {"kind": "file", "ref": "guide.md",
                                "excerpt": "Use the reliable workflow."},
                               {"kind": "chat", "ref": "session-2",
                                "excerpt": "Another topic entirely."},
                           ])

    report = engine.retract_evidence("guide.md", owner="luis", kind="file")
    assert report["forgotten_ids"] == [item["id"]]
