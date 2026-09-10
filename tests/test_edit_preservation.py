"""EDIT-03 · Preservación del contenido (Lote 19).

`edit_file`/`write_file`/`apply_patch` must not rewrite a file's EOL
convention (CRLF vs LF), strip its BOM, or touch its permission bits when
editing one line — only the bytes that actually changed should differ.
`_read_text_lf`/`_write_text_lf` (src/agent_tools/filesystem_tools.py) already
detect and preserve CRLF, and never re-encode the file's leading BOM (opened
as plain "utf-8", not "utf-8-sig", the BOM stays a literal character in the
text and round-trips through any edit that does not touch it); permissions
survive because `_write_text_lf` opens the EXISTING path and truncates it
rather than replacing the inode. This file is the regression test that
requisite EDIT-03 asks for — a CRLF+BOM fixture, checked byte-for-byte.
"""
import json
import os
import stat

import pytest

from src.agent_tools.filesystem_tools import ApplyPatchTool, EditFileTool, WriteFileTool

BOM = b"\xef\xbb\xbf"


def _crlf_bom_file(tmp_path, name="f.txt"):
    p = tmp_path / name
    raw = BOM + b"line one\r\nline two\r\nline three\r\n"
    p.write_bytes(raw)
    return p, raw


@pytest.mark.asyncio
async def test_edit_file_preserves_crlf_and_bom_and_only_touches_the_changed_line(tmp_path):
    p, raw = _crlf_bom_file(tmp_path)
    os.chmod(p, 0o754)
    res = await EditFileTool().execute(
        json.dumps({"path": str(p), "old_string": "line two", "new_string": "LINE TWO"}), {})
    assert res["exit_code"] == 0
    out = p.read_bytes()
    assert out.startswith(BOM)                                   # BOM survives
    assert out == BOM + b"line one\r\nLINE TWO\r\nline three\r\n"  # CRLF survives, only line 2 changed
    if os.name != "nt":  # Windows has no POSIX mode bits to preserve
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o754          # permissions survive


@pytest.mark.asyncio
async def test_write_file_overwrite_preserves_the_existing_crlf_convention(tmp_path):
    p, raw = _crlf_bom_file(tmp_path)
    res = await WriteFileTool().execute(
        json.dumps({"path": str(p), "content": BOM.decode("utf-8") + "one\ntwo\n"}), {})
    assert res["exit_code"] == 0
    out = p.read_bytes()
    # write_file's body was given with LF; the file's own CRLF convention wins.
    assert out == BOM + b"one\r\ntwo\r\n"


@pytest.mark.asyncio
async def test_apply_patch_update_preserves_crlf_and_bom(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p, raw = _crlf_bom_file(tmp_path)
    patch = """*** Begin Patch
*** Update File: f.txt
@@
-line two
+LINE TWO
*** End Patch"""
    res = await ApplyPatchTool().execute(patch, {})
    assert res["exit_code"] == 0
    out = p.read_bytes()
    assert out.startswith(BOM)
    assert out == BOM + b"line one\r\nLINE TWO\r\nline three\r\n"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="no executable bit on Windows")
async def test_executable_bit_survives_an_edit(tmp_path):
    p = tmp_path / "script.sh"
    p.write_text("#!/bin/sh\necho one\n")
    os.chmod(p, 0o755)
    res = await EditFileTool().execute(
        json.dumps({"path": str(p), "old_string": "echo one", "new_string": "echo two"}), {})
    assert res["exit_code"] == 0
    assert stat.S_IMODE(os.stat(p).st_mode) & stat.S_IXUSR
    assert p.read_text() == "#!/bin/sh\necho two\n"


# ── revert-proof: universal-newline text mode (the naive `open(path, "r")`)
# is exactly the bug EDIT-03 guards against — it silently rewrites every
# CRLF to LF. Demonstrate that mode on the same fixture to show the assertion
# above is real, not tautological. ─────────────────────────────────────────
@pytest.mark.skipif(os.name == "nt", reason="text mode on Windows re-emits CRLF, so the demonstration reads differently")
def test_naive_text_mode_would_have_destroyed_the_crlf_convention(tmp_path):
    p, raw = _crlf_bom_file(tmp_path)
    with open(p, "r", encoding="utf-8") as f:     # universal newlines: the bug
        text = f.read()
    with open(p, "w", encoding="utf-8") as f:
        f.write(text.replace("line two", "LINE TWO"))
    out = p.read_bytes()
    assert b"\r\n" not in out                      # exactly the corruption EDIT-03 prevents
