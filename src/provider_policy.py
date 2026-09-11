"""src/provider_policy.py — ADP-22: make the route of one turn explicit.

Three questions this module answers about ONE candidate connection+model,
never blended into a single opaque score: who pays (`billing`), does the
call leave this machine (`network`), and — if this route differs from what
was originally requested — how far the fallback reached
(`fallback_scope`). `resolve_route()` returns a `RouteDecision` carrying all
three plus the human-readable `reason` a caller can log or show, exactly the
"registro explícito de POR QUÉ se eligió" ADP-22 asks for.

This is deliberately a SEPARATE concern from `src.model_router` (MOD-05):
that module picks WHICH local model answers a turn from evidence
(capability probes, speed, outcome history); this module classifies WHATEVER
route a caller is about to use (local model, subscription, or paid API) and
enforces the policy invariants that must hold regardless of which one it
turns out to be. The two compose — `routes/chat_routes.py` calls
`model_router.choose()` first (when the turn's requested model is the
"auto" sentinel) and then `resolve_route()` on the result — but neither
requires the other.

Four invariants, each with a dedicated test in
`tests/test_adp22_provider_policy.py`, and each enforced by RAISING
`ProviderPolicyError` (never a silent downgrade) so a caller cannot ship a
turn through this module without deciding what to do about a violation:

  1. **Local never escalates to remote without authorization.** When the
     active privacy profile is `local_only` (`src.privacy_policy`) and the
     candidate route is not a local destination, `resolve_route` refuses —
     unless the caller passes `RouteRequirements(confirmed_remote=True)`,
     the same explicit, one-call opt-in discipline
     `model_router.Decision.escalated` uses for its own paid escalation.
  2. **A subscription connection never falls back to a paid API because of
     an auth failure.** `RouteRequirements.prior` describes the route that
     was just tried; when it was `billing="subscription"` and failed with
     an auth-shaped error (401/403/"payment_required"/402) and the NEW
     candidate is `billing="api_paid"`, `resolve_route` refuses unless
     `confirmed_remote=True` is set by the caller on purpose.
  3. **A required parameter that is confirmed unsupported is never ignored.**
     `RouteRequirements.required_parameters` is checked against capability
     evidence (`src.model_calibration`'s tested/announced manifest for a
     local model, or an explicit `endpoint["capabilities"]` list a caller
     already resolved for a remote one). A parameter PROVEN unsupported
     raises `provider.parameter_unsupported`; a parameter with no evidence
     either way is reported in `RouteDecision.reason` as unknown, never
     silently dropped and never treated as a pass.
  4. **A per-token price ceiling is never described as a run-level cap.**
     `RouteRequirements.max_price_per_token` mirrors
     `src.openrouter_options`'s own `max_price` field name and shape
     (`{"prompt": usd_per_token, "completion": usd_per_token}` —
     OpenRouter's own per-token ceiling, not a budget for the whole run).
     `resolve_route` carries it into `RouteDecision.data_restrictions` under
     the same `max_price_per_token` key — never renamed to anything that
     could read as a total/budget/cap on the run — and its `reason` text
     says "per token" explicitly whenever a price ceiling is present.

Nothing here performs network I/O or reads billing state; every fact it acts
on is handed in by the caller (already-resolved endpoint identity,
capability evidence, the prior attempt's outcome) or comes from
`src.privacy_policy`'s pure profile lookup, so it stays unit-testable with
fakes, as the contract for this lote requires.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src import privacy_policy

logger = logging.getLogger(__name__)

# ── billing / network / fallback vocabularies (closed, like src.privacy_policy.PROFILES) ──

BILLING_LOCAL = "local"
BILLING_SUBSCRIPTION = "subscription"
BILLING_API_PAID = "api_paid"
BILLING_UNKNOWN = "unknown"
BILLINGS: Tuple[str, ...] = (BILLING_LOCAL, BILLING_SUBSCRIPTION, BILLING_API_PAID, BILLING_UNKNOWN)

NETWORK_LOCAL = "local"
NETWORK_REMOTE = "remote"

FALLBACK_NONE = "none"
FALLBACK_MODEL = "model"
FALLBACK_CONNECTION = "connection"
FALLBACK_PROVIDER = "provider"
FALLBACK_SCOPES: Tuple[str, ...] = (FALLBACK_NONE, FALLBACK_MODEL, FALLBACK_CONNECTION, FALLBACK_PROVIDER)

#: Error classes a prior attempt can report that count as "authentication
#: failed", never grounds to silently jump from a subscription to a metered
#: API. Named strings, not raw HTTP codes, because a caller may only have a
#: classified error (`src.retry_policy.error_class_for`) rather than the
#: original status by the time it calls back into this module.
_AUTH_FAILURE_MARKERS: Tuple[str, ...] = ("401", "403", "402", "auth", "unauthorized", "payment_required")


class ProviderPolicyError(RuntimeError):
    """Raised by `resolve_route` when a candidate route violates one of the
    four invariants in the module docstring. `error_class` follows this
    repo's flat 4xx convention (`routes/git_routes.py::_error`,
    `{"error": "...", "error_class": "<area>.<motivo>"}`) — a caller with an
    HTTP boundary maps this straight onto that shape; one without (a
    background job, a test) reads `error_class`/`detail` directly."""

    def __init__(self, error_class: str, detail: str):
        self.error_class = error_class
        self.detail = detail
        super().__init__(detail)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read `key` off `obj`, which may be a plain mapping (a test fake, a
    JSON-decoded dict) or an attribute-bearing object (a `ModelEndpoint`
    row, a chat session). Never raises for a missing key on either shape."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


@dataclass(frozen=True)
class RouteRequirements:
    """What THIS resolution call needs to check, independent of what
    `endpoint`/`session` already describe. All fields are optional — the
    all-defaults instance checks only the local/remote and
    subscription/auth-failure invariants (1 and 2), which need no extra
    input beyond `endpoint`/`session`/`project`."""

    #: Parameter/capability names the turn cannot proceed without (MOD-05's
    #: own vocabulary when checked against a local model —
    #: `src.model_router.VALID_CAPABILITIES` — or whatever names the
    #: caller's own capability evidence uses for a remote one).
    required_parameters: Tuple[str, ...] = ()
    #: OpenRouter's own per-token price ceiling shape
    #: (`src.openrouter_options`'s `max_price`): `{"prompt": usd, "completion": usd}`,
    #: dollars PER TOKEN — never a total budget for the run. `None` means no
    #: ceiling was configured for this turn.
    max_price_per_token: Optional[Mapping[str, float]] = None
    #: An explicit, human-sourced one-time authorization for THIS call to
    #: leave `local_only` scope or to fall from a subscription to a paid API
    #: after an auth failure. Mirrors `model_router.Decision.escalated`'s
    #: discipline: never inferred, always something a caller set on purpose.
    confirmed_remote: bool = False
    #: The route that was already tried for this turn, if any —
    #: `{"connection_id":, "model":, "base_url":, "billing":, "error_class":}`.
    #: Drives both invariant 2 (subscription→paid on auth failure) and
    #: `RouteDecision.fallback_scope` (how far this candidate is from it).
    prior: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class RouteDecision:
    """One resolved, explained route. `to_dict()` is what a caller logs or
    (see `routes/chat_routes.py`) folds into an SSE event — never contains
    an API key or bearer token, only identity and classification."""

    connection_id: Optional[str]
    model: str
    billing: str
    network: str
    data_restrictions: Dict[str, Any]
    fallback_scope: str
    reason: str
    escalated: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "model": self.model,
            "billing": self.billing,
            "network": self.network,
            "data_restrictions": dict(self.data_restrictions),
            "fallback_scope": self.fallback_scope,
            "reason": self.reason,
            "escalated": self.escalated,
        }


def _vendor(base_url: str) -> str:
    """Best-effort provider vendor for `base_url`, reusing the one detector
    the rest of the app already trusts (`src.llm_core._detect_provider`)
    instead of a second, divergent host-matching table."""
    try:
        from src.llm_core import _detect_provider
        return _detect_provider(base_url or "")
    except Exception:
        logger.debug("provider_policy: could not detect vendor for endpoint", exc_info=True)
        return "unknown"


def _classify_billing(base_url: str, endpoint_kind: Optional[str], provider_auth_id: Optional[str]) -> str:
    """`local` (stays on this machine), `subscription` (ChatGPT/Copilot —
    session-backed credentials, never a per-token invoice), `api_paid`
    (`src.endpoint_resolver.endpoint_cost_tracked` says yes), or `unknown`
    (never `0` — this repo's cost-unknown convention, see
    `src.workflow_cost_estimate`)."""
    if privacy_policy.is_local_destination(base_url):
        return BILLING_LOCAL
    vendor = _vendor(base_url)
    if vendor in ("chatgpt-subscription", "copilot"):
        return BILLING_SUBSCRIPTION
    if provider_auth_id:
        # A session-backed credential (`ModelEndpoint.provider_auth_id`,
        # resolved at call time by `src.endpoint_resolver.resolve_endpoint_runtime`)
        # that isn't one of the two subscription vendors above is still not a
        # bare per-token API key — treat it as subscription-shaped billing
        # rather than guessing api_paid from a URL alone.
        return BILLING_SUBSCRIPTION
    try:
        from src.endpoint_resolver import endpoint_cost_tracked
        if endpoint_cost_tracked(base_url, endpoint_kind):
            return BILLING_API_PAID
    except Exception:
        logger.debug("provider_policy: endpoint_cost_tracked failed", exc_info=True)
        return BILLING_UNKNOWN
    return BILLING_UNKNOWN


def _is_auth_failure(error_class: Optional[str]) -> bool:
    ec = str(error_class or "").strip().lower()
    if not ec:
        return False
    return any(marker in ec for marker in _AUTH_FAILURE_MARKERS)


def _fallback_scope(
    connection_id: Optional[str],
    model: str,
    base_url: str,
    prior: Optional[Mapping[str, Any]],
) -> str:
    if not prior:
        return FALLBACK_NONE
    prior_connection = prior.get("connection_id")
    prior_model = prior.get("model")
    if connection_id is not None and connection_id == prior_connection:
        return FALLBACK_NONE if model == prior_model else FALLBACK_MODEL
    prior_base = prior.get("base_url") or ""
    if prior_base and _vendor(prior_base) == _vendor(base_url):
        return FALLBACK_CONNECTION
    return FALLBACK_PROVIDER


def _check_required_parameters(
    requested_model: str,
    endpoint: Any,
    network: str,
    required: Sequence[str],
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """`(unsupported, unknown)` for `required` against whatever capability
    evidence is available. `unsupported` is a PROVEN-false result (never
    reached silently — see invariant 3); `unknown` is "no evidence either
    way", surfaced in the decision's `reason` but not blocking, matching
    this repo's `unknown`-is-not-`false` discipline."""
    if not required:
        return (), ()

    explicit = _get(endpoint, "capabilities", None)
    if explicit is not None:
        have = {str(c) for c in explicit}
        missing = tuple(p for p in required if p not in have)
        # An explicit capability list is a caller-resolved fact, not a probe
        # result — a name absent from it is a clean "unsupported", not
        # "unknown" (the caller already did the evidence-gathering).
        return missing, ()

    if network != NETWORK_LOCAL:
        # No explicit capability evidence for a remote connection: this
        # module has no probe data of its own for it (that lives in
        # `src.model_capability_readers`, per-vendor and out of this lote's
        # scope) — every required parameter is honestly unknown, not
        # silently assumed supported.
        return (), tuple(required)

    try:
        from src import model_calibration
        from src import model_router as _model_router
    except Exception:
        logger.debug("provider_policy: capability lookup unavailable", exc_info=True)
        return (), tuple(required)

    key = model_calibration.manifest_key(vendor="ollama", model_id=requested_model)
    manifest = model_calibration.get_manifest(key)
    unsupported = []
    unknown = []
    for cap in required:
        if cap not in _model_router.VALID_CAPABILITIES:
            # Not one of MOD-05's probed capability names — this module has
            # no local evidence for it either.
            unknown.append(cap)
            continue
        tested_ok, declared = _model_router._capability_status(manifest, cap)
        if tested_ok is False:
            unsupported.append(cap)
        elif tested_ok is None and not declared:
            unknown.append(cap)
        # tested_ok is True, or (None and declared) -> treated as supported.
    return tuple(unsupported), tuple(unknown)


def resolve_route(
    *,
    requested_model: str,
    endpoint: Any,
    session: Any = None,
    project: Optional[Mapping[str, Any]] = None,
    requirements: Optional[RouteRequirements] = None,
) -> RouteDecision:
    """Classify and validate ONE candidate route for `requested_model` on
    `endpoint`. Raises `ProviderPolicyError` when the candidate violates one
    of the module docstring's four invariants; otherwise returns the
    explained `RouteDecision`.

    `endpoint` may be a `ModelEndpoint` row, a route descriptor dict
    (`src.endpoint_resolver.resolve_route_descriptor`'s shape, optionally
    extended with `base_url`/`endpoint_kind`/`provider_auth_id`/
    `capabilities`), or any object/mapping exposing those same names — see
    `_get`. Fields this function never needs (an API key, headers) are
    simply ignored, so a caller can pass its already-resolved endpoint
    object as-is without stripping it down first.
    """
    requirements = requirements or RouteRequirements()
    model = str(requested_model or "").strip()
    connection_id = (
        _get(endpoint, "connection_id")
        or _get(endpoint, "endpoint_id")
        or _get(endpoint, "id")
    )
    connection_id = str(connection_id) if connection_id is not None else None
    base_url = str(_get(endpoint, "base_url") or _get(endpoint, "endpoint_url") or "").strip()
    endpoint_kind = _get(endpoint, "endpoint_kind")
    provider_auth_id = _get(endpoint, "provider_auth_id")

    owner = _get(session, "owner")
    is_local = privacy_policy.is_local_destination(base_url)
    network = NETWORK_LOCAL if is_local else NETWORK_REMOTE
    billing = _classify_billing(base_url, endpoint_kind, provider_auth_id)

    profile = privacy_policy.get_privacy_profile(project=project, owner=owner)
    data_restrictions: Dict[str, Any] = {
        "privacy_profile": profile,
        "local_only": profile == privacy_policy.PROFILE_LOCAL_ONLY,
    }

    reasons = []
    escalated = False

    # Invariant 1: local_only never lets a remote candidate through silently.
    if network == NETWORK_REMOTE and profile == privacy_policy.PROFILE_LOCAL_ONLY:
        if not requirements.confirmed_remote:
            raise ProviderPolicyError(
                "provider.local_escalation_blocked",
                f"privacy profile is '{privacy_policy.PROFILE_LOCAL_ONLY}'; "
                f"connection {connection_id!r} is remote and was not explicitly authorized "
                f"(pass RouteRequirements(confirmed_remote=True) for a one-time, human-confirmed exception)",
            )
        escalated = True
        reasons.append("remote connection explicitly authorized despite local_only profile")

    # Invariant 2: a subscription never falls to a paid API because of an
    # auth failure without the same explicit confirmation.
    prior = requirements.prior or {}
    if (
        prior.get("billing") == BILLING_SUBSCRIPTION
        and _is_auth_failure(prior.get("error_class"))
        and billing == BILLING_API_PAID
    ):
        if not requirements.confirmed_remote:
            raise ProviderPolicyError(
                "provider.subscription_to_paid_blocked",
                "the subscription connection failed authentication; falling back to a "
                "metered API connection requires explicit confirmation, never automatic "
                "(pass RouteRequirements(confirmed_remote=True) for a one-time, human-confirmed exception)",
            )
        escalated = True
        reasons.append("paid API connection explicitly confirmed after a subscription auth failure")

    # Invariant 3: a required parameter proven unsupported is a hard error;
    # one with no evidence is reported, never dropped.
    unsupported, unknown = _check_required_parameters(model, endpoint, network, requirements.required_parameters)
    if unsupported:
        raise ProviderPolicyError(
            "provider.parameter_unsupported",
            f"{model!r} on connection {connection_id!r} does not support required "
            f"parameter(s): {', '.join(unsupported)}",
        )
    if unknown:
        reasons.append(f"support for {', '.join(unknown)} is unverified on this connection")

    # Invariant 4: a per-token price ceiling is carried under an unambiguous
    # name and described as per-token, never as a run-level budget.
    if requirements.max_price_per_token:
        data_restrictions["max_price_per_token"] = dict(requirements.max_price_per_token)
        reasons.append(
            "max_price_per_token is a per-token OpenRouter price ceiling, not a cap on the run's total cost"
        )

    fallback_scope = _fallback_scope(connection_id, model, base_url, requirements.prior)
    if fallback_scope != FALLBACK_NONE:
        reasons.append(f"fallback_scope={fallback_scope} vs. the prior attempt")

    reason = "; ".join(reasons) if reasons else f"{billing} connection, {network}, no fallback"

    return RouteDecision(
        connection_id=connection_id,
        model=model,
        billing=billing,
        network=network,
        data_restrictions=data_restrictions,
        fallback_scope=fallback_scope,
        reason=reason,
        escalated=escalated,
    )
