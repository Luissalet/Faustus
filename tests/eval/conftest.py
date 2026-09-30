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


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    """Record the process state before the first fixture of an eval test is set up (H24)."""
    from tests.eval import isolation
    item._leak_guard = isolation.guard()
    next(item._leak_guard)
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Fail any eval test that leaves the process, its environment or the
    developer's own data directory different from how it found them (H24).
    A hook wrapper, not a fixture: it runs after every fixture has been torn
    down and every monkeypatch undone, whatever order they were requested in."""
    yield
    guard = getattr(item, "_leak_guard", None)
    if guard is not None:
        item._leak_guard = None
        try:
            next(guard)
        except StopIteration:
            pass


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
