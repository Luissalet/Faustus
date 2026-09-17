"""routes/pwa_routes.py — manifest and service worker at the origin root
(lot P-B).

Both are served *unauthenticated* at the origin root, not under `/static`:

- A service worker's own scope is the path it is served from (and
  everything below it), never a path it merely fetches from
  (`Service-Worker-Allowed` widens that, but the browser still requires
  the registering script itself to come from within the scope it claims).
  `static/sw.js` needs `scope: '/'` (the whole app, every route the shell
  renders), and a worker served from `/static/sw.js` could only ever claim
  `/static/` without that header — which is why this module re-serves the
  same file's bytes at `/sw.js` instead of just linking the `/static/`
  copy.
- `/manifest.webmanifest` is the conventional extension browsers and
  install prompts increasingly expect; `.json` still works everywhere but
  some Android/Chrome install heuristics look for the dedicated mimetype.

Both must be reachable before a session cookie exists — the install
prompt and the very first service-worker registration happen on the
public shell — so `app.py` lists both paths in `AUTH_EXEMPT_EXACT`
alongside `/login`.
"""
from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import FileResponse

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
SW_PATH = os.path.join(STATIC_DIR, "sw.js")
MANIFEST_PATH = os.path.join(STATIC_DIR, "manifest.json")


def setup_pwa_routes() -> APIRouter:
    router = APIRouter(tags=["pwa"])

    @router.get("/sw.js")
    def service_worker():
        return FileResponse(
            SW_PATH,
            media_type="application/javascript",
            headers={
                # Lets the worker registered from /sw.js control the whole
                # origin (every Studio route), not just the /sw.js path
                # itself — the one thing a plain /static/sw.js serve could
                # not grant.
                "Service-Worker-Allowed": "/",
                # A stale cached worker never sees push/notificationclick
                # fixes; the browser already re-checks workers periodically,
                # but this keeps a manual reload honest too.
                "Cache-Control": "no-cache",
            },
        )

    @router.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(
            MANIFEST_PATH,
            media_type="application/manifest+json",
            headers={"Cache-Control": "no-cache"},
        )

    return router
