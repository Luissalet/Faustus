"""tests/test_cmp07_workflows_js.py — CMP-07 (W2-E, CONTRATO_CMP_W2.md).

Runs `studio/checks/workflows.check.mjs` (source inspection of the
`/workflows` screen: `WorkflowsScreen`, `PlanGraph`, `NodeInspector`,
`RunOverlay`, the four new `adapters/topology.ts` functions, and the
routing wiring in `routes.ts`/`AppShell.tsx`/`app.py`) and pins the two
guards CONTRATO_CMP_W2.md calls out by name for this lot: no inline SVG
without a `guard-ok:` exemption, and no new graph-drawing dependency.

`-p no:cacheprovider -W ignore` per COMUN.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "workflows.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_workflows_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok workflows" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_files_this_lot_owns_exist():
    for rel in (
        "studio/src/adapters/topology.ts",
        "studio/src/screens/workflows/WorkflowsScreen.tsx",
        "studio/src/screens/workflows/PlanGraph.tsx",
        "studio/src/screens/workflows/NodeInspector.tsx",
        "studio/src/screens/workflows/RunOverlay.tsx",
        "studio/src/screens/workflows/workflows.css",
        "src/workflows/simulate.py",
        "src/contracts/workflow_iteration.py",
        "docs/design/bounded-workflow-iterations.md",
        "docs/adaptations/decisions/CMP-07.md",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"


def test_no_svg_drawing_dependency_was_added():
    """CONTRATO_CMP_W2.md's own limit on PlanGraph: 'grafo SVG propio, sin
    dependencia nueva'."""
    pkg = json.loads((_REPO / "package.json").read_text(encoding="utf-8"))
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    for banned in ("reactflow", "react-flow-renderer", "d3", "mermaid", "cytoscape", "vis-network"):
        assert banned not in deps, f"{banned} must not be a new dependency for the plan graph"


def test_plan_graph_svg_carries_its_guard_exemption():
    src = (_REPO / "studio" / "src" / "screens" / "workflows" / "PlanGraph.tsx").read_text(encoding="utf-8")
    assert "<svg" in src
    assert "guard-ok:" in src


def test_routing_is_registered_server_side_too():
    app_py = (_REPO / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/workflows")' in app_py
    routes_ts = (_REPO / "studio" / "src" / "shell" / "routes.ts").read_text(encoding="utf-8")
    assert "/workflows" in routes_ts
