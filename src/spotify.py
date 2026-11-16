"""spotify.py — Spotify for Faustus: playback, devices, queue, search, playlists,
albums and the saved library, over the Spotify Web API.

Login is OAuth 2.0 with PKCE against the owner's OWN Spotify developer app
(Spotify allows no shared public client):

1. developer.spotify.com/dashboard → Create app, Web API checked, Redirect URI
   ``http://127.0.0.1:<Faustus port>/api/spotify/callback`` (loopback IP literal;
   :func:`redirect_uri` says the exact one for this install);
2. paste the app's Client ID in Ajustes › Spotify (``spotify_client_id``);
3. open ``/api/spotify/login`` in the browser and approve.

The callback exchanges the code for tokens; they are stored encrypted
(src/secret_storage.py) in ``DATA_DIR/spotify/auth.json`` and refreshed
automatically. Nothing here needs a client secret.

The seven tools (:data:`TOOLS`, dispatched by :func:`call`) are served by the
built-in MCP server ``mcp_servers/spotify_server.py`` and by
``POST /api/spotify/call``. Playback control needs a Premium account; search,
playlists, albums, library and reading the state work on Free.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

import httpx

ACCOUNTS = "https://accounts.spotify.com"
API = "https://api.spotify.com/v1"
DASHBOARD_URL = "https://developer.spotify.com/dashboard"
SCOPE = " ".join((
    "user-modify-playback-state", "user-read-playback-state", "user-read-currently-playing",
    "user-read-recently-played", "playlist-read-private", "playlist-read-collaborative",
    "playlist-modify-public", "playlist-modify-private", "user-library-read", "user-library-modify",
))
_REFRESH_SKEW_S = 120
_PENDING_TTL_S = 900
_lock = threading.Lock()
_pending: Dict[str, Dict[str, Any]] = {}


class SpotifyError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, relogin: bool = False):
        super().__init__(message)
        self.status = status
        self.relogin = relogin


# --------------------------------------------------------------------------- config

def _setting(key: str, default: Any = "") -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


def client_id() -> str:
    return str(_setting("spotify_client_id", "") or "").strip()


def redirect_uri() -> str:
    configured = str(_setting("spotify_redirect_uri", "") or "").strip()
    if configured:
        return configured
    # A built-in MCP subprocess is told where the app lives; the app itself knows its port.
    base = os.environ.get("FAUSTUS_INTERNAL_API_URL", "")
    if not base:
        from src.constants import internal_api_base
        base = internal_api_base()
    return base.rstrip("/") + "/api/spotify/callback"


def auth_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "spotify", "auth.json")


def load_tokens(path: Optional[str] = None) -> Dict[str, Any]:
    try:
        with open(path or auth_path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    blob = raw.get("enc") if isinstance(raw, dict) else None
    if not blob:
        return raw if isinstance(raw, dict) else {}
    try:
        from src.secret_storage import decrypt
        return json.loads(decrypt(blob) or "{}")
    except Exception:  # noqa: BLE001 - an unreadable store is "not logged in"
        return {}


def save_tokens(state: Dict[str, Any], path: Optional[str] = None) -> None:
    from core.atomic_io import atomic_write_json
    target = path or auth_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        from src.secret_storage import encrypt
        body = {"enc": encrypt(json.dumps(state))}
    except Exception:  # noqa: BLE001 - without the key store, owner-only plaintext
        body = state
    atomic_write_json(target, body, indent=2, private=True)


def logout(path: Optional[str] = None) -> bool:
    try:
        os.remove(path or auth_path())
        return True
    except OSError:
        return False


def status(path: Optional[str] = None) -> Dict[str, Any]:
    tok = load_tokens(path)
    cid = client_id()
    out = {
        "configured": bool(cid), "logged_in": bool(tok.get("refresh_token") or tok.get("access_token")),
        "redirect_uri": redirect_uri(), "login_url": "/api/spotify/login",
        "scope": tok.get("granted_scope") or "", "expires_at": tok.get("expires_at") or 0,
    }
    if not cid:
        out["setup"] = (f"Crea una app en {DASHBOARD_URL} (Web API) con Redirect URI {out['redirect_uri']}, "
                        "pega su Client ID en Ajustes › Spotify y abre /api/spotify/login.")
    elif not out["logged_in"]:
        out["setup"] = "Abre /api/spotify/login en el navegador para autorizar la cuenta."
    return out


# --------------------------------------------------------------------------- PKCE login

def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def begin_login(*, now: Callable[[], float] = time.time) -> str:
    """The Spotify authorize URL for a fresh PKCE login (state kept in memory)."""
    cid = client_id()
    if not cid:
        raise SpotifyError(f"Spotify needs your app's Client ID first (Ajustes › Spotify; create the app at {DASHBOARD_URL})")
    verifier = secrets.token_urlsafe(64)[:96]
    state = secrets.token_urlsafe(24)
    with _lock:
        for k in [k for k, v in _pending.items() if now() - v["at"] > _PENDING_TTL_S]:
            _pending.pop(k, None)
        _pending[state] = {"verifier": verifier, "at": now(), "client_id": cid, "redirect_uri": redirect_uri()}
    query = urlencode({"client_id": cid, "response_type": "code", "redirect_uri": redirect_uri(), "scope": SCOPE,
                       "state": state, "code_challenge_method": "S256", "code_challenge": _challenge(verifier)})
    return f"{ACCOUNTS}/authorize?{query}"


def _token_post(data: Dict[str, str], post: Callable = httpx.post) -> Dict[str, Any]:
    try:
        r = post(f"{ACCOUNTS}/api/token", data=data,
                 headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
    except httpx.HTTPError as exc:
        raise SpotifyError(f"Spotify token request failed: {exc}") from exc
    if r.status_code >= 400:
        raise SpotifyError(f"Spotify token request refused ({r.status_code}): {r.text[:300]}",
                           status=r.status_code, relogin=True)
    payload = r.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise SpotifyError("Spotify answered without an access token", relogin=True)
    return payload


def _state_from(payload: Dict[str, Any], cid: str, previous: Optional[Dict[str, Any]] = None,
                now: Callable[[], float] = time.time) -> Dict[str, Any]:
    state = dict(previous or {})
    state.update({
        "client_id": cid, "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token") or state.get("refresh_token") or "",
        "granted_scope": payload.get("scope") or state.get("granted_scope") or SCOPE,
        "expires_at": int(now() + int(payload.get("expires_in") or 3600)),
    })
    return state


def finish_login(code: str, state: str, *, post: Callable = httpx.post, path: Optional[str] = None) -> Dict[str, Any]:
    with _lock:
        pending = _pending.pop(str(state or ""), None)
    if not pending:
        raise SpotifyError("unknown or expired login state: open /api/spotify/login again")
    payload = _token_post({"grant_type": "authorization_code", "code": code,
                           "redirect_uri": pending["redirect_uri"], "client_id": pending["client_id"],
                           "code_verifier": pending["verifier"]}, post)
    tokens = _state_from(payload, pending["client_id"])
    save_tokens(tokens, path)
    return tokens


def access_token(*, force: bool = False, post: Callable = httpx.post, path: Optional[str] = None,
                 now: Callable[[], float] = time.time) -> str:
    tok = load_tokens(path)
    if not tok.get("access_token"):
        raise SpotifyError("Spotify is not connected: " + (status(path).get("setup") or ""), relogin=True)
    if force or int(tok.get("expires_at") or 0) - _REFRESH_SKEW_S <= now():
        if not tok.get("refresh_token"):
            raise SpotifyError("Spotify login expired: open /api/spotify/login again", relogin=True)
        payload = _token_post({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                               "client_id": tok.get("client_id") or client_id()}, post)
        tok = _state_from(payload, tok.get("client_id") or client_id(), tok, now)
        save_tokens(tok, path)
    return tok["access_token"]


# --------------------------------------------------------------------------- Web API client

def request(method: str, path: str, *, params: Optional[Dict[str, Any]] = None, body: Any = None,
            send: Optional[Callable] = None, token: Optional[Callable[..., str]] = None) -> Any:
    """One Web API call; refreshes once on 401. ``None`` params are dropped."""
    send = send or httpx.request
    token = token or access_token
    params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if v is not None}
    for attempt in (0, 1):
        r = send(method, API + path, params=params or None, json=body,
                 headers={"Authorization": "Bearer " + token(force=bool(attempt))}, timeout=30)
        if r.status_code == 401 and attempt == 0:
            continue
        break
    if r.status_code == 204 or not r.content:
        return {"ok": True, "status": r.status_code}
    try:
        data = r.json()
    except ValueError:
        data = {"text": r.text[:500]}
    if r.status_code >= 400:
        detail = (data.get("error") or {}).get("message") if isinstance(data, dict) and isinstance(data.get("error"), dict) else data
        hint = ""
        if r.status_code == 403 and path.startswith("/me/player"):
            hint = " (playback control needs Spotify Premium)"
        elif r.status_code == 404 and path.startswith("/me/player"):
            hint = " (no active device: open Spotify on a device, or use spotify_devices transfer)"
        elif r.status_code == 429:
            hint = f" (rate limited; retry after {r.headers.get('Retry-After', '?')} s)"
        raise SpotifyError(f"Spotify {method} {path} -> {r.status_code}: {detail}{hint}", status=r.status_code)
    return data


_ID = re.compile(r"^[A-Za-z0-9]{10,40}$")


def spotify_id(value: Any, kind: str) -> str:
    """Accept an id, a ``spotify:<kind>:<id>`` URI or an open.spotify.com URL."""
    text = str(value or "").strip()
    m = re.search(rf"(?:spotify:{kind}:|open\.spotify\.com/(?:intl-[a-z-]+/)?{kind}/)([A-Za-z0-9]+)", text)
    if m:
        return m.group(1)
    if _ID.match(text):
        return text
    raise SpotifyError(f"not a Spotify {kind} id, URI or link: {text!r}")


def spotify_uri(value: Any, kind: str = "track") -> str:
    text = str(value or "").strip()
    m = re.match(r"^spotify:(track|episode|album|artist|playlist|show):[A-Za-z0-9]+$", text)
    if m:
        return text
    m = re.search(r"open\.spotify\.com/(?:intl-[a-z-]+/)?(track|episode|album|artist|playlist|show)/([A-Za-z0-9]+)", text)
    if m:
        return f"spotify:{m.group(1)}:{m.group(2)}"
    return f"spotify:{kind}:{spotify_id(text, kind)}"


def _list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [v for v in re.split(r"[,\s]+", value) if v]
    return [str(v) for v in (value or []) if str(v).strip()]


def _limit(a: Dict[str, Any], default: int = 20, top: int = 50) -> int:
    try:
        return max(1, min(int(a.get("limit") or default), top))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- tools

def _playback(a: Dict[str, Any], rq: Callable) -> Any:
    act = str(a.get("action") or "get_state").lower()
    dev = {"device_id": a.get("device_id")}
    if act == "get_state":
        return rq("GET", "/me/player", params={"market": a.get("market")})
    if act == "get_currently_playing":
        return rq("GET", "/me/player/currently-playing", params={"market": a.get("market")})
    if act == "play":
        body: Dict[str, Any] = {}
        uris = [spotify_uri(u) for u in _list(a.get("uris"))]
        if a.get("context_uri"):
            body["context_uri"] = str(a["context_uri"])
        elif uris:
            body["uris"] = uris
        if a.get("position_ms") is not None:
            body["position_ms"] = int(a["position_ms"])
        return rq("PUT", "/me/player/play", params=dev, body=body or None)
    simple = {"pause": ("PUT", "/me/player/pause"), "next": ("POST", "/me/player/next"),
              "previous": ("POST", "/me/player/previous")}
    if act in simple:
        return rq(simple[act][0], simple[act][1], params=dev)
    if act == "seek":
        return rq("PUT", "/me/player/seek", params={**dev, "position_ms": int(a.get("position_ms") or 0)})
    if act == "set_volume":
        return rq("PUT", "/me/player/volume", params={**dev, "volume_percent": max(0, min(100, int(a.get("volume_percent") or 0)))})
    if act == "set_shuffle":
        return rq("PUT", "/me/player/shuffle", params={**dev, "state": str(str(a.get("state")).lower() in ("1", "true", "on", "yes")).lower()})
    if act == "set_repeat":
        state = str(a.get("state") or "off").lower()
        if state not in ("track", "context", "off"):
            raise SpotifyError("state for set_repeat: track, context or off")
        return rq("PUT", "/me/player/repeat", params={**dev, "state": state})
    if act == "recently_played":
        return rq("GET", "/me/player/recently-played", params={"limit": _limit(a), "after": a.get("after"), "before": a.get("before")})
    raise SpotifyError(f"unknown spotify_playback action {act}")


def _devices(a: Dict[str, Any], rq: Callable) -> Any:
    if str(a.get("action") or "list") == "transfer":
        if not a.get("device_id"):
            raise SpotifyError("device_id is required to transfer playback")
        return rq("PUT", "/me/player", body={"device_ids": [a["device_id"]], "play": bool(a.get("play"))})
    return rq("GET", "/me/player/devices")


def _queue(a: Dict[str, Any], rq: Callable) -> Any:
    if str(a.get("action") or "get") == "add":
        return rq("POST", "/me/player/queue", params={"uri": spotify_uri(a.get("uri")), "device_id": a.get("device_id")})
    return rq("GET", "/me/player/queue")


_SEARCH_TYPES = ("track", "album", "artist", "playlist", "show", "episode", "audiobook")


def _search(a: Dict[str, Any], rq: Callable) -> Any:
    q = str(a.get("query") or "").strip()
    if not q:
        raise SpotifyError("query is required")
    types = [t.lower() for t in _list(a.get("types") or a.get("type") or ["track"]) if t.lower() in _SEARCH_TYPES]
    return rq("GET", "/search", params={"q": q, "type": ",".join(types or ["track"]), "limit": _limit(a, 10, 50),
                                        "offset": a.get("offset"), "market": a.get("market")})


def _playlists(a: Dict[str, Any], rq: Callable) -> Any:
    act = str(a.get("action") or "list").lower()
    if act == "list":
        return rq("GET", "/me/playlists", params={"limit": _limit(a), "offset": a.get("offset")})
    if act == "create":
        if not str(a.get("name") or "").strip():
            raise SpotifyError("name is required to create a playlist")
        return rq("POST", "/me/playlists", body={"name": a["name"], "public": bool(a.get("public")),
                                                 "collaborative": bool(a.get("collaborative")),
                                                 "description": a.get("description")})
    pid = spotify_id(a.get("playlist_id"), "playlist")
    if act == "get":
        return rq("GET", f"/playlists/{pid}", params={"market": a.get("market")})
    if act == "items":
        return rq("GET", f"/playlists/{pid}/items", params={"limit": _limit(a), "offset": a.get("offset"), "market": a.get("market")})
    uris = [spotify_uri(u) for u in _list(a.get("uris"))]
    if act == "add_items":
        return rq("POST", f"/playlists/{pid}/items", body={"uris": uris, "position": a.get("position")})
    if act == "remove_items":
        return rq("DELETE", f"/playlists/{pid}/items", body={"items": [{"uri": u} for u in uris], "snapshot_id": a.get("snapshot_id")})
    if act == "update_details":
        return rq("PUT", f"/playlists/{pid}", body={"name": a.get("name"), "public": a.get("public"),
                                                     "collaborative": a.get("collaborative"), "description": a.get("description")})
    raise SpotifyError(f"unknown spotify_playlists action {act}")


def _albums(a: Dict[str, Any], rq: Callable) -> Any:
    aid = spotify_id(a.get("album_id") or a.get("id"), "album")
    if str(a.get("action") or "get") == "tracks":
        return rq("GET", f"/albums/{aid}/tracks", params={"limit": _limit(a), "offset": a.get("offset"), "market": a.get("market")})
    return rq("GET", f"/albums/{aid}", params={"market": a.get("market")})


def _library(a: Dict[str, Any], rq: Callable) -> Any:
    kind = str(a.get("kind") or "tracks").lower()
    if kind not in ("tracks", "albums"):
        raise SpotifyError("kind must be tracks or albums")
    act = str(a.get("action") or "list").lower()
    if act == "list":
        return rq("GET", f"/me/{kind}", params={"limit": _limit(a), "offset": a.get("offset"), "market": a.get("market")})
    uris = ",".join(spotify_uri(u, kind[:-1]) for u in _list(a.get("uris") or a.get("ids")))
    if not uris:
        raise SpotifyError("uris (or ids) are required")
    if act == "save":
        return rq("PUT", "/me/library", params={"uris": uris})
    if act == "remove":
        return rq("DELETE", "/me/library", params={"uris": uris})
    raise SpotifyError(f"unknown spotify_library action {act}")


_STR = {"type": "string"}
_INT = {"type": "integer"}
_ARR = {"type": "array", "items": {"type": "string"}}


def _enum(*v):
    return {"type": "string", "enum": list(v)}


TOOLS: Dict[str, Dict[str, Any]] = {
    "spotify_status": {
        "description": "Whether Spotify is configured and logged in, the exact Redirect URI to register and what to do next.",
        "parameters": {"type": "object", "properties": {}}, "fn": None},
    "spotify_playback": {
        "description": "Control Spotify playback (play, pause, next, previous, seek, shuffle, repeat, volume), read the "
                       "current state or what is playing, or the recently played tracks. Control needs Premium.",
        "parameters": {"type": "object", "properties": {
            "action": _enum("get_state", "get_currently_playing", "play", "pause", "next", "previous", "seek",
                            "set_repeat", "set_shuffle", "set_volume", "recently_played"),
            "uris": {**_ARR, "description": "play: tracks/episodes (ids, URIs or links)"},
            "context_uri": {**_STR, "description": "play: an album, playlist or artist URI"},
            "device_id": _STR, "position_ms": _INT, "volume_percent": _INT, "market": _STR,
            "state": {**_STR, "description": "set_repeat: track/context/off; set_shuffle: true/false"},
            "limit": _INT, "after": _INT, "before": _INT}, "required": ["action"]}, "fn": _playback},
    "spotify_devices": {
        "description": "List Spotify Connect devices, or transfer playback to one.",
        "parameters": {"type": "object", "properties": {"action": _enum("list", "transfer"), "device_id": _STR,
                                                        "play": {"type": "boolean"}}}, "fn": _devices},
    "spotify_queue": {
        "description": "Read the playback queue, or add a track/episode to it.",
        "parameters": {"type": "object", "properties": {"action": _enum("get", "add"), "uri": _STR, "device_id": _STR}},
        "fn": _queue},
    "spotify_search": {
        "description": "Search the Spotify catalog: tracks, albums, artists, playlists, shows, episodes, audiobooks.",
        "parameters": {"type": "object", "properties": {"query": _STR, "types": _ARR, "limit": _INT, "offset": _INT,
                                                        "market": _STR}, "required": ["query"]}, "fn": _search},
    "spotify_playlists": {
        "description": "List your playlists; read one or its items; create one; add or remove items; change its details.",
        "parameters": {"type": "object", "properties": {
            "action": _enum("list", "get", "items", "create", "add_items", "remove_items", "update_details"),
            "playlist_id": _STR, "name": _STR, "description": _STR, "public": {"type": "boolean"},
            "collaborative": {"type": "boolean"}, "uris": _ARR, "position": _INT, "snapshot_id": _STR,
            "limit": _INT, "offset": _INT, "market": _STR}, "required": ["action"]}, "fn": _playlists},
    "spotify_albums": {
        "description": "An album's details, or its track list.",
        "parameters": {"type": "object", "properties": {"action": _enum("get", "tracks"), "album_id": _STR,
                                                        "limit": _INT, "offset": _INT, "market": _STR},
                       "required": ["album_id"]}, "fn": _albums},
    "spotify_library": {
        "description": "Your saved tracks or albums (`kind`): list them, save some, remove some.",
        "parameters": {"type": "object", "properties": {"kind": _enum("tracks", "albums"), "action": _enum("list", "save", "remove"),
                                                        "uris": _ARR, "ids": _ARR, "limit": _INT, "offset": _INT, "market": _STR},
                       "required": ["kind", "action"]}, "fn": _library},
}


def call(name: str, args: Optional[Dict[str, Any]] = None, *, rq: Optional[Callable] = None) -> Any:
    """Run one tool. Raises SpotifyError with a readable message."""
    spec = TOOLS.get(name)
    if spec is None:
        raise SpotifyError(f"unknown Spotify tool {name}")
    if name == "spotify_status":
        return status()
    return spec["fn"](dict(args or {}), rq or request)
