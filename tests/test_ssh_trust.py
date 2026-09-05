"""B-025: SSH host trust.

Covers the five cases the audit asks for -- new host, known host, changed key,
alternate hostname/port spellings and a simulated MITM -- plus the property that
made the bug possible in the first place: every remote path spelling its own ssh
flags. No real key material appears here; the blobs are random bytes that are
only ever base64-decoded and hashed.
"""

import base64
import os
import subprocess
from pathlib import Path

import pytest

from src import ssh_trust

REPO_ROOT = Path(__file__).resolve().parents[1]

# The call sites B-025 named. Each used to hard-code StrictHostKeyChecking=no.
WIRED_SOURCES = (
    "routes/cookbook_routes.py",
    "routes/shell_routes.py",
    "src/cookbook_serve_lifecycle.py",
    "src/tools/cookbook.py",
)


def _throwaway_key() -> str:
    """A syntactically valid, meaningless host-key blob, fresh every call."""
    return base64.b64encode(os.urandom(48)).decode("ascii")


class _Keyscan:
    """Stand-in for the node: the test decides what answers the scan."""

    def __init__(self):
        self.keys: list[str] = []
        self.calls: list[list[str]] = []

    def serve(self, *keys: str) -> None:
        self.keys = list(keys)

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        # A deliberately wrong host field: ssh-keyscan echoes the name it was
        # asked, and the store must key on OUR canonical pattern regardless.
        body = "".join(f"scanned-HOST ssh-ed25519 {k}\n" for k in self.keys)
        return subprocess.CompletedProcess(argv, 0, body, "")


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "ssh" / "known_hosts"
    monkeypatch.setenv("FAUSTUS_SSH_KNOWN_HOSTS", str(path))
    return path


@pytest.fixture
def keyscan(monkeypatch):
    scanner = _Keyscan()
    monkeypatch.setattr(ssh_trust.subprocess, "run", scanner.run)
    return scanner


class TestFlags:
    def test_default_argv_is_strict_and_uses_the_private_store(self, store):
        argv = ssh_trust.ssh_argv("user@gpu-box", None, "true", connect_timeout=6)
        assert argv[0] == "ssh"
        assert "StrictHostKeyChecking=yes" in argv
        assert f"UserKnownHostsFile={store}" in argv
        assert "StrictHostKeyChecking=no" not in argv
        assert argv[-2:] == ["user@gpu-box", "true"]

    def test_accept_new_is_reachable_only_from_an_attended_call(self, store):
        assert "StrictHostKeyChecking=accept-new" in ssh_trust.ssh_argv(
            "gpu-box", None, attended=True
        )
        assert "StrictHostKeyChecking=accept-new" not in ssh_trust.ssh_argv(
            "gpu-box", None
        )

    def test_non_default_port_is_a_separate_argv_element(self, store):
        assert ssh_trust.ssh_argv("gpu-box", "2222")[-3:] == ["-p", "2222", "gpu-box"]
        assert "-p" not in ssh_trust.ssh_argv("gpu-box", "22")
        assert "-p" not in ssh_trust.ssh_argv("gpu-box", "")

    @pytest.mark.parametrize(
        "bad", ["-oProxyCommand=touch /tmp/pwn", "", "   ", "host name", "a;b"]
    )
    def test_option_injecting_remote_is_refused(self, store, bad):
        with pytest.raises(ValueError):
            ssh_trust.ssh_argv(bad, None)

    @pytest.mark.parametrize("bad", ["0", "70000", "-1", "8a", "22 22"])
    def test_bad_port_is_refused(self, store, bad):
        with pytest.raises(ValueError):
            ssh_trust.ssh_argv("gpu-box", bad)

    def test_scp_push_carries_the_same_trust_flags(self, store, tmp_path):
        runner = tmp_path / "run.sh"
        cmd = ssh_trust.scp_command(runner, "user@gpu-box", ".run.sh", ssh_port="2222")
        assert "StrictHostKeyChecking=yes" in cmd
        assert "StrictHostKeyChecking=no" not in cmd
        assert "UserKnownHostsFile=" in cmd
        assert "-P 2222" in cmd
        assert cmd.endswith("user@gpu-box:.run.sh")

    def test_option_fragment_matches_the_argv_builder(self, store):
        fragment = ssh_trust.option_flags(connect_timeout=5)
        for token in ssh_trust.ssh_argv("gpu-box", None, connect_timeout=5)[1:-1]:
            assert token in fragment

    def test_shell_rendering_quotes_the_remote_command(self, store):
        cmd = ssh_trust.ssh_command("gpu-box", "tmux kill-session -t serve-1")
        assert cmd.endswith("'tmux kill-session -t serve-1'")


class TestPairing:
    def test_new_host_is_unpaired_and_writes_nothing(self, store, keyscan):
        keyscan.serve(_throwaway_key())

        state = ssh_trust.pairing_state("gpu-box")

        assert state["state"] == "unpaired"
        assert state["stored"] == []
        assert not store.exists()

    def test_confirmed_fingerprint_makes_the_host_known(self, store, keyscan):
        key = _throwaway_key()
        keyscan.serve(key)

        result = ssh_trust.pair_host(
            "user@gpu-box", fingerprint=ssh_trust.fingerprint_for_key(key)
        )

        assert result["added"] is True
        assert ssh_trust.is_paired("gpu-box")
        assert ssh_trust.pairing_state("gpu-box")["state"] == "paired"
        # Keyed on our canonical pattern, not on what keyscan echoed back.
        assert store.read_text(encoding="utf-8").split()[0] == "gpu-box"

    def test_pairing_twice_is_idempotent(self, store, keyscan):
        key = _throwaway_key()
        keyscan.serve(key)
        fp = ssh_trust.fingerprint_for_key(key)
        ssh_trust.pair_host("gpu-box", fingerprint=fp)

        again = ssh_trust.pair_host("gpu-box", fingerprint=fp)

        assert again["added"] is False
        assert len(store.read_text(encoding="utf-8").strip().splitlines()) == 1

    def test_a_bare_fingerprint_is_accepted(self, store, keyscan):
        key = _throwaway_key()
        keyscan.serve(key)
        bare = ssh_trust.fingerprint_for_key(key).split(":", 1)[1]

        assert ssh_trust.pair_host("gpu-box", fingerprint=bare)["added"] is True

    def test_non_default_port_is_stored_in_openssh_bracket_form(self, store, keyscan):
        key = _throwaway_key()
        keyscan.serve(key)

        ssh_trust.pair_host(
            "gpu-box", "2222", fingerprint=ssh_trust.fingerprint_for_key(key)
        )

        assert store.read_text(encoding="utf-8").split()[0] == "[gpu-box]:2222"
        assert keyscan.calls[0][-3:] == ["-p", "2222", "gpu-box"]

    def test_scan_that_returns_nothing_is_a_failure_not_a_pairing(self, store, keyscan):
        keyscan.serve()

        with pytest.raises(ssh_trust.HostScanFailed):
            ssh_trust.scan_host_keys("gpu-box")
        assert not store.exists()


class TestKeyChange:
    def test_changed_key_is_reported_and_never_repaired(self, store, keyscan):
        original = _throwaway_key()
        keyscan.serve(original)
        ssh_trust.pair_host(
            "gpu-box", fingerprint=ssh_trust.fingerprint_for_key(original)
        )
        before = store.read_bytes()

        swapped = _throwaway_key()
        keyscan.serve(swapped)

        assert ssh_trust.pairing_state("gpu-box")["state"] == "changed"
        with pytest.raises(ssh_trust.HostKeyChanged):
            ssh_trust.pair_host(
                "gpu-box", fingerprint=ssh_trust.fingerprint_for_key(swapped)
            )
        assert store.read_bytes() == before

    def test_simulated_mitm_fails_the_fingerprint_check(self, store, keyscan):
        # The operator read the real node's fingerprint off its own console;
        # something else is answering for that address.
        real = _throwaway_key()
        impostor = _throwaway_key()
        keyscan.serve(impostor)

        with pytest.raises(ssh_trust.HostKeyMismatch):
            ssh_trust.pair_host(
                "gpu-box", fingerprint=ssh_trust.fingerprint_for_key(real)
            )
        assert not store.exists()
        assert not ssh_trust.is_paired("gpu-box")

    def test_revoking_is_the_only_way_back_to_pairable(self, store, keyscan):
        original = _throwaway_key()
        keyscan.serve(original)
        ssh_trust.pair_host(
            "gpu-box", fingerprint=ssh_trust.fingerprint_for_key(original)
        )
        replacement = _throwaway_key()
        keyscan.serve(replacement)

        assert ssh_trust.forget_host("gpu-box") == 1
        assert not ssh_trust.is_paired("gpu-box")
        assert ssh_trust.pair_host(
            "gpu-box", fingerprint=ssh_trust.fingerprint_for_key(replacement)
        )["added"] is True

    def test_revoking_leaves_other_targets_alone(self, store, keyscan):
        for host in ("gpu-box", "other-box"):
            key = _throwaway_key()
            keyscan.serve(key)
            ssh_trust.pair_host(host, fingerprint=ssh_trust.fingerprint_for_key(key))

        ssh_trust.forget_host("gpu-box")

        assert not ssh_trust.is_paired("gpu-box")
        assert ssh_trust.is_paired("other-box")


class TestAlternateSpellings:
    @pytest.fixture(autouse=True)
    def paired_gpu_box(self, store, keyscan):
        key = _throwaway_key()
        keyscan.serve(key)
        ssh_trust.pair_host("gpu-box", fingerprint=ssh_trust.fingerprint_for_key(key))

    def test_login_and_case_are_not_part_of_the_identity(self):
        assert ssh_trust.is_paired("gpu-box")
        assert ssh_trust.is_paired("root@gpu-box")
        assert ssh_trust.is_paired("GPU-BOX")

    def test_the_same_box_under_another_name_is_a_different_target(self):
        assert not ssh_trust.is_paired("10.0.0.5")
        assert ssh_trust.pairing_state("10.0.0.5", offered=[])["state"] == "unpaired"

    def test_the_same_name_on_another_port_is_a_different_target(self):
        assert not ssh_trust.is_paired("gpu-box", "2222")
        assert ssh_trust.pairing_state("gpu-box", "2222", offered=[])["state"] == "unpaired"

    def test_a_suffix_of_a_paired_name_does_not_match(self):
        assert not ssh_trust.is_paired("gpu-box.evil.example")


class TestCallSitesAreCentralized:
    @pytest.mark.parametrize("rel", WIRED_SOURCES)
    def test_no_call_site_still_disables_host_checking(self, rel):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "StrictHostKeyChecking=no" not in text

    @pytest.mark.parametrize("rel", WIRED_SOURCES)
    def test_every_call_site_builds_its_flags_here(self, rel):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "ssh_trust" in text

    def test_shell_probe_argv_comes_from_ssh_trust(self, store):
        from routes.shell_routes import _ssh_base_argv

        argv = _ssh_base_argv("gpu-box", "2222")

        assert "StrictHostKeyChecking=yes" in argv
        assert f"UserKnownHostsFile={store}" in argv
        assert "ConnectTimeout=6" in argv
        assert argv[-3:] == ["-p", "2222", "gpu-box"]

    @pytest.mark.asyncio
    async def test_cookbook_remote_probe_argv_comes_from_ssh_trust(
        self, store, monkeypatch
    ):
        import routes.cookbook_routes as cookbook_routes

        captured: dict = {}

        class _Proc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        async def _fake_exec(*argv, **kwargs):
            captured["argv"] = list(argv)
            return _Proc()

        monkeypatch.setattr(
            cookbook_routes.asyncio, "create_subprocess_exec", _fake_exec
        )

        assert await cookbook_routes._remote_binary_available(
            "user@gpu-box", "2222", "tmux"
        )
        assert "StrictHostKeyChecking=yes" in captured["argv"]
        assert f"UserKnownHostsFile={store}" in captured["argv"]
        assert "user@gpu-box" in captured["argv"]


    @pytest.mark.asyncio
    async def test_serve_lifecycle_stop_is_host_checked(self, store, monkeypatch):
        import httpx

        from src import cookbook_serve_lifecycle as lifecycle

        sent: dict = {}

        class _Response:
            status_code = 200
            content = b"{}"

            def json(self):
                return {"exit_code": 0}

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, **kw):
                sent["command"] = (json or {}).get("command", "")
                return _Response()

        monkeypatch.setattr(httpx, "AsyncClient", _Client)

        assert await lifecycle._stop_serve("serve-abc123", "user@gpu-box", "2222")

        assert "StrictHostKeyChecking=yes" in sent["command"]
        assert "StrictHostKeyChecking=no" not in sent["command"]
        assert f"UserKnownHostsFile={store}" in sent["command"]
        assert "-p 2222" in sent["command"]
        assert "tmux kill-session -t serve-abc123" in sent["command"]

    @pytest.mark.asyncio
    async def test_serve_lifecycle_refuses_an_unparseable_host(
        self, store, monkeypatch
    ):
        import httpx

        from src import cookbook_serve_lifecycle as lifecycle

        posted: list = []

        class _Response:
            status_code = 200
            content = b"{}"

            def json(self):
                return {"exit_code": 0}

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, **kw):
                posted.append((json or {}).get("command", ""))
                return _Response()

        monkeypatch.setattr(httpx, "AsyncClient", _Client)

        # Quoting an option-shaped host into a command line is not a refusal:
        # the run has to stop before anything is sent.
        assert await lifecycle._stop_serve("serve-abc123", "-oProxyCommand=x") is False
        assert posted == []
