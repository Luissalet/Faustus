"""tests/test_cmp_gen_panel_js.py — CMP-GEN.

Runs `studio/checks/gen-sampling-panel.check.mjs` — the composer's
generation chip becoming a real sampling panel (temperature/top_p/top_k/
max_tokens/think) instead of only `describeGen`'s read-only label, and the
`supportsThinking` mirror of `src/llm_core.py`'s `_THINKING_MODEL_PATTERNS`.
Same node-subprocess pattern `tests/test_w3a_composer_js.py` uses for its
own `.check.mjs` file.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "gen-sampling-panel.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_gen_sampling_panel_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok gen_sampling_panel" in proc.stdout
