"""EDIT-01 · Edición sobre versión conocida (Lote 19).

`edit_file`, `write_file` and `apply_patch` accept an optional `base_revision`
(`sha256:<hex>`, spec docs/spec/v2/Faustus_Especificacion_Integral_v2.md §34.2).
When the file on disk no longer matches it, the write is refused before
touching anything and the result is a `conflict` carrying
`error_code: BASE_REVISION_MISMATCH`, `next_action: read_current_and_reconcile`
and a trimmed base/current/proposed diff — never a silent overwrite of
whatever changed underneath the agent. Without `base_revision` every tool
behaves exactly as before (`unverified_base: true` on the result).

tests/qa/test_qa_15_edicion_concurrente.py already covers the parallel
document-editor path (routes/document/document_routes.py); this file covers
the same requisite for the raw-filesystem tools in
src/agent_tools/filesystem_tools.py, which QA-15 does not touch.
"""
import json
import os

import pytest

from src.agent_tools.filesystem_tools import (
    ApplyPatchTool,
    EditFileTool,
    WriteFileTool,
    sha256_revision,
)


def _revision_of(path: str) -> str:
    with open(path, "rb") as f:
        return sha256_revision(f.read())


# ── edit_file ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_edit_file_with_matching_base_revision_succeeds_and_returns_a_new_one(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("one\ntwo\n")
    rev = _revision_of(str(p))
    res = await EditFileTool().execute(
        json.dumps({"path": str(p), "old_string": "one", "new_string": "ONE", "base_revision": rev}), {})
    assert res["exit_code"] == 0
    assert res["base_revision"] == rev
    assert res["revision"] == _revision_of(str(p))
    assert res["revision"] != rev
    assert "unverified_base" not in res


@pytest.mark.asyncio
async def test_edit_file_with_stale_base_revision_conflicts_without_touching_the_file(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("one\ntwo\n")
    stale = _revision_of(str(p))
    p.write_text("one changed externally\ntwo\n")   # an external editor wins the race
    res = await EditFileTool().execute(
        json.dumps({"path": str(p), "old_string": "two", "new_string": "TWO", "base_revision": stale}), {})
    assert res["exit_code"] == 1
    assert res["status"] == "conflict"
    assert res["error_code"] == "BASE_REVISION_MISMATCH"
    assert res["next_action"] == "read_current_and_reconcile"
    assert res["three_way"]["base"] == "two"
    assert res["three_way"]["proposed"] == "TWO"
    assert "changed externally" in res["three_way"]["current"]
    # The user's (external) edit is never clobbered by the stale proposal.
    assert p.read_text() == "one changed externally\ntwo\n"


@pytest.mark.asyncio
async def test_edit_file_without_base_revision_is_unverified_but_unchanged_in_behavior(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("one\n")
    res = await EditFileTool().execute(
        json.dumps({"path": str(p), "old_string": "one", "new_string": "ONE"}), {})
    assert res["exit_code"] == 0
    assert res["unverified_base"] is True
    assert p.read_text() == "ONE\n"


@pytest.mark.asyncio
async def test_edit_file_rejects_a_malformed_base_revision(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("one\n")
    res = await EditFileTool().execute(
        json.dumps({"path": str(p), "old_string": "one", "new_string": "ONE", "base_revision": "not-a-hash"}), {})
    assert res["exit_code"] == 1 and "base_revision" in res["error"]
    assert p.read_text() == "one\n"


# ── write_file ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_write_file_with_stale_base_revision_conflicts_and_preserves_the_current_content(tmp_path):
    p = tmp_path / "g.txt"
    p.write_text("base content\n")
    stale = _revision_of(str(p))
    p.write_text("changed externally\n")
    res = await WriteFileTool().execute(
        json.dumps({"path": str(p), "content": "agent overwrite\n", "base_revision": stale}), {})
    assert res["exit_code"] == 1
    assert res["status"] == "conflict" and res["error_code"] == "BASE_REVISION_MISMATCH"
    assert p.read_text() == "changed externally\n"


@pytest.mark.asyncio
async def test_write_file_with_matching_base_revision_succeeds(tmp_path):
    p = tmp_path / "g.txt"
    p.write_text("base content\n")
    rev = _revision_of(str(p))
    res = await WriteFileTool().execute(
        json.dumps({"path": str(p), "content": "updated\n", "base_revision": rev}), {})
    assert res["exit_code"] == 0 and res["base_revision"] == rev
    assert p.read_text() == "updated\n"


# ── apply_patch ──────────────────────────────────────────────────────────
_PATCH = """*** Begin Patch
*** Update File: a.txt
@@
-one
+ONE
*** End Patch"""


@pytest.mark.asyncio
async def test_apply_patch_with_stale_base_revision_refuses_the_whole_patch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "a.txt"
    p.write_text("one\n")
    stale = _revision_of(str(p))
    p.write_text("one changed externally\n")
    res = await ApplyPatchTool().execute(
        json.dumps({"patch_text": _PATCH, "base_revision": stale}), {})
    assert res["exit_code"] == 1
    assert res["status"] == "conflict" and res["error_code"] == "BASE_REVISION_MISMATCH"
    assert p.read_text() == "one changed externally\n"


@pytest.mark.asyncio
async def test_apply_patch_with_matching_base_revision_succeeds_and_reports_revisions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "a.txt"
    p.write_text("one\n")
    rev = _revision_of(str(p))
    res = await ApplyPatchTool().execute(
        json.dumps({"patch_text": _PATCH, "base_revision": rev}), {})
    assert res["exit_code"] == 0
    assert res["base_revision"] == rev
    assert res["revisions"][str(p)] == _revision_of(str(p))
    assert p.read_text() == "ONE\n"


# ── revert-proof: the mismatch is really caught by base_revision, not by
# something else. Feeding today's (matching) content but a WRONG hash must
# still conflict — this is the line that a broken "always succeed" stub
# could slip past. ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_edit_file_conflict_is_driven_by_the_hash_not_by_content_having_changed(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("one\n")
    wrong_hash = "sha256:" + "0" * 64
    res = await EditFileTool().execute(
        json.dumps({"path": str(p), "old_string": "one", "new_string": "ONE", "base_revision": wrong_hash}), {})
    assert res["status"] == "conflict"
    assert p.read_text() == "one\n"
