"""The export UI: what it offers, and what it does when the server says no.

Half of this runs `static/js/chatExport.js` under node (the module is pure
enough to drive with stubbed browser globals), the other half pins the wiring
in the three callers by reading their source — the browser-heavy parts
(dropdowns, project hub) can't be executed here, but their contract can be.

The behaviour under test is the one the old UI got wrong: `window.open` on the
export URL cannot see the response, so a 400 (unknown format / batch too big)
or a 503 (PDF dependency missing) surfaced as a blank tab or a page of raw
JSON. Every path must now go through fetch + Blob and hand the server's own
message to the notifier that file already uses.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_JS = _REPO / "static" / "js"
_EXPORT_JS = _JS / "chatExport.js"
_SESSIONS_JS = _JS / "sessions.js"
_SLASH_JS = _JS / "slashCommands.js"
_PROJECTS_JS = _JS / "projects.js"

_MODULE_URI = _EXPORT_JS.as_uri()

FORMATS = ("md", "txt", "json", "html", "pdf", "docx")


# ---------------------------------------------------------------------------
# node harness
# ---------------------------------------------------------------------------

_PRELUDE = """
const revoked = [];
const clicks = [];
const fetched = [];

globalThis.window = { location: { origin: 'http://example.test' } };
globalThis.document = {
  body: { appendChild() {} },
  querySelectorAll: () => [],
  createElement: () => ({
    style: {}, remove() {},
    click() { clicks.push({ href: this.href, download: this.download }); },
  }),
};
URL.createObjectURL = () => 'blob:fake';
URL.revokeObjectURL = (u) => revoked.push(u);
// Run the deferred revoke immediately so the test can assert it happened at
// all; the production delay only exists to keep the download alive.
globalThis.setTimeout = (fn) => { fn(); return 0; };
"""


def _node(body: str):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    script = _PRELUDE + f"const mod = await import({json.dumps(_MODULE_URI)});\n" + body
    res = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True,
        text=True, encoding="utf-8", cwd=_REPO, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def _stub_fetch(status=200, detail=None, disposition="", body="data"):
    return f"""
globalThis.fetch = async (url) => {{
  fetched.push(url);
  return {{
    ok: {json.dumps(200 <= status < 300)},
    status: {status},
    headers: {{ get: (k) => (k.toLowerCase() === 'content-disposition' ? {json.dumps(disposition)} : null) }},
    json: async () => ({json.dumps({'detail': detail} if detail is not None else {})}),
    blob: async () => ({json.dumps(body)}),
  }};
}};
"""


# ---------------------------------------------------------------------------
# The six formats
# ---------------------------------------------------------------------------

def test_the_picker_offers_all_six_formats_with_pdf_and_docx_visible():
    out = _node("console.log(JSON.stringify({"
                " ids: mod.EXPORT_FORMAT_IDS,"
                " labels: mod.EXPORT_FORMATS.map(f => f.label) }));")
    assert out["ids"] == list(FORMATS)
    joined = " ".join(out["labels"]).lower()
    assert "pdf" in joined and "docx" in joined


def test_an_unknown_format_never_reaches_the_server_as_is():
    out = _node(_stub_fetch(disposition='attachment; filename="a.md"') + """
      await mod.exportSession('s1', 'epub');
      console.log(JSON.stringify({ fetched }));
    """)
    assert "fmt=md" in out["fetched"][0]


# ---------------------------------------------------------------------------
# URL construction must match the route's parameter names
# ---------------------------------------------------------------------------

def test_single_export_url_keeps_the_existing_parameter_names():
    out = _node(_stub_fetch(disposition='attachment; filename="a.pdf"') + """
      await mod.exportSession('sid-1', 'pdf', { filename: 'my file.pdf' });
      console.log(JSON.stringify({ fetched }));
    """)
    url = out["fetched"][0]
    assert "/api/session/sid-1/export?" in url
    assert "fmt=pdf" in url
    assert "filename=my+file.pdf" in url or "filename=my%20file.pdf" in url


def test_batch_export_url_supports_project_folder_and_ids():
    out = _node(_stub_fetch(disposition='attachment; filename="x.zip"') + """
      await mod.exportSessionsZip({ project: 'p1' }, 'docx');
      await mod.exportSessionsZip({ folder: 'Work' }, 'md');
      await mod.exportSessionsZip({ ids: ['a', 'b', 'c'] }, 'pdf');
      console.log(JSON.stringify({ fetched }));
    """)
    a, b, c = out["fetched"]
    assert "/api/sessions/export?" in a and "fmt=docx" in a and "project=p1" in a
    assert "folder=Work" in b
    assert "ids=a%2Cb%2Cc" in c


# ---------------------------------------------------------------------------
# Errors reach the user
# ---------------------------------------------------------------------------

def test_a_503_shows_the_servers_own_message_and_downloads_nothing():
    msg = "PDF export needs the 'reportlab' package: pip install reportlab"
    out = _node(_stub_fetch(status=503, detail=msg) + f"""
      const errors = [];
      const ok = await mod.exportSession('s1', 'pdf', {{ onError: (m) => errors.push(m) }});
      console.log(JSON.stringify({{ ok, errors, clicks }}));
    """)
    assert out["ok"] is False
    assert out["errors"] == [msg]
    assert out["clicks"] == []


def test_a_400_shows_the_servers_own_message():
    msg = "This selection has 500 conversations; a single export is capped at 100."
    out = _node(_stub_fetch(status=400, detail=msg) + """
      const errors = [];
      await mod.exportSessionsZip({ folder: 'Work' }, 'pdf', { onError: (m) => errors.push(m) });
      console.log(JSON.stringify({ errors }));
    """)
    assert out["errors"] == [msg]


def test_a_network_failure_is_reported_rather_than_swallowed():
    out = _node("""
      globalThis.fetch = async () => { throw new Error('offline'); };
      const errors = [];
      const ok = await mod.exportSession('s1', 'md', { onError: (m) => errors.push(m) });
      console.log(JSON.stringify({ ok, errors }));
    """)
    assert out["ok"] is False
    assert len(out["errors"]) == 1 and out["errors"][0]


def test_an_error_body_that_is_not_json_still_produces_a_message():
    out = _node("""
      globalThis.fetch = async () => ({ ok: false, status: 500,
        json: async () => { throw new Error('not json'); } });
      const errors = [];
      await mod.exportSession('s1', 'md', { onError: (m) => errors.push(m) });
      console.log(JSON.stringify({ errors }));
    """)
    assert out["errors"] and "500" in out["errors"][0]


# ---------------------------------------------------------------------------
# Download mechanics
# ---------------------------------------------------------------------------

def test_download_uses_the_utf8_filename_and_revokes_the_object_url():
    disp = ("attachment; filename=\"Informe_2026___a_o.md\"; "
            "filename*=UTF-8''Informe%202026%20%E2%80%94%20a%C3%B1o.md")
    out = _node(_stub_fetch(disposition=disp) + """
      const done = [];
      const ok = await mod.exportSession('s1', 'md', { onDone: (n) => done.push(n) });
      console.log(JSON.stringify({ ok, done, clicks, revoked }));
    """)
    assert out["ok"] is True
    assert out["done"] == ["Informe 2026 — año.md"]
    assert out["clicks"] == [{"href": "blob:fake", "download": "Informe 2026 — año.md"}]
    # A blob URL pins the whole payload until revoked — a 200 MB zip must not
    # be leaked for the life of the tab.
    assert out["revoked"] == ["blob:fake"]


def test_filename_parsing_prefers_rfc5987_then_falls_back():
    out = _node("""
      const f = mod.filenameFromDisposition;
      console.log(JSON.stringify({
        star: f("attachment; filename=\\"a_b.md\\"; filename*=UTF-8''a%20b.md"),
        quoted: f('attachment; filename="plain name.md"'),
        bare: f('attachment; filename=plain.md'),
        none: f('attachment', 'fallback.zip'),
        empty: f(null, 'fallback.zip'),
        broken: f("attachment; filename=\\"ok.md\\"; filename*=UTF-8''%E0%A4%A"),
      }));
    """)
    assert out["star"] == "a b.md"
    assert out["quoted"] == "plain name.md"
    assert out["bare"] == "plain.md"
    assert out["none"] == "fallback.zip"
    assert out["empty"] == "fallback.zip"
    # A malformed percent-escape must not throw; the ASCII half still works.
    assert out["broken"] == "ok.md"


# ---------------------------------------------------------------------------
# Wiring in the three callers (source-level)
# ---------------------------------------------------------------------------

def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    i = src.index(start_marker)
    return src[i:src.index(end_marker, i + len(start_marker))]


def test_slash_command_no_longer_opens_a_tab_it_cannot_read():
    src = _src(_SLASH_JS)
    body = _slice(src, "async function _cmdSessionExport(", "async function _cmdSessionExportAll(")
    assert "window.open" not in body
    assert "exportSession(" in body
    assert "onError:" in body and "slashReply" in body
    # It must not silently downgrade an unrecognised format to markdown.
    assert "Unknown export format" in body
    assert "EXPORT_FORMAT_IDS" in body
    assert "from './chatExport.js'" in src


def test_slash_command_gained_a_folder_zip_command():
    src = _src(_SLASH_JS)
    body = _slice(src, "async function _cmdSessionExportAll(", "\n// ")
    assert "exportSessionsZip(" in body
    assert "onError:" in body
    assert "'export-all'" in src and "_cmdSessionExportAll" in src


def test_no_export_url_is_opened_with_window_open_anywhere():
    for path in (_SLASH_JS, _SESSIONS_JS, _PROJECTS_JS, _EXPORT_JS):
        src = _src(path)
        for line in src.splitlines():
            if "window.open" in line:
                assert "/export" not in line, f"{path.name}: {line.strip()}"


def test_sidebar_chat_menu_offers_export_and_reports_failures():
    src = _src(_SESSIONS_JS)
    assert "from './chatExport.js'" in src
    body = _slice(src, "const exportItem = document.createElement('div');",
                  "// Copy & Move to folder")
    assert "openExportFormatMenu(" in body
    assert "exportSession(" in body
    # The dropdown item is actually attached to the menu.
    assert "dropdown.appendChild(exportItem);" in src
    # Failures go through the notifier this file already uses.
    run_export = _slice(src, "async function _runExport(", "\n}")
    assert "uiModule.showError" in run_export


def test_folder_header_exports_the_whole_folder_as_a_zip():
    src = _src(_SESSIONS_JS)
    body = _slice(src, "const exportFolderBtn = document.createElement('button');",
                  "// Delete folder button")
    assert "exportSessionsZip({ folder: folderName }" in body
    assert "openExportFormatMenu(" in body
    assert "header.appendChild(exportFolderBtn);" in src
    # The header's own click/dblclick handlers must ignore this button, or
    # exporting would also collapse or rename the folder.
    assert "folder-delete-btn folder-export-btn" in src
    assert "e.target.closest('.folder-delete-btn')" in src


def test_bulk_selection_can_be_exported_by_ids():
    src = _src(_SESSIONS_JS)
    body = _slice(src, "exportBtn.id = 'session-bulk-export';", "archiveBtn0.parentNode.insertBefore")
    assert "exportSessionsZip({ ids }" in body
    assert "openExportFormatMenu(" in body
    # Disabled while nothing is selected, like its sibling bulk buttons.
    assert "session-bulk-export" in _slice(src, "function _updateBulkCount(", "\n}")


def test_project_hub_can_export_the_whole_project():
    src = _src(_PROJECTS_JS)
    assert "from './chatExport.js'" in src
    assert 'id="project-export"' in src
    assert "$('project-export')?.addEventListener('click'" in src
    body = _slice(src, "function exportProject(project, anchorEl)", "async function setProjectFlags(")
    assert "exportSessionsZip({ project: project.id }" in body
    assert "openExportFormatMenu(" in body
    assert "uiModule.showError" in body


def test_every_touched_module_parses():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    for path in (_EXPORT_JS, _SESSIONS_JS, _SLASH_JS, _PROJECTS_JS):
        res = subprocess.run(["node", "--check", str(path)], capture_output=True,
                             text=True, encoding="utf-8", timeout=60)
        assert res.returncode == 0, f"{path.name}: {res.stderr}"
