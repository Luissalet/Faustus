"""Content Faustus did not write, and what the interface is allowed to do with it.

Six things reach the screen from outside: a model's markdown, an email's HTML,
a search result's URL, a screenshot a tool returned, a picture a note carries,
and a signature the user pasted from somewhere. Every one of them has been an
XSS in some app, and the shapes are always the same three:

  - a URL whose SCHEME is `javascript:` (React escapes an attribute's value,
    never its scheme, so this has to be checked by hand);
  - a data: URL that is not a raster — an SVG or an HTML document is a script
    with a picture's file extension;
  - HTML that survives one pass of a sanitiser and is dangerous on the second
    (`<scr<script>ipt>`), which is why the sanitiser runs to a fixpoint.

The behaviour is exercised in `studio/checks/untrusted.check.mjs`; this file
also pins that the guards are actually WIRED — a perfect sanitiser nobody
calls is the quietest way to ship the bug.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "untrusted.check.mjs"
_SRC = _REPO / "studio" / "src"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_the_guards_behave():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def test_every_external_link_goes_through_the_scheme_whitelist():
    """A citation, a search hit and a research source are all somebody else's
    URL. Each must be `safeExternal`-ed before it becomes an href."""
    for rel in (
        "screens/studio/Transcript.tsx",
        "screens/research/Research.tsx",
        "screens/compare/Compare.tsx",
        "screens/library/Research.tsx",
    ):
        src = _read(rel)
        assert "safeExternal" in src, f"{rel} renders external URLs without the whitelist"


def test_no_source_url_is_used_raw_as_an_href():
    """The regression is one careless `href={s.url}` slipping back in."""
    for rel in (
        "screens/studio/Transcript.tsx",
        "screens/research/Research.tsx",
        "screens/compare/Compare.tsx",
        "screens/library/Research.tsx",
    ):
        for line in _read(rel).splitlines():
            if "href={" not in line:
                continue
            # A checked href is either the helper inline, a variable the helper
            # produced (`href`), or one of our own same-origin builders.
            if any(ok in line for ok in ("safeExternal", "href={href}", "attachmentUrl", "reportUrl")):
                continue
            assert ".url}" not in line, f"{rel}: raw external URL in an href:\n  {line.strip()}"


def test_a_tool_screenshot_must_pass_the_raster_whitelist():
    """An SVG data URL is a script. The frame comes off the wire, so it is
    checked where it is parsed, not where it is drawn."""
    chat = _read("adapters/chat.ts")
    assert "safeFrameSrc" in chat
    assert "data:image\\/(?:png|jpe?g|gif|webp)" in chat, "the whitelist must name the raster types"
    # Every screenshot that reaches the panel went through it.
    assert chat.count("safeFrameSrc(") >= 3


def test_a_notes_picture_must_pass_the_same_kind_of_whitelist():
    paint = _read("lib/paint.ts")
    assert "export function safeImage" in paint
    assert "data:image/" in paint


def test_email_html_is_sanitised_to_a_fixpoint():
    """`<scr<script>ipt>` survives one pass and is a script on the second."""
    mail = _read("lib/mail.ts")
    assert "sanitizeOnce" in mail and "export function sanitizeMailHtml" in mail
    body = mail[mail.index("export function sanitizeMailHtml"):]
    assert "while" in body or "for (" in body, "the sanitiser must run to a fixpoint, not once"


def test_a_signature_is_a_raster_or_it_is_nothing():
    account = _read("adapters/account.ts")
    assert "safeDataImage" in account
