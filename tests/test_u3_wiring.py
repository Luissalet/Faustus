"""U3 wiring gap (see .../scratchpad/paridad_wave2/U3_wiring.md).

`routes/skill_source_routes.py` (A25, new) is real and its own tests
(`tests/acceptance/test_a25_skill_git_source.py`,
`tests/test_u3_skill_source_routes.py`) exercise it fully against a
standalone FastAPI app. What is NOT wired without touching `app.py` (not an
owned file for this lot — CONTRATO.md's ownership table) is registering
that router on the REAL app, so `/api/skill-sources/...` is unreachable over
the actual running server until the two-line addition in
`U3_wiring.md` lands.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_skill_source_routes_registered_on_the_real_app():
    text = (REPO / "app.py").read_text(encoding="utf-8")
    assert "setup_skill_source_routes" in text
