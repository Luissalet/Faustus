"""Tests for `src.context_engine.blocks` — §7 of the Context Engine plan.

The rules being pinned here are the ones that stop a block store from becoming
the thing it replaces: a rationed always-loaded lane, a conflict instead of a
silent overwrite, attachments that actually expire, no secrets, and an import
that proposes rather than migrates.
"""

import pytest

from src.context_engine import blocks, store
from src.context_engine.contracts import ContextCandidate


@pytest.fixture()
def ce_db(tmp_path):
    """Every test gets its own database file and puts the store back after."""
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield tmp_path
    finally:
        store.use_path(None)


def _block(**over):
    fields = {"type": "project_rules", "scope": "project", "owner": "luis",
              "project_id": "p1", "title": "Repo rules", "content": "Never touch migrations.",
              "priority": 50}
    fields.update(over)
    return blocks.create_block(**fields)


# ── contract ───────────────────────────────────────────────────────────────

def test_block_round_trips_through_its_own_contract(ce_db):
    created = _block(source_refs=["file:.odysseus/MEMORY.md"])
    assert blocks.ContextBlock.parse(created.to_dict()) == created
    assert blocks.get_block(created.id) == created


def test_parse_rejects_an_unknown_key_instead_of_defaulting(ce_db):
    with pytest.raises(Exception) as exc:
        blocks.ContextBlock.parse({"id": "block_x", "typo": "project_rules"})
    assert "typo" in str(exc.value)


def test_unknown_block_type_is_refused(ce_db):
    with pytest.raises(blocks.BlockError):
        _block(type="not_a_type")


# ── revisions and conflicts ────────────────────────────────────────────────

def test_update_bumps_the_revision_and_keeps_creation_time(ce_db):
    created = _block()
    updated = blocks.update_block(created.id, {"content": "Now with a reason."})
    assert updated.revision == created.revision + 1
    assert updated.created_at == created.created_at
    assert blocks.get_block(created.id).content == "Now with a reason."


def test_a_stale_expected_revision_is_a_conflict_not_an_overwrite(ce_db):
    created = _block()
    blocks.update_block(created.id, {"content": "first writer wins"})

    with pytest.raises(blocks.BlockConflict) as exc:
        blocks.update_block(created.id, {"content": "second writer"},
                            expected_revision=created.revision)

    # The conflict carries the revision that was actually there...
    assert exc.value.revision == created.revision + 1
    assert exc.value.expected == created.revision
    # ...and nothing was written.
    assert blocks.get_block(created.id).content == "first writer wins"
    assert isinstance(exc.value, ValueError)


def test_matching_expected_revision_applies(ce_db):
    created = _block()
    updated = blocks.update_block(created.id, {"priority": 90},
                                  expected_revision=created.revision)
    assert updated.priority == 90


def test_expected_revision_must_be_a_number(ce_db):
    created = _block()
    with pytest.raises(blocks.BlockError):
        blocks.update_block(created.id, {"priority": 10}, expected_revision="1")


def test_update_refuses_a_field_it_does_not_own(ce_db):
    created = _block()
    with pytest.raises(blocks.BlockError) as exc:
        blocks.update_block(created.id, {"owner": "someone_else"})
    assert "owner" in str(exc.value)
    assert blocks.get_block(created.id).owner == "luis"


def test_delete_takes_the_attachments_with_it(ce_db):
    created = _block(scope="session")
    blocks.attach(created.id, session_id="s1")
    assert blocks.attachments(created.id)

    assert blocks.delete_block(created.id) is True
    assert blocks.get_block(created.id) is None
    assert blocks.attachments(created.id) == []
    assert blocks.delete_block(created.id) is False


# ── owner isolation ────────────────────────────────────────────────────────

def test_queries_never_cross_owners(ce_db):
    mine = _block(owner="luis", title="Mine")
    _block(owner="someone_else", title="Theirs")

    listed = blocks.list_blocks(owner="luis", project_id="p1")
    assert [b.id for b in listed] == [mine.id]
    assert [b.id for b in blocks.blocks_for(owner="luis", project_id="p1",
                                            intent="code")] == [mine.id]


def test_a_project_block_is_not_served_to_another_project(ce_db):
    _block(project_id="p1", always_loaded=True)
    assert blocks.blocks_for(owner="luis", project_id="p2") == []


# ── the ration on `always_loaded` ──────────────────────────────────────────

def test_always_loaded_is_capped_by_block_count(ce_db):
    made = [_block(title=f"rule {i}", priority=90 - i, always_loaded=True)
            for i in range(blocks.ALWAYS_LOADED_MAX_BLOCKS + 2)]

    served = blocks.blocks_for(owner="luis", project_id="p1")

    assert len(served) == blocks.ALWAYS_LOADED_MAX_BLOCKS
    # Kept by priority, highest first; the two lowest are left connectable.
    assert [b.id for b in served] == [b.id for b in made[:blocks.ALWAYS_LOADED_MAX_BLOCKS]]
    for demoted in made[blocks.ALWAYS_LOADED_MAX_BLOCKS:]:
        assert blocks.get_block(demoted.id) is not None   # still there, just not loaded


def test_always_loaded_is_capped_by_characters_before_the_count(ce_db):
    big = "x" * 3000
    made = [_block(title=f"big {i}", priority=90 - i, always_loaded=True,
                   content=big, max_chars=3000) for i in range(3)]

    served = blocks.blocks_for(owner="luis", project_id="p1")

    # 3 x 3000 = 9000 > 8000, so the third does not fit even though the block
    # count cap (5) has not been reached.
    assert [b.id for b in served] == [made[0].id, made[1].id]
    assert sum(len(b.body()) for b in served) <= blocks.ALWAYS_LOADED_MAX_CHARS


def test_audit_reports_the_ration_instead_of_hiding_it(ce_db):
    made = [_block(title=f"rule {i}", priority=90 - i, always_loaded=True)
            for i in range(blocks.ALWAYS_LOADED_MAX_BLOCKS + 1)]

    report = blocks.audit(owner="luis", project_id="p1")

    assert report["ok"] is False
    assert report["always_loaded"] == blocks.ALWAYS_LOADED_MAX_BLOCKS + 1
    assert report["always_loaded_granted"] == blocks.ALWAYS_LOADED_MAX_BLOCKS
    assert len(report["over_ration"]) == 1
    over = report["over_ration"][0]
    assert over["scope"] == "project"
    assert over["max_blocks"] == blocks.ALWAYS_LOADED_MAX_BLOCKS
    assert over["demoted"] == [made[-1].id]


def test_audit_is_clean_when_nothing_is_wrong(ce_db):
    _block(always_loaded=True)
    report = blocks.audit(owner="luis", project_id="p1")
    assert report["over_ration"] == []
    assert report["oversized"] == []
    assert report["duplicates"] == []
    assert report["contradictions"] == []
    assert report["ok"] is True


def test_audit_sees_duplicates_and_oversized_and_contradictions(ce_db):
    _block(content="Same rule, twice.")
    _block(content="Same   rule,   TWICE.")             # duplicate by normalised text
    _block(content="y" * 500, max_chars=100)            # oversized: only a prefix loads
    _block(type="active_goal", always_loaded=True, title="goal a", content="Ship OAuth")
    _block(type="active_goal", always_loaded=True, title="goal b", content="Ship SSO")

    report = blocks.audit(owner="luis", project_id="p1")

    assert len(report["duplicates"]) == 1 and len(report["duplicates"][0]["ids"]) == 2
    assert [o["max_chars"] for o in report["oversized"]] == [100]
    assert [c["type"] for c in report["contradictions"]] == ["active_goal"]
    assert report["ok"] is False


# ── attachments ────────────────────────────────────────────────────────────

def test_an_expired_attachment_is_not_served_but_the_row_survives(ce_db):
    created = _block(scope="session", always_loaded=False)
    blocks.attach(created.id, session_id="s1", expires_at="2999-01-01T00:00:00Z")
    assert [b.id for b in blocks.blocks_for(owner="luis", project_id="p1",
                                            session_id="s1")] == [created.id]

    blocks.attach(created.id, session_id="s1", expires_at="2020-01-01T00:00:00Z")

    assert blocks.blocks_for(owner="luis", project_id="p1", session_id="s1") == []
    rows = blocks.attachments(created.id)
    assert len(rows) == 1 and rows[0]["expired"] is True     # maintenance reaps it, not us


def test_an_attachment_beats_the_ration(ce_db):
    made = [_block(title=f"rule {i}", priority=90 - i, always_loaded=True)
            for i in range(blocks.ALWAYS_LOADED_MAX_BLOCKS + 1)]
    demoted = made[-1]
    assert demoted.id not in {b.id for b in blocks.blocks_for(owner="luis", project_id="p1")}

    blocks.attach(demoted.id, agent_id="worker-1")

    served = blocks.blocks_for(owner="luis", project_id="p1", agent_id="worker-1")
    assert demoted.id in {b.id for b in served}
    assert len(served) == blocks.ALWAYS_LOADED_MAX_BLOCKS + 1


def test_attach_needs_something_to_attach_to(ce_db):
    created = _block()
    with pytest.raises(blocks.BlockError):
        blocks.attach(created.id, session_id="", agent_id="")
    with pytest.raises(blocks.BlockError):
        blocks.attach("block_does_not_exist", session_id="s1")
    with pytest.raises(blocks.BlockError):
        blocks.attach(created.id, session_id="s1", expires_at="next tuesday")


def test_detaching_what_was_never_attached_is_not_an_error(ce_db):
    created = _block()
    blocks.detach(created.id, session_id="s9")
    assert blocks.attachments(created.id) == []


# ── intent routing ─────────────────────────────────────────────────────────

def test_intent_connects_matching_types_and_nothing_else(ce_db):
    failures = _block(type="known_failures", title="Traps", always_loaded=False)
    style = _block(type="style_profile", title="Voice", always_loaded=False)

    assert blocks.blocks_for(owner="luis", project_id="p1") == []
    served = {b.id for b in blocks.blocks_for(owner="luis", project_id="p1",
                                              intent="code_review")}
    assert served == {failures.id}
    assert style.id not in served
    assert "style_profile" in blocks.types_for_intent("image_generate")


def test_intent_does_not_readmit_a_block_the_ration_demoted(ce_db):
    made = [_block(type="known_failures", title=f"trap {i}", priority=90 - i,
                   always_loaded=True) for i in range(blocks.ALWAYS_LOADED_MAX_BLOCKS + 1)]

    served = {b.id for b in blocks.blocks_for(owner="luis", project_id="p1",
                                              intent="code")}

    assert made[-1].id not in served
    assert len(served) == blocks.ALWAYS_LOADED_MAX_BLOCKS


# ── secrets ────────────────────────────────────────────────────────────────

def test_a_credential_in_the_content_is_refused_by_name(ce_db):
    with pytest.raises(blocks.SecretInBlock) as exc:
        _block(content='api_key: "sk-live-9f2c8ab41d77"')
    assert exc.value.pattern == "api_key"
    assert "api_key" in str(exc.value)
    assert blocks.list_blocks(owner="luis") == []


def test_a_credential_cannot_be_smuggled_in_by_an_update(ce_db):
    created = _block()
    with pytest.raises(blocks.SecretInBlock):
        blocks.update_block(created.id, {"content": "Authorization: Bearer abcdef123456"})
    assert blocks.get_block(created.id).content == "Never touch migrations."


def test_ordinary_prose_is_not_mistaken_for_a_credential(ce_db):
    created = _block(content="Rotate the deploy key every 90 days; token_count stays 812.")
    assert blocks.secret_pattern(created.content) == ""


# ── importing project memory ───────────────────────────────────────────────

def _workspace(tmp_path):
    root = tmp_path / "ws" / blocks.PROJECT_MEMORY_DIRNAME
    root.mkdir(parents=True)
    (root / "MEMORY.md").write_text("# Index\nThe repo builds with make.\n", encoding="utf-8")
    (root / "decisions.md").write_text("DEC-1: SQLite, not Postgres.\n", encoding="utf-8")
    return {"id": "p1", "workspace": str(tmp_path / "ws")}


def test_import_project_memory_only_proposes_by_default(ce_db, tmp_path):
    project = _workspace(tmp_path)

    report = blocks.import_project_memory(project, owner="luis")

    assert report["dry_run"] is True
    assert report["created"] == []
    assert blocks.list_blocks(owner="luis", project_id="p1") == []
    assert sorted(p["path"] for p in report["proposed"]) == [
        ".odysseus/MEMORY.md", ".odysseus/decisions.md"]
    # §7.3: no Markdown note is migrated to always_loaded automatically.
    assert all(p["always_loaded"] is False for p in report["proposed"])
    assert {p["type"] for p in report["proposed"]} == {"project_rules", "decision_log"}


def test_import_writes_once_and_then_skips_what_it_already_imported(ce_db, tmp_path):
    project = _workspace(tmp_path)

    first = blocks.import_project_memory(project, owner="luis", dry_run=False)
    assert len(first["created"]) == 2
    stored = blocks.list_blocks(owner="luis", project_id="p1")
    assert len(stored) == 2
    assert all(not b.always_loaded for b in stored)
    assert all(b.trust_class == "legacy_import" for b in stored)

    second = blocks.import_project_memory(project, owner="luis", dry_run=False)
    assert second["created"] == []
    assert {s["reason"] for s in second["skipped"]} == {"already imported"}


def test_import_refuses_a_note_that_carries_a_credential(ce_db, tmp_path):
    project = _workspace(tmp_path)
    root = tmp_path / "ws" / blocks.PROJECT_MEMORY_DIRNAME
    (root / "env.md").write_text("client_secret=9f2c8ab41d7700ff\n", encoding="utf-8")

    report = blocks.import_project_memory(project, owner="luis", dry_run=False)

    assert ".odysseus/env.md" not in [p["path"] for p in report["proposed"]]
    reasons = {s["path"]: s["reason"] for s in report["skipped"]}
    assert "client_secret" in reasons[".odysseus/env.md"]


def test_import_without_a_workspace_says_so_instead_of_raising(ce_db):
    report = blocks.import_project_memory({"id": "p1"}, owner="luis")
    assert report["proposed"] == []
    assert report["skipped"][0]["reason"] == "project has no workspace bound"


# ── handing blocks to the compiler ─────────────────────────────────────────

def test_blocks_become_valid_context_candidates(ce_db):
    created = _block(always_loaded=True, content="z" * 500, max_chars=100)

    candidate = blocks.as_candidates([created])[0]

    assert ContextCandidate.parse(candidate.to_dict()) == candidate
    assert candidate.source_type == "block"
    assert candidate.source_ref == f"block:{created.id}"
    assert candidate.section == "project_rules"
    assert candidate.lanes == ("mandatory",)
    assert candidate.body == "z" * 100        # capped at max_chars, not at content
    assert candidate.degraded is True         # and the reader is told it is a prefix
