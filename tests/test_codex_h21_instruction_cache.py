"""Instruction rendering follows content, never an mtime-only identity."""
import hashlib
import os

import pytest

from src import project_instructions as instructions
from src import workspace_trust as trust


@pytest.fixture
def settings(tmp_path, monkeypatch):
    values = {"agent_workspace_trust": "strict"}
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(trust, "DATA_DIR", str(tmp_path / "trust-store"))
    instructions.invalidate()
    yield values
    instructions.invalidate()


@pytest.mark.parametrize("elapsed", [0, 10])
def test_same_mtime_same_size_replacement_updates_within_and_after_ttl(tmp_path, settings, monkeypatch, elapsed):
    path = tmp_path / "AGENTS.md"
    path.write_text("Use runner alpha.", encoding="utf-8")
    original = path.stat()
    clock = [100.0]
    monkeypatch.setattr(instructions.time, "time", lambda: clock[0])
    first = instructions.block(str(tmp_path))
    path.write_text("Use runner bravo.", encoding="utf-8")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    clock[0] += elapsed
    assert path.stat().st_size == original.st_size
    assert path.stat().st_mtime_ns == original.st_mtime_ns
    second = instructions.block(str(tmp_path))
    assert "alpha" in first and "alpha" not in second and "bravo" in second


def test_normal_edit_deletion_precedence_and_limit_update_immediately(tmp_path, settings):
    path = tmp_path / "AGENTS.md"
    path.write_text("before", encoding="utf-8")
    assert "before" in instructions.block(str(tmp_path))
    path.write_text("after" * 200, encoding="utf-8")
    settings["agent_project_instructions_max_chars"] = 500
    assert "truncated" in instructions.block(str(tmp_path))
    settings["agent_project_instructions_max_chars"] = 2000
    assert "truncated" not in instructions.block(str(tmp_path))
    (tmp_path / "CLAUDE.md").write_text("fallback", encoding="utf-8")
    settings["agent_project_instructions_files"] = ["CLAUDE.md", "AGENTS.md"]
    assert "fallback" in instructions.block(str(tmp_path))
    settings["agent_project_instructions_files"] = ["AGENTS.md"]
    path.unlink()
    assert instructions.block(str(tmp_path)) == ""


def test_excerpt_digest_is_of_rendered_text_not_an_approval(tmp_path, settings):
    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"  Rule\r\nSecond line  ")
    info = instructions.read(str(tmp_path))
    assert info["text"] == "Rule\nSecond line"
    assert info["text_sha256"] == hashlib.sha256(info["text"].encode()).hexdigest()
    assert info["path"] == str(path) and info["rel"] == "AGENTS.md"
    assert not trust.instructions_trusted(str(tmp_path))


def test_real_trust_digest_invalidates_then_reapproval_uses_current_bytes(tmp_path, settings):
    path = tmp_path / "AGENTS.md"
    path.write_text("Use runner alpha.", encoding="utf-8")
    original = path.stat()
    first_digest = trust.digest_for(str(tmp_path))
    assert trust.trust(str(tmp_path), first_digest, by="test")["ok"]
    assert "alpha" in instructions.block(str(tmp_path), trusted=trust.instructions_trusted(str(tmp_path)))
    path.write_text("Use runner bravo.", encoding="utf-8")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert not trust.instructions_trusted(str(tmp_path))
    note = instructions.block(str(tmp_path), trusted=False)
    assert "NOT approved" in note and "alpha" not in note and "bravo" not in note
    assert not trust.trust(str(tmp_path), first_digest, by="test")["ok"]
    assert trust.trust(str(tmp_path), trust.digest_for(str(tmp_path)), by="test")["ok"]
    updated = instructions.block(str(tmp_path), trusted=trust.instructions_trusted(str(tmp_path)))
    assert "bravo" in updated and "alpha" not in updated
    assert trust.revoke(str(tmp_path))["ok"]
    assert "bravo" not in instructions.block(str(tmp_path), trusted=trust.instructions_trusted(str(tmp_path)))
