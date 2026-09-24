"""tests/test_e_wiring.py -- lot E's integrator-file cabling.

Everything functional for lot E (handoff lanes and night shift) lives in
this lot's own files (`src/handoff_lanes.py`, `src/night_shift.py`,
`routes/handoff_lanes_routes.py`, `routes/night_shift_routes.py`,
`src/agent_tools/night_shift_tools.py`) and is fully tested in
`tests/test_handoff_lanes.py` / `tests/test_night_shift.py`. The delegation
hook itself is wired directly into `src/agent_tools/subagent_tools.py`,
which is NOT an integrator file for this contract, so that part needed no
xfail.

The one integrator-owned file this lot needs touched is `app.py`, to
register the two new routers -- the exact lines are in `E_wiring.md`. This
assertion describes that wiring and is expected to fail until the
integrator applies it -- `strict=True` means this file starts failing
loudly the moment the wiring lands, as the signal to delete the `xfail`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(relpath: str) -> str:
    return (_REPO_ROOT / relpath).read_text(encoding="utf-8")


@pytest.mark.xfail(strict=True, reason="app.py wiring is the integrator's job (see E_wiring.md)")
def test_app_registers_the_handoff_lanes_and_night_shift_routers():
    src = _read("app.py")
    assert "from routes.handoff_lanes_routes import setup_handoff_lanes_routes" in src
    assert "app.include_router(setup_handoff_lanes_routes())" in src
    assert "from routes.night_shift_routes import setup_night_shift_routes" in src
    assert "app.include_router(setup_night_shift_routes())" in src
