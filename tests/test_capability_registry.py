"""The backend catalogue (src/capability_registry) — "definitions are durable
intent, observations are disposable facts".

The trap this exists to avoid: a registry that lists four backends and lets a
router believe any of them can take work. Three of them are not built yet, so
what is checked here is that the module says so — that `docker` on PATH is
reported as a CLI and not as a running daemon, that nothing rounds `unknown`
up to `available`, that the host backend is never a silent candidate, and that
a manifest with no eligible backend gets a sentence naming the nearest miss
instead of "no backend available".
"""
from __future__ import annotations

import pytest

from src import capability_registry as reg
from src import contracts as C


def manifest(**over):
    base = {
        "id": "media.video.short-form", "version": "1.0.0", "title": "t",
        "outputs": {"video": "artifact:video"},
        "permissions": {"backends": ["media_worker"]},
    }
    base.update(over)
    return C.SkillManifest.parse(base)


def test_nothing_is_available_that_has_no_code_behind_it():
    states = {o.backend_id: o for o in reg.observe_all()}
    assert states["local"].state == "available"
    for backend in ("media_worker", "remote_worker"):
        assert states[backend].state == "unavailable"
        assert "not implemented" in states[backend].evidence


def test_the_docker_state_comes_from_asking_and_says_what_it_asked():
    """Machine-independent on purpose: this asserts the *shape* of the answer,
    because whether Docker is up here is not a property of the code."""
    observed = reg.observe("docker_workspace", fresh=True)
    assert observed.state in reg.STATES
    if observed.state == "available":
        # It only gets to say that after talking to the daemon AND finding the
        # image; the evidence has to show both.
        assert "docker " in observed.evidence and "image" in observed.evidence
    else:
        assert observed.evidence, "an unavailable backend that will not say why is useless"


def test_a_cli_on_path_never_becomes_a_running_daemon(monkeypatch):
    """The exact shape that would be tempting to round up: docker installed,
    daemon not answering."""
    from src.capability_registry import Observation
    monkeypatch.setattr(reg, "_probe_cache", {})
    monkeypatch.setattr(reg, "_probe_docker", lambda stamp: Observation(
        "docker_workspace", "unavailable",
        "backend_unavailable: the docker CLI is installed but the daemon did not answer",
        stamp))
    observed = reg.observe("docker_workspace")
    assert observed.state == "unavailable"
    assert "daemon did not answer" in observed.evidence

    evidence = reg.docker_evidence()
    assert set(evidence) == {"cli_present", "path", "means", "checked_at"}
    assert evidence["means"] == "a CLI on PATH does not prove a daemon is running"


def test_the_probe_is_cached_briefly_and_every_answer_carries_its_own_timestamp(monkeypatch):
    from src.capability_registry import Observation
    calls = []
    monkeypatch.setattr(reg, "_probe_cache", {})
    monkeypatch.setattr(reg, "_probe_docker", lambda stamp: (
        calls.append(stamp), Observation("docker_workspace", "available", "stub", stamp))[1])
    first = reg.observe("docker_workspace")
    second = reg.observe("docker_workspace")
    assert len(calls) == 1                       # the second came from the cache
    assert first.checked_at == second.checked_at  # and says when it was taken
    reg.observe("docker_workspace", fresh=True)
    assert len(calls) == 2


def test_an_undeclared_backend_is_unavailable_and_says_why():
    obs = reg.observe("gpu_cluster")
    assert obs.state == "unavailable"
    assert obs.evidence == "no such backend is declared"


def test_the_requirement_is_read_off_the_outputs_not_asked_for_twice():
    assert "video" in reg.required_capabilities(manifest())
    docs = manifest(outputs={"report": "artifact:document"})
    assert "documents" in reg.required_capabilities(docs)
    hosted = manifest(permissions={"host_access": True, "backends": ["local"]})
    assert "host" in reg.required_capabilities(hosted)


def test_the_host_is_never_a_silent_candidate():
    rows = {r["backend"]: r for r in reg.candidates(
        manifest(outputs={"notes": "artifact:text"},
                 permissions={"host_access": True, "backends": ["local", "docker_workspace"]}))}
    assert rows["local"]["ok"] is False
    assert rows["local"]["reason"] == "attended_only"
    # …and the container backend cannot serve it either: it has no `host`.
    assert rows["docker_workspace"]["reason"] == "missing_capability"
    assert rows["docker_workspace"]["missing"] == ["host"]


def test_backends_the_manifest_did_not_ask_for_stay_in_the_answer():
    rows = {r["backend"]: r for r in reg.candidates(manifest())}
    assert set(rows) == {"local", "docker_workspace", "media_worker", "remote_worker"}
    assert rows["docker_workspace"]["reason"] == "not_requested"
    assert rows["media_worker"]["reason"] == "not_implemented"


def test_a_backend_the_build_does_not_know_is_named_as_such():
    rows = {r["backend"]: r for r in reg.candidates(
        manifest(permissions={"backends": ["quantum_worker"]}))}
    assert rows["quantum_worker"]["reason"] == "not_declared"


def test_the_refusal_names_the_nearest_miss():
    why = reg.why_no_backend(manifest())
    assert why.startswith("no backend can run media.video.short-form 1.0.0")
    assert "media_worker" in why and "not_implemented" in why


def test_a_spec_cannot_relabel_the_isolation_a_backend_provides():
    perms = manifest().permissions
    lying = C.ExecutionSpec.parse({"backend": "docker_workspace", "isolation": "remote"})
    verdict = reg.check_spec(lying, perms)
    assert verdict["ok"] is False
    assert any("relabel" in p["detail"] for p in verdict["problems"])


def test_a_spec_that_adds_a_secret_is_reported_field_by_field():
    perms = manifest(permissions={"backends": ["media_worker"], "secrets": ["comfy"]}).permissions
    spec = C.ExecutionSpec.parse({
        "backend": "media_worker", "isolation": "process",
        "secret_names": ["comfy", "openai"], "network": True,
    })
    verdict = reg.check_spec(spec, perms)
    details = " ".join(p["detail"] for p in verdict["problems"])
    assert "secrets:openai" in details
    assert "network" in details


def test_a_narrow_spec_passes():
    perms = manifest(permissions={"backends": ["media_worker"], "secrets": ["comfy"]}).permissions
    spec = C.ExecutionSpec.parse({"backend": "media_worker", "isolation": "process"})
    assert reg.check_spec(spec, perms) == {"ok": True, "backend": "media_worker", "problems": []}
