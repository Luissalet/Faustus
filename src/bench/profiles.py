"""src/bench/profiles.py — INF-04 A3: saved `InferenceProfile`s and the
promotion rule (§13's "policy of activation", applied to A1's evaluation
vocabulary instead of speculative decoding specifically).

Storage: one JSON file, `DATA_DIR/inference_profiles.json`, keyed by profile
id, written with `core.atomic_io.atomic_write_json` (same discipline as
`src/launch_receipts.py`: a crash mid-write must not corrupt every other
saved profile). Each stored entry is `{"profile": <InferenceProfile.to_dict()>,
"last_run_id": str|None}` — `last_run_id` is bookkeeping the A1 contract has
no field for (a profile is a CONFIGURATION, not a run), kept alongside it
the same way `launch_receipts.py` keeps `history` beside its `LaunchReceipt`.

Why this does not reuse `src/capability_promotion.py::CapabilityMatrix`
(CONTRATO_INF04 A3 asks to check): that module's vocabulary
(`not_tested -> experimental -> compatible -> recommended`) has no state for
"this got SLOWER" — a `BenchmarkRun` regression is not "not tested", it is
a fact this module needs to keep and show. Its registry is also in-process
only (its own docstring: "this class holds the RULES, not a storage
mechanism") and keyed by `(model, backend, template)`, not by a saved
profile id a person names and revisits across restarts. The DISCIPLINE is
still borrowed on purpose: `promote()` below requires a comparison computed
just now (never a cached/stale verdict) for the exact profile being
promoted, exactly like `CapabilityMatrix.promote`'s "evidencia vigente"
rule — just re-expressed against `Comparison`/`InferenceProfile` instead of
being imported and bent to fit a shape it was not built for.

`activate_profile` (making the chat actually USE a saved profile) was out
of scope for CONTRATO_INF04 Lote A — saving a profile and switching live
traffic to it are different decisions (§09), and the second needed its own
review of what happens to a chat already in flight. INF-05 Lote B (§13
"política de activación") adds it below, at the bottom of this file: for
Ollama it changes `model_load_options` (read per REQUEST by `llm_core`, so
"applies to the next request" is literal, never a mid-turn change to a
process); for every other engine it stays entirely `deferred` — a `plan`
for a person to relaunch with, never a restart this module performs.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import replace
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from core.atomic_io import atomic_write_json
from src.contracts.base import now_iso
from src.contracts.inference import (
    Comparison, EngineIdentity, InferenceProfile, ModelDescriptor,
    PROFILE_EVALUATIONS,
)

logger = logging.getLogger(__name__)

#: Resolved lazily (not at import time) so tests can monkeypatch
#: `src.constants.DATA_DIR` before the first call, same convention as
#: `src/hardware_profiles.py::_path`.
def _path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "inference_profiles.json")


class ProfileNotFound(ValueError):
    """No saved profile exists for the requested id."""


class PromotionRefused(ValueError):
    """`promote()` was asked for on a comparison that cannot back it."""


class ActivationRefused(ValueError):
    """`activate_profile()` was asked for something it cannot honestly do —
    e.g. the profile's engine is Ollama but no configured endpoint answers
    at its host:port, so there is nothing to apply the options against."""


#: Reserved key inside `inference_profiles.json`'s top-level dict for B3's
#: activation bookkeeping (`activations: {"<endpoint>|<model>": {...}}`,
#: CONTRATO_INF05 B3) — NOT a profile id (`uuid4().hex[:12]`/`current-...`
#: never collide with this), so `list_profiles()`/`get_profile()` can tell
#: the one bookkeeping row apart from every real saved profile.
_ACTIVATIONS_KEY = "__activations__"


#: Comparison.verdict -> the InferenceProfile.evaluation it leaves behind.
#: Only "improvement" reaches "recommended" (§13's activation policy); every
#: other verdict still records what was actually observed, so a profile's
#: evaluation always reflects the LAST comparison run against it, never a
#: stale "not_evaluated" once a run has actually happened.
_VERDICT_TO_EVALUATION = {
    "improvement": "recommended",
    "regression": "regression",
    "inconclusive": "inconclusive",
    "no_change": "evaluated",
}


def _load_all() -> Dict[str, Dict[str, Any]]:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("inference_profiles.json unreadable, treating as empty: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _save_all(data: Dict[str, Dict[str, Any]]) -> None:
    atomic_write_json(_path(), data, indent=2)


def _entry_profile(entry: Dict[str, Any]) -> InferenceProfile:
    return InferenceProfile.parse(entry["profile"])


# ── building a profile from what is running now ─────────────────────────────

def current_profile(
    endpoint_url: str, model: str, owner: str, *, objective: str = "interactive",
) -> InferenceProfile:
    """A3: the profile that describes what is running RIGHT NOW at
    `endpoint_url` for `model` — never saved by this call, just built, so a
    caller can compare it as a baseline before deciding to keep it.

    `engine` comes from `launch_receipts.identity_for_endpoint` — a
    Faustus-managed receipt if one matches this host:port, otherwise the
    same "external ollama" / `None` fallback every other INF-03 caller gets
    (§06 H04: never guess between the two llama.cpp implementations).
    `options` mirrors the matching receipt's recorded launch plan when one
    exists; absent a receipt (an externally-managed server, or one this
    process never launched) `options` stays `{}` — an unobserved option set
    is `{}`, never a guess at defaults the server might actually be running
    with. `owner` is accepted (and unused beyond being part of the caller's
    audit trail — the route layer logs/scopes by it) rather than silently
    dropped, so a future caller adding per-owner profile scoping does not
    have to change this function's signature.

    `objective` defaults to `"interactive"`: CONTRATO_INF04 A3 does not list
    it as a parameter, but `InferenceProfile.objective` has no vocabulary
    entry for "not yet chosen" (§09's three objectives are meant to be
    picked, not inferred) — the caller (the `/api/bench/profiles` route)
    overrides it once the person has actually chosen what they are
    optimizing for.
    """
    del owner  # see docstring: reserved for the caller's own audit trail
    from src import launch_receipts

    try:
        parsed = urlparse(endpoint_url or "")
        host, port = parsed.hostname, parsed.port
    except ValueError:
        host, port = None, None

    engine: Optional[EngineIdentity] = launch_receipts.identity_for_endpoint(endpoint_url)
    if engine is None:
        # `identity_for_endpoint` returning `None` means it could not tell
        # WHICH implementation this is (§06 H04) — but the host/port `url`
        # itself named are still known and are what `src/bench/runner.py`
        # needs to reach this endpoint again later; losing them here would
        # make every profile built from an unrecognised engine unrunnable.
        # An Ollama this machine can gate is recognisable by its URL alone
        # (`vram_admission.ollama_root`: port 11434 or an "ollama" host) —
        # the same hint INF-03's chat path uses — and nothing Faustus did not
        # launch is "managed: faustus" (seen on the first real run,
        # 12-09-2026: the resident Ollama came out `unknown`/`faustus`).
        from src import vram_admission
        implementation = "ollama" if vram_admission.ollama_root(endpoint_url or "") else "unknown"
        engine = EngineIdentity(implementation=implementation, host=host, port=port, managed="external")

    options: Dict[str, Any] = {}
    try:
        receipt = launch_receipts.find_by_endpoint(host, port) if host and port else None
    except Exception:  # noqa: BLE001 - a profile snapshot must never fail a request
        receipt = None
    if receipt is not None and isinstance(receipt.plan, dict):
        raw_options = receipt.plan.get("options")
        if isinstance(raw_options, dict):
            options = dict(raw_options)

    model_descriptor = ModelDescriptor.parse({"artifact_id": str(model or "").strip() or "unknown"})
    raw = {
        "id": f"current-{uuid.uuid4().hex[:12]}",
        "label": f"Current: {model}",
        "model": model_descriptor.to_dict(),
        "engine": engine.to_dict(),
        "hardware_id": None,
        "options": options,
        "objective": objective,
        "evaluation": "baseline",
        "created_at": now_iso(),
        "source": "current",
    }
    return InferenceProfile.parse(raw)


# ── persistence ──────────────────────────────────────────────────────────────

def save_profile(profile: InferenceProfile) -> InferenceProfile:
    """Persist `profile` under its own id — a re-save (same id) overwrites
    in place and keeps whatever `last_run_id` a prior `set_evaluation` call
    recorded, so re-saving a profile after editing its label never forgets
    the evidence trail `promote()` built for it."""
    all_profiles = _load_all()
    existing = all_profiles.get(profile.id) or {}
    all_profiles[profile.id] = {
        "profile": profile.to_dict(),
        "last_run_id": existing.get("last_run_id"),
    }
    _save_all(all_profiles)
    return profile


def list_profiles() -> List[InferenceProfile]:
    """Every saved profile, most recently created first. A profile that
    fails to parse (a hand-edited file, a future schema this build does not
    know) is skipped, never crashes the whole listing — the same "one bad
    row must not hide every good one" rule `hardware_profiles.list_profiles`
    follows. `_ACTIVATIONS_KEY`'s bookkeeping row is not a profile and never
    reaches this list."""
    out = []
    for key, entry in _load_all().items():
        if key == _ACTIVATIONS_KEY:
            continue
        try:
            out.append(_entry_profile(entry))
        except Exception as e:  # noqa: BLE001
            logger.warning("skipping unparseable saved profile: %s", e)
    out.sort(key=lambda p: p.created_at, reverse=True)
    return out


def get_profile(profile_id: str) -> Optional[InferenceProfile]:
    pid = str(profile_id or "")
    if pid == _ACTIVATIONS_KEY:
        return None
    entry = _load_all().get(pid)
    if entry is None:
        return None
    return _entry_profile(entry)


def get_last_run_id(profile_id: str) -> Optional[str]:
    entry = _load_all().get(str(profile_id or ""))
    return entry.get("last_run_id") if entry else None


def set_evaluation(profile_id: str, state: str, run_id: Optional[str] = None) -> InferenceProfile:
    """Update a saved profile's `evaluation` in place. `state` must already
    be one of `PROFILE_EVALUATIONS` — this function does not invent new
    vocabulary — and `run_id`, when given, becomes the profile's
    `last_run_id` (the evidence `promote()` points back to); omitted, the
    previous `last_run_id` is kept rather than cleared, so calling this for
    a reason unrelated to a specific run never erases which run justified
    the CURRENT evaluation."""
    if state not in PROFILE_EVALUATIONS:
        raise ValueError(f"state must be one of {PROFILE_EVALUATIONS}, got {state!r}")
    all_profiles = _load_all()
    entry = all_profiles.get(str(profile_id or ""))
    if entry is None:
        raise ProfileNotFound(profile_id)
    profile = _entry_profile(entry)
    updated = replace(profile, evaluation=state)
    all_profiles[profile.id] = {
        "profile": updated.to_dict(),
        "last_run_id": run_id if run_id is not None else entry.get("last_run_id"),
    }
    _save_all(all_profiles)
    return updated


def promote(profile_id: str, comparison: Comparison) -> InferenceProfile:
    """§13's activation policy: the CANDIDATE profile (`profile_id`, which
    must be the profile behind `comparison.candidate_run_id`'s run — the
    caller is responsible for that association, this function only trusts
    the `Comparison` it is handed) reaches `evaluation="recommended"` ONLY
    when `comparison.verdict == "improvement"`.

    Every other verdict still records the observed fact
    (`_VERDICT_TO_EVALUATION`) rather than leaving the profile at whatever
    it was before — a `regression` is written down as exactly that, which is
    what "preservar el resultado y motivo de la decisión" (§13) asks for;
    the MOTIVE itself (`comparison.reasons`) lives on the `Comparison`/
    `BenchmarkRun` already returned to the caller, not duplicated onto the
    profile. `promote()` never touches any OTHER profile (in particular, it
    never rewrites a baseline's evaluation back to `not_evaluated`) — "una
    regresión puede proponer volver al perfil anterior" is a suggestion the
    ROUTE/UI layer surfaces from `verdict == "regression"`, never an
    automatic revert this function performs.

    Refuses (`PromotionRefused`) for a `comparison` that is not
    `comparable` at all — an inconclusive-because-incomparable result must
    never promote, demote, or otherwise touch a profile's evaluation, since
    there is no evidence here that applies to it in the first place.
    """
    if not comparison.comparable:
        raise PromotionRefused(
            "comparison is not comparable"
            + (f": {'; '.join(comparison.reasons)}" if comparison.reasons else "")
            + " — an incomparable result never changes a profile's evaluation"
        )
    new_state = _VERDICT_TO_EVALUATION[comparison.verdict]
    return set_evaluation(profile_id, new_state, run_id=comparison.candidate_run_id)


# ── activation (INF-05 B3, §13 "política de activación") ───────────────────
#
# Saving a profile and switching live traffic to it are different decisions
# (§09) — this module's own header docstring said so and left the gap open.
# `activate_profile` closes it, but stays inside the same boundary §13 draws
# for speculative decoding: it may change what a FUTURE request/launch uses,
# never a process already running one. For Ollama, `model_load_options` is
# read fresh on every request (`resolve_for_request`, called by
# `src/llm_core.py`) — so "applies to the next request" is a literal fact
# about this codebase's request path, not a promise this function makes and
# hopes holds. For every other engine, a saved profile has nothing it can
# change without a relaunch, so activation is entirely `deferred`: a ready
# `plan` for a person to relaunch with, never a process this function starts
# or restarts on its own.


def _activation_key(endpoint_url: str, model: str) -> str:
    """`"<endpoint netloc>|<model>"` — loopback aliases collapsed the same
    way `model_load_options.resolve_for_request` already keys its own
    per-model options, so an activation recorded against one alias of an
    endpoint is still found when a caller asks with another."""
    from src.model_load_options import _netloc_key
    return f"{_netloc_key(endpoint_url)}|{str(model or '').strip()}"


def _load_activations() -> Dict[str, Dict[str, Any]]:
    raw = _load_all().get(_ACTIVATIONS_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _save_activations(activations: Dict[str, Dict[str, Any]]) -> None:
    all_data = _load_all()
    all_data[_ACTIVATIONS_KEY] = activations
    _save_all(all_data)


def _resolve_ollama_endpoint_id(host: Optional[str], port: Optional[int]) -> Optional[str]:
    """The configured endpoint id whose root matches `host:port` — the same
    identity `model_load_options.set_options`/`get_options` key on. `None`
    when nothing declared answers there (a profile captured against a
    server that has since been un-configured, or one this install never
    knew about) — `activate_profile` turns that into `ActivationRefused`
    rather than inventing an endpoint id nothing points at."""
    if not host or not port:
        return None
    try:
        from routes.local_models_routes import list_ollama_endpoints
        from src.model_load_options import _netloc_key
    except Exception:  # noqa: BLE001
        return None
    want = _netloc_key(f"http://{host}:{port}")
    if not want:
        return None
    try:
        endpoints = list_ollama_endpoints(owner="", is_admin=True)
    except Exception:  # noqa: BLE001
        return None
    for ep in endpoints:
        if _netloc_key(str(ep.get("root") or ep.get("base_url") or "")) == want:
            return str(ep.get("id") or "") or None
    return None


def activate_profile(profile_id: str, *, owner: str = "") -> Dict[str, Any]:
    """§13's activation policy, applied to a saved `InferenceProfile`
    instead of a speculative-decoding profile: `{profile_id, scope,
    applied, deferred, previous_profile_id, activated_at, note}`.

    Ollama (`profile.engine.implementation == "ollama"`): `profile.options`
    is filtered to what `model_load_options.ALLOWED_KEYS` accepts
    (`num_ctx`, `num_gpu`, `keep_alive`, `main_gpu`, `extra`) and MERGED
    (never a wholesale replace — a person's other saved knobs for this
    model are not this profile's business) on top of whatever is already
    saved for the resolved endpoint+model, via `set_options`. Everything
    NOT in that allow-list (`flash_attn`, `kv_cache_type`, `parallel`, a
    server's own startup flags) is `deferred` with `scope:
    "requires_restart"` — real settings, just not ones any per-request call
    can change, so this function does not pretend otherwise. `scope`
    itself is `"next_request"` for the whole activation, because the part
    that WAS applied genuinely takes effect that way; a caller wanting to
    know whether anything was deferred too reads `deferred`, not `scope`.

    Every other engine: the WHOLE profile is `deferred` (`scope:
    "requires_restart"`), carrying a `plan` (`{implementation, model,
    options}`) the Studio can offer as "Relaunch with this profile" — this
    function never launches it.

    `owner` is accepted for the caller's own audit trail (unused here
    beyond that), matching `current_profile`'s existing convention.

    Raises `ProfileNotFound` for an unknown id, `ActivationRefused` when
    the engine is Ollama but no configured endpoint matches its host:port.
    Never touches a process, never restarts a server, never changes
    anything mid-turn — `model_load_options` is read per request, so what
    this writes is picked up starting with whichever request comes next.
    """
    profile = get_profile(profile_id)
    if profile is None:
        raise ProfileNotFound(profile_id)

    host, port = profile.engine.host, profile.engine.port
    endpoint_url = f"http://{host}:{port}" if host and port else ""
    key = _activation_key(endpoint_url, profile.model.artifact_id)
    activations = _load_activations()
    previous_record = activations.get(key) or {}
    previous_profile_id = previous_record.get("profile_id")

    applied: Dict[str, Any] = {}
    deferred: Dict[str, Any] = {}
    previous_options_snapshot: Optional[Dict[str, Any]] = None
    applied_keys: List[str] = []
    endpoint_id: Optional[str] = None

    if profile.engine.implementation == "ollama":
        endpoint_id = _resolve_ollama_endpoint_id(host, port)
        if not endpoint_id:
            raise ActivationRefused(
                f"no configured Ollama endpoint answers at {host}:{port}; "
                "nothing to apply this profile's options against")
        from src.model_load_options import ALLOWED_KEYS, get_options, set_options
        raw_options = dict(profile.options or {})
        filtered = {k: v for k, v in raw_options.items() if k in ALLOWED_KEYS}
        server_scoped = {k: v for k, v in raw_options.items() if k not in ALLOWED_KEYS}
        previous_options_snapshot = dict(get_options(endpoint_id, profile.model.artifact_id))
        set_options(endpoint_id, profile.model.artifact_id, {**previous_options_snapshot, **filtered})
        applied_keys = list(filtered.keys())
        applied = {"endpoint_id": endpoint_id, "model": profile.model.artifact_id, "options": filtered}
        if server_scoped:
            deferred = {
                "options": server_scoped, "scope": "requires_restart",
                "note": "server-scoped option: applies to the next launch, not to this chat",
            }
        scope = "next_request"
        note = f"Applies to the next request on {host}:{port} · {profile.model.artifact_id}"
    else:
        plan = {"implementation": profile.engine.implementation,
               "model": profile.model.artifact_id, "options": dict(profile.options or {})}
        note = ("Server-scoped: needs a relaunch — open Cookbook › Running "
                "to relaunch with this profile")
        deferred = {"plan": plan, "scope": "requires_restart", "note": note}
        scope = "requires_restart"

    activated_at = now_iso()
    activations[key] = {
        "profile_id": profile.id, "previous_profile_id": previous_profile_id,
        "activated_at": activated_at, "scope": scope,
        "previous_options": previous_options_snapshot, "applied_keys": applied_keys,
        "endpoint_id": endpoint_id, "endpoint_url": endpoint_url, "model": profile.model.artifact_id,
    }
    _save_activations(activations)

    return {"profile_id": profile.id, "scope": scope, "applied": applied, "deferred": deferred,
           "previous_profile_id": previous_profile_id, "activated_at": activated_at, "note": note}


def active_profile_for(endpoint_url: str, model: str) -> Optional[str]:
    """The profile id currently activated for this endpoint+model, or
    `None` — never fetched over the network, pure lookup against the
    activation record `activate_profile` last wrote."""
    rec = _load_activations().get(_activation_key(endpoint_url, model))
    return rec.get("profile_id") if rec else None


def deactivate_profile(profile_id: str) -> Dict[str, Any]:
    """Undo `activate_profile(profile_id)`: restore whatever
    `model_load_options` held BEFORE that activation, if there was
    anything — or, if activation was the first thing to ever set options
    for that (endpoint, model), remove exactly the keys it added and
    nothing else a person may have set through a different path since.
    Raises `ProfileNotFound` when this profile has no activation record to
    undo (never activated, or already deactivated)."""
    activations = _load_activations()
    key = next((k for k, rec in activations.items() if rec.get("profile_id") == profile_id), None)
    if key is None:
        raise ProfileNotFound(profile_id)
    record = activations[key]
    endpoint_id = record.get("endpoint_id")
    model = record.get("model")
    if endpoint_id and model:
        from src.model_load_options import get_options, set_options
        previous = record.get("previous_options")
        if previous is not None:
            set_options(endpoint_id, model, previous)
        else:
            applied_keys = set(record.get("applied_keys") or [])
            remaining = dict(get_options(endpoint_id, model))
            for k in applied_keys:
                remaining.pop(k, None)
            set_options(endpoint_id, model, remaining)
    activations.pop(key, None)
    _save_activations(activations)
    return {"profile_id": profile_id, "deactivated": True}


def rollback_proposal(profile_id: str) -> Dict[str, Any]:
    """§13: "una regresión puede proponer volver al perfil anterior" — a
    SUGGESTION only, `{previous_profile_id, reason}`, never an automatic
    revert (that stays the route/UI layer's call, on an explicit click).
    `previous_profile_id` is `None` when this profile has never been
    activated, or its activation named no predecessor (it was the first
    profile ever activated for that endpoint+model)."""
    profile = get_profile(profile_id)
    if profile is None:
        raise ProfileNotFound(profile_id)
    for rec in _load_activations().values():
        if rec.get("profile_id") == profile_id:
            previous = rec.get("previous_profile_id")
            reason = (f"{profile.label} regressed; consider returning to the previous profile"
                     if previous else "no previous profile is on record for this activation")
            return {"previous_profile_id": previous, "reason": reason}
    return {"previous_profile_id": None, "reason": "this profile has not been activated"}
