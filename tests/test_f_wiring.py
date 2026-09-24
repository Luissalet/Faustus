"""tests/test_f_wiring.py — lot F's integrator-file cabling.

`src/code_history.py`, `src/agent_tools/code_history_tools.py` and
`routes/code_history_routes.py` are this lot's own files and are fully tested
in `tests/test_code_history.py`. Wiring the route into `app.py` and adding
`code_history` to `src/agent_loop.py`'s git-read family and code-intel family
is the integrator's job (those files are off limits to this lot); the exact
diff is in `F_wiring.md`. These assertions describe the wired behaviour and
are expected to fail until the integrator applies it — `strict=True` means
this file starts failing (loudly) the moment the wiring lands, as the signal
to delete the `xfail` mark.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(relpath: str) -> str:
    return (_REPO_ROOT / relpath).read_text(encoding="utf-8")


def test_app_registers_the_code_history_router():
    src = _read("app.py")
    assert "from routes.code_history_routes import setup_code_history_routes" in src
    assert "app.include_router(setup_code_history_routes())" in src


def test_agent_loop_offers_code_history_as_a_git_read_tool():
    src = _read("src/agent_loop.py")
    assert '"git_diff", "github_issue", "code_history"}' in src


def test_agent_loop_code_intel_family_includes_code_history():
    src = _read("src/agent_loop.py")
    assert '"code_graph_drift", "code_history"' in src
