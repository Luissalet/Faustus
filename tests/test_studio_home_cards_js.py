"""Home cards: automations pinned to the Home screen showing their latest
result. `studio/checks/home-cards.check.mjs` asserts the `homeCards.ts`
adapter, the "Your cards" block mounted first on Home, the inline two-step
unpin confirm, and the Automations pin toggle/badge against the real
source. Needs node — skipped otherwise."""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "home-cards.check.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node needed")


def test_home_cards_check_passes():
    proc = subprocess.run(["node", str(_CHECK)], capture_output=True, text=True,
                          encoding="utf-8", cwd=str(_REPO), timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
