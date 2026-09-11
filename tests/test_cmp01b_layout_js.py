"""tests/test_cmp01b_layout_js.py — W2-A2 (CMP-01, CONTRATO_CMP_W2.md).

Three arrangements of the same Studio work — conversation / document /
review — as a header switcher plus a pure CSS-grid rearrangement of the
existing stage/panel columns (studio/src/screens/Studio.tsx,
studio/src/screens/studio.css). No new state duplicates panel/turns; see
docs/adaptations/decisions/CMP-01-layout.md.

Studio.tsx is ~2700 lines of JSX wired to dozens of adapters — not pure
logic a bundled import can exercise in Node without a browser — so, like
tests/test_l99_studio_topology_js.py, this is source inspection
(studio/checks/studio_layout.check.mjs) rather than a DOM render.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "studio_layout.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_studio_layout_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok studio_layout" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_files_this_lot_owns_exist():
    for rel in (
        "studio/src/screens/Studio.tsx",
        "studio/src/screens/studio.css",
        "studio/src/screens/documents/ReviewPane.tsx",
        "docs/adaptations/decisions/CMP-01-layout.md",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"


def test_layout_is_a_closed_three_way_union():
    src = (_REPO / "studio" / "src" / "screens" / "Studio.tsx").read_text(encoding="utf-8")
    assert "type StudioLayout = 'conversation' | 'document' | 'review'" in src


def test_switching_layout_never_creates_a_session_document_or_draft():
    """The contract's decisive test for this lot: 'Cambiar de disposición
    NO crea otra conversación, documento ni borrador (test de que
    sessionId/doc.id no cambian y no se llama a save)'.

    setLayout is the one function the UI calls to change layout; its body
    is checked directly for anything that would create or persist state.
    """
    src = (_REPO / "studio" / "src" / "screens" / "Studio.tsx").read_text(encoding="utf-8")
    start = src.index("const setLayout = useCallback(")
    end = src.index("[panelDispatch],\n  );", start)
    body = src[start:end]
    for forbidden in (
        "createSession(", "createDoc(", "ensureSession(", "saveWorkspaceFile",
        ".save(", "setParams(", "navigate(", "uploadFiles(",
    ):
        assert forbidden not in body, f"setLayout must not call {forbidden}"
    # It IS allowed to open the panel — the same reducer action the header's
    # existing Source control chip already uses — but that touches only
    # local UI state (panelReducer), never sessionId or doc.id.
    assert "setLayoutState(next)" in body


def test_layout_is_not_duplicated_from_panel_or_turns_state():
    """'Sin duplicar estado: el layout es presentación pura sobre
    panel/turns existentes.' — layout stays a plain string, never a copy of
    the document or the conversation."""
    src = (_REPO / "studio" / "src" / "screens" / "Studio.tsx").read_text(encoding="utf-8")
    assert "useState<StudioLayout>(() => readLayoutFor(sessionId))" in src


def test_layout_persists_per_session_in_localstorage():
    src = (_REPO / "studio" / "src" / "screens" / "Studio.tsx").read_text(encoding="utf-8")
    assert "const LAYOUT_KEY_PREFIX = 'faustus_studio_layout:'" in src
    assert "layoutKeyFor(sessionId)" in src


def test_review_pane_is_lazy_and_named_export_only():
    src = (_REPO / "studio" / "src" / "screens" / "Studio.tsx").read_text(encoding="utf-8")
    assert "lazy(() => import('./documents/ReviewPane')" in src
    assert "m.ReviewPane" in src
    review_pane = (_REPO / "studio" / "src" / "screens" / "documents" / "ReviewPane.tsx").read_text(encoding="utf-8")
    # Studio.tsx's lazy import reads m.ReviewPane explicitly (see above), so
    # it works whether or not the file ALSO carries a default export — only
    # the named export is load-bearing here.
    assert "export function ReviewPane" in review_pane


def test_studio_css_uses_grid_tokens_not_a_second_layout_system():
    css = (_REPO / "studio" / "src" / "screens" / "studio.css").read_text(encoding="utf-8")
    assert "--fs-stage-col" in css
    assert "--fs-side-col" in css
    assert "grid-template-columns" in css
    # The panel's own drag-to-resize grip (studio.css's .fs-panel__grip,
    # SidePanel.tsx) must survive untouched — this lot swaps which grid
    # track --fs-panel-width feeds, it never removes the variable.
    assert "--fs-panel-width" in css


def test_the_shortcut_does_not_touch_settings_ts():
    """CONTRATO_CMP_W2.md: this lot owns only Studio.tsx/studio.css — the
    keybind table (adapters/settings.ts) is out of scope, so the Ctrl+Alt+L
    shortcut must not have been wired into DEFAULT_KEYBINDS there."""
    settings = (_REPO / "studio" / "src" / "adapters" / "settings.ts").read_text(encoding="utf-8")
    assert "toggle_layout" not in settings
    assert "ctrl+alt+l" not in settings.lower()


def test_i18n_rows_exist_for_this_lot():
    tsv = (_REPO / "docs" / "ui" / "i18n" / "es.tsv").read_text(encoding="utf-8")
    keys = {line.split("\t", 1)[0] for line in tsv.splitlines() if "\t" in line}
    for key in ("Layout", "Main conversation (Ctrl+Alt+L)", "Main document (Ctrl+Alt+L)", "Review (Ctrl+Alt+L)"):
        assert key in keys, f"missing i18n row for: {key}"
