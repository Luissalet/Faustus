"""Deliver existing decisions to the paired self-chat, and read literal replies.

No bridge startup, pairing, model call, contact lookup or third-party chat.
The watermark and delivered codes survive restart; old replies cannot approve
new requests. The existing bridge retains its own effect delivery semantics.
"""
import asyncio
import logging
import time

from src import approval_inbox as inbox

logger = logging.getLogger(__name__)


def _channel_state():
    with inbox._lock:
        return dict(inbox._load().get("channel", {}))


def _save_channel(state):
    inbox.change(lambda data: data.__setitem__("channel", state))


async def apply_remembered():
    from src import agent_runs
    busy = set(agent_runs.active_session_ids())
    for row in inbox.pending():
        if row["session_id"] in busy or time.time() - row["created_at"] < 2:
            continue
        rule = next((r for r in inbox.rules(row["owner"]) if r["pattern"] == row["pattern"]), None)
        if rule:
            try:
                await inbox.decide(row["owner"], row["code"], granted=rule["granted"])
            except Exception:
                logger.exception("Remembered approval could not resume")


async def whatsapp_tick():
    from src import whatsapp_bridge as wa
    from src.chat_bridges.turn import resolve_owner
    status = await asyncio.to_thread(wa.probe)
    if not status or status.get("status") != "connected":
        return {"state": "unavailable"}
    own = (status.get("me") or {}).get("jid", "")
    if not own or not own.endswith(("@s.whatsapp.net", "@lid")):
        return {"state": "unavailable"}
    owner = resolve_owner()
    if not owner:
        return {"state": "no_owner"}
    from src.owner_identity import single_user_mode
    if single_user_mode():
        owner = None
    state = _channel_state()
    if state.get("account") != own:
        state = {"account": own, "since": time.time(), "seen": [], "delivered": {}}
        _save_channel(state)
    delivered = state.setdefault("delivered", {})
    messages = await asyncio.to_thread(wa.messages, own, since_hours=24, limit=200)
    seen = set(state.get("seen", []))
    seen_order = list(state.get("seen", []))
    for message in sorted(messages, key=lambda m: (m.get("ts", 0), m.get("id", ""))):
        mid = message.get("id")
        if not mid or mid in seen or message.get("ts", 0) < state["since"]:
            continue
        # Only this account's own, unforwarded text in its self-chat is a
        # decision. A quoted/forwarded instruction from another chat isn't.
        if message.get("chat") != own or not message.get("from_me") or any(message.get(k) for k in ("forwarded", "edited", "deleted")):
            continue
        # Claim before executing; delivery replay cannot execute a second time.
        seen.add(mid)
        seen_order.append(mid)
        state["seen"] = seen_order[-500:]
        _save_channel(state)
        result = await inbox.reply(owner, message.get("text", ""), allowed_codes=set(delivered))
        if result["handled"]:
            count = sum(r.get("ok", False) for r in result["results"])
            text = f"Faustus · {count} decisiones resueltas."
            if any(not r.get("ok") for r in result["results"]):
                text += " Alguna petición ya no está pendiente o no pudo reanudarse. Consulta la bandeja."
            await asyncio.to_thread(wa.send, own, text)
    # Read replies before delivering newly arrived cards: 'yes all' cannot
    # include a request that only reached the phone during this very tick.
    for row in inbox.pending(owner):
        if row["code"] in delivered or time.time() - row["created_at"] < 2:
            continue
        sent = await asyncio.to_thread(wa.send, own, inbox.message(row))
        if sent.get("ok"):
            delivered[row["code"]] = row["id"]
            state["seen"] = (state.get("seen", []) + [sent.get("id", "")])[-500:]
            _save_channel(state)
    # Bound historical delivery state without forgetting active requests.
    active = {r["code"] for r in inbox.pending(owner)}
    state["delivered"] = {k: v for k, v in delivered.items() if k in active}
    _save_channel(state)
    return {"state": "connected", "pending": len(active)}


async def keep_loop():
    while True:
        try:
            await apply_remembered()
            await whatsapp_tick()
        except Exception:
            logger.exception("Approval inbox channel tick failed")
        await asyncio.sleep(3)
