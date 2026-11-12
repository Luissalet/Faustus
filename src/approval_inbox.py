"""A view and reply parser over existing approval cards; never creates a gate.

Remembered patterns deliberately match tool + complete input + workspace, not
just a tool name. A changed recipient, command or folder gets its own answer.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path

_lock = threading.RLock()
_decisions: dict[tuple[str, str], asyncio.Lock] = {}


def _path():
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "approval_inbox.json"


def _load():
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"rules": {}, "channel": {}, "origins": {}}


def change(fn):
    with _lock:
        data = _load()
        result = fn(data)
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
        return result


def owner_key(owner):
    return str(owner or "").strip().casefold()


_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _legacy_code(kind, identifier):
    return hashlib.sha256(f"{kind}:{identifier}".encode()).hexdigest()[:10].upper()


def code_for(kind, identifier):
    # Persist assignments, including retired ones: a delayed phone reply must
    # never resolve a different card. Linear probing handles hash collisions
    # without renaming codes that have already reached the phone.
    key = f"{kind}:{identifier}"
    with _lock:
        codes = _load().get("codes", {})
        if key in codes:
            return codes[key]
        def allocate(data):
            codes = data.setdefault("codes", {})
            used = set(codes.values())
            size = len(_CODE_ALPHABET) ** 4
            start = int(hashlib.sha256(key.encode()).hexdigest(), 16) % size
            for offset in range(size):
                value = (start + offset) % size
                chars = []
                for _ in range(4):
                    value, digit = divmod(value, len(_CODE_ALPHABET))
                    chars.append(_CODE_ALPHABET[digit])
                code = "".join(reversed(chars))
                if code not in used:
                    codes[key] = code
                    return code
            raise ValueError("No approval codes available")
        return change(allocate)


def parse_reply(text):
    text = unicodedata.normalize("NFKD", str(text or "").casefold())
    text = " ".join("".join(c for c in text if not unicodedata.combining(c)).split())
    if text in ("si a todo", "yes all"):
        return {"all": True, "granted": True, "remember": False}
    if text in ("si", "yes", "no"):
        return {"code": None, "granted": text != "no", "remember": False, "all": False}
    code_pattern = r"([2-9a-hj-np-z]{4}|[a-f0-9]{10})"
    match = re.fullmatch(code_pattern + r" (si|yes|no|siempre si|siempre no|always yes|always no)", text)
    if not match:
        match = re.fullmatch(r"(siempre si|siempre no|always yes|always no) " + code_pattern, text)
        if not match:
            return None
        answer, code = match.groups()
    else:
        code, answer = match.groups()
    return {"code": code.upper(), "granted": answer.endswith(("si", "yes")),
            "remember": answer.startswith(("siempre", "always")), "all": False}


def mark_origin(approval_id, *, kind, name, task_id=""):
    change(lambda data: data.setdefault("origins", {}).__setitem__(approval_id,
           {"kind": kind, "name": name, "task_id": task_id}))


def _pattern(kind, action):
    return hashlib.sha256(json.dumps([kind, action], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _level(effects):
    if set(effects) & {"destructive", "admin_change", "external_side_effect"}:
        return "high"
    if set(effects) & {"execute_code", "network_egress"}:
        return "medium"
    if set(effects) & {"write_workspace", "read_private"}:
        return "low"
    return "trivial"


def pending(owner=None):
    from src.tool_approvals import tool_approval_store
    from src import approval_store
    with _lock:
        origins = _load().get("origins", {})
    rows = []
    for card in tool_approval_store.list_pending(owner=owner):
        # Headless callers without a persisted chat still use their native
        # approval surface. They cannot be resumed by chat_stream.
        if not card.session_id:
            continue
        action = {"tool": card.tool_name, "content": card.content, "workspace": card.workspace,
                  "effects": list(card.effects), "document_id": card.document_id,
                  "document_version": card.document_version, "document_digest": card.document_digest}
        rows.append({"kind": "tool", "id": card.approval_id, "owner": card.owner,
                     "session_id": card.session_id, "title": card.tool_name, "action": action,
                     "preview": card._action_payload().get("command_preview") or card.content,
                     "created_at": card.created_at, "expires_at": card.expires_at,
                     "level": _level(card.effects), "origin": origins.get(card.approval_id, {"kind": "chat"})})
    # Contract cards keep their original grant/deny semantics. Their own
    # executor consumes the grant; they never become a fabricated chat turn.
    from datetime import datetime, timezone
    for card in approval_store.pending(owner=owner or "", limit=200):
        if owner is not None and owner_key(card.owner) != owner_key(owner):
            continue
        if card.expires_at and card.expires_at < datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"):
            continue
        action = card.plan.to_dict()
        rows.append({"kind": "contract", "id": card.id, "owner": card.owner,
                     "session_id": "", "title": card.plan.action, "action": action,
                     "preview": card.plan.detail, "created_at": datetime.fromisoformat(card.requested_at.replace("Z", "+00:00")).timestamp(),
                     "expires_at": None, "level": "medium", "origin": {"kind": "contract"}})
    from src.chat_bridges.turn import get_session, TurnUnavailable
    for row in rows:
        row["code"] = code_for(row["kind"], row["id"])
        if row["origin"]["kind"] == "chat":
            try:
                session = get_session(row["session_id"])
                if session and session.name:
                    row["origin"] = {**row["origin"], "name": session.name}
            except TurnUnavailable:
                pass
        row["pattern"] = _pattern(row["kind"], row["action"])
        row["age_seconds"] = max(0, int(time.time() - row["created_at"]))
    return sorted(rows, key=lambda row: row["created_at"])


def rules(owner):
    with _lock:
        buckets = _load().get("rules", {})
        if owner is None:
            return [rule for bucket in buckets.values() for rule in bucket.values()]
        return list(buckets.get(owner_key(owner), {}).values())


def edit_rule(owner, pattern, granted=None):
    def edit(data):
        buckets = data.setdefault("rules", {})
        targets = list(buckets.values()) if owner is None else [buckets.setdefault(owner_key(owner), {})]
        targets = [rows for rows in targets if pattern in rows]
        if not targets:
            raise ValueError("not_found")
        for rows in targets:
            if granted is None:
                del rows[pattern]
            else:
                rows[pattern]["granted"] = granted
    change(edit)


async def resume_tool(row, granted):
    """Same continuation HTTP path as the companion, including sealed action,
    current privileges, persistence, timeline, background run and single use."""
    import httpx
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
    from src.constants import internal_api_base
    body = {"session": row["session_id"], "message": "", "mode": "agent",
            "tool_approval_id": row["id"], "tool_approval_decision": "approve_action" if granted else "deny"}
    from src.chat_bridges.turn import get_session
    session = get_session(row["session_id"])
    headers = {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN,
               "X-Odysseus-Owner": getattr(session, "owner", None) or row["owner"]}
    client = httpx.AsyncClient(base_url=internal_api_base(), timeout=httpx.Timeout(15, read=1800), trust_env=False)
    try:
        response = await client.send(client.build_request("POST", "/api/chat_stream", json=body, headers=headers), stream=True)
        if not response.is_success:
            raise ValueError((await response.aread()).decode(errors="replace")[:500])
    except BaseException:
        await client.aclose()
        raise
    # Wait for HTTP acceptance, not merely removal from the native store:
    # later route validation may still reject a consumed card. Draining its
    # SSE body must not hold the message parser until model inference ends.
    async def drain():
        try:
            async for _ in response.aiter_bytes():
                pass
        finally:
            await response.aclose()
            await client.aclose()
    task = asyncio.create_task(drain(), name="approval-continuation")
    _running.add(task)
    task.add_done_callback(_running.discard)
    task.add_done_callback(_report_continuation)


_running: set[asyncio.Task] = set()


async def decide(owner, code, *, granted, remember=False):
    key = (owner_key(owner), code.upper())
    lock = _decisions.setdefault(key, asyncio.Lock())
    async with lock:
        matches = [r for r in pending(owner) if code.upper() in
                   (r["code"], _legacy_code(r["kind"], r["id"]))]
        if len(matches) != 1:
            return {"ok": False, "code": code, "reason": "not_found" if not matches else "ambiguous"}
        row = matches[0]
        if row["kind"] == "contract":
            from src import approval_store
            result = approval_store.decide(row["id"], granted=granted, by=owner or row["owner"] or "local")
            if not result.get("ok"):
                return {"code": code, **result}
            from routes.approvals_routes import _notify_decision
            _notify_decision(row["id"], "granted" if granted else "denied", result)
        else:
            # Hold this code until HTTP has accepted/consumed it. Concurrent
            # Studio/channel replies hit the native store's atomic consume.
            await _start_continuation(row, granted)
        if remember:
            rule = {"pattern": row["pattern"], "title": row["title"], "preview": row["preview"],
                    "granted": granted, "updated_at": time.time()}
            change(lambda data: data.setdefault("rules", {}).setdefault(owner_key(row["owner"]), {}).__setitem__(row["pattern"], rule))
        return {"ok": True, "code": code, "granted": granted, "remembered": remember}


async def _start_continuation(row, granted):
    from src.tool_approvals import tool_approval_store
    await resume_tool(row, granted)
    if tool_approval_store.peek(row["id"]) is not None:
        raise ValueError("The continuation did not consume the approval")


def _report_continuation(task):
    import logging
    if not task.cancelled() and task.exception():
        logging.getLogger(__name__).warning("Approval continuation failed: %s", task.exception())


async def reply(owner, text, *, allowed_codes=None):
    parsed = parse_reply(text)
    if parsed is None:
        return {"handled": False}
    rows = pending(owner)
    if not parsed["all"] and parsed["code"] is None:
        # An unqualified yes/no is meaningful only for exactly one pending
        # request, and only once that request was delivered to this channel.
        if len(rows) != 1:
            return {"handled": True, "results": [{"ok": False, "reason": "ambiguous" if rows else "not_found"}]}
        parsed["code"] = rows[0]["code"]
        if allowed_codes is not None and _legacy_code(rows[0]["kind"], rows[0]["id"]) in allowed_codes:
            parsed["code"] = _legacy_code(rows[0]["kind"], rows[0]["id"])
    codes = [r["code"] for r in rows] if parsed["all"] else [parsed["code"]]
    # A channel's 'all' is a snapshot of requests actually delivered there.
    if allowed_codes is not None:
        codes = [code for code in codes if code in allowed_codes]
    results = []
    for code in codes:
        try:
            results.append(await decide(owner, code, granted=parsed["granted"], remember=parsed["remember"]))
        except (ValueError, OSError) as exc:
            results.append({"ok": False, "code": code, "reason": str(exc)})
    return {"handled": True, "results": results}


def message(row):
    origin = row["origin"].get("name") or {"chat": "Conversación", "assistant": "Asistente", "automation": "Automatización", "contract": "Aprobación"}.get(row["origin"]["kind"], row["origin"]["kind"])
    levels = {"trivial": "trivial", "low": "bajo", "medium": "medio", "high": "alto"}
    return (f"Faustus · decisión {row['code']}\n{row['title']} · {origin} · nivel {levels[row['level']]}\n"
            f"{str(row['preview'])[:1200]}\n\nPor ejemplo: {row['code']} sí\nResponde: {row['code']} sí / {row['code']} no / "
            f"{row['code']} siempre sí / {row['code']} siempre no.\n"
            "Si solo hay una petición pendiente, basta con «sí» o «no».\n"
            "«Siempre» recuerda esta misma herramienta, entrada y carpeta. «Sí a todo» resuelve las peticiones recibidas.")
