"""A36 — session operations (edit, retry, branch/export, share) mapped to
the real Faustus surface that covers them (or named an open gap).

Real code under test: the real `app` object (`from app import app`), the
same "real app, not a rebuilt one" shape `tests/test_l55_app_wiring.py`
uses — this proves the paths cited in
`docs/spec/paridad/SUPERFICIE_PRODUCTO.md` are actually wired on the
server a client talks to, not just present in some router module nobody
mounts.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.acceptance.conftest import record_evidence

REPO_ROOT = Path(__file__).resolve().parents[2]
SURFACE_DOC = REPO_ROOT / "docs" / "spec" / "paridad" / "SUPERFICIE_PRODUCTO.md"


def _walk_routes(routes):
    """Yield every real route, descending into fastapi.routing.
    _IncludedRouter's original_router.routes — app.routes wraps each
    include_router() call as one opaque _IncludedRouter object rather than
    flattening its paths in, in this FastAPI version."""
    for r in routes:
        if type(r).__name__ == "_IncludedRouter":
            yield from _walk_routes(r.original_router.routes)
        else:
            yield r


@pytest.fixture(scope="module")
def app_paths():
    from app import app  # real app.py import, same seam test_l55 uses

    return {r.path for r in _walk_routes(app.routes) if hasattr(r, "path")}


# Every path SUPERFICIE_PRODUCTO.md cites as covering an operation. Kept as
# an explicit list (not parsed from the doc) so a doc typo fails loudly
# here instead of silently matching nothing.
CITED_PATHS = [
    "/api/session/{session_id}/edit-message",
    "/api/chat/regenerate/{sid}",
    "/api/session/{session_id}/fork",
    "/api/session/{session_id}/side-threads",
    "/api/session/{sid}/export",
    "/api/sessions/export",
]


@pytest.mark.acceptance("A36")
@pytest.mark.parametrize("path", CITED_PATHS)
def test_cited_route_exists_on_the_real_app(request, app_paths, path):
    assert path in app_paths, (
        f"SUPERFICIE_PRODUCTO.md cites {path!r} as covering a session "
        f"operation, but it is not a route on the real app"
    )
    record_evidence(request, path=path)


@pytest.mark.acceptance("A36")
def test_surface_doc_names_every_cited_path_and_the_open_share_gap(request, app_paths):
    doc_text = SURFACE_DOC.read_text(encoding="utf-8")
    for path in CITED_PATHS:
        assert path in doc_text, f"{path} is checked here but not documented in {SURFACE_DOC.name}"

    # The "share" operation is an honest open gap: no route anywhere on the
    # real app looks like a public/shareable-link surface.
    share_like = [p for p in app_paths if "share" in p.lower() or "public" in p.lower()]
    assert share_like == [], (
        f"found route(s) that look like a share surface ({share_like}) — "
        f"SUPERFICIE_PRODUCTO.md's 'open gap' claim for Share is now stale "
        f"and must be updated to cite the real route"
    )
    assert "Open gap" in doc_text and "Share" in doc_text

    record_evidence(request, share_like_routes_found=share_like)
