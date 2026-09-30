"""steering_journal.py — durable receipts for messages sent to a live run.

A steering message ("steer") is typed while a turn is working and is meant to
reach the model at the turn's next safe point. A "send after" message is held
until the turn ends and is then sent as a new turn. Both used to live only in
memory: a restart lost them, a run that ended before reading them lost them,
and the UI had already said they were delivered.

Every message now has a receipt whose state is folded from the execution
ledger (``src/exec_ledger.py``, ``steer_*`` rows, ``ref`` = receipt id):

    queued        accepted into the run's queue (nothing has read it)
    drained       the run took it off the queue (the loop holds it)
    handed_off    send-after only: given to the client to send as a new turn
    applied       the conversation received it: the loop appended it as a user
                  message (steer) or the client confirmed it sent the turn
                  (send after)
    dropped       it will not be delivered; ``reason`` says why and the text is
                  kept
    redelivered   a dropped message was carried into the context of a later turn

``applied`` is written only by the place that did the thing (the loop event, the
client acknowledgement); ``queued`` and ``drained`` never claim delivery.

Recovery (``recover_after_restart``) drops every message the dead process left
in ``queued``, ``drained`` or ``handed_off`` with an explicit reason, keeping the
text. ``carry_block`` puts dropped-but-unread messages in front of the model on
the next turn, once, and records that it did.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Iterable, List, Optional

from src import exec_ledger

logger = logging.getLogger(__name__)

LIVE_STATES = ("queued", "drained", "handed_off")
_EVENT_FOR_STATE = {
    "queued": "steer_queued", "drained": "steer_drained", "handed_off": "steer_drained",
    "applied": "steer_applied", "dropped": "steer_dropped", "redelivered": "steer_redelivered",
}
_TEXT_MAX = 4000


def new_receipt_id() -> str:
    return uuid.uuid4().hex


def _mode(mode: Any) -> str:
    return "send_after" if str(mode or "") == "send_after" else "steer"


def queued(session_id: str, run_id: str, *, owner: str, text: str, source: str, mode: str,
           receipt_id: Optional[str] = None) -> Dict[str, Any]:
    """Record an accepted message. Returns the receipt; ``durable`` says whether
    the record was actually written (the queue works either way, the receipt is
    honest about it)."""
    rid = receipt_id or new_receipt_id()
    seq = exec_ledger.record(
        "steer_queued", run_id=run_id, session_id=session_id, owner=owner, ref=rid,
        payload={"text": str(text or "")[:_TEXT_MAX], "source": source, "mode": _mode(mode)})
    return {"receipt_id": rid, "state": "queued", "mode": _mode(mode), "durable": seq is not None}


def transition(receipt_id: str, state: str, *, session_id: str = "", run_id: str = "", owner: str = "",
               reason: str = "", mode: str = "") -> bool:
    kind = _EVENT_FOR_STATE[state]
    if not (session_id or run_id):
        base = _latest(receipt_id)
        if base is None:
            return False
        session_id, run_id, owner = base["session_id"], base["run_id"], owner or base["owner"]
    seq = exec_ledger.record(
        kind, run_id=run_id, session_id=session_id, owner=owner, ref=receipt_id,
        payload={"state": state, "reason": str(reason or "")[:300], "mode": mode})
    return seq is not None


def _fold(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for ev in rows:
        rid = ev["ref"]
        rec = out.setdefault(rid, {"receipt_id": rid, "state": "queued", "history": []})
        p = ev["payload"]
        if ev["kind"] == "steer_queued":
            rec.update(session_id=ev["session_id"], run_id=ev["run_id"], owner=ev["owner"], text=p.get("text", ""),
                       source=p.get("source", "user"), mode=p.get("mode", "steer"), queued_at=ev["ts"], reason="",
                       boot_id=ev["boot_id"])
            rec["state"] = "queued"
        else:
            state = p.get("state") or {"steer_drained": "drained", "steer_applied": "applied",
                                       "steer_dropped": "dropped", "steer_redelivered": "redelivered"}[ev["kind"]]
            rec["state"] = state
            rec["reason"] = p.get("reason", "") if state in ("dropped", "redelivered") else rec.get("reason", "")
            rec[f"{state}_at"] = ev["ts"]
            rec["boot_id"] = ev["boot_id"]
        rec.setdefault("session_id", ev["session_id"])
        rec.setdefault("run_id", ev["run_id"])
        rec.setdefault("owner", ev["owner"])
        rec["history"].append({"state": rec["state"], "at": ev["ts"]})
    return out


def _rows(where: str = "", args: tuple = (), limit: int = 2000) -> List[Dict[str, Any]]:
    import sqlite3
    try:
        with exec_ledger._db() as conn:  # noqa: SLF001 - same package, one store
            rows = conn.execute(
                "SELECT * FROM exec_events WHERE kind IN ('steer_queued','steer_drained','steer_applied',"
                "'steer_dropped','steer_redelivered')" + where + " ORDER BY seq LIMIT ?", (*args, limit)).fetchall()
    except sqlite3.Error:
        return []
    return [exec_ledger._row(r) for r in rows]  # noqa: SLF001


def _latest(receipt_id: str) -> Optional[Dict[str, Any]]:
    folded = _fold(_rows(" AND ref = ?", (str(receipt_id),)))
    return folded.get(str(receipt_id))


def get(receipt_id: str, *, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    rec = _latest(receipt_id)
    if rec is None or (owner is not None and rec.get("owner") and rec["owner"] != str(owner)):
        return None
    return rec


def receipts(*, session_id: Optional[str] = None, owner: Optional[str] = None,
             states: Optional[Iterable[str]] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Receipts newest first, folded. ``states`` filters on the current state."""
    where, args = "", ()
    if session_id:
        where, args = " AND session_id = ?", (str(session_id),)
    folded = _fold(_rows(where, args, limit=20000))
    wanted = set(states) if states else None
    out = [r for r in folded.values()
           if (wanted is None or r["state"] in wanted)
           and (owner is None or not r.get("owner") or r["owner"] == str(owner))]
    out.sort(key=lambda r: r.get("queued_at", ""), reverse=True)
    return out[:max(1, min(int(limit), 500))]


def live(session_id: str, *, owner: Optional[str] = None) -> List[Dict[str, Any]]:
    return receipts(session_id=session_id, owner=owner, states=LIVE_STATES, limit=500)


def drop_unsent(session_id: str, run_id: str, *, reason: str, mode: str = "steer") -> List[str]:
    """Drop what a run ended without reading (still ``queued`` / ``drained``)."""
    dropped = []
    for rec in live(session_id):
        if rec.get("run_id") == run_id and rec.get("mode") == mode and rec["state"] in ("queued", "drained"):
            if transition(rec["receipt_id"], "dropped", session_id=session_id, run_id=run_id,
                          owner=rec.get("owner", ""), reason=reason, mode=mode):
                dropped.append(rec["receipt_id"])
    return dropped


def recover_after_restart() -> Dict[str, Any]:
    """Explicitly drop every message the previous process left unresolved."""
    folded = _fold(_rows(limit=20000))
    out: List[Dict[str, str]] = []
    for rec in folded.values():
        if rec["state"] not in LIVE_STATES or rec.get("boot_id", "") == exec_ledger.BOOT_ID:
            continue
        state = rec["state"]
        reason = {
            "queued": "process_restart_before_delivery",
            "drained": "process_restart_after_handoff_unconfirmed",
            "handed_off": "process_restart_client_never_confirmed",
        }[state]
        if transition(rec["receipt_id"], "dropped", session_id=rec.get("session_id", ""), run_id=rec.get("run_id", ""),
                      owner=rec.get("owner", ""), reason=reason, mode=rec.get("mode", "")):
            out.append({"receipt_id": rec["receipt_id"], "from": state, "reason": reason})
    return {"dropped": out}


def undelivered(session_id: str, *, owner: Optional[str] = None) -> List[Dict[str, Any]]:
    """Dropped messages that never reached a later turn: what the user typed
    and the conversation has not seen."""
    return receipts(session_id=session_id, owner=owner, states=("dropped",), limit=50)


CARRY_PREFIX = (
    "The user sent the following message(s) while an earlier turn was still working. They were not delivered "
    "(reason in brackets) and it is not confirmed the earlier turn read them. Treat them as part of the "
    "user's current request:"
)


def carry_block(session_id: str, *, owner: Optional[str] = None) -> Optional[str]:
    """A system line for the preface of the next turn carrying dropped messages,
    once. Recording ``redelivered`` is what stops it repeating."""
    pending = undelivered(session_id, owner=owner)
    if not pending:
        return None
    lines = [CARRY_PREFIX]
    for rec in sorted(pending, key=lambda r: r.get("queued_at", "")):
        text = " ".join(str(rec.get("text") or "").split())
        if not text:
            continue
        lines.append(f"- {text[:1000]} [{rec.get('reason') or 'not delivered'}]")
        transition(rec["receipt_id"], "redelivered", session_id=rec.get("session_id", ""), run_id=rec.get("run_id", ""),
                   owner=rec.get("owner", ""), reason="carried into the next turn's context", mode=rec.get("mode", ""))
    return "\n".join(lines) if len(lines) > 1 else None


__all__ = [
    "CARRY_PREFIX", "acknowledge", "claim_send_after", "LIVE_STATES", "carry_block", "drop_unsent", "get", "live", "new_receipt_id", "queued",
    "receipts", "recover_after_restart", "transition", "undelivered",
]


def claim_send_after(session_id: str, *, owner: Optional[str] = None) -> List[Dict[str, Any]]:
    """Hand the client the "send after" messages whose run has ended, so it can
    send each as a new turn. Marks them ``handed_off`` (given to the client,
    not yet sent); the client's acknowledgement makes them ``applied``."""
    out = []
    for rec in sorted(receipts(session_id=session_id, owner=owner, states=("queued",), limit=500),
                      key=lambda r: r.get("queued_at", "")):
        if rec.get("mode") != "send_after":
            continue
        if transition(rec["receipt_id"], "handed_off", session_id=session_id, run_id=rec.get("run_id", ""),
                      owner=rec.get("owner", ""), mode="send_after", reason="given to the client to send as a new turn"):
            out.append({"receipt_id": rec["receipt_id"], "text": rec.get("text", ""), "source": rec.get("source", "user")})
    return out


def acknowledge(receipt_id: str, *, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The client reports it sent the message as a turn."""
    rec = get(receipt_id, owner=owner)
    if rec is None or rec["state"] != "handed_off":
        return None
    transition(receipt_id, "applied", session_id=rec.get("session_id", ""), run_id=rec.get("run_id", ""),
               owner=rec.get("owner", ""), mode="send_after", reason="the client sent it as a new turn")
    return get(receipt_id, owner=owner)
