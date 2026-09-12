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

`activate_profile` (making the chat actually USE a saved profile) is
explicitly OUT of this lote (CONTRATO_INF04 Lote A): there is no function
of that name here, half-implemented or otherwise — saving a profile and
switching live traffic to it are different decisions (§09), and the second
one needs its own review of what happens to a chat already in flight. The
next lote that adds it should start from `current_profile()`'s reverse: it
would need to change what `EngineIdentity`/options a session's next request
actually uses, which nothing in Lote A touches.
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
        engine = EngineIdentity(implementation="unknown", host=host, port=port)

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
    follows."""
    out = []
    for entry in _load_all().values():
        try:
            out.append(_entry_profile(entry))
        except Exception as e:  # noqa: BLE001
            logger.warning("skipping unparseable saved profile: %s", e)
    out.sort(key=lambda p: p.created_at, reverse=True)
    return out


def get_profile(profile_id: str) -> Optional[InferenceProfile]:
    entry = _load_all().get(str(profile_id or ""))
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
