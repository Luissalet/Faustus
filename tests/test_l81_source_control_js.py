"""Lote 81 (OBJ-4) — Studio Source control panel.

The screen's pure logic (`studio/src/adapters/git.ts`: commit-graph lane
assignment, ref-chip parsing, the Commit button's enabled rule, unified-diff
line typing, the branch popover's search filter) is exercised without a
browser by `studio/checks/l81-source-control.check.mjs`, run the same way
`tests/test_l70_a11_model_installed_chip_js.py` runs its own `.check.mjs`.

The screen wiring itself (route, nav entry, `Project.tsx` link, reused
components) is checked against the source files directly — no DOM needed to
confirm the pieces the contract asks for are actually there.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "l81-source-control.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_source_control_check_js_passes():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all checks passed" in proc.stdout
    assert "FAIL" not in proc.stdout


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def test_route_and_nav_entry_registered():
    routes_src = _read("studio/src/shell/routes.ts")
    assert "'/source-control'" in routes_src, "SERVER_ROUTES must list /source-control (deep-link whitelist)"
    assert "Source control" in routes_src

    shell_src = _read("studio/src/shell/AppShell.tsx")
    assert "SourceControlScreen" in shell_src
    assert '"/source-control"' in shell_src or "'/source-control'" in shell_src


def test_project_screen_links_to_source_control():
    project_src = _read("studio/src/screens/Project.tsx")
    assert "/source-control" in project_src, "Project.tsx must link to /source-control?project=<id>"


def test_screen_reuses_shared_components_not_literal_colors():
    screen_src = _read("studio/src/screens/SourceControl.tsx")
    sc_dir = _REPO / "studio" / "src" / "screens" / "source-control"
    combined = screen_src + "\n".join(p.read_text(encoding="utf-8") for p in sc_dir.glob("*.tsx"))
    for name in ("Dialog", "Popover", "EmptyState", "IconButton", "Button"):
        assert name in combined, f"source-control screen files should reuse the shared {name} component"
    assert "GitBranch" in screen_src

    import re
    css_files = [_REPO / "studio" / "src" / "screens" / "source-control.css"]
    css_files += sorted((_REPO / "studio" / "src" / "screens" / "source-control").glob("*.css"))
    for path in css_files:
        css = path.read_text(encoding="utf-8")
        hex_colors = re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
        assert not hex_colors, f"{path.name} must use --fs-* tokens, not literal colors: {hex_colors}"


def test_git_adapter_types_match_contract_shapes():
    src = _read("studio/src/adapters/git.ts")
    for symbol in (
        "listRepos", "getRepo", "getStatus", "getLog", "getBranches", "getCommit",
        "getCommitDiff", "getWorkingDiff", "checkout", "createBranch", "fetchRemote",
        "pull", "push", "sync", "stage", "unstage", "discard", "commit",
    ):
        assert f"export function {symbol}" in src, f"git.ts must export {symbol}()"
    assert "GitApiError" in src
