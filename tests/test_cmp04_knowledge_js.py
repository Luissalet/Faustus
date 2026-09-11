"""tests/test_cmp04_knowledge_js.py — CMP-04 (CONTRATO_CMP_W2.md, W2-D).

Exposes the knowledge neighborhood (`src/knowledge_neighborhood.py`,
`routes/knowledge_routes.py`) in the Studio: adapters/knowledge.ts, a
compact "Contexto usado (n) — por qué" card in Transcript.tsx fed by the
new `context_receipts` SSE event (adapters/chat.ts), and "Vecindario de un
fichero" in Project.tsx's Context tab.

This wiring is JSX across several screens, not pure logic a bundled import
can exercise — so, like test_l99_studio_topology_js.py, it is checked by
source inspection (studio/checks/knowledge.check.mjs) rather than a DOM
render. No network calls anywhere in this file.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "knowledge.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_knowledge_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok knowledge" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_files_this_lot_owns_exist():
    for rel in (
        "src/knowledge_neighborhood.py",
        "routes/knowledge_routes.py",
        "studio/src/adapters/knowledge.ts",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"


def test_adapters_knowledge_has_no_direct_fetch_in_screens():
    for rel in (
        "studio/src/screens/studio/Transcript.tsx",
        "studio/src/screens/Project.tsx",
    ):
        text = (_REPO / rel).read_text(encoding="utf-8")
        assert "fetch(" not in text, f"{rel} must call the adapter, not fetch() directly"
