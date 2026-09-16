"""preflight.py — WP09: one preflight for any Creator operation.

Every Creator operation — a document command that produces media, a
production step, a render — goes through the SAME question before anything
happens: what is missing, what would it cost, does it fit, and does a human
have to say yes first? This module answers that question and nothing else.

**Pure, no side effects.** `run_preflight()` never generates, downloads,
reserves budget, opens a lease, or writes an approval — it reads the stores
it needs (artifact occurrences, capabilities, the project's budget ledger)
and computes a report. "Preflight no genera ni descarga" is a closure
criterion, not a suggestion: nothing in this module calls a media backend,
`resource_admission.acquire()`, `budget_account.reserve()`, or
`approval_store.request()`. The one place an approval is actually created is
`routes/creator_preflight_routes.py`'s `/preflight/{digest}/approve`, and
even that recomputes this same pure function first so the approval is bound
to a plan this module just computed, not to one the caller merely claims.

**Reuses, never re-invents:**

* ``src.artifact_identity.for_owner`` — every input id is an occurrence,
  checked to exist AND belong to the caller the same way every other Creator
  route does (CONTRATO.md rule 3: "no es tuyo" y "no existe" responden
  igual).
* ``src.media_consent`` — the existing consent registry (MEDIA-06), not a
  second one.
* ``src.contracts.approval.ApprovalPlan.fingerprint()`` — the exact canonical
  digest ``src/approval_store.py`` already keys ``covers()``/``check()`` on
  (SHA-256 over length-prefixed, order-free `(name, value)` pairs,
  ``src/contracts/base.py::fingerprint``). A preflight digest and a granted
  approval's ``plan_fingerprint`` are the same number computed the same way,
  so "does a granted card still cover this plan" is answered by the approval
  store's own ``covers()``/``check()`` — this module does not reimplement
  that comparison.
* ``src.budget_account`` (via ``src.creator.budget_policy``) — the existing
  per-run token/cost ledger. Reservation and reconciliation are real writes
  there, made by the *execution* path (another WP), never by this one.

**WP07 interface (parallel lot).** This lot ships alongside WP07
(``docs/spec/creator/plan/CONTRATO.md``, Sub-ola C2), which owns
``src.creator.capabilities.capabilities_for(deployment_id)`` and
``src.creator.params.validate(engine, task, params)``. Both are called only
through the two module-level seams below — ``_capabilities_for`` and
``_validate_params`` — so a test can ``monkeypatch.setattr`` either one
directly without needing the real WP07 module to exist yet. If WP07 has
landed by the time this runs, the seam just forwards to it unchanged.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "MISSING_KINDS", "Estimate", "Admission", "BudgetOutlook", "PreflightReport",
    "run_preflight", "build_approval_plan",
]

#: The four things `run_preflight` can report as missing, per WP09's ficha:
#: an input occurrence that does not exist/is not ours, a consent record that
#: was never given, a capability the deployment does not have, or a param
#: that failed validation.
MISSING_KINDS = ("input", "consent", "capability", "param")

_KNOWN_CLOUD_ENGINE_PREFIXES = (
    "openai", "anthropic", "openrouter", "cloud", "azure", "bedrock",
    "gemini", "google", "elevenlabs", "runway", "luma", "kling", "fal",
    "replicate", "stability",
)


# ── report shapes ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Estimate:
    """Resources and cost, with an honest ``"unknown"`` instead of a guessed
    zero wherever a number cannot actually be traced to a real signal."""

    tokens: Optional[int] = None
    seconds: Optional[float] = None
    vram_bytes: Optional[int] = None
    #: A float, or the literal string ``"unknown"`` — NEVER a known ``0.0``
    #: standing in for "we don't know", the same discipline
    #: ``src.budget_account.snapshot`` already enforces for
    #: ``consumed_cost``.
    cost_usd: Any = "unknown"
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tokens": self.tokens, "seconds": self.seconds,
            "vram_bytes": self.vram_bytes, "cost_usd": self.cost_usd,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Admission:
    """Whether the estimated VRAM fits. ``fits=True`` with an "unknown"
    reason is the honest default when nothing here can actually measure
    headroom — never a false negative, and never a silent pass dressed up as
    a real check either (the reason always says which it was)."""

    fits: bool
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"fits": self.fits, "reason": self.reason}


@dataclass(frozen=True)
class BudgetOutlook:
    reservation_needed: Optional[int]
    ceiling: Optional[int]
    remaining: Optional[int]
    verdict: str = "allow"
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reservation_needed": self.reservation_needed,
            "ceiling": self.ceiling, "remaining": self.remaining,
            "verdict": self.verdict, "reason": self.reason,
        }


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    missing: Tuple[Dict[str, str], ...]
    estimate: Estimate
    admission: Admission
    requires_approval: bool
    #: Same value as the would-be approval's ``plan_fingerprint`` once
    #: granted — see module docstring. ``None`` unless `requires_approval`.
    approval_digest: Optional[str]
    #: The plan's own `to_dict()`, so a caller (the `/approve` route) can
    #: reconstruct and re-request the EXACT `ApprovalPlan` this report
    #: computed, rather than trusting a client-supplied copy.
    approval_plan: Optional[Dict[str, Any]]
    budget: BudgetOutlook
    operation: str
    engine: str
    deployment_id: str
    project_id: str
    created_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "missing": [dict(m) for m in self.missing],
            "estimate": self.estimate.to_dict(),
            "admission": self.admission.to_dict(),
            "requires_approval": self.requires_approval,
            "approval_digest": self.approval_digest,
            "approval_plan": self.approval_plan,
            "budget": self.budget.to_dict(),
            "operation": self.operation, "engine": self.engine,
            "deployment_id": self.deployment_id, "project_id": self.project_id,
            "created_at": self.created_at,
        }


# ── WP07 seam (monkeypatchable) ─────────────────────────────────────────────

def _capabilities_for(deployment_id: str) -> Any:
    """``src.creator.capabilities.capabilities_for`` (WP07). Tests that run
    before WP07 lands monkeypatch this function directly:
    ``monkeypatch.setattr(preflight, "_capabilities_for", fake)``."""
    from src.creator.capabilities import capabilities_for
    return capabilities_for(deployment_id)


def _validate_params(engine: str, task: str, params: Mapping[str, Any]) -> Any:
    """``src.creator.params.validate`` (WP07). Same seam as above — tests
    monkeypatch ``_validate_params`` directly rather than requiring the real
    module."""
    from src.creator.params import validate
    return validate(engine, task, params)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read `key` off `obj`, which may be a mapping, a dataclass, or `None` —
    the capability/param interface is not fixed yet (WP07 in parallel), so
    every read here is defensive by construction."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ── missing: inputs ──────────────────────────────────────────────────────

def _missing_inputs(owner: str, inputs: Sequence[str]) -> List[Dict[str, str]]:
    from src.artifact_identity import ArtifactNotFound, NotTheOwner, for_owner

    missing: List[Dict[str, str]] = []
    for occurrence_id in inputs:
        try:
            for_owner(occurrence_id, owner=owner)
        except (ArtifactNotFound, NotTheOwner):
            # CONTRATO.md rule 3: "not yours" and "does not exist" answer
            # identically — the caller learns nothing about who else's it is.
            missing.append({
                "kind": "input", "id": occurrence_id,
                "detail": "input occurrence not found",
            })
    return missing


# ── missing: capability ─────────────────────────────────────────────────

def _missing_capability(operation: str, engine: str, capabilities: Any,
                         lookup_error: Optional[Exception]) -> List[Dict[str, str]]:
    if lookup_error is not None:
        return [{
            "kind": "capability", "id": f"{engine}:{operation}",
            "detail": f"capability lookup failed: {lookup_error}",
        }]
    if capabilities is None:
        return [{
            "kind": "capability", "id": f"{engine}:{operation}",
            "detail": "no capability record for this deployment",
        }]
    operations = _get(capabilities, "operations")
    if operations is not None and operation not in operations:
        return [{
            "kind": "capability", "id": operation,
            "detail": f"deployment does not support operation {operation!r}",
        }]
    return []


# ── missing: consent ─────────────────────────────────────────────────────

def _consent_requirement(capabilities: Any, operation: str) -> Tuple[bool, str]:
    """Whether this operation clones a voice/likeness and, if so, which
    params key names the subject — read off whatever `capabilities_for`
    returns (a mapping or a dataclass), defensively: an unreadable/missing
    field means "no requirement stated", never a crash."""
    op_caps = _get(capabilities, "operations")
    op_entry = None
    if isinstance(op_caps, Mapping):
        op_entry = op_caps.get(operation)
    requires = bool(_get(op_entry, "requires_consent", _get(capabilities, "requires_consent", False)))
    subject_param = str(
        _get(op_entry, "consent_subject_param", _get(capabilities, "consent_subject_param", "")) or ""
    )
    return requires, subject_param


def _missing_consent(params: Mapping[str, Any], requires_consent: bool,
                      subject_param: str) -> List[Dict[str, str]]:
    if not requires_consent:
        return []
    subject = str((params or {}).get(subject_param) or "").strip()
    if not subject:
        return [{
            "kind": "consent", "id": subject_param or "subject",
            "detail": "this operation clones a voice or likeness; no consent subject given",
        }]
    from src import media_consent
    if media_consent.has_consent(subject):
        return []
    return [{
        "kind": "consent", "id": subject,
        "detail": f"no recorded consent for {subject!r} — register it with "
                   "src.media_consent.register() before this can render",
    }]


# ── missing: params ───────────────────────────────────────────────────────

def _missing_params(engine: str, operation: str, params: Mapping[str, Any]) -> List[Dict[str, str]]:
    try:
        result = _validate_params(engine, operation, params)
    except Exception as exc:  # noqa: BLE001 - a broken validator must still surface as "missing", not crash preflight
        return [{"kind": "param", "id": "*", "detail": f"param validation raised: {exc}"}]

    if result is None or result is True:
        return []
    if result is False:
        return [{"kind": "param", "id": "*", "detail": "params rejected"}]
    if isinstance(result, Mapping):
        if result.get("ok", not bool(result.get("errors"))):
            return []
        errors = result.get("errors") or []
        out: List[Dict[str, str]] = []
        for err in errors:
            if isinstance(err, Mapping):
                out.append({
                    "kind": "param",
                    "id": str(err.get("field") or err.get("param") or "*"),
                    "detail": str(err.get("detail") or err.get("message") or "invalid"),
                })
            else:
                out.append({"kind": "param", "id": "*", "detail": str(err)})
        return out
    # An unrecognised shape is not silently "fine" — it is at least one
    # unnamed param problem, never swallowed.
    return [{"kind": "param", "id": "*", "detail": f"unrecognised validation result: {result!r}"}]


# ── estimate ─────────────────────────────────────────────────────────────

def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _estimate_vram_bytes(params: Mapping[str, Any], capabilities: Any,
                          notes: List[str]) -> Optional[int]:
    hint = _as_int((params or {}).get("vram_bytes_hint"))
    if hint is not None:
        return hint

    weights_bytes = _as_int(_get(capabilities, "weights_bytes"))
    model_info = _get(capabilities, "model_info")
    context_tokens = _as_int((params or {}).get("context_tokens"))
    if weights_bytes and isinstance(model_info, Mapping) and context_tokens:
        try:
            from src import vram_fit
            per_token, reason = vram_fit.kv_bytes_per_token_estimated(dict(model_info))
            if per_token is not None:
                return int(weights_bytes + per_token * context_tokens + vram_fit.DEFAULT_RESERVE_BYTES)
            notes.append(f"vram estimate: {reason}")
        except Exception as exc:  # noqa: BLE001 - an estimate must never crash preflight
            notes.append(f"vram estimate failed: {exc}")
    return None


def _estimate_cost(tokens: Optional[int], params: Mapping[str, Any],
                    capabilities: Any, notes: List[str]) -> Any:
    price = _as_float((params or {}).get("price_usd_per_1k_tokens"))
    if price is None:
        price = _as_float(_get(capabilities, "price_usd_per_1k_tokens"))
    if tokens is not None and price is not None:
        return round((tokens / 1000.0) * price, 6)
    notes.append("cost: unknown (no price signal) — reported as \"unknown\", never 0")
    return "unknown"


def _estimate_resources(params: Mapping[str, Any], capabilities: Any) -> Estimate:
    params = params or {}
    notes: List[str] = []
    tokens = _as_int(params.get("estimated_tokens"))
    seconds = _as_float(params.get("estimated_seconds"))
    if tokens is None:
        notes.append("tokens: unknown (no estimate available)")
    if seconds is None:
        notes.append("seconds: unknown (no estimate available)")
    vram_bytes = _estimate_vram_bytes(params, capabilities, notes)
    cost = _estimate_cost(tokens, params, capabilities, notes)
    return Estimate(tokens=tokens, seconds=seconds, vram_bytes=vram_bytes,
                     cost_usd=cost, notes=tuple(notes))


# ── admission (VRAM fit) ────────────────────────────────────────────────

def _available_vram_bytes(engine: str, deployment_id: str) -> Optional[int]:
    """Best-effort VRAM headroom for this deployment. Deliberately makes NO
    device probe itself — `run_preflight` must have zero side effects, and
    reading live GPU state is an ambient runtime read, not a pure
    computation. `None` (unknown) is the honest default; a caller with real
    device knowledge overrides this seam directly:
    ``monkeypatch.setattr(preflight, "_available_vram_bytes", fake)``."""
    return None


def _check_admission(estimate: Estimate, engine: str, deployment_id: str) -> Admission:
    if estimate.vram_bytes is None:
        return Admission(fits=True, reason="vram estimate unknown; admission not gated")
    # A bare call, resolved from this module's globals at call time — so a
    # test's `monkeypatch.setattr(preflight, "_available_vram_bytes", fake)`
    # is seen here without any extra indirection.
    available = _available_vram_bytes(engine, deployment_id)
    vram_bytes = estimate.vram_bytes
    if available is None:
        return Admission(fits=True, reason="available vram unknown; admission not gated")
    if vram_bytes <= available:
        return Admission(fits=True, reason=f"{vram_bytes} bytes <= {available} available")
    return Admission(fits=False,
                      reason=f"needs {vram_bytes} bytes, only {available} available")


# ── cloud-engine inference ──────────────────────────────────────────────

def _infer_cloud_engine(engine: str, capabilities: Any) -> bool:
    declared = _get(capabilities, "is_cloud")
    if declared is not None:
        return bool(declared)
    name = str(engine or "").strip().lower()
    return any(name.startswith(prefix) for prefix in _KNOWN_CLOUD_ENGINE_PREFIXES)


# ── approval plan / digest ──────────────────────────────────────────────

def build_approval_plan(*, operation: str, engine: str, deployment_id: str,
                         project_id: str, inputs: Sequence[str],
                         params: Mapping[str, Any], estimate: Estimate,
                         action: str, detail: str) -> Any:
    """The exact `ApprovalPlan` a granted approval for this operation would
    carry. `cost_units` is the estimate's token count (`budget_account`'s own
    unit — see its module docstring), never a currency figure this module
    would have to invent an exchange rate for. Any later change to
    `operation`/`engine`/`deployment_id`/`inputs`/`params`/`estimate`/
    `action`/`detail` produces a DIFFERENT fingerprint — that is what makes
    "a changed script/recipient/model invalidates the old approval" true by
    construction, the same way `src/approval_store.py::Approval.covers()`
    already enforces it for every other approved plan in Faustus."""
    from src.contracts import ApprovalPlan

    permissions = {
        "operation": operation,
        "deployment_id": deployment_id,
        "project_id": project_id,
        "inputs": sorted(str(i) for i in inputs),
        "params": dict(params or {}),
    }
    return ApprovalPlan.parse({
        "action": action,
        "backend": engine,
        "cost_units": estimate.tokens,
        "permissions": permissions,
        "detail": detail,
    })


# ── budget outlook ───────────────────────────────────────────────────────

def _budget_outlook(project_id: str, decision: Any, estimate: Estimate) -> BudgetOutlook:
    from src import budget_account
    from src.creator import budget_policy

    snap = budget_account.snapshot(budget_policy.run_id_for(project_id))
    ceiling = snap.get("ceiling_tokens") or (decision.ceiling_tokens or None)
    return BudgetOutlook(
        reservation_needed=estimate.tokens,
        ceiling=ceiling if ceiling else None,
        remaining=snap.get("remaining_tokens"),
        verdict=decision.verdict, reason=decision.reason,
    )


# ── the whole preflight ─────────────────────────────────────────────────

def run_preflight(*, owner: str, project_id: str, operation: str, engine: str,
                   deployment_id: str = "", params: Optional[Mapping[str, Any]] = None,
                   inputs: Sequence[str] = (), profile: Optional[Mapping[str, Any]] = None,
                   cloud_engine: Optional[bool] = None) -> PreflightReport:
    """Plan `operation` on `engine`/`deployment_id` for `project_id`, owned by
    `owner`. Pure: reads stores, writes nothing (see module docstring)."""
    params = dict(params or {})
    inputs = tuple(str(i) for i in (inputs or ()) if str(i or "").strip())

    missing: List[Dict[str, str]] = []
    missing.extend(_missing_inputs(owner, inputs))

    capabilities: Any = None
    lookup_error: Optional[Exception] = None
    try:
        capabilities = _capabilities_for(deployment_id)
    except Exception as exc:  # noqa: BLE001 - a lookup failure is a "missing capability", not a crash
        lookup_error = exc
    missing.extend(_missing_capability(operation, engine, capabilities, lookup_error))

    requires_consent, subject_param = _consent_requirement(capabilities, operation)
    missing.extend(_missing_consent(params, requires_consent, subject_param))

    missing.extend(_missing_params(engine, operation, params))

    estimate = _estimate_resources(params, capabilities)
    admission = _check_admission(estimate, engine, deployment_id)

    from src.creator import budget_policy
    resolved_cloud = (
        _infer_cloud_engine(engine, capabilities) if cloud_engine is None else bool(cloud_engine)
    )
    decision = budget_policy.decide(
        project_id=project_id, engine=engine, estimated_tokens=estimate.tokens,
        cloud_engine=resolved_cloud, profile=profile,
    )
    budget = _budget_outlook(project_id, decision, estimate)

    if decision.verdict == "deny":
        missing.append({"kind": "param", "id": "budget", "detail": decision.reason})

    requires_approval = decision.verdict == "ask"
    approval_digest: Optional[str] = None
    approval_plan_dict: Optional[Dict[str, Any]] = None
    if requires_approval:
        detail = (f"{operation} on {engine} (deployment={deployment_id or 'unspecified'}) "
                  f"for project {project_id}: {decision.reason}")
        plan = build_approval_plan(
            operation=operation, engine=engine, deployment_id=deployment_id,
            project_id=project_id, inputs=inputs, params=params, estimate=estimate,
            action=decision.action or "cost_over_budget", detail=detail,
        )
        approval_digest = plan.fingerprint()
        approval_plan_dict = plan.to_dict()

    ok = (not missing) and admission.fits and decision.verdict != "deny"

    return PreflightReport(
        ok=ok, missing=tuple(missing), estimate=estimate, admission=admission,
        requires_approval=requires_approval, approval_digest=approval_digest,
        approval_plan=approval_plan_dict, budget=budget, operation=operation,
        engine=engine, deployment_id=deployment_id, project_id=project_id,
        created_at=time.time(),
    )
