"""Shared helper for tests/acceptance/*.

Acceptance tests (docs/spec/paridad/) exercise real Faustus code end to
end - TestClient against real routes, real agent_runs/approval_store/
tool_approvals/workflows, fakes only for the LLM and for external processes
- never a mock of the module under test. Each test function carries exactly
one ``@pytest.mark.acceptance("A0N")`` (see pyproject.toml for the marker's
registration and tests/test_acceptance_index.py for what cross-checks it
against docs/spec/paridad/ESTADO_ACEPTACION.md).

Case A03 predates this directory and stays where it lives
(tests/test_tool_approvals.py::test_dispatcher_rejects_modified_approved_action);
it only gained the marker decorator in this lot. New cases belong in their
own tests/acceptance/test_a<NN>_<slug>.py module.
"""
from __future__ import annotations

from typing import Any

import pytest


def record_evidence(request: pytest.FixtureRequest, **refs: Any) -> None:
    """Attach ``refs`` to this test item as its ``evidence_refs``.

    scripts/acceptance_run.py's collector plugin reads this back off every
    passed/failed/xfailed item (via ``request.node.user_properties``) to
    fill the ``evidence_refs`` field of that case's JSONL row - concrete
    pointers (nodeids, files, run/session ids the test created) instead of a
    bare pass/fail. Safe to call more than once in one test; a later call
    replaces the previously recorded ``evidence_refs`` rather than stacking
    duplicates.

    A test that never calls this still gets an ``evidence_refs`` of at least
    its own pytest nodeid from the runner - this is for the *extra* pointers
    worth a reader's attention (e.g. the run_id a scenario exercised, the
    exact assertion that closes the case).
    """
    request.node.user_properties = [
        (name, value) for name, value in request.node.user_properties
        if name != "evidence_refs"
    ] + [("evidence_refs", dict(refs))]
