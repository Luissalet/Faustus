"""The Faustus mark lives inline in two places and there is no build step to
keep them honest, so pin them here.

The mark (arrowhead plus two wings) replaced the inherited "boat" glyph. It is
duplicated because each site needs a different escaping: a URL-encoded data:
URI in the page's `<link rel="icon">`, and JSX in the shell's `BrandMark`.

The regression this guards: swapping the artwork with a regex ate the double
quotes around a string literal, which is a syntax error that only shows up as
a blank page at runtime — every python test still passed.
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
_MARK = (_REPO / "studio" / "src" / "shell" / "BrandMark.tsx").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None

# First vertices of the body path — the apex, then the bottom-left corner.
_BODY_HEAD = "M16 0.738L4.674 25.559"


def _sites():
    m = re.search(r'<link rel="icon" type="image/svg\+xml" href="data:image/svg\+xml,([^"]+)"', _INDEX)
    yield "link rel=icon", unquote(m.group(1)) if m else None

    m = re.search(r"(<svg viewBox=\"0 0 32 32\".*?</svg>)", _MARK, re.S)
    svg = m.group(1) if m else None
    if svg:
        # JSX: braces and quoted prose in attributes are not XML. Only the
        # paths matter here, so rebuild the wrapper around them.
        paths = re.findall(r"<path\b[^>]*/>|<path\b[^>]*>", svg)
        body = re.search(r'd="(M16[^"]+)"', svg)
        wings = re.findall(r'<path d="(M\d[^"]+)" />', svg)
        if body and len(wings) == 2:
            svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                f'<path fill="#e06c75" d="{body.group(1)}"/>'
                f'<path fill="#e06c75" d="{wings[0]}"/>'
                f'<path fill="#e06c75" d="{wings[1]}"/>'
                '</svg>'
            )
        else:
            svg = None if not paths else svg
    yield "BrandMark", svg


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
    assert "M16 4L16 22L6 22Z" not in _MARK


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
@pytest.mark.parametrize("i", range(len(re.findall(r'<script nonce="\{\{CSP_NONCE\}\}">', _INDEX))))
def test_inline_scripts_still_parse(tmp_path, i):
    """The page's inline scripts run before anything else. A syntax error
    there is a blank screen, and nothing else in the suite would catch it."""
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


def test_the_page_points_at_the_assets_it_ships():
    """A manifest or an apple-touch-icon naming a file that is not there is a
    404 on every load, and nobody sees it except the log."""
    for rel in re.findall(r'href="(/static/[^"]+)"', _INDEX):
        path = _REPO / rel.lstrip("/")
        assert path.exists(), f"index.html links {rel}, which is not there"
