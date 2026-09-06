"""
context_engine/planner.py — deciding what to look for, before looking for it.

The failure this module exists to prevent is the one the plan opens with: a
user types "good morning" and the machine answers by running an embedding
search over their document collection, a lexical pass over the symbol index and
a graph walk over provenance — nine stores woken, four hundred milliseconds
spent, and not one token of it in the answer.  Retrieval that is decided
*after* the candidates are back is not a decision, it is a filter over work
already paid for; and the parts that cost the most (Chroma, the code index)
are exactly the parts a chat turn never needed.

So a `RetrievalPlan` is produced first, from the request alone, and it is the
contract `compiler.py` uses to decide which sources are consulted at all.  Four
properties make it worth having:

**It is deterministic and free.**  `classify_intent()` is a table of literal
signals matched against the query with word boundaries.  No model, no
embedding, no store.  A classifier on the turn path that can be slow, or wrong
in a new way after a fine-tune, would have to be measured; this one can be
read.

**The phase changes what is asked for, not what is scored.**  §1.4: "`phase`
permite priorizar instrucciones durante planificación y evidencia durante
verificación".  Priority applied at ranking time still pays for the retrieval;
priority applied here does not.  `phase="plan"` raises instructions and rules,
`phase="verify"` raises evidence and lowers memory, and both do it by changing
the section list the sources are asked for.

**A refusal is recorded.**  `skipped` names every source that was not consulted
and why, in the same shape `reasons` names the ones that were.  "Why did it not
read that file?" has to be answerable from the plan as well as from the packet,
because a source that was never consulted produces no candidate and therefore
no omission — it would otherwise vanish from the record entirely.

**Policy is applied here, not later.**  `allow_project_sources=False` removes
project sources from `source_ids`.  It does not rank them last: consulting a
store and discarding its rows leaves `last_used` fingerprints on it, and those
fingerprints are precisely what an incognito turn is supposed not to leave.

Signal table used by `classify_intent`, in the order it is applied:

===========================  ==========================================
signal                       result
===========================  ==========================================
`task.intent` set, not the   that intent, unchanged.  The runtime knows
contract default "chat"      better than a word list.
`hint` (same rule)           the caller's guess, when nothing was declared
`consumer == "voice"`        `voice` — a spoken turn is latency-bound
MEDIA_SIGNALS in the query   `media`
workspace + REVIEW_SIGNALS   `review`
workspace + CODE_SIGNALS or  `code_change`
a path-shaped token
RESEARCH_SIGNALS             `research`
nothing above                `chat` — and a chat consults neither RAG
                             nor the code index
===========================  ==========================================

Every one of those tuples is a module constant so an install can tune it
without editing the logic that reads it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .budgets import profile_for, section_order
from .contracts import (
    MANDATORY_SECTIONS,
    RETRIEVAL_LANES,
    SECTION_KINDS,
    ContextRequest,
)

logger = logging.getLogger(__name__)

#: The value `ContextTask.intent` carries when nobody set it.  Treated as
#: "not stated" rather than as a declaration, because the contract's default
#: and a caller who genuinely means chat are indistinguishable on the wire and
#: the signals give the same answer for the second case anyway.
DEFAULT_INTENT = "chat"

#: Same reasoning for the profile: `balanced_v1` is what `ContextPolicy`
#: defaults to, so honouring it as an explicit choice would mean the intent
#: never picked a profile for anybody who did not name one.
DEFAULT_PROFILE_ID = "balanced_v1"

#: Setting name and fallback for the retrieval deadline.  Two seconds is the
#: whole gather stage (the sources run concurrently), not one source's share.
TIMEOUT_SETTING = "agent_context_timeout_ms"
DEFAULT_TIMEOUT_MS = 2000

#: A spoken turn gets half of it.  Nine seconds of silence while the machine
#: thinks about which memory to read has already failed as a conversation,
#: whatever it eventually says.
VOICE_TIMEOUT_FACTOR = 0.5
MIN_TIMEOUT_S = 0.05
MAX_TIMEOUT_S = 30.0


# ── the signals ────────────────────────────────────────────────────────────

MEDIA_SIGNALS: Tuple[str, ...] = (
    "image", "imagen", "imagenes", "imágenes", "picture", "photo", "foto",
    "render", "video", "vídeo", "audio", "sound", "sonido", "music", "musica",
    "música", "sprite", "thumbnail", "comfyui", "stable diffusion", "flux",
    "upscale", "voiceover", "tts", "lora", "checkpoint model",
)

REVIEW_SIGNALS: Tuple[str, ...] = (
    "review", "code review", "pull request", "revisa", "revisar", "revision",
    "revisión", "audit", "audita", "auditar",
)

CODE_SIGNALS: Tuple[str, ...] = (
    "bug", "bugfix", "error", "errors", "errores", "traceback", "stacktrace",
    "exception", "excepción", "crash", "test", "tests", "pytest", "compile",
    "compilar", "build", "refactor", "refactoriza", "implement", "implementa",
    "implementar", "function", "función", "funcion", "class", "clase",
    "method", "método", "metodo", "import", "commit", "diff", "patch",
    "lint", "regression", "regresión", "arregla", "arreglar", "corrige",
    "corregir", "falla", "fallo",
)

RESEARCH_SIGNALS: Tuple[str, ...] = (
    "research", "investiga", "investigar", "investigación", "investigacion",
    "paper", "papers", "bibliografia", "bibliografía", "state of the art",
    "survey", "benchmark", "compara", "comparar", "sources", "fuentes",
)

#: A token that looks like a path or a source file.  Deliberately narrow: the
#: point is to catch "the traceback says src/app.py:41", not to guess that any
#: word with a dot in it is a filename.
PATH_HINT = re.compile(
    r"(?<!\w)(?:[\w.\-]+[/\\])+[\w.\-]+"          # a/b/c or a\b\c
    r"|(?<!\w)[\w.\-]+\.(?:py|js|mjs|ts|tsx|jsx|md|json|ya?ml|toml|ini|cfg|"
    r"c|h|cc|cpp|hpp|rs|go|java|kt|rb|php|sh|ps1|bat|sql|css|scss|html)(?!\w)",
    re.IGNORECASE,
)


def _matcher(words: Sequence[str]) -> "re.Pattern[str]":
    """One word-boundary alternation per signal list, compiled once.

    Substring matching would classify "the latest contest" as a coding turn
    ("test" is in both), and a classifier whose false positives are invisible
    is worse than no classifier: it spends the budget silently."""
    ordered = sorted((re.escape(w) for w in words if w), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(ordered) + r")(?!\w)", re.IGNORECASE)


_MEDIA = _matcher(MEDIA_SIGNALS)
_REVIEW = _matcher(REVIEW_SIGNALS)
_CODE = _matcher(CODE_SIGNALS)
_RESEARCH = _matcher(RESEARCH_SIGNALS)


# ── what each source is worth waking for ───────────────────────────────────

#: Sections each known source produces, keyed by the ``source_id`` its adapter
#: declares.  A source that is not listed here is still consulted — that is
#: exactly what :func:`candidates.gather` does with a source declaring no
#: sections, and the planner disagreeing with gather would be a second policy
#: nobody could find.  Add a line here when a new adapter arrives and it starts
#: being planned instead of merely tolerated.
SOURCE_SECTIONS: Dict[str, Tuple[str, ...]] = {
    "objectives": ("active_goal", "decisions"),
    "project_memory": ("project_rules",),
    # The project's typed context links (`adapters/project_links.py`).  The
    # manifest is `project_rules`; a query's excerpts land in the section of
    # whatever kind the link points at, which is why one adapter declares
    # four.  This tuple and `ProjectLinksSource.sections` are asserted equal
    # by tests/test_context_engine_derived_sources.py.
    "project_links": ("project_rules", "retrieved_documents", "code_map",
                      "multimodal_recipes"),
    "memory_engine": ("retrieved_memory",),
    "personal_memory": ("retrieved_memory",),
    "sessions": ("recent_messages",),
    "files": ("code_map",),
    "code_index": ("code_map",),
    "documents": ("retrieved_documents",),
    "experts": ("retrieved_documents",),
    "provenance": ("past_experiences", "decisions"),
    # The six derived stores, wired by `adapters/derived.py`.  Each line is the
    # `sections` tuple its adapter declares, and it has to stay that way: the
    # planner deciding a source fills `system_constraints` while `gather` asks
    # it for `project_rules` is two policies nobody can find.  `blocks` lists
    # what `blocks.SECTION_BY_TYPE` can actually produce that a round narrows
    # to; no block type maps to `system_constraints`.
    "blocks": ("project_rules", "active_goal", "current_state", "decisions",
               "tool_guidance"),
    "capsules": ("current_state",),
    "experiences": ("past_experiences",),
    "shared_memory": ("peer_findings",),
    "multimodal": ("multimodal_recipes",),
}

#: Gated by ``policy.allow_personal_memory``.  Narrow on purpose: a flag that
#: blocks more than it names turns incognito into "no context at all".
PERSONAL_SOURCE_IDS: Tuple[str, ...] = ("memory_engine", "personal_memory")

#: Gated by ``policy.allow_project_sources``.  ``code_index`` belongs here and
#: ``blocks``, ``experiences`` and ``multimodal`` deliberately do not, for the
#: reason stated one line above: this list must name exactly the sources whose
#: rows ``ranking.PROJECT_SOURCE_TYPES`` would refuse anyway — today
#: ``project_memory``, ``file``, ``symbol``, ``instruction`` and ``artifact``.
#: Every ``code_index`` row is a ``symbol``, so consulting it under this flag
#: buys a thread and a guaranteed omission.  A ``block``, an ``experience`` and
#: a ``recipe`` are not on that list: an incognito turn still gets to know who
#: it is and what has already been shown not to work, and a planner that gated
#: more than the ranker would be a second policy contradicting the first.
#: ``project_links`` is deliberately absent, and that is not an oversight:
#: its rows are ``document``, ``artifact``, ``file`` and ``recipe``, and the
#: ranker would let the first and the last through, so this list's own
#: contract ("exactly the sources whose rows the ranker would refuse")
#: cannot carry it without becoming a second, weaker policy.
#: ``ProjectLinksSource._gate`` enforces ``allow_project_sources`` itself,
#: on the event loop and before the store is consulted — a project's links
#: are project data by construction, and an incognito turn never sees one.
PROJECT_SOURCE_IDS: Tuple[str, ...] = ("project_memory", "files", "code_index")

#: Useless without ``execution.workspace``; asking them costs a thread each.
WORKSPACE_SOURCE_IDS: Tuple[str, ...] = ("files", "code_index")

#: Useless without ``execution.session_id``.
SESSION_SOURCE_IDS: Tuple[str, ...] = ("sessions",)

#: Useless without *some* scope to look under, where a session id is only one
#: of four that will do: a capsule is keyed by session, run, council or branch,
#: and a blackboard by the council or branch its workers share.  Deliberately
#: not folded into ``SESSION_SOURCE_IDS``, which means exactly what it says —
#: a delegated worker with a ``run_id`` and no ``session_id`` has a capsule to
#: resume from, and listing it there would skip the one source that could hand
#: it back.
SCOPED_SOURCE_IDS: Tuple[str, ...] = ("capsules", "shared_memory")


# ── what each intent and phase asks for ────────────────────────────────────

#: Always attempted: the mandatory sections plus the conversation itself.  They
#: are not ranked against anything (§6.3) and they are not optional, so an
#: intent cannot drop them.
BASE_SECTIONS: Tuple[str, ...] = tuple(MANDATORY_SECTIONS) + ("recent_messages",)

INTENT_SECTIONS: Dict[str, Tuple[str, ...]] = {
    # A conversation gets history and memory.  No RAG, no code map: this line
    # is the whole of rule 3, and tests/test_context_engine_compiler.py pins it.
    "chat": ("retrieved_memory",),
    "casual": ("retrieved_memory",),
    "voice": ("retrieved_memory",),
    "code_change": ("project_rules", "code_map", "retrieved_documents",
                    "past_experiences", "tool_guidance"),
    "bugfix": ("project_rules", "code_map", "retrieved_documents",
               "past_experiences", "tool_guidance"),
    "implement": ("project_rules", "code_map", "retrieved_documents",
                  "past_experiences", "tool_guidance"),
    "review": ("project_rules", "code_map", "retrieved_documents",
               "past_experiences", "peer_findings", "tool_guidance"),
    "code_review": ("project_rules", "code_map", "retrieved_documents",
                    "past_experiences", "peer_findings", "tool_guidance"),
    "research": ("retrieved_documents", "retrieved_memory", "past_experiences"),
    "deep_research": ("retrieved_documents", "retrieved_memory",
                      "past_experiences"),
    "media": ("multimodal_recipes", "retrieved_memory", "tool_guidance"),
    "image": ("multimodal_recipes", "retrieved_memory", "tool_guidance"),
    "video": ("multimodal_recipes", "retrieved_memory", "tool_guidance"),
    "audio": ("multimodal_recipes", "retrieved_memory", "tool_guidance"),
}

#: An intent nobody described gets the agentic middle, not nothing: a new
#: intent should degrade to "reasonable", the same way `profile_for` does.
DEFAULT_INTENT_SECTIONS: Tuple[str, ...] = (
    "project_rules", "retrieved_memory", "retrieved_documents", "tool_guidance",
)

#: §1.4 as section arithmetic rather than as score weights.
PHASE_ADD: Dict[str, Tuple[str, ...]] = {
    "plan": ("project_rules", "tool_guidance"),
    "act": (),
    "verify": ("past_experiences", "code_map", "retrieved_documents"),
    "summarize": (),
}
PHASE_DROP: Dict[str, Tuple[str, ...]] = {
    "plan": (),
    "act": (),
    # Verification is about evidence.  A remembered preference is not evidence,
    # and the slot it takes is one an actual test result wanted.
    "verify": ("retrieved_memory",),
    "summarize": ("code_map", "multimodal_recipes", "retrieved_documents"),
}

#: Retrieval lanes that are always in play.  `semantic` is added only when the
#: policy allows it; `graph` only where an edge means something.
BASE_LANES: Tuple[str, ...] = ("exact", "explicit", "mandatory", "lexical",
                               "temporal")
GRAPH_INTENTS: Tuple[str, ...] = ("code_change", "bugfix", "implement",
                                  "review", "code_review")

#: Per-source item ceilings that are tighter than the policy's.  A chat that
#: pulls eight memories has not been helped by six of them.
INTENT_ITEM_LIMITS: Dict[str, int] = {
    "chat": 4, "casual": 4, "voice": 3, "media": 6, "image": 6, "video": 6,
    "audio": 6,
}


@dataclass(frozen=True)
class RetrievalPlan:
    """What one retrieval round will attempt, and what it will not.

    Everything here is decided from the request alone, so two runs of the same
    turn plan identically — which is what lets Branching Futures claim two
    branches started from the same context instead of merely hoping so."""

    intent: str
    profile_id: str
    sections: Tuple[str, ...]
    source_ids: Tuple[str, ...]
    lanes: Tuple[str, ...]
    per_source_limit: int
    timeout_s: float
    reasons: Mapping[str, str]
    skipped: Mapping[str, str]

    def wants(self, section: str) -> bool:
        return section in self.sections

    def consults(self, source_id: str) -> bool:
        return source_id in self.source_ids

    def to_dict(self) -> Dict[str, object]:
        return {
            "intent": self.intent,
            "profile_id": self.profile_id,
            "sections": list(self.sections),
            "source_ids": list(self.source_ids),
            "lanes": list(self.lanes),
            "per_source_limit": self.per_source_limit,
            "timeout_s": self.timeout_s,
            "reasons": dict(self.reasons),
            "skipped": dict(self.skipped),
        }


# ── intent ─────────────────────────────────────────────────────────────────

def classify_intent(request: ContextRequest, *, hint: str = "") -> str:
    """The intent of this step, from the request alone.

    See the module docstring for the signal table.  Never raises and never
    calls anything: a classifier that can fail is a turn that can fail before
    it has started."""
    try:
        declared = (request.task.intent or "").strip().lower()
        if declared and declared != DEFAULT_INTENT:
            return declared
        hinted = (hint or "").strip().lower()
        if hinted and hinted != DEFAULT_INTENT:
            return hinted
        if (request.consumer or "") == "voice":
            return "voice"

        query = request.task.query or ""
        workspace = (request.execution.workspace or "").strip()
        if _MEDIA.search(query):
            return "media"
        if workspace and _REVIEW.search(query):
            return "review"
        if workspace and (_CODE.search(query) or PATH_HINT.search(query)):
            return "code_change"
        if _RESEARCH.search(query):
            return "research"
    except Exception:  # noqa: BLE001 - classification may never break a turn
        logger.debug("context intent classification failed; using %r", DEFAULT_INTENT)
    return DEFAULT_INTENT


# ── sections ───────────────────────────────────────────────────────────────

def sections_for(intent: str, phase: str) -> Tuple[str, ...]:
    """The sections this intent and phase will try to fill, in render order."""
    wanted: List[str] = list(BASE_SECTIONS)
    wanted.extend(INTENT_SECTIONS.get(intent, DEFAULT_INTENT_SECTIONS))
    wanted.extend(PHASE_ADD.get(phase, ()))
    dropped = set(PHASE_DROP.get(phase, ()))
    # A mandatory section is never dropped by a phase: it is mandatory.
    dropped -= set(MANDATORY_SECTIONS)
    kinds = {k for k in wanted if k in SECTION_KINDS} - dropped
    return tuple(section_order(kinds))


# ── the plan ───────────────────────────────────────────────────────────────

def _timeout_ms() -> int:
    try:
        from src.settings import get_setting

        raw = get_setting(TIMEOUT_SETTING, DEFAULT_TIMEOUT_MS)
        value = int(raw)
    except Exception:  # noqa: BLE001 - an unreadable setting is the default
        return DEFAULT_TIMEOUT_MS
    return value if value > 0 else DEFAULT_TIMEOUT_MS


def timeout_for(request: ContextRequest) -> float:
    """The retrieval deadline for this consumer, in seconds."""
    seconds = _timeout_ms() / 1000.0
    if (request.consumer or "") == "voice":
        seconds *= VOICE_TIMEOUT_FACTOR
    return max(MIN_TIMEOUT_S, min(seconds, MAX_TIMEOUT_S))


def _lanes_for(request: ContextRequest, intent: str) -> Tuple[str, ...]:
    lanes: List[str] = [lane for lane in BASE_LANES if lane in RETRIEVAL_LANES]
    if request.policy.allow_semantic_lane:
        lanes.append("semantic")
    if intent in GRAPH_INTENTS:
        lanes.append("graph")
    return tuple(lanes)


def _limit_for(request: ContextRequest, intent: str) -> int:
    try:
        ceiling = int(request.policy.max_items_per_source or 8)
    except (TypeError, ValueError):
        ceiling = 8
    ceiling = max(1, ceiling)
    return max(1, min(ceiling, INTENT_ITEM_LIMITS.get(intent, ceiling)))


def _source_verdict(source_id: str, request: ContextRequest,
                    wanted: Sequence[str]) -> Tuple[bool, str]:
    """`(consult, reason)` for one source.  The reason is recorded either way."""
    policy = request.policy
    execution = request.execution

    if source_id in PERSONAL_SOURCE_IDS and not policy.allow_personal_memory:
        return False, "personal memory is off for this request"
    if source_id in PROJECT_SOURCE_IDS and not policy.allow_project_sources:
        return False, "project sources are off for this request"
    if source_id in WORKSPACE_SOURCE_IDS and not (execution.workspace or "").strip():
        return False, "no workspace on the request"
    if source_id in SESSION_SOURCE_IDS and not (execution.session_id or "").strip():
        return False, "no session_id on the request"
    if source_id in SCOPED_SOURCE_IDS and not any(
            (value or "").strip() for value in (execution.council_id, execution.branch_id,
                                                execution.run_id, execution.session_id)):
        return False, "no council, branch, run or session to scope it to"

    produces = SOURCE_SECTIONS.get(source_id)
    if produces is None:
        return True, "declares no sections to the planner; gather applies the gate"
    overlap = [kind for kind in produces if kind in wanted]
    if not overlap:
        return False, f"produces {', '.join(produces)}; this turn wants none of it"
    return True, f"fills {', '.join(overlap)}"


def plan(request: ContextRequest, *, available: Sequence[str] = (),
         now: Optional[datetime] = None) -> RetrievalPlan:
    """What this turn will retrieve, from whom, and within what deadline.

    `available` is the set of source ids the caller actually has; with none
    given, the planner answers for every source it knows about, which is what a
    diagnostic ("what would you consult for this?") wants.

    `now` is accepted for symmetry with the rest of the package and is not
    read: a plan that depended on the clock could not be reproduced, and
    reproducing one is how two branches are shown to have started equal."""
    intent = classify_intent(request)
    phase = request.task.phase or "act"
    wanted = sections_for(intent, phase)

    explicit_profile = request.policy.context_profile_id or ""
    if explicit_profile == DEFAULT_PROFILE_ID:
        explicit_profile = ""
    profile = profile_for(profile_id=explicit_profile, intent=intent)

    ids: List[str] = []
    for source_id in (available or tuple(SOURCE_SECTIONS)):
        name = str(source_id or "").strip()
        if name and name not in ids:
            ids.append(name)

    reasons: Dict[str, str] = {}
    skipped: Dict[str, str] = {}
    chosen: List[str] = []
    for source_id in ids:
        consult, reason = _source_verdict(source_id, request, wanted)
        if consult:
            chosen.append(source_id)
            reasons[source_id] = reason
        else:
            skipped[source_id] = reason

    built = RetrievalPlan(
        intent=intent,
        profile_id=profile.profile_id,
        sections=wanted,
        source_ids=tuple(chosen),
        lanes=_lanes_for(request, intent),
        per_source_limit=_limit_for(request, intent),
        timeout_s=timeout_for(request),
        reasons=dict(reasons),
        skipped=dict(skipped),
    )
    logger.debug("context plan: intent=%s phase=%s sources=%d skipped=%d",
                 built.intent, phase, len(built.source_ids), len(built.skipped))
    return built


def explain(plan_: RetrievalPlan) -> str:
    """The plan as a few lines of plain text, for a log or a bug report."""
    lines = [
        f"intent={plan_.intent} profile={plan_.profile_id} "
        f"limit={plan_.per_source_limit} timeout={plan_.timeout_s:.2f}s",
        "  sections  " + ", ".join(plan_.sections),
        "  lanes     " + ", ".join(plan_.lanes),
    ]
    for source_id in plan_.source_ids:
        lines.append(f"  consult   {source_id}: {plan_.reasons.get(source_id, '')}")
    for source_id in sorted(plan_.skipped):
        lines.append(f"  skip      {source_id}: {plan_.skipped[source_id]}")
    return "\n".join(lines)


def known_sources() -> Tuple[str, ...]:
    """Every source id the planner has a section list for."""
    return tuple(sorted(SOURCE_SECTIONS))


__all__ = [
    "DEFAULT_INTENT", "DEFAULT_PROFILE_ID", "TIMEOUT_SETTING",
    "DEFAULT_TIMEOUT_MS", "VOICE_TIMEOUT_FACTOR",
    "MEDIA_SIGNALS", "REVIEW_SIGNALS", "CODE_SIGNALS", "RESEARCH_SIGNALS",
    "PATH_HINT", "SOURCE_SECTIONS", "PERSONAL_SOURCE_IDS",
    "PROJECT_SOURCE_IDS", "WORKSPACE_SOURCE_IDS", "SESSION_SOURCE_IDS",
    "SCOPED_SOURCE_IDS",
    "BASE_SECTIONS", "INTENT_SECTIONS", "DEFAULT_INTENT_SECTIONS",
    "PHASE_ADD", "PHASE_DROP", "BASE_LANES", "GRAPH_INTENTS",
    "INTENT_ITEM_LIMITS",
    "RetrievalPlan", "classify_intent", "sections_for", "timeout_for",
    "plan", "explain", "known_sources",
]
