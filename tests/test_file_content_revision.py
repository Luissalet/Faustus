"""FileSource revisions identify captured projection bytes, never stat metadata."""
import hashlib
import os

import pytest

from src.context_engine.adapters import files
from src.context_engine.candidates import RetrievalRequest
from src.context_engine.contracts import ContextExecution, ContextRequest


async def fetch(root, ref="file:synthetic.txt"):
    request = ContextRequest(execution=ContextExecution(owner="synthetic", workspace=str(root)))
    return await files.FileSource().fetch(ref, RetrievalRequest(request=request))


def assert_captured(candidate):
    assert candidate.meta["revision_scope"] == "captured_body"
    assert candidate.source_revision == "captured_utf8_sha256:" + hashlib.sha256(candidate.body.encode("utf-8")).hexdigest()


async def test_equal_size_and_mtime_changed_content_changes_revision(tmp_path):
    path = tmp_path / "synthetic.txt"
    path.write_text("old=1", encoding="utf-8")
    stat = path.stat()
    first = await fetch(tmp_path)
    path.write_text("new=2", encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = await fetch(tmp_path)
    assert first.meta["bytes"] == second.meta["bytes"]
    assert first.meta["modified"] == second.meta["modified"]
    assert first.source_revision != second.source_revision
    assert_captured(first)
    assert_captured(second)


async def test_unchanged_projection_is_stable_despite_mtime_change(tmp_path):
    path = tmp_path / "synthetic.txt"
    path.write_text("stable", encoding="utf-8")
    first = await fetch(tmp_path)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
    second = await fetch(tmp_path)
    assert first.meta["modified"] != second.meta["modified"]
    assert first.source_revision == second.source_revision


async def test_actual_empty_file_has_digest_but_missing_does_not(tmp_path):
    path = tmp_path / "synthetic.txt"
    path.write_bytes(b"")
    empty = await fetch(tmp_path)
    assert empty.body == ""
    assert_captured(empty)
    path.unlink()
    missing = await fetch(tmp_path)
    assert missing.body == "" and missing.source_revision == ""
    assert missing.meta["revision_scope"] == "unavailable"


@pytest.mark.parametrize("kind", ["binary", "large", "sensitive", "denied"])
async def test_withheld_or_failed_read_has_no_content_revision(tmp_path, monkeypatch, kind):
    path = tmp_path / (".env" if kind == "sensitive" else "synthetic.txt")
    path.write_bytes(b"a\x00b" if kind == "binary" else (b"x" * (files.MAX_FILE_CHARS + 1) if kind == "large" else b"synthetic"))
    if kind == "denied":
        real_open = open

        def denied(p, *a, **kw):
            if str(p) == str(path):
                raise PermissionError("synthetic unreadable file")
            return real_open(p, *a, **kw)

        monkeypatch.setattr("builtins.open", denied)
    candidate = await fetch(tmp_path, "file:" + path.name)
    assert candidate.body == "" and candidate.source_revision == ""
    assert candidate.meta["revision_scope"] == "unavailable"
    assert candidate.meta["withheld"] == {"binary": "unreadable_or_binary", "large": "too_large", "sensitive": "sensitive", "denied": "unreadable_or_binary"}[kind]


async def test_window_revision_tracks_only_captured_window(tmp_path):
    path = tmp_path / "synthetic.txt"
    path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    first = await fetch(tmp_path, "file:synthetic.txt#L2-L3")
    path.write_text("ONE\ntwo\nthree\nfour\n", encoding="utf-8")
    outside = await fetch(tmp_path, "file:synthetic.txt#L2-L3")
    assert first.source_revision == outside.source_revision
    path.write_text("ONE\nTWO\nthree\nfour\n", encoding="utf-8")
    inside = await fetch(tmp_path, "file:synthetic.txt#L2-L3")
    assert first.source_revision != inside.source_revision
    assert_captured(inside)


async def test_truncated_window_revision_ignores_uncaptured_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "MAX_FILE_CHARS", 60)
    path = tmp_path / "synthetic.txt"
    body = "\n".join(f"line {i}" for i in range(1, 101))
    path.write_text(body, encoding="utf-8")
    first = await fetch(tmp_path, "file:synthetic.txt#L1-L100")
    assert "truncated" in first.body
    path.write_text(body.replace("line 100", "tail 100"), encoding="utf-8")
    outside = await fetch(tmp_path, "file:synthetic.txt#L1-L100")
    assert first.source_revision == outside.source_revision
    path.write_text(body.replace("line 1\n", "LINE 1\n"), encoding="utf-8")
    inside = await fetch(tmp_path, "file:synthetic.txt#L1-L100")
    assert first.source_revision != inside.source_revision
    assert_captured(inside)


async def test_revision_identifies_normalized_utf8_not_raw_bytes(tmp_path):
    path = tmp_path / "synthetic.txt"
    path.write_bytes(b"a\r\nb\r\n")
    first = await fetch(tmp_path)
    path.write_bytes(b"a\nb\n")
    second = await fetch(tmp_path)
    assert first.body == second.body and first.source_revision == second.source_revision
    assert_captured(first)
