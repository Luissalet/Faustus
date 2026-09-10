"""L20 (integrates L18, item 4): `read_file` did not attach any evidence —
`src.context_ledger.evidence_for_read` existed (CTX-03) but had no caller,
and `write_file`/`edit_file`/`apply_patch` accept a `base_revision`
(EDIT-01, lote 19) that a model can only supply if a previous `read_file`
told it what the file's current revision is. Every successful `read_file`
now attaches `evidence_refs` (an `EvidenceRef` pinning the exact window
shown) and `revision` (the whole file's current `sha256:<hex>`, the same
format/hashing `edit_file` already returns) so the model has something to
pass back as `base_revision` on its next edit.

Mirrors `tests/test_read_plan_outline.py`'s workspace fixture and calling
convention (not owned by this lote, left untouched).
"""
import json

import pytest

from src.agent_tools.filesystem_tools import ReadFileTool, _read_text_lf
from src.contracts.tool import EvidenceRef
from src.tool_execution import _active_workspace


@pytest.fixture
def ws(tmp_path):
    token = _active_workspace.set(str(tmp_path))
    try:
        yield tmp_path
    finally:
        _active_workspace.reset(token)


async def read(path, ctx=None, **args):
    payload = {"path": str(path), **args} if args else str(path)
    content = json.dumps(payload) if args else payload
    return await ReadFileTool().execute(content, ctx or {})


@pytest.mark.asyncio
async def test_whole_file_read_attaches_evidence_and_matching_revision(ws):
    f = ws / "notes.txt"
    f.write_text("line one\nline two\nline three\n")

    result = await read(f)

    assert result["exit_code"] == 0
    assert "evidence_refs" in result and len(result["evidence_refs"]) == 1
    ev_mapping = result["evidence_refs"][0]
    ev = EvidenceRef.from_mapping(ev_mapping)
    assert ev.source_type == "conversation" or ev.source_type == "file"
    # It is specifically a file evidence ref (evidence_for_read's contract).
    assert ev.source_type == "file"
    assert ev.locator.kind == "lines"

    # revision matches exactly what edit_file computes for the same file
    # right now — sha256_revision's own format and hashing.
    _, _, expected_revision = _read_text_lf(str(f))
    assert result["revision"] == expected_revision
    assert result["revision"].startswith("sha256:")


@pytest.mark.asyncio
async def test_ranged_read_evidence_locator_matches_the_lines_actually_shown(ws):
    f = ws / "notes.txt"
    f.write_text("\n".join(f"line {i}" for i in range(1, 21)) + "\n")

    result = await read(f, offset=5, limit=3)

    assert result["output"] == "line 5\nline 6\nline 7\n"
    ev = EvidenceRef.from_mapping(result["evidence_refs"][0])
    assert ev.locator.value == "5-7"


@pytest.mark.asyncio
async def test_evidence_owner_and_project_come_from_ctx(ws):
    f = ws / "notes.txt"
    f.write_text("hello\n")

    result = await read(f, ctx={"owner": "alice", "project_id": "proj-1"})

    ev = EvidenceRef.from_mapping(result["evidence_refs"][0])
    assert ev.owner_id == "alice"
    assert ev.project_id == "proj-1"


@pytest.mark.asyncio
async def test_the_revision_read_file_returns_is_accepted_as_base_revision_by_edit_file(ws):
    """The actual point of the wiring: read, then edit using exactly the
    revision read_file handed back, with no separate probe needed."""
    from src.agent_tools.filesystem_tools import EditFileTool

    f = ws / "code.py"
    f.write_text("x = 1\n")

    read_result = await read(f)
    revision = read_result["revision"]

    edit_result = await EditFileTool().execute(json.dumps({
        "path": str(f),
        "old_string": "x = 1",
        "new_string": "x = 2",
        "base_revision": revision,
    }), {})

    assert edit_result["exit_code"] == 0
    assert "error" not in edit_result
    assert f.read_text() == "x = 2\n"
