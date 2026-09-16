"""A34 — importing a reference-harness-shaped project (documented interchange
format, docs/spec/paridad/MIGRACION.md) with an unsupported resource and a
connector carrying a credential.

Real code under test: ``src.migration.preview``/``apply`` against a real
fixture project written to ``tmp_path`` and a real ``DATA_DIR`` (also
``tmp_path``, via monkeypatch) — no mocks of the module under test.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import migration
from tests.acceptance.conftest import record_evidence

TOKEN_VALUE = "sk-super-secret-do-not-copy-9f3a1c"


def _build_fixture_project(root: Path) -> Path:
    project = root / "reference_project"
    (project / "agents").mkdir(parents=True)
    (project / "skills" / "greeter").mkdir(parents=True)
    (project / "sessions").mkdir(parents=True)
    (project / "files" / "notes").mkdir(parents=True)

    (project / "agents" / "reviewer.json").write_text(json.dumps({
        "name": "Reviewer", "model": "gpt-4o",
        "instructions": "Review pull requests for correctness.",
        "tools": ["read_file"],
    }), encoding="utf-8")

    (project / "models.json").write_text(json.dumps([
        {"id": "gpt-4o", "provider": "openai", "params": {}},
        {"id": "mystery-model", "provider": "acme-cloud", "params": {}},
    ]), encoding="utf-8")

    # One connector with an external credential — must never be copied.
    (project / "connectors.json").write_text(json.dumps([
        {"id": "slack-conn", "type": "slack", "config": {"channel": "#eng"},
         "credentials": {"token": TOKEN_VALUE}},
    ]), encoding="utf-8")

    (project / "skills" / "greeter" / "SKILL.md").write_text(
        "---\nname: greeter\n---\n\nSay hello.\n", encoding="utf-8",
    )

    (project / "sessions" / "s1.jsonl").write_text(
        "\n".join([
            json.dumps({"role": "user", "content": "hi", "ts": "2026-01-01T00:00:00Z"}),
            json.dumps({"role": "assistant", "content": "hello", "ts": "2026-01-01T00:00:01Z"}),
        ]),
        encoding="utf-8",
    )

    (project / "files" / "notes" / "readme.txt").write_text("project notes", encoding="utf-8")

    # One UNSUPPORTED resource: a schedule whose action type Faustus's
    # scheduler has no mapping for.
    (project / "schedules.json").write_text(json.dumps([
        {"id": "nightly-webhook", "cron": "0 3 * * *",
         "action": {"type": "webhook_call", "url": "https://example.invalid/hook"}},
        {"id": "morning-prompt", "cron": "0 8 * * *",
         "action": {"type": "prompt", "text": "Summarize overnight activity."}},
    ]), encoding="utf-8")

    return project


@pytest.mark.acceptance("A34")
def test_preview_classifies_unsupported_resource_and_connector_credential(request, tmp_path):
    project = _build_fixture_project(tmp_path)

    report = migration.preview(project)

    by_kind_status = {(e.kind, e.status) for e in report.entries}
    assert ("agent", "transformed") in by_kind_status
    assert ("model", "transformed") in by_kind_status  # gpt-4o/openai
    assert ("model", "unsupported") in by_kind_status  # mystery-model/acme-cloud
    assert ("skill", "preserved") in by_kind_status
    assert ("session", "transformed") in by_kind_status
    assert ("file", "preserved") in by_kind_status
    assert ("schedule", "transformed") in by_kind_status  # morning-prompt
    assert ("schedule", "unsupported") in by_kind_status  # nightly-webhook
    assert ("connector", "requires_reauthorization") in by_kind_status

    # The connector entry's reason names re-authorization and the real
    # connect route, never repeats the token value.
    connector_entry = next(e for e in report.entries if e.kind == "connector")
    assert TOKEN_VALUE not in connector_entry.reason
    assert TOKEN_VALUE not in connector_entry.to_dict().__repr__()
    assert "connect" in connector_entry.reason.lower() or "connect" in connector_entry.target_hint.lower()

    # The unsupported schedule is listed, not silently dropped.
    unsupported_schedule = next(
        e for e in report.entries if e.kind == "schedule" and e.status == "unsupported"
    )
    assert "webhook_call" in unsupported_schedule.reason

    record_evidence(
        request, project=str(project),
        entry_count=len(report.entries),
        statuses=sorted({e.status for e in report.entries}),
    )


@pytest.mark.acceptance("A34")
def test_apply_never_writes_the_token_and_omits_nothing_from_the_report(request, tmp_path):
    project = _build_fixture_project(tmp_path)
    data_dir = tmp_path / "data_dir"
    data_dir.mkdir()

    report = migration.preview(project)
    result = migration.apply(project, report, out_root=data_dir / "migrations")

    out_dir = Path(result.output_dir)
    assert out_dir.is_dir()

    # Grep the ENTIRE apply output tree (and manifest/losses) for the raw
    # token value: it must not appear anywhere DATA_DIR-rooted.
    all_text = []
    for f in out_dir.rglob("*"):
        if f.is_file():
            try:
                all_text.append(f.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass
    joined = "\n".join(all_text)
    assert TOKEN_VALUE not in joined, "credential token leaked into DATA_DIR migration output"

    # The connector was correctly excluded (requires_reauthorization is
    # never imported) but must still be named in losses.md — no silent
    # omission of what preview found.
    losses_text = Path(result.losses_path).read_text(encoding="utf-8")
    assert "slack-conn" in losses_text
    assert "requires_reauthorization" in losses_text
    assert "webhook_call" in losses_text or "nightly-webhook" in losses_text

    # Every entry preview found is accounted for in either imported or
    # excluded — nothing vanishes between preview and apply.
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    accounted = len(manifest["imported"]) + len(manifest["excluded"])
    assert accounted == len(report.entries)

    # What SHOULD have imported did: the agent, the supported model, the
    # skill, the session, the plain file, and the supported schedule.
    imported_kinds = [e["kind"] for e in manifest["imported"]]
    assert imported_kinds.count("agent") == 1
    assert "skill" in imported_kinds
    assert "session" in imported_kinds
    assert "file" in imported_kinds
    assert (out_dir / "agents" / "reviewer" / "AGENT.md").is_file()
    assert (out_dir / "skills" / "greeter" / "SKILL.md").is_file()
    assert (out_dir / "files" / "notes" / "readme.txt").is_file()

    record_evidence(
        request, import_id=result.import_id, output_dir=result.output_dir,
        imported=len(result.imported), excluded=len(result.excluded),
    )
