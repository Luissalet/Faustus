"""EVAL-01/EVAL-04 infra: one real Faustus server (subprocess) + one
scripted model for the whole `tests/eval` session — the app boots once
(~a few seconds) and every task in `tasks.py` reuses it, each in its own
session and its own tmp workspace so tasks never interfere with each other.

Not gated behind `ODYSSEUS_E2E` like `tests/e2e`: this suite needs no
browser (Playwright), only a subprocess and a loopback socket, so it is
"EJECUTABLE SIN MODELO" out of the box, exactly as EVAL-01 asks.
"""
from __future__ import annotations

import pytest

from tests.eval.harness import EvalApp


@pytest.fixture(autouse=True)
def project_python_for_task_verify(monkeypatch):
    """The tasks' verify() runs their tests in THIS process, with this
    interpreter (it has pytest; the host's PATH python may not). Set per test
    and undone after it: a session-wide change leaked into every later test
    of the same worker (test_project_tests_scoping saw it)."""
    import sys
    monkeypatch.setenv("FAUSTUS_PROJECT_PYTHON", sys.executable)


@pytest.fixture(scope="session")
def eval_app():
    app = EvalApp()
    app.start()
    yield app
    app.stop()
