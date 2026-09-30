"""The /workflows screen's model-driven nodes, by behaviour.

Runs `studio/checks/workflows-model-nodes.render.check.mjs`, which mounts the
real `WorkflowsScreen` (esbuild + happy-dom) against a fake server and drives
it like a person: blank workflow, palette, the agent / classify / guard / loop
forms, branch labels on edges, library save and publish, evaluation (simulate
by default, real behind a second confirmation), a template, and a finished
run with its per-pass loop table. Run the script by hand to see what it checks.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node needed")
def test_workflows_model_nodes_render():
    result = subprocess.run(
        ["node", "studio/checks/workflows-model-nodes.render.check.mjs"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok workflows-model-nodes" in result.stdout
