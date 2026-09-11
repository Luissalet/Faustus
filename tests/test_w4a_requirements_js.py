"""tests/test_w4a_requirements_js.py — W4-A.

The backend for versioned requirements (ADP-18/19/20:
``src/requirements/{store,context,evidence}.py``,
``routes/requirements_routes.py``, ``docs/api/requirements.md``,
``docs/requirements-format.md``) shipped with no screen. This lot is the
screen only: ``studio/src/adapters/requirements.ts`` (typed against
``docs/api/requirements.md``), ``studio/src/screens/project/Requirements.tsx``
(REQ-N list, human-only accept/reject, evidence matrix, "context for a
task", a read-only sidecar preview), and its registration as a tab in
``studio/src/screens/Project.tsx`` next to Board/Objectives.

Most of this is JSX wiring across a large screen, not pure logic a bundled
import alone can exercise — so, like ``test_l99_studio_topology_js.py`` and
``test_cmp13_alternatives_js.py``, it is checked by
``studio/checks/requirements.check.mjs``, which itself bundles and runs the
adapter's request-shaping logic against a mocked ``fetch`` before falling
back to static source inspection for the screen. No network calls anywhere
in this file.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "requirements.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_requirements_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok requirements" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_files_this_lot_owns_exist():
    for rel in (
        "studio/src/adapters/requirements.ts",
        "studio/src/screens/project/Requirements.tsx",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"


def test_project_tsx_registers_the_tab_next_to_board_and_objectives():
    project_tsx = (_REPO / "studio" / "src" / "screens" / "Project.tsx").read_text(encoding="utf-8")
    assert "import { ProjectRequirements } from './project/Requirements';" in project_tsx
    assert "id: 'requisitos'" in project_tsx
    assert "<ProjectRequirements projectId={project.id} say={say} />" in project_tsx
    tabs_block = project_tsx.split("const TABS = [", 1)[1].split("] as const;", 1)[0]
    for entry in ("id: 'board'", "id: 'objetivos'", "id: 'requisitos'"):
        assert entry in tabs_block, f"{entry} must be in the same TABS array"


def test_no_inline_svg_and_no_direct_fetch_in_the_screen():
    """The two guards worth keeping here specifically, so a regression fails
    fast without needing node — the full sweep is tests/test_studio_guards.py."""
    screen = (_REPO / "studio" / "src" / "screens" / "project" / "Requirements.tsx").read_text(encoding="utf-8")
    assert "<svg" not in screen.lower() or "guard-ok:" in screen
    assert "fetch(" not in screen, "Requirements.tsx must call the adapter, not fetch() directly"


def test_adapter_never_lets_a_write_claim_by_model():
    """ADP-18's own limit: only a human accepts or rejects a requirement.
    The server enforces this independently (``requirements.model_cannot_decide``);
    this checks the UI's own adapter never gives a caller a way to send
    ``by: 'model'`` on the wire at all."""
    adapter = (_REPO / "studio" / "src" / "adapters" / "requirements.ts").read_text(encoding="utf-8")
    assert "by: 'human'" in adapter or 'by: "human"' in adapter
    assert "RequirementUpdateInput" in adapter
    # The public patch type must not itself carry a `by` field for a caller
    # to set — the adapter is the only place that ever writes `by`.
    update_input = adapter.split("export interface RequirementUpdateInput {", 1)[1].split("}", 1)[0]
    assert "by" not in update_input, "RequirementUpdateInput must not expose a `by` field to override"


def test_docs_mention_the_screen_is_wired():
    """docs/api/requirements.md and routes/requirements_routes.py agree on the
    one write route this lot added: the scoped remove-link DELETE. A route
    the doc does not know about, or a documented route with no code, both
    fail here."""
    api_doc = (_REPO / "docs" / "api" / "requirements.md").read_text(encoding="utf-8")
    routes = (_REPO / "routes" / "requirements_routes.py").read_text(encoding="utf-8")
    assert '@router.delete("/{key}/links/{link_id}")' in routes
    if "## Rutas" in api_doc:
        table = api_doc.split("## Rutas", 1)[1]
        assert "| DELETE | `/requirements/{key}/links/{link_id}`" in table
