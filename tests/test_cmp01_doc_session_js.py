"""tests/test_cmp01_doc_session_js.py — W2-A1 (CONTRATO_CMP_W2.md, CMP-01/02/03).

Shared document session (`studio/src/lib/docSession.ts`), consumed by the
side panel's `DocTab` (`screens/studio/SidePanel.tsx`) and the full editor
(`screens/documents/Editor.tsx`): identity/base revision/draft/selection/
undo-redo/pending proposals live outside either component's state so a
document survives the panel <-> full-editor <-> "another conversation"
navigation the CMP-01 fiche's decisive test describes.

`docSession.ts` is pure logic (no JSX) — checked by bundling it with
esbuild and exercising it directly (`doc_session.check.mjs`), the same
pattern `test_l99_studio_topology_js.py`/`test_studio_panel_js.py` use for
their own pure-logic modules. The React-heavy consumers (`SidePanel.tsx`,
`Editor.tsx`, `ReviewPane.tsx`) are checked here by source inspection —
same reasoning `test_l99_studio_topology_js.py` gives for `Activity.tsx`:
JSX wiring across a screen is not a bundled-import's worth of pure logic.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "doc_session.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_doc_session_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_files_this_lot_owns_exist():
    for rel in (
        "studio/src/lib/docSession.ts",
        "studio/src/screens/documents/ReviewPane.tsx",
        "docs/adaptations/decisions/CMP-01.md",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def test_side_panel_and_editor_share_the_session_not_local_state():
    """The whole point of CMP-01: `DocTab` and the full editor read/write
    the SAME `docSession`, not two independent `useState` draft copies —
    the module-singleton store is what makes a draft/selection/proposal
    survive switching from one to the other and back."""
    panel = _read("studio/src/screens/studio/SidePanel.tsx")
    editor = _read("studio/src/screens/documents/Editor.tsx")
    for src, name in ((panel, "SidePanel.tsx"), (editor, "Editor.tsx")):
        assert "useDocSession" in src, f"{name} does not read the shared docSession"
        assert "setDraftText" in src or "setText(" in src, f"{name} has no draft-writing path"

    # DocTab no longer keeps its own `useState` draft copy for the document
    # text — that state moved into docSession (panel.ts's PanelState.drafts
    # stays, but only FileTab uses it now for workspace files).
    assert "const [text, setText] = useState" not in panel, (
        "SidePanel.tsx still has a local text useState — the doc session unification did not land"
    )
    assert "const [text, setText] = useState" not in editor, (
        "Editor.tsx still has a local text useState — the doc session unification did not land"
    )


def test_apply_suggestion_never_applies_the_first_match_blindly():
    """CMP-02's exact bug: `next.includes(sg.find)` / `next.replace(sg.find,
    sg.replace)` — first-occurrence-only — must be gone from `DocTab`, and
    the occurrence-aware helpers must be wired in instead."""
    panel = _read("studio/src/screens/studio/SidePanel.tsx")
    assert "next.includes(sg.find)" not in panel, "the old first-match includes() check is still here"
    assert re_search_none(r"next\s*=\s*next\.replace\(sg\.find" ,panel), (
        "the old blind String.replace(first match) is still here"
    )
    assert "findOccurrences" in panel, "DocTab does not look up every occurrence before applying a suggestion"
    assert "OccurrencePicker" in panel, "no UI path for the ambiguous (>1 occurrence) case"


def re_search_none(pattern: str, text: str) -> bool:
    import re
    return re.search(pattern, text) is None


def test_comments_api_is_wired_not_reimplemented():
    """The anchored-comment backend (`src/document_comments.py`,
    `routes/document_comments_routes.py`) already exists from W1-E — this
    lot's job is a client for it, not a second implementation."""
    adapters = _read("studio/src/adapters/documents.ts")
    for fn in ("listDocComments", "createDocComment", "updateDocComment", "deleteDocComment", "acceptDocComment", "listDocLinks", "listDocBacklinks"):
        assert fn in adapters, f"adapters/documents.ts is missing {fn}"
    assert "/api/documents/" in adapters and "/comments" in adapters, "comment routes not called"

    review = _read("studio/src/screens/documents/ReviewPane.tsx")
    assert "listDocComments" in review and "acceptDocComment" in review, "ReviewPane does not use the comments adapter"
    assert "orphan" in review, "ReviewPane has no visible orphan ('lost anchor') state"
    assert "sendComposerContext" in review, "ReviewPane's 'send as a task' does not use the composer-context event"


def test_selection_reference_never_fakes_a_user_message():
    """CMP-03's explicit limit: a selection/comment handed to the composer
    is CONTEXT for the next message, never text injected as though the
    user had typed it, and never an executed edit."""
    ds = _read("studio/src/lib/docSession.ts")
    assert "COMPOSER_CONTEXT_EVENT" in ds and "sendComposerContext" in ds
    panel = _read("studio/src/screens/studio/SidePanel.tsx")
    assert "sendComposerContext" in panel, "the selection chip does not go through the documented composer-context event"
    # Composer.tsx itself is explicitly off-limits this wave (owned by
    # W2-F) — nothing here should try to import it.
    import re
    for src in (ds, panel):
        assert not re.search(r"import[^;\n]*from\s+['\"][^'\"]*/Composer['\"]", src), "Composer.tsx must not be imported this wave"


def test_never_rewrites_an_unchanged_document_on_a_mode_switch():
    """`isDirty()` gates every `save()` call in both surfaces — switching
    preview/edit, tabs, or routes must never itself trigger a write."""
    panel = _read("studio/src/screens/studio/SidePanel.tsx")
    editor = _read("studio/src/screens/documents/Editor.tsx")
    assert "disabled={!dirty" in panel, "DocTab's Save button is not gated on dirty"
    assert "disabled={!dirty" in editor, "Editor's Save button is not gated on dirty"
