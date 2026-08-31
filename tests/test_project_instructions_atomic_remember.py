"""BUG 8 — `remember()` rewrote AGENTS.md non-atomically.

`open(path, "w")` truncates the USER's file first and only then fills it in, to
add a single bullet. A crash, an OOM kill or a full disk in between leaves the
repository's instructions file truncated or empty — and that file goes into the
system prompt of every turn. The repo already has the right pattern
(`core.atomic_io`, `services/review_state.py`): write a sibling temp file,
fsync, `os.replace`.

The rewrite must stay byte-identical to what it produces today, CRLF included
(`newline=""`), and must keep the file's permission bits.
"""

import os
import stat

import pytest

from src import project_instructions as pi


@pytest.fixture
def ws(tmp_path):
    pi.invalidate()
    root = tmp_path / "repo"
    root.mkdir()
    yield root
    pi.invalidate()


def _leftovers(root):
    return sorted(n for n in os.listdir(root) if "tmp" in n.lower())


# ── byte-for-byte output ───────────────────────────────────────────────────

def test_crlf_file_keeps_crlf_and_exact_layout(ws):
    path = ws / "AGENTS.md"
    path.write_bytes(b"# Repo\r\n\r\n## Rules\r\n\r\n- Use pnpm.\r\n")
    res = pi.remember(str(ws), "Never touch migrations")
    assert not res.get("error") and res["duplicate"] is False
    assert path.read_bytes() == (
        b"# Repo\r\n\r\n## Rules\r\n\r\n- Use pnpm.\r\n\r\n"
        + pi.REMEMBER_HEADING.encode() + b"\r\n\r\n- Never touch migrations\r\n"
    )
    assert _leftovers(ws) == [], "a temp file was left next to the user's AGENTS.md"


def test_lf_file_stays_lf(ws):
    path = ws / "AGENTS.md"
    path.write_bytes(b"# Repo\n\n" + pi.REMEMBER_HEADING.encode() + b"\n\n- One.\n")
    pi.remember(str(ws), "Two.")
    assert path.read_bytes() == (
        b"# Repo\n\n" + pi.REMEMBER_HEADING.encode() + b"\n\n- One.\n- Two.\n"
    )
    assert b"\r\n" not in path.read_bytes()
    assert _leftovers(ws) == []


def test_created_file_and_duplicate_are_unchanged(ws):
    res = pi.remember(str(ws), "# Run make test before finishing")
    assert res["created"] is True and res["rel"] == "AGENTS.md"
    body = (ws / "AGENTS.md").read_bytes()
    assert b"- Run make test before finishing" in body
    again = pi.remember(str(ws), "Run make test before finishing")
    assert again["duplicate"] is True
    assert (ws / "AGENTS.md").read_bytes() == body
    assert _leftovers(ws) == []


# ── atomicity ──────────────────────────────────────────────────────────────

def test_a_failed_write_leaves_the_original_intact(ws, monkeypatch):
    """The whole point: an interrupted rewrite must not truncate the file the
    system prompt reads every turn."""
    path = ws / "AGENTS.md"
    original = b"# Repo\r\n\r\n- Keep me.\r\n"
    path.write_bytes(original)

    def _boom(*a, **kw):
        raise OSError("no space left on device")
    monkeypatch.setattr(pi.os, "replace", _boom)

    res = pi.remember(str(ws), "A rule that will not land")
    assert res.get("error"), "a failed write must be reported"
    assert path.read_bytes() == original, "the user's instructions file was truncated"
    assert _leftovers(ws) == [], "the failed write left a temp file behind"


def test_the_file_is_replaced_never_truncated_in_place(ws, monkeypatch):
    """Pin the mechanism: the target path is never opened for writing, only
    os.replace()d into place from a temp file."""
    path = ws / "AGENTS.md"
    path.write_bytes(b"# Repo\n\n- Keep me.\n")
    real_open, opened_for_write = open, []

    def _spy_open(file, mode="r", *a, **kw):
        if "w" in mode or "a" in mode or "+" in mode:
            opened_for_write.append(os.fspath(file))
        return real_open(file, mode, *a, **kw)
    monkeypatch.setattr("builtins.open", _spy_open)

    replaced = []
    real_replace = pi.os.replace
    monkeypatch.setattr(pi.os, "replace", lambda src, dst: (replaced.append((src, dst)), real_replace(src, dst))[1])

    pi.remember(str(ws), "Another rule")
    assert str(path) not in opened_for_write, "AGENTS.md itself was opened with a truncating mode"
    assert replaced and os.path.realpath(replaced[0][1]) == os.path.realpath(str(path))
    assert b"- Another rule" in path.read_bytes()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_permission_bits_are_preserved(ws):
    path = ws / "AGENTS.md"
    path.write_bytes(b"# Repo\n\n- One.\n")
    os.chmod(path, 0o640)
    pi.remember(str(ws), "Two")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
