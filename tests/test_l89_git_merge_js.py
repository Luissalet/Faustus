"""Lote 89 (OBJ-4) -- merge and branch deletion, Studio side.

The request-shape logic (`studio/src/adapters/git.ts`: `merge()`,
`mergeAbort()`, `deleteBranch()`, and how a 409 `git.merge_conflict`
surfaces as a `GitApiError`) is exercised with a mocked `fetch` by
`studio/checks/l89-git-merge.check.mjs`, run the same way
`tests/test_l83_git_panel_js.py` runs lote 83's own `.check.mjs`.

The screen wiring -- new files present and reusing the shared components,
the new adapter exports/types, the "Merge..." button and dialog in
`SourceControlPanel`, the branch popover's per-row merge/delete actions, and
`ChangesPane`'s "Abort merge" button -- is checked against the source files
directly, same as lote 83's own wiring tests.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "l89-git-merge.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def test_git_merge_check_js_passes():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all checks passed" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_git_adapter_gains_merge_and_delete_branch():
    src = _read("studio/src/adapters/git.ts")
    for symbol in ("merge", "mergeAbort", "deleteBranch"):
        assert f"export function {symbol}" in src, f"git.ts must export {symbol}()"
    for iface in ("GitMergeResult", "GitMergeConflictPayload", "GitDeleteBranchResult", "GitMergeFf"):
        assert iface in src, f"git.ts must declare {iface}"
    # DELETE .../branches/{name} -- the route name is `:path`-typed
    # server-side specifically so a branch like "feature/x" round-trips.
    assert "/branches/${encodeURIComponent(name)}" in src


def test_new_studio_files_present_and_reuse_shared_components():
    sc_dir = _REPO / "studio" / "src" / "screens" / "source-control"
    for name in ("MergeDialog.tsx", "DeleteBranchDialog.tsx"):
        assert (sc_dir / name).exists(), f"missing studio/src/screens/source-control/{name}"

    combined = "\n".join((sc_dir / n).read_text(encoding="utf-8") for n in ("MergeDialog.tsx", "DeleteBranchDialog.tsx"))
    for name in ("Dialog", "Button", "Toggle", "Select"):
        assert name in combined, f"lote 89 dialogs should reuse the shared {name} component"
    # A conflict never leaves the model/agent's own concern -- it must not
    # silently retry with force or anything destructive; the two explicit
    # human choices are the whole point.
    merge_src = (sc_dir / "MergeDialog.tsx").read_text(encoding="utf-8")
    assert "keep_conflicts" not in merge_src  # camelCase on the TS side, snake_case is the adapter's job
    assert "keepConflicts" in merge_src
    assert "git.merge_conflict" in merge_src

    css = (_REPO / "studio" / "src" / "screens" / "source-control.css").read_text(encoding="utf-8")
    hex_colors = re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not hex_colors, f"source-control.css must use --fs-* tokens, not literal colors: {hex_colors}"


def test_source_control_panel_gains_merge_button_and_dialog():
    src = _read("studio/src/screens/source-control/SourceControlPanel.tsx")
    assert "MergeDialog" in src
    assert "Merge…" in src
    assert "onAbortMerge" in src
    assert "mergeAbort" in src
    # The header's own branch fetch must also arm for the merge dialog, not
    # just "New branch" -- otherwise Merge… opens with an empty branch list.
    assert "newBranchOpen || mergeOpen" in src


def test_branch_popover_gains_merge_and_delete_actions():
    src = _read("studio/src/screens/source-control/BranchPopover.tsx")
    assert "MergeDialog" in src
    assert "DeleteBranchDialog" in src
    assert "GitMerge" in src and "Trash2" in src
    assert "setMergeTarget" in src and "setDeleteTarget" in src
    # Branch rows are no longer bare buttons -- the wrap adds room for the
    # trailing merge/delete icons without breaking the existing checkout
    # click target.
    assert "fs-sc__branch-row-wrap" in src


def test_changes_pane_gains_abort_merge_button():
    src = _read("studio/src/screens/source-control/ChangesPane.tsx")
    assert "onAbortMerge" in src
    assert "Abort merge" in src
    # The Merge Conflicts section is already first among CHANGES's sections
    # (contract: "arriba") -- guard that this lot didn't reorder it below
    # Staged/Changes/Untracked.
    conflicts_idx = src.index("Merge Conflicts")
    staged_idx = src.index("Staged Changes")
    assert conflicts_idx < staged_idx, "Merge Conflicts must stay the first section in CHANGES"


def test_git_routes_doc_mentions_merge_and_delete_branch():
    doc = _read("docs/api/git.md")
    assert "/merge" in doc
    assert "git.merge_conflict" in doc
    assert "git.branch_unmerged" in doc
    assert "git.branch_is_current" in doc
