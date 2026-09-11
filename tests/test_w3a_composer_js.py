"""tests/test_w3a_composer_js.py — W3-A (CONTRATO_W3.md, ref CONTRATO_CMP_W2.md).

Runs `studio/checks/composer_context.check.mjs` — the `strategy` SSE event
(decode -> Turn.strategy -> metadata.strategy restore), `DocSuggestion.anchor`
decoding, `SidePanel.tsx`'s anchor-based auto-disambiguation, the composer's
document-context chip (`COMPOSER_CONTEXT_EVENT`), and "Guardar como receta"
on a finished turn's TURN SUMMARY. Same node-subprocess pattern
`tests/test_cmp09_strategy_js.py`/`tests/test_cmp01_doc_session_js.py` use
for their own `.check.mjs` files.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "composer_context.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_composer_context_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok composer_context" in proc.stdout
