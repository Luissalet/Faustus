"""conversation_bridge.py — Faustus's conversations for an outside agent.

One vocabulary over two kinds of conversation, so a coding agent (Claude Code,
Codex, Cursor or any MCP client) can follow and answer them through
`mcp_servers/conversations_server.py`:

    faustus:<session id>     a Faustus chat (Studio's conversation list)
    whatsapp:<jid>           a WhatsApp chat of the paired account (src/whatsapp_bridge.py)

Everything goes through the running Faustus's own HTTP API, the same routes
Studio uses, so what an outside agent sees is what the person sees:

* list / read: ``GET /api/sessions``, ``GET /api/history/<id>?limit=``,
  ``GET /api/whatsapp/chats`` and ``GET /api/whatsapp/messages``;
* send to a Faustus chat: ``POST /api/agent-mailbox/send`` — the message arrives
  framed as coming from the outside agent and, ``urgent`` (the default here),
  wakes an idle chat so Faustus answers it (src/agent_mailbox.py);
* send to WhatsApp: ``POST /api/whatsapp/send``, the WhatsApp screen's own path.

Events are new messages after a cursor (epoch milliseconds). A cursor-less poll
starts at "now": history before that is read with :meth:`Bridge.read`.

The HTTP layer is one callable, ``request(method, path, params=None, json=None)``
returning ``(status, payload)``, so tests drive this module without a server.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

Request = Callable[..., Tuple[int, Any]]

SOURCES = ("faustus", "whatsapp")
#: Messages read per chat when polling for events.
_POLL_TAIL = 20
#: How far back a WhatsApp poll asks the bridge (it filters by the cursor anyway).
_WA_WINDOW_H = 6


class BridgeError(RuntimeError):
    pass


def split_key(key: str) -> Tuple[str, str]:
    """``"faustus:abc"`` -> ``("faustus", "abc")``. A bare id is a Faustus chat,
    a bare JID (``...@s.whatsapp.net`` / ``@g.us``) a WhatsApp one."""
    key = str(key or "").strip()
    source, sep, ident = key.partition(":")
    if sep and source in SOURCES and ident.strip():
        return source, ident.strip()
    if "@" in key:
        return "whatsapp", key
    if key:
        return "faustus", key
    raise BridgeError("a conversation key is required (faustus:<id> or whatsapp:<jid>)")


def _iso_ms(value: Any) -> int:
    """ISO-8601 (with or without Z/offset; naive = UTC) -> epoch ms; 0 when unreadable."""
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z") if ms else ""


def _text(content: Any) -> str:
    """A history entry's content is a string or a list of parts; keep the text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    out.append(str(part.get("text") or ""))
                elif part.get("type"):
                    out.append(f"[{part.get('type')}]")
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(p for p in out if p)
    return "" if content is None else str(content)


def _wa_text(m: Dict[str, Any]) -> str:
    kind = m.get("kind") or "text"
    if m.get("deleted"):
        return "[message deleted]"
    if kind == "text":
        return str(m.get("text") or "")
    if kind == "audio" and m.get("transcript"):
        return f"[voice note] {m['transcript']}"
    return f"[{kind}] {m.get('text') or ''}".rstrip()


class Bridge:
    def __init__(self, request: Request, *, now: Callable[[], float] = time.time):
        self._request = request
        self._now = now
        self.started_ms = int(now() * 1000)

    # -- transport ---------------------------------------------------------
    def _call(self, method: str, path: str, **kw) -> Any:
        status, payload = self._request(method, path, **kw)
        if status >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            raise BridgeError(f"{method} {path} -> {status}: {detail or payload}")
        return payload

    # -- conversations -----------------------------------------------------
    def _faustus_rows(self) -> List[Dict[str, Any]]:
        rows = self._call("GET", "/api/sessions") or []
        out = []
        for s in rows if isinstance(rows, list) else []:
            if not isinstance(s, dict) or not s.get("id"):
                continue
            last = s.get("last_message_at") or s.get("updated_at") or s.get("created_at")
            out.append({
                "key": f"faustus:{s['id']}", "source": "faustus", "id": s["id"],
                "title": s.get("name") or "", "last_activity": _iso(_iso_ms(last)),
                "last_ms": _iso_ms(last), "messages": s.get("message_count") or 0,
                "model": s.get("model") or "", "folder": s.get("folder") or "",
            })
        return out

    def _whatsapp_rows(self, limit: int) -> List[Dict[str, Any]]:
        payload = self._call("GET", "/api/whatsapp/chats", params={"limit": max(1, min(int(limit), 500))})
        out = []
        for c in (payload or {}).get("chats") or []:
            if not isinstance(c, dict) or not c.get("jid"):
                continue
            ms = int(float(c.get("last_ts") or 0) * 1000)
            out.append({
                "key": f"whatsapp:{c['jid']}", "source": "whatsapp", "id": c["jid"],
                "title": c.get("name") or c["jid"], "last_activity": _iso(ms), "last_ms": ms,
                "unread": int(c.get("unread") or 0), "is_group": bool(c.get("is_group")),
                "last_text": str(c.get("last_text") or "")[:200],
            })
        return out

    def conversations(self, source: str = "all", query: str = "", limit: int = 30) -> Dict[str, Any]:
        """Newest first, across the sources asked for. A source that cannot be
        read (WhatsApp not paired, bridge stopped) is reported, not fatal."""
        source = (source or "all").strip().lower()
        wanted = SOURCES if source in ("", "all") else tuple(s for s in SOURCES if s == source)
        if not wanted:
            raise BridgeError(f"unknown source {source!r}: use all, faustus or whatsapp")
        limit = max(1, min(int(limit or 30), 500))
        rows: List[Dict[str, Any]] = []
        unavailable: Dict[str, str] = {}
        for src in wanted:
            try:
                rows += self._faustus_rows() if src == "faustus" else self._whatsapp_rows(max(limit, 50))
            except BridgeError as exc:
                unavailable[src] = str(exc)
        q = (query or "").strip().lower()
        if q:
            rows = [r for r in rows if q in r["title"].lower() or q in r["id"].lower()]
        rows.sort(key=lambda r: r["last_ms"], reverse=True)
        out = {"conversations": [{k: v for k, v in r.items() if k != "last_ms"} for r in rows[:limit]]}
        if unavailable:
            out["unavailable"] = unavailable
        return out

    def conversation(self, key: str) -> Dict[str, Any]:
        source, ident = split_key(key)
        rows = self._faustus_rows() if source == "faustus" else self._whatsapp_rows(500)
        for r in rows:
            if r["id"] == ident:
                return {k: v for k, v in r.items() if k != "last_ms"}
        raise BridgeError(f"conversation not found: {source}:{ident}")

    # -- messages ----------------------------------------------------------
    def _faustus_messages(self, sid: str, limit: int) -> List[Dict[str, Any]]:
        payload = self._call("GET", f"/api/history/{sid}", params={"limit": max(1, min(int(limit), 100))})
        out = []
        for i, entry in enumerate((payload or {}).get("history") or []):
            meta = entry.get("metadata") or {}
            text = _text(entry.get("content"))
            if not text.strip() and entry.get("role") not in ("user", "assistant"):
                continue
            ms = _iso_ms(meta.get("timestamp"))
            row = {"conversation": f"faustus:{sid}", "role": entry.get("role") or "",
                   "text": text, "time": _iso(ms), "ms": ms}
            sender = (meta.get("agent_message") or {}).get("sender") if isinstance(meta.get("agent_message"), dict) else None
            if sender:
                row["from_agent"] = sender
            out.append(row)
        return out

    def _whatsapp_messages(self, jid: Optional[str], limit: int, hours: float) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": max(1, min(int(limit), 500)), "hours": hours}
        if jid:
            params["chat"] = jid
        payload = self._call("GET", "/api/whatsapp/messages", params=params)
        out = []
        for m in (payload or {}).get("messages") or []:
            ms = int(float(m.get("ts") or 0) * 1000)
            out.append({
                "conversation": f"whatsapp:{m.get('chat') or jid}", "id": m.get("id") or "",
                "role": "me" if m.get("from_me") else "contact",
                "from": "" if m.get("from_me") else (m.get("from_name") or m.get("from") or ""),
                "text": _wa_text(m), "time": _iso(ms), "ms": ms,
            })
        out.sort(key=lambda r: r["ms"])
        return out

    def read(self, key: str, limit: int = 30) -> Dict[str, Any]:
        source, ident = split_key(key)
        limit = max(1, min(int(limit or 30), 100 if source == "faustus" else 500))
        rows = (self._faustus_messages(ident, limit) if source == "faustus"
                else self._whatsapp_messages(ident, limit, 24 * 365))[-limit:]
        return {"conversation": f"{source}:{ident}", "messages": [{k: v for k, v in r.items() if k != "ms"} for r in rows]}

    # -- events ------------------------------------------------------------
    def poll(self, cursor: Optional[int] = None, source: str = "all", conversations: Optional[List[str]] = None) -> Dict[str, Any]:
        """New messages after ``cursor`` (epoch ms), oldest first, plus the next cursor."""
        cursor = int(cursor) if cursor not in (None, "", 0) else self.started_ms
        source = (source or "all").strip().lower()
        only = {f"{s}:{i}" for s, i in (split_key(k) for k in (conversations or []))}
        events: List[Dict[str, Any]] = []
        unavailable: Dict[str, str] = {}
        if source in ("all", "faustus"):
            try:
                for conv in self._faustus_rows():
                    if conv["last_ms"] <= cursor or (only and conv["key"] not in only):
                        continue
                    for m in self._faustus_messages(conv["id"], _POLL_TAIL):
                        if m["ms"] > cursor:
                            events.append({**m, "title": conv["title"]})
            except BridgeError as exc:
                unavailable["faustus"] = str(exc)
        if source in ("all", "whatsapp"):
            try:
                for m in self._whatsapp_messages(None, 500, _WA_WINDOW_H):
                    if m["ms"] > cursor and (not only or m["conversation"] in only):
                        events.append(m)
            except BridgeError as exc:
                unavailable["whatsapp"] = str(exc)
        events.sort(key=lambda e: e["ms"])
        next_cursor = max([cursor] + [e["ms"] for e in events])
        out: Dict[str, Any] = {"events": [{k: v for k, v in e.items() if k != "ms"} for e in events],
                               "cursor": next_cursor}
        if unavailable:
            out["unavailable"] = unavailable
        return out

    def wait(self, cursor: Optional[int] = None, timeout_s: float = 30, source: str = "all",
             conversations: Optional[List[str]] = None, *, interval_s: float = 2.0,
             sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
        """Like :meth:`poll`, but holds the call until something arrives or ``timeout_s`` passes."""
        cursor = int(cursor) if cursor not in (None, "", 0) else int(self._now() * 1000)
        deadline = self._now() + max(0.0, min(float(timeout_s or 0), 300.0))
        while True:
            out = self.poll(cursor, source, conversations)
            if out["events"] or self._now() >= deadline:
                out["timed_out"] = not out["events"]
                return out
            sleep(min(interval_s, max(0.0, deadline - self._now())))

    # -- send --------------------------------------------------------------
    def send(self, key: str, text: str, *, urgent: bool = True, sender: str = "mcp") -> Dict[str, Any]:
        text = str(text or "").strip()
        if not text:
            raise BridgeError("text is required")
        source, ident = split_key(key)
        if source == "faustus":
            result = self._call("POST", "/api/agent-mailbox/send",
                                json={"to": ident, "body": text, "urgent": bool(urgent), "sender": sender or "mcp"})
            return {"ok": True, "conversation": f"faustus:{ident}", "delivery": result}
        result = self._call("POST", "/api/whatsapp/send", json={"to": ident, "text": text})
        return {"ok": True, "conversation": f"whatsapp:{ident}", "sent": result}
