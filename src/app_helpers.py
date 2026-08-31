# src/app_helpers.py
import base64
import hashlib
import re
import time
import logging
import os

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from starlette.requests import Request

logger = logging.getLogger(__name__)

def read_if_exists(path: str) -> str:
    """Read file if it exists, return empty string otherwise."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

def file_to_data_url(path: str, mime: str) -> str:
    """Convert file to data URL."""
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"

def abs_join(base_dir: str, rel: str) -> str:
    """Join paths and return absolute path."""
    return os.path.abspath(os.path.join(base_dir, rel))

def serve_html_with_nonce(request: Request, file_path: str) -> HTMLResponse:
    """Read an app-bundled HTML page and inject the CSP nonce into inline <script> tags.

    Callers pass fixed, server-owned template paths (index/login/backgrounds),
    never a client-supplied path. So any read failure here — a missing file
    (broken deployment) or a permission/IO error — is a server fault, not a
    client "not found": map all of them to a logged 500 so a missing core
    template surfaces in 5xx alerting instead of hiding behind a 404. If a
    future caller serves a client-influenced path where 404 is correct, branch
    that at the call site rather than defaulting this shared helper to 404.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            html = f.read()
    except OSError:
        logger.exception("Failed to read page %s", file_path)
        raise HTTPException(500, "Internal server error")
    nonce = getattr(request.state, "csp_nonce", "")
    html = html.replace("{{CSP_NONCE}}", nonce)
    html = substitute_asset_versions(html, os.path.dirname(file_path))
    return HTMLResponse(html)


# ── content-hashed asset versions (FAUSTUS) ──────────────────────────────────
# `style.css?v=20260831projectsframing` was a hand-typed token, and a hand-typed
# token gets forgotten: CSS shipped, nobody saw it without Ctrl+Shift+R, and the
# bug looked like "the layout fix did not work". `{{ASSET_V:style.css}}` in the
# template becomes a hash of the file's current bytes, so the URL changes when —
# and only when — the file does.
#
# JS keeps its literal tokens on purpose: those same strings appear inside
# `import ... from './x.js?v=…'` in other modules, and a mismatch there loads the
# module twice under two URLs.

_ASSET_VERSION_RE = re.compile(r"\{\{ASSET_V:([A-Za-z0-9_./-]+)\}\}")
_asset_version_cache: dict = {}
# Fallback when a referenced asset cannot be read: changes once per process, so
# a broken path degrades to "busted on restart" rather than "cached forever".
_ASSET_VERSION_FALLBACK = f"boot{int(time.time())}"


def asset_version(path: str) -> str:
    """Short content hash of a file, cached until its mtime/size changes."""
    try:
        stat = os.stat(path)
        key = (path, stat.st_mtime_ns, stat.st_size)
        cached = _asset_version_cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
        with open(path, "rb") as fh:
            digest = hashlib.sha1(fh.read()).hexdigest()[:10]
        _asset_version_cache[path] = (key, digest)
        return digest
    except OSError:
        return _ASSET_VERSION_FALLBACK


def substitute_asset_versions(html: str, base_dir: str) -> str:
    """Replace {{ASSET_V:<relative path>}} with that file's content hash."""
    def _one(match):
        rel = match.group(1)
        if ".." in rel.split("/"):
            return _ASSET_VERSION_FALLBACK
        target = os.path.join(base_dir, *rel.split("/"))
        if not inside_base_dir(base_dir, target):
            return _ASSET_VERSION_FALLBACK
        return asset_version(target)

    return _ASSET_VERSION_RE.sub(_one, html)


def inside_base_dir(base_dir: str, path: str) -> bool:
    """Check if path is inside base directory."""
    if not isinstance(base_dir, str) or not isinstance(path, str):
        return False
    base = os.path.realpath(base_dir)
    p = os.path.realpath(path)
    try:
        return os.path.commonpath([base, p]) == base
    except Exception:
        return False
