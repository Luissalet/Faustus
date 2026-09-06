"""The Delta Engine's façade: compile an intent, compare two revisions, store.

Everything a route or another subsystem needs is a method here, and every one
of them takes `owner` as a keyword and refuses to guess it. What this module
adds on top of the packages under it is the ORDER, which is where the honesty
lives:

    freeze the intent  ->  resolve both revisions  ->  extract  ->  check
    invariants  ->  classify against the frozen intent  ->  assess  ->  store

The intent is frozen and stored BEFORE either revision is read. That is §1.9.1
and it is not a formality: an intent compiled after the extraction can be
compiled to fit it, and every evaluation built that way scores full marks. The
sequence in `_run` is the enforcement, and `test_delta_engine_wiring.py` reads
it back out of this file.

Three things this service deliberately does not do:

* It does not fix anything. §31: "un Delta nunca amplía permisos ni ejecuta
  reparaciones". There is no method here that writes to a workspace.
* It does not decide that a run finished. `prove` keeps that, and
  `integrations/prove.py` only ever lowers an assessment.
* It does not resolve a conflict between two adapters about the same path. Two
  findings on one path become two assertions, and the reader sees both.

The switch, `agent_delta_engine`, gates COMPARING and not READING. A delta
already stored was a conclusion recorded honestly, and turning the engine off
is a decision about what the machine may spend, never an instruction to hide
what was concluded. `enabled()` is read live inside each method that costs
something, never captured at import.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.delta_engine import classification, coverage as coverage_mod, intent as intent_mod
from src.delta_engine import invariants as invariants_mod
from src.delta_engine import persistence, registry, sources, verdict as verdict_mod
from src.delta_engine.adapters.base import Extraction, Scope, Snapshot, unreadable
from src.delta_engine.contracts import (
    ASSESSMENTS,
    CLASSIFICATIONS,
    CONDITION_KINDS,
    CONFIDENCE,
    COVERAGE_DIMENSIONS,
    DOMAINS,
    EVIDENCE_KINDS,
    EXTRACTION_TIERS,
    INVARIANT_CLASSES,
    INVARIANT_STATUSES,
    OPERATIONS,
    SEVERITIES,
    Budget,
    DeltaError,
    DeltaRequest,
    IntentContract,
    Privacy,
    RevisionRef,
    UniversalDelta,
    new_id,
)
from src.delta_engine.events import DELTA_EVENTS, stream_for

logger = logging.getLogger(__name__)

__all__ = [
    "SETTING",
    "ERRORS",
    "DeltaServiceError",
    "DeltaEngineService",
    "enabled",
    "service",
    "reset_service",
]

SETTING = "agent_delta_engine"

#: The stable tokens this module puts in a refusal's `code`. Short on purpose:
#: a route's caller switches on these, and a vocabulary that grows with every
#: message is a vocabulary nobody switches on.
ERRORS: Tuple[str, ...] = (
    "not_found", "disabled", "invalid_argument", "unknown_domain",
    "adapter_unavailable", "source_unreadable", "revision_moved",
    "intent_required", "store_failed",
)


class DeltaServiceError(DeltaError):
    """A refusal from the service, carrying one of `ERRORS` as its code."""

    def __init__(self, path: str, message: str, *, code: str = "invalid_argument",
                 got: Any = None) -> None:
        super().__init__(path, message, got=got)
        self.code = code if code in ERRORS else "invalid_argument"


def enabled() -> bool:
    """Whether comparisons may RUN. Read live, never captured at import.

    Imported inside the function for the reason every other gate in this
    repository does it: `src.settings` reads a file, and a module-level import
    would make the setting's value a property of when this module was first
    loaded rather than of what the operator chose.
    """
    try:
        from src import settings as settings_mod

        return bool(settings_mod.get_setting(SETTING, False))
    except Exception as exc:  # noqa: BLE001 - a missing setting is "off", not a crash
        logger.debug("delta engine: setting unreadable, treating as off: %s", exc)
        return False


def _limit(name: str, default: int) -> int:
    """One of the two size ceilings, read live like the switch above."""
    try:
        from src.settings import get_setting

        return max(1, int(get_setting(name, default) or default))
    except Exception:  # noqa: BLE001 - a bad setting is the default, not a crash
        return default


class DeltaEngineService:
    """One instance per process. Holds a store and a publisher, nothing else.

    No cache of adapters, no cache of settings, no cache of deltas: the store
    is the cache (§22), the registry answers `available()` live, and a service
    that remembered any of the three would answer yesterday's question after
    an operator changed something today.
    """

    def __init__(self, *, store: Any = None, publisher: Any = None,
                 registry_module: Any = None) -> None:
        self._store = store
        self._publisher = publisher
        self._registry = registry_module or registry

    # -- plumbing ---------------------------------------------------------

    def store(self) -> Any:
        return self._store if self._store is not None else persistence.store()

    def _stream(self, owner: str) -> Any:
        """The publisher this service was given, or the owner's own stream.

        An injected publisher wins so that a test -- or a caller that owns the
        page -- sees exactly what was emitted. The council learned this one the
        hard way: a ledger that published to the module registry instead of the
        stream it was handed recorded everything perfectly and showed nothing.
        """
        if self._publisher is not None:
            return self._publisher
        return stream_for(owner)

    def _emit(self, owner: str, name: str, **payload: Any) -> None:
        """Publish one event, and never let a publisher failure lose a delta.

        A comparison that succeeded and could not be announced is still a
        comparison that succeeded. The inverse -- raising here -- would mean a
        page that went away takes the result with it.
        """
        if name not in DELTA_EVENTS:
            logger.warning("delta engine: %r is not a declared event name", name)
        try:
            stream = self._stream(owner)
            publish = getattr(stream, "publish", None)
            if callable(publish):
                publish(name, owner=owner, **payload)
        except Exception as exc:  # noqa: BLE001 - the page is not the record
            logger.warning("delta engine: publishing %s failed: %s", name, exc)

    def _scope(self, request: DeltaRequest, *, workspace: str = "") -> Scope:
        return Scope(
            owner=request.owner,
            project_id=request.project_id,
            workspace=workspace,
            budget=request.budget,
            privacy=request.privacy,
            correlation_id=request.correlation_id,
            run_id=request.run_id,
        )

    @staticmethod
    def _require_owner(owner: Any, path: str = "owner") -> str:
        value = str(owner or "").strip()
        if not value:
            raise DeltaServiceError(
                path,
                "is empty; every read here is scoped to a person and an "
                "unscoped read would cross owners silently",
                code="invalid_argument", got=owner,
            )
        return value

    # -- intent -----------------------------------------------------------

    def compile_intent(self, *, owner: str, domain: str, text: str = "",
                       requested: Sequence[Mapping[str, Any]] = (),
                       invariants: Sequence[Mapping[str, Any]] = (),
                       scope: Optional[Mapping[str, Any]] = None,
                       tolerances: Optional[Mapping[str, Any]] = None,
                       acceptance: Sequence[str] = (),
                       evaluation_profile: str = "default",
                       project_id: str = "", supersedes: str = "",
                       persist: bool = True) -> Dict[str, Any]:
        """Freeze a request into an `IntentContract` and store it.

        Gated by the switch even though it reads nothing: a frozen contract is
        the first half of a comparison, and letting one be minted while the
        engine is off would leave contracts in the store that no delta ever
        answers. Reading contracts back stays open.
        """
        owner = self._require_owner(owner)
        if not enabled():
            raise DeltaServiceError(
                "settings.agent_delta_engine",
                "is off, so no new intent is frozen; deltas already stored "
                "still read normally",
                code="disabled",
            )
        contract = intent_mod.compile(
            owner=owner, domain=domain, text=text, requested=requested,
            invariants=invariants, scope=scope, tolerances=tolerances,
            acceptance=acceptance, evaluation_profile=evaluation_profile,
            project_id=project_id, supersedes=supersedes,
        )
        if persist:
            self.store().save_intent(contract)
        self._emit(owner, "delta_intent_compiled",
                   intent_contract_id=contract.id, domain=contract.domain,
                   project_id=contract.project_id,
                   unknowns=len(contract.unknowns))
        return {
            "ok": True,
            "intent": contract.to_dict(),
            "fingerprint": contract.fingerprint(),
            "unknowns": list(contract.unknowns),
            "rendered": intent_mod.render(contract),
        }

    # -- comparing --------------------------------------------------------

    def create(self, payload: Mapping[str, Any], *, owner: str,
               workspace: str = "", run: bool = True) -> Dict[str, Any]:
        """Accept a comparison, freeze its intent, and (by default) run it.

        The order in this method is the guarantee. `_intent_for` is called and
        the contract stored BEFORE `_run` reads a single byte of either
        revision -- §1.9.1, and the reason the plan puts it first: a contract
        compiled after the result can be compiled to match it.
        """
        owner = self._require_owner(owner)
        # The switch is read HERE, before anything is frozen or stored, and not
        # inside `_run`. With the check further down, an engine that was off
        # still minted a contract and a request row for a comparison that then
        # refused to run -- and `create(run=False)` succeeded outright, because
        # on that path nothing read the switch at all. `compile_intent` states
        # the rule this restores: a frozen contract nobody will ever answer is
        # litter in the store, and the store is the audit trail.
        if not enabled():
            raise DeltaServiceError(
                "settings.agent_delta_engine",
                "is off, so no comparison is created; deltas already stored "
                "still read normally",
                code="disabled",
            )
        body = dict(payload or {})
        body.pop("workspace", None)
        body.pop("run", None)
        # The owner comes from the session and a payload that names one is
        # ignored rather than honoured -- the same rule the State Mirror's
        # routes state, and the reason an id from another owner answers 404.
        ignored = [key for key in ("owner",) if key in body]
        body["owner"] = owner
        request = DeltaRequest.parse(body, "delta_request")
        contract = self._intent_for(request)
        self.store().save_request(request, intent_id=contract.id)
        self._emit(owner, "delta_requested", request_id=request.id,
                   intent_contract_id=contract.id, domain=request.domain,
                   project_id=request.project_id,
                   source=request.source.identity(),
                   target=request.target.identity())
        out: Dict[str, Any] = {
            "ok": True,
            "request_id": request.id,
            "intent_contract_id": contract.id,
            "ignored_fields": ignored,
        }
        if run:
            result = self._run(request, contract, workspace=workspace)
            out.update(result)
        return out

    def run(self, request_id: str, *, owner: str, workspace: str = "") -> Dict[str, Any]:
        """Run a comparison that was created earlier."""
        owner = self._require_owner(owner)
        # Same reason as `create`: `_intent_for` can freeze a contract, so the
        # switch is read before it and not only inside `_run`.
        if not enabled():
            raise DeltaServiceError(
                "settings.agent_delta_engine",
                "is off, so no comparison runs; deltas already stored still "
                "read normally",
                code="disabled",
            )
        request = self.store().get_request(str(request_id or ""), owner=owner)
        if request is None:
            raise DeltaServiceError("request_id", "names no request of yours",
                                    code="not_found", got=request_id)
        contract = self._intent_for(request)
        return self._run(request, contract, workspace=workspace)

    def _intent_for(self, request: DeltaRequest) -> IntentContract:
        """The frozen contract this request is judged against.

        Three ways in, and they are checked in this order because only the
        first is a contract someone already committed to:

        1. `intent_contract_id` -- reuse the frozen one. It is read back from
           the store rather than trusted from the payload, so a caller cannot
           hand over a contract nobody froze.
        2. raw `requested` / `invariants` / `intent_text` -- compile and freeze
           now, before anything is read.
        3. nothing at all -- compile an EMPTY contract with the domain's
           default invariants. This is not a failure: "compare these two and
           tell me what changed" is a real question, it just cannot produce
           `matched`, because nothing was requested for the result to match.
        """
        store = self.store()
        if request.intent_contract_id:
            contract = store.get_intent(request.intent_contract_id, owner=request.owner)
            if contract is None:
                raise DeltaServiceError(
                    "intent_contract_id", "names no frozen intent of yours",
                    code="not_found", got=request.intent_contract_id,
                )
            if contract.domain != request.domain:
                raise DeltaServiceError(
                    "intent_contract_id",
                    f"was frozen for domain `{contract.domain}` and this "
                    f"request is `{request.domain}`; an intent about one kind "
                    f"of thing cannot judge another",
                    code="invalid_argument", got=request.intent_contract_id,
                )
            return contract
        contract = intent_mod.compile(
            owner=request.owner,
            domain=request.domain,
            text=request.intent_text,
            requested=[r.to_dict() for r in request.requested],
            invariants=[i.to_dict() for i in request.invariants],
            evaluation_profile=request.evaluation_profile,
            project_id=request.project_id,
        )
        store.save_intent(contract)
        self._emit(request.owner, "delta_intent_compiled",
                   intent_contract_id=contract.id, request_id=request.id,
                   domain=contract.domain, unknowns=len(contract.unknowns))
        return contract

    def _run(self, request: DeltaRequest, contract: IntentContract, *,
             workspace: str = "") -> Dict[str, Any]:
        """The comparison itself. The order of these steps is the guarantee."""
        if not enabled():
            raise DeltaServiceError(
                "settings.agent_delta_engine",
                "is off, so no comparison runs; deltas already stored still "
                "read normally",
                code="disabled",
            )
        owner = request.owner
        adapter = self._adapter(request.domain)
        cached = self._cached(request, contract, adapter)
        if cached is not None:
            return {"ok": True, "delta": cached.to_dict(), "cached": True,
                    "explanation": self._explain(cached)}

        scope = self._scope(request, workspace=workspace)
        started = time.monotonic()

        source = self._snapshot(adapter, request.source, scope=scope, side="source")
        target = self._snapshot(adapter, request.target, scope=scope, side="target")
        self._emit(owner, "delta_source_resolved", request_id=request.id,
                   domain=request.domain,
                   source_readable=source.readable, target_readable=target.readable)

        if not (source.readable and target.readable):
            # An end we could not read is an ANSWER, not an exception: it makes
            # the comparison `inconclusive` and it names which end failed. The
            # delta is stored, because "we tried and could not see the source"
            # is exactly the record a caller needs before acting.
            delta = self._build(request, contract, adapter,
                                extraction=Extraction(
                                    coverage=coverage_mod.build(
                                        source_readable=source.readable,
                                        target_readable=target.readable,
                                        notes=tuple(source.notes) + tuple(target.notes),
                                    ),
                                ),
                                classified=classification.Classified(),
                                invariant_results=(),
                                elapsed_ms=int((time.monotonic() - started) * 1000))
            self._store_and_announce(delta, owner=owner)
            return {"ok": True, "delta": delta.to_dict(), "cached": False,
                    "explanation": self._explain(delta)}

        extraction = adapter.compare(source, target, scope=scope)
        self._emit(owner, "delta_extraction_completed", request_id=request.id,
                   domain=request.domain, findings=len(extraction.findings))

        invariant_results = tuple(extraction.invariants) + tuple(
            adapter.check_invariants(contract, source, target, scope=scope)
        )
        for result in invariant_results:
            self._emit(owner, "delta_invariant_checked", request_id=request.id,
                       invariant_id=result.invariant_id, status=result.status,
                       severity=result.severity, confidence=result.confidence)

        classified = classification.classify_all(
            extraction.findings, intent=contract, invariant_results=invariant_results,
        )
        self._emit(owner, "delta_coverage_computed", request_id=request.id,
                   dimensions=dict(extraction.coverage.dimensions),
                   source_readable=extraction.coverage.source_readable,
                   target_readable=extraction.coverage.target_readable)

        delta = self._build(request, contract, adapter, extraction=extraction,
                            classified=classified,
                            invariant_results=invariant_results,
                            elapsed_ms=int((time.monotonic() - started) * 1000))
        delta = self._with_proof(delta, workspace=workspace)
        self._store_and_announce(delta, owner=owner)
        return {"ok": True, "delta": delta.to_dict(), "cached": False,
                "explanation": self._explain(delta)}

    def _adapter(self, domain: str) -> Any:
        if str(domain or "") not in DOMAINS:
            raise DeltaServiceError("domain", f"is not one of {list(DOMAINS)}",
                                    code="unknown_domain", got=domain)
        try:
            return self._registry.adapter_for(domain)
        except DeltaError as exc:
            # Deliberately not falling back to the binary adapter. It would
            # answer `reencoded` about a Python file and be believed, and a
            # confident wrong answer is worse here than no answer.
            raise DeltaServiceError("domain", str(getattr(exc, "message", exc)),
                                    code="adapter_unavailable", got=domain) from exc

    def _snapshot(self, adapter: Any, revision: RevisionRef, *, scope: Scope,
                  side: str) -> Snapshot:
        """One end, or an unreadable snapshot naming why. Never raises upward.

        An adapter that throws on a revision it cannot read would lose the work
        already done on the other end and tell the caller nothing about which
        side failed, so the exception is turned into the answer it actually is.
        """
        try:
            return adapter.snapshot(revision, scope=scope)
        except Exception as exc:  # noqa: BLE001 - the failure is the finding
            logger.warning("delta engine: %s snapshot failed for %s: %s",
                           side, revision.identity(), exc)
            return unreadable(revision, f"{side}: {exc}")

    def _versions(self, adapter: Any, extraction: Optional[Extraction] = None
                  ) -> Dict[str, str]:
        versions: Dict[str, str] = {str(adapter.domain): str(adapter.version)}
        if extraction is not None:
            for name, value in dict(extraction.extractor_versions).items():
                versions[str(name)] = str(value)
        return versions

    def _cached(self, request: DeltaRequest, contract: IntentContract,
                adapter: Any) -> Optional[UniversalDelta]:
        """The stored answer to this exact question, when it is still valid.

        The fingerprint identifies the QUESTION (source, target, intent,
        domain), so a hit here can still be an answer produced by an older
        parser. When it is, the old delta is superseded WITH A REASON and the
        comparison runs again -- which is the whole point of keeping the
        extractor versions out of the key: an invalidation you can see beats a
        cache miss you cannot.
        """
        probe = UniversalDelta(
            id="probe", request_id=request.id, owner=request.owner,
            domain=request.domain, source=request.source, target=request.target,
            assessment="inconclusive", intent_fingerprint=contract.fingerprint(),
        )
        try:
            found = self.store().find_delta(probe.fingerprint(), owner=request.owner)
        except Exception as exc:  # noqa: BLE001 - a cache that fails is a cache miss
            logger.warning("delta engine: cache probe failed: %s", exc)
            return None
        if found is None:
            return None
        current = self._versions(adapter)
        stored = {str(k): str(v) for k, v in dict(found.extractor_versions).items()}
        stale = [name for name, version in current.items()
                 if stored.get(name, version) != version]
        if stale:
            reason = ("extractor version changed for " + ", ".join(sorted(stale)))
            try:
                self.store().supersede(found.id, owner=request.owner, reason=reason)
            except Exception as exc:  # noqa: BLE001
                logger.warning("delta engine: supersede failed for %s: %s", found.id, exc)
            self._emit(request.owner, "delta_invalidated", delta_id=found.id,
                       reason=reason, domain=request.domain)
            return None
        return found

    def _build(self, request: DeltaRequest, contract: IntentContract, adapter: Any,
               *, extraction: Extraction,
               classified: classification.Classified,
               invariant_results: Sequence[Any],
               elapsed_ms: int) -> UniversalDelta:
        assessment = verdict_mod.assess(
            assertions=classified.assertions,
            invariants=invariant_results,
            coverage=extraction.coverage,
            intent=contract,
            unmet=classified.unmet,
        )
        limitations = list(extraction.limitations)
        for path in classified.out_of_scope:
            note = f"changed outside the declared scope: {path}"
            if note not in limitations:
                limitations.append(note)
        for path in classified.unmet:
            note = f"requested change not observed: {path}"
            if note not in limitations:
                limitations.append(note)
        payload = {
            "id": new_id("delta"),
            "request_id": request.id,
            "owner": request.owner,
            "domain": request.domain,
            "source": request.source.to_dict(),
            "target": request.target.to_dict(),
            "assessment": assessment,
            "intent_contract_id": contract.id,
            "intent_fingerprint": contract.fingerprint(),
            "assertions": [a.to_dict() for a in classified.assertions],
            "invariants": [r.to_dict() for r in invariant_results],
            "coverage": extraction.coverage.to_dict(),
            "evidence_refs": [e.to_dict() for e in extraction.evidence_refs],
            "extractor_versions": self._versions(adapter, extraction),
            "limitations": limitations,
            "project_id": request.project_id,
            "session_id": request.session_id,
            "run_id": request.run_id,
            "correlation_id": request.correlation_id,
            "elapsed_ms": max(0, int(elapsed_ms)),
        }
        return UniversalDelta.parse(payload, "delta")

    def _with_proof(self, delta: UniversalDelta, *, workspace: str = "") -> UniversalDelta:
        """Let `prove` lower the assessment, never raise it.

        Only for domains whose findings a ChangeSet can hold. For everything
        else there is no run-level evidence to judge and the delta stands on
        its own -- which is not a weakness: most comparisons in this system are
        not about a run at all.
        """
        if delta.domain != "code":
            return delta
        try:
            from src.delta_engine.integrations import changesets as changesets_int
            from src.delta_engine.integrations import prove as prove_int

            proof = changesets_int.judge(delta, workspace=workspace)
            if not proof:
                return delta
            return prove_int.attach(delta, proof)
        except Exception as exc:  # noqa: BLE001 - a proof that fails is not a delta that fails
            logger.warning("delta engine: proof step failed for %s: %s", delta.id, exc)
            return delta

    def _store_and_announce(self, delta: UniversalDelta, *, owner: str) -> None:
        try:
            self.store().save_delta(delta)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed silently
            logger.error("delta engine: storing %s failed: %s", delta.id, exc)
            self._emit(owner, "delta_error", delta_id=delta.id, detail=str(exc))
            raise DeltaServiceError("delta", f"could not be stored: {exc}",
                                    code="store_failed", got=delta.id) from exc
        for assertion in delta.material_assertions():
            self._emit(owner, "delta_assertion_created", delta_id=delta.id,
                       assertion_id=assertion.id, path=assertion.path,
                       classification=assertion.classification,
                       severity=assertion.severity, confidence=assertion.confidence)
        for assertion in delta.regressions():
            self._emit(owner, "delta_regression_detected", delta_id=delta.id,
                       assertion_id=assertion.id, path=assertion.path,
                       severity=assertion.severity)
        for result in delta.violated():
            self._emit(owner, "delta_regression_detected", delta_id=delta.id,
                       invariant_id=result.invariant_id, severity=result.severity)
        # `inconclusive` gets its own event rather than a flag on `completed`:
        # a consumer waiting for a usable answer should not be woken by a
        # comparison that could not see enough to give one.
        name = "delta_inconclusive" if delta.assessment == "inconclusive" else "delta_completed"
        self._emit(owner, name, delta_id=delta.id, domain=delta.domain,
                   assessment=delta.assessment, project_id=delta.project_id,
                   elapsed_ms=delta.elapsed_ms, run_id=delta.run_id,
                   correlation_id=delta.correlation_id)

    @staticmethod
    def _explain(delta: UniversalDelta) -> str:
        try:
            return verdict_mod.explain(
                delta.assessment, assertions=delta.assertions,
                invariants=delta.invariants, coverage=delta.coverage,
            )
        except Exception as exc:  # noqa: BLE001 - prose is never load-bearing
            logger.debug("delta engine: explain failed for %s: %s", delta.id, exc)
            return ""

    # -- reading (never gated by the switch) --------------------------------

    def get(self, delta_id: str, *, owner: str) -> Dict[str, Any]:
        owner = self._require_owner(owner)
        delta = self.store().get_delta(str(delta_id or ""), owner=owner)
        if delta is None:
            # 404 and not 403 for another owner's id: telling someone that an
            # id exists but is not theirs is itself a disclosure.
            raise DeltaServiceError("delta_id", "names no delta of yours",
                                    code="not_found", got=delta_id)
        return {
            "ok": True,
            "delta": delta.to_dict(),
            "explanation": self._explain(delta),
            "reclassifications": self.store().reclassifications(delta.id, owner=owner),
            "ref": delta.ref().to_dict(),
        }

    def list(self, *, owner: str, domain: str = "", project_id: str = "",
             assessment: str = "", limit: int = 50, cursor: str = "") -> Dict[str, Any]:
        owner = self._require_owner(owner)
        if assessment and assessment not in ASSESSMENTS:
            raise DeltaServiceError("assessment", f"is not one of {list(ASSESSMENTS)}",
                                    code="invalid_argument", got=assessment)
        if domain and domain not in DOMAINS:
            raise DeltaServiceError("domain", f"is not one of {list(DOMAINS)}",
                                    code="unknown_domain", got=domain)
        limit = max(1, min(200, int(limit or 50)))
        rows = self.store().list_deltas(owner=owner, domain=domain,
                                        project_id=project_id,
                                        assessment=assessment, limit=limit,
                                        cursor=cursor)
        next_cursor = ""
        if len(rows) >= limit and rows:
            page_cursor = getattr(self.store(), "page_cursor", None)
            if callable(page_cursor):
                next_cursor = page_cursor(rows[-1])
        return {
            "ok": True,
            "enabled": enabled(),
            "deltas": [self._summary(row) for row in rows],
            "next_cursor": next_cursor,
        }

    @staticmethod
    def _summary(delta: UniversalDelta) -> Dict[str, Any]:
        """The card. Counts and words, never the assertions themselves.

        `unknowns` is on the card next to `regressions` on purpose: a list
        where you can see what went wrong but have to open each row to find
        what was never checked teaches its reader that unchecked means fine.
        """
        summary = delta.summary()
        classifications = summary.get("classifications") or {}
        return {
            "id": delta.id,
            "domain": delta.domain,
            "assessment": delta.assessment,
            "created_at": delta.created_at,
            "project_id": delta.project_id,
            "intent_contract_id": delta.intent_contract_id,
            "source_label": delta.source.label or delta.source.ref,
            "target_label": delta.target.label or delta.target.ref,
            "source_hash": delta.source.hash,
            "target_hash": delta.target.hash,
            "counts": {
                "assertions": summary.get("assertions", 0),
                "material": summary.get("material", 0),
                "regressions": int(classifications.get("regression", 0)),
                "unknowns": int(classifications.get("unknown", 0)),
                "incidental": int(classifications.get("incidental", 0)),
                "requested": int(classifications.get("requested", 0)),
                "preserved": int(classifications.get("preserved", 0)),
                "invariants_violated": len(delta.violated()),
            },
            "severity": summary.get("severity", "info"),
            "confidence": summary.get("confidence", "unknown"),
            "elapsed_ms": delta.elapsed_ms,
            "proof_ref": delta.proof_ref,
        }

    def evidence(self, delta_id: str, *, owner: str) -> Dict[str, Any]:
        owner = self._require_owner(owner)
        delta = self.store().get_delta(str(delta_id or ""), owner=owner)
        if delta is None:
            raise DeltaServiceError("delta_id", "names no delta of yours",
                                    code="not_found", got=delta_id)
        rows: List[Dict[str, Any]] = []
        for ref in delta.evidence_refs:
            rows.append({**ref.to_dict(), "assertion_id": "", "invariant_id": ""})
        for assertion in delta.assertions:
            for ref in assertion.evidence_refs:
                rows.append({**ref.to_dict(), "assertion_id": assertion.id,
                             "invariant_id": ""})
        for result in delta.invariants:
            for ref in result.evidence_refs:
                rows.append({**ref.to_dict(), "assertion_id": "",
                             "invariant_id": result.invariant_id})
        return {"ok": True, "evidence": rows}

    # -- interpretation ----------------------------------------------------

    def reclassify(self, delta_id: str, assertion_id: str, *, owner: str,
                   classification_name: str, actor: str,
                   reason: str) -> Dict[str, Any]:
        """Change what one observation MEANS. Never what it was.

        §23: "reclassify no cambia observaciones; crea nueva revisión de
        interpretación con autor/motivo". The store enforces that; this method
        exists to check the vocabulary and to insist on a reason, because a
        reinterpretation nobody justified is indistinguishable from a mistake
        six months later.
        """
        owner = self._require_owner(owner)
        if classification_name not in CLASSIFICATIONS:
            raise DeltaServiceError("classification",
                                    f"is not one of {list(CLASSIFICATIONS)}",
                                    code="invalid_argument", got=classification_name)
        if not str(reason or "").strip():
            raise DeltaServiceError(
                "reason",
                "is empty; a reinterpretation without a stated reason cannot "
                "be told from a mistake once everyone has forgotten",
                code="invalid_argument", got=reason,
            )
        updated = self.store().reclassify(
            str(delta_id or ""), str(assertion_id or ""), owner=owner,
            classification=classification_name, actor=str(actor or ""),
            reason=str(reason),
        )
        if updated is None:
            raise DeltaServiceError("delta_id", "names no delta of yours, or no "
                                                "such assertion in it",
                                    code="not_found", got=delta_id)
        self._emit(owner, "delta_reclassified", delta_id=updated.id,
                   assertion_id=assertion_id, classification=classification_name,
                   actor=actor, reason=reason)
        return {"ok": True, "delta": updated.to_dict(),
                "explanation": self._explain(updated)}

    def invalidate(self, delta_id: str, *, owner: str, reason: str = "") -> Dict[str, Any]:
        owner = self._require_owner(owner)
        done = bool(self.store().supersede(str(delta_id or ""), owner=owner,
                                           reason=str(reason or "")))
        if not done:
            raise DeltaServiceError("delta_id", "names no live delta of yours",
                                    code="not_found", got=delta_id)
        self._emit(owner, "delta_invalidated", delta_id=str(delta_id), reason=reason)
        return {"ok": True, "superseded": True}

    def invalidate_revision(self, *, owner: str, revision_hash: str) -> Dict[str, Any]:
        """Retire every delta that compared a revision that turned out to move.

        §3.6's other half. Nothing calls this on a timer: it is for the caller
        who discovers that what it handed over as immutable was not.
        """
        owner = self._require_owner(owner)
        count = int(self.store().invalidate_for_revision(str(revision_hash or ""),
                                                         owner=owner))
        if count:
            self._emit(owner, "delta_invalidated", reason="revision moved",
                       detail=str(revision_hash), count=count)
        return {"ok": True, "superseded": count}

    # -- what the engine can do right now ----------------------------------

    def config(self) -> Dict[str, Any]:
        """Every closed vocabulary, so the page never hard-codes one.

        The State Mirror's screen taught this: a front end with its own copy of
        a vocabulary drifts from the server on the day someone adds a word, and
        the symptom is a row that renders blank rather than an error anyone
        notices.
        """
        return {
            "ok": True,
            "enabled": enabled(),
            "domains": list(DOMAINS),
            "assessments": list(ASSESSMENTS),
            "classifications": list(CLASSIFICATIONS),
            "operations": list(OPERATIONS),
            "severities": list(SEVERITIES),
            "confidence": list(CONFIDENCE),
            "tiers": list(EXTRACTION_TIERS),
            "invariant_classes": list(INVARIANT_CLASSES),
            "invariant_statuses": list(INVARIANT_STATUSES),
            "coverage_dimensions": list(COVERAGE_DIMENSIONS),
            "evidence_kinds": list(EVIDENCE_KINDS),
            "condition_kinds": list(CONDITION_KINDS),
            "events": list(DELTA_EVENTS),
            "errors": list(ERRORS),
        }

    def extractors(self) -> Dict[str, Any]:
        """Which domains can actually be compared on this machine, and why not.

        Answered live from the registry. A domain that says `available: false`
        with a reason is worth more than a page that lists nine domains and
        fails on the fourth.
        """
        try:
            status = self._registry.status()
        except Exception as exc:  # noqa: BLE001 - a broken registry is a report, not a 500
            logger.warning("delta engine: registry status failed: %s", exc)
            status = {}
        return {"ok": True, "enabled": enabled(), "extractors": status}

    def profiles(self) -> Dict[str, Any]:
        """The evaluation profiles and what each one widens.

        Everything here is about COVERAGE and budget. No profile removes a
        security or permission invariant -- `invariants.MANDATORY_CLASSES` is
        what enforces that, and this list only describes it.
        """
        return {
            "ok": True,
            "profiles": [
                {"id": "literal", "title": "Literal",
                 "detail": "Only what was named, plus the mandatory security "
                           "and permission invariants."},
                {"id": "default", "title": "Professional",
                 "detail": "Adds the domain's standard invariants and the "
                           "regressions closest to what changed."},
                {"id": "greedy", "title": "Greedy",
                 "detail": "Also looks downstream of the change, inside the "
                           "declared scope."},
                {"id": "maximalist", "title": "Maximalist",
                 "detail": "Widest coverage and every extractor the budget "
                           "allows."},
            ],
            "mandatory_classes": list(invariants_mod.MANDATORY_CLASSES),
        }

    def diagnostics(self, *, owner: str) -> Dict[str, Any]:
        owner = self._require_owner(owner)
        store = self.store()
        try:
            counts = store.counts(owner=owner)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delta engine: counts failed: %s", exc)
            counts = {}
        try:
            path = store.path()
        except Exception:  # noqa: BLE001
            path = ""
        return {
            "ok": True,
            "enabled": enabled(),
            "counts": counts,
            "db_path": path,
            "schemas": list(persistence.registered_schemas()),
            "extractors": self.extractors().get("extractors", {}),
            "max_bytes": _limit("agent_delta_engine_max_bytes", 8_000_000),
            "max_elements": _limit("agent_delta_engine_max_elements", 5000),
            "stash": sources.stash_size(),
        }

    def events(self, *, owner: str) -> Any:
        """The owner's live stream. Reading it is never gated by the switch."""
        return self._stream(self._require_owner(owner))

    def budget_from_settings(self) -> Budget:
        """The default budget, from the two operator-facing ceilings."""
        return Budget(
            max_bytes=_limit("agent_delta_engine_max_bytes", 8_000_000),
            max_elements=_limit("agent_delta_engine_max_elements", 5000),
        )


_SERVICE: Optional[DeltaEngineService] = None


def service() -> DeltaEngineService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = DeltaEngineService()
    return _SERVICE


def reset_service() -> None:
    """Drop the memoized service. For tests and for `use_path`."""
    global _SERVICE
    _SERVICE = None
