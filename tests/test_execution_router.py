"""The router (src/execution_router) — one rule, tested from several angles.

**The host is never a fallback.** Not when Docker is down, not when the image
is missing, not when the preferred backend cannot take the run. The tests
below take each of those routes and check the answer is a refusal that names
what happened, because the failure mode being guarded against is not a crash —
it is a successful-looking run that quietly happened on the host.

The second rule is quieter: the spec the router builds is derived from the
manifest's permissions, so no argument a caller passes can widen it.
"""
from __future__ import annotations

import pytest

from src import capability_registry as registry
from src import execution_router as router
from src.contracts import SkillManifest


def manifest(**over):
    body = {
        "id": "document.report", "version": "1.0.0", "title": "Report",
        "outputs": {"report": "artifact:document"},
        "permissions": {"backends": ["docker_workspace"], "max_seconds": 60},
    }
    body.update(over)
    return SkillManifest.parse(body)


@pytest.fixture()
def paths(tmp_path):
    work = tmp_path / "ws"
    work.mkdir()
    return {"workspace": str(work), "artifacts_root": str(tmp_path / "runs")}


@pytest.fixture()
def docker_down(monkeypatch):
    """Docker installed, daemon not answering — the exact shape of the outage
    that a fallback would paper over."""
    from src.capability_registry import Observation
    monkeypatch.setattr(registry, "_probe_cache", {})
    monkeypatch.setattr(registry, "_probe_docker", lambda stamp: Observation(
        "docker_workspace", "unavailable",
        "backend_unavailable: the docker CLI is installed but the daemon did not answer",
        stamp))
    return True


@pytest.fixture()
def docker_up(monkeypatch):
    from src.capability_registry import Observation
    monkeypatch.setattr(registry, "_probe_cache", {})
    monkeypatch.setattr(registry, "_probe_docker", lambda stamp: Observation(
        "docker_workspace", "available", "docker 99.0 (stubbed)", stamp))
    return True


def test_a_dead_daemon_is_a_refusal_and_never_the_host(docker_down, paths):
    decision = router.choose(manifest(), run_id="r1", **paths)
    assert decision.ok is False
    assert decision.backend == ""
    assert decision.reason == "no_backend"
    assert "daemon did not answer" in decision.detail
    rows = {c["backend"]: c for c in decision.candidates}
    assert rows["docker_workspace"]["reason"] == "unavailable"
    assert rows["local"]["ok"] is False


def test_the_host_needs_two_independent_yeses(docker_down, paths):
    hostly = manifest(id="system.check", outputs={},
                      permissions={"backends": ["local"], "host_access": True})

    # The manifest names it, but nobody acknowledged.
    assert router.choose(hostly, run_id="r2", **paths).ok is False

    # Somebody acknowledged, but the manifest never asked for the host.
    assert router.choose(manifest(), run_id="r3", attended_ack=True, **paths).ok is False

    # Both.
    both = router.choose(hostly, run_id="r4", attended_ack=True, **paths)
    assert both.ok is True and both.backend == "local"
    assert both.spec.attended_ack is True


def test_an_outage_does_not_become_an_acknowledgement(docker_down, paths):
    """The dangerous composition: a manifest that lists both backends. Docker
    is down and the caller did acknowledge — but for a run that was going to
    the sandbox, so the acknowledgement is not a licence to reroute it."""
    both_listed = manifest(permissions={"backends": ["docker_workspace"], "max_seconds": 60})
    decision = router.choose(both_listed, run_id="r5", attended_ack=True, **paths)
    assert decision.ok is False
    assert decision.backend != "local"


def test_a_preferred_backend_that_cannot_take_it_refuses_rather_than_reroutes(docker_up, paths):
    decision = router.choose(manifest(permissions={"backends": ["docker_workspace",
                                                                "media_worker"]}),
                             run_id="r6", prefer="media_worker", **paths)
    assert decision.ok is False
    assert decision.reason == "preferred_backend_unusable"
    assert "Not falling back" in decision.detail


def test_the_spec_is_derived_from_the_permissions_and_cannot_be_widened(docker_up, paths):
    decision = router.choose(manifest(), run_id="r7", **paths)
    assert decision.ok is True
    spec = decision.spec
    assert spec.network is False
    assert spec.secret_names == ()
    assert spec.isolation == "container"
    assert spec.attended_ack is False
    assert spec.limits.seconds == 60              # from the manifest, not the default
    assert spec.grants_beyond(manifest().permissions) == ()


def test_the_backend_default_timeout_applies_when_the_manifest_is_silent(docker_up, paths):
    quiet = manifest(permissions={"backends": ["docker_workspace"]})
    spec = router.choose(quiet, run_id="r8", **paths).spec
    assert spec.limits.seconds == registry.declaration("docker_workspace").max_seconds_default


def test_each_run_gets_its_own_output_directory(docker_up, paths, tmp_path):
    import os
    a = router.choose(manifest(), run_id="run-a", **paths)
    b = router.choose(manifest(), run_id="run-b", **paths)
    assert a.spec.artifacts_dir != b.spec.artifacts_dir
    assert os.listdir(a.spec.artifacts_dir) == []
    assert sorted(os.listdir(paths["artifacts_root"])) == ["run-a", "run-b"]


def test_a_declared_secret_with_no_value_stops_the_run_before_it_starts(docker_up, paths):
    """`require_approval=False` on purpose: asking for a secret raises an
    approval card (`implied_approvals`), and that gate now fires FIRST — you
    do not hand a credential to a run nobody approved. The ordering is checked
    in tests/test_approval_gate_on_runs.py; what is checked here is the second
    gate, which would otherwise be unreachable in this test."""
    needs_key = manifest(permissions={"backends": ["docker_workspace"],
                                      "secrets": ["OPENAI_API_KEY"]})
    decision, result = router.execute(needs_key, ["true"], run_id="r9",
                                      require_approval=False, **paths)
    assert decision.ok is True                    # the routing was fine
    assert result.status == "refused"             # the run was not
    assert "OPENAI_API_KEY" in result.reason
    assert result.exit_code is None


def test_the_full_candidate_list_travels_with_every_answer(docker_up, paths):
    decision = router.choose(manifest(), run_id="r10", **paths)
    names = {c["backend"] for c in decision.candidates}
    assert names == {"local", "docker_workspace", "media_worker", "remote_worker"}
    assert all("reason" in c for c in decision.candidates)
    assert decision.to_dict()["spec"]["backend"] == "docker_workspace"
