"""synthesis.py — the close of a Council activity, built from the ledger.

The failure this file exists to prevent: a room of models works for twenty
minutes, and the closing text is produced by asking one of them to "summarise
the session". The summary then says the work was verified when no verification
ran, drops the decision that was taken in round two because round five talked
about something else, and turns a participant's unanswered "this is wrong" into
"the group agreed". Every one of those is a state claim, and a state claim
belongs to the ledger (plan 12.2: "El cierre no debe ser una nueva
interpretación libre de todo el transcript. Debe construirse desde el ledger").

So `build()` reads `ledger.snapshot()` and structured evidence -- a `prove`
packet, a usage record, a stop reason -- and nothing else. It calls no model.
It takes no free text to describe the outcome: there is deliberately no
parameter through which a sentence could arrive and become the result. The
`messages` it accepts are counted, never interpreted: they answer "who spoke
and how much", which is a fact about the transcript, not a claim about state.
A model may afterwards rewrite the prose of `render()` for readability -- it
may not add, remove or soften anything in `CouncilSummary`.

Two consequences worth naming:

* Dissent survives. An open objection or a decision with dissenters appears in
  the summary and makes `status_of` say `disputed` (plan 3.4: "el consenso no
  borra el disenso relevante").
* `verified` is never reached without a proof packet whose verdict is
  `proved`. That word belongs to `src/prove.py` -- "nothing else earns this
  word" -- and this module does not lower the bar for it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.convergence import assess as _assess_rounds
from src.memory import get_text_similarity

logger = logging.getLogger(__name__)

__all__ = [
    "CouncilSummary",
    "build",
    "render",
    "convergence",
    "contributions",
    "status_of",
    "evidence_weighted_support",
    "STATUSES",
    "PROVED_VERDICT",
    "MATERIAL_SEVERITIES",
    "NO_EVIDENCE_WEIGHT",
]

#: PLAN-04 — how much a supporter's vote counts when their own message on the
#: decision cites no evidence. Not zero: silence is not proof the supporter
#: is wrong, and a policy that discarded unsupported votes entirely could not
#: represent "everyone agreed but nobody checked" at all — which is exactly
#: the state `unanimous_without_evidence` below exists to name instead.
NO_EVIDENCE_WEIGHT = 0.5

#: The five states a close may report (plan 3.4).
STATUSES = ("decided", "verified", "unverified", "blocked", "disputed")

#: The only verdict from `src/prove.py` that earns `verified`.
PROVED_VERDICT = "proved"
#: A verification that FAILED is not a matter of opinion and not a mere
#: absence of evidence; it needs a person, so it reports as `blocked`.
CONTRADICTED_VERDICT = "contradicted"

#: Severities that keep a close `disputed`. A `note` is a remark; a `concern`
#: or a `blocking` objection is unfinished business.
MATERIAL_SEVERITIES = frozenset({"concern", "blocking"})

#: Task statuses that count as work finished well. Both are in
#: `contracts.TASK_STATUSES` and they are not the same claim: `done` is what the
#: run reported, `verified` is what `prove` returned about a ChangeSet built
#: from what Faustus observed. A close counts both as finished and reports the
#: difference through `verification`.
SATISFIED_TASK_STATUSES = frozenset({"done", "verified"})
#: Task statuses that mean the task will not finish on its own.
STUCK_TASK_STATUSES = frozenset({"blocked", "failed"})
#: Message types that record a participant declining to add anything.
#: `abstention` is the one in `contracts.MESSAGE_TYPES`; `abstain` is here for
#: an invoker that returns the verb instead of the noun, which `orchestrator`
#: maps away but a direct caller of `contributions()` may not.
ABSTENTION_TYPES = frozenset({"abstain", "abstention"})

#: Never leave a close without a stop reason: a close nobody can audit.
UNKNOWN_STOP_REASON = "unknown"


# --- small readers (all total: a close must not fail while being written) ---

def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _row(value: Any) -> Dict[str, Any]:
    """A message/participant/record as a plain dict, whatever it arrived as.

    A bare string is read as an id. The orchestrator's participant list falls
    back to plain ids when the seat rows cannot be read, and a close that
    dropped those rows would report an empty room at exactly the moment the
    room's own state was hardest to read.
    """
    if isinstance(value, str):
        ident = value.strip()
        return {"id": ident} if ident else {}
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return dict(to_dict())
        except Exception:  # noqa: BLE001 - a close never dies on one row
            logger.debug("council synthesis: to_dict failed for %r", type(value), exc_info=True)
    out: Dict[str, Any] = {}
    for field in ("id", "author_id", "author_kind", "message_type", "content", "display_name",
                  "roles", "participant_id", "task_id", "created_at", "decision_id", "metadata"):
        if hasattr(value, field):
            out[field] = getattr(value, field)
    return out


def _rows(values: Any) -> List[Dict[str, Any]]:
    try:
        return [_row(v) for v in (values or ())]
    except TypeError:
        return []


def _snapshot(ledger: Any) -> Dict[str, Any]:
    """The ledger's own view. The ONLY source of state in this module."""
    snapshot = getattr(ledger, "snapshot", None)
    if not callable(snapshot):
        logger.error("council synthesis: %r has no snapshot(); nothing can be built from it", type(ledger))
        return {}
    try:
        data = snapshot()
        return dict(data) if isinstance(data, Mapping) else {}
    except Exception:  # noqa: BLE001 - read path
        logger.exception("council synthesis: ledger snapshot failed")
        return {}


def _live_decisions(snapshot: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [d for d in _rows(snapshot.get("decisions"))
            if _text(d.get("status")) != "superseded"]


def _proof_verdict(proof: Any) -> str:
    if not isinstance(proof, Mapping):
        return ""
    return _text(proof.get("verdict")).lower()


def _material_objections(snapshot: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [o for o in _rows(snapshot.get("open_objections"))
            if _text(o.get("severity")).lower() in MATERIAL_SEVERITIES]


# --- status ---------------------------------------------------------------

def status_of(ledger: Any, *, proof: Any = None) -> str:
    """One of `decided|verified|unverified|blocked|disputed` (plan 3.4).

    The ladder, in order, and why that order:

    1. `disputed` -- an open objection of severity `concern` or `blocking`, or
       a decision in force with dissenters. Unfinished disagreement outranks
       everything else: a close that reports progress over it hides the one
       thing the reader needed.
    2. `blocked` -- a task that is `blocked`/`failed`, a task waiting on a
       dependency that will not arrive, or a proof packet that came back
       `contradicted`. Work that cannot continue without someone deciding.
    3. `verified` -- a proof packet with verdict `proved` AND every task
       finished well. Without a proof packet this rung does not exist: there
       is no argument, no majority and no model output that can substitute for
       it.
    4. `decided` -- no tasks at all and at least one decision in force: a
       deliberation that reached a conclusion and executed nothing.
    5. `unverified` -- everything else: work happened, nothing proves it.
    """
    snapshot = _snapshot(ledger)
    if _material_objections(snapshot):
        return "disputed"
    decisions = _live_decisions(snapshot)
    if any(list(d.get("dissenters") or ()) for d in decisions):
        return "disputed"
    tasks = _rows(snapshot.get("tasks"))
    verdict = _proof_verdict(proof)
    stuck = [t for t in tasks if _text(t.get("status")).lower() in STUCK_TASK_STATUSES]
    if stuck or list(snapshot.get("blocked_task_ids") or ()) or verdict == CONTRADICTED_VERDICT:
        return "blocked"
    finished = [t for t in tasks if _text(t.get("status")).lower() in SATISFIED_TASK_STATUSES]
    if verdict == PROVED_VERDICT and len(finished) == len(tasks):
        return "verified"
    if not tasks and decisions:
        return "decided"
    return "unverified"


def _verification(proof: Any) -> Dict[str, Any]:
    """What verification actually happened, in the packet's own words."""
    if not isinstance(proof, Mapping):
        return {"verdict": "none", "sufficient": False, "source": "none",
                "note": "no proof packet was supplied; nothing here claims verification"}
    verdict = _proof_verdict(proof)
    out: Dict[str, Any] = {
        "verdict": verdict or "none",
        "sufficient": verdict == PROVED_VERDICT,
        "source": _text(proof.get("source")) or "prove",
        "confidence": proof.get("confidence"),
        "uncertainty": list(proof.get("uncertainty") or ()),
        "observations": list(proof.get("observations") or ()),
        "proof_id": _text(proof.get("identity")) or _text(proof.get("proof_id")),
    }
    if not out["sufficient"]:
        out["note"] = f"verdict {out['verdict']!r} does not earn 'verified'"
    return out


# --- who said what, counted (never interpreted) ----------------------------

def contributions(messages: Any, participants: Any) -> List[Dict[str, Any]]:
    """A compact contribution line per participant (plan 12.2).

    Counting is all this does. It does not judge whose contribution was
    useful -- that is a claim about content, and this module does not make
    those. A participant who said nothing still gets a row: silence in a room
    that was asked for opinions is information, and dropping the row hides it.
    """
    rows = _rows(messages)
    known: Dict[str, Dict[str, Any]] = {}
    for participant in _rows(participants):
        pid = _text(participant.get("id")) or _text(participant.get("participant_id"))
        if pid:
            known[pid] = participant

    stats: Dict[str, Dict[str, Any]] = {}

    def entry(pid: str) -> Dict[str, Any]:
        if pid not in stats:
            participant = known.get(pid, {})
            stats[pid] = {
                "participant_id": pid,
                "display_name": _text(participant.get("display_name")) or pid,
                "roles": list(participant.get("roles") or ()),
                "messages": 0,
                "abstentions": 0,
                "types": {},
                "chars": 0,
                "first_message_id": "",
                "last_message_id": "",
            }
        return stats[pid]

    for message in rows:
        pid = _text(message.get("author_id")) or "(unattributed)"
        record = entry(pid)
        kind = _text(message.get("message_type")) or "message"
        record["messages"] += 1
        record["types"][kind] = record["types"].get(kind, 0) + 1
        if kind.lower() in ABSTENTION_TYPES:
            record["abstentions"] += 1
        record["chars"] += len(_text(message.get("content")))
        ident = _text(message.get("id"))
        if ident:
            record["first_message_id"] = record["first_message_id"] or ident
            record["last_message_id"] = ident
    for pid in known:
        entry(pid)
    return sorted(stats.values(), key=lambda r: (-r["messages"], r["participant_id"]))


# --- PLAN-04: evidence, not headcount --------------------------------------

def _message_evidence_refs(message: Mapping[str, Any]) -> Tuple[str, ...]:
    """A message's own evidence citations. Read from `metadata.evidence_refs`
    -- the same open dict every `CouncilMessage` already carries (contracts.py
    §3.2), never a new field on the frozen contract -- so a message written
    before this function existed simply has none, not an error. A caller that
    wants a vote to *count* as evidenced writes to this key; nothing else in
    the council package reads or writes it, so there is nothing to migrate."""
    meta = message.get("metadata")
    if not isinstance(meta, Mapping):
        return ()
    refs = meta.get("evidence_refs") or ()
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        return ()
    return tuple(_text(r) for r in refs if _text(r))


def evidence_weighted_support(decision: Any, messages: Any) -> Dict[str, Any]:
    """PLAN-04: a decision's `supporters` tally, weighted by whether each
    supporter's OWN most recent message on this decision actually cites
    evidence -- never a bare headcount.

    Plan §1.9.3's rule that a room does not substitute agreement for a check
    means a decision's `supporters` list must never be read as "N independent
    confirmations" when it is really "N restatements of the same unsourced
    claim". This is where that distinction becomes a number: every supporter
    who cited nothing counts for `NO_EVIDENCE_WEIGHT` of a vote instead of a
    whole one, and `unanimous_without_evidence` is True exactly when the room
    "agreed" but not one voice pointed at anything -- the literal case this
    requirement's acceptance test names ("three models repeat the same claim,
    no source, and that does not substitute for a check").

    Reads only the room's own messages and the decision's own `supporters` --
    no model call, no new store, matching the rest of this module (module
    docstring: "It calls no model").
    """
    dec = _row(decision)
    supporters = [s for s in (dec.get("supporters") or ()) if _text(s)]
    decision_id = _text(dec.get("id"))
    latest_by_author: Dict[str, Dict[str, Any]] = {}
    for message in _rows(messages):
        if _text(message.get("decision_id")) != decision_id:
            continue
        author = _text(message.get("author_id"))
        if not author:
            continue
        # Messages arrive in the order the ledger recorded them; the last one
        # from a given author on this decision is that supporter's current
        # word, so a later, evidence-free restatement cannot be padded out by
        # an earlier message that happened to cite something.
        latest_by_author[author] = message
    rows: List[Dict[str, Any]] = []
    for supporter_id in supporters:
        message = latest_by_author.get(supporter_id)
        refs = _message_evidence_refs(message) if message is not None else ()
        has_evidence = bool(refs)
        rows.append({
            "id": supporter_id,
            "has_evidence": has_evidence,
            "evidence_refs": list(refs),
            "weight": 1.0 if has_evidence else NO_EVIDENCE_WEIGHT,
        })
    weighted = sum(r["weight"] for r in rows)
    return {
        "decision_id": decision_id,
        "supporters": rows,
        "raw_count": len(rows),
        "weighted_count": round(weighted, 2),
        "with_evidence": sum(1 for r in rows if r["has_evidence"]),
        "unanimous_without_evidence": bool(rows) and all(not r["has_evidence"] for r in rows),
    }


# --- convergence ----------------------------------------------------------

def convergence(rounds: Sequence[Sequence[str]], *, threshold: float = 0.85) -> Dict[str, Any]:
    """Do the last two rounds still add anything (plan 12.3, `convergence`)?

    What `src/convergence.py` already does, and is reused here unchanged: read
    a TREND across an ordered list of round artifacts -- size, change velocity
    and whether successive similarity is rising -- with no model call. It is
    written for a fix loop, where each round leaves ONE artifact (a diff, a
    verification output).

    What this adds: a council round is not one artifact, it is N answers, one
    per participant. So the primary measure here is per-participant, comparing
    each speaker's last answer with their own previous one via
    `src.memory.get_text_similarity` (the repository's Jaccard), and rounds
    whose participant COUNT changed never converge -- a voice that was not
    there before is new information by definition. The trend from
    `src/convergence.py` is computed over the joined rounds and reported as
    corroboration under ``"trend"``; the explicit `threshold` decides, so the
    caller's stopping rule stays the caller's.
    """
    normalized: List[List[str]] = []
    try:
        for entry in (rounds or ()):
            normalized.append([_text(answer) for answer in (entry or ())])
    except TypeError:
        normalized = []
    count = len(normalized)
    base: Dict[str, Any] = {"converged": False, "similarity": 0.0, "threshold": float(threshold),
                            "rounds": count, "compared": 0, "pairs": [], "joint_similarity": 0.0,
                            "unmatched": 0, "trend": {}}
    if count < 2:
        return {**base, "reason": f"{count} round(s) so far; two are needed to compare"}
    previous, current = normalized[-2], normalized[-1]
    pairs = [{"index": i, "similarity": round(get_text_similarity(previous[i], current[i]), 3)}
             for i in range(min(len(previous), len(current)))]
    joint = round(get_text_similarity("\n".join(previous), "\n".join(current)), 3)
    similarity = round(sum(p["similarity"] for p in pairs) / len(pairs), 3) if pairs else joint
    unmatched = abs(len(previous) - len(current))
    try:
        trend = _assess_rounds(["\n".join(entry) for entry in normalized])
    except Exception:  # noqa: BLE001 - corroboration must not break the check
        logger.debug("council synthesis: trend assessment failed", exc_info=True)
        trend = {}
    converged = bool(pairs) and not unmatched and similarity >= float(threshold)
    if converged:
        reason = (f"the last round repeats the previous one (mean similarity {similarity:.2f} "
                  f">= {float(threshold):.2f} across {len(pairs)} participant(s)); another round "
                  f"is unlikely to add anything")
    elif unmatched:
        reason = (f"the two rounds have a different number of answers ({len(previous)} then "
                  f"{len(current)}); a voice that was not there before is new information")
    else:
        reason = (f"the last round still differs from the previous one (mean similarity "
                  f"{similarity:.2f} < {float(threshold):.2f})")
    return {**base, "converged": converged, "similarity": similarity, "compared": len(pairs),
            "pairs": pairs, "joint_similarity": joint, "unmatched": unmatched,
            "trend": trend, "reason": reason}


# --- the summary ----------------------------------------------------------

@dataclass(frozen=True)
class CouncilSummary:
    """The close of an activity, in the order plan 12.2 asks for it.

    Frozen on purpose: once built from the ledger, a summary is a record. A
    caller that wants different wording renders it again; it does not edit the
    stated outcome.
    """

    session_id: str
    result: str
    decisions: Tuple[Dict[str, Any], ...]
    changes: Tuple[Dict[str, Any], ...]
    verification: Dict[str, Any]
    open_objections: Tuple[Dict[str, Any], ...]
    contributions: Tuple[Dict[str, Any], ...]
    usage: Dict[str, Any]
    stop_reason: str
    status: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "result": self.result,
            "decisions": [dict(d) for d in self.decisions],
            "changes": [dict(c) for c in self.changes],
            "verification": dict(self.verification),
            "open_objections": [dict(o) for o in self.open_objections],
            "contributions": [dict(c) for c in self.contributions],
            "usage": dict(self.usage),
            "stop_reason": self.stop_reason,
            "status": self.status,
        }


def _decision_row(decision: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "id": _text(decision.get("id")),
        "question": _text(decision.get("question")),
        "chosen": _text(decision.get("chosen")),
        "rationale": list(decision.get("rationale") or ()),
        "alternatives": list(decision.get("alternatives") or ()),
        "supporters": list(decision.get("supporters") or ()),
        "dissenters": list(decision.get("dissenters") or ()),
        "evidence_refs": list(decision.get("evidence_refs") or ()),
        "supersedes": _text(decision.get("supersedes")),
        "status": _text(decision.get("status")),
    }


def _objection_row(objection: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "id": _text(objection.get("id")),
        # `target_kind` is the contract's name; `target` is the plan's (12.1).
        "target": _text(objection.get("target_kind")) or _text(objection.get("target")),
        "target_id": _text(objection.get("target_id")),
        "author_id": _text(objection.get("author_id")),
        "severity": _text(objection.get("severity")),
        "claim": _text(objection.get("claim")),
        "evidence": list(objection.get("evidence_refs") or objection.get("evidence") or ()),
        "proposed_resolution": _text(objection.get("proposed_resolution")),
        "status": _text(objection.get("status")),
    }


def _changes(snapshot: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """What was produced, read off the tasks and the resources they claimed.

    A change nobody claimed and no task recorded does not appear here, and that
    is the point: this list is what the ledger can account for, not what a
    participant said it did.
    """
    out: List[Dict[str, Any]] = []
    for task in _rows(snapshot.get("tasks")):
        resources = [_text(r) for r in (task.get("claimed_resources") or ()) if _text(r)]
        status = _text(task.get("status"))
        if not resources and status not in SATISFIED_TASK_STATUSES:
            continue
        out.append({
            "task_id": _text(task.get("id")),
            "title": _text(task.get("title")),
            "status": status,
            "owner": _text(task.get("owner_participant_id")),
            "reviewer": _text(task.get("reviewer_participant_id")),
            "resources": resources,
            "run_id": _text(task.get("run_id")),
            "proof_id": _text(task.get("proof_id")),
        })
    return out


def _usage(usage: Any) -> Dict[str, Any]:
    """Consumption, always present. `source` says where the numbers came from,
    because an estimate and a measurement are not the same claim."""
    out: Dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                           "calls": 0, "wall_seconds": 0.0, "source": "unreported"}
    if isinstance(usage, Mapping):
        out.update(usage)
        if _text(out.get("source")) in ("", "unreported"):
            out["source"] = "reported"
    return out


def _result_line(snapshot: Mapping[str, Any], status: str, decisions: Sequence[Mapping[str, Any]],
                 changes: Sequence[Mapping[str, Any]], open_objections: Sequence[Mapping[str, Any]]) -> str:
    """The headline, counted from the ledger.

    It is a tally, not a narrative, and that is deliberate: the moment this
    line is written by a model it stops being checkable.
    """
    tasks = _rows(snapshot.get("tasks"))
    parts: List[str] = []
    if tasks:
        finished = len([t for t in tasks if _text(t.get("status")).lower() in SATISFIED_TASK_STATUSES])
        parts.append(f"{finished} of {len(tasks)} task(s) finished")
    parts.append(f"{len(decisions)} decision(s) in force")
    if changes:
        touched = len({r for change in changes for r in (change.get("resources") or ())})
        parts.append(f"{touched} resource(s) accounted for")
    if open_objections:
        blocking = len([o for o in open_objections if _text(o.get("severity")).lower() == "blocking"])
        parts.append(f"{len(open_objections)} objection(s) open"
                     + (f" ({blocking} blocking)" if blocking else ""))
    dissenting = len([d for d in decisions if d.get("dissenters")])
    if dissenting:
        parts.append(f"{dissenting} decision(s) recorded with dissent")
    return f"{status}: " + "; ".join(parts) + "."


def build(ledger: Any, *, messages: Sequence[Any] = (), participants: Sequence[Any] = (),
          usage: Any = None, stop_reason: str = "", proof: Any = None) -> CouncilSummary:
    """Build the close of an activity FROM THE LEDGER.

    This function calls no model and reads no prose. Its only source of state
    is `ledger.snapshot()`; `proof` is a structured `src/prove.py` packet,
    `usage` a structured consumption record, and `messages` are counted for the
    contribution lines and never parsed for meaning. There is no parameter
    through which a sentence describing the outcome could be passed in, and
    that absence is the point: everything in the returned summary can be
    checked against the ledger that produced it.

    `participants` is the room's seats. It is a separate argument from
    `messages` because `contributions()` needs both to be honest: the transcript
    alone can only report who SPOKE, and a close that lists three contributors
    in a room of five has quietly deleted two abstentions. Passing it is what
    gives a silent participant a row of its own.

    `stop_reason` is never left empty -- an unlabelled close is one nobody can
    audit later, so an absent reason is recorded as ``"unknown"`` rather than
    silently dropped.
    """
    snapshot = _snapshot(ledger)
    session_id = _text(snapshot.get("session_id")) or _text(getattr(ledger, "session_id", ""))
    status = status_of(ledger, proof=proof)
    decisions = [_decision_row(d) for d in _live_decisions(snapshot)]
    # PLAN-04 (Lote 50 wiring): the close is where "consensus reached" is
    # actually declared (`_result_line`/`status_of` below read `decisions`
    # as-is), so this is the one place `evidence_weighted_support` must run
    # for that declaration to ever reflect it — an additive `evidence_support`
    # key on each row, never a change to the existing decision fields a
    # caller may already read.
    message_rows = _rows(messages)
    for row in decisions:
        row["evidence_support"] = evidence_weighted_support(row, message_rows)
    changes = _changes(snapshot)
    open_objections = [_objection_row(o) for o in _rows(snapshot.get("open_objections"))]
    summary = CouncilSummary(
        session_id=session_id,
        result=_result_line(snapshot, status, decisions, changes, open_objections),
        decisions=tuple(decisions),
        changes=tuple(changes),
        verification=_verification(proof),
        open_objections=tuple(open_objections),
        contributions=tuple(contributions(messages, participants)),
        usage=_usage(usage),
        stop_reason=_text(stop_reason) or UNKNOWN_STOP_REASON,
        status=status,
    )
    if snapshot.get("persistence_errors"):
        logger.warning("council synthesis: session %s closed with %s persistence error(s)",
                       session_id, snapshot.get("persistence_errors"))
    return summary


# --- rendering ------------------------------------------------------------

_NONE = "(none recorded)"


def _joined(values: Sequence[Any], empty: str = "none") -> str:
    items = [_text(v) for v in (values or ()) if _text(v)]
    return ", ".join(items) if items else empty


def render(summary: CouncilSummary) -> str:
    """The summary as plain text, in the order of plan 12.2.

    Plain text on purpose: this close is read by a person deciding whether to
    accept the work, and by a diff when the same session is closed twice. No
    markup, no emoji, no decoration that changes between runs.
    """
    lines: List[str] = []
    lines.append(f"Council close - session {summary.session_id or '(unknown)'}")
    lines.append("=" * min(78, max(24, len(lines[0]))))
    lines.append("")

    lines.append("Result")
    lines.append(f"  {summary.result}")
    lines.append(f"  Status: {summary.status}")
    lines.append("")

    lines.append("Decisions")
    if not summary.decisions:
        lines.append(f"  {_NONE}")
    for decision in summary.decisions:
        lines.append(f"  - {decision.get('question') or '(no question recorded)'}")
        lines.append(f"      Chosen: {decision.get('chosen') or '(nothing recorded)'}")
        if decision.get("rationale"):
            lines.append(f"      Because: {_joined(decision.get('rationale'))}")
        if decision.get("alternatives"):
            lines.append(f"      Alternatives: {_joined(decision.get('alternatives'))}")
        lines.append(f"      Supported by: {_joined(decision.get('supporters'))}"
                     f" | Dissent: {_joined(decision.get('dissenters'))}")
        if decision.get("supersedes"):
            lines.append(f"      Supersedes: {decision.get('supersedes')}")
        if decision.get("evidence_refs"):
            lines.append(f"      Evidence: {_joined(decision.get('evidence_refs'))}")
    lines.append("")

    lines.append("Changes and artifacts")
    if not summary.changes:
        lines.append(f"  {_NONE}")
    for change in summary.changes:
        title = change.get("title") or change.get("task_id") or "(untitled task)"
        lines.append(f"  - {title} [{change.get('status') or 'no status'}]"
                     f" owner: {change.get('owner') or 'unassigned'}")
        lines.append(f"      Resources: {_joined(change.get('resources'))}")
        if change.get("run_id") or change.get("proof_id"):
            lines.append(f"      Run: {change.get('run_id') or 'none'}"
                         f" | Proof: {change.get('proof_id') or 'none'}")
    lines.append("")

    verification = summary.verification or {}
    lines.append("Verification")
    lines.append(f"  Verdict: {verification.get('verdict', 'none')}"
                 f" (sufficient for 'verified': {'yes' if verification.get('sufficient') else 'no'})")
    if verification.get("confidence") is not None:
        lines.append(f"  Confidence: {verification.get('confidence')}")
    if verification.get("uncertainty"):
        lines.append(f"  Uncertainty: {len(verification.get('uncertainty'))} item(s)")
        for item in verification.get("uncertainty") or ():
            if isinstance(item, Mapping):
                lines.append(f"      - {_text(item.get('kind'))}: {_text(item.get('detail'))}")
            else:
                lines.append(f"      - {_text(item)}")
    if verification.get("note"):
        lines.append(f"  Note: {verification.get('note')}")
    lines.append("")

    lines.append("Open objections and uncertainties")
    if not summary.open_objections:
        lines.append("  (none open)")
    for objection in summary.open_objections:
        lines.append(f"  - [{objection.get('severity') or 'unrated'}] "
                     f"{objection.get('claim') or '(no claim text)'}")
        lines.append(f"      Against: {objection.get('target') or 'unknown'} "
                     f"{objection.get('target_id') or ''}".rstrip()
                     + f" | By: {objection.get('author_id') or 'unknown'}")
        if objection.get("proposed_resolution"):
            lines.append(f"      Proposed: {objection.get('proposed_resolution')}")
    lines.append("")

    lines.append("Contributions")
    if not summary.contributions:
        lines.append(f"  {_NONE}")
    for row in summary.contributions:
        types = ", ".join(f"{k} {v}" for k, v in sorted((row.get("types") or {}).items()))
        detail = f" ({types})" if types else ""
        abstentions = f", {row.get('abstentions')} abstention(s)" if row.get("abstentions") else ""
        lines.append(f"  - {row.get('display_name') or row.get('participant_id')}: "
                     f"{row.get('messages', 0)} message(s){detail}{abstentions}")
    lines.append("")

    usage = summary.usage or {}
    lines.append("Usage and stop reason")
    lines.append(f"  Tokens in/out/total: {usage.get('input_tokens', 0)}/"
                 f"{usage.get('output_tokens', 0)}/{usage.get('total_tokens', 0)}"
                 f" (source: {usage.get('source', 'unreported')})")
    lines.append(f"  Model calls: {usage.get('calls', 0)}"
                 f" | Wall seconds: {usage.get('wall_seconds', 0)}")
    lines.append(f"  Stop reason: {summary.stop_reason or UNKNOWN_STOP_REASON}")
    return "\n".join(lines)
