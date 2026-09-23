"""tests/test_brain_studio_js.py — docs/ui build contract, "Lot D".

The Brain screen (a markdown-vault note app inside Faustus) is mostly JSX —
a three-pane layout, a canvas graph, an editor — but its two independent
pieces of pure logic (`lib/wikilinks.ts`'s wikilink parsing/rewriting and
quick-switcher fuzzy match, and `adapters/brain.ts`'s shape guards, which
have to tolerate the exact `/api/brain` contract Lot C builds separately)
are exercised directly, the same way `tests/test_studio_markdown_js.py`
exercises `lib/markdown.ts` and `tests/test_l99_studio_topology_js.py`
checks a mostly-JSX lot by source inspection. No network calls anywhere in
this file.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "brain.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_brain_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok brain" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_files_this_lot_owns_exist():
    for rel in (
        "studio/src/screens/Brain.tsx",
        "studio/src/screens/brain.css",
        "studio/src/screens/brain/Explorer.tsx",
        "studio/src/screens/brain/NoteView.tsx",
        "studio/src/screens/brain/NoteMarkdown.tsx",
        "studio/src/screens/brain/Properties.tsx",
        "studio/src/screens/brain/RightPanel.tsx",
        "studio/src/screens/brain/EntityPanel.tsx",
        "studio/src/screens/brain/GraphView.tsx",
        "studio/src/screens/brain/QuickSwitcher.tsx",
        "studio/src/screens/brain/SettingsDrawer.tsx",
        "studio/src/screens/brain/TrashPanel.tsx",
        "studio/src/screens/brain/SyncBar.tsx",
        "studio/src/adapters/brain.ts",
        "studio/src/lib/wikilinks.ts",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"


def test_adapters_brain_has_an_api_error_and_no_direct_fetch_in_screens():
    """Every read goes through `adapters/brain.ts`'s `ApiError`, and no
    screen component calls `fetch` on its own — same guard the topology and
    context lots already carry, kept here for this lot's own files."""
    brain_ts = (_REPO / "studio" / "src" / "adapters" / "brain.ts").read_text(encoding="utf-8")
    assert "ApiError" in brain_ts

    for rel in (
        "studio/src/screens/Brain.tsx",
        "studio/src/screens/brain/Explorer.tsx",
        "studio/src/screens/brain/NoteView.tsx",
        "studio/src/screens/brain/RightPanel.tsx",
        "studio/src/screens/brain/EntityPanel.tsx",
        "studio/src/screens/brain/GraphView.tsx",
        "studio/src/screens/brain/QuickSwitcher.tsx",
        "studio/src/screens/brain/SettingsDrawer.tsx",
        "studio/src/screens/brain/TrashPanel.tsx",
        "studio/src/screens/brain/SyncBar.tsx",
        "studio/src/screens/brain/Properties.tsx",
        "studio/src/screens/brain/NoteMarkdown.tsx",
    ):
        text = (_REPO / rel).read_text(encoding="utf-8")
        assert "fetch(" not in text, f"{rel} must call the adapter, not fetch() directly"


def test_no_forbidden_names_in_this_lots_own_files():
    """The build contract's hard rule: no other product/app/company name,
    anywhere in code, comments or CSS — checked here for the files this lot
    owns specifically (the repo-wide sweep is a separate, slower test)."""
    forbidden = ("obsidian", "notion", "roam research", "logseq", "evernote", "onenote")
    for rel in (
        "studio/src/screens/Brain.tsx",
        "studio/src/screens/brain.css",
        "studio/src/adapters/brain.ts",
        "studio/src/lib/wikilinks.ts",
    ):
        text = (_REPO / rel).read_text(encoding="utf-8").lower()
        for name in forbidden:
            assert name not in text, f"{rel} names a product ({name})"


def test_wikilinks_module_has_no_dom_or_network_dependency():
    """Pure logic only: this is what lets `brain.check.mjs` bundle and run
    it under plain node, no jsdom, no fetch mock."""
    text = (_REPO / "studio" / "src" / "lib" / "wikilinks.ts").read_text(encoding="utf-8")
    for banned in ("document.", "window.", "fetch(", "from 'react'", "useState(", "useEffect("):
        assert banned not in text, f"lib/wikilinks.ts should stay pure logic (found {banned!r})"


def test_routes_and_shell_registration():
    routes_ts = (_REPO / "studio" / "src" / "shell" / "routes.ts").read_text(encoding="utf-8")
    assert "'/brain'" in routes_ts, "routes.ts should list /brain in SERVER_ROUTES"
    assert "label: 'Brain'" in routes_ts, "routes.ts should carry a Brain tool entry"

    app_shell = (_REPO / "studio" / "src" / "shell" / "AppShell.tsx").read_text(encoding="utf-8")
    assert "screens/Brain" in app_shell, "AppShell.tsx should lazy-load the Brain screen"
    assert '<Route path="/brain" element={<BrainScreen />} />' in app_shell
    assert "pathname.startsWith('/brain')" in app_shell, "the Brain screen should get the wide-screen layout"


def test_i18n_es_has_the_brain_label():
    es_tsv = (_REPO / "docs" / "ui" / "i18n" / "es.tsv").read_text(encoding="utf-8")
    assert "\nBrain\tCerebro\n" in es_tsv or es_tsv.startswith("Brain\tCerebro\n"), (
        "docs/ui/i18n/es.tsv should translate the 'Brain' label to 'Cerebro'"
    )
