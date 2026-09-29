"""Checked instruction bytes survive edits between trust and prompt rendering."""
import dataclasses
import hashlib
from types import SimpleNamespace

import pytest

from src import project_instructions as pi
from src import workspace_trust as wt


@pytest.fixture
def repo(tmp_path, monkeypatch):
    values = {"agent_workspace_trust": "strict"}
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(wt, "DATA_DIR", str(tmp_path / "store"))
    root = tmp_path / "repo"
    root.mkdir()
    pi.invalidate()
    yield root, values
    pi.invalidate()


def _approve(root):
    digest = wt.digest_for(str(root))
    assert wt.trust(str(root), digest, by="synthetic-test")["ok"]
    return digest


def test_snapshot_keeps_checked_bytes_and_digest_when_file_changes(repo, monkeypatch):
    root, _ = repo
    path = root / "AGENTS.md"
    path.write_text("APPROVED_ALPHA", encoding="utf-8")
    digest = _approve(root)
    snapshot = wt.instructions_snapshot(str(root))
    path.write_text("UNAPPROVED_BRAVO", encoding="utf-8")
    monkeypatch.setattr(pi, "read", lambda *_: pytest.fail("snapshot must not reopen instructions"))
    assert snapshot.trusted and not snapshot.degraded and not snapshot.legacy_read
    assert snapshot.digest == digest
    assert wt._digest_from_parts([{
        "rel": p.rel, "bytes": p.size, "_data": p.data,
    } for p in snapshot.files]) == digest
    assert "APPROVED_ALPHA" in pi.block_from_snapshot(snapshot)
    assert "UNAPPROVED_BRAVO" not in pi.block_from_snapshot(snapshot)
    assert wt.state_for(str(root))["state"] == "changed"


def test_priority_is_frozen_even_when_new_preferred_file_appears(repo):
    root, values = repo
    (root / "CLAUDE.md").write_text("APPROVED_FALLBACK", encoding="utf-8")
    _approve(root)
    snapshot = wt.instructions_snapshot(str(root))
    (root / "AGENTS.md").write_text("UNAPPROVED_PRIORITY", encoding="utf-8")
    values["agent_project_instructions_files"] = ["AGENTS.md"]
    assert snapshot.selected.rel == "CLAUDE.md"
    assert "APPROVED_FALLBACK" in pi.block_from_snapshot(snapshot)
    assert "UNAPPROVED_PRIORITY" not in pi.block_from_snapshot(snapshot)


def test_empty_snapshot_does_not_discover_new_file(repo):
    root, _ = repo
    snapshot = wt.instructions_snapshot(str(root))
    assert snapshot.state == wt.STATE_NONE and snapshot.selected is None
    (root / "AGENTS.md").write_text("UNAPPROVED_NEW", encoding="utf-8")
    assert pi.read_snapshot(snapshot) == {}
    assert pi.block_from_snapshot(snapshot) == ""


def test_changed_before_capture_is_excluded(repo):
    root, _ = repo
    path = root / "AGENTS.md"
    path.write_text("APPROVED_ALPHA", encoding="utf-8")
    _approve(root)
    path.write_text("UNAPPROVED_BRAVO", encoding="utf-8")
    snapshot = wt.instructions_snapshot(str(root))
    assert not snapshot.trusted and snapshot.state == wt.STATE_CHANGED
    assert pi.read_snapshot(snapshot) == {}
    block = pi.block_from_snapshot(snapshot)
    assert "NOT approved" in block
    assert "UNAPPROVED_BRAVO" not in block


@pytest.mark.parametrize("limit", [500, 6000, 60000])
def test_snapshot_matches_live_utf8_newline_excerpt_cap_and_hash(repo, limit):
    root, values = repo
    values["agent_project_instructions_max_chars"] = limit
    raw = b"\r\nstart\r" + ("\U0001f30d" * (limit + 10)).encode("utf-8") + b"\xff\r\nend"
    (root / "AGENTS.md").write_bytes(raw)
    _approve(root)
    snapshot = wt.instructions_snapshot(str(root))
    actual = pi.read_snapshot(snapshot)
    assert actual == pi.read(str(root))
    assert actual["truncated"] and actual["chars"] == limit
    assert actual["text_sha256"] == hashlib.sha256(actual["text"].encode("utf-8")).hexdigest()


def test_snapshot_decodes_invalid_utf8_and_crlf_like_live_read(repo):
    root, _ = repo
    (root / "AGENTS.md").write_bytes(b"\r\nalpha\xff\rbravo\r\n")
    _approve(root)
    snapshot = wt.instructions_snapshot(str(root))
    assert pi.read_snapshot(snapshot) == pi.read(str(root))
    assert pi.read_snapshot(snapshot)["text"] == "alpha\ufffd\nbravo"


def test_digest_covers_all_captured_files_and_retains_existing_byte_cap(repo):
    root, _ = repo
    (root / "AGENTS.md").write_bytes(b"a" * (wt._MAX_FILE_BYTES + 10))
    (root / "CLAUDE.md").write_text("SECOND_FILE", encoding="utf-8")
    digest = _approve(root)
    snapshot = wt.instructions_snapshot(str(root))
    assert snapshot.digest == digest
    assert len(snapshot.files) == 2
    selected = snapshot.selected
    assert selected.size == wt._MAX_FILE_BYTES + 10
    assert len(selected.data) == wt._MAX_FILE_BYTES
    assert pi.read_snapshot(snapshot) == pi.read(str(root))
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.trusted = False
    with pytest.raises(dataclasses.FrozenInstanceError):
        selected.data = b"replacement"
    assert isinstance(snapshot.files, tuple) and isinstance(selected.data, bytes)


def test_off_keeps_legacy_behavior_without_store_or_snapshot_file_reads(repo, monkeypatch):
    root, values = repo
    values["agent_workspace_trust"] = "off"
    path = root / "AGENTS.md"
    path.write_text("alpha", encoding="utf-8")
    monkeypatch.setattr(wt, "file_parts", lambda *a, **k: pytest.fail("off must skip digest"))
    snapshot = wt.instructions_snapshot(str(root))
    path.write_text("bravo", encoding="utf-8")
    assert snapshot.trusted and snapshot.legacy_read and snapshot.mode == "off"
    assert "bravo" in pi.block_from_snapshot(snapshot)


def test_ask_autoapproval_and_degraded_policy_are_preserved(repo, monkeypatch):
    root, values = repo
    values["agent_workspace_trust"] = "ask"
    (root / "AGENTS.md").write_text("alpha", encoding="utf-8")
    monkeypatch.setattr(wt, "has_checkpoint_history", lambda _: True)
    snapshot = wt.instructions_snapshot(str(root))
    assert snapshot.trusted and not snapshot.degraded
    assert wt.state_for(str(root))["state"] == wt.STATE_TRUSTED
    assert wt.revoke(str(root))["ok"]
    monkeypatch.setattr(wt, "_save_locked", lambda _: False)
    degraded = wt.instructions_snapshot(str(root))
    assert degraded.trusted and degraded.degraded
    assert "alpha" in pi.block_from_snapshot(degraded)


def test_failure_keeps_explicit_fail_open_legacy_path(repo, monkeypatch):
    root, _ = repo
    (root / "AGENTS.md").write_text("alpha", encoding="utf-8")
    def fail(*a, **k):
        raise OSError("synthetic error")
    monkeypatch.setattr(wt, "resolve", fail)
    snapshot = wt.instructions_snapshot(str(root))
    assert snapshot.trusted and snapshot.degraded and snapshot.legacy_read
    assert "alpha" in pi.block_from_snapshot(snapshot)


def test_compactor_uses_captured_bytes_after_edit(repo, monkeypatch):
    from src.context_compactor import post_compact_reminder
    root, _ = repo
    path = root / "AGENTS.md"
    path.write_text("APPROVED_ALPHA", encoding="utf-8")
    _approve(root)
    monkeypatch.setattr("services.projects.project_for_session", lambda *a: {"workspace": str(root)})
    monkeypatch.setattr("services.objectives.objectives_block", lambda *a, **k: "")
    capture = wt.instructions_snapshot
    def edit_after_capture(workspace):
        snapshot = capture(workspace)
        path.write_text("UNAPPROVED_BRAVO", encoding="utf-8")
        return snapshot
    monkeypatch.setattr(wt, "instructions_snapshot", edit_after_capture)
    reminder = post_compact_reminder(SimpleNamespace(id="synthetic-session"))
    assert reminder is not None
    assert "APPROVED_ALPHA" in reminder["content"]
    assert "UNAPPROVED_BRAVO" not in reminder["content"]
