"""Lote 93 (OBJ-6) — Studio project board.

The board's pure logic (`studio/src/adapters/board.ts`: kanban column
grouping, the toolbar filter, ready ordering, allowed status transitions and
the issue-id chip regex/splitter) is exercised without a browser by
`studio/checks/l93-board.check.mjs`, run the same way
`tests/test_l81_source_control_js.py` runs its own `.check.mjs`.

The screen wiring itself (the Board tab, the chat panel's Board tab, the
issue-id chips in Transcript/CommitGraph) is checked against the source
files directly — no DOM needed to confirm the pieces the contract asks for
are actually there.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "l93-board.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def test_board_check_js_passes():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all checks passed" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_board_adapter_exports_contract_routes():
    src = _read("studio/src/adapters/board.ts")
    for symbol in (
        "listIssues", "listReady", "getSummary", "createIssue", "getIssue",
        "updateIssue", "claimIssue", "addComment", "addLink", "removeLink",
        "addRef", "deleteIssue", "importBoard", "setBoardKey", "exportMdUrl",
    ):
        assert f"export function {symbol}" in src, f"board.ts must export {symbol}()"
    assert "BoardApiError" in src
    assert "BOARD_REFRESH_EVENT" in src and "pingBoardRefresh" in src


def test_project_screen_has_board_tab():
    src = _read("studio/src/screens/Project.tsx")
    assert "'board'" in src or '"board"' in src, "Project.tsx must add a 'board' tab id"
    assert "Board" in src


def test_side_panel_board_tab_wired():
    panel_src = _read("studio/src/screens/studio/panel.ts")
    side_src = _read("studio/src/screens/studio/SidePanel.tsx")
    assert "'board'" in panel_src, "panel.ts's PanelTab must include 'board'"
    assert "pingBoardRefresh" in panel_src, "panel.ts must ping the board on turn-end, like it pings git"
    assert "board" in side_src.lower()


def test_transcript_and_commitgraph_link_issue_ids():
    transcript_src = _read("studio/src/screens/studio/Transcript.tsx")
    commitgraph_src = _read("studio/src/screens/source-control/CommitGraph.tsx")
    # Both go through the shared `renderIssueSegments` (board/IssueChips.tsx),
    # itself built on board.ts's pure `linkIssueIds` — see that file's own
    # doc comment for why the splitting logic lives in exactly one place.
    assert "renderIssueSegments" in transcript_src, "Transcript.tsx must render issue-id chips via board/IssueChips.tsx's renderIssueSegments"
    assert "renderIssueSegments" in commitgraph_src, "CommitGraph.tsx must render issue-id chips via board/IssueChips.tsx's renderIssueSegments"
    issue_chips_src = _read("studio/src/screens/board/IssueChips.tsx")
    assert "linkIssueIds" in issue_chips_src, "IssueChips.tsx must build on board.ts's pure linkIssueIds"


def test_board_screens_reuse_shared_components_not_literal_colors():
    board_dir = _REPO / "studio" / "src" / "screens" / "board"
    assert board_dir.is_dir(), "studio/src/screens/board/ must exist"
    tsx_files = sorted(board_dir.glob("*.tsx"))
    assert tsx_files, "studio/src/screens/board/ must contain component files"
    combined = "\n".join(p.read_text(encoding="utf-8") for p in tsx_files)
    for name in ("Dialog", "EmptyState", "IconButton", "Button"):
        assert name in combined, f"board screen files should reuse the shared {name} component"

    css_files = sorted(board_dir.glob("*.css")) + sorted(
        p for p in (_REPO / "studio" / "src" / "screens").glob("board*.css")
    )
    assert css_files, "the board tab needs at least one new .css file"
    for path in css_files:
        css = path.read_text(encoding="utf-8")
        hex_colors = re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
        assert not hex_colors, f"{path.name} must use --fs-* tokens, not literal colors: {hex_colors}"


def test_board_screens_use_t_for_new_strings():
    board_dir = _REPO / "studio" / "src" / "screens" / "board"
    for path in sorted(board_dir.glob("*.tsx")):
        src = path.read_text(encoding="utf-8")
        # A file with no t(...)/tn(...) call has no user-facing string to
        # translate in the first place (e.g. IssueChips.tsx, a structural
        # chip-splitting helper) — only files that actually call t()/tn()
        # must import it.
        if not re.search(r"\bt\(|\btn\(", src):
            continue
        assert "from '../../i18n'" in src or 'from "../../i18n"' in src, f"{path.name} must import t()/tn() from ../../i18n"
