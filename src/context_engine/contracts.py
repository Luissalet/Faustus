"""
context_engine/contracts.py — what a model is allowed to be told, and why.

Faustus already stores plenty: learned memory, project memory, objectives,
documents, expert corpora, provenance, changesets, artifacts.  What it did not
have was a single answer to the only question that matters at call time:

    which authorised, still-true, useful piece of information does THIS actor
    need for THIS step, inside THIS budget, and where did it come from?

These are the shapes of that answer.  `ContextRequest` is the question,
`ContextPacket` is what gets rendered into the prompt, and `ContextReceipt` is
what happened afterwards.  Nothing here reads a database, opens a file, calls a
model or looks at the clock except through an injected `now` — the compiler
does all of that, and it does it against these types.

Three rules inherited from `src/contracts/base.py`, for the same reasons:
a rejection names the field and the value; an unknown key is an error and never
a default; nothing is coerced across a type boundary.

Two rules that only exist here:

4. **Nothing enters a packet without a source.**  `ContextItem.source_ref` is
   required and `source_type` is a closed list.  A sentence with no provenance
   is a sentence the system cannot later invalidate, and the whole point of
   this subsystem is that it can.

5. **An omission is data.**  `ContextOmission` is not logging; it is part of
   the packet.  "Why did it not read that file?" has to be answerable from the
   packet alone, months later, without the sources still being around.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src.contracts.base import (
    SCHEMA_VERSION,
    ContractError,
    as_mapping,
    fingerprint,
    ident,
    now_iso,
    one_of,
    reject_unknown,
    text,
    text_list,
    timestamp,
    whole,
    flag,
)

# ── closed vocabularies ────────────────────────────────────────────────────
#
# Each of these is closed on purpose.  A section kind that anyone can invent is
# a section kind no budget policy can price and no test can assert on; the cost
# of adding one here is a one-line diff and a reason.

SECTION_KINDS: Tuple[str, ...] = (
    "system_constraints",
    "role_and_permissions",
    "active_goal",
    "current_state",
    "recent_messages",
    "decisions",
    "project_rules",
    "code_map",
    "retrieved_memory",
    "retrieved_documents",
    "past_experiences",
    "multimodal_recipes",
    "peer_findings",
    "tool_guidance",
)

#: Sections that are never subject to relevance ranking.  §6.3 of the plan:
#: safety, identity, authorisation, the live goal and binding decisions are
#: either present or the packet is degraded — they do not compete with a
#: semantically similar memory for a slot.
MANDATORY_SECTIONS: Tuple[str, ...] = (
    "system_constraints",
    "role_and_permissions",
    "active_goal",
    "current_state",
    "decisions",
)

SOURCE_TYPES: Tuple[str, ...] = (
    "memory",          # src.memory_engine / src.memory
    "project_memory",  # <workspace>/.odysseus/*.md
    "objective",       # services.objectives
    "decision",        # a recorded, binding decision
    "file",            # a file in the workspace
    "symbol",          # an entry in the structural code index
    "document",        # a RAG chunk
    "expert",          # an expert corpus chunk
    "experience",      # a verified run, distilled
    "artifact",        # src.artifact_store
    "recipe",          # a multimodal generation recipe
    "message",         # a turn from this or another session
    "state",           # a State Mirror projection
    "delta",           # a Universal Delta Engine result
    "finding",         # a shared blackboard entry
    "capability",      # a skill / tool / workflow description
    "block",           # an attachable context block
    "capsule",         # a resumption capsule
    "instruction",     # AGENTS.md, project instructions, presets
    "web",             # fetched page or search result
)

#: Ordered least-lossy first.  `transforms.py` may only ever move an item DOWN
#: this list, never up, and the manifest records where it stopped.
TRANSFORMATIONS: Tuple[str, ...] = (
    "reference",        # id and title only; no content
    "verbatim",         # the whole thing, unchanged
    "excerpt",          # a literal contiguous slice
    "structured",       # normalised into a table/record, no prose invented
    "projected",        # irrelevant fields dropped
    "extractive",       # sentences selected from the source, not rewritten
    "generated",        # a model wrote this summary
)

#: A transformation a model produced.  Anything at or past this point in
#: `TRANSFORMATIONS` must be labelled as such in the rendered packet, because
#: the reader can no longer quote it as the source's own words.
GENERATIVE_TRANSFORMATIONS: Tuple[str, ...] = ("generated",)

OMISSION_REASONS: Tuple[str, ...] = (
    "budget",
    "duplicate",
    "low_confidence",
    "stale",
    "unauthorised",
    "contradicted",
    "irrelevant",
    "quarantined",
    "unavailable",
    "policy",
    # CTX-05: the user said "don't use this source" (or its folder) for this
    # request's scope — a preference, not an authorisation failure, and
    # never a deletion of the source itself (see `ContextPolicy.excluded_refs`).
    "user_excluded",
)

RETRIEVAL_LANES: Tuple[str, ...] = (
    "exact",      # an id or path was named
    "lexical",    # BM25 / keyword
    "semantic",   # embeddings
    "graph",      # provenance / dependency edges
    "temporal",   # recency window
    "explicit",   # the caller passed it in `explicit_refs`
    "mandatory",  # placed by policy, not found by a search
)

TASK_PHASES: Tuple[str, ...] = ("plan", "act", "verify", "summarize")

CONSUMERS: Tuple[str, ...] = (
    "agent", "chat", "council", "branch", "workflow", "voice", "shadow",
)

FEEDBACK_KINDS: Tuple[str, ...] = ("helpful", "mixed", "unused", "harmful", "unknown")

TRIM_POLICIES: Tuple[str, ...] = ("drop_lowest", "truncate_tail", "never", "summarize")

#: Trust classes, mirrored from `src.memory_engine.TRUST_CLASSES` plus the two
#: classes only this layer can see.  The numbers are authority ceilings, not
#: probabilities: they cap how far an item can climb, never how true it is.
TRUST_CLASSES: Dict[str, float] = {
    "observed": 0.95,          # read from disk / an API just now
    "human_explicit": 0.85,    # the user said it
    "proved": 0.80,            # a `prove` verdict backs it
    "agent_validated": 0.65,
    "agent_assertion": 0.50,
    "legacy_import": 0.30,
    "untrusted": 0.20,         # web page, third-party document, peer draft
}

#: §14.2, as a lookup instead of prose.  Higher wins a contradiction.  These
#: are not multiplied into the score; they decide which of two *conflicting*
#: items is presented as current, which is a different question from which is
#: more relevant.
AUTHORITY_ORDER: Dict[str, int] = {
    "user_instruction": 90,
    "system_policy": 80,
    "observed_state": 70,
    "binding_decision": 60,
    "proved_result": 50,
    "human_memory": 40,
    "validated_experience": 30,
    "agent_claim": 20,
    "inference": 10,
}


def new_id(prefix: str) -> str:
    """`ctxpkt_9f2c…`.  Short enough to paste into a bug report, long enough
    that two runs on two machines will not collide in the same ledger."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _ratio(data: Mapping[str, Any], key: str, path: str, *,
           default: Optional[float] = None, required: bool = False,
           minimum: float = 0.0, maximum: float = 1.0) -> Optional[float]:
    """A score in [0, 1].  `int` is accepted because 0 and 1 are the two most
    common literal scores and refusing them would be pedantry, not safety;
    `bool` is not, because `True` is not a confidence of 1.0."""
    raw = data.get(key, None)
    if raw is None:
        if required:
            raise ContractError(f"{path}.{key}", "is required (a number in [0, 1])")
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ContractError(f"{path}.{key}", "expected a number in [0, 1]", got=raw)
    value = float(raw)
    if value < minimum or value > maximum:
        raise ContractError(f"{path}.{key}", f"must be between {minimum} and {maximum}", got=raw)
    return value


def _ts(data: Mapping[str, Any], key: str, path: str) -> str:
    """An ISO-8601 timestamp, or `""` for "not recorded".

    `to_dict()` writes `""` when a packet has no timestamp, so `parse()` has to
    read `""` back as absent.  A contract that cannot re-read its own output is
    not a contract, it is a one-way serialiser — and every one of these objects
    round-trips through the ledger, the API and the branch comparator."""
    raw = data.get(key, None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    return timestamp(data, key, path, default="") or ""


def _str_map(data: Mapping[str, Any], key: str, path: str) -> Dict[str, float]:
    """A `{name: score}` bag — used for `ContextItem.scores`, where the set of
    signals is genuinely open (a reranker may add one) but every value has to
    be a number, so a stray string cannot silently sort first."""
    raw = data.get(key, None)
    if raw is None:
        return {}
    bag = as_mapping(raw, f"{path}.{key}")
    out: Dict[str, float] = {}
    for name, value in bag.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractError(f"{path}.{key}.{name}", "expected a number", got=value)
        out[str(name)] = float(value)
    return out


# ── the question ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ContextActor:
    """Who the packet is for.  Not who asked for it — a coordinator compiles
    packets for its workers, and the worker's limits are the ones that apply."""

    agent_id: str = ""
    role: str = ""
    model: str = ""
    participant_id: str = ""
    _KEYS = ("agent_id", "role", "model", "participant_id")

    @classmethod
    def parse(cls, raw: Any, path: str = "actor") -> "ContextActor":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            agent_id=text(data, "agent_id", path, required=False, max_len=128),
            role=text(data, "role", path, required=False, max_len=64),
            model=text(data, "model", path, required=False, max_len=256),
            participant_id=text(data, "participant_id", path, required=False, max_len=128),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"agent_id": self.agent_id, "role": self.role,
                "model": self.model, "participant_id": self.participant_id}


@dataclass(frozen=True)
class ContextExecution:
    """The scope.  Every one of these comes from the runtime; none of them may
    be read out of a model's message.  That is the whole isolation story: an
    actor can ask for information, and cannot ask to be someone else."""

    owner: str = ""
    session_id: str = ""
    run_id: str = ""
    project_id: str = ""
    workspace: str = ""
    council_id: str = ""
    branch_id: str = ""
    turn_id: str = ""
    _KEYS = ("owner", "session_id", "run_id", "project_id", "workspace",
             "council_id", "branch_id", "turn_id")

    @classmethod
    def parse(cls, raw: Any, path: str = "execution") -> "ContextExecution":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            owner=text(data, "owner", path, required=False, max_len=256),
            session_id=text(data, "session_id", path, required=False, max_len=128),
            run_id=text(data, "run_id", path, required=False, max_len=128),
            project_id=text(data, "project_id", path, required=False, max_len=128),
            workspace=text(data, "workspace", path, required=False, max_len=1024),
            council_id=text(data, "council_id", path, required=False, max_len=128),
            branch_id=text(data, "branch_id", path, required=False, max_len=128),
            turn_id=text(data, "turn_id", path, required=False, max_len=128),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "owner": self.owner, "session_id": self.session_id, "run_id": self.run_id,
            "project_id": self.project_id, "workspace": self.workspace,
            "council_id": self.council_id, "branch_id": self.branch_id,
            "turn_id": self.turn_id,
        }

    def scope_key(self) -> str:
        """The one string two packets must share before anything may be reused
        between them.  Deliberately excludes run/turn: caching across turns of
        the same session is fine, across owners is not."""
        return "|".join((self.owner, self.project_id or self.workspace,
                         self.council_id, self.branch_id))


@dataclass(frozen=True)
class ContextTask:
    """What the step is.  `phase` exists so that planning gets instructions and
    verification gets evidence, rather than both getting the same average."""

    intent: str = "chat"
    phase: str = "act"
    scope_envelope_id: str = ""
    required_evidence: Tuple[str, ...] = ()
    query: str = ""
    _KEYS = ("intent", "phase", "scope_envelope_id", "required_evidence", "query")

    @classmethod
    def parse(cls, raw: Any, path: str = "task") -> "ContextTask":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            intent=text(data, "intent", path, required=False, default="chat", max_len=64),
            phase=one_of(data, "phase", path, choices=TASK_PHASES,
                         required=False, default="act") or "act",
            scope_envelope_id=text(data, "scope_envelope_id", path, required=False, max_len=128),
            required_evidence=text_list(data, "required_evidence", path, max_items=64),
            query=text(data, "query", path, required=False, max_len=8192, allow_blank=True),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"intent": self.intent, "phase": self.phase,
                "scope_envelope_id": self.scope_envelope_id,
                "required_evidence": list(self.required_evidence),
                "query": self.query}


@dataclass(frozen=True)
class ContextPolicy:
    """What the actor is allowed to be given, and how much of it.

    `token_budget` is a request, not a grant: the compiler clamps it against
    the model's real window.  `allow_personal_memory=False` is what incognito
    means here, and it is a hard gate applied before retrieval, never a filter
    applied after ranking — a search that ran already leaked the timing."""

    context_profile_id: str = "balanced_v1"
    token_budget: int = 0
    allow_personal_memory: bool = True
    allow_project_sources: bool = True
    allow_semantic_lane: bool = True
    minimum_freshness_s: Optional[int] = None
    max_items_per_source: int = 8
    #: CTX-05: refs and folder/prefix strings the user excluded FOR THIS
    #: REQUEST's scope — "no usar esta fuente".  Matched against
    #: `ContextCandidate.source_ref`; never touches the file on disk and never
    #: reaches past the scope this policy was built for (a session-scoped
    #: request never inherits a project-scoped exclusion silently — the
    #: caller that builds the policy is the one that decides which list a
    #: given exclusion belongs in; see `src/context_selection.py`).
    excluded_refs: Tuple[str, ...] = ()
    excluded_prefixes: Tuple[str, ...] = ()
    _KEYS = ("context_profile_id", "token_budget", "allow_personal_memory",
             "allow_project_sources", "allow_semantic_lane", "minimum_freshness_s",
             "max_items_per_source", "excluded_refs", "excluded_prefixes")

    @classmethod
    def parse(cls, raw: Any, path: str = "policy") -> "ContextPolicy":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            context_profile_id=text(data, "context_profile_id", path, required=False,
                                    default="balanced_v1", max_len=64),
            token_budget=whole(data, "token_budget", path, default=0, minimum=0) or 0,
            allow_personal_memory=flag(data, "allow_personal_memory", path, default=True),
            allow_project_sources=flag(data, "allow_project_sources", path, default=True),
            allow_semantic_lane=flag(data, "allow_semantic_lane", path, default=True),
            minimum_freshness_s=whole(data, "minimum_freshness_s", path, default=None, minimum=0),
            max_items_per_source=whole(data, "max_items_per_source", path,
                                       default=8, minimum=1, maximum=200) or 8,
            excluded_refs=text_list(data, "excluded_refs", path, max_items=256, max_len=2048),
            excluded_prefixes=text_list(data, "excluded_prefixes", path,
                                        max_items=128, max_len=1024),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "context_profile_id": self.context_profile_id,
            "token_budget": self.token_budget,
            "allow_personal_memory": self.allow_personal_memory,
            "allow_project_sources": self.allow_project_sources,
            "allow_semantic_lane": self.allow_semantic_lane,
            "minimum_freshness_s": self.minimum_freshness_s,
            "max_items_per_source": self.max_items_per_source,
            "excluded_refs": list(self.excluded_refs),
            "excluded_prefixes": list(self.excluded_prefixes),
        }


@dataclass(frozen=True)
class ContextRequest:
    """§1.4.  The thing a consumer builds instead of concatenating sources.

    There is exactly one of these per model call that goes through the engine.
    If a consumer finds itself building two, it wanted two calls."""

    request_id: str = ""
    actor: ContextActor = field(default_factory=ContextActor)
    execution: ContextExecution = field(default_factory=ContextExecution)
    task: ContextTask = field(default_factory=ContextTask)
    policy: ContextPolicy = field(default_factory=ContextPolicy)
    explicit_refs: Tuple[str, ...] = ()
    consumer: str = "agent"
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION
    _KEYS = ("request_id", "actor", "execution", "task", "policy",
             "explicit_refs", "consumer", "created_at", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "context_request") -> "ContextRequest":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            request_id=text(data, "request_id", path, required=False,
                            default=new_id("ctxreq"), max_len=128),
            actor=ContextActor.parse(data.get("actor") or {}, f"{path}.actor"),
            execution=ContextExecution.parse(data.get("execution") or {}, f"{path}.execution"),
            task=ContextTask.parse(data.get("task") or {}, f"{path}.task"),
            policy=ContextPolicy.parse(data.get("policy") or {}, f"{path}.policy"),
            explicit_refs=text_list(data, "explicit_refs", path, max_items=256, max_len=1024),
            consumer=one_of(data, "consumer", path, choices=CONSUMERS,
                            required=False, default="agent") or "agent",
            created_at=_ts(data, "created_at", path),
            schema_version=whole(data, "schema_version", path,
                                 default=SCHEMA_VERSION, minimum=1) or SCHEMA_VERSION,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "actor": self.actor.to_dict(),
            "execution": self.execution.to_dict(),
            "task": self.task.to_dict(),
            "policy": self.policy.to_dict(),
            "explicit_refs": list(self.explicit_refs),
            "consumer": self.consumer,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }


# ── candidates: what a source hands back before anything is decided ────────

@dataclass(frozen=True)
class ContextCandidate:
    """One thing a source thinks might be worth knowing.

    A candidate is not yet an item: it has not been validated, deduplicated,
    ranked, budgeted or transformed.  Sources produce these and nothing else,
    which is what keeps `compiler.py` from importing eleven subsystems'
    internals."""

    candidate_id: str = ""
    source_type: str = "memory"
    source_ref: str = ""
    title: str = ""
    body: str = ""
    section: str = "retrieved_memory"
    lanes: Tuple[str, ...] = ()
    scores: Mapping[str, float] = field(default_factory=dict)
    trust_class: str = "agent_assertion"
    authority: str = "agent_claim"
    source_revision: str = ""
    observed_at: str = ""
    owner: str = ""
    project_id: str = ""
    degraded: bool = False
    meta: Mapping[str, Any] = field(default_factory=dict)
    _KEYS = ("candidate_id", "source_type", "source_ref", "title", "body", "section",
             "lanes", "scores", "trust_class", "authority", "source_revision",
             "observed_at", "owner", "project_id", "degraded", "meta")

    @classmethod
    def parse(cls, raw: Any, path: str = "candidate") -> "ContextCandidate":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        source_type = one_of(data, "source_type", path, choices=SOURCE_TYPES) or "memory"
        source_ref = text(data, "source_ref", path, max_len=2048)
        section = one_of(data, "section", path, choices=SECTION_KINDS,
                         required=False, default="retrieved_memory") or "retrieved_memory"
        trust_class = one_of(data, "trust_class", path, choices=tuple(TRUST_CLASSES),
                             required=False, default="agent_assertion") or "agent_assertion"
        authority = one_of(data, "authority", path, choices=tuple(AUTHORITY_ORDER),
                           required=False, default="agent_claim") or "agent_claim"
        return cls(
            candidate_id=text(data, "candidate_id", path, required=False,
                              default=new_id("ctxcand"), max_len=128),
            source_type=source_type,
            source_ref=source_ref,
            title=text(data, "title", path, required=False, max_len=512),
            body=text(data, "body", path, required=False, max_len=1_000_000, allow_blank=True),
            section=section,
            lanes=text_list(data, "lanes", path, choices=RETRIEVAL_LANES, max_items=8),
            scores=_str_map(data, "scores", path),
            trust_class=trust_class,
            authority=authority,
            source_revision=text(data, "source_revision", path, required=False, max_len=128),
            observed_at=_ts(data, "observed_at", path),
            owner=text(data, "owner", path, required=False, max_len=256),
            project_id=text(data, "project_id", path, required=False, max_len=128),
            degraded=flag(data, "degraded", path, default=False),
            meta=dict(as_mapping(data.get("meta") or {}, f"{path}.meta")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id, "source_type": self.source_type,
            "source_ref": self.source_ref, "title": self.title, "body": self.body,
            "section": self.section, "lanes": list(self.lanes),
            "scores": dict(self.scores), "trust_class": self.trust_class,
            "authority": self.authority, "source_revision": self.source_revision,
            "observed_at": self.observed_at, "owner": self.owner,
            "project_id": self.project_id, "degraded": self.degraded,
            "meta": dict(self.meta),
        }

    def trust(self) -> float:
        return TRUST_CLASSES.get(self.trust_class, 0.5)

    def authority_rank(self) -> int:
        return AUTHORITY_ORDER.get(self.authority, 0)


# ── the answer ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ContextItem:
    """One thing the model will actually be told, and where it came from.

    `tokens` is what the item costs in this packet, measured with the same
    estimator the budget used.  It is stored rather than recomputed because a
    packet has to be auditable after the estimator has been improved."""

    item_id: str = ""
    source_type: str = "memory"
    source_ref: str = ""
    title: str = ""
    body: str = ""
    transformation: str = "verbatim"
    lanes: Tuple[str, ...] = ()
    scores: Mapping[str, float] = field(default_factory=dict)
    trust_class: str = "agent_assertion"
    authority: str = "agent_claim"
    source_revision: str = ""
    observed_at: str = ""
    chars: int = 0
    tokens: int = 0
    reason: str = ""
    degraded: bool = False
    _KEYS = ("item_id", "source_type", "source_ref", "title", "body", "transformation",
             "lanes", "scores", "trust_class", "authority", "source_revision",
             "observed_at", "chars", "tokens", "reason", "degraded")

    @classmethod
    def parse(cls, raw: Any, path: str = "item") -> "ContextItem":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            item_id=text(data, "item_id", path, required=False,
                         default=new_id("ctxitem"), max_len=128),
            source_type=one_of(data, "source_type", path, choices=SOURCE_TYPES) or "memory",
            source_ref=text(data, "source_ref", path, max_len=2048),
            title=text(data, "title", path, required=False, max_len=512),
            body=text(data, "body", path, required=False, max_len=1_000_000, allow_blank=True),
            transformation=one_of(data, "transformation", path, choices=TRANSFORMATIONS,
                                  required=False, default="verbatim") or "verbatim",
            lanes=text_list(data, "lanes", path, choices=RETRIEVAL_LANES, max_items=8),
            scores=_str_map(data, "scores", path),
            trust_class=one_of(data, "trust_class", path, choices=tuple(TRUST_CLASSES),
                               required=False, default="agent_assertion") or "agent_assertion",
            authority=one_of(data, "authority", path, choices=tuple(AUTHORITY_ORDER),
                             required=False, default="agent_claim") or "agent_claim",
            source_revision=text(data, "source_revision", path, required=False, max_len=128),
            observed_at=_ts(data, "observed_at", path),
            chars=whole(data, "chars", path, default=0, minimum=0) or 0,
            tokens=whole(data, "tokens", path, default=0, minimum=0) or 0,
            reason=text(data, "reason", path, required=False, max_len=512),
            degraded=flag(data, "degraded", path, default=False),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id, "source_type": self.source_type,
            "source_ref": self.source_ref, "title": self.title, "body": self.body,
            "transformation": self.transformation, "lanes": list(self.lanes),
            "scores": dict(self.scores), "trust_class": self.trust_class,
            "authority": self.authority, "source_revision": self.source_revision,
            "observed_at": self.observed_at, "chars": self.chars, "tokens": self.tokens,
            "reason": self.reason, "degraded": self.degraded,
        }

    def is_generated(self) -> bool:
        return self.transformation in GENERATIVE_TRANSFORMATIONS


@dataclass(frozen=True)
class ContextSection:
    """A priced bucket.  `trim_policy="never"` is how a mandatory section says
    it would rather the packet be marked degraded than be quietly shortened."""

    kind: str = "retrieved_memory"
    priority: int = 50
    items: Tuple[ContextItem, ...] = ()
    budget_tokens: int = 0
    trim_policy: str = "drop_lowest"
    #: "tokens" is emitted by `to_dict()` and derived by `tokens()`; it is
    #: listed here only so that a round-trip through JSON parses back.
    _KEYS = ("kind", "priority", "items", "budget_tokens", "trim_policy", "tokens")

    @classmethod
    def parse(cls, raw: Any, path: str = "section") -> "ContextSection":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        raw_items = data.get("items") or []
        if not isinstance(raw_items, (list, tuple)):
            raise ContractError(f"{path}.items", "expected a list", got=raw_items)
        return cls(
            kind=one_of(data, "kind", path, choices=SECTION_KINDS) or "retrieved_memory",
            priority=whole(data, "priority", path, default=50, minimum=0, maximum=100) or 0,
            items=tuple(ContextItem.parse(i, f"{path}.items[{n}]")
                        for n, i in enumerate(raw_items)),
            budget_tokens=whole(data, "budget_tokens", path, default=0, minimum=0) or 0,
            trim_policy=one_of(data, "trim_policy", path, choices=TRIM_POLICIES,
                               required=False, default="drop_lowest") or "drop_lowest",
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "priority": self.priority,
            "items": [i.to_dict() for i in self.items],
            "budget_tokens": self.budget_tokens, "trim_policy": self.trim_policy,
            "tokens": self.tokens(),
        }

    def tokens(self) -> int:
        return sum(i.tokens for i in self.items)

    def is_mandatory(self) -> bool:
        return self.kind in MANDATORY_SECTIONS


@dataclass(frozen=True)
class ContextOmission:
    """Rule 5.  What was left out, and why, in a form a person can act on.

    `recoverable` is the field that matters at runtime: it tells the agent that
    the thing exists and can still be opened with a tool, which is the whole
    point of leaving it out instead of never mentioning it."""

    source_type: str = "memory"
    source_ref: str = ""
    reason: str = "budget"
    score: float = 0.0
    recoverable: bool = True
    detail: str = ""
    _KEYS = ("source_type", "source_ref", "reason", "score", "recoverable", "detail")

    @classmethod
    def parse(cls, raw: Any, path: str = "omission") -> "ContextOmission":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            source_type=one_of(data, "source_type", path, choices=SOURCE_TYPES) or "memory",
            source_ref=text(data, "source_ref", path, required=False, max_len=2048),
            reason=one_of(data, "reason", path, choices=OMISSION_REASONS) or "budget",
            score=_ratio(data, "score", path, default=0.0, maximum=1000.0) or 0.0,
            recoverable=flag(data, "recoverable", path, default=True),
            detail=text(data, "detail", path, required=False, max_len=512),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"source_type": self.source_type, "source_ref": self.source_ref,
                "reason": self.reason, "score": self.score,
                "recoverable": self.recoverable, "detail": self.detail}


@dataclass(frozen=True)
class ContextBudget:
    """The arithmetic, in one object, so that no consumer has to redo it.

    Invariant, checked in `__post_init__` because getting it wrong is silent:
    `input_budget + reserved_output + reserved_tools <= max_tokens`.  A model
    that runs out of room to answer has been given a perfect context and no
    way to use it."""

    max_tokens: int = 0
    reserved_output: int = 0
    reserved_tools: int = 0
    input_budget: int = 0
    estimator: str = "heuristic"
    window_known: bool = False
    _KEYS = ("max_tokens", "reserved_output", "reserved_tools", "input_budget",
             "estimator", "window_known")

    def __post_init__(self) -> None:
        total = self.input_budget + self.reserved_output + self.reserved_tools
        if self.max_tokens and total > self.max_tokens:
            raise ContractError(
                "budget",
                "input_budget + reserved_output + reserved_tools must fit in max_tokens "
                f"({self.input_budget} + {self.reserved_output} + {self.reserved_tools} "
                f"> {self.max_tokens})",
            )

    @classmethod
    def parse(cls, raw: Any, path: str = "window") -> "ContextBudget":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            max_tokens=whole(data, "max_tokens", path, default=0, minimum=0) or 0,
            reserved_output=whole(data, "reserved_output", path, default=0, minimum=0) or 0,
            reserved_tools=whole(data, "reserved_tools", path, default=0, minimum=0) or 0,
            input_budget=whole(data, "input_budget", path, default=0, minimum=0) or 0,
            estimator=text(data, "estimator", path, required=False,
                           default="heuristic", max_len=64),
            window_known=flag(data, "window_known", path, default=False),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"max_tokens": self.max_tokens, "reserved_output": self.reserved_output,
                "reserved_tools": self.reserved_tools, "input_budget": self.input_budget,
                "estimator": self.estimator, "window_known": self.window_known}


@dataclass(frozen=True)
class ContextPacket:
    """§5.  What one model call is told, with the receipt for every sentence.

    A packet is immutable once rendered.  `with_sections()` exists so that the
    trimming passes can produce a new one instead of mutating the one that was
    already measured — the manifest of a packet has to describe that packet."""

    packet_id: str = ""
    request_id: str = ""
    owner: str = ""
    project_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    participant_id: str = ""
    branch_id: str = ""
    model: str = ""
    intent: str = "chat"
    phase: str = "act"
    consumer: str = "agent"
    window: ContextBudget = field(default_factory=ContextBudget)
    sections: Tuple[ContextSection, ...] = ()
    omissions: Tuple[ContextOmission, ...] = ()
    warnings: Tuple[str, ...] = ()
    degraded: bool = False
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION
    _KEYS = ("packet_id", "request_id", "owner", "project_id", "session_id", "turn_id",
             "participant_id", "branch_id", "model", "intent", "phase", "consumer",
             "window", "sections", "omissions", "warnings", "degraded",
             "created_at", "schema_version", "manifest", "tokens")

    @classmethod
    def parse(cls, raw: Any, path: str = "packet") -> "ContextPacket":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        raw_sections = data.get("sections") or []
        if not isinstance(raw_sections, (list, tuple)):
            raise ContractError(f"{path}.sections", "expected a list", got=raw_sections)
        raw_omissions = data.get("omissions") or []
        if not isinstance(raw_omissions, (list, tuple)):
            raise ContractError(f"{path}.omissions", "expected a list", got=raw_omissions)
        return cls(
            packet_id=text(data, "packet_id", path, required=False,
                           default=new_id("ctxpkt"), max_len=128),
            request_id=text(data, "request_id", path, required=False, max_len=128),
            owner=text(data, "owner", path, required=False, max_len=256),
            project_id=text(data, "project_id", path, required=False, max_len=128),
            session_id=text(data, "session_id", path, required=False, max_len=128),
            turn_id=text(data, "turn_id", path, required=False, max_len=128),
            participant_id=text(data, "participant_id", path, required=False, max_len=128),
            branch_id=text(data, "branch_id", path, required=False, max_len=128),
            model=text(data, "model", path, required=False, max_len=256),
            intent=text(data, "intent", path, required=False, default="chat", max_len=64),
            phase=one_of(data, "phase", path, choices=TASK_PHASES,
                         required=False, default="act") or "act",
            consumer=one_of(data, "consumer", path, choices=CONSUMERS,
                            required=False, default="agent") or "agent",
            window=ContextBudget.parse(data.get("window") or {}, f"{path}.window"),
            sections=tuple(ContextSection.parse(s, f"{path}.sections[{n}]")
                           for n, s in enumerate(raw_sections)),
            omissions=tuple(ContextOmission.parse(o, f"{path}.omissions[{n}]")
                            for n, o in enumerate(raw_omissions)),
            warnings=text_list(data, "warnings", path, max_items=64, max_len=512),
            degraded=flag(data, "degraded", path, default=False),
            created_at=_ts(data, "created_at", path),
            schema_version=whole(data, "schema_version", path,
                                 default=SCHEMA_VERSION, minimum=1) or SCHEMA_VERSION,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "packet_id": self.packet_id, "request_id": self.request_id,
            "owner": self.owner, "project_id": self.project_id,
            "session_id": self.session_id, "turn_id": self.turn_id,
            "participant_id": self.participant_id, "branch_id": self.branch_id,
            "model": self.model, "intent": self.intent, "phase": self.phase,
            "consumer": self.consumer,
            "window": self.window.to_dict(),
            "sections": [s.to_dict() for s in self.sections],
            "omissions": [o.to_dict() for o in self.omissions],
            "warnings": list(self.warnings), "degraded": self.degraded,
            "created_at": self.created_at, "schema_version": self.schema_version,
            "manifest": self.manifest(),
            "tokens": self.tokens(),
        }

    # ── derived views ──────────────────────────────────────────────────────

    def tokens(self) -> int:
        return sum(s.tokens() for s in self.sections)

    def items(self) -> Tuple[ContextItem, ...]:
        return tuple(i for s in self.sections for i in s.items)

    def item_ids(self) -> Tuple[str, ...]:
        return tuple(i.item_id for i in self.items())

    def section(self, kind: str) -> Optional[ContextSection]:
        for s in self.sections:
            if s.kind == kind:
                return s
        return None

    def manifest(self) -> list:
        """§5.2.  One row per injected item, without the body.

        Derived rather than stored: a manifest that can drift from the packet
        it describes is worse than no manifest, because it will be believed."""
        rows = []
        for section in self.sections:
            for item in section.items:
                rows.append({
                    "context_item_id": item.item_id,
                    "section": section.kind,
                    "source_type": item.source_type,
                    "source_ref": item.source_ref,
                    "retrieval_lanes": list(item.lanes),
                    "scores": dict(item.scores),
                    "trust_class": item.trust_class,
                    "authority": item.authority,
                    "source_revision": item.source_revision,
                    "chars": item.chars,
                    "tokens": item.tokens,
                    "transformation": item.transformation,
                    "generated": item.is_generated(),
                    "reason": item.reason,
                })
        return rows

    def fits(self) -> bool:
        return not self.window.input_budget or self.tokens() <= self.window.input_budget

    def with_sections(self, sections: Sequence[ContextSection], *,
                      omissions: Sequence[ContextOmission] = (),
                      warnings: Sequence[str] = (),
                      degraded: Optional[bool] = None) -> "ContextPacket":
        return replace(
            self,
            sections=tuple(sections),
            omissions=tuple(self.omissions) + tuple(omissions),
            warnings=tuple(self.warnings) + tuple(w for w in warnings if w not in self.warnings),
            degraded=self.degraded if degraded is None else bool(degraded),
        )

    def identity(self) -> str:
        """A stable hash of what was said, not of when it was compiled.

        Branching Futures needs to prove two branches started from the same
        context; `created_at` and `packet_id` differ between them by
        construction, so neither may be in the hash."""
        return fingerprint([
            ("owner", self.owner),
            ("project_id", self.project_id),
            ("model", self.model),
            ("intent", self.intent),
            ("phase", self.phase),
            ("items", [f"{i.source_ref}@{i.source_revision}:{i.transformation}"
                       for i in self.items()]),
        ])


# ── what happened next ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class ContextReceipt:
    """§1.5.  Observed use first, self-reported use last.

    `used_item_ids` is what the runtime saw (a citation marker, a tool that
    opened a source ref, an item's text appearing in a changeset rationale).
    `declared_item_ids` is what the model claimed.  They are separate fields
    because merging them would let a model award itself credit for a memory
    it never read, and that credit becomes training signal for the curator."""

    packet_id: str = ""
    request_id: str = ""
    consumer: str = "agent"
    used_item_ids: Tuple[str, ...] = ()
    declared_item_ids: Tuple[str, ...] = ()
    cited_item_ids: Tuple[str, ...] = ()
    opened_source_refs: Tuple[str, ...] = ()
    tool_results_added: int = 0
    contradictions_detected: Tuple[str, ...] = ()
    feedback: str = "unknown"
    outcome_ref: str = ""
    verdict: str = ""
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION
    _KEYS = ("packet_id", "request_id", "consumer", "used_item_ids", "declared_item_ids",
             "cited_item_ids", "opened_source_refs", "tool_results_added",
             "contradictions_detected", "feedback", "outcome_ref", "verdict",
             "created_at", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "receipt") -> "ContextReceipt":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            packet_id=text(data, "packet_id", path, max_len=128),
            request_id=text(data, "request_id", path, required=False, max_len=128),
            consumer=one_of(data, "consumer", path, choices=CONSUMERS,
                            required=False, default="agent") or "agent",
            used_item_ids=text_list(data, "used_item_ids", path, max_items=512),
            declared_item_ids=text_list(data, "declared_item_ids", path, max_items=512),
            cited_item_ids=text_list(data, "cited_item_ids", path, max_items=512),
            opened_source_refs=text_list(data, "opened_source_refs", path,
                                         max_items=512, max_len=2048),
            tool_results_added=whole(data, "tool_results_added", path,
                                     default=0, minimum=0) or 0,
            contradictions_detected=text_list(data, "contradictions_detected", path,
                                              max_items=64, max_len=512),
            feedback=one_of(data, "feedback", path, choices=FEEDBACK_KINDS,
                            required=False, default="unknown") or "unknown",
            outcome_ref=text(data, "outcome_ref", path, required=False, max_len=512),
            verdict=text(data, "verdict", path, required=False, max_len=64),
            created_at=_ts(data, "created_at", path),
            schema_version=whole(data, "schema_version", path,
                                 default=SCHEMA_VERSION, minimum=1) or SCHEMA_VERSION,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "packet_id": self.packet_id, "request_id": self.request_id,
            "consumer": self.consumer,
            "used_item_ids": list(self.used_item_ids),
            "declared_item_ids": list(self.declared_item_ids),
            "cited_item_ids": list(self.cited_item_ids),
            "opened_source_refs": list(self.opened_source_refs),
            "tool_results_added": self.tool_results_added,
            "contradictions_detected": list(self.contradictions_detected),
            "feedback": self.feedback, "outcome_ref": self.outcome_ref,
            "verdict": self.verdict, "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    def observed_ids(self) -> Tuple[str, ...]:
        """The union the curator is allowed to learn from: only what the
        runtime saw.  `declared_item_ids` is diagnostics."""
        seen = list(self.used_item_ids)
        for value in self.cited_item_ids:
            if value not in seen:
                seen.append(value)
        return tuple(seen)


__all__ = [
    "SECTION_KINDS", "MANDATORY_SECTIONS", "SOURCE_TYPES", "TRANSFORMATIONS",
    "GENERATIVE_TRANSFORMATIONS", "OMISSION_REASONS", "RETRIEVAL_LANES",
    "TASK_PHASES", "CONSUMERS", "FEEDBACK_KINDS", "TRIM_POLICIES",
    "TRUST_CLASSES", "AUTHORITY_ORDER",
    "ContextActor", "ContextExecution", "ContextTask", "ContextPolicy",
    "ContextRequest", "ContextCandidate", "ContextItem", "ContextSection",
    "ContextOmission", "ContextBudget", "ContextPacket", "ContextReceipt",
    "new_id",
]
