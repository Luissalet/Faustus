"""A14 - acceptance-parity case.

Contract (docs/spec/paridad/, A14): "Compact with unresolved approval and
important earlier constraints" -> "Approval identity, constraints, objective
and source references survive".

Exercises `src.context_compactor.compact_with_integrity` (the deterministic
midturn compaction path, CTX-02) against a REAL `src.approval_store` pending
card (real SQLite row, `core.database.SessionLocal`, same pattern as
tests/acceptance/test_a02_approval_race.py) plus a real message history
containing an ES/EN constraint, a URL and a path. Nothing here mocks
`context_compactor` itself.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src import approval_store, context_compactor
from tests.acceptance.conftest import record_evidence

PLAN = {
    "action": "destructive", "skill_id": "", "skill_version": "",
    "backend": "docker_workspace", "recipients": [],
    "cost_units": 0, "secret_names": [], "output_kinds": [],
    "detail": "Run the deploy script against production.",
}


@pytest.fixture()
def real_db(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "a14.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()


@pytest.mark.acceptance("A14")
def test_compaction_preserves_pending_approval_constraints_objective_and_sources(
    real_db, request,
):
    approval = approval_store.request(PLAN, owner="luis", session_id="s-a14")
    assert approval.status == "pending"

    url = "https://example.com/runbooks/deploy"
    path = "src/deploy/runner.py"
    constraint_en = "Never touch tests/ without asking me first."
    constraint_es = "No toques la carpeta tests/ sin preguntarme antes."
    objective = "Investigate why the nightly deploy failed and fix it."

    older_block = [
        {"role": "user", "content": objective},
        {"role": "user", "content": constraint_en},
        {"role": "user", "content": constraint_es},
        {"role": "assistant",
         "content": f"I need your approval to run the deploy script. "
                    f"approval_id={approval.id}"},
        {"role": "assistant",
         "content": f"See the runbook at {url} and the script at {path}."},
        {"role": "user", "content": "Sounds good, go ahead and check it."},
    ]
    # Enough recent turns to keep `older_block` fully outside `keep_recent`
    # so compact_with_integrity actually folds it.
    recent_block = [{"role": "user", "content": f"follow-up turn {i}"} for i in range(6)]
    messages = older_block + recent_block

    folded, evidence = context_compactor.compact_with_integrity(
        messages, owner_id="luis", session_id="s-a14", keep_recent=4,
    )
    assert evidence, "expected compact_with_integrity to actually fold something"

    marker = next(
        m for m in folded
        if isinstance(m, dict) and m.get("metadata", {}).get("ctx02_compacted")
    )
    text = marker["content"]

    # (1) approval identity, literal - never "you asked for permission".
    assert approval.id in text
    assert PLAN["action"] in text
    assert approval.plan.fingerprint() in text

    # (2) both ES/EN constraints, literal.
    assert "tests/" in text
    assert "Never touch tests/" in text or "never touch tests/" in text.lower()
    assert "No toques la carpeta tests/" in text

    # (3) the declared objective, literal.
    assert objective in text

    # (4) source references: the URL and the path, literal.
    assert url in text
    assert path in text

    preserve = marker["metadata"]["compaction_preserve"]
    assert preserve["approvals"] and preserve["approvals"][0]["approval_id"] == approval.id
    assert preserve["approvals"][0]["tool"] == PLAN["action"]
    assert preserve["approvals"][0]["args_digest"] == approval.plan.fingerprint()
    assert any("tests/" in c for c in preserve["constraints"])
    assert objective in preserve["objective"]
    assert url in preserve["source_refs"]
    assert path in preserve["source_refs"]

    # The approval itself is STILL pending in the real store - compaction
    # never resolved it, only preserved its identity in the summary.
    assert approval_store.get(approval.id).status == "pending"

    record_evidence(
        request,
        approval_id=approval.id,
        session_id="s-a14",
        marker_metadata_keys=sorted(marker["metadata"].keys()),
    )
