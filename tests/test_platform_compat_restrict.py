"""`restrict_to_owner` on Windows: same ACL as the `icacls` call, no child process.

It used to run `icacls` (and `whoami` the first time) from inside atomic writes
that happen on the event loop; `/api/diag/loop` showed 5-7 s stalls at every
start. The Win32 API sets the same protected, owner-only DACL in milliseconds.
"""
import os
import subprocess

import pytest

from core import platform_compat as pc

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")

#: The tests below patch `subprocess.run` to prove nothing is spawned; the
#: helpers that inspect the result keep the real one.
_REAL_RUN = subprocess.run


def _acl(path) -> list:
    """The ACL `icacls` reports for ``path``, without the path itself."""
    out = _REAL_RUN(["icacls", str(path)], capture_output=True, text=True).stdout
    rows = [ln.strip() for ln in out.splitlines()
            if ln.strip() and not ln.startswith("Successfully processed")]
    return sorted(row.replace(str(path), "").strip() for row in rows)


def _icacls_reference(path, sid) -> None:
    done = _REAL_RUN(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.fixture(autouse=True)
def _fresh_sid_cache(monkeypatch):
    monkeypatch.setattr(pc, "_WINDOWS_USER_SID", None)
    monkeypatch.setattr(pc, "_WINDOWS_SID_UNAVAILABLE", False)


def _no_child_process(monkeypatch):
    def boom(*a, **k):
        raise AssertionError(f"a child process was started: {a!r}")
    monkeypatch.setattr(pc.subprocess, "run", boom)


def test_sid_comes_from_the_process_token(monkeypatch):
    whoami = _REAL_RUN(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True)
    expected = whoami.stdout.strip().split(",")[-1].strip().strip('"')
    _no_child_process(monkeypatch)
    assert pc._current_user_sid() == expected
    assert expected.startswith("S-1-")


def test_native_acl_equals_the_icacls_acl(tmp_path):
    sid = pc._current_user_sid()
    native, reference = tmp_path / "native.txt", tmp_path / "reference.txt"
    for p in (native, reference):
        p.write_text("x")
    assert pc.restrict_to_owner(native) is True
    _icacls_reference(reference, sid)
    assert _acl(native) == _acl(reference)
    assert len(_acl(native)) == 1 and "(F)" in _acl(native)[0]


def test_restricting_spawns_nothing_and_the_owner_keeps_access(tmp_path, monkeypatch):
    target = tmp_path / "secret.json"
    target.write_text("{}")
    _no_child_process(monkeypatch)
    assert pc.restrict_to_owner(target) is True
    assert target.read_text() == "{}"
    target.write_text('{"a": 1}')
    assert target.read_text() == '{"a": 1}'


def test_directory_gets_the_same_treatment(tmp_path, monkeypatch):
    folder = tmp_path / "private"
    folder.mkdir()
    _no_child_process(monkeypatch)
    assert pc.restrict_dir_to_owner(folder) is True
    assert len(_acl(folder)) == 1


def test_a_file_that_does_not_exist_is_reported_not_raised(tmp_path):
    assert pc.restrict_to_owner(tmp_path / "missing.bin") is False


def test_icacls_is_still_the_fallback_when_the_api_is_unavailable(tmp_path, monkeypatch):
    target = tmp_path / "fallback.txt"
    target.write_text("x")
    monkeypatch.setattr(pc, "_win_api", lambda: None)
    calls = []
    real_run = _REAL_RUN

    def spy(args, *a, **k):
        calls.append(list(args))
        return real_run(args, *a, **k)

    monkeypatch.setattr(pc.subprocess, "run", spy)
    assert pc.restrict_to_owner(target) is True
    assert any(c and c[0] == "icacls" for c in calls)
    assert len(_acl(target)) == 1
