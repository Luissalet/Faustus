"""
context_engine/compiler.py — the one place that decides what a model is told.

Everything else in this package is a part: `planner` decides what to look for,
`candidates` federates the stores, `ranking` orders what came back,
`transforms` shortens it honestly, `budgets` prices it and `manifest` explains
it.  This module is the sequence, and the sequence is §6.2's, method for
method, so that a reader with the plan open can follow the code without a
translation table:

    _classify_intent -> _establish_hard_context -> _build_retrieval_plan
    -> _gather_candidates -> _validate_candidates -> _dedupe_and_resolve
    -> _rerank_for_task -> _allocate_budget -> _transform_to_fit
    -> _render_packet -> _record_manifest

Five rules are load-bearing.  Each is a specific failure, and each has a test.

**Mandatory context does not compete (§6.3).**  Safety instructions, the
actor's role, the live goal, the current state and binding decisions are placed
first and out of the budget's top, before a single ranked candidate is
considered.  A packet that dropped a security instruction to make room for a
semantically similar memory is not a smaller packet, it is a different and
more dangerous one.  When mandatory content will not fit, the packet comes back
`degraded=True` with a warning that names what was lost — never quietly
trimmed.

**The packet never exceeds `window.input_budget`.**  Not "usually", not "within
a few percent".  A turn that overflows the window dies at the provider *after*
the tools have already run and the side effects have already happened, which is
the most expensive way this system can fail.  Every placement is bounded by
what is left, and `_enforce_budget` is a second, independent check that would
rather drop the lowest-priority section than hand back a packet that does not
fit.

**`compile()` never raises.**  A source that throws, a store that is locked, an
estimator that fails on a surrogate pair: all of them degrade.  The caller gets
a valid packet — in the worst case only the mandatory context — with a warning
saying so.  A chat that dies because one expert's `index.json` was half written
is a far worse outcome than a chat with one section missing, and the chat route
has no better idea than this module about what to do instead.

**It is deterministic.**  The same candidates, the same budget and the same
injected clock produce the same packet, byte for byte, apart from `packet_id`
and `created_at`.  Branching Futures proves two branches started from one
context by comparing exactly that; a compiler that reordered ties by dictionary
iteration would make every such comparison fail for a reason that has nothing
to do with context.

**Every discard produces exactly one omission.**  `packet.omissions` is output,
not logging: a candidate dropped at one stage never reaches the next, so no
absence is explained twice and none goes unexplained.

`_record_manifest` writes one row per packet to `context_packets` — the Context
Ledger of §24 — with the tokens, the section split, the omission counts and
nothing else.  No bodies: a ledger that stores content is a second copy of
everything the model was ever told, growing without bound, and it is the first
thing an audit would have to be kept away from.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import manifest, planner, ranking, store, transforms
from .budgets import (
    BudgetProfile,
    TokenEstimator,
    allocate,
    estimator_for,
    profile_for,
    resolve_budget,
    section_order,
    tool_schema_tokens,
)
from .cache import PREFIX_REQUEST, WorkingSet, scope_of, working_set
from .candidates import (
    ContextSource,
    RetrievalRequest,
    default_sources,
    fetch_ref,
    gather,
)
from .contracts import (
    MANDATORY_SECTIONS,
    OMISSION_REASONS,
    SECTION_KINDS,
    ContextBudget,
    ContextCandidate,
    ContextItem,
    ContextOmission,
    ContextPacket,
    ContextReceipt,
    ContextRequest,
    ContextSection,
    new_id,
)
from .planner import RetrievalPlan

logger = logging.getLogger(__name__)

#: Warnings are read by people and stored in the ledger; the contract caps them
#: at 64 entries of 512 characters, and a packet that spent its warning budget
#: on one traceback has stopped being readable anyway.
MAX_WARNINGS = 64
MAX_WARNING_CHARS = 512

#: How a packet says a mandatory piece did not fit.  A constant because tests,
#: the ledger and the UI all look for it, and three spellings of the same
#: sentence is three things to grep for.
MANDATORY_LOST = "mandatory context did not fit: "

#: How an expanded packet records the one it grew from.  `ContextPacket` has no
#: field for a parent and inventing one would be a contract change for a
#: diagnostic; a warning is where a packet records what a reader needs to know
#: about how it was made.
EXPANDED_FROM = "expanded from packet "

#: The ledger keeps a row per packet, and `maintenance.prune_packets` keeps the
#: rows bounded by age.  Everything here is a number or an id.
SCHEMA: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS context_packets (
        packet_id       TEXT PRIMARY KEY,
        request_id      TEXT NOT NULL DEFAULT '',
        owner           TEXT NOT NULL DEFAULT '',
        project_id      TEXT NOT NULL DEFAULT '',
        session_id      TEXT NOT NULL DEFAULT '',
        model           TEXT NOT NULL DEFAULT '',
        intent          TEXT NOT NULL DEFAULT '',
        phase           TEXT NOT NULL DEFAULT '',
        consumer        TEXT NOT NULL DEFAULT '',
        tokens          INTEGER NOT NULL DEFAULT 0,
        input_budget    INTEGER NOT NULL DEFAULT 0,
        items           INTEGER NOT NULL DEFAULT 0,
        section_tokens  TEXT NOT NULL DEFAULT '{}',
        omission_counts TEXT NOT NULL DEFAULT '{}',
        degraded        INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_context_packets_created "
    "ON context_packets(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_context_packets_scope "
    "ON context_packets(owner, project_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS context_receipts (
        packet_id     TEXT PRIMARY KEY,
        request_id    TEXT NOT NULL DEFAULT '',
        consumer      TEXT NOT NULL DEFAULT '',
        used_ids      TEXT NOT NULL DEFAULT '[]',
        cited_ids     TEXT NOT NULL DEFAULT '[]',
        declared_ids  TEXT NOT NULL DEFAULT '[]',
        opened_refs   TEXT NOT NULL DEFAULT '[]',
        feedback      TEXT NOT NULL DEFAULT 'unknown',
        verdict       TEXT NOT NULL DEFAULT '',
        outcome_ref   TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_context_receipts_created "
    "ON context_receipts(created_at)",
)

store.register_schema("packets", SCHEMA)


# ── small shared helpers ───────────────────────────────────────────────────

def _clip(value: Any, limit: int = MAX_WARNING_CHARS) -> str:
    text = str(value or "").strip()
    return text[:limit] if len(text) > limit else text


def _warnings(values: Sequence[str]) -> Tuple[str, ...]:
    """Deduplicated, clipped, capped, in first-seen order.

    Order matters: two compilations of the same request must produce the same
    warning list, and a set would not."""
    out: List[str] = []
    for value in values or ():
        text = _clip(value)
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_WARNINGS:
            break
    return tuple(out)


def _omission(candidate: ContextCandidate, reason: str, detail: str,
              score: float = 0.0, *, recoverable: bool = True) -> ContextOmission:
    safe = reason if reason in OMISSION_REASONS else "irrelevant"
    return ContextOmission(
        source_type=candidate.source_type,
        source_ref=candidate.source_ref,
        reason=safe,
        score=round(float(score or 0.0), 6),
        recoverable=bool(recoverable),
        detail=_clip(detail),
    )


def _ref_key(candidate: ContextCandidate) -> str:
    """Identity for "the packet already carries this".

    The `source_ref` alone, and not the `source_type:source_ref` pair that
    `ranking` uses for its diversity buckets: a ref is already namespaced by
    its own prefix (`mem:`, `doc:`, `file:`), and two lanes that found the same
    thing may label its type differently — a project instruction returned by
    the document lane arrives as a `document`.  Keying on the pair would let
    the same sentence into the packet twice under two labels, which is the
    exact repetition this is here to stop."""
    return candidate.source_ref


def _as_candidate(raw: Any) -> Optional[ContextCandidate]:
    """A candidate from whatever the caller had.

    Callers hand mandatory context in as `ContextCandidate`s most of the time
    and as plain dicts when they built it from a route payload; refusing the
    second shape would push a `parse()` call into every consumer and a
    `ContractError` onto the turn path."""
    if isinstance(raw, ContextCandidate):
        return raw
    if isinstance(raw, Mapping):
        try:
            return ContextCandidate.parse(dict(raw))
        except Exception:  # noqa: BLE001 - a bad mandatory item is not a turn
            logger.warning("context compiler dropped an unparseable mandatory item")
    return None


@dataclass
class _Allocation:
    """The budget decision, with the mandatory pass already paid for.

    The mandatory items live here rather than in `_transform_to_fit` because
    what they cost is not a share of anything: like the output reserve, it
    comes off the top, and the optional split is computed from what is left."""

    window: ContextBudget
    profile: BudgetProfile
    mandatory_items: Dict[str, List[ContextItem]] = field(default_factory=dict)
    mandatory_tokens: int = 0
    section_budgets: Dict[str, int] = field(default_factory=dict)
    omissions: List[ContextOmission] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    degraded: bool = False


# ── the compiler ───────────────────────────────────────────────────────────

class ContextCompiler:
    """§6.2, as one object with an injectable world.

    Every dependency that could make a test slow, flaky or clock-bound is a
    constructor argument: the sources, the estimator, the cache and the clock.
    Nothing else is reached for."""

    def __init__(self, *, sources: Optional[Sequence[ContextSource]] = None,
                 estimator: Optional[TokenEstimator] = None,
                 cache: Optional[WorkingSet] = None,
                 clock: Optional[Callable[[], datetime]] = None) -> None:
        self._sources = list(sources) if sources is not None else None
        self._estimator = estimator
        self._cache = cache
        self._clock = clock

    # ── injected world ─────────────────────────────────────────────────────

    def sources(self) -> List[ContextSource]:
        """The pool, resolved late.  `default_sources()` memoises its adapters
        in a process-wide registry, so a source registered after this compiler
        was built is still seen — which is what a route installing a history
        provider at startup depends on."""
        if self._sources is not None:
            return list(self._sources)
        try:
            return list(default_sources())
        except Exception:  # noqa: BLE001 - no sources is a degraded packet
            logger.warning("context sources unavailable", exc_info=True)
            return []

    def cache(self) -> Optional[WorkingSet]:
        if self._cache is not None:
            return self._cache
        try:
            return working_set()
        except Exception:  # noqa: BLE001 - the cache is never load-bearing
            return None

    def now(self) -> datetime:
        if self._clock is None:
            return datetime.now(timezone.utc)
        try:
            moment = self._clock()
        except Exception:  # noqa: BLE001 - an injected clock may not break a turn
            return datetime.now(timezone.utc)
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def _created_at(self) -> str:
        return self.now().replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _estimator_for(self, model: str) -> TokenEstimator:
        if self._estimator is not None:
            return self._estimator
        try:
            return estimator_for(model or "")
        except Exception:  # noqa: BLE001
            return estimator_for("")

    # ── 1. classify ────────────────────────────────────────────────────────

    def _classify_intent(self, request: ContextRequest) -> str:
        return planner.classify_intent(request)

    # ── 2. hard context ────────────────────────────────────────────────────

    def _hard_gate(self, candidate: ContextCandidate,
                   request: ContextRequest) -> Tuple[bool, str, str]:
        """Isolation, and only isolation, for caller-supplied mandatory items.

        The policy flags (`allow_personal_memory`, `allow_project_sources`)
        gate *retrieval*: consulting a store leaves fingerprints on it, which
        is what incognito is meant to prevent.  A mandatory candidate was not
        retrieved — the runtime handed it in — so applying those flags here
        would delete the system prompt of an incognito turn and call it
        privacy.  Ownership, project and quarantine still apply, because those
        are about whose information it is."""
        if not (candidate.source_ref or "").strip():
            return False, "unavailable", "mandatory candidate has no source_ref"
        if not (candidate.body or "").strip() and not (candidate.title or "").strip():
            return False, "unavailable", "mandatory candidate has neither title nor body"
        owner = (request.execution.owner or "").strip()
        if owner and (candidate.owner or "").strip() and candidate.owner != owner:
            return False, "unauthorised", f"owned by {candidate.owner!r}"
        project = (request.execution.project_id or "").strip()
        if project and (candidate.project_id or "").strip() and candidate.project_id != project:
            return False, "unauthorised", f"project {candidate.project_id!r}"
        try:
            if bool((candidate.meta or {}).get("quarantined")):
                return False, "quarantined", "the source is quarantined"
        except Exception:  # noqa: BLE001 - a hostile meta is not a Mapping
            pass
        return True, "", ""

    def _establish_hard_context(self, request: ContextRequest,
                                mandatory: Sequence[Any]
                                ) -> Tuple[List[ContextCandidate], List[ContextOmission]]:
        """The context that is present or the packet is degraded.

        Order is the caller's, because a system prompt assembled in a
        particular order was assembled that way on purpose."""
        kept: List[ContextCandidate] = []
        omissions: List[ContextOmission] = []
        seen: set = set()
        for raw in mandatory or ():
            candidate = _as_candidate(raw)
            if candidate is None:
                continue
            ok, reason, detail = self._hard_gate(candidate, request)
            if not ok:
                omissions.append(_omission(candidate, reason, detail,
                                           recoverable=False))
                continue
            key = _ref_key(candidate)
            if key in seen:
                omissions.append(_omission(candidate, "duplicate",
                                           "the same mandatory ref was passed twice"))
                continue
            seen.add(key)
            kept.append(candidate)
        return kept, omissions

    # ── 3. plan ────────────────────────────────────────────────────────────

    def _build_retrieval_plan(self, request: ContextRequest,
                              sources: Sequence[ContextSource]) -> RetrievalPlan:
        available = []
        for source in sources:
            name = str(getattr(source, "source_id", "") or "").strip()
            if name and name not in available:
                available.append(name)
        return planner.plan(request, available=tuple(available), now=self.now())

    # ── 4. gather ──────────────────────────────────────────────────────────

    async def _gather_candidates(self, request: ContextRequest,
                                 plan_: RetrievalPlan,
                                 sources: Sequence[ContextSource]
                                 ) -> Tuple[List[ContextCandidate], List[str]]:
        """Ask the planned sources at once.  Never raises: `gather()` returns
        one row per source and every row says what happened."""
        wanted = set(plan_.source_ids)
        pool = [s for s in sources
                if str(getattr(s, "source_id", "") or "") in wanted]
        if not pool:
            return [], []
        retrieval = RetrievalRequest(
            request=request,
            query=request.task.query or "",
            sections=tuple(plan_.sections),
            limit=plan_.per_source_limit,
            lanes=tuple(plan_.lanes),
            explicit_refs=tuple(request.explicit_refs),
        )
        try:
            results = await gather(pool, retrieval, timeout_s=plan_.timeout_s)
        except Exception:  # noqa: BLE001 - gather promises not to, belt and braces
            logger.warning("context gather failed", exc_info=True)
            return [], ["retrieval failed entirely; the packet carries only "
                        "mandatory context"]

        out: List[ContextCandidate] = []
        warnings: List[str] = []
        for result in results:
            out.extend(result.candidates)
            if result.degraded:
                warnings.append(f"source {result.source_id} degraded: "
                                f"{result.error or 'unknown'}")
        return out, warnings

    # ── 5. validate ────────────────────────────────────────────────────────

    def _validate_candidates(self, request: ContextRequest,
                             candidates: Sequence[ContextCandidate], *,
                             now: Optional[datetime] = None
                             ) -> Tuple[List[ContextCandidate], List[ContextOmission]]:
        """The hard gates, applied before anything is scored.

        `ranking.select` validates too, and re-validating there is free: a
        candidate that passed here passes there.  Doing it once here as well is
        what lets a mandatory-section candidate skip ranking without skipping
        authorisation."""
        kept: List[ContextCandidate] = []
        omissions: List[ContextOmission] = []
        for candidate in candidates or ():
            if not isinstance(candidate, ContextCandidate):
                continue
            verdict = ranking.validate(candidate, request, now=now)
            if verdict.ok:
                kept.append(candidate)
            else:
                omissions.append(_omission(candidate, verdict.reason, verdict.detail,
                                           recoverable=verdict.reason in
                                           ranking.RECOVERABLE_OMISSION_REASONS))
        return kept, omissions

    # ── 6. dedupe against what is already placed ───────────────────────────

    def _dedupe_and_resolve(self, hard: Sequence[ContextCandidate],
                            optional: Sequence[ContextCandidate]
                            ) -> Tuple[List[ContextCandidate], List[ContextOmission]]:
        """Drop optional candidates the mandatory pass already carries.

        Ranking's own dedupe compares the optional pool with itself; it never
        sees the mandatory items, because those never entered it.  Without this
        pass the active goal arrives twice — once as policy and once as a
        well-scored memory — and the second copy costs a slot a different
        source wanted."""
        placed = {_ref_key(c) for c in hard}
        kept: List[ContextCandidate] = []
        omissions: List[ContextOmission] = []
        for candidate in optional or ():
            if _ref_key(candidate) in placed:
                omissions.append(_omission(
                    candidate, "duplicate",
                    "already placed as mandatory context, which does not compete"))
                continue
            kept.append(candidate)
        return kept, omissions

    # ── 7. rank ────────────────────────────────────────────────────────────

    def _rerank_for_task(self, candidates: Sequence[ContextCandidate],
                         request: ContextRequest, *,
                         now: Optional[datetime] = None
                         ) -> Tuple[List[ranking.Scored], List[ContextOmission]]:
        try:
            return ranking.select(candidates, request, now=now)
        except Exception:  # noqa: BLE001 - ranking promises not to raise
            logger.warning("context ranking failed", exc_info=True)
            return [], []

    # ── 8. budget ──────────────────────────────────────────────────────────

    def _window(self, request: ContextRequest, *, tool_schemas: Sequence[Any],
                max_output_tokens: int, context_length: int,
                window_known: bool) -> ContextBudget:
        """Reserves off the top, then whatever is left is the input budget."""
        model = request.actor.model or ""
        try:
            reserved_tools = tool_schema_tokens(tool_schemas, model=model)
        except Exception:  # noqa: BLE001 - an unpriceable tool list is not free
            logger.debug("tool schemas could not be priced; reserving nothing")
            reserved_tools = 0
        try:
            return resolve_budget(
                model=model,
                context_length=int(context_length or 0),
                window_known=bool(window_known),
                max_output_tokens=int(max_output_tokens or 0),
                tool_schema_tokens=reserved_tools,
                configured_input_budget=int(request.policy.token_budget or 0),
            )
        except Exception:  # noqa: BLE001 - a budget that cannot be computed is
            logger.warning("context budget could not be resolved", exc_info=True)
            return resolve_budget(model=model)

    @staticmethod
    def _mandatory_order(hard: Sequence[ContextCandidate]) -> List[ContextCandidate]:
        """Safety first, literally: the mandatory sections in the order §6.3
        lists them, and the caller's own order inside each one.  When the room
        runs out it has to run out at the bottom of that list, not wherever a
        dictionary happened to iterate."""
        rank = {kind: index for index, kind in enumerate(MANDATORY_SECTIONS)}
        floor = len(MANDATORY_SECTIONS)
        rows = list(enumerate(hard))
        rows.sort(key=lambda pair: (rank.get(pair[1].section, floor), pair[0]))
        return [candidate for _, candidate in rows]

    def _place_mandatory(self, request: ContextRequest, window: ContextBudget,
                         hard: Sequence[ContextCandidate], profile: BudgetProfile,
                         estimator: TokenEstimator) -> _Allocation:
        """Rule 1.  Mandatory items are fitted against the *whole* budget,
        before any share is computed, so no optional section can be allocated
        room that a safety instruction still needed.

        Separate from `_allocate_budget` because the degraded path needs it on
        its own: when retrieval blows up, the packet that comes back is this
        pass and nothing else."""
        out = _Allocation(window=window, profile=profile)
        budget = max(0, int(window.input_budget or 0))
        query = request.task.query or ""

        spent = 0
        for candidate in self._mandatory_order(hard):
            item, omission = transforms.fit(
                candidate, budget_tokens=max(0, budget - spent),
                estimator=estimator, query=query,
                reason="mandatory context; not ranked against anything",
            )
            if item is not None:
                out.mandatory_items.setdefault(candidate.section, []).append(item)
                spent += item.tokens
            if omission is not None:
                out.omissions.append(omission)
            lost = item is None or (item.transformation == "reference"
                                    and bool((candidate.body or "").strip()))
            if lost:
                # Never trimmed silently: the packet says which one, by ref.
                out.degraded = True
                out.warnings.append(
                    f"{MANDATORY_LOST}{candidate.source_type}:{candidate.source_ref}"
                    f" ({'no room at all' if item is None else 'reference only'})")
        out.mandatory_tokens = spent
        return out

    def _allocate_budget(self, request: ContextRequest, plan_: RetrievalPlan,
                         window: ContextBudget, hard: Sequence[ContextCandidate],
                         scored: Sequence[ranking.Scored],
                         estimator: TokenEstimator) -> _Allocation:
        """The mandatory pass, then the profile's split of what it left.

        Only sections that actually have candidates get a share: handing 15% to
        `multimodal_recipes` on a Python bugfix wastes it, and `allocate()`
        redistributes what nobody claimed."""
        profile = profile_for(profile_id=plan_.profile_id, intent=plan_.intent)
        out = self._place_mandatory(request, window, hard, profile, estimator)
        budget = max(0, int(window.input_budget or 0))
        spent = out.mandatory_tokens

        present: List[str] = []
        for entry in scored:
            kind = entry.candidate.section
            if kind in MANDATORY_SECTIONS or kind not in SECTION_KINDS:
                continue
            if kind not in present:
                present.append(kind)
        remaining = max(0, budget - spent)
        if present and remaining > 0:
            try:
                optional_window = replace(window, input_budget=remaining)
                out.section_budgets = allocate(optional_window, profile,
                                               present=present)
            except Exception:  # noqa: BLE001 - an unsplittable budget is zero
                logger.warning("context budget split failed", exc_info=True)
                out.section_budgets = {kind: remaining // max(len(present), 1)
                                       for kind in present}
        return out

    # ── 9. transform ───────────────────────────────────────────────────────

    def _transform_to_fit(self, request: ContextRequest, allocation: _Allocation,
                          scored: Sequence[ranking.Scored],
                          estimator: TokenEstimator
                          ) -> Tuple[List[ContextSection], List[ContextOmission]]:
        """Place the ranked candidates in their sections, highest priority
        first, and hand what a section does not spend to the next one.

        Rule 7: an empty `multimodal_recipes` must not sit on 15% of the window
        while `retrieved_documents` truncates.  The carry is why the sections
        are walked in priority order rather than in render order — the order
        they are *printed* in is fixed by `SECTION_KINDS` and decided at the
        end."""
        budget = max(0, int(allocation.window.input_budget or 0))
        spent = allocation.mandatory_tokens
        query = request.task.query or ""
        omissions: List[ContextOmission] = []

        items: Dict[str, List[ContextItem]] = {
            kind: list(values) for kind, values in allocation.mandatory_items.items()
        }
        grouped: Dict[str, List[ranking.Scored]] = {}
        for entry in scored:
            grouped.setdefault(entry.candidate.section, []).append(entry)

        order = sorted(
            (kind for kind in grouped if kind not in MANDATORY_SECTIONS),
            key=lambda kind: (-allocation.profile.priority(kind), kind),
        )
        carry = 0
        for kind in order:
            allowance = int(allocation.section_budgets.get(kind, 0)) + carry
            used = 0
            for entry in grouped[kind]:
                room = max(0, min(allowance - used, budget - spent))
                item, omission = transforms.fit(
                    entry.candidate, budget_tokens=room, estimator=estimator,
                    query=query, allow_generative=True,
                )
                if item is not None:
                    items.setdefault(kind, []).append(item)
                    used += item.tokens
                    spent += item.tokens
                if omission is not None:
                    omissions.append(omission)
            carry = max(0, allowance - used)

        sections: List[ContextSection] = []
        for kind in section_order(items):
            rows = items.get(kind) or []
            if not rows:
                continue
            mandatory = kind in MANDATORY_SECTIONS
            sections.append(ContextSection(
                kind=kind,
                priority=allocation.profile.priority(kind),
                items=tuple(rows),
                budget_tokens=(sum(i.tokens for i in rows) if mandatory
                               else int(allocation.section_budgets.get(kind, 0))),
                trim_policy="never" if mandatory else "drop_lowest",
            ))
        return sections, omissions

    # ── 10. render ─────────────────────────────────────────────────────────

    def _render_packet(self, request: ContextRequest, *, intent: str,
                       window: ContextBudget, sections: Sequence[ContextSection],
                       omissions: Sequence[ContextOmission],
                       warnings: Sequence[str], degraded: bool) -> ContextPacket:
        execution = request.execution
        packet = ContextPacket(
            packet_id=new_id("ctxpkt"),
            request_id=request.request_id,
            owner=execution.owner,
            project_id=execution.project_id,
            session_id=execution.session_id,
            turn_id=execution.turn_id,
            participant_id=request.actor.participant_id,
            branch_id=execution.branch_id,
            model=request.actor.model,
            intent=intent,
            phase=request.task.phase or "act",
            consumer=request.consumer or "agent",
            window=window,
            sections=tuple(sections),
            omissions=tuple(omissions),
            warnings=_warnings(warnings),
            degraded=bool(degraded),
            created_at=self._created_at(),
        )
        return self._enforce_budget(packet)

    @staticmethod
    def _enforce_budget(packet: ContextPacket) -> ContextPacket:
        """Rule 2, checked independently of the arithmetic that produced it.

        Nothing should reach here over budget.  If something does, the packet
        loses its lowest-priority optional items — never a mandatory one — and
        says so, because a packet that quietly does not fit is a turn that dies
        at the provider with the tools already run."""
        limit = int(packet.window.input_budget or 0)
        if not limit or packet.tokens() <= limit:
            return packet

        sections = {s.kind: list(s.items) for s in packet.sections}
        meta = {s.kind: s for s in packet.sections}
        order = sorted((s for s in packet.sections if not s.is_mandatory()),
                       key=lambda s: (s.priority, s.kind))
        dropped: List[ContextOmission] = []
        total = packet.tokens()
        for section in order:
            rows = sections.get(section.kind) or []
            while rows and total > limit:
                item = rows.pop()
                total -= item.tokens
                dropped.append(ContextOmission(
                    source_type=item.source_type, source_ref=item.source_ref,
                    reason="budget", score=0.0, recoverable=True,
                    detail=_clip(f"dropped by the final budget check: the packet "
                                 f"was {packet.tokens()} tokens against a "
                                 f"{limit}-token budget")))
            if total <= limit:
                break

        rebuilt: List[ContextSection] = []
        for kind in section_order(sections):
            rows = sections.get(kind) or []
            if rows:
                rebuilt.append(replace(meta[kind], items=tuple(rows)))
        return packet.with_sections(
            rebuilt, omissions=dropped,
            warnings=(f"the packet did not fit and {len(dropped)} item(s) were "
                      f"dropped by the final budget check",),
            degraded=True)

    # ── 11. the ledger ─────────────────────────────────────────────────────

    def _record_manifest(self, packet: ContextPacket) -> None:
        record_packet(packet)

    def _remember_request(self, packet: ContextPacket,
                          request: ContextRequest) -> None:
        """Keep the request beside its packet so `expand()` can reopen a ref
        with the same authorisation the packet was built under.

        In the working set, not in the ledger: it is rebuildable (badly, from
        the packet's own fields) and it holds a workspace path, which is
        exactly the kind of thing an audit table should not accumulate."""
        cache = self.cache()
        if cache is None:
            return
        try:
            cache.put(scope_of(request), PREFIX_REQUEST + packet.packet_id,
                      request, cost_bytes=1024)
        except Exception:  # noqa: BLE001 - the cache is never load-bearing
            logger.debug("context compiler could not cache a request")

    # ── the public surface ─────────────────────────────────────────────────

    async def compile(self, request: ContextRequest, *,
                      mandatory: Sequence[ContextCandidate] = (),
                      tool_schemas: Sequence[Any] = (),
                      max_output_tokens: int = 0,
                      context_length: int = 0,
                      window_known: bool = False) -> ContextPacket:
        """One `ContextPacket` for one model call.  Never raises; see rule 3."""
        return await self._compile(
            request, mandatory=mandatory, tool_schemas=tool_schemas,
            max_output_tokens=max_output_tokens, context_length=context_length,
            window_known=window_known, record=True)

    async def _compile(self, request: ContextRequest, *,
                       mandatory: Sequence[Any] = (),
                       tool_schemas: Sequence[Any] = (),
                       max_output_tokens: int = 0,
                       context_length: int = 0,
                       window_known: bool = False,
                       record: bool = True) -> ContextPacket:
        moment = self.now()
        estimator = self._estimator_for(request.actor.model or "")
        window = self._window(request, tool_schemas=tool_schemas,
                              max_output_tokens=max_output_tokens,
                              context_length=context_length,
                              window_known=window_known)
        intent = self._classify_intent(request)
        hard, omissions = self._establish_hard_context(request, mandatory)
        warnings: List[str] = []
        sections: List[ContextSection] = []
        degraded = False

        try:
            sources = self.sources()
            plan_ = self._build_retrieval_plan(request, sources)
            intent = plan_.intent
            raw, gather_warnings = await self._gather_candidates(request, plan_,
                                                                 sources)
            warnings.extend(gather_warnings)

            pool, invalid = self._validate_candidates(request, raw, now=moment)
            omissions.extend(invalid)

            # A retrieved candidate that lands in a mandatory section joins the
            # hard pool: §6.3 says those sections do not compete.  It still had
            # to pass `_validate_candidates` first — not competing is not the
            # same as not being checked.
            optional = [c for c in pool if c.section not in MANDATORY_SECTIONS]
            promoted = sorted((c for c in pool if c.section in MANDATORY_SECTIONS),
                              key=lambda c: (c.section, c.source_ref, c.candidate_id))
            placed = {_ref_key(c) for c in hard}
            for candidate in promoted:
                key = _ref_key(candidate)
                if key in placed:
                    continue
                placed.add(key)
                hard.append(candidate)

            optional, duplicates = self._dedupe_and_resolve(hard, optional)
            omissions.extend(duplicates)

            scored, ranked_out = self._rerank_for_task(optional, request, now=moment)
            omissions.extend(ranked_out)

            allocation = self._allocate_budget(request, plan_, window, hard,
                                               scored, estimator)
            omissions.extend(allocation.omissions)
            warnings.extend(allocation.warnings)
            degraded = allocation.degraded

            sections, fitted_out = self._transform_to_fit(request, allocation,
                                                          scored, estimator)
            omissions.extend(fitted_out)
        except Exception:  # noqa: BLE001 - rule 3: compile() never raises
            logger.exception("context compilation failed; degrading to the "
                             "mandatory context")
            warnings.append("context compilation failed; this packet carries "
                            "only the mandatory context")
            degraded = True
            try:
                allocation = self._place_mandatory(
                    request, window, hard, profile_for(intent=intent), estimator)
                omissions.extend(allocation.omissions)
                warnings.extend(allocation.warnings)
                degraded = True
                sections, fitted_out = self._transform_to_fit(request, allocation,
                                                              (), estimator)
                omissions.extend(fitted_out)
            except Exception:  # noqa: BLE001 - an empty packet is still a packet
                logger.exception("mandatory context could not be placed either")
                sections = []

        packet = self._render_packet(request, intent=intent, window=window,
                                     sections=sections, omissions=omissions,
                                     warnings=warnings, degraded=degraded)
        if record:
            self._record_manifest(packet)
        self._remember_request(packet, request)
        return packet

    async def expand(self, packet: ContextPacket, *, refs: Sequence[str],
                     extra_tokens: int = 0) -> ContextPacket:
        """Add named references to a packet that has already been compiled.

        The Greedy Completion path (§1.8: "pedir una ampliación incremental sin
        recompilar lo ya visto").  Nothing already in the packet is fetched,
        ranked or re-measured; what fits is whatever the budget still has, plus
        `extra_tokens` if the caller is granting more room.  The result is a
        new packet — a packet is immutable once rendered — and it names the one
        it grew from in a warning, because `ContextPacket` has no parent field
        and adding one for a diagnostic would be a contract change."""
        try:
            request = self._recall_request(packet)
            estimator = self._estimator_for(packet.model or "")
            window = self._expanded_window(packet.window, extra_tokens)
            profile = profile_for(intent=packet.intent)

            sections = {s.kind: list(s.items) for s in packet.sections}
            meta = {s.kind: s for s in packet.sections}
            present = {item.source_ref for item in packet.items()}
            omissions: List[ContextOmission] = []
            warnings: List[str] = [f"{EXPANDED_FROM}{packet.packet_id}"]
            spent = packet.tokens()
            limit = max(0, int(window.input_budget or 0))

            pool = self.sources()
            retrieval = RetrievalRequest(
                request=request, query=request.task.query or "", limit=1,
                explicit_refs=tuple(str(r or "") for r in refs or ()),
            )
            for raw in refs or ():
                ref = str(raw or "").strip()
                if not ref or ref in present:
                    continue
                candidate = await self._fetch_one(ref, retrieval, pool, request)
                if candidate is None:
                    omissions.append(ContextOmission(
                        source_type=_ref_source_type(ref), source_ref=ref,
                        reason="unavailable", score=0.0, recoverable=True,
                        detail=_clip("no source claimed this reference, or it is "
                                     "no longer there")))
                    continue
                item, omission = transforms.fit(
                    candidate, budget_tokens=max(0, limit - spent),
                    estimator=estimator, query=request.task.query or "",
                    allow_generative=True, reason="expanded on request")
                if item is not None:
                    sections.setdefault(candidate.section, []).append(item)
                    spent += item.tokens
                    present.add(ref)
                if omission is not None:
                    omissions.append(omission)

            rebuilt: List[ContextSection] = []
            for kind in section_order(sections):
                rows = sections.get(kind) or []
                if not rows:
                    continue
                if kind in meta:
                    rebuilt.append(replace(meta[kind], items=tuple(rows)))
                else:
                    rebuilt.append(ContextSection(
                        kind=kind, priority=profile.priority(kind),
                        items=tuple(rows), budget_tokens=0,
                        trim_policy="never" if kind in MANDATORY_SECTIONS
                        else "drop_lowest"))

            grown = replace(
                packet, packet_id=new_id("ctxpkt"), window=window,
                sections=tuple(rebuilt),
                omissions=tuple(packet.omissions) + tuple(omissions),
                warnings=_warnings(list(packet.warnings) + warnings),
                created_at=self._created_at())
            grown = self._enforce_budget(grown)
            self._record_manifest(grown)
            return grown
        except Exception:  # noqa: BLE001 - expansion may never break a turn
            logger.exception("context expansion failed; the packet is unchanged")
            return packet.with_sections(
                packet.sections,
                warnings=("expansion failed; this packet is the one that was "
                          "already compiled",))

    async def shadow(self, request: ContextRequest, *,
                     messages: Sequence[Mapping[str, Any]],
                     **kw: Any) -> Dict[str, Any]:
        """Phase 1: compile a packet, do not deliver it, and say what the
        difference would have been.

        This is the instrument that measures the baseline before the hot path
        changes.  It writes no memory, marks nothing as used, records no ledger
        row and never touches the prompt — the packet exists only inside the
        report.  Both sides are counted with the same estimator, because a
        difference produced by two different rulers is not a difference."""
        allowed = ("mandatory", "tool_schemas", "max_output_tokens",
                   "context_length", "window_known")
        options = {name: kw[name] for name in allowed if name in kw}
        try:
            packet = await self._compile(request, record=False, **options)
            estimator = self._estimator_for(request.actor.model or "")
            report = dict(manifest.compare(packet, messages, estimator=estimator))
            report["packet"] = packet.to_dict()
            report["summary"] = manifest.summarize(packet)
            report["delivered"] = False
            return report
        except Exception:  # noqa: BLE001 - a measurement may never break a turn
            logger.exception("shadow compilation failed")
            return {"error": "shadow compilation failed", "delivered": False,
                    "packet": None, "summary": None}

    # ── expand helpers ─────────────────────────────────────────────────────

    async def _fetch_one(self, ref: str, retrieval: RetrievalRequest,
                         pool: Sequence[ContextSource],
                         request: ContextRequest) -> Optional[ContextCandidate]:
        try:
            candidate = await fetch_ref(ref, retrieval, sources=list(pool),
                                        timeout_s=planner.timeout_for(request))
        except Exception:  # noqa: BLE001 - fetch_ref promises not to raise
            logger.warning("context expand could not fetch %s", ref, exc_info=True)
            return None
        if candidate is None:
            return None
        ok = ranking.validate(candidate, request, now=self.now())
        if not ok.ok:
            logger.debug("context expand refused %s: %s", ref, ok.detail)
            return None
        return candidate

    @staticmethod
    def _expanded_window(window: ContextBudget, extra_tokens: int) -> ContextBudget:
        """A bigger input budget, still inside the model's window.

        `extra_tokens` grants room; it does not suspend rule 2.  What the
        caller asks for is clamped to what the window has left after the
        output and tool reserves, so the expanded packet's own budget is still
        a number the provider will honour."""
        try:
            extra = max(0, int(extra_tokens or 0))
        except (TypeError, ValueError):
            extra = 0
        if not extra:
            return window
        wanted = window.input_budget + extra
        if window.max_tokens:
            ceiling = max(0, window.max_tokens - window.reserved_output
                          - window.reserved_tools)
            wanted = min(wanted, ceiling)
        return replace(window, input_budget=max(window.input_budget, wanted))

    def _recall_request(self, packet: ContextPacket) -> ContextRequest:
        """The request this packet was compiled from, or a reconstruction.

        The cached copy is verified against the packet's own owner and request
        id before it is used: a scope scan that trusted whatever it found would
        be the leak this package spends `cache.py` preventing."""
        cache = self.cache()
        if cache is not None:
            key = PREFIX_REQUEST + packet.packet_id
            try:
                for scope in cache.scopes():
                    value = cache.get(scope, key)
                    if (isinstance(value, ContextRequest)
                            and value.execution.owner == packet.owner
                            and value.request_id == packet.request_id):
                        return value
            except Exception:  # noqa: BLE001 - a cache miss is not a failure
                logger.debug("context compiler could not recall a request")
        return _request_from_packet(packet)


# ── reference plumbing ─────────────────────────────────────────────────────

#: `source_ref` prefix -> the `SOURCE_TYPES` value it belongs to.  Used only to
#: label an omission for a reference nothing could resolve: an omission whose
#: `source_type` is a guess is still better than one that says "memory" about a
#: document, because the reason people read omissions is to find the store that
#: should have answered.
REF_SOURCE_TYPES: Dict[str, str] = {
    "mem:": "memory",
    "pmem:": "memory",
    "objective:": "objective",
    "project:": "project_memory",
    "doc:": "document",
    "expert:": "expert",
    "session:": "message",
    "prov:": "delta",
    "file:": "file",
    "symbol:": "symbol",
    "capsule:": "capsule",
    "finding:": "finding",
    "block:": "block",
    "experience:": "experience",
    "recipe:": "recipe",
    "artifact:": "artifact",
}


def _ref_source_type(ref: str) -> str:
    prefix = str(ref or "").split(":", 1)[0] + ":"
    return REF_SOURCE_TYPES.get(prefix, "memory")


def _request_from_packet(packet: ContextPacket) -> ContextRequest:
    """A request rebuilt from a packet, for when the cache no longer has the
    original.

    Deliberately lossy and deliberately not fixed: `workspace` is not on a
    packet, so a file reference will not resolve through this path.  The
    alternative — putting the workspace path in the ledger so it could be read
    back — would put a filesystem layout in an audit table forever, to save a
    cache miss."""
    from .contracts import ContextActor, ContextExecution, ContextPolicy, ContextTask

    return ContextRequest(
        request_id=packet.request_id,
        actor=ContextActor(model=packet.model,
                           participant_id=packet.participant_id),
        execution=ContextExecution(owner=packet.owner,
                                   session_id=packet.session_id,
                                   project_id=packet.project_id,
                                   branch_id=packet.branch_id,
                                   turn_id=packet.turn_id),
        task=ContextTask(intent=packet.intent, phase=packet.phase),
        policy=ContextPolicy(),
        consumer=packet.consumer,
        created_at=packet.created_at,
    )


# ── the ledger (§24) ───────────────────────────────────────────────────────

def record_packet(packet: ContextPacket) -> None:
    """One row per compiled packet: numbers, ids and nothing else.

    Never raises.  A ledger that can take the turn down with it is a worse
    audit trail than no ledger, because the first incident it causes is the one
    that gets it switched off."""
    try:
        section_tokens = {s.kind: s.tokens() for s in packet.sections}
        counts: Dict[str, int] = {}
        for omission in packet.omissions:
            counts[omission.reason] = counts.get(omission.reason, 0) + 1
        with store.db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO context_packets "
                "(packet_id, request_id, owner, project_id, session_id, model, "
                " intent, phase, consumer, tokens, input_budget, items, "
                " section_tokens, omission_counts, degraded, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (packet.packet_id, packet.request_id, packet.owner,
                 packet.project_id, packet.session_id, packet.model,
                 packet.intent, packet.phase, packet.consumer, packet.tokens(),
                 packet.window.input_budget, len(packet.items()),
                 store.dumps(section_tokens), store.dumps(counts),
                 1 if packet.degraded else 0,
                 packet.created_at or store.now_iso()))
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("context ledger could not record %s: %s",
                       packet.packet_id, exc)


def record_receipt(receipt: ContextReceipt) -> None:
    """What happened to a packet after it was delivered (§1.5).

    Ids only, and observed use kept apart from declared use: merging them would
    let a model award itself credit for a memory it never read, and that credit
    becomes training signal for the curator."""
    if not isinstance(receipt, ContextReceipt) or not receipt.packet_id:
        logger.debug("context ledger ignored a receipt with no packet_id")
        return
    try:
        with store.db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO context_receipts "
                "(packet_id, request_id, consumer, used_ids, cited_ids, "
                " declared_ids, opened_refs, feedback, verdict, outcome_ref, "
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (receipt.packet_id, receipt.request_id, receipt.consumer,
                 store.dumps(list(receipt.used_item_ids)),
                 store.dumps(list(receipt.cited_item_ids)),
                 store.dumps(list(receipt.declared_item_ids)),
                 store.dumps(list(receipt.opened_source_refs)),
                 receipt.feedback, receipt.verdict, receipt.outcome_ref,
                 receipt.created_at or store.now_iso()))
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("context ledger could not record a receipt for %s: %s",
                       receipt.packet_id, exc)


def _row(raw: Mapping[str, Any]) -> Dict[str, Any]:
    row = dict(raw)
    row["section_tokens"] = store.loads_dict(row.get("section_tokens"))
    row["omission_counts"] = store.loads_dict(row.get("omission_counts"))
    row["degraded"] = bool(row.get("degraded"))
    return row


def get_packet_row(packet_id: str) -> Optional[Dict[str, Any]]:
    """The ledger row for one packet, or None.  The row has no content: it
    answers "how big, how degraded, what was left out", not "what did it say"."""
    wanted = str(packet_id or "").strip()
    if not wanted:
        return None
    try:
        with store.db() as conn:
            row = conn.execute(
                "SELECT * FROM context_packets WHERE packet_id = ?",
                (wanted,)).fetchone()
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("context ledger read failed: %s", exc)
        return None
    return _row(dict(row)) if row else None


def _scope_sql(owner: str, project_id: str) -> Tuple[str, List[Any]]:
    """Filter only on what the caller actually named.

    `store.scope_clause` is the right tool for a store where an empty owner
    means "shared with the whole install"; the ledger has no such rows, so an
    empty owner here means "do not filter" and using that clause would answer
    every diagnostic with zero rows."""
    where: List[str] = []
    params: List[Any] = []
    if owner:
        where.append("owner = ?")
        params.append(owner)
    if project_id:
        where.append("project_id = ?")
        params.append(project_id)
    return (" AND ".join(where) or "1 = 1"), params


def recent_packets(*, owner: str = "", project_id: str = "",
                   limit: int = 50) -> List[Dict[str, Any]]:
    """The newest packets for a scope, newest first."""
    try:
        count = max(1, min(int(limit or 50), 1000))
    except (TypeError, ValueError):
        count = 50
    where, params = _scope_sql(str(owner or ""), str(project_id or ""))
    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(
                "SELECT * FROM context_packets WHERE " + where
                + " ORDER BY created_at DESC, packet_id DESC LIMIT ?",
                params + [count]))
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("context ledger listing failed: %s", exc)
        return []
    return [_row(row) for row in rows]


def diagnostics(*, owner: str = "", project_id: str = "") -> Dict[str, Any]:
    """§24, answered from the ledger alone.

    Deliberately a small number of blunt numbers.  The question this has to
    answer at three in the morning is "is the compiler degrading, and what is
    it dropping?", and a report with forty fields does not answer it faster."""
    out: Dict[str, Any] = {
        "owner": owner, "project_id": project_id, "packets": 0, "degraded": 0,
        "degraded_pct": 0.0, "avg_tokens": 0, "avg_budget_pct": 0.0,
        "by_intent": {}, "by_consumer": {}, "omissions": {}, "receipts": 0,
        "store_bytes": 0, "last_packet_at": "",
    }
    where, params = _scope_sql(str(owner or ""), str(project_id or ""))
    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(
                "SELECT intent, consumer, tokens, input_budget, degraded, "
                "omission_counts, created_at FROM context_packets WHERE " + where,
                params))
            out["receipts"] = int(conn.execute(
                "SELECT COUNT(*) AS n FROM context_receipts").fetchone()["n"])
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("context diagnostics failed: %s", exc)
        return out

    tokens = 0
    shares: List[float] = []
    for row in rows:
        out["packets"] += 1
        tokens += int(row.get("tokens") or 0)
        if row.get("degraded"):
            out["degraded"] += 1
        budget = int(row.get("input_budget") or 0)
        if budget:
            shares.append(int(row.get("tokens") or 0) * 100.0 / budget)
        intent = str(row.get("intent") or "")
        consumer = str(row.get("consumer") or "")
        out["by_intent"][intent] = out["by_intent"].get(intent, 0) + 1
        out["by_consumer"][consumer] = out["by_consumer"].get(consumer, 0) + 1
        for reason, count in store.loads_dict(row.get("omission_counts")).items():
            try:
                out["omissions"][reason] = out["omissions"].get(reason, 0) + int(count)
            except (TypeError, ValueError):
                continue
        stamp = str(row.get("created_at") or "")
        if stamp > out["last_packet_at"]:
            out["last_packet_at"] = stamp

    if out["packets"]:
        out["avg_tokens"] = int(tokens / out["packets"])
        out["degraded_pct"] = round(out["degraded"] * 100.0 / out["packets"], 1)
    if shares:
        out["avg_budget_pct"] = round(sum(shares) / len(shares), 1)
    try:
        out["store_bytes"] = store.store_bytes()
    except Exception:  # noqa: BLE001 - a diagnostic may never raise
        out["store_bytes"] = 0
    return out


def prune_packets(*, days: int = 30, now: Optional[datetime] = None) -> int:
    """Drop ledger rows older than `days`, and say how many.

    Only the ledger.  Nothing here touches an experience, a finding or a piece
    of evidence: those have owners, and pruning somebody else's evidence on a
    timer is the one thing §13 forbids a background task outright."""
    try:
        window = max(1, int(days or 30))
    except (TypeError, ValueError):
        window = 30
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    cutoff = (moment - timedelta(days=window)).replace(microsecond=0)
    stamp = cutoff.isoformat().replace("+00:00", "Z")
    try:
        with store.db() as conn:
            cursor = conn.execute(
                "DELETE FROM context_packets WHERE created_at != '' "
                "AND created_at < ?", (stamp,))
            removed = int(cursor.rowcount or 0)
            cursor = conn.execute(
                "DELETE FROM context_receipts WHERE packet_id NOT IN "
                "(SELECT packet_id FROM context_packets)")
            removed += int(cursor.rowcount or 0)
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("context ledger prune failed: %s", exc)
        return 0
    if removed:
        logger.info("context ledger pruned %d row(s) older than %d day(s)",
                    removed, window)
    return removed


# ── the process singleton ──────────────────────────────────────────────────

_LOCK = threading.RLock()
_COMPILER: Optional[ContextCompiler] = None


def compiler() -> ContextCompiler:
    """The process's compiler, built on first use.

    One per process because §1.6's first invariant is "un solo compilador": a
    second one with its own sources and its own cache would answer "why did it
    know that?" differently depending on which consumer asked."""
    global _COMPILER
    with _LOCK:
        if _COMPILER is None:
            _COMPILER = ContextCompiler()
        return _COMPILER


def reset_compiler() -> None:
    """Forget the singleton.  For tests, and for a process that has just been
    pointed at a different data directory."""
    global _COMPILER
    with _LOCK:
        _COMPILER = None


async def compile_packet(request: ContextRequest, **kw: Any) -> ContextPacket:
    """`compiler().compile(request, **kw)`, for callers that want one line."""
    return await compiler().compile(request, **kw)


__all__ = [
    "SCHEMA", "MANDATORY_LOST", "EXPANDED_FROM", "MAX_WARNINGS",
    "REF_SOURCE_TYPES",
    "ContextCompiler", "compiler", "reset_compiler", "compile_packet",
    "record_packet", "record_receipt", "get_packet_row", "recent_packets",
    "diagnostics", "prune_packets",
]
