import subprocess
import sys
import tracemalloc

import pytest

from src import bg_jobs


def expected(raw):
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    text = raw.decode(encoding, errors="replace")
    if len(text) <= 16000:
        return text
    return text[:8000] + "\n…[truncated]…\n" + text[-8000:]


@pytest.mark.parametrize("encoding,bom", [("utf-8", b""), ("utf-8", b"\xef\xbb\xbf"), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")])
@pytest.mark.parametrize("text", ["", "😀" * 15999, "α😀abc" * 19000 + "\ufeffTAIL", "abc" * 7000], ids=["empty", "small-emoji", "large-mixed", "large-ascii"])
def test_output_matches_full_decode(tmp_path, encoding, bom, text):
    raw = bom + text.encode(encoding)
    path = tmp_path / "log"
    path.write_bytes(raw)
    assert bg_jobs._read_output({"log_path": str(path)}) == expected(raw)


@pytest.mark.parametrize("raw", [b"\x80" * 100000, b"abc\xff\x80" * 30000, b"\xff\xfe" + b"a\x00" * 40000 + b"\x00", b"\xfe\xff" + b"\x00a" * 40000 + b"\xff"], ids=["continuation-only", "utf8", "utf16-le", "utf16-be"])
def test_malformed_decode_compatible(tmp_path, raw):
    path = tmp_path / "log"
    path.write_bytes(raw)
    assert bg_jobs._read_output({"log_path": str(path)}) == expected(raw)


def test_real_child_bounded_reads_and_allocation(tmp_path, monkeypatch):
    path = tmp_path / "child.log"
    with path.open("wb") as output:
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'A'*(8*1024*1024)); sys.stdout.buffer.flush()"], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL)
        assert child.wait(timeout=30) == 0
    original = type(path).open
    reads = []

    class Spy:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def fileno(self): return self.stream.fileno()
        def seek(self, offset): return self.stream.seek(offset)
        def read(self, size):
            reads.append(size)
            return self.stream.read(size)

    def open_spy(target, *args, **kwargs):
        stream = original(target, *args, **kwargs)
        return Spy(stream) if target == path else stream

    monkeypatch.setattr(type(path), "open", open_spy)
    tracemalloc.start()
    try:
        result = bg_jobs._read_output({"log_path": str(path)})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == "A" * 8000 + "\n…[truncated]…\n" + "A" * 8000
    assert len(reads) == 2 and max(reads) <= 64017
    assert sum(reads) <= 128033
    assert peak < 1024 * 1024


def test_missing_log_and_exit_reader_unchanged(tmp_path):
    assert bg_jobs._read_output({"log_path": str(tmp_path / "missing")}) == ""
    exit_path = tmp_path / "exit"
    exit_path.write_bytes(b"\xff\xfe" + "12".encode("utf-16-le"))
    assert bg_jobs._read_job_text(exit_path) == "12"


@pytest.mark.parametrize("mode", ["grow", "shrink"])
def test_log_resize_after_fstat_is_bounded(tmp_path, monkeypatch, mode):
    path = tmp_path / "moving.log"
    path.write_bytes(b"A" * 100000)
    original = bg_jobs.os.fstat
    def resize(fd):
        captured = original(fd)
        if mode == "grow":
            with path.open("ab") as stream:
                stream.write(b"B" * 100000)
        else:
            path.write_bytes(b"short")
        return captured
    monkeypatch.setattr(bg_jobs.os, "fstat", resize)
    output = bg_jobs._read_output({"log_path": str(path)})
    assert len(output) <= 16015
    if mode == "grow":
        assert "B" not in output
    else:
        assert "short" in output


@pytest.mark.parametrize("encoding,bom", [("utf-8", b""), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")])
@pytest.mark.parametrize("padding", [0, 1, 2, 3])
def test_tail_boundary_keeps_complete_multibyte_characters(tmp_path, encoding, bom, padding):
    raw = bom + ("😀" * 30000 + "a" * padding).encode(encoding)
    path = tmp_path / "boundary.log"
    path.write_bytes(raw)
    assert bg_jobs._read_output({"log_path": str(path)}) == expected(raw)
