"""What happened in a finished task, read deterministically (no model call).

Two jobs:

* :func:`load_trajectory` reads a session's recent messages out of the chat
  database (the same place the skills sleep pass reads), with the tool calls
  each assistant message made.
* :func:`assess` decides whether that trajectory has a signal worth spending a
  model call on. A task that simply went well produces NO signal and therefore
  no compute: the cheap pre-filter is what keeps "run after every task" from
  meaning "run a model after every task".

Signals (weight): the user corrected the assistant (3), the user said the same
thing again (2), a tool call failed and was repeated unchanged (2), a tool call
failed (1, up to 3), the assistant asked clarifying questions turn after turn
(1 beyond the first, up to 2). A trajectory is worth a proposal at
``WORTH_SCORE`` or more, so one explicit correction is enough on its own and a
couple of tool hiccups are not.

Only messages newer than ``since_ts`` count as fresh evidence (the older ones
are context): a correction the proposer already looked at never triggers a
second proposal.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

logger = logging.getLogger(__name__)

WORTH_SCORE = 3
MAX_MESSAGES = 40
QUOTE_CHARS = 220

W_CORRECTION = 3
W_REPEAT = 2
W_RETRY = 2
W_FAILURE = 1
W_CLARIFY = 1


@dataclass
class TMsg:
    id: str
    role: str
    content: str
    ts: float
    tool_events: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class Trajectory:
    session_id: str
    owner: str = ""
    messages: List[TMsg] = field(default_factory=list)
    project_id: str = ""
    project_name: str = ""
    since_ts: float = 0.0

    def fresh(self) -> List[TMsg]:
        return [m for m in self.messages if m.ts > self.since_ts]

    @property
    def last_ts(self) -> float:
        return max((m.ts for m in self.messages), default=0.0)


@dataclass
class Assessment:
    worth: bool
    score: int
    reason: str
    signals: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    trigger: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"worth": self.worth, "score": self.score, "reason": self.reason,
                "signals": self.signals, "evidence": self.evidence, "trigger": self.trigger}


def _events_of(raw_meta: Any) -> List[Dict[str, Any]]:
    meta: Any = raw_meta
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return []
    if not isinstance(meta, dict):
        return []
    events = meta.get("tool_events")
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def load_trajectory(session_id: str, owner: Optional[str] = None, *, max_messages: int = MAX_MESSAGES,
                    since_ts: Optional[float] = None) -> Optional[Trajectory]:
    """The last ``max_messages`` messages of a session, oldest first, or None
    when the session does not exist / is not this owner's."""
    from core.database import ChatMessage, Session as DbSession, SessionLocal
    from src.harness_refinement import store

    db = SessionLocal()
    try:
        session = db.query(DbSession).filter(DbSession.id == session_id).first()
        if session is None:
            return None
        if owner and getattr(session, "owner", None) not in (None, "", owner):
            return None
        rows = (db.query(ChatMessage).filter(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.timestamp.desc()).limit(max(2, int(max_messages))).all())
        rows.reverse()
        messages = []
        for row in rows:
            stamp = row.timestamp
            ts = stamp.replace(tzinfo=timezone.utc).timestamp() if stamp is not None and stamp.tzinfo is None \
                else (stamp.timestamp() if stamp is not None else 0.0)
            messages.append(TMsg(id=str(row.id), role=str(row.role or ""), content=str(row.content or ""),
                                 ts=ts, tool_events=_events_of(row.meta_data)))
        traj = Trajectory(session_id=session_id, owner=owner or str(getattr(session, "owner", "") or ""),
                          messages=messages,
                          since_ts=float(store.processed_until(session_id) if since_ts is None else since_ts))
    finally:
        db.close()
    try:
        from services.projects import project_for_session
        project = project_for_session(session_id, owner or None)
        if project:
            traj.project_id = str(project.get("id") or "")
            traj.project_name = str(project.get("name") or "")
    except Exception:  # noqa: BLE001 - a session without a project is normal
        logger.debug("project lookup failed for %s", session_id, exc_info=True)
    return traj


# -- tool event helpers ----------------------------------------------------

def event_args(event: Mapping[str, Any]) -> Dict[str, Any]:
    command = event.get("command")
    if isinstance(command, dict):
        return command
    if isinstance(command, str) and command.strip().startswith("{"):
        try:
            parsed = json.loads(command)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def is_failure(event: Mapping[str, Any]) -> bool:
    exit_code = event.get("exit_code")
    if exit_code not in (0, None, "0"):
        return True
    head = str(event.get("output") or "").lstrip()[:80].lower()
    return head.startswith(("error", "traceback", "exception", "fatal", '{"error"'))


def _quote(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text[:QUOTE_CHARS]


def _evidence(traj: Trajectory, index: int, kind: str, quote: str) -> Dict[str, Any]:
    msg = traj.messages[index]
    return {"id": f"{traj.session_id}:{msg.id}:{kind}", "session_id": traj.session_id,
            "message_id": msg.id, "turn": index, "kind": kind, "quote": _quote(quote)}


def skills_used(traj: Trajectory) -> List[str]:
    names: List[str] = []
    for msg in traj.messages:
        for ev in msg.tool_events:
            if str(ev.get("tool") or "") != "manage_skills":
                continue
            args = event_args(ev)
            if str(args.get("action") or "").lower() in ("view", "view_ref", "edit", "patch"):
                name = str(args.get("name") or args.get("skill_id") or "").strip()
                if name and name not in names:
                    names.append(name)
    return names


def agents_used(traj: Trajectory) -> List[str]:
    slugs: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            agent = node.get("agent")
            if isinstance(agent, str) and agent.strip() and agent.strip() not in slugs:
                slugs.append(agent.strip())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for msg in traj.messages:
        for ev in msg.tool_events:
            tool = str(ev.get("tool") or "").lower()
            if "delegate" in tool or "subagent" in tool or "swarm" in tool:
                walk(event_args(ev))
    return slugs


# -- the pre-filter --------------------------------------------------------

def assess(traj: Optional[Trajectory], *, classify=None, similarity=None) -> Assessment:
    """Score a trajectory. Pure and cheap: no database, no model."""
    if traj is None or len(traj.messages) < 2:
        return Assessment(False, 0, "too_short")
    if classify is None:
        from src.skills_runtime.sleep_optimize import classify_reaction as classify
    if similarity is None:
        from src.memory import get_text_similarity as similarity

    signals: List[Dict[str, Any]] = []
    evidence: List[Dict[str, Any]] = []
    fresh_from = traj.since_ts
    seen_user: List[str] = []
    failures = 0
    retries = 0
    clarifications = 0
    last_event: Optional[Mapping[str, Any]] = None
    last_failed = False

    for i, msg in enumerate(traj.messages):
        is_fresh = msg.ts > fresh_from
        if msg.role == "user":
            text = msg.content.strip()
            prior_assistant = any(m.role == "assistant" for m in traj.messages[:i])
            if prior_assistant and classify(text) == "negative" and is_fresh:
                signals.append({"kind": "correction", "weight": W_CORRECTION, "turn": i})
                evidence.append(_evidence(traj, i, "correction", text))
            words = text.split()
            if is_fresh and len(words) >= 4:
                for earlier in seen_user:
                    if similarity(text, earlier) >= 0.7:
                        signals.append({"kind": "repeat", "weight": W_REPEAT, "turn": i})
                        evidence.append(_evidence(traj, i, "repeat", text))
                        break
            seen_user.append(text)
        elif msg.role == "assistant":
            if not msg.tool_events and msg.content.strip().endswith("?") and is_fresh:
                clarifications += 1
                if clarifications >= 2 and clarifications <= 3:
                    signals.append({"kind": "clarification", "weight": W_CLARIFY, "turn": i})
                    evidence.append(_evidence(traj, i, "clarification", msg.content[-QUOTE_CHARS:]))
            for ev in msg.tool_events:
                failed = is_failure(ev)
                same_call = (last_event is not None and last_failed
                             and str(ev.get("tool")) == str(last_event.get("tool"))
                             and str(ev.get("command")) == str(last_event.get("command")))
                if is_fresh and failed and failures < 3:
                    failures += 1
                    signals.append({"kind": "tool_failure", "weight": W_FAILURE, "turn": i})
                    evidence.append(_evidence(traj, i, "tool_failure",
                                              f"{ev.get('tool')}: {ev.get('output') or ev.get('exit_code')}"))
                if is_fresh and same_call and retries < 2:
                    retries += 1
                    signals.append({"kind": "retry", "weight": W_RETRY, "turn": i})
                    evidence.append(_evidence(traj, i, "retry", f"{ev.get('tool')} repeated unchanged after a failure"))
                last_event, last_failed = ev, failed

    score = sum(int(s["weight"]) for s in signals)
    kinds = sorted({s["kind"] for s in signals})
    if score < WORTH_SCORE:
        return Assessment(False, score, "no_signal" if not signals else "weak_signal", signals, evidence)
    primary = "correction" if "correction" in kinds else kinds[0]
    return Assessment(True, score, "signal", signals, _dedupe(evidence), trigger=f"turn_end:{primary}")


def _dedupe(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for item in items:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        out.append(item)
    return out
