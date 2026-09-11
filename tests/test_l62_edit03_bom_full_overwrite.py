"""Lote 62 — EDIT-03: BOM preservation on a full `write_file` overwrite.

`tests/test_edit_preservation.py` (Lote 19) already proves BOM round-trips
through `edit_file`/`apply_patch` (partial edits: the BOM is an ordinary
leading U+FEFF character in the text — see `_read_text_lf`'s docstring — and
an edit elsewhere in the file never touches it) and through `write_file` when
the CALLER'S OWN content already re-includes the BOM character verbatim.

That last case is not how a real caller writes: a model rewriting a file's
full body never retypes an invisible formatting character. Before this
lote's fix, `WriteFileTool` discarded a pre-existing BOM whenever the new
`content` did not start with one — silently destroying a byte marker other
tools (Windows editors, some CSV importers) depend on, exactly the "no
destruye caracteres" failure EDIT-03's acceptance names. `WriteFileTool`
already preserves the file's CRLF convention the same way when the caller's
body doesn't say `\\r\\n`; the fix gives the BOM the same treatment.
"""
import json

import pytest

from src.agent_tools.filesystem_tools import WriteFileTool

BOM = b"\xef\xbb\xbf"


@pytest.mark.asyncio
async def test_write_file_overwrite_keeps_a_bom_the_new_content_never_retyped(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(BOM + b"hello\r\nworld\r\n")

    # The model's own new content, as a real caller would send it: no BOM
    # character anywhere in it (it cannot see or retype an invisible one).
    res = await WriteFileTool().execute(
        json.dumps({"path": str(p), "content": "hello\nnew world\n"}), {})

    assert res["exit_code"] == 0
    out = p.read_bytes()
    assert out.startswith(BOM), "the file's pre-existing BOM must survive a full overwrite"
    assert out == BOM + b"hello\r\nnew world\r\n"  # BOM kept, CRLF convention kept, content updated


@pytest.mark.asyncio
async def test_write_file_overwrite_does_not_double_a_bom_the_caller_already_included(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(BOM + b"one\r\n")

    res = await WriteFileTool().execute(
        json.dumps({"path": str(p), "content": BOM.decode("utf-8") + "two\n"}), {})

    assert res["exit_code"] == 0
    out = p.read_bytes()
    assert out == BOM + b"two\r\n"  # exactly one BOM, not two
    assert out.count(BOM) == 1


@pytest.mark.asyncio
async def test_write_file_of_a_brand_new_file_never_invents_a_bom(tmp_path):
    p = tmp_path / "new.txt"
    res = await WriteFileTool().execute(
        json.dumps({"path": str(p), "content": "fresh content\n"}), {})
    assert res["exit_code"] == 0
    out = p.read_bytes()
    assert not out.startswith(BOM)  # nothing on disk before, nothing to preserve
    assert out == b"fresh content\n"


@pytest.mark.asyncio
async def test_write_file_overwrite_of_a_non_bom_file_stays_bom_free(tmp_path):
    p = tmp_path / "plain.txt"
    p.write_bytes(b"old\n")  # bytes: text mode would write CRLF on Windows, which the tool preserves
    res = await WriteFileTool().execute(
        json.dumps({"path": str(p), "content": "new\n"}), {})
    assert res["exit_code"] == 0
    out = p.read_bytes()
    assert not out.startswith(BOM)
    assert out == b"new\n"
