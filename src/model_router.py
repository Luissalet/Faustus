"""src/model_router.py — measured router for local models (MOD-05).

`docs/spec/v2/MAPA_P1.md`'s own audit of MOD-05 ("router por capacidad/
calidad observada/latencia; ver cada escalado; nunca pasar a pago
silenciosamente") is explicit about what was still missing after the
privacy-transitivity half of the ID closed: "el enrutamiento por capacidad
... no está aquí — el routing por capacidad/calidad observada/latencia del
título del ID (elegir modelo/backend, no solo permitir o no salir) ... ya
vive en `src/execution_router.py` (ajeno, sin auditar contra MOD-05)". That
statement is not quite right either: `src/execution_router.py` picks a
SANDBOX BACKEND for a skill manifest (docker vs. host); it has never chosen
between LLM models. This module is the piece that was actually absent —
picking WHICH installed local model answers a turn, from evidence this
install has actually collected, never from a name heuristic.

Three kinds of fact feed a decision, never blended, same discipline as
`src/model_calibration.py`'s own module docstring insists on for
announced-vs-tested:

  - PROVEN capability (`src.model_calibration.get_manifest(...)["tested"]`) —
    a real probe against a resident model, weighted highest.
  - DECLARED capability (`get_manifest(...)["announced"]["capabilities"]`,
    built by `src.model_capabilities`' vocabulary) — a fallback signal, only
    consulted when nothing has been tested, weighted lower.
  - THIS INSTALL'S OWN HISTORY (`DATA_DIR/model_router_stats.json`,
    `record_outcome`) — ok/fail counts and an EWMA latency per model, the
    same slow-moving-average discipline `src.llm_core.remember_local_speed`
    already uses for tok/s, so one bad turn cannot condemn a model for good
    and one lucky one cannot clear it either.

Every decision is appended to `DATA_DIR/model_router_log.jsonl` (rotated at
`_LOG_MAX_LINES` lines) whether or not it escalates, so "ver cada escalado"
is answerable by reading a file, not by trusting a log statement nobody
grepped for. Escalation to a paid/remote model is never automatic: `choose`
only sets `Decision.escalated=True` when `RouterConfig.allow_paid_escalation`
is on AND no local candidate qualifies AND the active privacy profile
(`src.privacy_policy.get_privacy_profile`) is not `local_only` — three
independent yeses, mirroring `src.execution_router`'s own "two independent
yeses" discipline for its one hard rule. Even then this module never PICKS a
paid model; it hands back `escalated=True` and an `escalation` dict and lets
the caller decide, so "nunca pasar a pago silenciosamente" holds by
construction: nothing here can complete a turn on a paid model without a
caller reading `escalated` and acting on it on purpose.

Integration note (see this lote's report): wiring `choose()` into the actual
per-turn model resolution in `routes/chat_routes.py` was judged too invasive
for this lote's scope — `sess.model` is read directly, not only through the
route-descriptor metadata this lote was scoped to touch, across thousands of
lines of that streaming handler. `choose()`/`explain()` are ready to be
called from there; nothing here assumes it is.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src import model_calibration
from src import privacy_policy
from src.llm_core import local_speed

logger = logging.getLogger(__name__)

# ── capability vocabulary (MOD-05's own, distinct from model_calibration's
# TEST_* probe names and model_capabilities' CAP_* declared names) ──────────
CAP_TOOL_CALL = "tool_call"
CAP_JSON_MODE = "json_mode"
CAP_VISION = "vision"
CAP_REASONING = "reasoning"
VALID_CAPABILITIES: Tuple[str, ...] = (CAP_TOOL_CALL, CAP_JSON_MODE, CAP_VISION, CAP_REASONING)

# Maps a MOD-05 capability name to the calibration manifest's `tested` key
# (`src.model_calibration.TEST_*`) that proves it, or None when no probe for
# it exists yet (e.g. "reasoning" has no dedicated calibration probe today —
# see model_calibration.py's TEST_KEYS).
_TESTED_KEY_FOR_CAPABILITY: Dict[str, Optional[str]] = {
    CAP_TOOL_CALL: model_calibration.TEST_TOOL_CALLING,
    CAP_JSON_MODE: model_calibration.TEST_JSON_MODE,
    CAP_VISION: model_calibration.TEST_VISION,
    CAP_REASONING: None,
}
# Maps the same capability to the key `model_calibration.announced_from_ollama`
# uses inside `announced["capabilities"]` (built from `src.model_capabilities`'
# CAP_TOOL_CALL/CAP_VISION/CAP_REASONING vocabulary). `announced_from_ollama`
# never fills a "json_mode" flag, so that one has no declared fallback.
_ANNOUNCED_KEY_FOR_CAPABILITY: Dict[str, Optional[str]] = {
    CAP_TOOL_CALL: "tools",
    CAP_JSON_MODE: None,
    CAP_VISION: "vision",
    CAP_REASONING: "reasoning",
}

_LOG_MAX_LINES = 2000

# RLock, not Lock: `update_router_config` holds `_config_lock` for its whole
# read-modify-write and calls `get_router_config` (which also takes the
# lock) to get the base it merges onto -- a plain Lock would self-deadlock
# on that nested acquisition from the same thread.
_config_lock = threading.RLock()
_stats_lock = threading.Lock()
_log_lock = threading.Lock()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_data_dir() -> str:
    # Imported inside the function, not at module load, so tests that
    # monkeypatch `src.constants.DATA_DIR` (the single source of truth --
    # see that module's own docstring) are picked up on the next call
    # rather than frozen at import time.
    from src.constants import DATA_DIR
    return DATA_DIR


# ── RouterConfig: DATA_DIR/model_router_config.json ─────────────────────────
#
# Not read through `src/settings.py`: that module validates every
# `update_settings` patch against `DEFAULT_SETTINGS`, a file outside this
# lote's ownership, and adding an entry there would be exactly the kind of
# unscoped edit the contract asks every lote to avoid. This mirrors the
# sibling lote's own choice for `src/openrouter_options.py`
# (`DATA_DIR/openrouter_endpoints.json`) and this module's own
# `model_router_stats.json` / `model_router_log.jsonl`: one small dedicated
# store, atomic writes, same discipline as `src/model_calibration.py`.

@dataclass(frozen=True)
class RouterConfig:
    """Settings for MOD-05. Defaults are the inert ones: `enabled=False`
    means `choose()` is never invoked in a wired caller and nothing about
    today's behavior changes; `allow_paid_escalation=False` means a turn
    with no qualifying local model gets `escalated=False` and a `None`
    model rather than a silent paid completion."""

    enabled: bool = False
    prefer_local: bool = True
    max_latency_s: Optional[float] = None
    allow_paid_escalation: bool = False
    candidates: Tuple[str, ...] = ()
    min_capabilities: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "prefer_local": self.prefer_local,
            "max_latency_s": self.max_latency_s,
            "allow_paid_escalation": self.allow_paid_escalation,
            "candidates": list(self.candidates),
            "min_capabilities": list(self.min_capabilities),
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "RouterConfig":
        return _validate_config(dict(data or {}))


def _validate_config(patch: Mapping[str, Any], base: Optional[RouterConfig] = None) -> RouterConfig:
    """Merge `patch` onto `base` (or the inert defaults), validated field by
    field. Raises `ValueError` with a message naming the bad field -- never
    a partial write, never a silently-ignored key."""
    cur = dict((base or RouterConfig()).to_dict())
    for key, value in dict(patch or {}).items():
        if key not in cur:
            raise ValueError(f"unknown model_router setting {key!r}")
        if key in ("enabled", "prefer_local", "allow_paid_escalation"):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a bool")
        elif key == "max_latency_s":
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ValueError("max_latency_s must be a number or null")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0:
                raise ValueError("max_latency_s must be > 0")
        elif key == "candidates":
            if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                raise ValueError("candidates must be a list of non-empty strings")
        elif key == "min_capabilities":
            if not isinstance(value, list) or not all(v in VALID_CAPABILITIES for v in value):
                raise ValueError(f"min_capabilities must be a subset of {VALID_CAPABILITIES}")
        cur[key] = value
    return RouterConfig(
        enabled=bool(cur["enabled"]),
        prefer_local=bool(cur["prefer_local"]),
        max_latency_s=(float(cur["max_latency_s"]) if cur["max_latency_s"] is not None else None),
        allow_paid_escalation=bool(cur["allow_paid_escalation"]),
        candidates=tuple(cur["candidates"]),
        min_capabilities=tuple(cur["min_capabilities"]),
    )


def _config_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(data_dir or _default_data_dir(), "model_router_config.json")


def get_router_config(data_dir: Optional[str] = None) -> RouterConfig:
    """The stored config, or the inert defaults. A corrupt file degrades to
    defaults (quarantined, same convention as `model_calibration.py` and
    `model_capabilities.py`) rather than raising out of a read path."""
    path = _config_path(data_dir)
    with _config_lock:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError("wrong shape")
        except FileNotFoundError:
            return RouterConfig()
        except (ValueError, OSError):
            try:
                os.replace(path, path + ".corrupt")
                logger.warning("model_router_config.json was corrupt; moved to .corrupt")
            except OSError:
                pass
            return RouterConfig()
    try:
        return _validate_config(raw)
    except ValueError as exc:
        logger.warning("model_router_config.json failed validation (%s); using defaults", exc)
        return RouterConfig()


def update_router_config(patch: Mapping[str, Any], *, data_dir: Optional[str] = None) -> RouterConfig:
    """Validated merge-write. Raises `ValueError` (never writes) on a bad
    patch -- the route layer turns that into a flat 4xx body."""
    from core.atomic_io import atomic_write_json
    with _config_lock:
        current = get_router_config(data_dir=data_dir)
        merged = _validate_config(patch, base=current)
        path = _config_path(data_dir)
        atomic_write_json(path, merged.to_dict(), indent=2)
        return merged


# ── Requirements / Scored / Decision ────────────────────────────────────────

@dataclass(frozen=True)
class Requirements:
    """What THIS turn needs, independent of what the operator's config
    always requires (`RouterConfig.min_capabilities` -- the two are unioned
    in `score_candidates`)."""

    capabilities: Tuple[str, ...] = ()
    max_latency_s: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"capabilities": list(self.capabilities), "max_latency_s": self.max_latency_s}

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "Requirements":
        data = dict(data or {})
        raw_caps = data.get("capabilities")
        caps = tuple(
            c for c in (raw_caps if isinstance(raw_caps, list) else [])
            if isinstance(c, str) and c in VALID_CAPABILITIES
        )
        raw_latency = data.get("max_latency_s")
        latency = raw_latency if isinstance(raw_latency, (int, float)) and not isinstance(raw_latency, bool) else None
        return cls(capabilities=caps, max_latency_s=latency)


@dataclass(frozen=True)
class Scored:
    """One candidate's score, and the human-readable receipts behind it
    (`why`) -- so a decision can be explained without re-deriving it."""

    model: str
    score: float
    why: Tuple[str, ...]
    meets_requirements: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "score": self.score,
            "why": list(self.why),
            "meets_requirements": self.meets_requirements,
        }


@dataclass(frozen=True)
class Decision:
    """`model=None` means "no local model was chosen this call" -- that is
    either because nothing qualified and escalation stayed off/blocked, or
    because `installed`/`candidates` was empty. It is never a paid model:
    a paid completion needs a human-visible `escalated=True` on THIS
    object, read and acted on by the caller on purpose."""

    model: Optional[str]
    reason: str
    alternatives: Tuple[Scored, ...]
    escalated: bool
    escalation: Optional[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "reason": self.reason,
            "alternatives": [s.to_dict() for s in self.alternatives],
            "escalated": self.escalated,
            "escalation": self.escalation,
        }


def _capability_status(manifest: Mapping[str, Any], capability: str) -> Tuple[Optional[bool], bool]:
    """`(tested_ok, declared)` for `capability` against one manifest
    (`model_calibration.get_manifest`'s return shape). `tested_ok` is
    True/False/None (proven-works / proven-fails / never probed); `declared`
    is the lower-weight announced-capabilities fallback."""
    tested = manifest.get("tested") if isinstance(manifest, Mapping) else None
    tested = tested if isinstance(tested, Mapping) else {}
    announced = manifest.get("announced") if isinstance(manifest, Mapping) else None
    announced = announced if isinstance(announced, Mapping) else {}
    caps = announced.get("capabilities")
    caps = caps if isinstance(caps, Mapping) else {}

    tested_key = _TESTED_KEY_FOR_CAPABILITY.get(capability)
    tested_result = tested.get(tested_key) if tested_key else None
    tested_ok = tested_result.get("ok") if isinstance(tested_result, Mapping) else None
    if tested_ok is not True and tested_ok is not False:
        tested_ok = None

    announced_key = _ANNOUNCED_KEY_FOR_CAPABILITY.get(capability)
    declared = bool(caps.get(announced_key)) if announced_key else False
    return tested_ok, declared


def score_candidates(
    requirements: Requirements,
    candidates: Sequence[str],
    *,
    config: Optional[RouterConfig] = None,
    data_dir: Optional[str] = None,
) -> List[Scored]:
    """Score every candidate against `requirements` (unioned with
    `config.min_capabilities`), from manifest evidence
    (`model_calibration.get_manifest`), measured speed
    (`src.llm_core.local_speed`) and this install's own outcome history
    (`read_stats`). Sorted best-first (score descending, model name as a
    deterministic tiebreak). No I/O is ever written here -- reads only."""
    config = config or RouterConfig()
    required_caps: Tuple[str, ...] = tuple(dict.fromkeys(tuple(config.min_capabilities) + tuple(requirements.capabilities)))
    max_latency = requirements.max_latency_s if requirements.max_latency_s is not None else config.max_latency_s
    stats = read_stats(data_dir=data_dir)

    out: List[Scored] = []
    for model in candidates:
        why: List[str] = []
        score = 0.0
        missing: List[str] = []

        key = model_calibration.manifest_key(vendor="ollama", model_id=model)
        manifest = model_calibration.get_manifest(key, data_dir=data_dir)
        for cap in required_caps:
            tested_ok, declared = _capability_status(manifest, cap)
            if tested_ok is True:
                score += 3.0
                why.append(f"{cap} probado")
            elif tested_ok is False:
                missing.append(cap)
                why.append(f"{cap} probado y falla")
            elif declared:
                score += 1.0
                why.append(f"{cap} declarado (no probado)")
            else:
                missing.append(cap)
                why.append(f"{cap} desconocido")

        tps = local_speed(model)
        if tps:
            score += min(tps / 20.0, 3.0)
            why.append(f"{tps:.0f} tok/s medidos")
        else:
            why.append("velocidad desconocida")

        entry = stats.get(model) or {}
        ok_n = int(entry.get("ok") or 0)
        fail_n = int(entry.get("fail") or 0)
        if ok_n or fail_n:
            rate = ok_n / (ok_n + fail_n)
            score += rate * 2.0
            why.append(f"{ok_n} ok / {fail_n} fallos recientes")
            if fail_n and entry.get("last_error_class"):
                why.append(f"último error: {entry['last_error_class']}")
        else:
            why.append("sin historial")

        latency_ok = True
        ewma = entry.get("ewma_latency_s")
        if max_latency is not None and isinstance(ewma, (int, float)):
            if ewma > max_latency:
                latency_ok = False
                why.append(f"latencia media {ewma:.1f}s excede el máximo {max_latency}s")

        meets = (not missing) and latency_ok
        if not meets:
            # Kept in the list (transparency: alternatives always shows every
            # candidate) but pushed below anything that qualifies.
            score -= 1000.0
        out.append(Scored(model=model, score=round(score, 3), why=tuple(why), meets_requirements=meets))

    out.sort(key=lambda s: (-s.score, s.model))
    return out


def installed_local_models(*, timeout: float = 3.0) -> List[str]:
    """The model tags the local Ollama actually has right now (`/api/tags`),
    via `gpu_policy.model_sizes` (cached ~2 min). Empty when Ollama is not
    reachable -- the router then has no local candidate and says so, rather
    than guessing from a catalogue. Callers that already know the installed
    set (a test, a preview with an explicit list) pass it and skip this."""
    try:
        from routes.system_usage_routes import _ollama_base
        from src.gpu_policy import model_sizes
        return sorted(model_sizes(_ollama_base(), timeout=timeout).keys())
    except Exception:  # noqa: BLE001 - discovery is best-effort
        logger.debug("model_router: could not list installed models", exc_info=True)
        return []


def choose(
    requirements: Requirements,
    *,
    installed: Sequence[str],
    config: RouterConfig,
    requested: Optional[str] = None,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    project: Optional[Dict[str, Any]] = None,
    data_dir: Optional[str] = None,
    log: bool = True,
) -> Decision:
    """Pick a local model for `requirements`, or explain why none was
    picked. Never proposes a model that isn't in `installed`: when
    `config.candidates` is set it is first intersected with `installed`, so
    a stale/misconfigured candidate list can only narrow, never invent, what
    gets offered.

    Escalation (`Decision.escalated=True`) requires ALL of: no local
    candidate meets `requirements`, `config.allow_paid_escalation`, and the
    active privacy profile (`src.privacy_policy.get_privacy_profile`) is not
    `local_only`. Even then this function never names a paid model -- see
    the module docstring."""
    installed = list(installed or [])
    if config.candidates:
        candidates = [c for c in config.candidates if c in installed]
    else:
        candidates = list(installed)

    scored = score_candidates(requirements, candidates, config=config, data_dir=data_dir)
    usable = [s for s in scored if s.meets_requirements]

    chosen: Optional[str] = None
    escalated = False
    escalation: Optional[Dict[str, Any]] = None

    if usable:
        top = usable[0]
        chosen = top.model
        reason = "mejor candidato local" + (f": {', '.join(top.why)}" if top.why else "")
    else:
        reason = "ningún modelo local cumple los requisitos"
        if not candidates:
            reason += " (sin candidatos instalados)"
        if config.allow_paid_escalation:
            profile = privacy_policy.get_privacy_profile(project=project, owner=owner)
            if profile == privacy_policy.PROFILE_LOCAL_ONLY:
                reason += "; escalado a pago bloqueado por política de privacidad local-only"
            else:
                escalated = True
                escalation = {
                    "reason": reason,
                    "requirements": requirements.to_dict(),
                    "privacy_profile": profile,
                }
                reason += "; escalado a pago (requiere que el caller confirme explícitamente, nunca automático)"
        else:
            reason += "; escalado a pago deshabilitado (allow_paid_escalation=False)"

    decision = Decision(model=chosen, reason=reason, alternatives=tuple(scored), escalated=escalated, escalation=escalation)

    if log:
        try:
            _append_log(
                {
                    "ts": _utcnow_iso(),
                    "session_id": session_id,
                    "requested": requested,
                    "chosen": decision.model,
                    "reason": decision.reason,
                    "escalated": decision.escalated,
                    "candidates": [{"model": s.model, "score": s.score, "why": list(s.why)} for s in scored],
                },
                data_dir=data_dir,
            )
        except Exception as exc:  # noqa: BLE001 - logging must never break a decision
            logger.debug("model_router: failed to append decision log: %s", exc)

    return decision


def explain(decision: Decision) -> str:
    """A one-line, human-readable receipt for `decision` -- meant to go into
    the transcript as a system note (SSE `model_router` event) when a caller
    wires `choose()` in."""
    if decision.model:
        top = next((s for s in decision.alternatives if s.model == decision.model), None)
        detail = f" ({', '.join(top.why)})" if top and top.why else ""
        return f"Modelo elegido: {decision.model}{detail}"
    if decision.escalated:
        return f"Sin modelo local adecuado — {decision.reason}"
    return f"Sin modelo disponible — {decision.reason}"


# ── outcome history: DATA_DIR/model_router_stats.json ──────────────────────

def _stats_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(data_dir or _default_data_dir(), "model_router_stats.json")


def _load_stats_locked(data_dir: Optional[str] = None) -> Dict[str, Any]:
    path = _stats_path(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("wrong shape")
        return data
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        try:
            os.replace(path, path + ".corrupt")
            logger.warning("model_router_stats.json was corrupt; moved to .corrupt")
        except OSError:
            pass
        return {}


def read_stats(data_dir: Optional[str] = None) -> Dict[str, Any]:
    """Every model's `{ok, fail, ewma_latency_s, last_error_class,
    updated_at}`, or an empty dict for a model never recorded."""
    with _stats_lock:
        return dict(_load_stats_locked(data_dir))


def record_outcome(
    model: str,
    ok: bool,
    latency_s: Optional[float] = None,
    error_class: Optional[str] = None,
    *,
    data_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Fold one turn's outcome into `model`'s running record. `latency_s`
    updates an EWMA (0.7 old / 0.3 new, same slow-moving-average constant
    `src.llm_core.remember_local_speed` already uses for tok/s) rather than
    a simple average, so one slow outlier cannot dominate the figure and one
    fast one cannot erase a real slowdown."""
    from core.atomic_io import atomic_write_json
    if not model:
        raise ValueError("model is required")
    with _stats_lock:
        data = _load_stats_locked(data_dir)
        entry = dict(data.get(model) or {"ok": 0, "fail": 0, "ewma_latency_s": None, "last_error_class": None})
        if ok:
            entry["ok"] = int(entry.get("ok") or 0) + 1
            entry["last_error_class"] = None
        else:
            entry["fail"] = int(entry.get("fail") or 0) + 1
            if error_class:
                entry["last_error_class"] = str(error_class)
        if isinstance(latency_s, (int, float)) and not isinstance(latency_s, bool) and latency_s >= 0:
            prev = entry.get("ewma_latency_s")
            entry["ewma_latency_s"] = (
                round(float(latency_s), 3) if not isinstance(prev, (int, float))
                else round(prev * 0.7 + latency_s * 0.3, 3)
            )
        entry["updated_at"] = _utcnow_iso()
        data[model] = entry
        atomic_write_json(_stats_path(data_dir), data, indent=2)
        return dict(entry)


# ── decision log: DATA_DIR/model_router_log.jsonl (rotated) ────────────────

def _log_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(data_dir or _default_data_dir(), "model_router_log.jsonl")


def _append_log(entry: Dict[str, Any], *, data_dir: Optional[str] = None) -> None:
    from core.atomic_io import atomic_write_text
    path = _log_path(data_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with _log_lock:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        except FileNotFoundError:
            lines = []
        lines.append(json.dumps(entry, ensure_ascii=False))
        if len(lines) > _LOG_MAX_LINES:
            lines = lines[-_LOG_MAX_LINES:]
        atomic_write_text(path, "\n".join(lines) + "\n")


def read_log(limit: int = 50, *, data_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """The most recent `limit` decisions, newest first. A line that failed
    to parse (a half-written file from outside this module, say) is skipped
    rather than raising -- a log reader must never itself become the reason
    a turn fails."""
    path = _log_path(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except FileNotFoundError:
        return []
    out: List[Dict[str, Any]] = []
    for ln in reversed(lines):
        if limit and len(out) >= limit:
            break
        try:
            parsed = json.loads(ln)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out
