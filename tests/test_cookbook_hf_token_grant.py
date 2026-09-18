"""B-024: a Cookbook runner script must never carry the HF token.

Sentinel-based, like the rest of the SEC-1 matrix: a value generated here and
found nowhere else is planted as the token, then every artifact the flow
produces -- the staged runner, the ssh/scp command line, the staging directory
once the supervisor has returned, the log records -- is searched for it.
Nothing in this file is ever a real credential.

The interesting instant is *while the launch runs*, because that is the only
moment the staged files still exist. The fake ``create_subprocess_shell`` below
therefore snapshots the whole staging directory before it answers, which
doubles as the audit's fault-injection point: the same hook can return a
non-zero code or raise, standing in for a refused scp or a node that died
between push and launch.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from starlette.requests import Request

import routes.cookbook_routes as cookbook_routes
from core.platform_compat import IS_WINDOWS
from routes.cookbook_helpers import ModelDownloadRequest, ServeRequest

ROOT = Path(__file__).resolve().parents[1]
COOKBOOK_ROUTES = ROOT / "routes" / "cookbook_routes.py"


# ── harness ────────────────────────────────────────────────────────────────

@pytest.fixture
def sentinel() -> str:
    """A token-shaped value that exists only for this test.

    Generated, never checked in: a fixture holding a literal would itself be a
    secret-shaped string in the repo, and would stop distinguishing "the code
    leaked the token" from "the code echoed a constant".
    """
    return "hf_b024sentinel" + uuid.uuid4().hex


@pytest.fixture
def staging(tmp_path, monkeypatch) -> Path:
    """Point the runner staging directory at a disposable one."""
    directory = tmp_path / "odysseus-tmux"
    monkeypatch.setattr(cookbook_routes, "TMUX_LOG_DIR", directory)
    # Reset the per-process memo so this directory really gets locked down.
    # `raising=False` keeps the fixture usable against a build without the
    # grant machinery, so reverting the fix produces assertion failures that
    # name the leak rather than an AttributeError in setup.
    monkeypatch.setattr(cookbook_routes, "_staging_dirs_restricted", set(), raising=False)
    return directory


@pytest.fixture(autouse=True)
def _admin_and_no_probes(monkeypatch):
    """Admin gate open, remote binary probes answered without touching a node."""
    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)

    async def _available(*args, **kwargs):
        return True

    monkeypatch.setattr(cookbook_routes, "_binary_available", _available)


def _endpoint(path: str, method: str = "POST"):
    router = cookbook_routes.setup_cookbook_routes()
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} route not found")


def _admin_request(path: str) -> Request:
    request = Request(
        {"type": "http", "method": "POST", "path": path, "headers": [], "state": {}}
    )
    request.state.current_user = "admin"
    return request


class _FakeStream:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        return self._data


class _FakeProc:
    def __init__(self, returncode: int):
        self.returncode = returncode
        self.stdout = _FakeStream(b"")
        self.stderr = _FakeStream(b"launch refused by the node")

    async def wait(self) -> int:
        return self.returncode


def _capture_launch(monkeypatch, staging: Path, *, returncode: int = 0, raises=None):
    """Stand in for the scp/ssh launch and photograph the staging dir mid-flight."""
    seen: dict = {"calls": []}

    async def fake_shell(cmd, **kwargs):
        seen["calls"].append(cmd)
        if len(seen["calls"]) == 1:
            # Only the launch itself is photographed. A later call is the
            # supervisor revoking a grant it could not start, and its view of
            # the staging dir is deliberately not what these tests assert on.
            seen["cmd"] = cmd
            files = [p for p in sorted(staging.glob("*")) if p.is_file()]
            seen["staged"] = {p.name: p.read_bytes() for p in files}
            seen["modes"] = {p.name: p.stat().st_mode & 0o777 for p in files}
            seen["owner_only"] = {p.name: _is_owner_only(p) for p in files}
            if raises is not None:
                raise raises
        return _FakeProc(returncode)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    return seen


def _is_owner_only(path: Path) -> bool:
    """No account but the one that wrote it may read this file."""
    if IS_WINDOWS:
        acl = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True
        ).stdout
        # `(I)` marks an inherited ACE: still answering to whatever the parent
        # directory grants (Administrators, SYSTEM, a second local account).
        return "(I)" not in acl and os.environ.get("USERNAME", "?").lower() in acl.lower()
    return path.stat().st_mode & 0o077 == 0


def _carriers(snapshot: dict, sentinel: str) -> list[str]:
    return sorted(name for name, blob in snapshot.items() if sentinel.encode() in blob)


def _leftovers(staging: Path, sentinel: str) -> list[str]:
    if not staging.exists():
        return []
    return sorted(
        p.name
        for p in staging.rglob("*")
        if p.is_file() and sentinel.encode() in p.read_bytes()
    )


# ── download: remote POSIX node ────────────────────────────────────────────

async def test_remote_linux_download_keeps_the_token_out_of_the_runner(
    staging, sentinel, monkeypatch
):
    seen = _capture_launch(monkeypatch, staging)

    body = await _endpoint("/api/model/download")(
        _admin_request("/api/model/download"),
        ModelDownloadRequest(
            repo_id="org/gated-model",
            hf_token=sentinel,
            remote_host="gpu-box",
            env_prefix="source ~/vllm-env/bin/activate",
            platform="linux",
        ),
    )

    assert body["ok"] is True
    runner = next(blob for name, blob in seen["staged"].items() if name.endswith("_run.sh"))
    assert sentinel.encode() not in runner
    # The script names the grant and burns it; it never spells the value.
    assert b'ODYSSEUS_HF_GRANT="$HOME/.' in runner
    assert b'rm -f "$ODYSSEUS_HF_GRANT"' in runner
    assert b"trap " in runner

    # argv is world-readable in the node's process table, so the value goes on
    # stdin behind `umask 077` instead.
    assert sentinel not in seen["cmd"]
    assert "umask 077" in seen["cmd"]
    assert "cat > " in seen["cmd"]

    # Exactly one staged artifact held it, and it was owner-only from birth.
    carriers = _carriers(seen["staged"], sentinel)
    assert len(carriers) == 1 and carriers[0].endswith(".hfenv"), carriers
    assert seen["owner_only"][carriers[0]] is True
    if not IS_WINDOWS:
        assert seen["modes"][carriers[0]] == 0o600

    # Nothing the supervisor staged for the node outlives the push.
    assert _leftovers(staging, sentinel) == []
    assert list(staging.glob("*.hfenv")) == []
    assert list(staging.glob("*_run.sh")) == []


async def test_remote_linux_download_stages_the_runner_owner_only(
    staging, sentinel, monkeypatch
):
    seen = _capture_launch(monkeypatch, staging)
    await _endpoint("/api/model/download")(
        _admin_request("/api/model/download"),
        ModelDownloadRequest(
            repo_id="org/gated-model",
            hf_token=sentinel,
            remote_host="gpu-box",
            env_prefix="source ~/vllm-env/bin/activate",
            platform="linux",
        ),
    )
    runner = next(name for name in seen["staged"] if name.endswith("_run.sh"))
    if not IS_WINDOWS:
        # 0755 let every local account read a script staged in a shared /tmp.
        assert seen["modes"][runner] == 0o700
    assert seen["owner_only"][runner] is True


# ── download: remote Windows node ──────────────────────────────────────────

async def test_remote_windows_download_keeps_the_token_out_of_the_ps1(
    staging, sentinel, monkeypatch
):
    seen = _capture_launch(monkeypatch, staging)

    body = await _endpoint("/api/model/download")(
        _admin_request("/api/model/download"),
        ModelDownloadRequest(
            repo_id="org/gated-model",
            hf_token=sentinel,
            remote_host="winbox",
            env_prefix="source ~/vllm-env/bin/activate",
            platform="windows",
        ),
    )

    assert body["ok"] is True
    runner = next(blob for name, blob in seen["staged"].items() if name.endswith("_run.ps1"))
    assert sentinel.encode() not in runner
    assert b"$odysseusHfGrant" in runner
    assert b"Remove-Item -Force -LiteralPath $odysseusHfGrant" in runner
    assert sentinel not in seen["cmd"]

    carriers = _carriers(seen["staged"], sentinel)
    assert len(carriers) == 1 and carriers[0].endswith(".hfenv"), carriers
    assert seen["owner_only"][carriers[0]] is True
    assert _leftovers(staging, sentinel) == []


# ── download: this machine ─────────────────────────────────────────────────

async def test_local_posix_download_reads_a_grant_the_wrapper_does_not_contain(
    staging, sentinel, monkeypatch
):
    """The tmux wrapper survives the request, so it is the worst place for a value."""
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", False)
    seen = _capture_launch(monkeypatch, staging)

    body = await _endpoint("/api/model/download")(
        _admin_request("/api/model/download"),
        ModelDownloadRequest(repo_id="org/gated-model", hf_token=sentinel),
    )

    assert body["ok"] is True
    wrapper = next(blob for name, blob in seen["staged"].items() if name.endswith(".sh"))
    assert sentinel.encode() not in wrapper
    assert b"ODYSSEUS_HF_GRANT=" in wrapper

    carriers = _carriers(seen["staged"], sentinel)
    assert len(carriers) == 1 and carriers[0].endswith(".hfenv"), carriers
    assert seen["owner_only"][carriers[0]] is True

    # A local grant is consumed by the runner that is now starting, so unlike a
    # staged copy it must still be there when the request returns.
    assert [p.name for p in staging.glob("*.hfenv")] == carriers


async def test_local_windows_download_passes_the_token_by_inheritance(
    staging, sentinel, monkeypatch
):
    """Faustus starts this process itself, so nothing has to be written down."""
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", True)
    monkeypatch.setattr(cookbook_routes, "find_bash", lambda: "bash")
    captured = _stub_popen(monkeypatch)

    body = await _endpoint("/api/model/download")(
        _admin_request("/api/model/download"),
        ModelDownloadRequest(repo_id="org/gated-model", hf_token=sentinel),
    )

    assert body["ok"] is True
    assert captured["env"]["HF_TOKEN"] == sentinel
    assert sentinel not in " ".join(str(a) for a in captured["argv"])
    # No file was needed, so none was written -- not even a 0600 one.
    assert _leftovers(staging, sentinel) == []
    assert list(staging.glob("*.hfenv")) == []


class _ShimSubprocess:
    """`subprocess` with only Popen swapped out.

    Patching the stdlib attribute itself would also hijack `subprocess.run`,
    which `restrict_to_owner` uses for icacls on Windows -- the staging
    lockdown would then break before the code under test ever ran.
    """

    def __init__(self, real, popen):
        self._real = real
        self.Popen = popen

    def __getattr__(self, name):
        return getattr(self._real, name)


def _stub_popen(monkeypatch) -> dict:
    captured: dict = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env") or {}
            self.pid = 4242

    monkeypatch.setattr(
        cookbook_routes, "subprocess", _ShimSubprocess(subprocess, _FakePopen)
    )
    return captured


# ── serve ──────────────────────────────────────────────────────────────────

_PIP_SERVE = dict(repo_id="huggingface-hub", cmd="python -m pip install huggingface-hub")


async def test_remote_linux_serve_keeps_the_token_out_of_the_runner(
    staging, sentinel, monkeypatch
):
    seen = _capture_launch(monkeypatch, staging)

    body = await _endpoint("/api/model/serve")(
        _admin_request("/api/model/serve"),
        ServeRequest(
            hf_token=sentinel, remote_host="gpu-box", platform="linux", **_PIP_SERVE
        ),
    )

    assert body["ok"] is True
    runner = next(blob for name, blob in seen["staged"].items() if name.endswith("_run.sh"))
    assert sentinel.encode() not in runner
    assert b'ODYSSEUS_HF_GRANT="$HOME/.' in runner
    assert sentinel not in seen["cmd"]
    assert "umask 077" in seen["cmd"]

    carriers = _carriers(seen["staged"], sentinel)
    assert len(carriers) == 1 and carriers[0].endswith(".hfenv"), carriers
    assert _leftovers(staging, sentinel) == []


async def test_remote_windows_serve_keeps_the_token_out_of_the_ps1(
    staging, sentinel, monkeypatch
):
    seen = _capture_launch(monkeypatch, staging)

    body = await _endpoint("/api/model/serve")(
        _admin_request("/api/model/serve"),
        ServeRequest(
            hf_token=sentinel, remote_host="winbox", platform="windows", **_PIP_SERVE
        ),
    )

    assert body["ok"] is True
    runner = next(blob for name, blob in seen["staged"].items() if name.endswith("_run.ps1"))
    assert sentinel.encode() not in runner
    assert b"$odysseusHfGrant" in runner
    assert sentinel not in seen["cmd"]
    assert _leftovers(staging, sentinel) == []


async def test_local_windows_serve_passes_the_token_by_inheritance(
    staging, sentinel, monkeypatch
):
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", True)
    monkeypatch.setattr(cookbook_routes, "find_bash", lambda: "bash")
    captured = _stub_popen(monkeypatch)

    body = await _endpoint("/api/model/serve")(
        _admin_request("/api/model/serve"),
        ServeRequest(hf_token=sentinel, **_PIP_SERVE),
    )

    assert body["ok"] is True
    assert captured["env"]["HF_TOKEN"] == sentinel
    assert _leftovers(staging, sentinel) == []


# ── fault injection ────────────────────────────────────────────────────────

async def test_a_refused_launch_leaves_no_secret_on_this_disk(
    staging, sentinel, monkeypatch
):
    """Non-zero launch: the node never ran anything, so nothing self-cleans."""
    seen = _capture_launch(monkeypatch, staging, returncode=255)

    body = await _endpoint("/api/model/download")(
        _admin_request("/api/model/download"),
        ModelDownloadRequest(
            repo_id="org/gated-model",
            hf_token=sentinel,
            remote_host="gpu-box",
            env_prefix="source ~/vllm-env/bin/activate",
            platform="linux",
        ),
    )

    assert body["ok"] is False
    assert sentinel not in str(body)
    assert _carriers(seen["staged"], sentinel)  # it did exist mid-flight
    assert _leftovers(staging, sentinel) == []
    assert list(staging.glob("*.hfenv")) == []

    # The grant is the first link of the chain, so it may already have landed
    # on the node with nothing left to burn it. The supervisor revokes it.
    revoke = seen["calls"][-1]
    assert revoke.startswith("ssh ")
    assert 'rm -f "$HOME/.' in revoke and ".hfenv" in revoke
    assert sentinel not in revoke


async def test_an_scp_that_dies_mid_push_leaves_no_secret_on_this_disk(
    staging, sentinel, monkeypatch
):
    """The push itself blows up: the `finally` is the only cleanup left."""
    boom = RuntimeError("ssh: connect to host gpu-box port 22: No route to host")
    _capture_launch(monkeypatch, staging, raises=boom)

    with pytest.raises(RuntimeError):
        await _endpoint("/api/model/download")(
            _admin_request("/api/model/download"),
            ModelDownloadRequest(
                repo_id="org/gated-model",
                hf_token=sentinel,
                remote_host="gpu-box",
                env_prefix="source ~/vllm-env/bin/activate",
                platform="linux",
            ),
        )

    assert _leftovers(staging, sentinel) == []


async def test_a_failed_local_launch_burns_the_grant_nobody_will_read(
    staging, sentinel, monkeypatch
):
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", False)
    _capture_launch(monkeypatch, staging, returncode=1)

    body = await _endpoint("/api/model/download")(
        _admin_request("/api/model/download"),
        ModelDownloadRequest(repo_id="org/gated-model", hf_token=sentinel),
    )

    assert body["ok"] is False
    # The tmux session never started, so the wrapper's own `rm` never runs.
    assert list(staging.glob("*.hfenv")) == []
    assert _leftovers(staging, sentinel) == []


async def test_a_grant_no_runner_claimed_is_swept_on_the_next_launch(
    staging, sentinel, monkeypatch
):
    """SIGKILL runs neither the runner's `rm` nor its `trap`."""
    staging.mkdir(parents=True, exist_ok=True)
    orphan = staging / "cookbook-deadbeef.hfenv"
    orphan.write_text(f"HF_TOKEN='{sentinel}'\n", encoding="utf-8")
    os.utime(orphan, (0, 0))

    _capture_launch(monkeypatch, staging)
    await _endpoint("/api/model/download")(
        _admin_request("/api/model/download"),
        ModelDownloadRequest(
            repo_id="org/model",
            remote_host="gpu-box",
            env_prefix="source ~/vllm-env/bin/activate",
            platform="linux",
        ),
    )

    assert not orphan.exists()


# ── logs ───────────────────────────────────────────────────────────────────

async def test_the_launch_logs_status_and_never_the_value(
    staging, sentinel, monkeypatch, caplog
):
    """caplog sees raw records, before any handler redaction -- deliberately.

    Leaning on `SecretRedactingFilter` here would make the test pass for the
    wrong reason: the route must not put the value into a record at all.
    """
    _capture_launch(monkeypatch, staging)

    with caplog.at_level(logging.DEBUG):
        await _endpoint("/api/model/download")(
            _admin_request("/api/model/download"),
            ModelDownloadRequest(
                repo_id="org/gated-model",
                hf_token=sentinel,
                remote_host="gpu-box",
                env_prefix="source ~/vllm-env/bin/activate",
                platform="linux",
            ),
        )

    for record in caplog.records:
        message = record.getMessage()
        assert sentinel not in message
        assert sentinel not in str(record.args or "")
        # Not the length or the prefix either: a prefix names which credential
        # leaked, a length narrows a brute force.
        assert sentinel[:8] not in message
    assert "HF token: applied" in caplog.text


async def test_the_launch_logs_not_set_when_there_is_no_token(
    staging, monkeypatch, caplog
):
    _capture_launch(monkeypatch, staging)

    with caplog.at_level(logging.INFO):
        await _endpoint("/api/model/download")(
            _admin_request("/api/model/download"),
            ModelDownloadRequest(
                repo_id="org/model",
                remote_host="gpu-box",
                env_prefix="source ~/vllm-env/bin/activate",
                platform="linux",
            ),
        )

    assert "HF token: not-set" in caplog.text


# ── the generated shell, actually run ──────────────────────────────────────

_BASH = shutil.which("bash")
_posix_shell_only = pytest.mark.skipif(
    os.name == "nt" or not _BASH,
    reason="needs a POSIX shell whose file modes are the ones the snippet checks",
)


@_posix_shell_only
def test_the_grant_snippet_exports_the_token_and_deletes_the_file(tmp_path, sentinel):
    grant = tmp_path / "session.hfenv"
    grant.write_text(f"HF_TOKEN='{sentinel}'\n", encoding="utf-8")
    grant.chmod(0o600)
    script = "\n".join(
        cookbook_routes._bash_hf_grant_lines(shlex.quote(str(grant)))
        + ['printf "TOKEN=%s\\n" "$HF_TOKEN"']
    )

    out = subprocess.run([_BASH, "-c", script], capture_output=True, text=True, timeout=30)

    assert out.returncode == 0, out.stderr
    assert f"TOKEN={sentinel}" in out.stdout
    assert not grant.exists()


@_posix_shell_only
def test_the_grant_snippet_refuses_a_grant_others_could_read(tmp_path, sentinel):
    grant = tmp_path / "session.hfenv"
    grant.write_text(f"HF_TOKEN='{sentinel}'\n", encoding="utf-8")
    grant.chmod(0o644)
    script = "\n".join(
        cookbook_routes._bash_hf_grant_lines(shlex.quote(str(grant)))
        + ['printf "TOKEN=%s\\n" "$HF_TOKEN"']
    )

    out = subprocess.run([_BASH, "-c", script], capture_output=True, text=True, timeout=30)

    assert "TOKEN=" in out.stdout and sentinel not in out.stdout
    assert "refused: mode 644" in out.stdout
    # Refused or not, a credential that reached a readable file is destroyed.
    assert not grant.exists()


@_posix_shell_only
def test_the_trap_burns_a_grant_the_runner_dies_before_reading(tmp_path, sentinel):
    grant = tmp_path / "session.hfenv"
    grant.write_text(f"HF_TOKEN='{sentinel}'\n", encoding="utf-8")
    grant.chmod(0o600)
    # A preflight that exits between the trap and the read -- the case a
    # self-deleting last line of the script cannot cover.
    lines = cookbook_routes._bash_hf_grant_lines(shlex.quote(str(grant)))
    script = "\n".join(lines[:2] + ["exit 127"])

    out = subprocess.run([_BASH, "-c", script], capture_output=True, text=True, timeout=30)

    assert out.returncode == 127
    assert not grant.exists()


# ── the shape of the source itself ─────────────────────────────────────────

def test_no_cookbook_route_interpolates_the_token_into_a_script_line():
    """The regression guard: these two spellings are how B-024 happened."""
    source = COOKBOOK_ROUTES.read_text(encoding="utf-8")
    assert "export HF_TOKEN='{" not in source
    assert "$env:HF_TOKEN = '{" not in source
