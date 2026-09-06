"""
council/policies.py -- who should speak or work, decided before anybody pays.

The failure this file exists to prevent is the one group chat has today: every
model answers every message.  Four participants over six turns is twenty-four
generations, most of them paraphrases of one another, each carrying the
previous ones in its context; the room gets slower and more expensive exactly
as it becomes less informative, and the user ends up reading four ways of
saying "I agree with the above" (plan 2, 9).

The second failure is subtler and costs as much: asking a coordinator MODEL who
should speak.  "@Claude, look at this" needs no reasoning to route, and paying
a generation to decide it doubles the latency of the cheapest turn in the room.
So the deterministic router runs FIRST (9.1), and `route()` answers `None` only
when the rules genuinely do not decide -- that `None` is the signal a caller may
spend a coordinator call on, not a failure.

Three rules shape everything below:

1. **A selection is a recommendation about attention, never about authority.**
   `Selection` has no permission field, and this module imports nothing that
   computes one.  Who may write is `participants.effective_profile()` over the
   room's policy ceiling and the participant's own roles, decided in one place
   (11.1, 25).  A policy that could widen a profile would be a policy that
   could talk itself into a write.

2. **Order comes from a seed, not from insertion.**  `rotation_order` keys the
   round on the turn id, so the participant somebody happened to add first does
   not speak first in every round of every session (22), and a sequential round
   hands each later speaker only what has already been consolidated.

3. **Blindness is a value, not a habit.**  A round that must not anchor says so
   in `Selection.blindness`, and `context.build_packet` enforces it.  A blind
   round implemented by "remembering not to include the peers" stops being
   blind the first time somebody refactors the caller (1.4, 4.2).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from src.council.contracts import POLICIES, CouncilError
from src.council.participants import rotation_order

logger = logging.getLogger(__name__)

__all__ = [
    "MODES",
    "SEQUENTIAL",
    "PARALLEL",
    "BLIND_PARALLEL",
    "BLINDNESS_VALUES",
    "NO_BLINDNESS",
    "PEER_OUTPUTS_HIDDEN",
    "IDENTITIES_HIDDEN",
    "DEBATE_PHASES",
    "PHASES",
    "INTERVENTION_CAPS",
    "PROPOSING_ROLES",
    "CRITIQUING_ROLES",
    "JUDGING_ROLES",
    "COORDINATING_ROLES",
    "WRITING_ROLES",
    "TESTING_ROLES",
    "MAX_SOCIAL_WORDS",
    "Selection",
    "PhasePlan",
    "CouncilPolicy",
    "ChatPolicy",
    "ConsultPolicy",
    "DebatePolicy",
    "CollaboratePolicy",
    "PairPolicy",
    "TournamentPolicy",
    "policy_for",
    "known_policies",
    "route",
    "mentions",
    "is_social",
]


# -- vocabularies ----------------------------------------------------------
#
# Closed, for the same reason every vocabulary in `contracts.py` is closed: a
# mode nothing routes on is a string in a log.

SEQUENTIAL = "sequential"
PARALLEL = "parallel"
BLIND_PARALLEL = "blind_parallel"

#: How a round is run.  `parallel` and `blind_parallel` differ in one thing and
#: it is the thing that matters: in `blind_parallel` no participant is shown
#: what a peer said in the SAME round, so the second answer is an answer and
#: not an echo (4.2, 20).
MODES: Tuple[str, ...] = (SEQUENTIAL, PARALLEL, BLIND_PARALLEL)

NO_BLINDNESS = "none"
PEER_OUTPUTS_HIDDEN = "peer_outputs_hidden"
IDENTITIES_HIDDEN = "identities_hidden"

#: `context.BLINDNESS`, repeated as three constants rather than imported, so
#: that this module depends on the router's vocabulary and not on the context
#: compiler.  `tests/test_council_policies.py` asserts the two agree.
BLINDNESS_VALUES: Tuple[str, ...] = (NO_BLINDNESS, PEER_OUTPUTS_HIDDEN, IDENTITIES_HIDDEN)

#: 4.3, in order.  A debate that runs `critique` before `proposal` is a debate
#: about nothing, and one that skips `rebuttal` records an objection nobody was
#: allowed to answer.
DEBATE_PHASES: Tuple[str, ...] = ("proposal", "critique", "rebuttal", "synthesis", "verdict")

#: What each room's activity is made of.  `chat` has one phase because a
#: moderated conversation has no phases; the rest follow section 4.
PHASES: Dict[str, Tuple[str, ...]] = {
    "chat": ("chat",),
    "consult": ("consult", "synthesis"),
    "debate": DEBATE_PHASES,
    "collaborate": ("plan", "execution", "review", "verification", "synthesis"),
    "pair": ("plan", "implement", "review"),
    "tournament": ("blind", "fusion", "verdict"),
}

#: 9.3.  `chat` is 2 per turn and `debate` is 1 per participant and phase; both
#: are written in the plan.  The rest are this module's, and are the smallest
#: number that lets the room do its job: a consult is one independent answer
#: each, a pair is a driver and a navigator, and a collaboration needs the
#: coordinator, the driver and the two people who check the work.
INTERVENTION_CAPS: Dict[str, int] = {
    "chat": 2,
    "consult": 1,
    "debate": 1,
    "collaborate": 4,
    "pair": 2,
    "tournament": 1,
}

#: Section 5's table, read as columns.  These are ROLES -- expectations about
#: what a seat is in the room to hand back -- and deliberately not profiles: a
#: role says who should propose, a profile says who may write, and this file is
#: only ever allowed to have an opinion about the first.
PROPOSING_ROLES: Tuple[str, ...] = ("architect", "driver", "researcher", "integrator")
CRITIQUING_ROLES: Tuple[str, ...] = ("critic", "reviewer")
JUDGING_ROLES: Tuple[str, ...] = ("judge",)
COORDINATING_ROLES: Tuple[str, ...] = ("coordinator",)
TESTING_ROLES: Tuple[str, ...] = ("tester",)
#: The roles that produce a change.  Used only to keep `pair` down to one of
#: them (4.5); it grants nothing and is checked against nothing.
WRITING_ROLES: Tuple[str, ...] = ("driver", "integrator")

#: Past this many words a greeting has stopped being a greeting and started
#: being a request with a greeting on the front.
MAX_SOCIAL_WORDS = 8


# -- reading a message the same way twice ----------------------------------

def _fold(value: Any) -> str:
    """Lowercase and accent-stripped.

    A router that matches `critica` and misses `crítica` is a router that works
    for whoever wrote the tests and for nobody else; the room is bilingual and
    accents are typed inconsistently by everyone, including the models."""
    raw = str(value if value is not None else "")
    decomposed = unicodedata.normalize("NFKD", raw)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _words(*items: str) -> str:
    """An alternation where every literal space matches any run of whitespace.

    `re.escape` escapes the space itself (it is special under `re.VERBOSE`), so
    the escaped form has to be replaced first: replacing the bare space in
    ``todo\\ el\\ mundo`` produces ``todo\\\\s+el``, which matches a backslash
    followed by the letter s and therefore matches nothing anybody types.  That
    is exactly the bug this comment exists to stop from coming back -- it fails
    silently, and every multi-word rule simply stops firing."""
    return "|".join(
        re.escape(item).replace("\\ ", r"\s+").replace(" ", r"\s+") for item in items)


#: "todos", "cada uno", "opiniones independientes" (9.1) -- the room, in
#: parallel and blind.  `todos`/`todas` carry a negative lookahead because
#: "revisad todos los tests" is a scope and not an audience, and routing it to
#: the whole room would make the most common sentence in a code review the most
#: expensive one.
_EVERYONE_RE = re.compile(
    r"\b(?:"
    r"tod[oa]s(?!\s+(?:los|las|el|la|lo|est[oa]s|es[oa]s|aquell[oa]s|mis|tus|sus|nuestr[oa]s))"
    r"|" + _words(
        "cada uno", "cada una", "todo el mundo", "opiniones independientes",
        "de forma independiente", "por separado", "independientemente",
        "ronda ciega", "sin veros", "sin ver las respuestas",
        "everyone", "everybody", "each of you", "all of you", "each one",
        "independent opinions", "independently", "separately", "blind round",
        "without seeing", "one each") +
    r")\b")

#: "rebate / revisa / compara" (9.1) -- a proposal and somebody paid to find
#: what is wrong with it.
_CONTRAST_RE = re.compile(
    r"\b(?:" + _words(
        "rebate", "rebatid", "rebatir", "rebatan", "rebatelo", "refuta", "refutad",
        "revisa", "revisad", "revisar", "revise", "revisen", "revisalo",
        "compara", "comparad", "comparar", "comparen", "contrasta", "contrastad",
        "critica", "criticad", "criticar", "objeta", "objetad", "cuestiona",
        "segunda opinion", "que opina", "que opinas", "estas de acuerdo",
        "rebut", "rebuts", "review", "reviews", "compare", "contrast", "critique",
        "criticise", "criticize", "challenge", "second opinion", "do you agree",
        "poke holes", "tear apart") +
    r")\b")

#: "implementa / haz / cambia" (9.1) -- work, which needs an owner and, if the
#: room has them, somebody to test and somebody to review.
_IMPLEMENT_RE = re.compile(
    r"\b(?:" + _words(
        "implementa", "implementad", "implementar", "implementen", "implementalo",
        "haz", "hazlo", "haced", "hacer", "cambia", "cambiad", "cambiar", "cambialo",
        "modifica", "modificad", "modificar", "arregla", "arreglad", "arreglar",
        "corrige", "corregid", "corregir", "escribe", "escribid", "escribir",
        "refactoriza", "refactorizad", "refactorizar", "anade", "anadid", "anadir",
        "borra", "borrad", "elimina", "eliminad", "aplica", "aplicad", "aplicar",
        "crea", "cread", "crear", "programa", "programad",
        "implement", "implements", "make", "change", "modify", "fix", "write",
        "refactor", "build", "apply", "add", "remove", "delete", "patch",
        "create", "rename", "migrate") +
    r")\b")

#: A greeting, a thank-you, a goodbye.  One or two participants answer these;
#: the room does not (9.1).
_SOCIAL_RE = re.compile(
    r"\b(?:" + _words(
        "hola", "buenas", "buenos dias", "buenas tardes", "buenas noches",
        "gracias", "muchas gracias", "adios", "hasta luego", "que tal",
        "como estais", "como estas", "bienvenido", "bienvenida", "un saludo",
        "hello", "hi", "hey", "thanks", "thank you", "good morning",
        "good afternoon", "good evening", "bye", "goodbye", "how are you",
        "welcome", "cheers", "nice to meet you") +
    r")\b")

#: `@claude`, `@p_codex`, `@qwen3-coder`.  Bounded so a stray `@` in a diff or
#: an email address does not become a 4 KB participant name.
_MENTION_RE = re.compile(r"@([A-Za-z0-9_.\-À-ɏ]{1,64})")

#: `@todos` addresses the room.  It is a mention, so it beats every other rule
#: (9.1), and it is a mention of everyone, so it is exempt from the per-turn
#: intervention cap (9.3) -- the user asked for the room by name.
_MENTION_EVERYONE = frozenset({
    "all", "everyone", "everybody", "room", "channel", "here",
    "todos", "todas", "sala", "consejo", "council", "equipo", "team",
})

_ID_UNSAFE = re.compile(r"[^a-z0-9]+")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _get(obj: Any, name: str, default: Any = "") -> Any:
    """A field of a contract object or of the mapping standing in for one.

    Every caller in this package holds one of the two and never both, and a
    router that only accepted the dataclass would be a router no route handler
    could call with the JSON it just parsed."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _slug(value: Any) -> str:
    return _ID_UNSAFE.sub("_", _fold(value)).strip("_")


def _seat(entry: Any) -> Any:
    """The participant inside a `ResolvedParticipant`, or the thing itself."""
    inner = getattr(entry, "participant", None)
    return inner if inner is not None else entry


def _id_of(entry: Any) -> str:
    if isinstance(entry, str):
        return _text(entry)
    return _text(_get(_seat(entry), "id", ""))


def _ids(participants: Sequence[Any]) -> Tuple[str, ...]:
    """The room's seats, in the caller's order, none of them twice."""
    out: List[str] = []
    for entry in participants or ():
        seat = _id_of(entry)
        if seat and seat not in out:
            out.append(seat)
    return tuple(out)


def _roles_of(entry: Any) -> Tuple[str, ...]:
    raw = _get(_seat(entry), "roles", ()) or ()
    if isinstance(raw, str):
        raw = (raw,)
    return tuple(_fold(role) for role in raw if _text(role))


def _with_roles(participants: Sequence[Any], roles: Sequence[str]) -> Tuple[str, ...]:
    """The seats holding any of these roles, in room order."""
    wanted = {_fold(role) for role in roles}
    out: List[str] = []
    for entry in participants or ():
        seat = _id_of(entry)
        if seat and seat not in out and wanted & set(_roles_of(entry)):
            out.append(seat)
    return tuple(out)


def _unique(*groups: Sequence[str]) -> Tuple[str, ...]:
    """Concatenate, keep the first appearance of each seat.

    A participant listed twice in one phase would be asked twice for the same
    intervention, which is exactly the duplication 9.3 caps."""
    out: List[str] = []
    for group in groups:
        for seat in group or ():
            name = _text(seat)
            if name and name not in out:
                out.append(name)
    return tuple(out)


def _content(message: Any) -> str:
    """The text of whatever a caller passed as `message`.

    A `str`, a `CouncilMessage`, a `CouncilTurn` or the mapping either one
    arrives as: all four reach this router, and refusing three of them would
    only move the unwrapping to every call site."""
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    for field in ("content", "text", "message"):
        value = _get(message, field, "")
        if _text(value):
            return str(value)
    return ""


def _seed(message: Any, session: Any = None) -> str:
    """The rotation seed for this round.

    The turn id, because 22 asks for an order that is deterministic (a replayed
    or recovered turn speaks in the order it spoke in) without being fixed (the
    first-listed seat does not open every round of every session).  Falling
    back to the message id, then to the room's phase and id, keeps both
    properties for a caller that has no turn yet."""
    for field in ("turn_id", "id"):
        value = _text(_get(message, field, ""))
        if value:
            return value
    return f"{_text(_get(session, 'id', ''))}:{_text(_get(session, 'phase', ''))}"


# -- the two shapes --------------------------------------------------------

@dataclass(frozen=True)
class Selection:
    """Who is being asked to intervene, how, and why.

    **There is no permission field here, and there will not be one.**  A
    selection is about attention: it says whose turn it is to think, never what
    they may touch.  The profile a participant actually holds is computed by
    `participants.effective_profile()` from the participant, its roles and the
    room's policy ceiling (11.1), and a policy cannot raise it because a policy
    is never asked.  If a `tool_profile` ever appears in this dataclass, the
    room has acquired a second answer to "who may write", and the two will
    disagree on the day it matters.

    `reason` is a sentence for a person and for the audit trail, not a token to
    branch on: which rule fired is the caller's business only in so far as it
    can read it back in the ledger.
    """

    participant_ids: Tuple[str, ...]
    mode: str
    reason: str
    phase: str = ""
    blindness: str = NO_BLINDNESS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "participant_ids": list(self.participant_ids),
            "mode": self.mode,
            "reason": self.reason,
            "phase": self.phase,
            "blindness": self.blindness,
        }


@dataclass(frozen=True)
class PhasePlan:
    """The whole shape of an activity: its phases, who acts in each, how long.

    `max_rounds` comes from the session's budget rather than from the policy,
    because 3.6 makes cost part of the contract and a policy that could set its
    own round limit could spend money the user never authorised.
    """

    phases: Tuple[str, ...]
    per_phase: Mapping[str, Selection]
    max_rounds: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phases": list(self.phases),
            "per_phase": {name: sel.to_dict() for name, sel in self.per_phase.items()},
            "max_rounds": self.max_rounds,
        }


class CouncilPolicy(Protocol):
    """What every room's rules must be able to answer.

    Three questions and no more.  A policy does not run anything, does not
    build context, does not call a model and does not know what a claim is:
    `scheduler` decides when a call may start, `context` decides what it is
    told, the ledger decides what is agreed, and the gate decides what it may
    do (6.3).  Keeping this protocol at three methods is what stops the sixth
    policy from quietly becoming a second orchestrator.
    """

    name: str

    def select(self, *, session: Any, participants: Sequence[Any], message: Any,
               ledger: Any, history: Sequence[Any]) -> Selection:
        ...

    def plan(self, *, session: Any, participants: Sequence[Any], message: Any,
             ledger: Any) -> PhasePlan:
        ...

    def intervention_cap(self) -> int:
        ...


def _selection(ids: Sequence[str], mode: str, reason: str, *, phase: str = "",
               blindness: str = NO_BLINDNESS) -> Selection:
    """Build a `Selection`, refusing a mode or a blindness nobody declared.

    Raising here rather than at the caller is deliberate: an unknown mode would
    otherwise reach the orchestrator, which would fall back to "sequential" and
    silently un-blind a consult round."""
    if mode not in MODES:
        raise CouncilError("selection.mode", f"unknown mode; the declared modes are {list(MODES)}",
                           got=mode)
    if blindness not in BLINDNESS_VALUES:
        raise CouncilError("selection.blindness",
                           f"unknown blindness; the declared values are {list(BLINDNESS_VALUES)}",
                           got=blindness)
    return Selection(participant_ids=_unique(ids), mode=mode, reason=str(reason or ""),
                     phase=str(phase or ""), blindness=blindness)


# -- the three questions the router asks the message -----------------------

def mentions(message: Any, participants: Sequence[Any]) -> Tuple[str, ...]:
    """The participants this message named with `@`, in the order it named them.

    Matched against the seat id, the seat id without its `p_` prefix, the
    display name and the display name's first word, all accent-folded: a user
    writing `@Claude`, `@claude` or `@p_claude` means the same seat, and being
    told "no such participant" because of a capital letter is the kind of
    friction that makes people go back to copying answers between tabs.

    `@todos` / `@everyone` returns the whole room -- it is a mention of
    everyone, which is what 9.3 exempts from the intervention cap.

    An `@` that matches nobody is dropped in silence and not guessed at: a
    fuzzy match here would route a turn to a model the user did not ask for.
    """
    text = _content(message)
    if not text or "@" not in text:
        return ()
    room = _ids(participants)
    alias: Dict[str, str] = {}
    for entry in participants or ():
        seat = _id_of(entry)
        if not seat:
            continue
        name = _text(_get(_seat(entry), "display_name", ""))
        for candidate in (seat, seat[2:] if seat.lower().startswith("p_") else "", name,
                          name.split()[0] if name.split() else ""):
            key = _slug(candidate)
            # First seat wins a shared alias: two seats called "Claude" is a
            # room the resolver already numbered, and guessing between them
            # would be worse than answering with the one that was added first.
            if key and key not in alias:
                alias[key] = seat
    out: List[str] = []
    for token in _MENTION_RE.findall(text):
        folded = _fold(token).strip("._-")
        if folded in _MENTION_EVERYONE:
            return room
        seat = alias.get(_slug(folded))
        if seat and seat not in out:
            out.append(seat)
        elif not seat:
            logger.debug("council policies: @%s matches no participant of this room", token)
    return tuple(out)


def is_social(message: Any) -> bool:
    """Whether this message is a greeting and nothing else (9.1).

    Three conditions, and the last two are what keep the rule honest: it must
    look social, it must be short, and it must not also ask for work.  "Hola,
    implementad el callback de OAuth" is a request with a greeting on the
    front, and answering it with one participant saying "hola" would be the
    most annoying possible behaviour."""
    folded = _fold(_content(message))
    if not folded.strip():
        return False
    if _IMPLEMENT_RE.search(folded) or _CONTRAST_RE.search(folded) or _EVERYONE_RE.search(folded):
        return False
    if len(folded.split()) > MAX_SOCIAL_WORDS:
        return False
    return bool(_SOCIAL_RE.search(folded))


# -- who fills each function of a round ------------------------------------

def _proposers(participants: Sequence[Any]) -> Tuple[str, ...]:
    """The seats that put something on the table.

    A room with no declared proposer still has to propose something, so the
    fallback is "everybody who is not only here to find fault", and the last
    resort is the whole room: a debate with an empty `proposal` phase is a
    debate that cannot start."""
    named = _with_roles(participants, PROPOSING_ROLES)
    if named:
        return named
    judges = set(_with_roles(participants, tuple(CRITIQUING_ROLES) + tuple(JUDGING_ROLES)))
    rest = tuple(seat for seat in _ids(participants) if seat not in judges)
    return rest or _ids(participants)


def _critics(participants: Sequence[Any], proposers: Sequence[str] = ()) -> Tuple[str, ...]:
    """The seats paid to find what is wrong, never the ones being criticised.

    Excluding the proposers is section 5's rule about not being the only judge
    of your own work, applied to a phase instead of to a task.  A room of one
    gets its own proposer back rather than an empty phase, because 1.9.10 lets
    the room degrade to a single agent and an empty critique would silently
    turn a debate into a monologue that claims to have been reviewed.
    """
    taken = set(proposers or ())
    named = tuple(seat for seat in _with_roles(participants, CRITIQUING_ROLES) if seat not in taken)
    if named:
        return named
    rest = tuple(seat for seat in _ids(participants) if seat not in taken)
    return rest or tuple(proposers or ())


def _coordinator(participants: Sequence[Any]) -> Tuple[str, ...]:
    named = _with_roles(participants, COORDINATING_ROLES)
    return named[:1]


def _synthesiser(participants: Sequence[Any]) -> Tuple[str, ...]:
    """One seat writes the combined proposal: the coordinator, else the
    integrator, else whoever proposed.  One, because a synthesis written by
    three participants is three syntheses."""
    for group in (_coordinator(participants), _with_roles(participants, ("integrator",)),
                  _proposers(participants)):
        if group:
            return group[:1]
    return _ids(participants)[:1]


def _judge(participants: Sequence[Any], proposers: Sequence[str] = ()) -> Tuple[str, ...]:
    """One seat records the verdict, preferring somebody who did not propose.

    When the only available judge is a proposer, it still gets the phase and
    the room keeps a judge that judged itself -- which `participants.
    check_role_conflicts()` reports and the summary shows.  Silently dropping
    the verdict would hide the conflict instead of recording it (3.4)."""
    taken = set(proposers or ())
    for group in (_with_roles(participants, JUDGING_ROLES), _coordinator(participants),
                  _with_roles(participants, CRITIQUING_ROLES)):
        free = tuple(seat for seat in group if seat not in taken)
        if free:
            return free[:1]
    outside = tuple(seat for seat in _ids(participants) if seat not in taken)
    return (outside or _ids(participants))[:1]


def _drivers(participants: Sequence[Any]) -> Tuple[str, ...]:
    named = _with_roles(participants, WRITING_ROLES)
    if named:
        return named
    # No declared driver: the work still needs an owner, and the coordinator is
    # the wrong one (5: the coordinator organises and does not modify the
    # workspace).  The first seat that is not coordinating gets it.
    coordinating = set(_with_roles(participants, COORDINATING_ROLES))
    rest = tuple(seat for seat in _ids(participants) if seat not in coordinating)
    return rest[:1] or _ids(participants)[:1]


def _reply_author(message: Any, participants: Sequence[Any],
                  history: Sequence[Any] = ()) -> str:
    """The participant whose message this one answers, if it is in the room.

    `reply_to` is a message id, so this needs the transcript to turn it into an
    author.  A reply to a message from outside the room, or to one this caller
    did not pass, resolves to `""` and the rule simply does not fire -- guessing
    an author from the text is precisely what 3.2 forbids."""
    ref = _text(_get(message, "reply_to", ""))
    if not ref:
        return ""
    room = set(_ids(participants))
    for row in history or ():
        if _text(_get(row, "id", "")) == ref:
            author = _text(_get(row, "author_id", ""))
            return author if author in room else ""
    return ""


# -- the deterministic router (9.1) ----------------------------------------

def _route(message: Any, participants: Sequence[Any], *, policy: str = "chat",
           history: Sequence[Any] = ()) -> Tuple[Optional[Selection], str]:
    """`route()` plus the name of the rule that fired.

    The rule name never leaves this module in a value; it decides whether the
    per-turn cap applies (9.3 exempts "mención a todos") and it goes into the
    reason a person reads.  Keeping it out of `Selection` keeps the dataclass
    free of a field callers would start branching on."""
    room = _ids(participants)
    if not room:
        return None, "empty_room"

    text = _content(message)
    named = mentions(text, participants)
    if named:
        everyone = len(named) == len(room) and len(room) > 1
        mode = SEQUENTIAL if len(named) == 1 else PARALLEL
        return _selection(
            named, mode,
            f"the user named {', '.join(named)} with @; a mention selects the seats it "
            f"names and obliges nobody else (3.1, 9.1)"), ("everyone" if everyone else "mention")

    folded = _fold(text)

    if _EVERYONE_RE.search(folded):
        return _selection(
            room, BLIND_PARALLEL,
            "the message asked the whole room for independent opinions, so the round runs "
            "blind: nobody is shown a peer's answer from this round, or the second answer "
            "is an echo of the first (4.2, 9.1)",
            blindness=PEER_OUTPUTS_HIDDEN), "everyone"

    if _CONTRAST_RE.search(folded):
        author = _reply_author(message, participants, history)
        proposers = (author,) if author else _proposers(participants)[:1]
        critics = _critics(participants, proposers)[:1]
        return _selection(
            _unique(proposers, critics), SEQUENTIAL,
            "the message asked for a rebuttal, a review or a comparison, so it goes to the "
            "proposal's author and to one critic and not to the room (9.1)"), "contrast"

    if _IMPLEMENT_RE.search(folded):
        chosen = _unique(_coordinator(participants), _drivers(participants),
                         _with_roles(participants, TESTING_ROLES),
                         _with_roles(participants, ("reviewer",)))
        return _selection(
            chosen, SEQUENTIAL,
            "the message asked for work, so it goes to the coordinator and the driver, plus "
            "the tester and the reviewer this room has; the rest are not asked to comment on "
            "a change they are not doing (9.1)"), "implement"

    author = _reply_author(message, participants, history)
    if author:
        reviewer = tuple(seat for seat in _critics(participants, (author,)) if seat != author)[:1]
        return _selection(
            _unique((author,), reviewer), SEQUENTIAL,
            f"the message answers something {author} wrote, so {author} and its reviewer are "
            f"asked and the rest of the room is not (9.1)"), "reply"

    if is_social(text):
        chosen = _unique(_coordinator(participants), room[:1])[:1]
        return _selection(
            chosen, SEQUENTIAL,
            "a greeting is answered by one participant; a room of six models saying hello is "
            "six generations nobody asked for (9.1)"), "social"

    return None, "undecided"


def route(message: Any, participants: Sequence[Any], *, policy: str = "chat",
          history: Sequence[Any] = ()) -> Optional[Selection]:
    """Who should answer this message, or `None` when the rules do not decide.

    `None` is the whole point of the function.  It is not a failure and it is
    not "ask everybody": it is the signal that this is one of the messages a
    coordinator MODEL is worth paying for -- an ambiguous request, or work that
    has to be decomposed (9.1).  Every other message is routed here for free.

    `policy` is accepted because a caller has it and because a later rule may
    need it; today the rules are the same in every room, which is deliberate:
    `@Claude` must mean the same thing in a debate and in a pair session.

    `history` is the room's transcript and is only read to turn a `reply_to`
    into an author; without it the reply rule cannot fire, which is why it is a
    parameter with a default rather than a lookup this module invents.
    """
    return _route(message, participants, policy=policy, history=history)[0]


# -- the six rooms (4) -----------------------------------------------------

def _max_rounds(session: Any) -> int:
    """The room's round budget, never the policy's opinion of it (3.6).

    `0` survives: a room told to run no further rounds has been told something,
    and `budgets.max_rounds or 4` would quietly give it four."""
    budgets = _get(session, "budgets", None)
    value = _get(budgets, "max_rounds", None) if budgets is not None else None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 4


def _ordered(selection: Selection, participants: Sequence[Any], *, seed: str) -> Selection:
    """Put a sequential round in rotation order (22).

    Only sequential rounds are reordered, and that is the point: in a
    sequential round the order decides who speaks into an empty room and who
    speaks into three consolidated answers, so leaving it at insertion order
    would give the first-listed model the first word in every round of every
    session.  A parallel round has no order to bias, so its selection is left
    exactly as the rule produced it."""
    if selection.mode != SEQUENTIAL or len(selection.participant_ids) < 2:
        return selection
    chosen = set(selection.participant_ids)
    try:
        order = [seat for seat in rotation_order(participants, seed=seed) if seat in chosen]
    except Exception:  # noqa: BLE001 - a round must not die choosing an order
        logger.exception("council policies: rotation failed; keeping the routed order")
        return selection
    # Anything the rotation did not know about keeps its place at the end
    # rather than being dropped: losing a selected participant to a helper is
    # worse than an imperfect order.
    tail = [seat for seat in selection.participant_ids if seat not in order]
    if not order:
        return selection
    return Selection(participant_ids=tuple(order) + tuple(tail), mode=selection.mode,
                     reason=selection.reason, phase=selection.phase,
                     blindness=selection.blindness)


class _BasePolicy:
    """The machinery all six share: route, shape, cap, order.

    `select()` is written once here because the router is the same in every
    room (9.1) and the differences are all in `_shape`, which is where a reader
    looking for "what makes a debate a debate" should find it."""

    name: str = ""
    default_mode: str = SEQUENTIAL
    default_blindness: str = NO_BLINDNESS

    def intervention_cap(self) -> int:
        return INTERVENTION_CAPS.get(self.name, 2)

    def phases(self) -> Tuple[str, ...]:
        return PHASES.get(self.name, ("chat",))

    # -- selection ---------------------------------------------------------

    def select(self, *, session: Any = None, participants: Sequence[Any] = (),
               message: Any = None, ledger: Any = None,
               history: Sequence[Any] = ()) -> Selection:
        room = _ids(participants)
        phase = self._phase(session)
        if not room:
            return _selection((), self.default_mode,
                              "the room has no participants; nobody can be asked to speak",
                              phase=phase)
        routed, rule = _route(message, participants, policy=self.name, history=history)
        if routed is None:
            routed = self._fallback(participants, phase)
            rule = "fallback"
        shaped = self._shape(routed, participants=participants, rule=rule, session=session,
                             phase=phase, ledger=ledger)
        return _ordered(shaped, participants, seed=_seed(message, session))

    def _phase(self, session: Any) -> str:
        """The phase this room is in: the session's, if this policy declares it.

        A session carrying a phase from another policy (a room whose policy was
        changed between activities) falls back to this policy's first phase
        rather than being asked to run a phase it has no rules for."""
        declared = self.phases()
        current = _text(_get(session, "phase", ""))
        return current if current in declared else (declared[0] if declared else "")

    def _fallback(self, participants: Sequence[Any], phase: str) -> Selection:
        """What to do when the router abstained.

        Not "everybody": that is the behaviour this whole file exists to
        replace.  The cap's worth of seats, in rotation order, and a reason
        that says out loud that a coordinator model is what would decide this
        properly -- so the ledger records that the room guessed."""
        chosen = _unique(_coordinator(participants), _ids(participants))[:max(1, self.intervention_cap())]
        return _selection(
            chosen, self.default_mode,
            "the deterministic rules did not decide this message; a coordinator model is what "
            "would choose here (9.1), so a bounded default was used and recorded",
            phase=phase, blindness=self.default_blindness)

    def _shape(self, selection: Selection, *, participants: Sequence[Any], rule: str,
               session: Any, phase: str, ledger: Any) -> Selection:
        return self._capped(selection, rule=rule, phase=phase)

    # -- the cap (9.3) -----------------------------------------------------

    def _capped(self, selection: Selection, *, rule: str, phase: str) -> Selection:
        """Trim to `intervention_cap()`, except when the user named the room.

        Two exemptions, both from 9.3 read with 3.1: a mention of everyone, and
        an explicit mention of specific seats.  In both the user said who should
        answer, and a cap that overrode them would be the room deciding it knew
        better than the person paying for it."""
        cap = max(0, int(self.intervention_cap()))
        ids = selection.participant_ids
        if rule in ("mention", "everyone") or cap <= 0 or len(ids) <= cap:
            return Selection(ids, selection.mode, selection.reason, phase or selection.phase,
                             selection.blindness)
        dropped = ids[cap:]
        logger.debug("council policies %s: capped the round at %d; %s were not asked",
                     self.name, cap, list(dropped))
        return Selection(
            ids[:cap], selection.mode,
            f"{selection.reason}; capped at {cap} intervention(s) for this turn, so "
            f"{', '.join(dropped)} were not asked (9.3)",
            phase or selection.phase, selection.blindness)

    # -- the plan ----------------------------------------------------------

    def plan(self, *, session: Any = None, participants: Sequence[Any] = (),
             message: Any = None, ledger: Any = None) -> PhasePlan:
        phases = self.phases()
        per_phase = {name: self._phase_selection(name, participants) for name in phases}
        return PhasePlan(phases=phases, per_phase=per_phase, max_rounds=_max_rounds(session))

    def _phase_selection(self, phase: str, participants: Sequence[Any]) -> Selection:
        return _selection(_ids(participants)[:max(1, self.intervention_cap())], self.default_mode,
                          f"{self.name}: {phase}", phase=phase, blindness=self.default_blindness)


class ChatPolicy(_BasePolicy):
    """4.1 -- a moderated group.  Zero to a few participants per message.

    The cap is the whole policy: two answers per turn unless the user asked the
    room (9.3).  Everything else is the shared router."""

    name = "chat"


class ConsultPolicy(_BasePolicy):
    """4.2 -- an independent second opinion, with no anchoring.

    Every selected participant answers WITHOUT seeing a peer's answer from this
    round, which is why `blind_parallel` and `peer_outputs_hidden` are set here
    and not left to the caller: a consult that shares the first answer produces
    one opinion wearing two names, and it looks exactly like agreement."""

    name = "consult"
    default_mode = BLIND_PARALLEL
    default_blindness = PEER_OUTPUTS_HIDDEN

    def _shape(self, selection: Selection, *, participants, rule, session, phase, ledger) -> Selection:
        # A mention narrows WHO is consulted -- the user's authority (3.1) --
        # and never turns the round sighted.
        ids = selection.participant_ids if rule in ("mention", "everyone") else _ids(participants)
        if len(ids) < 2:
            return _selection(ids, SEQUENTIAL,
                              f"{selection.reason}; one participant is being consulted, so there "
                              f"is no peer answer to hide",
                              phase=phase, blindness=NO_BLINDNESS)
        return _selection(ids, BLIND_PARALLEL,
                          "a consult asks each participant the same question without showing it "
                          "what the others said, so the answers can be compared instead of "
                          "agreeing with each other (4.2)",
                          phase=phase, blindness=PEER_OUTPUTS_HIDDEN)

    def _phase_selection(self, phase: str, participants: Sequence[Any]) -> Selection:
        if phase == "consult":
            return _selection(_ids(participants), BLIND_PARALLEL,
                              "every participant answers the question blind (4.2)",
                              phase=phase, blindness=PEER_OUTPUTS_HIDDEN)
        return _selection(_synthesiser(participants), SEQUENTIAL,
                          "one seat presents the agreements, the differences and the "
                          "uncertainties (4.2)", phase=phase)


class DebatePolicy(_BasePolicy):
    """4.3 -- proposal, critique, rebuttal, synthesis, verdict, in that order.

    The order is the policy.  A critique before a proposal is a critique of
    nothing; a synthesis before a rebuttal buries the objection instead of
    answering it; and a verdict without a rubric is `tournament`'s three
    generic axes applied to a question they were not written for, which is what
    4.3 tells us not to do.

    `plan()` is the authority on who acts in each phase, and `select()` answers
    with that phase's selection so a caller cannot get one answer from the plan
    and a different one from the selection."""

    name = "debate"

    def _shape(self, selection: Selection, *, participants, rule, session, phase, ledger) -> Selection:
        if rule in ("mention", "everyone"):
            return self._capped(selection, rule=rule, phase=phase)
        plan = self.plan(session=session, participants=participants)
        return plan.per_phase.get(phase) or self._capped(selection, rule=rule, phase=phase)

    def plan(self, *, session: Any = None, participants: Sequence[Any] = (),
             message: Any = None, ledger: Any = None) -> PhasePlan:
        proposers = _proposers(participants)
        critics = _critics(participants, proposers)
        per_phase = {
            "proposal": _selection(
                proposers, BLIND_PARALLEL,
                "independent proposals: nobody is shown a peer's proposal while writing its "
                "own, or the second proposal is a variation on the first (4.3)",
                phase="proposal", blindness=PEER_OUTPUTS_HIDDEN),
            "critique": _selection(
                critics, PARALLEL,
                "the critics look for concrete faults in the proposals, in parallel because "
                "they are not answering each other (4.3)",
                phase="critique"),
            "rebuttal": _selection(
                proposers, SEQUENTIAL,
                "each proposer accepts, corrects or rebuts the objections against its own "
                "proposal; an objection nobody answered is not a settled objection (4.3, 25)",
                phase="rebuttal"),
            "synthesis": _selection(
                _synthesiser(participants), SEQUENTIAL,
                "one seat combines what survived into a single proposal (4.3)",
                phase="synthesis"),
            "verdict": _selection(
                _judge(participants, proposers), SEQUENTIAL,
                "the judge applies the rubric for this task and records the decision and the "
                "dissent; with no reliable judge the result stays a synthesis without an "
                "authoritative verdict (4.3, 3.4)",
                phase="verdict"),
        }
        return PhasePlan(phases=DEBATE_PHASES, per_phase=per_phase,
                         max_rounds=_max_rounds(session))


class CollaboratePolicy(_BasePolicy):
    """4.4 -- a team that produces something, with one owner per piece of work.

    This policy selects who is asked; it does not hand anybody a claim, a lock
    or a tool.  Tasks, owners and claims are the ledger's (11.2), and the write
    gate is still the gate."""

    name = "collaborate"

    def _shape(self, selection: Selection, *, participants, rule, session, phase, ledger) -> Selection:
        if rule in ("mention", "everyone"):
            return self._capped(selection, rule=rule, phase=phase)
        chosen = _unique(_coordinator(participants), _drivers(participants),
                         _with_roles(participants, TESTING_ROLES),
                         _with_roles(participants, ("reviewer",)))
        chosen = chosen or selection.participant_ids
        return self._capped(
            _selection(chosen, SEQUENTIAL,
                       "the coordinator plans, the driver does the work, and the tester and "
                       "reviewer check it; only genuinely independent work is parallelised, "
                       "and that decision belongs to the tasks and not to the round (4.4)",
                       phase=phase),
            rule=rule, phase=phase)

    def _phase_selection(self, phase: str, participants: Sequence[Any]) -> Selection:
        table = {
            "plan": (_coordinator(participants) or _ids(participants)[:1],
                     "the coordinator writes the plan and finds the dependencies (4.4)"),
            "execution": (_drivers(participants),
                          "the owners do the work inside the resources they have claimed (4.4)"),
            "review": (_critics(participants, _drivers(participants)),
                       "an independent reviewer inspects the result (4.4)"),
            "verification": (_with_roles(participants, TESTING_ROLES) or _drivers(participants),
                             "the evidence is produced and `prove` verifies it (4.4)"),
            "synthesis": (_synthesiser(participants),
                          "one seat publishes the result, the dissent and the evidence (12.2)"),
        }
        ids, why = table.get(phase, (_ids(participants)[:1], f"collaborate: {phase}"))
        return _selection(ids, SEQUENTIAL, why, phase=phase)


class PairPolicy(_BasePolicy):
    """4.5 -- one driver and one navigator.

    **One writer.**  The navigator reviews the plan, the diff, the risks and
    the tests and may ask for changes; it does not reimplement the work in
    parallel, because two implementations of the same task are two claims on
    the same files and the second one is thrown away after being paid for.

    Enforced by construction: `_shape` keeps at most one seat holding a writing
    role.  It is a rule about ATTENTION -- who is asked to implement -- and the
    permission half is still `participants.effective_profile()` over the
    claims; a policy cannot make the navigator writable and does not try."""

    name = "pair"

    @staticmethod
    def _one_writer(ids: Sequence[str], writers: Sequence[str]) -> Tuple[str, ...]:
        """The selection with at most one writing seat left in it."""
        pool = set(writers or ())
        kept: List[str] = []
        seen_writer = False
        for seat in ids or ():
            if seat in pool:
                if seen_writer:
                    continue
                seen_writer = True
            kept.append(seat)
        return tuple(kept)

    def _shape(self, selection: Selection, *, participants, rule, session, phase, ledger) -> Selection:
        drivers = _drivers(participants)
        if rule in ("mention", "everyone"):
            # The user named these seats and 3.1 says that stands.  The one
            # thing the policy still enforces is its own invariant: a pair
            # writes once, so a second writing seat is dropped and said so.
            chosen = self._one_writer(selection.participant_ids, drivers)
            dropped = [seat for seat in selection.participant_ids if seat not in chosen]
            note = (f"; {', '.join(dropped)} also hold a writing role and were not asked, "
                    f"because a pair writes through exactly one driver (4.5)") if dropped else ""
            return self._capped(
                _selection(chosen, SEQUENTIAL, f"{selection.reason}{note}", phase=phase),
                rule=rule, phase=phase)
        driver = tuple(seat for seat in selection.participant_ids if seat in drivers)[:1] \
            or drivers[:1]
        navigator = tuple(seat for seat in selection.participant_ids
                          if seat not in drivers)[:1] \
            or _critics(participants, driver)[:1]
        chosen = _unique(driver, navigator)
        extra = [seat for seat in selection.participant_ids
                 if seat in drivers and seat not in driver]
        note = (f"; {', '.join(extra)} also hold a writing role and were not asked, because a "
                f"pair writes through exactly one driver (4.5)") if extra else ""
        return _selection(
            chosen, SEQUENTIAL,
            f"{driver[0] if driver else 'nobody'} drives and everyone else navigates: the "
            f"navigator reviews the plan, the diff and the tests and does not reimplement the "
            f"work in parallel (4.5){note}",
            phase=phase)

    def _phase_selection(self, phase: str, participants: Sequence[Any]) -> Selection:
        drivers = _drivers(participants)
        table = {
            "plan": (_unique(drivers[:1], _critics(participants, drivers)[:1]),
                     "driver and navigator agree the step before it is written (4.5)"),
            "implement": (drivers[:1], "only the driver writes (4.5)"),
            "review": (_critics(participants, drivers)[:1] or drivers[:1],
                       "the navigator, and an independent reviewer when the room has one, "
                       "check the step (4.5)"),
        }
        ids, why = table.get(phase, (drivers[:1], f"pair: {phase}"))
        return _selection(ids, SEQUENTIAL, why, phase=phase)


class TournamentPolicy(_BasePolicy):
    """4.6 -- the existing competition, kept as a specialised policy.

    Blind first round, anonymised fusion, convergence, judge and deterministic
    tie-break all still belong to `src/tournament.py`; this only says who is in
    and how blind each phase is, so that adapting the engine does not require
    re-deciding the room's rules (17.2)."""

    name = "tournament"
    default_mode = BLIND_PARALLEL
    default_blindness = PEER_OUTPUTS_HIDDEN

    def _shape(self, selection: Selection, *, participants, rule, session, phase, ledger) -> Selection:
        ids = selection.participant_ids if rule in ("mention", "everyone") else _ids(participants)
        if phase == "verdict":
            return _selection(_judge(participants, ids), SEQUENTIAL,
                              "the judge applies the rubric and the deterministic tie-break "
                              "(4.6)", phase=phase)
        blindness = IDENTITIES_HIDDEN if phase == "fusion" else PEER_OUTPUTS_HIDDEN
        return _selection(
            ids, BLIND_PARALLEL,
            "every entrant answers blind; the fusion rounds are anonymised so an answer is "
            "judged on what it says and not on whose name is on it (4.6, 22)",
            phase=phase, blindness=blindness)

    def _phase_selection(self, phase: str, participants: Sequence[Any]) -> Selection:
        if phase == "verdict":
            return _selection(_judge(participants), SEQUENTIAL,
                              "judge and deterministic tie-break (4.6)", phase=phase)
        blindness = IDENTITIES_HIDDEN if phase == "fusion" else PEER_OUTPUTS_HIDDEN
        return _selection(_ids(participants), BLIND_PARALLEL,
                          f"tournament: {phase}", phase=phase, blindness=blindness)


# -- the registry ----------------------------------------------------------

_POLICIES: Dict[str, _BasePolicy] = {
    "chat": ChatPolicy(),
    "consult": ConsultPolicy(),
    "debate": DebatePolicy(),
    "collaborate": CollaboratePolicy(),
    "pair": PairPolicy(),
    "tournament": TournamentPolicy(),
}


def known_policies() -> Tuple[str, ...]:
    """The policies this build implements, in `contracts.POLICIES` order."""
    return tuple(name for name in POLICIES if name in _POLICIES)


def policy_for(name: str) -> CouncilPolicy:
    """The rules of one room.

    Raises on a name nobody declared rather than falling back to `chat`: a
    misspelt policy that silently becomes a moderated conversation is a debate
    that never had a critique phase and never said so (20, "rechaza políticas
    desconocidas").

    The instances are shared because they hold no state -- a policy that
    remembered anything between turns would be a second source of truth about
    what the room has done, and the ledger is the first."""
    key = _text(name)
    policy = _POLICIES.get(key)
    if policy is None:
        raise CouncilError(
            "session.policy",
            f"unknown council policy; this build implements {list(known_policies())}",
            got=name)
    return policy


# A policy in the contracts' vocabulary with no implementation here would be a
# room a user can create and nothing can run.  The test suite asserts this; the
# log line is for whoever adds the seventh policy and ships before running it.
_MISSING = [name for name in POLICIES if name not in _POLICIES]
if _MISSING:  # pragma: no cover - guarded by a test
    logger.error("council policies: %s are declared in contracts.POLICIES and implemented "
                 "nowhere; a room with one of those policies cannot take a turn", _MISSING)
