# tests/test_csp_no_external_hosts.py
"""Agora #204: the browser UI is fully self-hosted.

The default page CSP must not allow any external script/style/font/connect
host, and nothing the app serves to the browser (static/, studio sources and
the built studio bundle) may reference a public CDN.
"""
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import SecurityHeadersMiddleware

ROOT = Path(__file__).resolve().parent.parent

CDN_HOSTS = re.compile(
    r"(cdn\.jsdelivr\.net|unpkg\.com|cdnjs\.cloudflare\.com|fonts\.googleapis\.com|"
    r"fonts\.gstatic\.com|ajax\.googleapis\.com|code\.jquery\.com|esm\.sh|"
    r"cdn\.skypack\.dev|cdn\.tailwindcss\.com|stackpath\.bootstrapcdn\.com)",
    re.IGNORECASE,
)
EXTERNAL_TAG = re.compile(
    r"<(?:script|link)\b[^>]*?\b(?:src|href)\s*=\s*[\"']?(?:https?:)?//(?!www\.w3\.org)",
    re.IGNORECASE,
)
SCAN_SUFFIXES = {".html", ".htm", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".css", ".json"}


def _default_csp() -> str:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/")
    def root():
        return {"ok": True}

    return TestClient(app).get("/").headers["content-security-policy"]


def _directives(csp: str) -> dict:
    out = {}
    for part in csp.split(";"):
        bits = part.strip().split()
        if bits:
            out[bits[0]] = bits[1:]
    return out


def test_default_csp_has_no_external_hosts():
    d = _directives(_default_csp())
    for name in ("default-src", "script-src", "style-src", "font-src", "connect-src",
                 "frame-src", "media-src", "object-src"):
        for src in d.get(name, []):
            assert not re.match(r"^(https?:|//|\*|[\w.-]+\.[a-z]{2,}$)", src), (name, src)
    assert "https://cdn.jsdelivr.net" not in _default_csp()


def test_default_csp_locks_object_and_base():
    d = _directives(_default_csp())
    assert d["object-src"] == ["'none'"]
    assert d["base-uri"] == ["'self'"]
    assert d["frame-ancestors"] == ["'none'"]
    assert "'unsafe-eval'" not in d["script-src"]
    assert "'unsafe-inline'" not in d["script-src"]


def _served_files():
    roots = [ROOT / "static", ROOT / "studio" / "src", ROOT / "studio" / "index.html",
             ROOT / "templates"]
    for r in roots:
        if r.is_file():
            yield r
        elif r.is_dir():
            for p in r.rglob("*"):
                if p.is_file() and p.suffix.lower() in SCAN_SUFFIXES:
                    yield p


def test_served_frontend_never_references_a_cdn():
    offenders = []
    for p in _served_files():
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if CDN_HOSTS.search(text) or EXTERNAL_TAG.search(text):
            offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, f"external CDN references in served front-end: {offenders}"
