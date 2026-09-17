"""src/whatsapp_bridge.py — Faustus's side of the WhatsApp bridge.

The bridge itself is `bridges/whatsapp/server.mjs` (Node, WhatsApp Web
multi-device protocol): it pairs a personal account by QR, keeps the
session and the message store under ``DATA_DIR/whatsapp/`` and answers on
the loopback with a bearer token this module writes. This module:

* starts the bridge as a **detached** child (`process_launch.spawn_detached`),
  so the WhatsApp session survives a Faustus restart, and recognises an
  already-running bridge by probing its port with the token;
* installs its Node dependencies the first time (`npm install --omit=dev`
  in the bridge folder) — once, under a lock;
* wraps the bridge's HTTP API for the routes, the tools and the watcher
  action: status/QR, chats, messages, contacts, resolve, send, mark-read,
  logout.

Sending is a real side effect on the person's own account: the tool that
sends is classed EXTERNAL_SIDE_EFFECT (approval card), the route is
`require_human`, and nothing here sends on its own. Message text coming
back is DATA — a summarising prompt says so — never an instruction.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from src import process_ownership
from src.process_launch import poll_readiness, spawn_detached

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8790
_LOCK = threading.Lock()
_INSTALL_LOCK = threading.Lock()
HTTP_TIMEOUT = 20.0


class BridgeError(Exception):
    pass


# ---------------------------------------------------------------------------
# paths / config
# ---------------------------------------------------------------------------

def bridge_dir() -> str:
    from src.constants import BASE_DIR  # type: ignore
    return os.path.join(BASE_DIR, "bridges", "whatsapp")


def data_dir() -> str:
    from src.constants import DATA_DIR
    path = os.path.join(DATA_DIR, "whatsapp")
    os.makedirs(path, exist_ok=True)
    return path


def port() -> int:
    try:
        from src.settings import get_setting
        return int(get_setting("whatsapp_port", DEFAULT_PORT) or DEFAULT_PORT)
    except Exception:  # noqa: BLE001
        return DEFAULT_PORT


def token() -> str:
    path = os.path.join(data_dir(), "token")
    try:
        with open(path, encoding="utf-8") as fh:
            tok = fh.read().strip()
            if tok:
                return tok
    except OSError:
        pass
    tok = secrets.token_hex(24)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(tok)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return tok


def base_url() -> str:
    return f"http://127.0.0.1:{port()}"


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {token()}"}


def _record_path() -> str:
    return os.path.join(data_dir(), "bridge.json")


def _read_record() -> Dict[str, Any]:
    try:
        with open(_record_path(), encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_record(rec: Dict[str, Any]) -> None:
    with open(_record_path(), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def installed() -> bool:
    return os.path.isdir(os.path.join(bridge_dir(), "node_modules", "@whiskeysockets", "baileys"))


def node_path() -> Optional[str]:
    return shutil.which("node") or (r"C:\Program Files\nodejs\node.exe" if os.path.exists(r"C:\Program Files\nodejs\node.exe") else None)


def install(timeout_s: float = 240.0) -> Dict[str, Any]:
    """`npm install --omit=dev` in the bridge folder (idempotent, locked)."""
    with _INSTALL_LOCK:
        if installed():
            return {"ok": True, "already": True}
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            return {"ok": False, "error": "npm not found on PATH"}
        try:
            proc = subprocess.run([npm, "install", "--omit=dev", "--no-audit", "--no-fund"], cwd=bridge_dir(),
                                  capture_output=True, text=True, timeout=timeout_s, shell=False)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        ok = proc.returncode == 0 and installed()
        return {"ok": ok, "error": "" if ok else (proc.stderr or proc.stdout)[-600:]}


def probe(timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    """The bridge's /status when something answering with our token is on
    the port; None otherwise."""
    try:
        r = httpx.get(base_url() + "/status", headers=_headers(), timeout=timeout)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:  # noqa: BLE001
        return None


def is_running() -> bool:
    return probe() is not None


def start(*, owner: str = "") -> Dict[str, Any]:
    """Start the bridge (or report it already runs). Never pairs by itself:
    pairing is the person scanning the QR the status carries."""
    with _LOCK:
        st = probe()
        if st is not None:
            return {"started": False, "already_running": True, "status": st}
        node = node_path()
        if not node:
            return {"started": False, "error": "node is not installed (the bridge needs Node.js)"}
        if not installed():
            inst = install()
            if not inst.get("ok"):
                return {"started": False, "error": f"npm install failed: {inst.get('error')}"}
        env = dict(os.environ)
        env.update({
            "WA_PORT": str(port()), "WA_TOKEN": token(),
            "WA_AUTH_DIR": os.path.join(data_dir(), "auth"), "WA_DATA_DIR": data_dir(),
        })
        log_path = os.path.join(data_dir(), "bridge.log")
        result = spawn_detached([node, "server.mjs"], cwd=bridge_dir(), env=env, log_path=log_path, owner=owner or "whatsapp")
        _write_record({"pid": result.pid, "spawned_at": result.spawned_at, "port": port(), "started_at": time.time()})
        ready = poll_readiness(lambda: probe(1.5) is not None, timeout_s=20.0, interval_s=0.5)
        return {"started": True, "pid": result.pid, "ready": ready, "log_path": log_path,
                "status": probe() if ready else None}


def stop() -> Dict[str, Any]:
    """Stop the bridge process this module started (the WhatsApp session
    stays on disk; `start` resumes it without a new QR)."""
    rec = _read_record()
    pid, spawned_at = rec.get("pid"), rec.get("spawned_at")
    if not pid:
        return {"stopped": False, "reason": "no bridge record; use the Processes screen if one is running"}
    outcome = process_ownership.terminate_tree(int(pid), spawned_at=spawned_at)
    if outcome.owned or outcome.code == "gone":
        _write_record({})
        return {"stopped": True, "pid": pid}
    return {"stopped": False, "reason": outcome.reason}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    try:
        r = httpx.get(base_url() + path, params=params or {}, headers=_headers(), timeout=HTTP_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        raise BridgeError(f"WhatsApp bridge is not running ({exc.__class__.__name__}); start it in Tools → WhatsApp")
    if r.status_code == 409:
        raise BridgeError("ambiguous: " + ", ".join(c.get("name") or c.get("jid") for c in (r.json().get("ambiguous") or [])))
    if r.status_code >= 400:
        try:
            err = (r.json() or {}).get("error")
        except ValueError:
            err = None
        raise BridgeError(err or f"HTTP {r.status_code}")
    return r.json()


def _post(path: str, body: Dict[str, Any]) -> Any:
    try:
        r = httpx.post(base_url() + path, json=body, headers=_headers(), timeout=HTTP_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        raise BridgeError(f"WhatsApp bridge is not running ({exc.__class__.__name__}); start it in Tools → WhatsApp")
    data = r.json() if r.content else {}
    if r.status_code == 409:
        raise BridgeError("ambiguous — which one? " + ", ".join(f"{c.get('name')} ({c.get('jid')})" for c in (data.get("ambiguous") or [])))
    if r.status_code >= 400:
        raise BridgeError(data.get("error") or f"HTTP {r.status_code}")
    return data


def status() -> Dict[str, Any]:
    st = probe()
    if st is None:
        return {"status": "stopped", "installed": installed(), "node": bool(node_path()), "port": port()}
    st["installed"] = True
    st["port"] = port()
    return st


def chats(limit: int = 50) -> List[Dict[str, Any]]:
    return _get("/chats", {"limit": limit})


def messages(chat: Optional[str] = None, *, since_hours: float = 24, limit: int = 200,
             unread_only: bool = False) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"limit": limit, "since": int(time.time() - since_hours * 3600)}
    if chat:
        params["chat"] = chat
    if unread_only:
        params["unread"] = 1
    return _get("/messages", params)


def contacts(q: str = "") -> List[Dict[str, Any]]:
    return _get("/contacts", {"q": q} if q else None)


def resolve(to: str) -> Dict[str, Any]:
    return _post("/resolve", {"to": to})


def send(to: str, text: str = "", *, quote: Optional[str] = None, mentions: Optional[List[str]] = None,
         media: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"to": to, "text": text or ""}
    if quote:
        body["quote"] = quote
    if mentions:
        body["mentions"] = list(mentions)
    if media:
        body["media"] = media
    return _post("/send", body)


def send_file(to: str, data: bytes, mime: str, *, filename: str = "", caption: str = "", quote: Optional[str] = None,
              voice: bool = False) -> Dict[str, Any]:
    """Photo / document / audio. A voice note must be ogg/opus: browsers record
    webm/opus, so it goes through ffmpeg when that is installed; otherwise it
    is sent as a plain audio message."""
    import base64
    if voice and not mime.startswith("audio/ogg"):
        converted = _to_ogg_opus(data)
        if converted:
            data, mime = converted, "audio/ogg; codecs=opus"
        else:
            voice = False
    media = {"base64": base64.b64encode(data).decode("ascii"), "mime": mime, "filename": filename,
             "caption": caption, "voice": bool(voice)}
    return send(to, caption, quote=quote, media=media)


def _to_ogg_opus(data: bytes) -> Optional[bytes]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run([ffmpeg, "-loglevel", "error", "-i", "pipe:0", "-vn", "-c:a", "libopus", "-b:a", "32k",
                               "-ac", "1", "-ar", "48000", "-f", "ogg", "pipe:1"], input=data, capture_output=True,
                              timeout=60)
        return proc.stdout if proc.returncode == 0 and proc.stdout else None
    except Exception:  # noqa: BLE001
        return None


def react(message_id: str, emoji: str) -> Dict[str, Any]:
    return _post("/react", {"id": message_id, "emoji": emoji})


def delete(message_id: str) -> Dict[str, Any]:
    return _post("/delete", {"id": message_id})


def forward(message_id: str, to: str) -> Dict[str, Any]:
    return _post("/forward", {"id": message_id, "to": to})


def edit(message_id: str, text: str) -> Dict[str, Any]:
    return _post("/edit", {"id": message_id, "text": text})


def typing(chat: str, state: str = "composing") -> Dict[str, Any]:
    return _post("/typing", {"chat": chat, "state": state})


def subscribe(chat: str) -> Dict[str, Any]:
    return _post("/subscribe", {"chat": chat})


def search(q: str, chat: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"q": q, "limit": limit}
    if chat:
        params["chat"] = chat
    return _get("/search", params)


def mark_read(chat: Optional[str] = None) -> Dict[str, Any]:
    return _post("/mark-read", {"chat": chat} if chat else {})


def logout() -> Dict[str, Any]:
    return _post("/logout", {})


def history(chat: str, count: int = 50) -> Dict[str, Any]:
    """Ask the phone for older messages of one chat (they arrive asynchronously)."""
    return _post("/history", {"chat": chat, "count": int(count)})


def _get_bytes(path: str, params: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Optional[tuple]:
    try:
        r = httpx.get(base_url() + path, params=params or {}, headers=_headers(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise BridgeError(f"WhatsApp bridge is not running ({exc.__class__.__name__}); start it in Tools → WhatsApp")
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise BridgeError(f"HTTP {r.status_code}")
    return r.content, r.headers.get("content-type", "application/octet-stream")


def avatar(jid: str) -> Optional[tuple]:
    """(bytes, content_type) of the profile picture, or None when there is none."""
    return _get_bytes("/avatar", {"jid": jid}, timeout=15.0)


def media(name: str) -> Optional[tuple]:
    """(bytes, content_type) of a pulled voice note / photo / document."""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    return _get_bytes(f"/media/{name}")


# ---------------------------------------------------------------------------
# voice notes → text (Faustus's own speech-to-text, cached per message)
# ---------------------------------------------------------------------------

def _transcripts_path() -> str:
    return os.path.join(data_dir(), "transcripts.json")


def _load_transcripts() -> Dict[str, str]:
    try:
        with open(_transcripts_path(), encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cached_transcript(message_id: str) -> Optional[str]:
    return _load_transcripts().get(message_id)


def transcribe(message: Dict[str, Any]) -> Optional[str]:
    """Text of a voice note (or any audio message), through the configured
    speech provider; cached so a chat is transcribed once. None when the
    message has no audio or no provider is available."""
    mid = str(message.get("id") or "")
    name = message.get("media")
    if not mid or not name or message.get("kind") != "audio":
        return None
    cache = _load_transcripts()
    if mid in cache:
        return cache[mid]
    try:
        from services.stt import get_stt_service
        svc = get_stt_service()
        if not svc.available:
            return None
        got = media(str(name))
        if not got:
            return None
        text = svc.transcribe(got[0]) or ""
    except BridgeError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[whatsapp] transcription failed for %s: %s", mid, exc)
        return None
    text = text.strip()
    cache = _load_transcripts()
    cache[mid] = text
    with _LOCK:
        try:
            with open(_transcripts_path(), "w", encoding="utf-8") as fh:
                json.dump(cache, fh, ensure_ascii=False)
        except OSError:
            pass
    return text


def with_transcripts(rows: List[Dict[str, Any]], *, max_new: int = 8) -> List[Dict[str, Any]]:
    """Attach `transcript` to audio rows: cached ones always, up to `max_new`
    fresh transcriptions per call (voice notes are short; keep a read bounded)."""
    cache = _load_transcripts()
    fresh = 0
    for m in rows:
        if m.get("kind") != "audio" or not m.get("media"):
            continue
        mid = str(m.get("id") or "")
        if mid in cache:
            m["transcript"] = cache[mid]
        elif fresh < max_new:
            fresh += 1
            text = transcribe(m)
            if text is not None:
                m["transcript"] = text
    return rows


# ---------------------------------------------------------------------------
# text helpers for tools / cards
# ---------------------------------------------------------------------------

def _fmt_ts(ts: Any) -> str:
    try:
        from datetime import datetime
        return datetime.fromtimestamp(float(ts)).strftime("%d/%m %H:%M")
    except Exception:  # noqa: BLE001
        return ""


def _line(m: Dict[str, Any]) -> str:
    who = "yo" if m.get("from_me") else (m.get("from_name") or m.get("from") or "?")
    kind = m.get("kind")
    if m.get("deleted"):
        text = "[message deleted]"
    elif kind == "audio":
        secs = int(m.get("seconds") or 0)
        label = f"[voice note {secs // 60}:{secs % 60:02d}]" if secs else "[voice note]"
        text = f"{label} {m['transcript']}" if m.get("transcript") else f"{label} (not transcribed)"
    elif kind == "text":
        text = m.get("text") or ""
    else:
        text = f"[{kind}] {m.get('text') or ''}".rstrip()
    reply = m.get("reply_to") or {}
    if reply.get("text") or reply.get("from_name"):
        quoted = str(reply.get("text") or "")[:60]
        text = f"(replying to {'yo' if reply.get('from_me') else reply.get('from_name') or '?'}: «{quoted}») {text}"
    reactions = m.get("reactions") or []
    if reactions:
        text += "  [reactions: " + ", ".join(f"{r.get('emoji')} by {'yo' if r.get('from_me') else r.get('from_name') or '?'}" for r in reactions) + "]"
    return f"[{_fmt_ts(m.get('ts'))}] {who}: {text}"


def transcript(rows: List[Dict[str, Any]], *, max_chars: int = 12000) -> str:
    """Messages grouped **per chat** (a `### ` header naming the chat, then its
    lines oldest first). One interleaved stream mixed people and chats up in
    the model's summary (seen live: a group message attributed to a 1:1 chat);
    the grouping makes 'who said what where' unambiguous."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    names: Dict[str, str] = {}
    for m in rows:
        jid = str(m.get("chat") or "")
        groups.setdefault(jid, []).append(m)
        if m.get("chat_name"):
            names[jid] = str(m["chat_name"])
    blocks = []
    for jid, ms in groups.items():
        ms = sorted(ms, key=lambda x: float(x.get("ts") or 0))
        is_group = jid.endswith("@g.us")
        title = names.get(jid) or jid
        header = f"### {'Group' if is_group else 'Chat'}: {title} ({len(ms)} messages)"
        blocks.append(header + "\n" + "\n".join(_line(m) for m in ms))
    out = "\n\n".join(blocks)
    return out[-max_chars:]
