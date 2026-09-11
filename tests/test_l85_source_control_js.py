"""Lote 85 (OBJ-4, tercera tanda) — Studio: full-width layout (VS Code
style), repo dedupe, light polling and GitHub (`gh`) create/publish in
Source control.

The pure logic (`studio/src/adapters/git.ts`: `dedupeRepos`,
`repoProjectsLabel`, `mergeLightRepos`) is exercised without a browser by
`studio/checks/l85-source-control.check.mjs`, run the same way
`tests/test_l83_git_panel_js.py` runs lote 83's own `.check.mjs`.

The screen wiring — new files present, the GitHub adapter functions/types
exported, the screen widened and restructured, no literal colors in the
touched CSS — is checked against the source files directly, same as lote
83's own wiring tests.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "l85-source-control.check.mjs"
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


def test_git_adapter_gains_github_dedupe_and_light_polling():
    src = _read("studio/src/adapters/git.ts")
    for symbol in (
        "getGithubAccounts", "publishToGithub", "dedupeRepos", "repoProjectsLabel", "mergeLightRepos",
    ):
        assert f"export function {symbol}" in src, f"git.ts must export {symbol}()"
    for iface in ("GithubAccount", "GithubAccountsResponse", "GithubRepoResult", "GitRepoProjectRef"):
        assert f"interface {iface}" in src, f"git.ts must declare {iface}"
    assert "'gh'" in src, "GitIdentitySource must gain the 'gh' source"
    assert "projects?:" in src, "GitRepo must gain the optional projects list (additive)"
    assert "light?:" in src or "light: opts.light" in src, "listRepos must support the ?light=1 poll mode"


def test_new_studio_files_present():
    sc_dir = _REPO / "studio" / "src" / "screens" / "source-control"
    assert (sc_dir / "PublishToGithubDialog.tsx").exists(), "missing studio/src/screens/source-control/PublishToGithubDialog.tsx"


def test_source_control_screen_wires_full_width_and_github():
    src = _read("studio/src/screens/SourceControl.tsx")
    assert "PublishToGithubDialog" in src
    assert "CreateBranchDialog" in src
    assert "mergeLightRepos" in src, "the repos rail must merge its own light poll"
    assert "data-diff-open" in src, "the diff column must be collapsible, not always rendered"
    assert "repo-header-fetch" in src and "repo-header-pull" in src and "repo-header-push" in src and "repo-header-sync" in src, (
        "the center column's header needs its own Fetch/Pull/Push/Sync buttons"
    )
    assert "repo-header-new-branch" in src
    assert "repo-header-publish" in src


def test_layout_widens_without_touching_the_shared_shell():
    css = _read("studio/src/screens/source-control.css")
    assert ":has(" in css and ".fs-main__inner" in css, (
        "the screen must widen itself (no fs-screen--wide modifier exists yet) without a shell.css/AppShell.tsx change"
    )
    shell_css = _read("studio/src/styles/shell.css")
    assert "fs-sc" not in shell_css, "the shared shell.css must stay untouched by this lot"

    hex_colors = re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not hex_colors, f"source-control.css must use --fs-* tokens, not literal colors: {hex_colors}"

    dialog_css_combined = "\n".join((_REPO / "studio" / "src" / "screens" / "source-control" / n).read_text(encoding="utf-8")
                                     for n in ("PublishToGithubDialog.tsx",))
    assert "Dialog" in dialog_css_combined and "Button" in dialog_css_combined, "PublishToGithubDialog should reuse the shared Dialog/Button components"


def test_repo_list_shows_projects_and_ellipsis_name():
    src = _read("studio/src/screens/source-control/RepoList.tsx")
    assert "repoProjectsLabel" in src, "the repo subtitle must use repo.projects, not the bare project_name"
    assert "fs-sc__repo-name-text" in src, "the repo name needs its own single-line, ellipsized span"


def test_commit_graph_message_has_its_own_ellipsis_and_title():
    src = _read("studio/src/screens/source-control/CommitGraph.tsx")
    assert 'title={commit.message}' in src, "each commit message needs a title attribute with the full text"
    assert "fs-sc__graph-message-row" in src, "the message and its ref chips must not share one ellipsized flex box"


def test_identity_chip_distinguishes_gh_source():
    src = _read("studio/src/screens/source-control/IdentityChip.tsx")
    assert "Github" in src, "a gh-sourced identity needs a distinct icon in the identity list"
    assert "source === 'gh'" in src, "the gh source must be special-cased (label + ssh alias preview)"


def test_new_repository_dialog_has_github_section():
    src = _read("studio/src/screens/source-control/NewRepositoryDialog.tsx")
    assert "getGithubAccounts" in src
    assert "Create on GitHub too" in src
    assert "github:" in src, "submit must forward the github block to createRepo()"
