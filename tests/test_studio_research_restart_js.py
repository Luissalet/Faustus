"""A server restart does not turn a running research into "The research failed."

10-09-2026: the Research screen's follower gave up on the first network
error and a reloaded screen never heard of the run again. The adapter now
rides out a short outage, reads the server's "interrupted" answer, adopts
interrupted runs from /api/research/active, and dismisses them once shown;
studio/checks/research-restart.check.mjs drives it against a scripted fetch.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js required")
def test_the_follower_rides_out_a_restart_and_reads_interrupted():
    result = subprocess.run(["node", "studio/checks/research-restart.check.mjs"], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok research-restart" in result.stdout


def test_the_screen_shows_interrupted_runs_as_failed_cards_with_retry():
    screen = (ROOT / "studio/src/screens/research/Research.tsx").read_text(encoding="utf-8")
    assert "a.status !== 'interrupted'" in screen
    assert screen.count("void dismissResearch(") == 2
    assert "The server restarted while this research was running. Retry starts it again." in screen
    route = (ROOT / "routes/research/research_routes.py").read_text(encoding="utf-8")
    assert "research_handler.list_interrupted(user)" in route
    assert '@router.post("/api/research/{session_id}/dismiss")' in route
