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


@pytest.fixture(autouse=True)
def no_stale_probes(monkeypatch):
    """Every test here starts from an empty probe cache.

    The cache is a 10-second memo, which is right in production and wrong
    across tests: one test that stands up a fake engine would otherwise decide
    what the next one observes. This caught exactly that.
    """
    monkeypatch.setattr(reg, "_probe_cache", {})


def test_a_backend_with_no_code_behind_it_is_unavailable_and_says_so():
    """The rule, not the inventory. `remote_worker` is Phase 6 and has no code,
    so it answers `unavailable` for that reason and no other — and crucially
    it is never probed, because there is nothing to probe."""
    observed = reg.observe("remote_worker")
    assert observed.state == "unavailable"
    assert "not implemented" in observed.evidence


def test_an_implemented_backend_has_to_be_asked_rather_than_assumed(monkeypatch):
    """`media_worker` has code behind it now, and that is exactly why its
    state may no longer come from the fact that the code exists.

    This test used to assert "media_worker is unavailable because it is not
    implemented". Implementing it broke that, with reason: the assertion
    described the world in September rather than the rule. The rule is that an
    implemented backend reports what a real probe found, so what is pinned
    here is that the probe is what decides — both ways.
    """
    from src.capability_registry import Observation

    monkeypatch.setattr(reg, "_probe_comfyui", lambda stamp: Observation(
        "media_worker", "available", "ComfyUI at http://127.0.0.1:8188", stamp))
    assert reg.observe("media_worker", fresh=True).state == "available"

    monkeypatch.setattr(reg, "_probe_comfyui", lambda stamp: Observation(
        "media_worker", "unavailable",
        "backend_unavailable: nothing answered at http://127.0.0.1:8188", stamp))
    down = reg.observe("media_worker", fresh=True)
    assert down.state == "unavailable"
    assert "nothing answered" in down.evidence


def test_a_probe_that_blows_up_is_unknown_rather_than_taking_the_page_with_it(monkeypatch):
    def explode(stamp):
        raise RuntimeError("the network stack is on fire")

    monkeypatch.setattr("src.media_backends.ComfyUIBackend",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("the network stack is on fire")))
    observed = reg.observe("media_worker", fresh=True)
    assert observed.state == "unknown"
    assert "the probe itself failed" in observed.evidence


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
    """Every declared backend appears with a reason, including the ones this
    manifest did not name. A candidate list that only showed the eligible one
    answers "which backend" and not "why not the others", which is the
    question somebody actually has."""
    rows = {r["backend"]: r for r in reg.candidates(manifest())}
    assert set(rows) == {"local", "docker_workspace", "media_worker", "remote_worker"}
    assert rows["docker_workspace"]["reason"] == "not_requested"
    assert rows["remote_worker"]["reason"] == "not_requested"
    # The one it DID ask for is answered from a probe, so its reason depends on
    # whether an engine is running — which is not a property of this code.
    assert rows["media_worker"]["reason"] in ("eligible", "unavailable")

    # And a manifest that asks for something with no code behind it is told
    # that. The outputs are text here on purpose: a capability gap is checked
    # first and is a truer reason, so asking for video from a backend that
    # has no video would report the gap and never reach "not implemented".
    asked = {r["backend"]: r for r in reg.candidates(
        manifest(outputs={"notes": "artifact:text"},
                 permissions={"backends": ["remote_worker"]}))}
    assert asked["remote_worker"]["reason"] == "not_implemented"


def test_a_backend_the_build_does_not_know_is_named_as_such():
    rows = {r["backend"]: r for r in reg.candidates(
        manifest(permissions={"backends": ["quantum_worker"]}))}
    assert rows["quantum_worker"]["reason"] == "not_declared"


def test_the_refusal_names_the_nearest_miss(monkeypatch):
    """"No backend available" sends someone hunting. "media_worker, and here
    is what it said when we asked it" sends them to the engine.

    The engine is stubbed DOWN on purpose: whether ComfyUI happens to be
    running on the machine running the tests is not a property of this code,
    and a test that passes only when it is absent is a test that will fail on
    the first machine that has it."""
    from src.capability_registry import Observation

    monkeypatch.setattr(reg, "_probe_comfyui", lambda stamp: Observation(
        "media_worker", "unavailable",
        "backend_unavailable: nothing answered at http://127.0.0.1:8188", stamp))

    why = reg.why_no_backend(manifest())
    assert why.startswith("no backend can run media.video.short-form 1.0.0")
    assert "media_worker" in why and "nothing answered" in why


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
