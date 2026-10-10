"""src/whatsapp_commitments.py — promises made in WhatsApp chats, offered to People's Hoard.

Faustus already reads the paired WhatsApp bridge (`src/whatsapp_bridge.py`). This module hands the messages of the chats the
person *enabled* to People's Hoard (the personal address book), which finds the promises in them ("te lo mando el lunes",
"me debes la factura") and keeps them as commitments with the sentence, the date of the message, the contact and the direction.

Privacy, in order of importance:
  * **Off by default.** Nothing leaves this module unless `whatsapp_commitments_enabled` is on, and then only the chats named in
    `whatsapp_commitments_chats` (a chat name, its JID or the contact's phone number) — or every one-to-one chat when the person
    also turned on `whatsapp_commitments_all_chats`. Group chats are never sent.
  * **Message bodies go to one place only**: the `commitments_from_messages` tool of People's Hoard, through the Hoard Link hub's
    tool proxy (`POST /api/apps/people/call`). They are never put on the hub's event bus and never written to a log; the state
    file this module keeps holds counts and a timestamp, not text.
  * Only plain text messages that are not forwarded and not deleted are offered; voice notes, media and reactions are not.
  * People's Hoard skips what it already read (a ledger of message ids), so a window that overlaps the last one is harmless.

A scheduled task with the action `whatsapp_commitments` (src/watchers.py) runs `sync()` periodically; the WhatsApp routes expose
the same for a button (`/api/whatsapp/commitments*`).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src import whatsapp_bridge as wa

logger = logging.getLogger(__name__)

SETTING_ENABLED = "whatsapp_commitments_enabled"
SETTING_ALL = "whatsapp_commitments_all_chats"
SETTING_CHATS = "whatsapp_commitments_chats"

PEOPLE_APP = "people"
PEOPLE_TOOL = "commitments_from_messages"
BATCH_SIZE = 400            # People's Hoard accepts 500 per call
FIRST_WINDOW_HOURS = 24.0   # the first run only looks a day back
MAX_WINDOW_HOURS = 72.0     # and a long pause is never replayed in full
OVERLAP_HOURS = 1.0
CALL_TIMEOUT_S = 90.0
STATE_TASK = "whatsapp_commitments"
STATE_KEY = "cursor"


# ── settings ────────────────────────────────────────────────────────────────

def config() -> Dict[str, Any]:
    """The three settings, normalised. `enabled` is the master switch (default off)."""
    from src import settings
    cfg = settings.load_settings()
    raw = cfg.get(SETTING_CHATS) or []
    chats = [str(c).strip() for c in raw if str(c).strip()] if isinstance(raw, (list, tuple)) else []
    return {"enabled": bool(cfg.get(SETTING_ENABLED, False)), "all_chats": bool(cfg.get(SETTING_ALL, False)), "chats": chats}


def update_config(*, enabled: Optional[bool] = None, all_chats: Optional[bool] = None,
                  chats: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    from src import settings
    patch: Dict[str, Any] = {}
    if enabled is not None:
        patch[SETTING_ENABLED] = bool(enabled)
    if all_chats is not None:
        patch[SETTING_ALL] = bool(all_chats)
    if chats is not None:
        seen: List[str] = []
        for chat in chats:
            text = str(chat).strip()
            if text and text.lower() not in (c.lower() for c in seen):
                seen.append(text[:200])
        patch[SETTING_CHATS] = seen[:200]
    if patch:
        settings.update_settings(patch)
    return config()


# ── which chats ─────────────────────────────────────────────────────────────

def _digits(text: Any) -> str:
    return re.sub(r"\D", "", str(text or ""))


def jid_phone(jid: Any) -> str:
    """'34600111222@s.whatsapp.net' -> '+34600111222'; a group or a hidden id gives ''."""
    m = re.match(r"^(\d{6,})@(?:s\.whatsapp\.net|c\.us)$", str(jid or ""))
    return f"+{m.group(1)}" if m else ""


def is_group(jid: Any) -> bool:
    return str(jid or "").endswith("@g.us")


def chat_enabled(row: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """True when the person turned this chat on (by name, JID or phone), or every one-to-one chat is on."""
    jid = str(row.get("chat") or "")
    if not jid or is_group(jid):
        return False
    if cfg.get("all_chats"):
        return True
    name = str(row.get("chat_name") or "").strip().lower()
    phone = _digits(jid_phone(jid))
    for entry in cfg.get("chats") or []:
        want = str(entry).strip().lower()
        if not want:
            continue
        if want == jid.lower() or (name and want == name):
            return True
        wd = _digits(want)
        if phone and len(wd) >= 6 and wd == phone:
            return True
    return False


# ── batch ───────────────────────────────────────────────────────────────────

def _iso(ts: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def to_batch(rows: Iterable[Dict[str, Any]], cfg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Messages in the shape People's Hoard takes, plus how many were left out and why. Pure: no I/O."""
    batch: List[Dict[str, Any]] = []
    skipped = {"group": 0, "not_enabled": 0, "not_text": 0, "forwarded": 0}
    for m in rows:
        jid = str(m.get("chat") or "")
        if is_group(jid):
            skipped["group"] += 1
            continue
        if not chat_enabled(m, cfg):
            skipped["not_enabled"] += 1
            continue
        text = str(m.get("text") or "").strip()
        if m.get("kind") not in (None, "", "text") or m.get("deleted") or not text:
            skipped["not_text"] += 1
            continue
        if m.get("forwarded") or m.get("is_forwarded"):
            skipped["forwarded"] += 1
            continue
        stamp = _iso(m.get("ts"))
        if not stamp or not m.get("id"):
            skipped["not_text"] += 1
            continue
        item: Dict[str, Any] = {
            "id": str(m["id"]), "chat_id": jid, "contact_name": str(m.get("chat_name") or "")[:200],
            "sender": "me" if m.get("from_me") else "them", "text": text[:4000], "timestamp": stamp,
        }
        phone = jid_phone(jid)
        if phone:
            item["phone"] = phone
        batch.append(item)
    batch.sort(key=lambda it: it["timestamp"])
    return batch, skipped


def _call_people(messages: List[Dict[str, Any]], *, refine: bool) -> Dict[str, Any]:
    """One call to People's Hoard through the hub's tool proxy. Never raises; `status` says what happened."""
    from src import hoard_hub
    body = {"tool": PEOPLE_TOOL, "arguments": {"messages": messages, "refine": bool(refine)}, "timeout_s": CALL_TIMEOUT_S}
    status, parsed, _where = hoard_hub._call("POST", f"/api/apps/{PEOPLE_APP}/call", body, timeout=CALL_TIMEOUT_S + 10)
    if status is None:
        return {"status": "hub_down", "detail": "No se puede contactar con el hub de Hoard Link."}
    if status in (401, 403):
        return {"status": "hub_refused", "detail": "El hub rechazó la llamada (revisa el token del conector Hoard Hub)."}
    if not isinstance(parsed, dict):
        return {"status": "hub_error", "detail": f"Respuesta inesperada del hub (HTTP {status})."}
    if not parsed.get("ok"):
        detail = str(parsed.get("error") or f"HTTP {parsed.get('status') or status}")[:300]
        if re.search(r"unknown tool|herramienta desconocida", detail, re.I):
            return {"status": "people_outdated", "detail": "Esta versión de People's Hoard aún no sabe leer mensajes (falta commitments_from_messages)."}
        if parsed.get("status") in (None, 502, 503, 504) or re.search(r"not reachable|unreachable|no responde", detail, re.I):
            return {"status": "people_down", "detail": "People's Hoard no está en marcha."}
        return {"status": "people_error", "detail": detail}
    result = parsed.get("result")
    return {"status": "ok", "result": result if isinstance(result, dict) else {}}


def _state() -> Dict[str, Any]:
    from src.watchers import load_state
    return load_state(STATE_TASK, STATE_KEY)


def _save(state: Dict[str, Any]) -> None:
    from src.watchers import save_state
    save_state(STATE_TASK, STATE_KEY, state)


def status() -> Dict[str, Any]:
    """Settings plus what the last run did (counts and a time, never text)."""
    state = _state()
    return {
        "config": config(),
        "last_run_at": state.get("last_run_at"), "last_status": state.get("last_status"),
        "last_counts": state.get("last_counts") or {}, "cursor_ts": state.get("cursor_ts"),
    }


def sync(owner: str = "", *, hours: Optional[float] = None, refine: bool = False,
         now: Optional[float] = None) -> Dict[str, Any]:
    """Offer the new messages of the enabled chats to People's Hoard. Safe to repeat and to call while the bridge or People are down.

    status: ok | off | no_chats | bridge_down | hub_down | hub_refused | hub_error | people_down | people_outdated | people_error.
    """
    cfg = config()
    if not cfg["enabled"]:
        return {"status": "off", "detail": "Desactivado: los compromisos de WhatsApp no se envían a People's Hoard hasta que lo actives."}
    if not cfg["all_chats"] and not cfg["chats"]:
        return {"status": "no_chats", "detail": "Activado, pero sin chats elegidos: indica qué chats pueden leerse."}
    clock = time.time() if now is None else now
    state = _state()
    if hours is None:
        last = state.get("cursor_ts")
        hours = FIRST_WINDOW_HOURS if not isinstance(last, (int, float)) else min(
            MAX_WINDOW_HOURS, max(OVERLAP_HOURS, (clock - float(last)) / 3600.0 + OVERLAP_HOURS))
    hours = max(0.25, min(float(hours), 24.0 * 14))
    try:
        rows = wa.messages(None, since_hours=hours, limit=500)
    except wa.BridgeError as exc:
        return {"status": "bridge_down", "detail": str(exc)[:200]}
    rows = [r for r in rows if isinstance(r, dict)]
    batch, skipped = to_batch(rows, cfg)
    totals = {"created": 0, "queued": 0, "duplicates": 0, "linked": 0, "unlinked": 0, "seen": 0}
    commitments: List[Dict[str, Any]] = []
    outcome = "ok"
    detail = ""
    for start in range(0, len(batch), BATCH_SIZE):
        reply = _call_people(batch[start:start + BATCH_SIZE], refine=refine)
        if reply["status"] != "ok":
            outcome, detail = reply["status"], reply.get("detail", "")
            break
        res = reply["result"]
        for key in ("created", "queued", "duplicates", "linked", "unlinked"):
            totals[key] += int(res.get(key) or 0)
        totals["seen"] += int((res.get("skipped") or {}).get("seen") or 0)
        commitments.extend(res.get("commitments") or [])
    stamp = datetime.fromtimestamp(clock, tz=timezone.utc).isoformat(timespec="seconds")
    if outcome == "ok":
        # the cursor only moves on success; People's ledger makes the overlap harmless
        stamps = [float(r.get("ts") or 0) for r in rows]
        if isinstance(state.get("cursor_ts"), (int, float)):
            stamps.append(float(state["cursor_ts"]))
        state["cursor_ts"] = max(stamps, default=clock)
    state.update({"last_run_at": stamp, "last_status": outcome, "last_counts": {**totals, "read": len(rows), "offered": len(batch)}})
    _save(state)
    out: Dict[str, Any] = {
        "status": outcome, "window_hours": round(hours, 2), "read": len(rows), "offered": len(batch), "skipped": skipped,
        **totals, "commitments": commitments,
    }
    if detail:
        out["detail"] = detail
    logger.info("[whatsapp_commitments] %s: read=%d offered=%d created=%d queued=%d", outcome, len(rows), len(batch),
                totals["created"], totals["queued"])
    return out


async def action_whatsapp_commitments(owner: str, **kwargs) -> Tuple[str, bool]:
    """Scheduled action `whatsapp_commitments`: offer the new messages of the enabled chats to People's Hoard and say what was
    recorded. Quiet (a no-op run) when the feature is off, nothing is enabled or nothing new was found."""
    import asyncio
    from src.builtin_actions import TaskNoop
    from src.watchers import parse_params
    params = parse_params(kwargs.get("prompt"), {"refine": False, "always": False})
    result = await asyncio.to_thread(sync, owner, refine=bool(params.get("refine")))
    status = result.get("status")
    if status in ("off", "no_chats"):
        raise TaskNoop(f"whatsapp_commitments: {status}")
    if status != "ok":
        return summary_line(result), False
    if not params.get("always") and not (int(result.get("created") or 0) or int(result.get("queued") or 0)):
        raise TaskNoop(f"whatsapp_commitments: nothing new ({result.get('offered', 0)} messages offered)")
    lines = [summary_line(result)]
    for c in (result.get("commitments") or [])[:10]:
        who = c.get("person_name") or "alguien"
        lines.append(f"- {'Le debes a' if c.get('direction') == 'i_owe' else 'Te debe'} {who}: {c.get('text')}" + (f" (para el {c['due']})" if c.get("due") else ""))
    return "\n".join(lines), True


def summary_line(result: Dict[str, Any]) -> str:
    """One Spanish sentence for a card or a task log; no message text."""
    status = result.get("status")
    if status != "ok":
        return f"Compromisos de WhatsApp: {result.get('detail') or status}"
    created, queued = int(result.get("created") or 0), int(result.get("queued") or 0)
    parts = [f"{created} compromiso{'s' if created != 1 else ''} nuevo{'s' if created != 1 else ''}"]
    if queued:
        parts.append(f"{queued} por revisar en People's Hoard")
    return f"Compromisos de WhatsApp: {', '.join(parts)} (de {result.get('offered', 0)} mensajes de los chats activados)."
