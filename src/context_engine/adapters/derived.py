"""
context_engine/adapters/derived.py — connecting the six stores this package
built and then never called.

Six derived stores live under ``src/context_engine/`` — blocks, capsules,
experiences, the structural code index, the shared blackboard and the recipe
book.  Every one of them already knew how to answer a question and how to hand
the answer over as :class:`~..contracts.ContextCandidate` s.  Not one of them
was ever registered as a :class:`~..candidates.ContextSource`, so
``candidates.default_sources()`` did not return them, ``compiler.py`` could not
consult them, and ``/context`` reported six declared sources as *unavailable* —
built, populated, and unreachable.  A store nothing can read is a store that
does not exist.  This module is the wiring that stops that being true.

Nothing here is new behaviour: each adapter asks the store it does not own the
question that store already answered, and returns what it already returned.
The rules that make that safe live one file up, in ``adapters/__init__.py``,
and they are followed here.

**The stores are imported inside the method that needs them.**  Six SQLite
schemas, ``core.log_safety`` and ``src.repo_map`` are not the price of
``import src.context_engine.adapters``.

**Availability is not emptiness.**  ``available()`` answers "can this store be
opened", and an empty store answers *yes*: it is available, and it returns zero
candidates.  Those are two different facts and the diagnostics line on the
``/context`` screen shows them differently — "six declared sources are not
available" has to mean something other than "you have not written a block yet",
or nobody can act on either sentence.  The probe is cached per database path
(all six share one file, ``store.db_path()``) because ``gather`` calls
``available()`` on the event loop, once per source, on every turn.

**The gate runs before the store does.**  ``_gate`` is evaluated on the loop and
its string is the reason ``gather`` reports: the recipe book is not consulted
for a turn that is not about media, the code index is not consulted without a
workspace, and the blackboard is not consulted without a scope to look under.
A store consulted and then discarded has already cost a thread — and, for a
store that stamps what it served, left a fingerprint an incognito turn was
promised it would not leave.  ``_ref_gate`` is the narrower half of the same
gate, applied when the runtime has *named* a row: most of ``_gate`` is a guess
about whether the store can help this round, and a guess has no business
refusing a reference somebody passed in on purpose.

**Isolation is a query argument, never a filter over results.**  ``owner`` and
``project_id`` are read off ``request.execution`` and passed into the store
call.  The four read-by-id paths that take no owner — ``blocks.get_block``,
``experiences.get``, ``shared_memory.get`` and ``multimodal_memory.get`` — are
checked against ``req.owner`` here, before the row is allowed to become a
candidate.  (``capsules.load`` and ``code_index.symbols_in`` take the scope
themselves, so there is nothing left to check afterwards.)

``source_ref`` schemes reachable through this module, each minted by the store
that owns it:

    block:<block_id>                       blocks.py
    capsule:<scope_id>                     capsules.py
    experience:<exp_id>                    experiences.py
    symbol:<path>#L<start>-L<end>          code_index.py
    finding:<finding_id>                   shared_memory.py
    recipe:<recipe_id>                     multimodal_memory.py

:class:`ExperienceSource` answers to the short ``exp:`` prefix as well as the
long one: ``experience:`` is what the store writes into a packet, ``exp:`` is
what a caller writes by hand into ``explicit_refs``, and a reference nobody can
resolve is not provenance.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import threading
from typing import Any, Dict, Optional, Sequence, Tuple

from ..candidates import RetrievalRequest, ThreadedSource
from ..contracts import ContextCandidate

logger = logging.getLogger(__name__)

#: Intents that mean "this turn is about generated media".  The same four the
#: planner routes to ``multimodal_recipes``, restated rather than imported: an
#: adapter that depended on the planner would invert the dependency, and the
#: planner is the one module in this package that reads nothing.
MEDIA_INTENTS: Tuple[str, ...] = ("media", "image", "video", "audio")

#: Which recipe media type an intent narrows the search to.  ``media`` names no
#: type on purpose: a request that only said "media" gets every kind of recipe
#: and lets the ranking decide, rather than being answered with nothing.
MEDIA_TYPE_FOR_INTENT: Dict[str, str] = {
    "image": "image",
    "video": "video",
    "audio": "audio",
}

#: What ``ContextTask.intent`` carries when nobody set it.  Restated here for
#: the same reason ``planner.DEFAULT_INTENT`` restates it: on the wire, "chat"
#: and "nothing was declared" are the same bytes, and an adapter that read the
#: default as a declaration would hand ``experiences.search`` a hard
#: ``intent = 'chat'`` filter and answer every coding turn with nothing.
UNSET_INTENT = "chat"


# ── is the store there at all? ─────────────────────────────────────────────

_PROBE_LOCK = threading.RLock()
_PROBE: Dict[str, bool] = {}


def store_reachable() -> bool:
    """Can ``context_engine.db`` be opened and read?  Cached per path.

    This is the honest middle between the two answers that would be useless.
    "The module imported" says nothing about the database; "there are rows"
    would report an empty install as broken.  Opening the store says whether a
    query would work, which is exactly what ``available()`` promises.

    The cache is keyed on ``store.db_path()``, so a test that repoints the
    store with ``use_path`` re-probes and a turn pays the open once per
    process.  Opening the store creates the file when it is missing, the same
    thing the first write would do a moment later.
    """
    from .. import store

    path = store.db_path()
    with _PROBE_LOCK:
        cached = _PROBE.get(path)
    if cached is not None:
        return cached
    try:
        with store.db() as conn:
            conn.execute("SELECT 1")
        ok = True
    except Exception as exc:  # noqa: BLE001 - a store that will not open is not available
        logger.warning("context engine store %s is not reachable: %s", path, exc)
        ok = False
    with _PROBE_LOCK:
        _PROBE[path] = ok
    return ok


def forget_store_probe() -> None:
    """Forget the cached reachability answers.

    For a test that repoints the store at a path it has already used, and for
    the doctor, which repairs a database this module has already given up on.
    """
    with _PROBE_LOCK:
        _PROBE.clear()


# ── small shared readers ───────────────────────────────────────────────────

def _ref_id(source_ref: str, *prefixes: str) -> str:
    """The id inside a ``source_ref``, or ``""`` when the prefix is not ours."""
    ref = str(source_ref or "").strip()
    for prefix in prefixes:
        if ref.startswith(prefix):
            return ref[len(prefix):].strip()
    return ""


def _first(*values: Any) -> str:
    """The first of these that is a non-empty string."""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _raw_intent(req: RetrievalRequest) -> str:
    """The intent the request carries, lowercased.  May be the unset default."""
    return str(req.request.task.intent or "").strip().lower()


def _declared_intent(req: RetrievalRequest) -> str:
    """The intent somebody actually declared, or ``""``.

    Use this wherever the intent NARROWS a store's answer; use
    :func:`_raw_intent` where it only widens one.  The difference matters: a
    filter fed the unset default returns nothing and looks like an empty store,
    while a widening fed the same default adds the two block types a chat
    would have wanted anyway."""
    intent = _raw_intent(req)
    return "" if intent == UNSET_INTENT else intent


def _one(candidates: Sequence[ContextCandidate]) -> Optional[ContextCandidate]:
    """The single candidate a fetch produced, or None.

    ``as_candidates`` drops a row it cannot render rather than raising, so an
    empty list here means "the store had it and could not offer it", which for
    a fetch is the same answer as "gone"."""
    return candidates[0] if candidates else None


class _DerivedSource(ThreadedSource):
    """Shared availability for the six stores that share one SQLite file.

    ``available()`` is two questions in one, and both have to be yes: does the
    module that owns the store import, and does the store open?  Neither of
    them asks how many rows there are — see the module docstring on why an
    empty store is available.
    """

    #: The module under ``src/context_engine`` this adapter reads.
    store_module: str = ""

    def available(self) -> bool:
        try:
            importlib.import_module("..%s" % self.store_module, __package__)
        except Exception as exc:  # noqa: BLE001 - one store, not the retrieval
            logger.debug("context_engine.%s unavailable: %s", self.store_module, exc)
            return False
        return store_reachable()

    def _ref_gate(self, req: RetrievalRequest) -> str:
        """The part of :meth:`_gate` that a named reference cannot argue with.

        ``_gate`` answers "is this store worth consulting for this round", and
        most of that question is about the round: which sections it wants, what
        the intent is, whether there is a query to match on.  None of it
        applies to a ``fetch``, where the runtime has already named the row —
        refusing to reopen ``recipe:abc`` because the turn is not about media
        would break the one channel by which a caller names a specific thing,
        and ``explicit_refs`` comes from the runtime, never from a model.

        What survives is policy and scope: an incognito turn does not get a
        symbol because somebody passed its range, and that range is still
        relative to a workspace nobody named.  The default is the whole gate,
        which is the safe reading for a source with no round-only conditions.
        """
        return self._gate(req)

    async def fetch(self, source_ref: str,
                    req: RetrievalRequest) -> Optional[ContextCandidate]:
        reason = self._ref_gate(req)
        if reason:
            logger.debug("context source %s will not reopen %s: %s",
                         self.source_id, source_ref, reason)
            return None
        return await asyncio.to_thread(self._fetch, source_ref, req)


# ── blocks (§7) ────────────────────────────────────────────────────────────

class BlockSource(_DerivedSource):
    """``context_engine/blocks.py`` — the standing context somebody connected.

    A block is placed, not found: ``blocks_for()`` already applies the
    always-loaded ration, the attachments for this session and agent, and the
    types this intent asks for, so this adapter adds no selection of its own
    beyond dropping the blocks whose section this round did not ask for.

    ``sections`` lists the five a block can land in that a round ever narrows
    to; the map from block type to section is ``blocks.SECTION_BY_TYPE`` and it
    is the store's, not this adapter's.  Three of the five are mandatory
    sections, so this source is woken on every round — which is correct: an
    always-loaded rule that stopped being injected because the turn was a chat
    would be exactly the failure blocks exist to fix.
    """

    source_id = "blocks"
    sections = ("project_rules", "active_goal", "current_state", "decisions",
                "tool_guidance")
    handles = ("block:",)
    store_module = "blocks"

    def _gate(self, req: RetrievalRequest) -> str:
        # No `allow_project_sources` check, on purpose and not by omission:
        # `ranking.PROJECT_SOURCE_TYPES` does not list `block`, so a block that
        # was consulted is a block that will be kept, and an incognito turn
        # still gets to be told who it is and what it may not touch.  See
        # `planner.PROJECT_SOURCE_IDS` for the whole of that argument.
        if not any(req.wants(kind) for kind in self.sections):
            return "no section this source fills was requested"
        return ""

    def _ref_gate(self, req: RetrievalRequest) -> str:
        # `block:<id>` names one row; the section this round wants decides what
        # is worth searching for, never what may be reopened.
        return ""

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        from .. import blocks

        found = blocks.blocks_for(
            owner=req.owner,
            project_id=req.project_id,
            session_id=req.session_id,
            agent_id=req.request.actor.agent_id,
            # The raw intent, default included: `types_for_intent` only ever
            # ADDS types, so reading the unset "chat" as a declaration offers a
            # couple of identity blocks rather than hiding anything.
            intent=_raw_intent(req),
        )
        # `blocks_for` answers with everything this actor should be given; the
        # round decides which of those sections it is filling this time.
        wanted = [block for block in found if req.wants(block.section())]
        return tuple(blocks.as_candidates(wanted[:req.top()]))

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        from .. import blocks

        block_id = _ref_id(source_ref, *self.handles)
        if not block_id:
            return None
        block = blocks.get_block(block_id)
        if block is None:
            return None
        # `get_block` reads by primary key and takes no owner: the isolation
        # every other path gets from `scope_clause` has to be applied here.
        if req.owner and block.owner and block.owner != req.owner:
            logger.debug("blocks: %s is not owned by %s", block_id, req.owner)
            return None
        return _one(blocks.as_candidates([block]))


# ── capsules (§8) ──────────────────────────────────────────────────────────

class CapsuleSource(_DerivedSource):
    """``context_engine/capsules.py`` — the checkpoint that survives the window.

    One capsule per scope, so there is nothing to search: the scope is the
    query.  The run is tried before the session because a run is the narrower
    of the two and a delegated worker's capsule is the one it has to resume
    from; a session with no run of its own falls back to the session id, which
    is what a plain chat compaction writes.

    The capsule carries its own honesty: ``as_candidate`` marks anything but a
    ``verified`` last effect ``degraded``, and ``render()`` puts the uncertain
    effect warning above everything else it claims.
    """

    source_id = "capsules"
    sections = ("current_state",)
    handles = ("capsule:",)
    store_module = "capsules"

    @staticmethod
    def scope_of(req: RetrievalRequest) -> str:
        execution = req.request.execution
        return _first(execution.run_id, execution.session_id)

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.wants("current_state"):
            return "current_state was not requested"
        if not self.scope_of(req):
            return "no run_id and no session_id on the request"
        return ""

    def _ref_gate(self, req: RetrievalRequest) -> str:
        # `capsule:<scope_id>` carries its own scope; the owner check is inside
        # `capsules.load`.  There is nothing left for a gate to decide.
        return ""

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        from .. import capsules

        capsule = capsules.load(self.scope_of(req), owner=req.owner)
        if capsule is None:
            return ()
        return (capsules.as_candidate(capsule),)

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        from .. import capsules

        scope_id = _ref_id(source_ref, *self.handles)
        if not scope_id:
            return None
        capsule = capsules.load(scope_id, owner=req.owner)
        return None if capsule is None else capsules.as_candidate(capsule)


# ── experiences (§9) ───────────────────────────────────────────────────────

class ExperienceSource(_DerivedSource):
    """``context_engine/experiences.py`` — what a run proved, as a pattern.

    ``search`` is the recommendation surface and it already refuses to
    recommend: ``unproved`` rows never come back, and a ``contradicted`` one
    comes back labelled ``anti_pattern`` with the header that says so.  This
    adapter must not undo either, so it passes the hits through
    ``as_candidates`` unchanged — that is where the role reaches both
    ``meta["role"]``, for a renderer, and ``authority``, for the contradiction
    resolver.  A consumer reading only one of the two would print a warning as
    advice.
    """

    source_id = "experiences"
    sections = ("past_experiences",)
    #: The long prefix is what the store writes; the short one is what a person
    #: types into `explicit_refs`.  Both resolve, because a reference nothing
    #: can reopen is not provenance.
    handles = ("exp:", "experience:")
    store_module = "experiences"

    def _gate(self, req: RetrievalRequest) -> str:
        # `experience` is not in `ranking.PROJECT_SOURCE_TYPES` either: "we
        # tried that and the disk disagreed" is the most expensive sentence to
        # have to learn twice, incognito included.
        if not req.wants("past_experiences"):
            return "past_experiences was not requested"
        if not req.allows("lexical"):
            return "the lexical lane is closed"
        return ""

    def _ref_gate(self, req: RetrievalRequest) -> str:
        # `exp:` / `experience:` names one run; the lexical lane and the section
        # list are about searching, and this is not a search.
        return ""

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        from .. import experiences

        hits = experiences.search(
            req.query,
            owner=req.owner,
            project_id=req.project_id,
            # `intent` is an exact SQL filter in this store, so only a declared
            # one is passed: the contract's unset "chat" would narrow every
            # coding turn to the runs somebody labelled "chat" and answer with
            # an empty section that looks like an empty store.
            intent=_declared_intent(req),
            k=req.top(),
        )
        return tuple(experiences.as_candidates(hits))

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        from .. import experiences

        exp_id = _ref_id(source_ref, *self.handles)
        if not exp_id:
            return None
        experience = experiences.get(exp_id)
        if experience is None:
            return None
        if req.owner and experience.owner and experience.owner != req.owner:
            logger.debug("experiences: %s is not owned by %s", exp_id, req.owner)
            return None
        return _one(experiences.as_candidates([experience]))


# ── the code index (§10) ───────────────────────────────────────────────────

class CodeIndexSource(_DerivedSource):
    """``context_engine/code_index.py`` — where a symbol is, not what it says.

    Two gates, both from §10.  Without a workspace there is nothing to scope
    the index to and the answer would be somebody else's checkout; without a
    query there is nothing lexical to match, and the freshness term alone would
    return the four most recently indexed symbols in the repository, which is
    noise dressed as a code map.

    ``as_candidates`` hands over the signature, the range and the summary and
    never the file body: the index points, and the edit is still made against
    the file as it is on disk right now.
    """

    source_id = "code_index"
    sections = ("code_map",)
    handles = ("symbol:",)
    store_module = "code_index"

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.policy.allow_project_sources:
            return "policy.allow_project_sources is false"
        if not req.wants("code_map"):
            return "code_map was not requested"
        if not req.workspace:
            return "no workspace on the request"
        if not req.allows("lexical"):
            return "the lexical lane is closed"
        if not (req.query or "").strip():
            return "no query to look a symbol up by"
        return ""

    def _ref_gate(self, req: RetrievalRequest) -> str:
        # A reference brings its own path and range, but not the root they are
        # relative to: without a workspace this would resolve `src/app.py`
        # against every checkout in the store.
        if not req.policy.allow_project_sources:
            return "policy.allow_project_sources is false"
        if not req.workspace:
            return "no workspace on the request"
        return ""

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        from .. import code_index

        hits = code_index.search(
            req.query,
            workspace=req.workspace,
            project_id=req.project_id,
            k=req.top(),
        )
        return tuple(code_index.as_candidates(hits, workspace=req.workspace))

    @staticmethod
    def _range(ref: str) -> Tuple[str, int, int]:
        """``symbol:<path>#L<start>-L<end>`` split into its three parts.

        A reference with no range is legal and answers ``(path, 0, 0)``: "the
        symbol at this path" is a question ``symbols_in`` can answer, and
        refusing it would make a hand-written reference harder to use than a
        generated one."""
        path, _, span = ref.partition("#L")
        if not span:
            return path.strip(), 0, 0
        first, _, last = span.partition("-L")
        try:
            return path.strip(), int(first), int(last or first)
        except (TypeError, ValueError):
            return path.strip(), 0, 0

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        from .. import code_index

        ref = _ref_id(source_ref, *self.handles)
        if not ref:
            return None
        path, start, end = self._range(ref)
        if not path:
            return None
        symbols = code_index.symbols_in(path, workspace=req.workspace,
                                        project_id=req.project_id)
        if not symbols:
            return None
        chosen = None
        for symbol in symbols:
            if start and symbol.start_line == start and (not end or symbol.end_line == end):
                chosen = symbol
                break
        if chosen is None and start:
            # A range nobody minted exactly: the smallest symbol that contains
            # it is the definition the caller was pointing inside of.
            holding = [s for s in symbols
                       if s.start_line <= start and s.end_line >= (end or start)]
            if holding:
                chosen = min(holding, key=lambda s: s.end_line - s.start_line)
        if chosen is None and not start:
            chosen = symbols[0]
        if chosen is None:
            return None
        return _one(code_index.as_candidates([chosen], workspace=req.workspace))


# ── the blackboard (§11) ───────────────────────────────────────────────────

class FindingSource(_DerivedSource):
    """``context_engine/shared_memory.py`` — what a peer put on the board.

    The scope is the board: a council, a branch, a run or a session, in that
    order, first one that exists.  Widest first is deliberate — a worker inside
    a council branch shares the council's board with its siblings, and falling
    back to its own session id would give it a board with only its own notes on
    it.

    Nothing is consulted without a scope.  A search with none would read every
    finding in the install and rank another owner's council against this turn,
    which is the one thing a shared store must never do.
    """

    source_id = "shared_memory"
    sections = ("peer_findings",)
    handles = ("finding:",)
    store_module = "shared_memory"

    @staticmethod
    def scope_of(req: RetrievalRequest) -> str:
        execution = req.request.execution
        return _first(execution.council_id, execution.branch_id,
                      execution.run_id, execution.session_id)

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.wants("peer_findings"):
            return "peer_findings was not requested"
        if not self.scope_of(req):
            return "no council, branch, run or session to read a board for"
        return ""

    def _ref_gate(self, req: RetrievalRequest) -> str:
        # `finding:<id>` names one row and `get()` reads it in any status, which
        # is how a reader follows a correction back to what it corrected.  The
        # owner check is in `_fetch`, where the row is.
        return ""

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        from .. import shared_memory

        findings = shared_memory.search(
            scope=self.scope_of(req),
            owner=req.owner,
            query=req.query,
            k=req.top(),
        )
        return tuple(shared_memory.as_candidates(findings))

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        from .. import shared_memory

        finding_id = _ref_id(source_ref, *self.handles)
        if not finding_id:
            return None
        finding = shared_memory.get(finding_id)
        if finding is None:
            return None
        # `get` reads by primary key in any status and takes no owner, because
        # that is how a reader follows a correction back.  The owner check the
        # search gets for free has to be made here.
        if req.owner and finding.owner and finding.owner != req.owner:
            logger.debug("findings: %s is not owned by %s", finding_id, req.owner)
            return None
        return _one(shared_memory.as_candidates([finding]))


# ── the recipe book (§12) ──────────────────────────────────────────────────

class RecipeSource(_DerivedSource):
    """``context_engine/multimodal_memory.py`` — how that result was made.

    Only consulted for a turn about media.  The planner is the thing that
    decides this — ``multimodal_recipes`` reaches ``plan.sections`` from the
    four media intents and from nowhere else — so a round that carries a
    section list is asked whether that section is in it, and a round with no
    section list at all (a diagnostic, a direct ``gather``) falls back to the
    intent the request declared.  Reading the intent off the query here would
    be a second classifier disagreeing with the planner's.

    ``search`` hard-filters on compatibility before it ranks: a recipe whose
    model this machine does not have is not offered at all, because a recipe
    with an unknown sampler renders something plausible and nobody notices it
    is not what was asked for.
    """

    source_id = "multimodal"
    sections = ("multimodal_recipes",)
    handles = ("recipe:",)
    store_module = "multimodal_memory"

    @staticmethod
    def wants_media(req: RetrievalRequest) -> bool:
        if req.sections:
            return "multimodal_recipes" in req.sections
        return _raw_intent(req) in MEDIA_INTENTS

    def _gate(self, req: RetrievalRequest) -> str:
        # `recipe` is not in `ranking.PROJECT_SOURCE_TYPES`; the intent is the
        # gate that earns its keep here.
        if not self.wants_media(req):
            return "this turn is not about media, so no recipe can help it"
        return ""

    def _ref_gate(self, req: RetrievalRequest) -> str:
        # The media gate is about not spending a thread on a store that cannot
        # help this turn.  A named `recipe:` is not a guess about the turn.
        return ""

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        from .. import multimodal_memory

        hits = multimodal_memory.search(
            req.query,
            owner=req.owner,
            project_id=req.project_id,
            media_type=MEDIA_TYPE_FOR_INTENT.get(_declared_intent(req), ""),
            k=req.top(),
        )
        return tuple(multimodal_memory.as_candidates(hits))

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        from .. import multimodal_memory

        recipe_id = _ref_id(source_ref, *self.handles)
        if not recipe_id:
            return None
        recipe = multimodal_memory.get(recipe_id)
        if recipe is None:
            return None
        if req.owner and recipe.owner and recipe.owner != req.owner:
            logger.debug("recipes: %s is not owned by %s", recipe_id, req.owner)
            return None
        return _one(multimodal_memory.as_candidates([recipe]))


#: Every adapter in this module, in the order `adapters/__init__.py` registers
#: them.  Exported so a test can assert the six without naming them twice.
DERIVED_SOURCES: Tuple[type, ...] = (
    BlockSource, CapsuleSource, ExperienceSource, CodeIndexSource,
    FindingSource, RecipeSource,
)


__all__ = [
    "MEDIA_INTENTS", "MEDIA_TYPE_FOR_INTENT", "UNSET_INTENT", "DERIVED_SOURCES",
    "BlockSource", "CapsuleSource", "ExperienceSource", "CodeIndexSource",
    "FindingSource", "RecipeSource",
    "store_reachable", "forget_store_probe",
]
