"""The live line says what the model is doing; the VRAM gate reaches the turn.

"Waiting for the model" with the model loaded read as a hang (09-09-2026).
The heartbeat now carries Ollama's `model_state` and the chat streams the
gate's `vram_admission` events; studio/checks/vram-live.check.mjs drives the
adapter and the turn reducer with both.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js required")
def test_model_state_and_vram_gate_reach_the_turn():
    result = subprocess.run(["node", "studio/checks/vram-live.check.mjs"], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok vram-live" in result.stdout


def test_the_chat_route_gates_before_its_first_call_and_the_screen_shows_the_dialog():
    route = (ROOT / "routes/chat_routes.py").read_text(encoding="utf-8")
    assert "async for _adm_ev in _vram_admission_events(sess.endpoint_url, sess.model, _user, _admission)" in route
    assert route.index("_vram_admission_events(sess.endpoint_url") < route.index("_terminal_saved = False")
    studio = (ROOT / "studio/src/screens/Studio.tsx").read_text(encoding="utf-8")
    assert "<VramAdmissionDialog" in studio and "tn.vram" in studio
