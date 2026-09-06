"""
council/contracts.py — who said it, who may act on it, and what may follow.

The failure this file exists to prevent is already in Faustus and is one line
long.  Group chat hands a peer's answer to the next model like this:

    {"role": "user", "content": "[Claude]: validate the OAuth state client-side"}

Everything that is wrong with a council springs from that line.  The receiving
model is told, in the only channel it trusts, that its *user* said something a
peer said; a model that writes ``Usuario:`` at the top of its answer becomes
the user; and a reviewer that says "I'll just fix it myself" is one prompt away
from writing files, because nothing in the representation says it may not.

So identity, audience, visibility and authority are fields here, never prose.
The plan's central rule (§1.9.3) is the one every shape below serves:

    many models may think, object and review the same matter; only the
    designated owner may execute each effect or modify each resource.

Four rules are inherited from `src/contracts/base.py` for the same reasons a
manifest obeys them: a rejection names the field and the value, an unknown key
is an error and never a default, nothing is coerced across a type boundary, and
every object round-trips — `parse(x.to_dict()) == x` — because all nine of
these go through SQLite, the API and an event stream before anyone reads them.

Three rules only exist here:

5. **A state machine is a declared graph, not a pile of ``if``s.**
   `TRANSITIONS` and `SESSION_TRANSITIONS` are data (§8, §7.1).  A transition
   nobody wrote down is a transition nobody can test, and `check_transition`
   rejects by naming the targets that *are* legal from where you are.

6. **A decision is never edited.**  There is no setter, no ``replace`` and no
   ``update_decision``; `supersede()` returns the old decision marked
   ``superseded`` and a new one pointing back at it (§12).  A decision quietly
   rewritten is a disagreement quietly deleted.

7. **Identity is never inferred from text.**  `looks_like_impersonation()`
   exists so a surface can *warn*; it changes nothing, returns a string, and is
   deliberately not wired into `parse()`.  A message keeps the `author_id` and
   `author_kind` the runtime gave it whatever its first line claims.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from src.contracts.base import (
    SCHEMA_VERSION,
    ContractError,
    as_mapping,
    one_of,
    reject_unknown,
    text,
    text_list,
    timestamp,
    whole,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SCHEMA_VERSION", "POLICIES", "SESSION_STATUSES", "TURN_STATES",
    "TURN_INTERRUPTED", "TURN_STATES_STORED", "TERMINAL_TURN_STATES",
    "TURN_STATES_AT_REST", "ROLES", "TOOL_PROFILES", "AUTHOR_KINDS",
    "MESSAGE_TYPES", "VISIBILITIES", "AUDIENCE_ROOM", "TASK_STATUSES",
    "TERMINAL_TASK_STATUSES", "CLAIM_KINDS", "CLAIM_STATES",
    "ACTIVE_CLAIM_STATES", "OBJECTION_TARGETS", "OBJECTION_SEVERITIES",
    "OBJECTION_STATUSES", "OPEN_OBJECTION_STATUSES", "DECISION_STATUSES",
    "DECISION_TRANSITIONS", "STOP_REASONS", "RESERVED_IDS",
    "TRANSITIONS", "SESSION_TRANSITIONS", "ROLE_TOOL_PROFILES",
    "WRITING_PROFILES", "DEFAULT_BUDGETS",
    "CouncilError",
    "CouncilBudgets", "CouncilSession", "CouncilParticipant", "CouncilMessage",
    "CouncilTask", "CouncilClaim", "CouncilObjection", "CouncilDecision",
    "CouncilTurn",
    "new_id", "check_transition", "can_write", "role_default_profile",
    "looks_like_impersonation", "has_blocking", "supersede", "is_terminal_turn",
]


# ── closed vocabularies ────────────────────────────────────────────────────
#
# Every one of these is closed on purpose.  A role anyone can invent is a role
# no permission table can price; a state anyone can invent is a state no
# recovery pass can classify.  Adding one is a one-line diff and a reason.

#: §4.  What a room does, not what it talks about.
POLICIES: Tuple[str, ...] = (
    "chat", "consult", "debate", "collaborate", "pair", "tournament",
)

#: §7.1.  ``blocked`` is deliberately absent: a blocked activity or decision
#: does not kill the room it happened in.
SESSION_STATUSES: Tuple[str, ...] = (
    "draft", "ready", "active", "paused", "completing", "completed",
    "cancelling", "cancelled", "failed", "interrupted",
)

#: §8.  The life of one user message.
TURN_STATES: Tuple[str, ...] = (
    "received", "classified", "agenda_ready", "participants_selected",
    "awaiting_approval", "running", "speaking", "delegating", "reviewing",
    "verifying", "synthesizing", "completed", "blocked", "cancelled", "failed",
)

#: The mark `persistence.recover()` puts on a turn that was mid-flight when the
#: process died (§15.2).  It is NOT in `TURN_STATES` because no policy may ever
#: *choose* it: a scheduler picks `running`, a coordinator picks `synthesizing`,
#: and only a restart picks this.  It is a node in `TRANSITIONS` and a legal
#: stored value, so `parse()` can read a recovered turn back.
TURN_INTERRUPTED: str = "interrupted"

#: What `CouncilTurn.parse` accepts — the states a policy may choose, plus the
#: one a restart writes.
TURN_STATES_STORED: Tuple[str, ...] = TURN_STATES + (TURN_INTERRUPTED,)

#: Nothing follows these.
TERMINAL_TURN_STATES: Tuple[str, ...] = (
    "completed", "blocked", "cancelled", "failed", TURN_INTERRUPTED,
)

#: States a restart may leave exactly as they are.  The terminal ones plus
#: `awaiting_approval`, which waits for a person and not for a process — a
#: pending approval survives a restart (§15.2, §20 "Integración").
TURN_STATES_AT_REST: Tuple[str, ...] = TERMINAL_TURN_STATES + ("awaiting_approval",)

#: §5.  A role is an expectation and a permission, not a personality.
ROLES: Tuple[str, ...] = (
    "coordinator", "architect", "researcher", "driver", "critic", "tester",
    "reviewer", "judge", "integrator",
)

#: §11.1, ordered least to most dangerous.  A policy may lower a participant's
#: profile; nothing here may raise it above what the user authorised.
TOOL_PROFILES: Tuple[str, ...] = (
    "none", "read_only", "review", "scoped_write", "integrator", "full_with_gates",
)

#: §3.2.  The author kinds a message may carry.  There is no "peer" kind: a
#: peer is a `model`, and what makes it a peer is the reader, not the writer.
AUTHOR_KINDS: Tuple[str, ...] = ("user", "model", "coordinator", "system", "tool")

MESSAGE_TYPES: Tuple[str, ...] = (
    "message", "proposal", "critique", "rebuttal", "synthesis", "decision",
    "objection", "evidence", "status", "abstention",
)

VISIBILITIES: Tuple[str, ...] = ("room", "participant", "coordinator", "system")

#: The audience token that means "everyone in this room" (§7.3 writes
#: ``"audience": ["room"]``).  It is a token and not an empty list because an
#: empty audience is an omission and this is a statement.
AUDIENCE_ROOM: str = "room"

#: §12.1.  `done` and `verified` are two different claims and the difference is
#: the whole point of this list.  `done` is what an executor reported: the run
#: finished.  `verified` is what `prove` returned about a ChangeSet built from
#: what Faustus OBSERVED on disk, and `ledger.set_task_status` refuses it over
#: an open blocking objection.  A vocabulary with only `done` in it would make
#: "the worker said so" and "we checked" the same word, which is exactly the
#: sentence §12.1 forbids a council from writing.
TASK_STATUSES: Tuple[str, ...] = (
    "pending", "claimed", "running", "review", "done", "verified", "blocked",
    "failed", "cancelled",
)

TERMINAL_TASK_STATUSES: Tuple[str, ...] = ("done", "verified", "failed", "cancelled")

CLAIM_KINDS: Tuple[str, ...] = (
    "file", "directory", "artifact", "document", "external", "effect",
)

CLAIM_STATES: Tuple[str, ...] = (
    "requested", "held", "handoff_pending", "released", "expired", "conflicted",
)

#: The states in which a claim still owns its resource.  One active claim per
#: resource is the whole of §3.3, and `persistence` makes it a unique index.
ACTIVE_CLAIM_STATES: Tuple[str, ...] = ("requested", "held", "handoff_pending")

#: §12.1.  An objection points at something that exists; "the design is bad" is
#: not an objection, it is a mood.
OBJECTION_TARGETS: Tuple[str, ...] = ("message", "decision", "task", "changeset")

OBJECTION_SEVERITIES: Tuple[str, ...] = ("note", "concern", "blocking")

OBJECTION_STATUSES: Tuple[str, ...] = (
    "open", "accepted", "rejected_with_reason", "resolved", "withdrawn",
)

#: Still owed an answer.  `accepted` is here on purpose: accepting an objection
#: is agreeing with it, not doing the work it asks for.
OPEN_OBJECTION_STATUSES: Tuple[str, ...] = ("open", "accepted")

DECISION_STATUSES: Tuple[str, ...] = (
    "proposed", "decided", "superseded", "withdrawn",
)

#: §12.3.  Why an activity stopped, chosen from a list, so that "it finished"
#: and "it ran out of budget" are never the same sentence.
STOP_REASONS: Tuple[str, ...] = (
    "completed", "convergence", "judge_verdict", "proof_passed",
    "budget_exhausted", "max_rounds", "max_turns", "user_stopped",
    "blocked", "failed",
)

#: §16.  A participant may not be called `user`, `system` or `tool`: those are
#: author kinds, and a room where a participant id collides with an author kind
#: is a room where a filter can be talked out of its own meaning.
RESERVED_IDS: Tuple[str, ...] = (
    "user", "system", "tool", "model", "coordinator", "assistant", "council",
    "room", "all", "everyone", "none",
)


# ── the state machines, as data ────────────────────────────────────────────
#
# §8 drawn as a graph instead of as branches spread over an orchestrator.  Two
# properties are worth more than the prettiness: a test can walk it (no state
# is unreachable, no invented edge is accepted), and a rejection can name the
# legal targets instead of saying "invalid transition".
#
# Every non-terminal state can reach `blocked`, `cancelled` and `failed`: a
# room can always be stopped, and a user can always take the floor.  Only
# `TURN_INTERRUPTED` is unreachable from the room's own choices — a restart
# writes it, which is exactly why it is not something a policy may pick.

TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "received": ("classified", "blocked", "cancelled", "failed", TURN_INTERRUPTED),
    "classified": ("agenda_ready", "blocked", "cancelled", "failed", TURN_INTERRUPTED),
    "agenda_ready": ("participants_selected", "blocked", "cancelled", "failed",
                     TURN_INTERRUPTED),
    "participants_selected": ("awaiting_approval", "running", "blocked",
                              "cancelled", "failed", TURN_INTERRUPTED),
    # A pending approval is at rest: it waits for a person, so a restart leaves
    # it alone and it never reaches TURN_INTERRUPTED from here.
    "awaiting_approval": ("running", "blocked", "cancelled", "failed"),
    "running": ("speaking", "delegating", "reviewing", "verifying",
                "synthesizing", "blocked", "cancelled", "failed", TURN_INTERRUPTED),
    "speaking": ("running", "reviewing", "synthesizing", "blocked", "cancelled",
                 "failed", TURN_INTERRUPTED),
    "delegating": ("running", "reviewing", "verifying", "synthesizing", "blocked",
                   "cancelled", "failed", TURN_INTERRUPTED),
    "reviewing": ("running", "verifying", "synthesizing", "blocked", "cancelled",
                  "failed", TURN_INTERRUPTED),
    "verifying": ("running", "synthesizing", "blocked", "cancelled", "failed",
                  TURN_INTERRUPTED),
    "synthesizing": ("completed", "blocked", "cancelled", "failed", TURN_INTERRUPTED),
    # `blocked` is not the end of the world: it means a person owes the room an
    # answer.  When they give it, the turn resumes.
    "blocked": ("running", "synthesizing", "cancelled", "failed"),
    "completed": (),
    "cancelled": (),
    "failed": (),
    # A turn that died with the process is not resumed in place.  §15.2: the
    # user gets a recovery card offering resume, review or close, and resuming
    # is a new turn — replaying this one is how a restart repeats an effect.
    TURN_INTERRUPTED: (),
}

#: §7.1.  ``draft → ready → active ↔ paused → completing → completed``, with
#: cancellation and failure reachable from anything still alive, and
#: `interrupted` written by recovery and cleared by the user's choice.
SESSION_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "draft": ("ready", "cancelling", "cancelled", "failed"),
    "ready": ("active", "paused", "cancelling", "cancelled", "failed"),
    "active": ("paused", "completing", "cancelling", "failed", "interrupted"),
    "paused": ("active", "completing", "cancelling", "failed", "interrupted"),
    "completing": ("completed", "cancelling", "failed", "interrupted"),
    "cancelling": ("cancelled", "failed", "interrupted"),
    "completed": (),
    "cancelled": (),
    "failed": (),
    "interrupted": ("active", "paused", "completing", "cancelling", "failed"),
}

#: §12.  The only thing about a decision that may change.  Its content cannot:
#: `supersede()` is the whole editing story.
DECISION_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "proposed": ("decided", "withdrawn"),
    "decided": ("superseded", "withdrawn"),
    "superseded": (),
    "withdrawn": (),
}

#: §5, as a table rather than as a habit.  A critic, a reviewer, a judge and an
#: architect are read-only by default: the plan says so twice (§3.3, §5) because
#: "I will only review, I promise" is not a permission system.  `tester` gets
#: `review` — it may run the suite, not rewrite the code under test.
ROLE_TOOL_PROFILES: Dict[str, str] = {
    "coordinator": "read_only",
    "architect": "read_only",
    "researcher": "read_only",
    "driver": "scoped_write",
    "critic": "read_only",
    "tester": "review",
    "reviewer": "read_only",
    "judge": "read_only",
    "integrator": "integrator",
}

#: The profiles that may modify anything.  Written as the small set rather than
#: as "not in the read-only set": a profile added tomorrow is read-only until
#: somebody puts it here on purpose.
WRITING_PROFILES: Tuple[str, ...] = ("scoped_write", "integrator", "full_with_gates")

#: §7.1.  Every room has limits; a room without them is a bill.
DEFAULT_BUDGETS: Dict[str, int] = {
    "max_rounds": 4,
    "max_turns": 20,
    "max_wall_seconds": 1800,
    "max_total_tokens": 120_000,
    "max_parallel": 2,
}

#: Sanity ceilings.  These are not policy — policy lives in `scheduler.py` —
#: they only stop a typo from asking for ten thousand rounds.
MAX_BUDGET_ROUNDS = 100
MAX_BUDGET_TURNS = 1000
MAX_BUDGET_WALL_SECONDS = 86_400
MAX_BUDGET_TOKENS = 100_000_000
MAX_BUDGET_PARALLEL = 32

MAX_CONTENT_CHARS = 1_000_000


class CouncilError(ContractError):
    """A council contract said no, and said which field and what it saw.

    It is a `ContractError`, so routes that already map contract failures to a
    400 need no new branch; it is its own class so a council-specific handler
    can tell "your manifest is wrong" from "that transition does not exist"."""


# ── small shared readers ───────────────────────────────────────────────────

def new_id(prefix: str) -> str:
    """``turn_9f2c…``.  Short enough to paste into a bug report, long enough
    that two machines writing into one ledger will not collide."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _int(data: Mapping[str, Any], key: str, path: str, *, default: int,
         minimum: int = 0, maximum: Optional[int] = None) -> int:
    """`base.whole` with the `or default` trap removed.

    `whole()` returns `Optional[int]`, and every `whole(...) or 4` in a codebase
    turns a deliberate `0` into `4`.  A budget of zero rounds is a real thing to
    ask for, so it has to survive the reader."""
    value = whole(data, key, path, default=None, minimum=minimum, maximum=maximum)
    return default if value is None else int(value)


def _ts(data: Mapping[str, Any], key: str, path: str) -> str:
    """An ISO-8601 timestamp, or `""` for "not recorded".

    `to_dict()` writes `""` when there is none, so `parse()` must read `""`
    back as absent — otherwise the object cannot re-read its own output, and
    every one of these round-trips through SQLite on every single read."""
    raw = data.get(key, None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    return timestamp(data, key, path, default="") or ""


def _choice(data: Mapping[str, Any], key: str, path: str, *,
            choices: Sequence[str], default: str) -> str:
    """A closed vocabulary where `""` means "not set" and anything else must be
    in the list.  Same round-trip reason as `_ts`: `to_dict()` writes `""` for
    an unset `stop_reason`, and `one_of` would reject its own output."""
    raw = data.get(key, None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    return one_of(data, key, path, choices=choices) or default


def _identifier(data: Mapping[str, Any], key: str, path: str, *,
                required: bool = False, default: str = "",
                reserved: bool = False) -> str:
    """An id the runtime chose.  Deliberately looser than `base.ident` — these
    are `p_claude` and `msg_9f2c…`, not dotted capability names — but a
    participant id is refused when it collides with an author kind (§16)."""
    value = text(data, key, path, required=required, default=default, max_len=128)
    if reserved and value and value.strip().casefold() in RESERVED_IDS:
        raise CouncilError(
            f"{path}.{key}",
            "collides with a reserved name; a participant may not be called one "
            f"of {list(RESERVED_IDS)} because those are author kinds and "
            "audience tokens, and a filter cannot tell them apart afterwards",
            got=value,
        )
    return value


# ── the three questions everyone asks these vocabularies ───────────────────

def check_transition(current: str, target: str, *,
                     graph: Mapping[str, Sequence[str]] = TRANSITIONS) -> None:
    """Raise unless ``current → target`` is an edge the graph declares.

    Naming the legal targets is the whole point.  "invalid transition" sends the
    reader to the source; "cannot move from 'running' to 'draft'; from 'running'
    you may go to [...]" sends them to their bug."""
    where = ("session.status" if graph is SESSION_TRANSITIONS
             else "decision.status" if graph is DECISION_TRANSITIONS
             else "turn.state")
    now = str(current or "").strip()
    want = str(target or "").strip()
    if now not in graph:
        raise CouncilError(
            where, f"unknown state; the graph declares {sorted(graph)}", got=current)
    if want not in graph:
        raise CouncilError(
            where, f"unknown target state; the graph declares {sorted(graph)}", got=target)
    allowed = tuple(graph[now])
    if want in allowed:
        return
    if not allowed:
        raise CouncilError(
            where, f"{now!r} is terminal; nothing follows it", got=target)
    raise CouncilError(
        where,
        f"cannot move from {now!r} to {want!r}; from {now!r} the valid targets "
        f"are {list(allowed)}",
        got=target,
    )


def can_write(profile: str) -> bool:
    """Whether a tool profile may modify anything at all.

    Fails closed.  An unknown profile is not a new kind of permission, it is a
    typo or a newer version of this file talking to an older one, and both of
    those answer "no" (§3.3)."""
    return str(profile or "").strip() in WRITING_PROFILES


def role_default_profile(role: str) -> str:
    """The profile a role gets before anyone narrows it.

    An unknown role gets `none`, not `read_only`: a role this file has never
    heard of has no declared expectations either, and a participant with no
    expectations should be given no tools rather than "only harmless ones"."""
    return ROLE_TOOL_PROFILES.get(str(role or "").strip(), "none")


def is_terminal_turn(state: str) -> bool:
    return str(state or "").strip() in TERMINAL_TURN_STATES


# ── identity is never inferred from text (§3.2) ────────────────────────────
#
# These patterns are for *warning a human*, and they are not called by
# `CouncilMessage.parse`.  That is deliberate: if the parser acted on them, a
# model could change how its own message is stored by choosing its first line,
# which is the exact power this whole file exists to withhold.

_IMPERSONATION_PATTERNS: Tuple[re.Pattern, ...] = (
    # [Claude]:  /  <Claude>:
    re.compile(r"^\s*[\[<]\s*([^\]>\n]{1,64}?)\s*[\]>]\s*:", re.UNICODE),
    # Usuario: / User: / System: / Tool: — a reserved speaker, unbracketed.
    re.compile(
        r"^\s*(usuario|user|sistema|system|asistente|assistant|herramienta|tool|"
        r"coordinador|coordinator|consejo|council)\s*:",
        re.IGNORECASE | re.UNICODE),
    # Claude dijo: / ChatGPT said: / Codex wrote:
    re.compile(
        r"^\s*([^\n:]{1,64}?)\s+(dijo|dice|said|says|wrote|escribió|escribio|"
        r"respondió|respondio|replied)\s*:",
        re.IGNORECASE | re.UNICODE),
)


def looks_like_impersonation(content: str) -> str:
    """The prefix by which a message *claims* an identity, or `""`.

    Advisory only.  A hit means a surface should show "this model wrote a
    speaker label" next to the message; it never means the message changes
    hands.  `author_id` and `author_kind` come from the runtime that made the
    call and from nowhere else.

    Only the first non-blank line is examined: a quoted transcript in the
    middle of an argument is evidence, and flagging it would train everyone to
    ignore the flag."""
    raw = str(content or "")
    if not raw.strip():
        return ""
    for line in raw.splitlines():
        if not line.strip():
            continue
        for pattern in _IMPERSONATION_PATTERNS:
            found = pattern.match(line)
            if found:
                return found.group(0).strip()
        return ""
    return ""


def has_blocking(objections: Iterable[Any]) -> bool:
    """Whether any objection is `blocking` and still owed an answer (§12.1).

    Accepts contracts or plain mappings, because the ledger, the API and a test
    fixture all ask this question and only one of them has parsed objects.  An
    unreadable entry counts as *not* blocking and is logged: this answer gates
    `verified`, and a corrupt row must not be able to block a room forever."""
    for item in objections or ():
        try:
            if isinstance(item, Mapping):
                severity = str(item.get("severity") or "")
                status = str(item.get("status") or "")
            else:
                severity = str(getattr(item, "severity", "") or "")
                status = str(getattr(item, "status", "") or "")
        except Exception:  # noqa: BLE001 - a bad row costs itself, not the room
            logger.warning("council.has_blocking: unreadable objection %r", item)
            continue
        if severity == "blocking" and status in OPEN_OBJECTION_STATUSES:
            return True
    return False


# ── the session ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CouncilBudgets:
    """§3.6.  Cost and latency are part of the contract, so they are a field.

    Zero is a legal value everywhere and means "no more of this": `max_parallel`
    of 0 is a room that has been told to stop starting things, and it must
    survive being read back as 0 rather than becoming the default."""

    max_rounds: int = DEFAULT_BUDGETS["max_rounds"]
    max_turns: int = DEFAULT_BUDGETS["max_turns"]
    max_wall_seconds: int = DEFAULT_BUDGETS["max_wall_seconds"]
    max_total_tokens: int = DEFAULT_BUDGETS["max_total_tokens"]
    max_parallel: int = DEFAULT_BUDGETS["max_parallel"]
    _KEYS = ("max_rounds", "max_turns", "max_wall_seconds", "max_total_tokens",
             "max_parallel")

    @classmethod
    def parse(cls, raw: Any, path: str = "budgets") -> "CouncilBudgets":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            max_rounds=_int(data, "max_rounds", path,
                            default=DEFAULT_BUDGETS["max_rounds"],
                            maximum=MAX_BUDGET_ROUNDS),
            max_turns=_int(data, "max_turns", path,
                           default=DEFAULT_BUDGETS["max_turns"],
                           maximum=MAX_BUDGET_TURNS),
            max_wall_seconds=_int(data, "max_wall_seconds", path,
                                  default=DEFAULT_BUDGETS["max_wall_seconds"],
                                  maximum=MAX_BUDGET_WALL_SECONDS),
            max_total_tokens=_int(data, "max_total_tokens", path,
                                  default=DEFAULT_BUDGETS["max_total_tokens"],
                                  maximum=MAX_BUDGET_TOKENS),
            max_parallel=_int(data, "max_parallel", path,
                              default=DEFAULT_BUDGETS["max_parallel"],
                              maximum=MAX_BUDGET_PARALLEL),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_rounds": self.max_rounds,
            "max_turns": self.max_turns,
            "max_wall_seconds": self.max_wall_seconds,
            "max_total_tokens": self.max_total_tokens,
            "max_parallel": self.max_parallel,
        }


@dataclass(frozen=True)
class CouncilSession:
    """§7.1.  The room: who owns it, what it is for, and what it may spend.

    `revision` is the field that stops two contradictory orders from both
    winning (§8).  It is not decoration and it is not a counter for the UI: a
    `pause` and a `change policy` arriving together must not both apply to the
    state neither of them saw, so every write states the revision it read.

    `phase` is free text on purpose.  The phase vocabulary belongs to the
    policy that invented it (`proposal`, `critique`, `execution`, …) and
    `policies.py` owns those names; closing the list here would mean this file
    had to change every time somebody adds a policy."""

    id: str = ""
    owner: str = ""
    title: str = ""
    policy: str = "chat"
    status: str = "draft"
    phase: str = ""
    parent_session_id: str = ""
    workspace: str = ""
    project_id: str = ""
    participants: Tuple[str, ...] = ()
    budgets: CouncilBudgets = field(default_factory=CouncilBudgets)
    activity_completion_mode: str = ""
    created_at: str = ""
    updated_at: str = ""
    revision: int = 1
    _KEYS = ("id", "owner", "title", "policy", "status", "phase",
             "parent_session_id", "workspace", "project_id", "participants",
             "budgets", "activity_completion_mode", "created_at", "updated_at",
             "revision")

    @classmethod
    def parse(cls, raw: Any, path: str = "council_session") -> "CouncilSession":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            id=_identifier(data, "id", path, default=new_id("council")) or new_id("council"),
            owner=text(data, "owner", path, required=False, max_len=256),
            title=text(data, "title", path, required=False, max_len=512),
            policy=_choice(data, "policy", path, choices=POLICIES, default="chat"),
            status=_choice(data, "status", path, choices=SESSION_STATUSES, default="draft"),
            phase=text(data, "phase", path, required=False, max_len=64),
            parent_session_id=_identifier(data, "parent_session_id", path),
            workspace=text(data, "workspace", path, required=False, max_len=1024),
            project_id=_identifier(data, "project_id", path),
            participants=text_list(data, "participants", path, max_items=64, max_len=128),
            budgets=CouncilBudgets.parse(data.get("budgets") or {}, f"{path}.budgets"),
            # Completion modes are resolved by the agent-profile layer; this
            # field carries the name it chose, it does not re-declare the list.
            activity_completion_mode=text(data, "activity_completion_mode", path,
                                          required=False, max_len=64),
            created_at=_ts(data, "created_at", path),
            updated_at=_ts(data, "updated_at", path),
            revision=_int(data, "revision", path, default=1, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "owner": self.owner, "title": self.title,
            "policy": self.policy, "status": self.status, "phase": self.phase,
            "parent_session_id": self.parent_session_id,
            "workspace": self.workspace, "project_id": self.project_id,
            "participants": list(self.participants),
            "budgets": self.budgets.to_dict(),
            "activity_completion_mode": self.activity_completion_mode,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class CouncilParticipant:
    """§7.2.  One voice in the room, with its own resolved execution.

    `roles` decides what is expected; `tool_profile` decides what is possible,
    and the two are separate fields because a room may narrow the second
    without lying about the first — a driver asked to stop writing is still the
    driver, and the ledger should say so.

    `status` is free text: availability comes from State Mirror and the Immune
    System, and inventing a closed list here would put this file in the way of
    theirs."""

    id: str = ""
    display_name: str = ""
    kind: str = "model"
    model: str = ""
    endpoint_id: str = ""
    agent_slug: str = ""
    roles: Tuple[str, ...] = ()
    tool_profile: str = "none"
    status: str = "available"
    private_session_id: str = ""
    capabilities: Tuple[str, ...] = ()
    resolution_id: str = ""
    completion_mode: str = ""
    _KEYS = ("id", "display_name", "kind", "model", "endpoint_id", "agent_slug",
             "roles", "tool_profile", "status", "private_session_id",
             "capabilities", "resolution_id", "completion_mode")

    @classmethod
    def parse(cls, raw: Any, path: str = "participant") -> "CouncilParticipant":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            id=_identifier(data, "id", path, default=new_id("p"), reserved=True)
               or new_id("p"),
            display_name=text(data, "display_name", path, required=False, max_len=128),
            kind=_choice(data, "kind", path, choices=AUTHOR_KINDS, default="model"),
            model=text(data, "model", path, required=False, max_len=256),
            endpoint_id=_identifier(data, "endpoint_id", path),
            agent_slug=text(data, "agent_slug", path, required=False, max_len=128),
            roles=text_list(data, "roles", path, choices=ROLES, max_items=len(ROLES)),
            tool_profile=_choice(data, "tool_profile", path, choices=TOOL_PROFILES,
                                 default="none"),
            status=text(data, "status", path, required=False,
                        default="available", max_len=64),
            private_session_id=_identifier(data, "private_session_id", path),
            capabilities=text_list(data, "capabilities", path, max_items=64, max_len=64),
            resolution_id=_identifier(data, "resolution_id", path),
            completion_mode=text(data, "completion_mode", path, required=False, max_len=64),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "display_name": self.display_name, "kind": self.kind,
            "model": self.model, "endpoint_id": self.endpoint_id,
            "agent_slug": self.agent_slug, "roles": list(self.roles),
            "tool_profile": self.tool_profile, "status": self.status,
            "private_session_id": self.private_session_id,
            "capabilities": list(self.capabilities),
            "resolution_id": self.resolution_id,
            "completion_mode": self.completion_mode,
        }

    def may_write(self) -> bool:
        """What the participant may do *now* — the profile it actually holds,
        never the profile its roles would suggest.  A reviewer handed
        `scoped_write` by an explicit user authorisation writes; a driver
        narrowed to `read_only` does not."""
        return can_write(self.tool_profile)

    def suggested_profile(self) -> str:
        """The most permissive profile this participant's roles justify, for a
        resolver to *propose*.  Proposing is all it does: §11.1 lets a policy
        lower a profile and never lets one raise it."""
        best = "none"
        for role in self.roles:
            candidate = role_default_profile(role)
            if TOOL_PROFILES.index(candidate) > TOOL_PROFILES.index(best):
                best = candidate
        return best


# ── what was said ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CouncilMessage:
    """§3.2, §7.3.  A typed utterance: author, audience, kind and visibility.

    Append-only by construction.  There is no `update` here and none in the
    store: a message that can be rewritten is a transcript that cannot be
    audited, and a correction is a new message with `reply_to` set.

    `audience` and `visibility` answer two different questions and both are
    load-bearing.  Visibility is *who may read it* — the whole room, one
    participant, the coordinator, the system.  Audience is *who it is for*, and
    a blind round is exactly a message whose visibility is `participant` and
    whose audience is one id.  `persistence.list_messages` enforces it; this
    models it, so the rule has one definition rather than two."""

    id: str = ""
    session_id: str = ""
    turn_id: str = ""
    author_id: str = ""
    author_kind: str = "model"
    audience: Tuple[str, ...] = ()
    message_type: str = "message"
    content: str = ""
    reply_to: str = ""
    task_id: str = ""
    decision_id: str = ""
    visibility: str = "room"
    created_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    _KEYS = ("id", "session_id", "turn_id", "author_id", "author_kind",
             "audience", "message_type", "content", "reply_to", "task_id",
             "decision_id", "visibility", "created_at", "metadata")

    @classmethod
    def parse(cls, raw: Any, path: str = "message") -> "CouncilMessage":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            id=_identifier(data, "id", path, default=new_id("msg")) or new_id("msg"),
            session_id=_identifier(data, "session_id", path),
            turn_id=_identifier(data, "turn_id", path),
            # NOT validated against the content.  Whatever the text claims, the
            # author is who the runtime says it is (§3.2).
            author_id=_identifier(data, "author_id", path),
            author_kind=_choice(data, "author_kind", path, choices=AUTHOR_KINDS,
                                default="model"),
            audience=text_list(data, "audience", path, max_items=64, max_len=128),
            message_type=_choice(data, "message_type", path, choices=MESSAGE_TYPES,
                                 default="message"),
            content=text(data, "content", path, required=False,
                         max_len=MAX_CONTENT_CHARS, allow_blank=True),
            reply_to=_identifier(data, "reply_to", path),
            task_id=_identifier(data, "task_id", path),
            decision_id=_identifier(data, "decision_id", path),
            visibility=_choice(data, "visibility", path, choices=VISIBILITIES,
                               default="room"),
            created_at=_ts(data, "created_at", path),
            metadata=dict(as_mapping(data.get("metadata") or {}, f"{path}.metadata")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "session_id": self.session_id, "turn_id": self.turn_id,
            "author_id": self.author_id, "author_kind": self.author_kind,
            "audience": list(self.audience), "message_type": self.message_type,
            "content": self.content, "reply_to": self.reply_to,
            "task_id": self.task_id, "decision_id": self.decision_id,
            "visibility": self.visibility, "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    def is_visible_to(self, viewer_id: str, *, is_owner: bool = False) -> bool:
        """Whether one reader may see this message.  Half of the blind round.

        The rules, in the order they are checked:

        * the session owner and the audit path see everything — a room the user
          cannot fully read is a room they cannot supervise (§3.1);
        * an author always sees what it wrote;
        * `room` visibility is the room's, regardless of who it is addressed to
          — an `@mention` directs a reply, it does not hide the message;
        * anything narrower is for its audience alone, and `room` in the
          audience widens it back to everyone.

        An empty `viewer_id` is nobody, not everybody: the caller that wants the
        unfiltered transcript asks for it by name (`is_owner=True`), so that
        forgetting to pass the viewer fails closed."""
        if is_owner:
            return True
        viewer = str(viewer_id or "").strip()
        if not viewer:
            return False
        if viewer == self.author_id:
            return True
        if self.visibility == "room":
            return True
        return viewer in self.audience or AUDIENCE_ROOM in self.audience

    def claims_identity(self) -> str:
        """The speaker label this message's text claims, or `""` — a warning a
        surface can render, never a fact the store acts on."""
        return looks_like_impersonation(self.content)


@dataclass(frozen=True)
class CouncilTurn:
    """§8.  One user message, and everything the room did about it.

    `idempotency_key` is the anti-duplicate mechanism, not a nicety: a retried
    POST, a reconnecting client and a restarted process must all land on the
    same turn, or a room answers twice and pays twice (§15.3, §20).

    `stop_reason` is empty until the turn stops, and then it is one of
    `STOP_REASONS`.  A restart does not invent one: `persistence.recover()`
    moves the state to `interrupted` and leaves this field alone, because "the
    process died" is not a reason the room reached a conclusion."""

    id: str = ""
    session_id: str = ""
    state: str = "received"
    author_id: str = ""
    content: str = ""
    policy: str = "chat"
    selected_participants: Tuple[str, ...] = ()
    idempotency_key: str = ""
    stop_reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    _KEYS = ("id", "session_id", "state", "author_id", "content", "policy",
             "selected_participants", "idempotency_key", "stop_reason",
             "created_at", "updated_at")

    @classmethod
    def parse(cls, raw: Any, path: str = "turn") -> "CouncilTurn":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            id=_identifier(data, "id", path, default=new_id("turn")) or new_id("turn"),
            session_id=_identifier(data, "session_id", path),
            state=_choice(data, "state", path, choices=TURN_STATES_STORED,
                          default="received"),
            author_id=_identifier(data, "author_id", path),
            content=text(data, "content", path, required=False,
                         max_len=MAX_CONTENT_CHARS, allow_blank=True),
            policy=_choice(data, "policy", path, choices=POLICIES, default="chat"),
            selected_participants=text_list(data, "selected_participants", path,
                                            max_items=64, max_len=128),
            idempotency_key=text(data, "idempotency_key", path,
                                 required=False, max_len=256),
            stop_reason=_choice(data, "stop_reason", path, choices=STOP_REASONS,
                                default=""),
            created_at=_ts(data, "created_at", path),
            updated_at=_ts(data, "updated_at", path),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "session_id": self.session_id, "state": self.state,
            "author_id": self.author_id, "content": self.content,
            "policy": self.policy,
            "selected_participants": list(self.selected_participants),
            "idempotency_key": self.idempotency_key,
            "stop_reason": self.stop_reason,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    def is_terminal(self) -> bool:
        return is_terminal_turn(self.state)


# ── who owns what ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CouncilTask:
    """§7.4.  A unit of work with exactly one owner and one reviewer.

    `owner_participant_id` is the answer to "who may write this?" and
    `reviewer_participant_id` to "who checks it?".  They are two fields because
    §5 forbids the same participant being the implementer and the only judge of
    its own work when an alternative exists — a rule a single `assignee` field
    could not even express."""

    id: str = ""
    session_id: str = ""
    title: str = ""
    instruction: str = ""
    owner_participant_id: str = ""
    reviewer_participant_id: str = ""
    status: str = "pending"
    depends_on: Tuple[str, ...] = ()
    claimed_resources: Tuple[str, ...] = ()
    acceptance: Tuple[str, ...] = ()
    run_id: str = ""
    proof_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    _KEYS = ("id", "session_id", "title", "instruction", "owner_participant_id",
             "reviewer_participant_id", "status", "depends_on",
             "claimed_resources", "acceptance", "run_id", "proof_id",
             "created_at", "updated_at")

    @classmethod
    def parse(cls, raw: Any, path: str = "task") -> "CouncilTask":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            id=_identifier(data, "id", path, default=new_id("task")) or new_id("task"),
            session_id=_identifier(data, "session_id", path),
            title=text(data, "title", path, required=False, max_len=512),
            instruction=text(data, "instruction", path, required=False,
                             max_len=MAX_CONTENT_CHARS, allow_blank=True),
            owner_participant_id=_identifier(data, "owner_participant_id", path),
            reviewer_participant_id=_identifier(data, "reviewer_participant_id", path),
            status=_choice(data, "status", path, choices=TASK_STATUSES, default="pending"),
            depends_on=text_list(data, "depends_on", path, max_items=64, max_len=128),
            claimed_resources=text_list(data, "claimed_resources", path,
                                        max_items=256, max_len=1024),
            acceptance=text_list(data, "acceptance", path, max_items=64, max_len=1024),
            run_id=_identifier(data, "run_id", path),
            proof_id=_identifier(data, "proof_id", path),
            created_at=_ts(data, "created_at", path),
            updated_at=_ts(data, "updated_at", path),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "session_id": self.session_id, "title": self.title,
            "instruction": self.instruction,
            "owner_participant_id": self.owner_participant_id,
            "reviewer_participant_id": self.reviewer_participant_id,
            "status": self.status, "depends_on": list(self.depends_on),
            "claimed_resources": list(self.claimed_resources),
            "acceptance": list(self.acceptance), "run_id": self.run_id,
            "proof_id": self.proof_id, "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES


@dataclass(frozen=True)
class CouncilClaim:
    """§11.2.  One holder, one resource, one moment.

    This is a *ledger row*, not a lock.  `FileLockRegistry` in
    `agent_tools/subagent_tools.py` is still the thing that stops a write; a
    second lock implementation with its own idea of who holds what is the
    failure mode §11.2 names outright.  What this adds is the part a registry
    living inside one delegation cannot have: an auditable record that outlives
    the run, survives a restart, and says who held what and why."""

    id: str = ""
    session_id: str = ""
    kind: str = "file"
    resource: str = ""
    holder_id: str = ""
    state: str = "requested"
    task_id: str = ""
    acquired_at: str = ""
    expires_at: str = ""
    note: str = ""
    _KEYS = ("id", "session_id", "kind", "resource", "holder_id", "state",
             "task_id", "acquired_at", "expires_at", "note")

    @classmethod
    def parse(cls, raw: Any, path: str = "claim") -> "CouncilClaim":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            id=_identifier(data, "id", path, default=new_id("claim")) or new_id("claim"),
            session_id=_identifier(data, "session_id", path),
            kind=_choice(data, "kind", path, choices=CLAIM_KINDS, default="file"),
            # Not normalised here: normalisation needs the workspace root, and
            # a contract that guesses one would resolve two different rooms'
            # `src/auth.py` to the same claim (§16).
            resource=text(data, "resource", path, required=False, max_len=2048),
            holder_id=_identifier(data, "holder_id", path),
            state=_choice(data, "state", path, choices=CLAIM_STATES, default="requested"),
            task_id=_identifier(data, "task_id", path),
            acquired_at=_ts(data, "acquired_at", path),
            expires_at=_ts(data, "expires_at", path),
            note=text(data, "note", path, required=False, max_len=1024),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "session_id": self.session_id, "kind": self.kind,
            "resource": self.resource, "holder_id": self.holder_id,
            "state": self.state, "task_id": self.task_id,
            "acquired_at": self.acquired_at, "expires_at": self.expires_at,
            "note": self.note,
        }

    def is_active(self) -> bool:
        return self.state in ACTIVE_CLAIM_STATES


@dataclass(frozen=True)
class CouncilObjection:
    """§12.1.  A disagreement that points at something.

    `target_kind` and `target_id` are required in spirit and checked by the
    ledger: an objection with no target cannot be resolved, cannot be tested
    against, and cannot block anything — it is a mood with a row id.

    `status` never becomes `resolved` by silence.  §25: do not declare consensus
    because nobody answered an objection."""

    id: str = ""
    session_id: str = ""
    target_kind: str = "message"
    target_id: str = ""
    author_id: str = ""
    severity: str = "concern"
    claim: str = ""
    evidence_refs: Tuple[str, ...] = ()
    proposed_resolution: str = ""
    status: str = "open"
    created_at: str = ""
    resolved_at: str = ""
    resolution_note: str = ""
    _KEYS = ("id", "session_id", "target_kind", "target_id", "author_id",
             "severity", "claim", "evidence_refs", "proposed_resolution",
             "status", "created_at", "resolved_at", "resolution_note")

    @classmethod
    def parse(cls, raw: Any, path: str = "objection") -> "CouncilObjection":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            id=_identifier(data, "id", path, default=new_id("obj")) or new_id("obj"),
            session_id=_identifier(data, "session_id", path),
            target_kind=_choice(data, "target_kind", path, choices=OBJECTION_TARGETS,
                                default="message"),
            target_id=_identifier(data, "target_id", path),
            author_id=_identifier(data, "author_id", path),
            severity=_choice(data, "severity", path, choices=OBJECTION_SEVERITIES,
                             default="concern"),
            claim=text(data, "claim", path, required=False, max_len=8192, allow_blank=True),
            evidence_refs=text_list(data, "evidence_refs", path,
                                    max_items=64, max_len=1024),
            proposed_resolution=text(data, "proposed_resolution", path,
                                     required=False, max_len=8192, allow_blank=True),
            status=_choice(data, "status", path, choices=OBJECTION_STATUSES,
                           default="open"),
            created_at=_ts(data, "created_at", path),
            resolved_at=_ts(data, "resolved_at", path),
            resolution_note=text(data, "resolution_note", path, required=False,
                                 max_len=4096, allow_blank=True),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "session_id": self.session_id,
            "target_kind": self.target_kind, "target_id": self.target_id,
            "author_id": self.author_id, "severity": self.severity,
            "claim": self.claim, "evidence_refs": list(self.evidence_refs),
            "proposed_resolution": self.proposed_resolution,
            "status": self.status, "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "resolution_note": self.resolution_note,
        }

    def blocks_verification(self) -> bool:
        return self.severity == "blocking" and self.status in OPEN_OBJECTION_STATUSES


@dataclass(frozen=True)
class CouncilDecision:
    """§7.5, §12.  What was decided, by whom, over whose objection.

    **This object has no editing surface at all.**  No setter, no `replace`, no
    `with_status`, and — deliberately — no `update_decision` in the store.  A
    decision that can be edited is a room in which a dissent can be removed
    after the fact, and `dissenters` is the field that costs the most and is
    the easiest to lose.

    Revising a decision is `supersede()`: the old one is marked `superseded`
    and the new one names it in `supersedes`.  Both rows stay."""

    id: str = ""
    session_id: str = ""
    question: str = ""
    status: str = "proposed"
    chosen: str = ""
    alternatives: Tuple[str, ...] = ()
    rationale: Tuple[str, ...] = ()
    supporters: Tuple[str, ...] = ()
    dissenters: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    supersedes: str = ""
    created_at: str = ""
    _KEYS = ("id", "session_id", "question", "status", "chosen", "alternatives",
             "rationale", "supporters", "dissenters", "evidence_refs",
             "supersedes", "created_at")

    @classmethod
    def parse(cls, raw: Any, path: str = "decision") -> "CouncilDecision":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            id=_identifier(data, "id", path, default=new_id("decision")) or new_id("decision"),
            session_id=_identifier(data, "session_id", path),
            question=text(data, "question", path, required=False,
                          max_len=4096, allow_blank=True),
            status=_choice(data, "status", path, choices=DECISION_STATUSES,
                           default="proposed"),
            chosen=text(data, "chosen", path, required=False,
                        max_len=8192, allow_blank=True),
            alternatives=text_list(data, "alternatives", path, max_items=32, max_len=4096),
            rationale=text_list(data, "rationale", path, max_items=64, max_len=4096),
            supporters=text_list(data, "supporters", path, max_items=64, max_len=128),
            dissenters=text_list(data, "dissenters", path, max_items=64, max_len=128),
            evidence_refs=text_list(data, "evidence_refs", path, max_items=64, max_len=1024),
            supersedes=_identifier(data, "supersedes", path),
            created_at=_ts(data, "created_at", path),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "session_id": self.session_id,
            "question": self.question, "status": self.status,
            "chosen": self.chosen, "alternatives": list(self.alternatives),
            "rationale": list(self.rationale), "supporters": list(self.supporters),
            "dissenters": list(self.dissenters),
            "evidence_refs": list(self.evidence_refs),
            "supersedes": self.supersedes, "created_at": self.created_at,
        }

    def is_current(self) -> bool:
        return self.status in ("proposed", "decided")


def supersede(previous: CouncilDecision,
              replacement: CouncilDecision) -> Tuple[CouncilDecision, CouncilDecision]:
    """The only sanctioned way to revise a decision (§7.5).

    Returns ``(previous marked superseded, replacement pointing at it)``.
    Neither input is mutated — they are frozen — and nothing is invented: the
    replacement's own question, rationale, supporters and dissenters are the
    caller's, because a superseding decision that silently inherited the old
    supporters would put names behind a conclusion they never saw.

    Refused when the previous decision is already `superseded` or `withdrawn`:
    a chain with two heads is a chain nobody can read backwards."""
    if not isinstance(previous, CouncilDecision) or not isinstance(replacement, CouncilDecision):
        raise CouncilError("decision", "supersede() takes two CouncilDecision objects")
    if previous.id and replacement.id and previous.id == replacement.id:
        raise CouncilError("decision.supersedes",
                           "a decision cannot supersede itself", got=previous.id)
    if replacement.supersedes and replacement.supersedes != previous.id:
        raise CouncilError(
            "decision.supersedes",
            f"already points at {replacement.supersedes!r}, not at {previous.id!r}",
            got=replacement.supersedes)
    check_transition(previous.status, "superseded", graph=DECISION_TRANSITIONS)
    retired = CouncilDecision.parse({**previous.to_dict(), "status": "superseded"})
    current = CouncilDecision.parse({
        **replacement.to_dict(),
        "supersedes": previous.id,
        "session_id": replacement.session_id or previous.session_id,
    })
    return retired, current
