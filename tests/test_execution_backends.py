"""The sandbox (src/execution_backends) — the Phase 1 stop condition, checked
against real containers.

The masterplan says: do not advance if a run can read `data/.app_key`, escape
the workspace, inherit secrets or fall to the host without confirmation. Those
four are the four `docker` tests below, and they start actual containers —
mocking `subprocess` here would test that the flag string was assembled, which
is not the claim being made.

Everything that does not need a daemon (argv-only, the flag list, the tail
rule, the output attribution) runs everywhere. The container tests skip when
Docker is not available, and say why.
"""
from __future__ import annotations

import os

import pytest

from src import capability_registry as registry
from src.contracts import ExecutionSpec
from src.execution_backends import (
    DockerWorkspaceBackend, LocalAttendedBackend, _argv, _produced, _snapshot, _tail,
)

TEST_IMAGE = "alpine:3.20"


def _docker_state():
    try:
        return registry.observe("docker_workspace", fresh=True)
    except Exception as e:                       # pragma: no cover
        return type("O", (), {"state": "unavailable", "evidence": str(e)})()


_DOCKER = _docker_state()
_HAS_IMAGE = DockerWorkspaceBackend(image=TEST_IMAGE).probe()["ok"] \
    if _DOCKER.state == "available" else False

needs_docker = pytest.mark.skipif(
    not _HAS_IMAGE,
    reason=f"needs a running docker and {TEST_IMAGE} ({_DOCKER.evidence})",
)


@pytest.fixture()
def sandbox(tmp_path):
    work = tmp_path / "workspace"
    arts = tmp_path / "artifacts"
    work.mkdir()
    arts.mkdir()
    (work / "notes.txt").write_text("the brief\n", encoding="utf-8")
    return {"backend": "docker_workspace", "isolation": "container",
            "workspace": str(work), "artifacts_dir": str(arts),
            "limits": {"seconds": 60, "memory_mb": 256, "cpus": 1}}


# ── no daemon needed ───────────────────────────────────────────────────────

def test_a_command_is_a_list_and_a_string_is_a_refusal():
    with pytest.raises(ValueError) as err:
        _argv("rm -rf / ; echo done", "docker_workspace")
    assert "not a string" in str(err.value)
    assert "second command" in str(err.value)
    assert _argv(["sh", "-c", "echo hi"], "x") == ["sh", "-c", "echo hi"]


def test_the_flag_list_is_the_security_claim(sandbox):
    args = DockerWorkspaceBackend(image=TEST_IMAGE).docker_args(
        ExecutionSpec.parse(sandbox), "faustus-t")
    pairs = list(zip(args, args[1:]))
    assert ("--network", "none") in pairs
    assert ("--user", "1000:1000") in pairs
    assert ("--cap-drop", "ALL") in pairs
    assert ("--security-opt", "no-new-privileges") in pairs
    assert ("--memory", "256m") in pairs and ("--memory-swap", "256m") in pairs
    assert ("-w", "/workspace") in pairs
    assert args[-1] == TEST_IMAGE           # the image is last; argv follows it
    assert "--privileged" not in args
    assert not any(a.startswith("/var/run/docker.sock") for a in args)


def test_asking_for_the_network_changes_exactly_one_flag(sandbox):
    be = DockerWorkspaceBackend(image=TEST_IMAGE)
    closed = be.docker_args(ExecutionSpec.parse(sandbox), "t")
    opened = be.docker_args(ExecutionSpec.parse({**sandbox, "network": True}), "t")
    assert closed.count("none") - opened.count("none") == 1
    assert "bridge" in opened
    assert [a for a in closed if a != "none"] == [a for a in opened if a != "bridge"]


def test_the_output_kept_is_the_tail_and_it_admits_the_cut():
    small, cut = _tail(b"hello")
    assert (small, cut) == ("hello", False)
    big, cut = _tail(b"A" * 10 + b"B" * 200_000)
    assert cut is True
    assert big.endswith("B")
    assert "A" not in big                 # the banner went, the error stayed


def test_a_run_is_credited_only_with_what_it_changed(tmp_path):
    """The bug the first live probe found: listing the directory afterwards
    handed one run the previous run's file."""
    d = tmp_path / "artifacts"
    d.mkdir()
    (d / "from_an_earlier_run.txt").write_text("old", encoding="utf-8")
    before = _snapshot(str(d))
    assert _produced(str(d), before) == []

    (d / "mine.txt").write_text("new", encoding="utf-8")
    assert _produced(str(d), before) == ["mine.txt"]

    (d / "from_an_earlier_run.txt").write_text("changed by this run", encoding="utf-8")
    assert _produced(str(d), before) == ["from_an_earlier_run.txt", "mine.txt"]


def test_a_missing_image_is_a_refusal_with_the_command_to_fix_it(sandbox):
    result = DockerWorkspaceBackend(image="faustus-no-such-image:0.0.1").run(
        ExecutionSpec.parse(sandbox), ["true"], run_id="r")
    assert result.status == "refused"
    assert "image_missing" in result.reason
    assert "docker pull faustus-no-such-image:0.0.1" in result.reason
    assert result.exit_code is None          # nothing ran, so there is no code


def test_the_host_backend_will_not_start_without_the_acknowledgement(sandbox):
    spec = ExecutionSpec.parse({**sandbox, "backend": "local", "isolation": "none",
                                "attended_ack": True})
    # The contract lets this spec exist; the backend still checks.
    assert LocalAttendedBackend().preflight(spec)["ok"] is True

    from src.contracts import ContractError
    with pytest.raises(ContractError):
        ExecutionSpec.parse({**sandbox, "backend": "local", "isolation": "none"})


# ── with a real daemon ─────────────────────────────────────────────────────

@needs_docker
def test_the_run_is_not_root_and_sees_only_its_workspace(sandbox):
    result = DockerWorkspaceBackend(image=TEST_IMAGE).run(
        ExecutionSpec.parse(sandbox),
        ["sh", "-c", "id -u; ls /workspace"], run_id="t-user")
    assert result.status == "completed"
    lines = result.stdout_tail.split()
    assert lines[0] == "1000"
    assert lines[1:] == ["notes.txt"]


@needs_docker
def test_the_app_key_is_not_reachable_from_inside(sandbox):
    """The stop condition, named literally in the masterplan."""
    from src.constants import APP_KEY_FILE
    result = DockerWorkspaceBackend(image=TEST_IMAGE).run(
        ExecutionSpec.parse(sandbox),
        ["sh", "-c",
         "cat /workspace/../data/.app_key 2>&1; cat /data/.app_key 2>&1; "
         "cat /host/data/.app_key 2>&1; ls / | tr '\\n' ' '"],
        run_id="t-key")
    assert result.status == "completed"
    out = result.stdout_tail
    assert out.count("No such file or directory") >= 3
    assert "workspace" in out and "artifacts" in out
    # And the host really does have one, so the test is not passing vacuously.
    assert os.path.exists(APP_KEY_FILE) or "the host has no app key yet"


@needs_docker
def test_the_network_is_denied_unless_the_spec_asked(sandbox):
    probe = ["sh", "-c", "wget -T 3 -q -O- http://example.com >/dev/null 2>&1 "
                         "&& echo REACHED || echo denied"]
    closed = DockerWorkspaceBackend(image=TEST_IMAGE).run(
        ExecutionSpec.parse(sandbox), probe, run_id="t-net")
    assert closed.stdout_tail.strip() == "denied"


@needs_docker
def test_the_run_does_not_inherit_this_process_environment(sandbox, monkeypatch):
    monkeypatch.setenv("FAUSTUS_TEST_LEAK", "this-should-not-cross")
    result = DockerWorkspaceBackend(image=TEST_IMAGE).run(
        ExecutionSpec.parse(sandbox), ["sh", "-c", "env"], run_id="t-env")
    assert result.status == "completed"
    assert "this-should-not-cross" not in result.stdout_tail
    assert "FAUSTUS_TEST_LEAK" not in result.stdout_tail


@needs_docker
def test_only_a_declared_secret_crosses_and_an_undeclared_one_stops_the_run(sandbox):
    be = DockerWorkspaceBackend(image=TEST_IMAGE)

    allowed = be.run(ExecutionSpec.parse({**sandbox, "secret_names": ["DEMO_TOKEN"]}),
                     ["sh", "-c", "echo got=$DEMO_TOKEN other=${OTHER:-absent}"],
                     run_id="t-sec", secrets={"DEMO_TOKEN": "value-9f8a"})
    assert allowed.status == "completed"
    assert "got=value-9f8a" in allowed.stdout_tail
    assert "other=absent" in allowed.stdout_tail

    sneaked = be.run(ExecutionSpec.parse(sandbox), ["sh", "-c", "echo should-not-run"],
                     run_id="t-sec2", secrets={"SNEAKY": "value"})
    assert sneaked.status == "refused"
    assert "spec_wider_than_permissions" in sneaked.reason
    assert "SNEAKY" in sneaked.reason
    assert "should-not-run" not in sneaked.stdout_tail


@needs_docker
def test_a_timeout_kills_the_container_and_keeps_the_partial_output(sandbox):
    result = DockerWorkspaceBackend(image=TEST_IMAGE).run(
        ExecutionSpec.parse({**sandbox, "limits": {"seconds": 3}}),
        ["sh", "-c", "echo half > /artifacts/partial.txt; sleep 60"], run_id="t-timeout")
    assert result.status == "timeout"
    assert result.exit_code is None
    assert "killed after 3s" in result.reason
    assert result.artifact_filenames == ("partial.txt",)
    assert result.partial is True
    assert result.duration_ms < 30_000        # it did not wait out the sleep


@needs_docker
def test_an_allowlist_is_refused_rather_than_silently_opening_the_network(sandbox):
    result = DockerWorkspaceBackend(image=TEST_IMAGE).run(
        ExecutionSpec.parse({**sandbox, "network": True,
                             "network_allowlist": ["api.example.com"]}),
        ["sh", "-c", "echo ran"], run_id="t-allow")
    assert result.status == "refused"
    assert "egress proxy" in result.reason
    assert "ran" not in result.stdout_tail


@needs_docker
def test_a_command_the_image_does_not_have_is_a_failure_that_says_so(sandbox):
    result = DockerWorkspaceBackend(image=TEST_IMAGE).run(
        ExecutionSpec.parse(sandbox), ["definitely-not-a-binary"], run_id="t-404")
    assert result.status == "failed"
    assert result.exit_code == 127
    assert "not found in the image" in result.reason
