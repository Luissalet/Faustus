"""src/sandbox_provider.py — the SandboxProvider port in isolation, no
agent tool involved. `tests/acceptance/test_a16_*` and `test_a17_*` exercise
it through the real bash tool; these pin the port's own contract: what each
method returns for each Docker CLI answer, and the registry helpers that
tell "never created" apart from "created, then disappeared"."""
from __future__ import annotations

import subprocess

import pytest

from src import sandbox_provider as sp


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    reg = tmp_path / "sandbox_sessions.json"
    monkeypatch.setattr(sp, "_registry_path", lambda: str(reg))


def _cp(args, code, out=b"", err=b""):
    return subprocess.CompletedProcess(args, code, out, err)


class FakeClient:
    def __init__(self, *, daemon_ok=True, image_ok=True):
        self.daemon_ok = daemon_ok
        self.image_ok = image_ok
        self.running = False

    def __call__(self, args, timeout=30):
        head = args[0] if args else ""
        if head == "version":
            return _cp(args, 0 if self.daemon_ok else 1, b"", b"" if self.daemon_ok
                       else b"cannot connect")
        if head == "image":
            return _cp(args, 0 if self.image_ok else 1, b"id", b"" if self.image_ok
                       else b"no such image")
        if head == "rm":
            return _cp(args, 0)
        if head == "run":
            self.running = True
            return _cp(args, 0, b"cid\n")
        if head == "inspect":
            return _cp(args, 0, b"true\n") if self.running else \
                   _cp(args, 1, b"", b"Error: No such container")
        if head == "exec":
            if not self.running:
                return _cp(args, 126, b"", b"Error: No such container")
            return _cp(args, 0, b"ok\n")
        return _cp(args, 0)


def _provider(monkeypatch, client):
    monkeypatch.setattr(sp.DockerSandboxProvider, "_run_docker",
                        lambda self, a, timeout=30: client(a, timeout))
    return sp.get_provider(image="faustus/sandbox:test", workspace="/tmp/ws")


def test_probe_reports_daemon_absent(monkeypatch):
    provider = _provider(monkeypatch, FakeClient(daemon_ok=False))
    avail = provider.probe()
    assert avail.available is False
    assert avail.kind == "daemon"
    assert "connect" in avail.reason.lower()


def test_probe_reports_image_missing(monkeypatch):
    provider = _provider(monkeypatch, FakeClient(image_ok=False))
    avail = provider.probe()
    assert avail.available is False
    assert avail.kind == "image"


def test_probe_reports_unsupported_platform(monkeypatch):
    provider = _provider(monkeypatch, FakeClient())
    monkeypatch.setattr("core.platform_compat.IS_WINDOWS", True, raising=False)
    avail = provider.probe()
    assert avail.available is False
    assert avail.kind == "platform"


def test_status_is_missing_before_create_and_exists_after(monkeypatch):
    client = FakeClient()
    provider = _provider(monkeypatch, client)
    assert provider.status("s1") == "missing"
    assert sp.session_seen("s1") is False
    created = provider.create("s1")
    assert created["created"] is True
    assert sp.session_seen("s1") is True
    assert provider.status("s1") == "exists"


def test_status_is_unknown_when_the_daemon_itself_cannot_be_reached(monkeypatch):
    client = FakeClient()
    provider = _provider(monkeypatch, client)
    provider.create("s1")

    def _daemon_gone(args, timeout=30):
        if args[0] == "inspect":
            return _cp(args, 1, b"", b"error during connect: daemon unreachable")
        return client(args, timeout)
    monkeypatch.setattr(sp.DockerSandboxProvider, "_run_docker",
                        lambda self, a, timeout=30: _daemon_gone(a, timeout))
    assert provider.status("s1") == "unknown"


def test_exec_records_touches_and_recreate_reports_them_as_lost(monkeypatch):
    client = FakeClient()
    provider = _provider(monkeypatch, client)
    provider.create("s1")
    result = provider.exec("s1", ["echo", "hi"], touches=["a.txt", "b.txt"])
    assert result["executed"] is True
    assert result["exit_code"] == 0
    assert sp.known_files("s1") == ["a.txt", "b.txt"]

    client.running = False  # removed externally
    recreated = provider.recreate("s1", "recreate_empty")
    assert recreated["recreated"] is True
    assert recreated["lost_files"] == ["a.txt", "b.txt"]
    assert "did not survive" in recreated["message"].lower()
    assert "a.txt" in recreated["message"]
    # The ledger reflects the NEW, empty session, not the old files.
    assert sp.known_files("s1") == []


def test_recreate_with_fail_policy_recreates_nothing(monkeypatch):
    client = FakeClient()
    provider = _provider(monkeypatch, client)
    provider.create("s1")
    provider.exec("s1", ["echo", "hi"], touches=["a.txt"])
    client.running = False

    result = provider.recreate("s1", "fail")
    assert result["recreated"] is False
    assert result["policy"] == "fail"
    assert result["lost_files"] == ["a.txt"]
    assert "not recreated" in result["message"].lower() or "not run" in result["message"].lower() \
        or "did not survive" in result["message"].lower()
    # A previously-created session is unaffected by a failed recreate attempt
    # -- still reported missing, not silently marked exists.
    assert provider.status("s1") == "missing"


def test_forget_session_drops_the_ledger_entry(monkeypatch):
    provider = _provider(monkeypatch, FakeClient())
    provider.create("s1")
    provider.exec("s1", ["echo", "hi"], touches=["a.txt"])
    assert sp.session_seen("s1") is True
    sp.forget_session("s1")
    assert sp.session_seen("s1") is False
    assert sp.known_files("s1") == []
