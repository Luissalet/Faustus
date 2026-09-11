"""Lote 83 (OBJ-4, segunda tanda) — SSH identity chip/selector, "New
repository" dialog, branch-creation dialog and the agent's git policy
cards (global + per repo).

The pure logic (`studio/src/adapters/git.ts`: repo-name validation, the
https/ssh remote-URL rewrite onto an ssh host alias, the push-toggle's
disabled-without-commit rule, the identity chip's label) is exercised
without a browser by `studio/checks/l83-git-panel.check.mjs`, run the same
way `tests/test_l81_source_control_js.py` runs lote 81's own `.check.mjs`.

The screen wiring — new files present, the shared components reused, the
new adapter functions/types exported, the Settings "Repositories" section
registered, no literal colors in the new CSS — is checked against the
source files directly, same as lote 81's own wiring tests.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "l83-git-panel.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_git_panel_check_js_passes():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all checks passed" in proc.stdout
    assert "FAIL" not in proc.stdout


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def test_git_adapter_gains_identities_folders_create_and_policy():
    src = _read("studio/src/adapters/git.ts")
    for symbol in (
        "listIdentities", "createIdentity", "deleteIdentity", "probeIdentity",
        "getRepoIdentity", "setRepoIdentity", "listFolders", "createRepo",
        "getGlobalPolicy", "setGlobalPolicy", "getRepoPolicy", "setRepoPolicy",
        "isValidRepoName", "rewriteRemoteToAlias", "pushToggleDisabled", "identityChipLabel",
    ):
        assert f"export function {symbol}" in src, f"git.ts must export {symbol}()"
    for iface in ("GitIdentity", "GitFolder", "AgentGitPolicy", "RepoPolicyResponse", "RepoIdentity"):
        assert f"interface {iface}" in src, f"git.ts must declare {iface}"
    assert "identity?:" in src and "policy?:" in src, "GitRepo must gain identity/policy (additive, optional)"


def test_new_studio_files_present_and_reuse_shared_components():
    sc_dir = _REPO / "studio" / "src" / "screens" / "source-control"
    for name in (
        "NewRepositoryDialog.tsx", "IdentityChip.tsx", "CreateBranchDialog.tsx",
        "AgentPolicyFields.tsx", "RepoPolicyDialog.tsx", "agent-policy.css",
    ):
        assert (sc_dir / name).exists(), f"missing studio/src/screens/source-control/{name}"

    combined = "\n".join((sc_dir / n).read_text(encoding="utf-8") for n in (
        "NewRepositoryDialog.tsx", "IdentityChip.tsx", "CreateBranchDialog.tsx",
        "AgentPolicyFields.tsx", "RepoPolicyDialog.tsx",
    ))
    for name in ("Dialog", "Popover", "Toggle", "Button"):
        assert name in combined, f"lote 83 dialogs should reuse the shared {name} component"

    css_files = [sc_dir / "agent-policy.css"]
    css_files += [_REPO / "studio" / "src" / "screens" / "source-control.css"]
    for path in css_files:
        css = path.read_text(encoding="utf-8")
        hex_colors = re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
        assert not hex_colors, f"{path.name} must use --fs-* tokens, not literal colors: {hex_colors}"


def test_source_control_screen_wires_new_repo_identity_and_policy():
    src = _read("studio/src/screens/SourceControl.tsx")
    assert "NewRepositoryDialog" in src
    assert "IdentityChip" in src
    assert "RepoPolicyDialog" in src
    assert "New repository" in src


def test_branch_popover_uses_create_branch_dialog_not_inline_input():
    src = _read("studio/src/screens/source-control/BranchPopover.tsx")
    assert "CreateBranchDialog" in src, "branch creation must go through the dialog, not an inline text field"
    assert "branch-new-name" not in src, "the old inline creation input must be gone"


def test_settings_gains_repositories_section():
    src = _read("studio/src/screens/Settings.tsx")
    assert "'repositories'" in src
    assert "RepositoriesSection" in src
    assert "AgentPolicyFields" in src
    assert "getGlobalPolicy" in src and "setGlobalPolicy" in src


def test_transcript_renders_git_policy_chip():
    chat_src = _read("studio/src/adapters/chat.ts")
    assert "GitPolicyEvent" in chat_src
    assert "'git_policy'" in chat_src

    model_src = _read("studio/src/screens/studio/model.ts")
    assert "gitPolicy" in model_src

    transcript_src = _read("studio/src/screens/studio/Transcript.tsx")
    assert "turn.gitPolicy" in transcript_src
    assert "git-policy-chip" in transcript_src
