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


@pytest.fixture(scope="session")
def eval_app():
    app = EvalApp()
    app.start()
    yield app
    app.stop()
