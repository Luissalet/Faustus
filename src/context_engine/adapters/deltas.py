"""
context_engine/adapters/deltas.py — the comparison, retrievable as context.

``contracts.SOURCE_TYPES`` has reserved ``delta`` — "a Universal Delta Engine
result" — since the day it was written, and reserved is not wired.
``adapters/__init__.py`` records what that costs: six derived stores were built
with an ``as_candidates()`` each and registered nowhere, so the compiler could
not see them and ``/context`` reported six declared sources as *unavailable*.
This module is the delta engine's half of that lesson.  It owns no data and
opens no index: ``delta_engine/persistence.py`` already knew how to answer, and
this is the adapter that asks.

**The section is ``past_experiences``, and only that one.**  A delta is two
things at once — what an artifact is like after a change, and the record of
what that change did — so the tempting answer is to declare ``current_state``
as well.  It is the wrong one, twice over.  ``current_state`` is in
``contracts.MANDATORY_SECTIONS``: what lands there is placed by policy and
never ranked (§6.3), so a delta declared there would be injected whatever its
age, its coverage or its relevance — which is §1.4's "no usa deltas como
memoria infalible" mechanised backwards.  And a delta is not the present: it is
a conclusion about two named, immutable revisions, both already in the past by
the time it exists.  ``past_experiences`` is where a conclusion about what a
change did belongs, it is ranked against everything else competing for the
slot, and it is what ``planner.PHASE_ADD["verify"]`` raises — the phase that
most wants to know what the last change actually did.

**The engine's switch does not gate the reading.**  ``agent_delta_engine`` is
off by default and ``src/settings.py`` says why: a comparison re-reads and
re-parses both ends, which is real work nobody asked for at that moment.  It is
a decision about what may COST the machine and never an instruction to hide
what was concluded — so :meth:`DeltaSource.available` never reads it.  A delta
already stored was recorded honestly and stays readable with the engine off,
exactly as ``/api/deltas`` keeps answering its reads.  What makes this source
unavailable is a store that will not open, and nothing else.

**Summaries in the search, detail on demand.**  §1.9.7 and §20: the Context
Engine keeps refs and summaries and opens the detail when somebody asks.  So
``_search`` produces ONE candidate per delta — never one per assertion, because
a delta with three hundred assertions would otherwise flood the budget of every
section it touches — and its body is ``verdict.explain()``, the delta engine's
own sentence about what was requested, what changed without being asked, what
regressed and what could not be checked.  That sentence is bounded by
construction: every clause caps its names at three and adds "and N more", so a
delta with three assertions and one with three hundred produce bodies of the
same order.  :data:`SUMMARY_MAX_CHARS` is the ceiling underneath it, which is
what makes the bound a rule rather than a hope.  ``_fetch`` is the other half:
``delta:<id>`` opens ``material_assertions()``, which the contract already
sorts strongest-severity-first so that a truncated render loses the least.

**A verdict never travels without its reservations.**  §1.4 again: a
``matched`` delta whose semantic coverage was 0.2 comes back saying both,
because ``verdict.explain`` ends with ``coverage.gaps()`` and nothing here
trims that clause off.  Recovering it as "matched" alone is how a conclusion
with reservations becomes a fact three turns later.

**History is reachable, and labelled as history.**  §1.9.6: a quarantine does
not rewrite the deltas that were live when a decision was taken.
``list_deltas`` answers with LIVE rows only, so a superseded delta never
appears in a search; ``get_delta`` answers by id whatever its state, so a
decision that points at one can still open it.  What ``_fetch`` adds is the
label: a retired interpretation says so in its first line, arrives
``degraded``, and drops to ``agent_claim`` so that it cannot outrank the delta
that replaced it.

**Isolation is the query, never a filter over results.**  ``owner`` comes off
``request.execution`` and is passed to ``DeltaStore``, whose every read takes
it as a required keyword.  A ``delta:<id>`` belonging to somebody else answers
None — not an error that would confirm the id exists.  Nothing here reads an
owner out of a ``source_ref``.

``allow_project_sources`` is enforced in ``_gate``, on the event loop and
before the store is consulted, and NOT by adding ``delta`` to
``ranking.PROJECT_SOURCE_TYPES``: the same shape ``ProjectLinksSource`` uses,
for the same reason.  A delta's assertions carry literal ``before``/``after``
excerpts of the two revisions and the paths they live at, which is the content
``file``, ``symbol`` and ``artifact`` are gated for — but a delta of domain
``state`` or ``image`` carries none of it, so the ranker's per-ROW list cannot
name ``delta`` without becoming a second, wider policy over rows it was never
meant to refuse.  Gating the SOURCE is the honest form of the same rule: an
incognito turn never reads one, and the ranker's list stays exactly what it
says it is.

``source_ref`` scheme, minted by ``UniversalDeltaRef.source_ref()`` and never
rebuilt here:

    delta:<delta_id>                       delta_engine/persistence.py
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..candidates import RetrievalRequest, ThreadedSource, make_candidate
from ..contracts import ContextCandidate

logger = logging.getLogger(__name__)

#: How many of the owner's newest deltas one round reads before it filters.
#: A fixed ceiling rather than a multiple of ``req.top()``: the page is read to
#: be filtered by project below, and a scan whose cost grows with the store is
#: how a retrieval that was fast on an empty install becomes the slowest source
#: on a busy one.
SCAN_LIMIT = 30

#: The ceiling on a SEARCH candidate's body.  ``verdict.explain`` is already
#: bounded — every clause names at most three paths and then counts the rest —
#: so this is the belt to that suspenders: whatever a future clause does, one
#: delta cannot grow its share of a packet with the number of assertions it
#: happens to carry.  §1.9.7: refs and summaries here, detail on demand.
SUMMARY_MAX_CHARS = 1200

#: How many material assertions ``_fetch`` opens.  ``material_assertions()``
#: is sorted strongest-severity-first by the contract, so the rows this keeps
#: are the ones §12 says a caller must have seen, and the ones it drops are
#: counted in a line that names the ref they can all be read through.
DETAIL_ASSERTIONS = 20

#: How much of one ``before``/``after`` value a detail line quotes.  The
#: contract already caps each at 2000 characters; twenty of those would be a
#: file pasted into a packet under the name of a summary.
VALUE_CHARS = 120

#: How much of a revision's own name a headline carries.  ``RevisionRef.ref``
#: may be a 1024-character path, and ``make_candidate`` would clip the title
#: silently — clipping the part that is long, rather than the end of the line,
#: keeps the hash visible, which is the half that identifies the revision.
NAME_CHARS = 80


# ── is the store there at all? ─────────────────────────────────────────────

_PROBE_LOCK = threading.RLock()
_PROBE: Dict[str, bool] = {}


def store_reachable() -> bool:
    """Can ``delta_engine.db`` be opened and read?  Cached per path.

    The honest middle between two useless answers, the same one
    ``adapters/derived.py`` settled on: "the module imported" says nothing
    about the database, and "there are rows" would report an empty install as
    broken.  ``counts()`` is the probe because it is the only public read that
    can distinguish the two failures — every other read on ``DeltaStore``
    answers empty both when the store is fine and when the query failed, by
    design ("reads never raise"), while ``counts`` answers ``-1`` per key for a
    read that did not run.

    Cached because ``gather`` calls ``available()`` on the event loop, once per
    source, on every turn; keyed on ``persistence.db_path()`` so a test that
    repoints the store re-probes instead of inheriting the last answer.
    """
    from src.delta_engine import persistence

    path = persistence.db_path()
    with _PROBE_LOCK:
        cached = _PROBE.get(path)
    if cached is not None:
        return cached
    try:
        counts = persistence.store().counts(owner="")
        ok = bool(counts) and all(int(value) >= 0 for value in counts.values())
        if not ok:
            logger.warning("delta store %s did not answer a count; it is not "
                           "available to the context engine", path)
    except Exception as exc:  # noqa: BLE001 - a store that will not open is not available
        logger.warning("delta store %s is not reachable: %s", path, exc)
        ok = False
    with _PROBE_LOCK:
        _PROBE[path] = ok
    return ok


def forget_store_probe() -> None:
    """Forget the cached reachability answers.

    For a test that repoints the store at a path it has already used, and for
    a process that has just been reconfigured with a new data dir.
    """
    with _PROBE_LOCK:
        _PROBE.clear()


# ── small readers ──────────────────────────────────────────────────────────

def _ref_id(source_ref: str, *prefixes: str) -> str:
    """The id inside a ``source_ref``, or ``""`` when the prefix is not ours."""
    ref = str(source_ref or "").strip()
    for prefix in prefixes:
        if ref.startswith(prefix):
            return ref[len(prefix):].strip()
    return ""


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _revision(revision: Any) -> str:
    """``before (checkpoint:9f2c1a4b7d03)`` — the name and the identity.

    Both halves, because they answer different questions: the label is what a
    person recognises, and the hash is what makes the comparison repeatable.
    Twelve hex characters are enough to tell two revisions apart in a sentence
    and short enough that the sentence stays readable; the whole digest is in
    ``meta`` for anything that needs to match on it.
    """
    name = _clip(getattr(revision, "label", "") or getattr(revision, "ref", ""),
                 NAME_CHARS)
    kind = str(getattr(revision, "kind", "") or "revision")
    digest = str(getattr(revision, "hash", "") or "")[:12]
    return f"{name} ({kind}:{digest})" if name else f"{kind}:{digest}"


# ── the source ─────────────────────────────────────────────────────────────

class DeltaSource(ThreadedSource):
    """``delta_engine/persistence.py`` — what changed, and what we could not check.

    One candidate per delta and never one per assertion, a summary in the
    search and the material rows on demand.  See the module docstring for why
    the section is ``past_experiences`` alone, why ``agent_delta_engine`` does
    not gate a read, and why ``allow_project_sources`` is enforced here rather
    than in ``ranking.PROJECT_SOURCE_TYPES``.
    """

    source_id = "deltas"
    sections = ("past_experiences",)
    handles = ("delta:",)

    def available(self) -> bool:
        """Can a delta be read at all?  Never "is the engine switched on".

        ``agent_delta_engine`` decides whether a comparison may RUN.  A delta
        that is already stored was concluded honestly before the switch moved,
        and hiding it would make the switch an instruction to forget rather
        than a budget for work — which is the opposite of what
        ``src/settings.py`` says it is for.  So this asks two questions and
        neither of them is about the setting: does the module import, and does
        the store open.
        """
        try:
            import src.delta_engine.persistence  # noqa: F401
        except Exception as exc:  # noqa: BLE001 - one store, not the retrieval
            logger.debug("src.delta_engine.persistence unavailable: %s", exc)
            return False
        return store_reachable()

    def _gate(self, req: RetrievalRequest) -> str:
        # `allow_project_sources` first, and before any thread is spent: a
        # delta quotes both ends of a comparison, and "consulted and discarded"
        # is not the same thing as "never consulted".  It is enforced here
        # rather than through `ranking.PROJECT_SOURCE_TYPES` because that list
        # names ROW kinds the ranker refuses, and a `state` or `image` delta
        # carries no project content — see the module docstring.
        if not req.policy.allow_project_sources:
            return "policy.allow_project_sources is false"
        if not req.wants("past_experiences"):
            return "past_experiences was not requested"
        # A delta search is a temporal read of the owner's newest comparisons:
        # the store has no text index over deltas, and inventing one here would
        # be the twelfth store `candidates.py` exists to refuse.  Relevance is
        # `ranking._task_fit`'s job, over the summary this source writes.
        if not req.allows("temporal"):
            return "the temporal lane is closed"
        return ""

    def _ref_gate(self, req: RetrievalRequest) -> str:
        """The part of :meth:`_gate` a named reference cannot argue with.

        Most of ``_gate`` is a guess about whether this store can help THIS
        round — which section it wants, which lanes are open.  None of that
        applies to a ``fetch``, where the runtime has already named the row: a
        council decision or a proof pointing at ``delta:<id>`` is not a guess,
        and refusing to reopen it because the turn is a chat would break the
        one channel by which a caller names a specific thing.  What survives is
        the policy, which is not about the round.
        """
        if not req.policy.allow_project_sources:
            return "policy.allow_project_sources is false"
        return ""

    async def fetch(self, source_ref: str,
                    req: RetrievalRequest) -> Optional[ContextCandidate]:
        reason = self._ref_gate(req)
        if reason:
            logger.debug("context source %s will not reopen %s: %s",
                         self.source_id, source_ref, reason)
            return None
        return await asyncio.to_thread(self._fetch, source_ref, req)

    # ── search: the owner's newest comparisons, one summary each ───────────

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        from src.delta_engine import persistence

        # LIVE rows only, which is `list_deltas`' own contract and exactly
        # §1.9.6: a superseded delta is what we used to think, and a search
        # that mixed it in would show one comparison two or three times and
        # leave the reader to work out which is current.  It stays reachable
        # by id — see `_fetch`.
        project = str(req.project_id or "").strip()
        # The wider page is read only when there is something to filter WITH.
        # A request with no project keeps every row it is offered, so asking
        # for more than the round can carry would parse payloads nobody will
        # look at — and a delta payload is as big as the number of assertions
        # it holds.
        rows = persistence.store().list_deltas(
            owner=req.owner, limit=SCAN_LIMIT if project else req.top())
        out: List[ContextCandidate] = []
        for delta in rows:
            # A delta computed outside any project is still this owner's, so
            # the filter keeps the unscoped ones instead of asking the store
            # for `project_id = ?`, which would hide every one of them.  A
            # delta belonging to ANOTHER project is dropped here rather than
            # left for `ranking.validate` to refuse: a guaranteed omission that
            # spent one of `req.top()` slots on the way is not a retrieval.
            if project and delta.project_id and delta.project_id != project:
                continue
            candidate = self._candidate(delta, req, lanes=("temporal",),
                                        detail=False)
            if candidate is not None:
                out.append(candidate)
            if len(out) >= req.top():
                break
        return tuple(out)

    # ── fetch: one delta, with the rows a caller has to have seen ──────────

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        from src.delta_engine import persistence

        delta_id = _ref_id(source_ref, *self.handles)
        if not delta_id:
            return None
        store = persistence.store()
        # The owner comes off the request and never out of the ref.  Another
        # owner's delta answers None here, the same None a delta that never
        # existed answers: telling the two apart would confirm the id.
        delta = store.get_delta(delta_id, owner=req.owner)
        if delta is None:
            return None
        live, successor = self._live_state(store, delta)
        return self._candidate(delta, req, lanes=("exact",), detail=True,
                               live=live, successor=successor)

    @staticmethod
    def _live_state(store: Any, delta: Any) -> Tuple[bool, str]:
        """``(is_live, successor_id)`` for a delta reached by id.

        ``UniversalDelta`` has no ``superseded`` field — the genealogy is in
        the store's columns — so this asks the question the store can answer:
        ``find_delta`` returns the LIVE delta for a fingerprint, and
        ``idx_delta_cache`` guarantees there is at most one.  Same id means
        this one is current; a different id names what replaced it; none at all
        means it was retired without a successor, which is what
        ``invalidate_for_revision`` does to a comparison whose revision turned
        out to be wrong.

        A read that fails answers ``None`` too, so a store having a bad day
        makes a live delta read as history.  That is the direction to be wrong
        in: a retired interpretation labelled current is how a decision gets
        taken on what we used to think.
        """
        live = store.find_delta(delta.fingerprint(), owner=delta.owner)
        if live is None:
            return False, ""
        if live.id == delta.id:
            return True, ""
        return False, str(live.id)

    # ── one delta, as one candidate ────────────────────────────────────────

    def _candidate(self, delta: Any, req: RetrievalRequest, *,
                   lanes: Tuple[str, ...], detail: bool, live: bool = True,
                   successor: str = "") -> Optional[ContextCandidate]:
        """The summary, plus the material rows when somebody asked for them.

        Everything a search produces goes through the ``detail=False`` half:
        counts, the verdict, its reservations, and the ref to open for the
        rest.  ``detail=True`` is what ``delta:<id>`` gets, and it is the only
        path on which an assertion's ``before``/``after`` values are quoted at
        all.
        """
        ref = delta.ref()
        material = delta.material_assertions()
        counts = delta.summary()

        lines: List[str] = []
        if not live:
            lines.append(self._superseded_notice(successor))
        lines.append(self._explain(delta))
        lines.append(f"Compared {delta.domain}: {_revision(delta.source)} -> "
                     f"{_revision(delta.target)}.")
        lines.append(
            f"{counts['assertions']} assertions, {counts['material']} material; "
            f"strongest severity {counts['severity']}, weakest confidence "
            f"{counts['confidence']}.")
        if detail:
            lines.extend(self._detail_lines(delta, material))
        else:
            lines.append(f"Open {ref.source_ref()} for the material assertions.")

        body = "\n".join(line for line in lines if line)
        if not detail:
            # The rule, with a ceiling rather than a hope: a search candidate
            # is a summary, and a summary whose length tracks the number of
            # assertions is one delta spending everybody else's budget.
            body = _clip(body, SUMMARY_MAX_CHARS)

        # A comparison with an unreadable end established nothing, and
        # `inconclusive` is the delta engine saying so itself; both are worth
        # less than a measurement, and `ranking` already knows what to do with
        # `degraded`.  A retired interpretation is degraded for a different
        # reason and to the same effect: it must not outrank what replaced it.
        readable = bool(delta.coverage.both_readable)
        confident = readable and delta.assessment != "inconclusive"
        title = (f"{'SUPERSEDED ' if not live else ''}delta {delta.assessment}: "
                 f"{delta.domain} {_revision(delta.source)} -> "
                 f"{_revision(delta.target)}")

        meta: Dict[str, Any] = {
            "delta_id": delta.id,
            "assessment": delta.assessment,
            "domain": delta.domain,
            "source_revision": ref.source_revision,
            "target_revision": ref.target_revision,
            "intent_contract_id": delta.intent_contract_id,
            "coverage": dict(ref.coverage),
            "assertions": int(counts["assertions"]),
            "material_assertions": int(counts["material"]),
            "live": bool(live),
        }
        if successor:
            meta["superseded_by"] = successor

        return make_candidate(
            source_type="delta",
            source_ref=ref.source_ref(),
            section="past_experiences",
            title=title,
            body=body,
            lanes=lanes,
            scores={"assertions": float(counts["assertions"]),
                    "material": float(counts["material"])},
            # An extractor measured this under a contract that refuses
            # `preserved` without an observation, which is more than an agent's
            # claim and less than a `prove` verdict — rule 6 of the delta
            # contracts keeps those two apart, so this never says `proved`.
            trust_class="agent_validated" if confident else "agent_assertion",
            authority="validated_experience" if (confident and live) else "agent_claim",
            # A delta is superseded rather than edited, so its fingerprint —
            # source, target, intent, domain and extractor versions — is what
            # identifies the comparison this text came from.  A recomputation
            # mints a new id and a new candidate instead of moving this one.
            source_revision=delta.fingerprint(),
            observed_at=delta.created_at,
            owner=delta.owner or req.owner,
            project_id=delta.project_id,
            degraded=(not readable) or (not live),
            meta=meta,
        )

    @staticmethod
    def _explain(delta: Any) -> str:
        """The delta engine's own sentence about its own result.

        Written by ``verdict.explain`` rather than here, and that is the point:
        §35's four clauses — achieved, unrequested, preserved, not checked —
        already exist, they are already bounded (three names per clause, then a
        count), and a second summariser in this package would be a second
        answer that drifts from the first.  The last clause is the one §1.4
        needs: a `matched` delta whose semantic coverage was 0.2 says both, and
        nothing here trims it off.

        ``unmet`` is not passed because a delta does not store one — it is
        computed against the frozen intent while the comparison runs and is not
        a field of ``UniversalDelta``.  Every other clause is built from the
        rows this delta carries, so what is missing is a clause, never a
        reassurance invented to fill it.
        """
        from src.delta_engine import verdict

        try:
            return verdict.explain(delta.assessment, assertions=delta.assertions,
                                   invariants=delta.invariants,
                                   coverage=delta.coverage)
        except Exception as exc:  # noqa: BLE001 - a summary may never break a turn
            logger.warning("delta %s could not be explained: %s", delta.id, exc)
            return f"Assessment: {delta.assessment}."

    @staticmethod
    def _superseded_notice(successor: str) -> str:
        tail = f" by delta:{successor}" if successor else " and nothing replaced it"
        return ("SUPERSEDED: this interpretation was retired" + tail + ". It is "
                "still readable because a decision taken while it was live may "
                "point at it; it is not what this owner's record says now.")

    @staticmethod
    def _detail_lines(delta: Any, material: Sequence[Any]) -> List[str]:
        """The rows §12 says a caller must have seen, strongest first.

        ``material_assertions()`` is already sorted by the contract, so the
        rows kept here are the ones that matter and the rows dropped are the
        weakest — and the count of them is stated rather than elided, with the
        ref that still holds all of them.  A truncation nobody declares is the
        summary §12 forbids; a truncation that says what it left out and where
        to find it is the only way a delta with three hundred assertions is
        readable at all.
        """
        if not material:
            return ["No material assertion: nothing in this delta reached "
                    "`material` or `blocking` severity."]
        shown = list(material[:DETAIL_ASSERTIONS])
        out: List[str] = [f"Material assertions ({len(material)}), strongest first:"]
        for assertion in shown:
            out.append(f"- {assertion.path}: {assertion.operation} / "
                       f"{assertion.classification} [{assertion.severity}], "
                       f"confidence {assertion.confidence} ({assertion.tier})")
            if assertion.before or assertion.after:
                out.append(f"    {_clip(assertion.before, VALUE_CHARS) or '(absent)'}"
                           f" -> {_clip(assertion.after, VALUE_CHARS) or '(absent)'}")
            if assertion.detail:
                out.append("    " + _clip(assertion.detail, VALUE_CHARS))
            if assertion.limitations:
                out.append("    not established: "
                           + _clip("; ".join(assertion.limitations), VALUE_CHARS))
        if len(material) > len(shown):
            out.append(f"... and {len(material) - len(shown)} more material "
                       f"assertions, none of them stronger than these; "
                       f"delta:{delta.id} holds all of them.")
        if delta.limitations:
            out.append("Limitations: " + _clip("; ".join(delta.limitations), 300))
        return out


__all__ = ["DeltaSource", "store_reachable", "forget_store_probe",
           "SCAN_LIMIT", "SUMMARY_MAX_CHARS", "DETAIL_ASSERTIONS", "VALUE_CHARS",
           "NAME_CHARS"]
