"""The Faustus mark lives inline in four places and there is no build step to
keep them honest, so pin them here.

The mark (arrowhead + knocked-out speech bubble + two wings) replaced the
inherited "boat" glyph. It is duplicated because each site needs a different
escaping: a URL-encoded data: URI in the <link>, a JS string concat in the boot
script, a template literal in theme.js, and plain HTML on the welcome screen.

Regression this guards: swapping the artwork with a regex ate the double quotes
around the boot script's string literal, which is a syntax error that only shows
up as a blank page at runtime — every python test still passed.
"""
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

import pytest

_REPO = Path(__file__).resolve().parent.parent
_INDEX = (_REPO / "static" / "index.html").read_text(encoding="utf-8")
_THEME = (_REPO / "static" / "js" / "theme.js").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None

# first vertices of the body path — apex, then the bottom-left corner
_BODY_HEAD = "M16 0.738L4.674 25.559"


def _sites():
    m = re.search(r'<link rel="icon" type="image/svg\+xml" href="data:image/svg\+xml,([^"]+)"', _INDEX)
    yield "link rel=icon", unquote(m.group(1)) if m else None

    m = re.search(r'encodeURIComponent\("(<svg.*?</svg>)"\);', _INDEX, re.S)
    yield "boot script", m.group(1).replace('" + ac + "', "#e06c75") if m else None

    # the else-branch of _updateFavicon; the first literal is the route-icon one
    lits = re.findall(r"svg = `(<svg.*?</svg>)`", _THEME, re.S)
    yield "_updateFavicon", lits[1].replace("${fg}", "#e06c75") if len(lits) > 1 else None

    m = re.search(r'(<svg class="welcome-boat".*?</svg>)', _INDEX, re.S)
    yield "welcome screen", m.group(1) if m else None


@pytest.mark.parametrize("label,svg", list(_sites()))
def test_every_site_carries_the_same_well_formed_mark(label, svg):
    assert svg, f"{label}: mark not found"
    root = ET.fromstring(svg)                      # raises on malformed SVG
    assert root.get("viewBox") == "0 0 32 32", label
    paths = [el.get("d") for el in root.iter() if el.tag.endswith("path")]
    assert len(paths) == 3, f"{label}: expected body + 2 wings, got {len(paths)}"
    assert paths[0].startswith(_BODY_HEAD), f"{label}: body path drifted"


def test_the_old_boat_glyph_is_gone():
    assert "M16 4L16 22L6 22Z" not in _INDEX
    assert "M16 4L16 22L6 22Z" not in _THEME


def test_route_icon_registries_stay_in_sync():
    """index.html ships a copy so a bookmarked route gets its icon before the
    module loads; theme.js owns the copy used after a theme change."""
    inline = _INDEX[_INDEX.find("var SHAPES"):_INDEX.find("var inner")]
    module = _THEME[_THEME.find("_ROUTE_FAVICON_SHAPES"):_THEME.find("function _updateFavicon")]
    assert set(re.findall(r"'(/[a-z]+)':", inline)) == set(re.findall(r"'(/[a-z]+)':", module))


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
@pytest.mark.parametrize("i", range(len(re.findall(r'<script nonce="\{\{CSP_NONCE\}\}">', _INDEX))))
def test_inline_scripts_still_parse(tmp_path, i):
    block = re.findall(r'<script nonce="\{\{CSP_NONCE\}\}">(.*?)</script>', _INDEX, re.S)[i]
    f = tmp_path / f"block_{i}.js"
    f.write_text(block, encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True,
                          encoding="utf-8", timeout=30)
    assert proc.returncode == 0, proc.stderr


def test_the_brand_assets_exist():
    for rel in ("static/icons/icon-192.png", "static/icons/icon-512.png",
                "static/icons/icon-maskable-512.png", "static/icons/faustus-mark.svg",
                "static/icon.ico", "static/favicon.ico", "static/favicon.png"):
        p = _REPO / rel
        assert p.exists() and p.stat().st_size > 0, rel


def test_notification_icons_resolve():
    """notes.js / tasks.js / settings.js / reminders.js point Notification at
    these paths; they were 404ing before the mark landed."""
    referenced = set()
    for js in (_REPO / "static" / "js").rglob("*.js"):
        referenced |= set(re.findall(r"'/static/(favicon\.(?:ico|png))'", js.read_text(encoding="utf-8")))
    assert referenced, "expected the notification icon references to still exist"
    for name in referenced:
        assert (_REPO / "static" / name).exists(), name
