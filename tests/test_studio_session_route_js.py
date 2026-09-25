"""routeForSession (studio/src/adapters/chat.ts): a reopened conversation
lands on the model AND server it was using, even when the model list loads
after its history. Seen live: a chat held on the llama-server opened on
"No models", then on another server's default model."""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "session-route.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_session_route_checks_pass():
    proc = subprocess.run(["node", str(_CHECK)], capture_output=True, text=True,
                          encoding="utf-8", cwd=str(_REPO), timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout


def test_the_screen_waits_for_the_model_list():
    src = (_REPO / "studio" / "src" / "screens" / "Studio.tsx").read_text(encoding="utf-8")
    assert "routeForSession(routes, result.model, result.endpointUrl)" in src
    assert "pendingSessionRoute.current = { sessionId" in src
    assert "routeForSession(routes, pending.model, pending.endpointUrl)" in src
