"""
src/inference_capabilities.py — INF-02 §06: "validate before you build the
command." Pure functions only: no process is started, no network call is
made, no file other than the versioned manifest itself is touched.

`assess_options` answers, for each option a client wants to set, three
things that must never collapse into one boolean (§05):

  support   — does the manifest say this implementation accepts the option
              at all, and (if it lists prerequisites) are they met?
  effective — always `unconfirmed` here; only a running process, probed
              after launch, can confirm what actually took effect
              (`launch_receipts.verify`, not this module).
  benefit   — always `not_evaluated` here; only a benchmark run establishes
              that (out of scope for INF-02).

An option this module has never heard of is `unknown`, not `unsupported` —
the manifest not mentioning something is not the same fact as a vendor
actively rejecting it. An architecture-dependent requirement checked against
an unknown architecture is `unknown` for the same reason: "we could not
verify the mixture-of-experts claim" is not "this is a dense model".

`python -m llama_cpp.server` is a special case documented in
`config/inference_capabilities.json`'s absence: several flags llama-server
accepts have no equivalent in the Python wrapper at all (H04 — same family
name, different binary). Those are hard-coded here (`LLAMA_CPP_PY_OMITTED`,
copied from `studio/src/lib/cookbook/serve.ts`'s `LLAMA_CPP_PYTHON_NO_EQUIVALENT`
so the two lists cannot drift silently) rather than left out of the manifest
and defaulted to `unknown` — a flag with NO equivalent is a stronger, more
useful claim than "not looked up yet".
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.contracts.inference import (
    Benefit, CapabilityAssessment, Effective, Evidence,
)

MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "inference_capabilities.json",
)

#: Canonical option names `python -m llama_cpp.server` has no flag for at
#: all — copied from serve.ts's `LLAMA_CPP_PYTHON_NO_EQUIVALENT` (studio is
#: Lote B's territory; this file only mirrors the *names*, never imports or
#: modifies that TypeScript). Keep in sync by hand if that list changes.
LLAMA_CPP_PY_OMITTED = frozenset({
    "fit", "no_mmap", "no_warmup", "split_mode", "tensor_split", "main_gpu",
    "parallel", "batch_size", "ubatch_size", "mtp", "flash_attn",
})

_LLAMA_CPP_PY_IMPL = "llama_cpp.server"

_manifest_lock = threading.Lock()
_manifest_cache: Optional[Dict[str, Any]] = None


def load_manifest(*, force_reload: bool = False) -> Dict[str, Any]:
    """The versioned manifest (`config/inference_capabilities.json`),
    cached in memory after the first read — it ships with the app and does
    not change at runtime, so re-parsing it on every assessment would be
    pure overhead. Tests that want a different manifest pass one explicitly
    to `assess_options` instead of mutating this cache."""
    global _manifest_cache
    with _manifest_lock:
        if _manifest_cache is None or force_reload:
            with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
                _manifest_cache = json.load(f)
        return _manifest_cache


def _implementation_options(manifest: Mapping[str, Any], implementation: str) -> Dict[str, Any]:
    entry = manifest.get(implementation)
    if not isinstance(entry, Mapping):
        return {}
    options = entry.get("options")
    return dict(options) if isinstance(options, Mapping) else {}


_TRUE_TOKENS = {"on", "true", "yes", "1", "enabled"}
_FALSE_TOKENS = {"off", "false", "no", "0", "disabled"}


def _truthy(value: Any) -> bool:
    """Whether a requested option value reads as "enabled" — accepts the
    handful of spellings the serve form and manual commands actually use
    (`True`, `"on"`, `"true"`, `1`, ...). Anything else (including absence)
    reads as not enabled; there is no separate "unknown" here because this
    is about a value the *caller supplied in this same request*, not
    something that had to be observed."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_TOKENS
    return False


_ARCH_REQUIRE_RE = re.compile(r"^arch\.([A-Za-z_][A-Za-z0-9_]*)==(\w+)$")
_GPU_REQUIRE_RE = re.compile(r"^gpus>=(\d+)$")
_OPTION_REQUIRE_RE = re.compile(r"^option:([A-Za-z_][A-Za-z0-9_]*)==(\w+)$")


def _coerce_literal(token: str) -> Any:
    low = token.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return token


def _check_requirement(
    requirement: str, *, arch: Optional[Mapping[str, Any]],
    gpus: Optional[Sequence[Any]], options: Mapping[str, Any],
) -> str:
    """One requirement string -> "pass" | "fail" | "unknown". "unknown"
    covers both "we have no evidence either way" (arch/gpus not supplied)
    and "the evidence we have does not resolve the question" (arch known,
    but its relevant field is itself `"unknown"`/`None`) — §06 is explicit
    that those must read the same as "not yet established", never as a
    silent `False`.
    """
    m = _ARCH_REQUIRE_RE.match(requirement)
    if m:
        field, want = m.groups()
        if arch is None:
            return "unknown"
        have = arch.get(field)
        if have is None or have == "unknown":
            return "unknown"
        return "pass" if have == _coerce_literal(want) else "fail"

    m = _GPU_REQUIRE_RE.match(requirement)
    if m:
        if gpus is None:
            return "unknown"
        return "pass" if len(gpus) >= int(m.group(1)) else "fail"

    m = _OPTION_REQUIRE_RE.match(requirement)
    if m:
        # `want` here is always an on/off spelling ("on"/"off", "true"/
        # "false", ...) — `option:flash_attn==on` — never an arbitrary
        # string value, so both sides go through the same truthy reader
        # rather than an exact-string compare (`_coerce_literal` is only
        # for arch.* comparisons, where the value IS an arbitrary string
        # like "moe").
        opt_name, want = m.groups()
        return "pass" if _truthy(options.get(opt_name)) == _truthy(want) else "fail"

    # A requirement string this module does not recognize is neither proven
    # nor disproven by anything we can check — never silently "pass".
    return "unknown"


def assess_options(
    implementation: str,
    options: Mapping[str, Any],
    *,
    arch: Optional[Mapping[str, Any]] = None,
    engine_version: Optional[str] = None,
    gpus: Optional[Sequence[Any]] = None,
    manifest: Optional[Mapping[str, Any]] = None,
) -> List[CapabilityAssessment]:
    """Assess every key in `options` against the versioned manifest for
    `implementation`. Pure: no probe, no process, no file other than the
    (already-loaded) manifest is touched. `engine_version` is accepted for
    the day a manifest entry's `since` is a real version and this can
    compare against it; today every `since` is `null` (§06: never invent a
    minimum version), so it is not yet compared against anything.
    """
    manifest = manifest if manifest is not None else load_manifest()
    impl_options = _implementation_options(manifest, implementation)

    out: List[CapabilityAssessment] = []
    for option, requested in options.items():
        reasons: List[str] = []

        if implementation == _LLAMA_CPP_PY_IMPL and option in LLAMA_CPP_PY_OMITTED:
            out.append(CapabilityAssessment(
                option=option,
                requested=requested,
                support="unsupported",
                scope=(impl_options.get(option) or {}).get("scope", "server_start"),
                requirements=(),
                effective=Effective(),
                benefit=Benefit(),
                evidence=Evidence(kind="none"),
                reasons=("no equivalent flag in python -m llama_cpp.server",),
            ))
            continue

        spec = impl_options.get(option)
        if spec is None:
            out.append(CapabilityAssessment(
                option=option,
                requested=requested,
                support="unknown",
                # The manifest is silent, so nothing here can name a real
                # scope either; `server_start` is the common case for a
                # launch flag and is flagged as unconfirmed in `reasons`
                # rather than invented as a fact.
                scope="server_start",
                requirements=(),
                effective=Effective(),
                benefit=Benefit(),
                evidence=Evidence(kind="none"),
                reasons=(f"not in the versioned manifest for {implementation}",),
            ))
            continue

        requirements = tuple(spec.get("requires") or ())
        scope = spec.get("scope", "server_start")
        since = spec.get("since")
        support = "supported"
        for requirement in requirements:
            outcome = _check_requirement(requirement, arch=arch, gpus=gpus, options=options)
            if outcome == "fail":
                support = "unsupported"
                reasons.append(f"requirement not met: {requirement}")
            elif outcome == "unknown":
                if support == "supported":
                    support = "unknown"
                reasons.append(f"requirement cannot be verified: {requirement}")

        if since is None:
            reasons.append(
                spec.get("note")
                or f"minimum {implementation} version supporting '{option}' is not verified"
            )

        out.append(CapabilityAssessment(
            option=option,
            requested=requested,
            support=support,
            scope=scope,
            requirements=requirements,
            effective=Effective(),
            benefit=Benefit(),
            evidence=Evidence(kind="versioned_manifest"),
            reasons=tuple(reasons),
        ))
    return out


def hard_blockers(assessments: Sequence[CapabilityAssessment]) -> List[CapabilityAssessment]:
    """The `unsupported` assessments — every one of them was for an option
    the caller explicitly asked for (`assess_options` only ever assesses
    what is in the `options` mapping it was given), so this needs no extra
    filter for "was it requested"."""
    return [a for a in assessments if a.support == "unsupported"]
