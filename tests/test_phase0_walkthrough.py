"""Phase 0 acceptance, end to end: "a fictional skill can validate its
manifest, create a run, emit events and produce an artifact without
privileges."

This is the one test that reads like the masterplan sentence it comes from.
It walks a made-up document skill from manifest to artifact and checks the
things that would make the whole layer pointless if they were wrong: that the
event order is the one the reference names, that the artifact ends up carrying
the run that made it, that the run's spec never widened past what the manifest
asked for, and — the part worth writing down — that the same walk with one
extra secret in the spec is stopped before anything starts.
"""
from __future__ import annotations

import pytest

from src import capability_registry as registry
from src import contracts as C


MANIFEST = {
    "id": "document.report",
    "version": "1.0.0",
    "title": "Write a report from notes",
    "family": "writing",
    "inputs": {"notes": "text", "sources": "artifact[]"},
    "outputs": {"report": "artifact:document"},
    "memory": {"read_scopes": ["project"], "write_scopes": ["run"]},
    "permissions": {"network": False, "secrets": [], "backends": ["docker_workspace"],
                    "filesystem": "workspace", "max_seconds": 600},
    "approval": {"required_when": []},
}


def walk(spec_body):
    """Manifest → run → events → artifact. Returns everything for inspection."""
    manifest = C.SkillManifest.parse(MANIFEST)
    spec = C.ExecutionSpec.parse(spec_body)
    gate = registry.check_spec(spec, manifest.permissions)

    events = []
    run = C.Run.parse({
        "id": "run-phase0", "kind": "skill", "owner": "luis",
        "project_id": "book", "skill_id": manifest.id,
        "skill_version": manifest.version,
        "execution_fingerprint": spec.fingerprint(),
    })
    events.append(C.emit("run.created", run_id=run.id, skill=manifest.id,
                         backend=spec.backend, seq=len(events)))
    if not gate["ok"]:
        run = run.advanced_to("failed", reason="spec grants more than the manifest asked for")
        events.append(C.emit("run.failed", run_id=run.id, seq=len(events),
                             problems=gate["problems"]))
        return {"manifest": manifest, "spec": spec, "gate": gate,
                "run": run, "events": events, "artifact": None}

    run = run.advanced_to("running")
    events.append(C.emit("backend.started", run_id=run.id, seq=len(events),
                         isolation=spec.isolation))
    events.append(C.emit("tool.progress", run_id=run.id, seq=len(events), percent=50))

    artifact = C.Artifact.parse({
        "id": "art-phase0", "kind": "document", "filename": "report.md",
        "sha256": "b" * 64, "media_type": "text/markdown", "byte_size": 2048,
        "owner": run.owner, "project_id": run.project_id, "run_id": run.id,
        "skill_id": manifest.id, "skill_version": manifest.version,
        "provenance": {"backend": spec.backend, "model": "qwen3.5:9b",
                       "model_license": "apache-2.0", "recipe": "report.v1",
                       "recipe_version": "1.0.0", "inputs_digest": "c" * 64},
        "retention": {"policy": "keep"},
    })
    events.append(C.emit("artifact.created", run_id=run.id, seq=len(events),
                         artifact_id=artifact.id, kind=artifact.kind))
    run = run.with_artifact(artifact.id).advanced_to("completed")
    events.append(C.emit("run.completed", run_id=run.id, seq=len(events)))
    return {"manifest": manifest, "spec": spec, "gate": gate,
            "run": run, "events": events, "artifact": artifact}


NARROW_SPEC = {
    "backend": "docker_workspace", "isolation": "container",
    "workspace": "/workspace", "artifacts_dir": "/artifacts",
    "limits": {"seconds": 600, "memory_mb": 2048},
}


def test_a_fictional_skill_gets_from_manifest_to_artifact():
    out = walk(NARROW_SPEC)
    assert out["gate"]["ok"] is True
    assert out["run"].status == "completed"
    assert out["run"].outcome == "success"
    assert out["artifact"].run_id == out["run"].id
    assert out["artifact"].id in out["run"].artifact_ids
    assert out["artifact"].provenance_gaps() == ()      # nothing unaccounted for


def test_the_event_order_is_the_one_the_reference_names():
    names = [e.name for e in walk(NARROW_SPEC)["events"]]
    assert names == ["run.created", "backend.started", "tool.progress",
                     "artifact.created", "run.completed"]
    assert [e.seq for e in walk(NARROW_SPEC)["events"]] == [0, 1, 2, 3, 4]


def test_nothing_in_the_walk_gained_a_privilege_it_did_not_ask_for():
    out = walk(NARROW_SPEC)
    perms = out["manifest"].permissions
    assert out["spec"].grants_beyond(perms) == ()
    assert out["spec"].network is False
    assert out["spec"].secret_names == ()
    assert out["spec"].isolation == "container"
    assert out["spec"].attended_ack is False


def test_one_extra_secret_stops_the_walk_before_anything_starts():
    out = walk({**NARROW_SPEC, "secret_names": ["openai"]})
    assert out["gate"]["ok"] is False
    assert out["artifact"] is None
    assert out["run"].status == "failed"
    assert [e.name for e in out["events"]] == ["run.created", "run.failed"]
    assert "secrets:openai" in " ".join(p["detail"] for p in out["gate"]["problems"])


def test_the_run_can_be_reproduced_from_its_own_record():
    """A run stores the fingerprint of the spec it ran under, so "was this the
    same setup as yesterday's?" is a comparison, not an argument."""
    a, b = walk(NARROW_SPEC), walk(NARROW_SPEC)
    assert a["run"].execution_fingerprint == b["run"].execution_fingerprint
    wider = walk({**NARROW_SPEC, "network": True})
    assert wider["run"].execution_fingerprint != a["run"].execution_fingerprint


def test_the_walk_survives_a_round_trip_through_json():
    import json
    out = walk(NARROW_SPEC)
    for obj in (out["run"], out["artifact"], out["spec"], out["manifest"]):
        payload = json.loads(json.dumps(obj.to_dict()))
        assert type(obj).parse(payload).to_dict() == obj.to_dict()
    for ev in out["events"]:
        assert C.Event.parse(json.loads(json.dumps(ev.to_dict()))).name == ev.name
