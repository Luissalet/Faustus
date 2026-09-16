"""Unit coverage for src/tool_result_offload.py, underneath the A12/A13
acceptance tests in tests/acceptance/. Real artifact store/DB throughout —
no mock of the module under test.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from src import artifact_store, settings, tool_result_offload as offload


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "offload.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    db_mod.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    yield engine
    engine.dispose()


def test_default_threshold_is_20000_and_reads_the_live_setting(monkeypatch):
    assert settings.DEFAULT_SETTINGS["agent_tool_result_offload_chars"] == 20000
    assert offload.offload_threshold_chars() == 20000
    monkeypatch.setattr(offload, "get_setting", lambda key, default=None: 5000)
    assert offload.offload_threshold_chars() == 5000


def test_small_result_passes_through_untouched(own_database):
    result = {"output": "fine", "exit_code": 0}
    out = offload.offload_if_oversized(result, owner="alice", threshold_chars=20000)
    assert out is result


def test_oversized_result_is_stored_and_truncated(own_database):
    big = {"output": "x" * 30000, "exit_code": 0}
    out = offload.offload_if_oversized(
        big, owner="alice", session_id="s1", run_id="r1", call_id="c1",
        tool="bash", threshold_chars=1000)
    assert out is not big
    assert out[offload.OFFLOAD_MARKER] is True
    assert len(out["output"]) < len(big["output"])
    assert out["artifact_id"]
    assert out["exit_code"] == 0  # non-string fields pass through untouched


def test_offload_is_idempotent_on_its_own_output(own_database):
    big = {"output": "y" * 30000}
    once = offload.offload_if_oversized(
        big, owner="alice", session_id="s1", run_id="r1", call_id="c1",
        tool="bash", threshold_chars=1000)
    twice = offload.offload_if_oversized(once, owner="alice", threshold_chars=1000)
    assert twice is once


def test_repeated_offload_of_the_same_call_reuses_one_artifact(own_database):
    big = {"output": "z" * 30000}
    first = offload.offload_if_oversized(
        dict(big), owner="alice", session_id="s1", run_id="r1", call_id="c1",
        tool="bash", threshold_chars=1000)
    second = offload.offload_if_oversized(
        dict(big), owner="alice", session_id="s1", run_id="r1", call_id="c1",
        tool="bash", threshold_chars=1000)
    assert first["artifact_id"] == second["artifact_id"]


def test_no_owner_means_no_storage_and_an_honest_note(own_database):
    big = {"output": "w" * 30000}
    out = offload.offload_if_oversized(big, owner="", threshold_chars=1000)
    assert out[offload.OFFLOAD_MARKER] is True
    assert "artifact_id" not in out
    assert "NOT durably stored" in out["offload_note"]
    assert len(out["output"]) < len(big["output"])


def test_read_artifact_range_by_offsets_and_query(own_database):
    text_body = json.dumps({"output": "needle-in-a-haystack " + "pad" * 5000})
    big = {"output": "needle-in-a-haystack " + "pad" * 5000}
    out = offload.offload_if_oversized(
        big, owner="alice", session_id="s1", run_id="r1", call_id="c1",
        tool="bash", threshold_chars=1000)
    artifact_id = out["artifact_id"]

    ranged = offload.read_artifact_range(artifact_id, owner="alice", start=0, end=50)
    assert ranged["total_chars"] > 0
    assert len(ranged["text"]) == 50

    found = offload.read_artifact_range(artifact_id, owner="alice", query="needle")
    assert found["matches"] and "needle" in found["matches"][0]["text"]

    missing = offload.read_artifact_range(artifact_id, owner="alice", query="no-such-term")
    assert missing["matches"] == []


def test_read_artifact_range_denies_wrong_owner_and_unknown_id(own_database):
    big = {"output": "v" * 30000}
    out = offload.offload_if_oversized(
        big, owner="alice", session_id="s1", run_id="r1", call_id="c1",
        tool="bash", threshold_chars=1000)
    artifact_id = out["artifact_id"]

    with pytest.raises(offload.ArtifactAccessDenied):
        offload.read_artifact_range(artifact_id, owner="mallory")
    with pytest.raises(offload.ArtifactAccessDenied):
        offload.read_artifact_range("occ_does_not_exist", owner="alice")
