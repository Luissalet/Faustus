"""Spotify API — /api/spotify/* (src/spotify.py).

  GET  /api/spotify/status     configured? logged in? the Redirect URI to register
  GET  /api/spotify/login      302 to Spotify's consent page (PKCE, the owner's own app)
  GET  /api/spotify/callback   Spotify comes back here; exchanges the code and says so
  POST /api/spotify/logout     forget the tokens
  POST /api/spotify/call       {"tool": "spotify_search", "args": {...}} -> the tool's answer

The callback is reachable without a session (app.py AUTH_EXEMPT_EXACT): its
single-use `state` comes from a login the owner started, and the code is only
exchanged together with that login's PKCE verifier.
"""
from __future__ import annotations

import asyncio
import html
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.middleware import require_admin
from src import spotify


def _page(title: str, text: str, status: int = 200) -> HTMLResponse:
    body = (f"<!doctype html><html lang=\"es\"><meta charset=\"utf-8\"><title>{html.escape(title)}</title>"
            f"<body style=\"font-family:system-ui;margin:3rem;max-width:40rem\"><h1>{html.escape(title)}</h1>"
            f"<p>{html.escape(text)}</p></body></html>")
    return HTMLResponse(body, status_code=status)


def setup_spotify_routes() -> APIRouter:
    router = APIRouter(prefix="/api/spotify", tags=["spotify"])

    @router.get("/status")
    async def get_status(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return spotify.status()

    @router.get("/login")
    async def login(request: Request):
        require_admin(request)
        try:
            return RedirectResponse(spotify.begin_login(), status_code=302)
        except spotify.SpotifyError as exc:
            return _page("Spotify sin configurar", str(exc), 400)

    @router.get("/callback")
    async def callback(code: str = "", state: str = "", error: str = ""):
        if error:
            return _page("Spotify no autorizado", f"Spotify devolvió: {error}", 400)
        try:
            await asyncio.to_thread(spotify.finish_login, code, state)
        except spotify.SpotifyError as exc:
            return _page("No se pudo conectar Spotify", str(exc), 400)
        return _page("Spotify conectado", "Faustus ya puede usar tu cuenta de Spotify. Puedes cerrar esta pestaña.")

    @router.post("/logout")
    async def post_logout(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return {"ok": True, "removed": spotify.logout()}

    @router.post("/call")
    async def post_call(request: Request) -> Dict[str, Any]:
        require_admin(request)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = str((body or {}).get("tool") or "")
        try:
            result = await asyncio.to_thread(spotify.call, name, (body or {}).get("args") or {})
        except spotify.SpotifyError as exc:
            raise HTTPException(exc.status if 400 <= exc.status < 600 else 400, str(exc))
        return {"ok": True, "tool": name, "result": result}

    return router
