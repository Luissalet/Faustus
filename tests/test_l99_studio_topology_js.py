"""tests/test_l99_studio_topology_js.py — B2 (OBJ-8, CONTRATO_OBJ8_B.md).

Exposes Lote A4's already-implemented-and-tested backend
(docs/api/topology.md — Mermaid diagrams, agent-profile/workflow lint,
workflow cost estimate) in the Studio: adapters/topology.ts,
components/MermaidView.tsx, screens/agents/ProfileLint.tsx (plus a "Lint"
button on Defs.tsx), and "Ver diagrama"/"Estimar coste" on a workflow run's
detail in Activity.tsx.

This wiring is JSX across several screens, not pure logic a bundled import
can exercise — so, like test_l86_source_control_panel_js.py, it is checked
by source inspection (studio/checks/topology.check.mjs) rather than a DOM
render. No network calls anywhere in this file.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "topology.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_topology_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok topology" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_files_this_lot_owns_exist():
    for rel in (
        "studio/src/adapters/topology.ts",
        "studio/src/components/MermaidView.tsx",
        "studio/src/screens/agents/ProfileLint.tsx",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"


def test_no_inline_svg_or_new_mermaid_dependency():
    """The two guards CONTRATO_OBJ8_B.md calls out by name for this lot
    specifically, kept here too so a regression fails fast without needing
    node — the full sweep is tests/test_studio_guards.py."""
    mermaid_view = (_REPO / "studio" / "src" / "components" / "MermaidView.tsx").read_text(encoding="utf-8")
    assert "<svg" not in mermaid_view.lower() or "guard-ok:" in mermaid_view
    import json

    pkg = json.loads((_REPO / "package.json").read_text(encoding="utf-8"))
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    assert "mermaid" not in deps, "no mermaid renderer dependency was to be added (CONTRATO_OBJ8_B.md)"


def test_adapters_topology_has_an_api_error_and_no_direct_fetch_in_screens():
    topology_ts = (_REPO / "studio" / "src" / "adapters" / "topology.ts").read_text(encoding="utf-8")
    assert "ApiError" in topology_ts

    for rel in (
        "studio/src/screens/agents/ProfileLint.tsx",
        "studio/src/screens/agents/Defs.tsx",
        "studio/src/screens/Activity.tsx",
    ):
        text = (_REPO / rel).read_text(encoding="utf-8")
        assert "fetch(" not in text, f"{rel} must call the adapter, not fetch() directly"
