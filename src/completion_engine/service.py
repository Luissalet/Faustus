"""The Greedy Completion Engine's façade: decide whether a turn is finished.

Called from exactly one place that matters -- `src/agent_loop.py`, at the
moment the model produces a round with no tool calls and the loop is about to
`break` -- and it answers one question: is this done, or is there work left that
this mode says to do?

The order here IS the policy, and it is the same order for every mode:

    resolve the mode  ->  compile the scope envelope  ->  write the contract
    ->  budget it  ->  discover candidates  ->  admit, score, rank
    ->  select what fits and is safe  ->  decide  ->  record

Three things this module refuses to do, and each is a rule the plan states:

* **It grants nothing.** A mode is depth, not authority (§1.12.1). Every
  candidate passes `scope.admits` -- permissions first, then effects, then
  protected paths, then the envelope -- BEFORE it is scored, so a valuable
  candidate cannot argue its way past a missing permission.
* **It does not spend the verification reserve.** In any mode. `CompletionBudget`
  makes that structural and this module never looks for a way around it.
* **It does not call a model.** Every generator in `discovery.py` is
  deterministic, and the value of a candidate is computed from what produced it
  rather than asked of the thing that proposed it (§30).

**Shadow mode is the default and it is the point.** `agent_completion_engine`
is off; `agent_completion_engine_shadow` is on. In shadow the engine does all of
the above, records the decision it WOULD have taken, and returns nothing for the
loop to act on -- so the turn behaves exactly as it did before. That is the
measurement `agent_context_engine_shadow` exists for in the context engine, for
the same reason: the alternative to a measured change is an unmeasured one.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.completion_engine import budgeting, closeout as closeout_mod
from src.completion_engine import convergence as convergence_mod
from src.completion_engine import discovery, frontier as frontier_mod, persistence, scope
from src.completion_engine.adapters import degraded as degraded_integrations
from src.completion_engine.contracts import (
    COMPLETION_MODES,
    LAYERS,
    BudgetSpend,
    CompletionContract,
    CompletionDecision,
    CompletionError,
    ImprovementCandidate,
    ScopeEnvelope,
    policy_for,
)
from src.completion_engine.events import COMPLETION_EVENTS, stop_event, stream_for

logger = logging.getLogger(__name__)

__all__ = [
    "SETTING",
    "SHADOW_SETTING",
    "ERRORS",
    "CompletionServiceError",
    "CompletionEngineService",
    "enabled",
    "shadow_enabled",
    "active",
    "resolve_mode_for_turn",
    "service",
    "reset_service",
]

SETTING = "agent_completion_engine"
SHADOW_SETTING = "agent_completion_engine_shadow"

#: How many improvements one round may hand back. See the comment where it is
#: applied: a batch, not a backlog.
MAX_PER_BATCH = 3

ERRORS: Tuple[str, ...] = (
    "not_found", "disabled", "invalid_argument", "unknown_mode", "store_failed",
)


class CompletionServiceError(CompletionError):
    """A refusal from the service, carrying one of `ERRORS` as its code."""

    def __init__(self, path: str, message: str, *, code: str = "invalid_argument",
                 got: Any = None) -> None:
        super().__init__(path, message, got=got)
        self.code = code if code in ERRORS else "invalid_argument"


def _setting(name: str, default: Any) -> Any:
    """Read a setting live. Never captured at import, never allowed to raise."""
    try:
        from src.settings import get_setting

        return get_setting(name, default)
    except Exception:  # noqa: BLE001 - a turn never fails over a settings lookup
        logger.debug("completion engine: %s unreadable; using %r", name, default)
        return default


def enabled() -> bool:
    """Whether the engine may CHANGE what a turn does."""
    return bool(_setting(SETTING, False))


def shadow_enabled() -> bool:
    """Whether the engine may compute and record without changing anything."""
    return bool(_setting(SHADOW_SETTING, True))


def active() -> bool:
    """Whether there is any reason to run at all.

    Both switches off means not even the measurement runs, and that is a real
    choice an operator can make: the engine costs a discovery pass per turn.
    """
    return enabled() or shadow_enabled()


def resolve_mode_for_turn(instruction: str = "", *, project_default: str = "",
                          agent_default: str = "", task: str = "",
                          activity: str = "") -> Any:
    """The completion mode for this turn, and where it came from.

    Delegates entirely to `src/agent_profiles/completion.py`, which has owned
    this since plan 3: `resolve_mode` applies the precedence and
    `from_instruction` reads an explicit override out of what the person
    actually wrote. Re-implementing either here would produce two answers to
    one question -- and the interesting half of that module is precisely the
    part nobody was calling.

    An instruction override wins over every default, which is §4.5: "solo cambia
    esta línea" has to be able to narrow a greedy run, and no default may
    outrank a sentence the person typed.
    """
    from src.agent_profiles.completion import from_instruction, resolve_mode

    try:
        override = from_instruction(instruction or "")
    except Exception:  # noqa: BLE001 - a mode reading never breaks a turn
        logger.debug("completion engine: instruction unreadable", exc_info=True)
        override = ""
    return resolve_mode(
        activity=activity,
        task=task or override,
        run=override,
        agent_default=agent_default,
        project_default=project_default,
        global_default="greedy",
    )


class CompletionEngineService:
    """One instance per process. A store, a publisher, and no memory of its own.

    No cached mode, no cached budget, no cached frontier: a turn is decided from
    what the turn presents, and a service that remembered any of the three would
    answer the previous turn's question.
    """

    def __init__(self, *, store: Any = None, publisher: Any = None) -> None:
        self._store = store
        self._publisher = publisher

    # -- plumbing ---------------------------------------------------------

    def store(self) -> Any:
        return self._store if self._store is not None else persistence.store()

    def _stream(self, owner: str) -> Any:
        if self._publisher is not None:
            return self._publisher
        return stream_for(owner)

    def _emit(self, owner: str, name: str, **payload: Any) -> None:
        """Publish one event, and never let a publisher failure lose a turn.

        The loop this runs inside is producing the user's answer. Nothing here
        is allowed to be the reason it stops.
        """
        if name not in COMPLETION_EVENTS:
            logger.warning("completion engine: %r is not a declared event name", name)
        try:
            stream = self._stream(owner)
            publish = getattr(stream, "publish", None)
            if callable(publish):
                publish(name, owner=owner, **payload)
        except Exception as exc:  # noqa: BLE001 - the page is not the record
            logger.warning("completion engine: publishing %s failed: %s", name, exc)

    # -- the one call the agent loop makes --------------------------------

    def decide_for_turn(
        self, *, owner: str, instruction: str = "",
        ledger_summary: Optional[Mapping[str, Any]] = None,
        workspace: str = "", project_id: str = "", session_id: str = "",
        run_id: str = "", rounds_used: int = 0, rounds_budget: int = 0,
        tool_calls: int = 0, tokens: int = 0, seconds: float = 0.0,
        completion_choice: Any = None, permissions: Any = None,
        static_checks: Optional[Mapping[str, Any]] = None,
        tests: Optional[Mapping[str, Any]] = None,
        review: Optional[Mapping[str, Any]] = None,
        changeset: Optional[Mapping[str, Any]] = None,
        proof: Optional[Mapping[str, Any]] = None,
        delta: Optional[Mapping[str, Any]] = None,
        situations: Optional[Mapping[str, Sequence[Mapping[str, Any]]]] = None,
        persist: bool = True,
    ) -> Dict[str, Any]:
        """Is this turn finished? Returns the decision and what to do next.

        Never raises. A turn that fails here still ends -- the answer is
        `{"ok": False, ...}` and the loop breaks exactly as it did before the
        engine existed. That is not defensiveness for its own sake: this
        function runs while the user is waiting for their reply, and an
        exception escaping it would turn "the completion engine had a bug" into
        "your answer disappeared".
        """
        started = time.monotonic()
        try:
            return self._decide(
                owner=owner, instruction=instruction,
                ledger_summary=dict(ledger_summary or {}),
                workspace=workspace, project_id=project_id, session_id=session_id,
                run_id=run_id, rounds_used=rounds_used, rounds_budget=rounds_budget,
                tool_calls=tool_calls, tokens=tokens, seconds=seconds,
                completion_choice=completion_choice, permissions=permissions,
                static_checks=dict(static_checks or {}), tests=dict(tests or {}),
                review=dict(review or {}), changeset=dict(changeset or {}),
                proof=dict(proof or {}), delta=dict(delta or {}),
                situations=dict(situations or {}), persist=persist,
                elapsed_start=started,
            )
        except Exception as exc:  # noqa: BLE001 - the turn is not ours to break
            logger.warning("completion engine: decision failed: %s", exc, exc_info=True)
            self._emit(owner, "completion_error", run_id=run_id, detail=str(exc))
            return {"ok": False, "error": str(exc), "continue_with": [],
                    "shadow": not enabled()}

    def _decide(self, *, owner: str, instruction: str, ledger_summary: Dict[str, Any],
                workspace: str, project_id: str, session_id: str, run_id: str,
                rounds_used: int, rounds_budget: int, tool_calls: int, tokens: int,
                seconds: float, completion_choice: Any, permissions: Any,
                static_checks: Dict[str, Any], tests: Dict[str, Any],
                review: Dict[str, Any], changeset: Dict[str, Any],
                proof: Dict[str, Any], delta: Dict[str, Any],
                situations: Dict[str, Any], persist: bool,
                elapsed_start: float) -> Dict[str, Any]:
        shadow = not enabled()

        # 1. The mode. An explicit `CompletionChoice` from a resolved execution
        #    wins over re-resolving here, because that one was decided with the
        #    project's and the agent's defaults in hand and this one has only
        #    what the loop passed.
        choice = completion_choice
        if choice is None:
            choice = resolve_mode_for_turn(instruction)
        mode = str(getattr(choice, "mode", "") or "greedy")
        if mode not in COMPLETION_MODES:
            mode = "greedy"
        policy = policy_for(mode)

        # 2. The envelope. Compiled from what was asked and what was touched,
        #    then narrowed by anything the instruction said. Never widened.
        touched = tuple(ledger_summary.get("mutations") or ())
        if not touched:
            touched = tuple(ledger_summary.get("mutated_paths") or ())
        envelope = scope.compile_envelope(
            goal=instruction, instruction=instruction, owner=owner,
            project_id=project_id, workspace=workspace, mode=mode,
            touched=touched, permissions=permissions,
        )
        envelope = scope.narrow_from_instruction(envelope, instruction)
        self._emit(owner, "completion_scope_compiled", run_id=run_id,
                   scope_envelope_id=envelope.id, mode=mode,
                   resources=len(envelope.allowed_resources),
                   protected=len(envelope.protected_resources))

        # 3. The contract: what this turn owes, with the domain's own checklist.
        from src.completion_engine.playbooks import playbook_for

        book = playbook_for("code")
        expectations = tuple(book.expectations(mode=mode)) if book is not None else ()
        done = tuple(book.definition_of_done(layer="professional")) if book is not None else ()
        contract = CompletionContract.parse({
            "mode": mode,
            "policy_version": policy.policy_version,
            "mode_source": str(getattr(choice, "source", "") or ""),
            "scope_envelope_id": envelope.id,
            "core_deliverables": [instruction[:480]] if instruction else [],
            "professional_expectations": list(expectations),
            "definition_of_done": list(done),
            "playbook": "code",
            "owner": owner, "project_id": project_id, "run_id": run_id,
            "session_id": session_id,
        }, "completion_contract")
        self._emit(owner, "completion_contract_created", run_id=run_id,
                   contract_id=contract.id, mode=mode,
                   policy_version=policy.policy_version,
                   scope_envelope_id=envelope.id)

        # 4. The budget. This is where `bonus_budget_share` finally has a
        #    denominator: the totals of the turn the loop just spent.
        #    A CEILING is not a SPEND, and passing the second as the first is
        #    the mistake this comment exists to stop coming back: handing
        #    `tokens=12000` in as the total and then recording 12000 against
        #    core makes every turn report `budget` as its stop reason, which is
        #    the dishonest answer this engine was built to avoid.
        #
        #    Only rounds are metered today, and that is deliberate. The chat
        #    loop has a round ceiling (`agent_max_rounds`) and no per-turn
        #    ceiling for tokens, tool calls or wall time, so declaring one here
        #    would invent a budget nobody set and then report running out of
        #    it. `CompletionBudget` reads a total of zero as "this unit is not
        #    counted", which is the truthful state.
        budget = budgeting.from_settings(
            mode=mode,
            max_rounds=max(1, int(rounds_budget or rounds_used or 1)),
            max_tool_calls=0,
            token_budget=0,
            wall_seconds=0.0,
        )
        budget = budgeting.record(budget, "core", BudgetSpend(
            rounds=max(0, int(rounds_used or 0)),
            tool_calls=max(0, int(tool_calls or 0)),
            tokens=max(0, int(tokens or 0)),
            seconds=max(0.0, float(seconds or 0.0)),
        ))

        # 5. Discovery. Deterministic generators only, every candidate with
        #    evidence behind it.
        data = discovery.DiscoveryInput(
            contract=contract, envelope=envelope,
            ledger_summary=ledger_summary, static_checks=static_checks,
            tests=tests, review=review, changeset=changeset, proof=proof,
            delta=delta, situations=situations, workspace=workspace,
        )
        found = discovery.discover(data)
        for candidate in found:
            self._emit(owner, "completion_candidate_discovered", run_id=run_id,
                       contract_id=contract.id, candidate_id=candidate.id,
                       layer=candidate.layer, source=candidate.source,
                       title=candidate.title[:200])

        # 6. Admission, scoring, ranking. `frontier.build` runs them in that
        #    order and nothing here reorders it: a candidate that fails
        #    admission must never reach the scorer, or a high value becomes an
        #    argument against a permission.
        #    `declined` is the one input to this step that did not come from
        #    this turn. It is what the OWNER has already said no to, read from
        #    the store, and it is passed here rather than filtered afterwards
        #    so that a refusal is enforced where blocking decisions belong --
        #    above scoring, where nothing can outrank it. §1.8. A read that
        #    fails costs a repeated question and never the turn.
        try:
            declined = self.store().declined_keys(
                owner=owner, project_id=str(project_id or ""))
        except Exception:  # noqa: BLE001
            logger.warning("completion engine: refusals unreadable; a declined "
                           "improvement may be offered again", exc_info=True)
            declined = ()
        ranked = frontier_mod.build(
            found, envelope=envelope, budget=budget, mode=mode,
            permissions=permissions, declined=declined, minimum=0.0,
        )
        self._emit(owner, "completion_frontier_recomputed", run_id=run_id,
                   contract_id=contract.id,
                   candidates=len(found), admitted=len(ranked.selected()))
        for entry in ranked.entries:
            if not entry.admitted:
                self._emit(owner, "completion_candidate_rejected", run_id=run_id,
                           candidate_id=entry.candidate.id, reason=entry.reason,
                           layer=entry.candidate.layer)

        # 7. What would actually run. Two filters and they are different: the
        #    budget says what fits, and `safe_to_run_unasked` says what §12
        #    allows without going back to the person. A candidate that fits and
        #    is not safe is DEFERRED, not silently dropped -- it is exactly the
        #    thing worth asking about.
        selected, batch_reason = frontier_mod.batch(
            [e for e in ranked.entries if e.admitted], budget=budget, line="bonus")
        layers_open = contract.layers()
        runnable: List[ImprovementCandidate] = []
        deferred: List[ImprovementCandidate] = []
        for candidate in selected:
            if candidate.layer not in layers_open:
                deferred.append(candidate)
                continue
            if candidate.layer in ("core", "professional") or candidate.safe_to_run_unasked:
                runnable.append(candidate)
            else:
                deferred.append(candidate)

        # A batch, not a backlog. The first measured turn produced twenty-seven
        # runnable items -- every unmet line of the domain's checklist at once
        # -- and handing a model twenty-seven instructions is worse than
        # handing it three: it reads as a wall, the ordering is lost, and
        # nothing on the list gets the attention the top of it deserved. The
        # rest are DEFERRED and keep their evidence, so the next round proposes
        # them again if they are still true, which is what §9's re-evaluation
        # is for.
        overflow: List[ImprovementCandidate] = []
        if len(runnable) > MAX_PER_BATCH:
            overflow = runnable[MAX_PER_BATCH:]
            runnable = runnable[:MAX_PER_BATCH]

        # 8. Why we are stopping. The two honest stops and the interruptions,
        #    kept apart by `convergence.stop_reason` and then again by the
        #    contract, which refuses `converged` while a line is dry.
        state = convergence_mod.step(
            convergence_mod.ConvergenceState(), frontier=ranked,
            executed_now=len(runnable),
            max_rounds=int(_setting("agent_completion_max_bonus_rounds", 3)),
        )
        stop_reason = convergence_mod.stop_reason(state=state, budget=budget)
        if not stop_reason:
            # `convergence.stop_reason` answers `""` for "do not stop yet",
            # which is a real state and not an error: there is admitted,
            # affordable work left. The turn is ending anyway -- the model
            # produced a round with no tool calls -- so what gets recorded is
            # `unfinished`, the one word that is neither "there was nothing
            # more worth doing" nor "we ran out". In shadow that IS the
            # measurement; live, it is what the loop was asked to continue on.
            stop_reason = "unfinished" if runnable else "converged"
        if policy.stop_on_core_proved and not runnable:
            stop_reason = "core_only"
        completed = tuple(layer for layer in LAYERS
                          if layer in layers_open and layer != "exploratory")

        decision = CompletionDecision.parse({
            "contract_id": contract.id,
            "scope_envelope_id": envelope.id,
            "mode": mode, "policy_version": policy.policy_version,
            "completed_layers": list(completed),
            "stop_reason": stop_reason,
            "stop_detail": batch_reason,
            "executed": [],
            # `ranked.rejected()` and NOT a comprehension over the entries.
            # That method exists precisely to move the reason and the status off
            # the entry and onto the candidate, because the candidate is what
            # outlives the frontier and lands in this decision. Building the
            # list by hand from `e.candidate` stored every rejection this engine
            # has ever made as a bare `candidate` with no reason at all -- the
            # screen drew them as "nobody recorded why", which is the exact
            # sentence a closed vocabulary exists to make impossible, and it was
            # invisible until a run finally refused something.
            "rejected": [c.to_dict() for c in ranked.rejected()],
            "deferred": [c.to_dict() for c in
                         _as_deferred(deferred, "below_threshold")
                         + _as_deferred(overflow, "round_full")],
            "budget": budget.to_dict(),
            "proof_refs": [str(proof.get("identity") or "")] if proof.get("identity") else [],
            "delta_refs": [str(delta.get("id") or "")] if delta.get("id") else [],
            "changeset_refs": [str(changeset.get("id") or "")] if changeset.get("id") else [],
            "degraded_integrations": list(degraded_integrations()),
            "shadow": shadow,
            "owner": owner, "project_id": project_id, "run_id": run_id,
            "session_id": session_id,
        }, "decision")

        if persist:
            try:
                self.store().save_scope(envelope)
                self.store().save_contract(contract)
                self.store().save_decision(decision)
            except Exception as exc:  # noqa: BLE001 - a record is not the turn
                logger.warning("completion engine: storing the decision failed: %s", exc)

        self._emit(owner, stop_event(stop_reason), run_id=run_id,
                   decision_id=decision.id, contract_id=contract.id,
                   mode=mode, stop_reason=stop_reason, shadow=shadow)
        self._emit(owner, "completion_decision_recorded", run_id=run_id,
                   decision_id=decision.id, contract_id=contract.id, mode=mode,
                   stop_reason=stop_reason, shadow=shadow,
                   runnable=len(runnable), deferred=len(deferred),
                   rejected=len([e for e in ranked.entries if not e.admitted]))

        report = closeout_mod.build(decision, contract=contract, proof=proof, delta=delta)
        return {
            "ok": True,
            "shadow": shadow,
            "mode": mode,
            "decision": decision.to_dict(),
            "decision_id": decision.id,
            "stop_reason": stop_reason,
            "closeout": closeout_mod.render(report),
            "summary": closeout_mod.summary_line(report),
            # The ONLY field the agent loop acts on, and it is empty in shadow.
            # Everything above happens either way; this is the whole difference
            # between measuring and doing.
            "continue_with": ([] if shadow else
                              [c.title for c in runnable if c.title]),
            "runnable": [c.to_dict() for c in runnable],
            "deferred": [c.to_dict() for c in deferred] + [c.to_dict() for c in overflow],
            "elapsed_ms": int((time.monotonic() - elapsed_start) * 1000),
        }

    # -- reading (never gated) ---------------------------------------------

    def get(self, decision_id: str, *, owner: str) -> Dict[str, Any]:
        row = self.store().get_decision(str(decision_id or ""), owner=owner)
        if row is None:
            raise CompletionServiceError("decision_id", "names no decision of yours",
                                         code="not_found", got=decision_id)
        contract = self.store().get_contract(row.contract_id, owner=owner)
        report = closeout_mod.build(row, contract=contract)
        return {"ok": True, "decision": row.to_dict(),
                "closeout": closeout_mod.render(report),
                "summary": row.summary()}

    def list(self, *, owner: str, shadow: Optional[bool] = False, mode: str = "",
             project_id: str = "", stop_reason: str = "", limit: int = 50,
             cursor: str = "") -> Dict[str, Any]:
        rows = self.store().list_decisions(
            owner=owner, shadow=shadow, mode=mode, project_id=project_id,
            stop_reason=stop_reason, limit=max(1, min(200, int(limit or 50))),
            cursor=str(cursor or ""))
        return {
            "ok": True,
            "enabled": enabled(),
            "shadow_enabled": shadow_enabled(),
            "decisions": [{**row.summary(), "id": row.id, "run_id": row.run_id,
                           "created_at": row.created_at,
                           "project_id": row.project_id} for row in rows],
            "next_cursor": (self.store().page_cursor(rows[-1])
                            if rows and len(rows) >= limit else ""),
        }

    def config(self) -> Dict[str, Any]:
        from src.completion_engine.contracts import (
            BUDGET_LINES, CANDIDATE_CATEGORIES, DISCOVERY_SOURCES, EFFECTS,
            REJECTION_REASONS, RELATIONS, SPENDABLE_UNITS, STOP_REASONS,
        )

        return {
            "ok": True,
            "enabled": enabled(),
            "shadow_enabled": shadow_enabled(),
            "modes": list(COMPLETION_MODES),
            "layers": list(LAYERS),
            "relations": list(RELATIONS),
            "stop_reasons": list(STOP_REASONS),
            "rejection_reasons": list(REJECTION_REASONS),
            "categories": list(CANDIDATE_CATEGORIES),
            "sources": list(DISCOVERY_SOURCES),
            "budget_lines": list(BUDGET_LINES),
            "units": list(SPENDABLE_UNITS),
            "effects": list(EFFECTS),
            "events": list(COMPLETION_EVENTS),
            "policies": {mode: {
                "policy_version": policy_for(mode).policy_version,
                "description": policy_for(mode).description,
                "explore_frontier": policy_for(mode).explore_frontier,
                "bonus_budget_share": policy_for(mode).bonus_budget_share,
                "stop_on_core_proved": policy_for(mode).stop_on_core_proved,
                "max_extra_layers": policy_for(mode).max_extra_layers,
                "requires_verification": policy_for(mode).requires_verification,
            } for mode in COMPLETION_MODES},
        }

    def diagnostics(self, *, owner: str) -> Dict[str, Any]:
        store = self.store()
        try:
            counts = store.counts(owner=owner)
        except Exception:  # noqa: BLE001
            counts = {}
        try:
            rejections = store.rejection_stats(owner=owner, shadow=None)
        except Exception:  # noqa: BLE001
            rejections = {}
        return {
            "ok": True,
            "enabled": enabled(),
            "shadow_enabled": shadow_enabled(),
            "counts": counts,
            "rejections": rejections,
            "degraded": list(degraded_integrations()),
            "db_path": store.path(),
            "schemas": list(persistence.registered_schemas()),
        }

    def events(self, *, owner: str) -> Any:
        return self._stream(owner)


def _as_deferred(candidates: Sequence[ImprovementCandidate], reason: str
                 ) -> Tuple[ImprovementCandidate, ...]:
    """Stamp `deferred` on each, with the reason it was actually deferred.

    Two reasons and not one, because they are different facts and §19 hands
    these on to be reconsidered: `below_threshold` says the value did not clear
    the bar, and `round_full` says it did and this round had already taken its
    batch. Filing the second as the first would tell the next reader that
    twenty good candidates were judged not worth doing.
    """
    out = []
    for candidate in candidates:
        payload = candidate.to_dict()
        payload["status"] = "deferred"
        payload["rejection_reason"] = reason
        out.append(ImprovementCandidate.parse(payload, "deferred"))
    return tuple(out)


_SERVICE: Optional[CompletionEngineService] = None


def service() -> CompletionEngineService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = CompletionEngineService()
    return _SERVICE


def reset_service() -> None:
    global _SERVICE
    _SERVICE = None
