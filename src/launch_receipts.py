"""
src/launch_receipts.py — INF-02 §07: "requested → translated → accepted by
the process → observed in execution", kept as one durable record per serve
session rather than reconstructed from logs after the fact.

Two operations, cleanly split by what they are allowed to do:

  record()  synchronous, no I/O beyond the receipt file itself. Called right
            after a serve launch with exactly what INF-01 already computed
            (`requested_cmd`/`final_cmd`/`rewrites`) plus what INF-02 added
            (`plan`, `assessments`). Bumps `engine.generation` and files the
            previous receipt into `history` when this session already had
            one — a restart does not erase what was observed before it.

  verify()  the only place in this module that touches the network, and even
            then only with passive, read-only probes (§06: "verifying is
            reading"). Every probe has a 5 s timeout; a probe that cannot be
            reached produces `verify_state: "failed"` and never invents an
            observed value to fill the gap. `authorized_probe=True` is the
            one path that sends anything resembling a real request (a
            1-token chat completion) and it is off unless the caller asks
            for it explicitly, every single call.

Storage: `DATA_DIR/launch_receipts/<session_id>.json`, written with
`core.atomic_io.atomic_write_text` so a crash mid-write cannot corrupt a
record two different endpoints (record/verify) may both touch. The file
holds `{"receipt": <LaunchReceipt.to_dict()>, "history": [...]}` — `history`
is storage bookkeeping, not part of the `LaunchReceipt` contract itself, so
call sites get a single canonical shape either way.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlparse

import httpx

from core.atomic_io import atomic_write_text
from src.constants import DATA_DIR
from src.contracts.base import now_iso
from src.contracts.inference import (
    Check, CapabilityAssessment, Difference, EngineIdentity, Effective,
    Evidence, LaunchReceipt, ModelDescriptor, RewriteStep,
)
from src.model_capability_readers.llamacpp import _limits_from_props, _props_params

logger = logging.getLogger(__name__)

RECEIPTS_DIR = os.path.join(DATA_DIR, "launch_receipts")

#: A launch session id, exactly as INF-01 mints it (`f"serve-{uuid4().hex[:8]}"`)
#: — validated before it becomes part of a filename so nothing can path-traverse
#: out of RECEIPTS_DIR.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

#: How many previous generations to keep in `history` per session. Unbounded
#: growth would turn a restart-happy session into an ever-growing file; this
#: is generous enough that no realistic debugging session loses anything it
#: would actually go looking for.
MAX_HISTORY = 20

_PROBE_TIMEOUT_S = 5.0


class LaunchReceiptError(Exception):
    """Raised for a receipt operation that cannot proceed — no receipt for
    that session, or a corrupt receipt file. Callers (the route layer) map
    this to a 404, never invent a receipt to paper over it."""


def _validate_session_id(session_id: str) -> str:
    if not session_id or not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"invalid session_id: {session_id!r}")
    return session_id


def _receipt_path(session_id: str) -> str:
    return os.path.join(RECEIPTS_DIR, f"{_validate_session_id(session_id)}.json")


def _read_raw(session_id: str) -> Optional[Dict[str, Any]]:
    path = _receipt_path(session_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        raise LaunchReceiptError(f"launch receipt for {session_id!r} is unreadable: {e}") from e
    return data if isinstance(data, dict) else None


def _write_raw(session_id: str, payload: Dict[str, Any]) -> None:
    os.makedirs(RECEIPTS_DIR, exist_ok=True)
    atomic_write_text(_receipt_path(session_id), json.dumps(payload, ensure_ascii=False, indent=2))


def get(session_id: str) -> Optional[LaunchReceipt]:
    """The current (latest-generation) receipt for `session_id`, or `None`
    if this session never recorded one."""
    raw = _read_raw(session_id)
    if raw is None:
        return None
    receipt_raw = raw.get("receipt")
    if receipt_raw is None:
        return None
    return LaunchReceipt.parse(receipt_raw, "receipt")


def find_by_endpoint(host: Optional[str], port: Optional[int]) -> Optional[LaunchReceipt]:
    """The newest receipt whose `engine.host`/`engine.port` match, or `None`
    if no managed launch ever recorded one for this endpoint.

    Receipts are stored one file per `session_id`, not indexed by endpoint —
    INF-03 needs "was THIS host:port launched by Faustus" for a turn's
    `EngineIdentity` (`managed="faustus"` plus the confirmed implementation,
    versus an unverified `"external"` guess), and that is rare/cheap enough
    (a handful of concurrent local launches, not thousands) that a directory
    scan beats maintaining a second index that could drift from the receipt
    files themselves. Best-effort: an unreadable receipt is skipped, never
    raised — this is a courtesy lookup for a metrics label, not a source of
    truth callers depend on being complete."""
    if not host or not port:
        return None
    try:
        names = [n for n in os.listdir(RECEIPTS_DIR) if n.endswith(".json")]
    except OSError:
        return None
    best: Optional[LaunchReceipt] = None
    for name in names:
        session_id = name[:-len(".json")]
        try:
            receipt = get(session_id)
        except LaunchReceiptError:
            continue
        if receipt is None or receipt.engine.host != host or receipt.engine.port != port:
            continue
        if best is None or (receipt.created_at or "") > (best.created_at or ""):
            best = receipt
    return best


def identity_for_endpoint(
    endpoint_url: str, *, implementation_hint: Optional[str] = None,
) -> Optional[EngineIdentity]:
    """`EngineIdentity` for a turn's execution metrics (INF-03).

    A Faustus-managed launch's receipt, matched by host:port, wins outright
    — it carries the confirmed implementation/version/session_id INF-01/02
    already verified, not a guess. Failing that, `implementation_hint` (what
    the SSE stream itself told the caller — currently only `"ollama"`, from
    `engine_timings["source"]` in `src/llm_core.py`) becomes an `"external"`
    identity. Any other hint — including `"llamacpp"`, which cannot
    distinguish the native `llama-server` binary from the `llama_cpp.server`
    Python wrapper without a receipt (§06 H04) — returns `None` rather than
    guess at one of `IMPLEMENTATIONS`'s two llama.cpp entries; so does a
    cloud OpenAI-compatible endpoint, which has no vocabulary entry at all.
    """
    try:
        parsed = urlparse(endpoint_url or "")
    except ValueError:
        return None
    host = parsed.hostname or None
    port = parsed.port
    if host and port:
        try:
            receipt = find_by_endpoint(host, port)
        except Exception:  # noqa: BLE001 - a metrics label must never break a turn
            receipt = None
        if receipt is not None:
            return receipt.engine
    if implementation_hint != "ollama":
        return None
    return EngineIdentity(implementation="ollama", host=host, port=port, managed="external")


def record(
    session_id: str,
    *,
    engine: EngineIdentity,
    model: Optional[ModelDescriptor],
    requested_cmd: str,
    final_cmd: str,
    rewrites: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
    assessments: Sequence[CapabilityAssessment],
) -> LaunchReceipt:
    """File a new receipt for a serve attempt. `engine.generation` is
    ignored from the caller and recomputed here (previous generation + 1, or
    1 for a session's first launch) — the caller building it fresh every
    launch would either have to know the file's contents already or guess,
    and guessing a generation number is exactly the kind of invented fact
    this module exists to avoid.
    """
    _validate_session_id(session_id)
    previous = _read_raw(session_id)
    history: List[Dict[str, Any]] = []
    next_generation = 1
    if previous is not None:
        prev_receipt = previous.get("receipt")
        if isinstance(prev_receipt, dict):
            prev_engine = prev_receipt.get("engine") or {}
            prev_generation = prev_engine.get("generation")
            if isinstance(prev_generation, int) and prev_generation > 0:
                next_generation = prev_generation + 1
            history = list(previous.get("history") or [])
            history.append(prev_receipt)
            history = history[-MAX_HISTORY:]

    engine = replace(engine, generation=next_generation, session_id=session_id)
    rewrite_steps = tuple(
        RewriteStep.parse(item, f"rewrites[{i}]") for i, item in enumerate(rewrites or ())
    )
    receipt = LaunchReceipt(
        session_id=session_id,
        engine=engine,
        model=model,
        requested_cmd=requested_cmd or "",
        final_cmd=final_cmd or "",
        rewrites=rewrite_steps,
        plan=dict(plan or {}),
        assessments=tuple(assessments or ()),
        observed={},
        differences=(),
        checks=(),
        created_at=now_iso(),
        verified_at=None,
        verify_state="pending",
    )
    _write_raw(session_id, {"receipt": receipt.to_dict(), "history": history})
    return receipt


def mark_stale(session_id: str, reason: str) -> Optional[LaunchReceipt]:
    """Mark the current receipt `stale` — INF-01 §D already knows when
    `final_cmd` changes between generations (a binary/model swap); this is
    what it should call when that happens, so a client reading an old
    receipt sees that it no longer certifies the running process."""
    raw = _read_raw(session_id)
    if raw is None or not isinstance(raw.get("receipt"), dict):
        return None
    receipt = LaunchReceipt.parse(raw["receipt"], "receipt")
    checks = tuple(c for c in receipt.checks if c.name != "freshness") + (
        Check(name="freshness", state="failed", detail=reason or "marked stale"),
    )
    updated = replace(receipt, verify_state="stale", checks=checks)
    _write_raw(session_id, {"receipt": updated.to_dict(), "history": raw.get("history") or []})
    return updated


# ── verification: passive probes only ───────────────────────────────────────

def _clip_observed(payload: Any) -> Dict[str, Any]:
    """Keep `observed` inside the contract's 32 KB cap — a probe answers
    with whatever the engine sends, and this must never grow a receipt file
    without bound. Cropping (not refusing) the payload is deliberate: a
    receipt that says "here is as much as we kept" is more useful than one
    that discarded a slightly-too-large payload wholesale."""
    from src.contracts.inference import MAX_OBSERVED_BYTES
    if not isinstance(payload, dict):
        return {}
    encoded = json.dumps(payload)
    if len(encoded.encode("utf-8")) <= MAX_OBSERVED_BYTES:
        return payload
    return {"_truncated": True, "_original_bytes": len(encoded.encode("utf-8"))}


async def _get_json(client: httpx.AsyncClient, url: str) -> tuple[Optional[Any], Optional[str]]:
    try:
        resp = await client.get(url, timeout=_PROBE_TIMEOUT_S)
    except httpx.HTTPError as e:
        return None, f"{url}: {e}"
    if resp.status_code != 200:
        return None, f"{url}: HTTP {resp.status_code}"
    try:
        return resp.json(), None
    except ValueError:
        return None, f"{url}: invalid JSON"


async def _post_json(client: httpx.AsyncClient, url: str, body: Mapping[str, Any]) -> tuple[Optional[Any], Optional[str]]:
    try:
        resp = await client.post(url, json=dict(body), timeout=_PROBE_TIMEOUT_S)
    except httpx.HTTPError as e:
        return None, f"{url}: {e}"
    if resp.status_code != 200:
        return None, f"{url}: HTTP {resp.status_code}"
    try:
        return resp.json(), None
    except ValueError:
        return None, f"{url}: invalid JSON"


def _norm_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("on", "true", "1", "yes", "enabled"):
            return True
        if low in ("off", "false", "0", "no", "disabled"):
            return False
    return None


def _values_match(requested: Any, observed: Any) -> bool:
    """Loose-but-honest comparison for one option's requested vs. observed
    value: numbers compare numerically (`"8192"` vs `8192` is a match), the
    on/off vocabulary compares as booleans, everything else compares as
    stripped, case-insensitive text. Never a source of a false `confirmed`
    for genuinely different values."""
    if requested is None or observed is None:
        return False
    r_bool, o_bool = _norm_bool(requested), _norm_bool(observed)
    if r_bool is not None and o_bool is not None:
        return r_bool == o_bool
    try:
        return float(requested) == float(observed)
    except (TypeError, ValueError):
        pass
    return str(requested).strip().lower() == str(observed).strip().lower()


async def _probe_llama(
    client: httpx.AsyncClient, base_url: str, *, has_props: bool,
) -> tuple[Dict[str, Any], Dict[str, Any], Optional[str], bool]:
    """Shared probe for `llama-server` and `llama_cpp.server`. Returns
    (observed_fields, raw_for_receipt, error, model_listed). `has_props` is
    False for `llama_cpp.server` — the wrapper does not expose `/props`, so
    every option for it stays `unconfirmed` (§A4) and this only checks
    `/v1/models`.
    """
    observed: Dict[str, Any] = {}
    raw: Dict[str, Any] = {}
    error: Optional[str] = None
    model_listed = False

    if has_props:
        props, props_err = await _get_json(client, base_url.rstrip("/") + "/props")
        if props_err:
            return observed, raw, props_err, model_listed
        raw["props"] = props
        props_map = props if isinstance(props, dict) else {}
        params = _props_params(props_map)
        limits = _limits_from_props(props_map)
        if limits.get("context_tokens") is not None:
            observed["ctx"] = limits["context_tokens"]
        if limits.get("parallel_slots") is not None:
            observed["parallel"] = limits["parallel_slots"]
        if "flash_attn" in params:
            observed["flash_attn"] = params.get("flash_attn")
        if "cache_type_k" in params:
            observed["cache_type_k"] = params.get("cache_type_k")
        if "cache_type_v" in params:
            observed["cache_type_v"] = params.get("cache_type_v")
        model_path = props_map.get("model_path")
        if model_path:
            observed["_model_path"] = model_path
            model_listed = True
        build_info = props_map.get("build_info") or props_map.get("version")
        if build_info:
            observed["_build_info"] = build_info

        slots, slots_err = await _get_json(client, base_url.rstrip("/") + "/slots")
        if not slots_err:
            raw["slots"] = slots

    models_payload, models_err = await _get_json(client, base_url.rstrip("/") + "/v1/models")
    if not models_err:
        raw["models"] = models_payload
        items = (models_payload or {}).get("data") if isinstance(models_payload, dict) else None
        if isinstance(items, list) and items:
            model_listed = True
    elif not has_props:
        # llama_cpp.server has nothing else to try — no /v1/models means
        # unreachable, full stop.
        error = models_err

    return observed, raw, error, model_listed


async def _probe_ollama(
    client: httpx.AsyncClient, base_url: str, *, model_name: str,
) -> tuple[Dict[str, Any], Dict[str, Any], Optional[str], bool]:
    observed: Dict[str, Any] = {}
    raw: Dict[str, Any] = {}
    model_listed = False

    version_payload, version_err = await _get_json(client, base_url.rstrip("/") + "/api/version")
    if version_err:
        return observed, raw, version_err, model_listed
    raw["version"] = version_payload

    ps_payload, ps_err = await _get_json(client, base_url.rstrip("/") + "/api/ps")
    if not ps_err:
        raw["ps"] = ps_payload
        models = (ps_payload or {}).get("models") if isinstance(ps_payload, dict) else None
        if isinstance(models, list):
            for entry in models:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or entry.get("model") or "")
                if model_name and name and model_name not in name and name not in model_name:
                    continue
                model_listed = True
                ctx_len = entry.get("context_length")
                if ctx_len is not None:
                    observed["num_ctx"] = ctx_len
                break

    if model_name:
        show_payload, show_err = await _post_json(
            client, base_url.rstrip("/") + "/api/show", {"model": model_name, "name": model_name},
        )
        if not show_err:
            raw["show"] = show_payload
            model_listed = True

    return observed, raw, None, model_listed


async def _probe_openai_like(
    client: httpx.AsyncClient, base_url: str, *, try_version: bool,
) -> tuple[Dict[str, Any], Dict[str, Any], Optional[str], bool]:
    """`vllm`/`sglang`: `GET /v1/models` (+ `GET /version` for vllm). Neither
    exposes runtime option state, so `observed` stays empty — every assessed
    option for these implementations stays `unconfirmed`."""
    raw: Dict[str, Any] = {}
    models_payload, models_err = await _get_json(client, base_url.rstrip("/") + "/v1/models")
    if models_err:
        return {}, raw, models_err, False
    raw["models"] = models_payload
    items = (models_payload or {}).get("data") if isinstance(models_payload, dict) else None
    model_listed = bool(isinstance(items, list) and items)
    if try_version:
        version_payload, version_err = await _get_json(client, base_url.rstrip("/") + "/version")
        if not version_err:
            raw["version"] = version_payload
    return {}, raw, None, model_listed


_OPTION_OBSERVATION_KEY = {
    # canonical option name -> key in the `observed` dict produced above.
    "ctx": "ctx",
    "parallel": "parallel",
    "flash_attn": "flash_attn",
    "cache_type_k": "cache_type_k",
    "cache_type_v": "cache_type_v",
    "num_ctx": "num_ctx",
}


def _model_name_from_plan(plan: Mapping[str, Any]) -> str:
    model = plan.get("model")
    if isinstance(model, str):
        return model
    if isinstance(model, Mapping):
        return str(model.get("artifact_id") or model.get("name") or "")
    return ""


async def verify(
    session_id: str,
    *,
    base_url: str,
    authorized_probe: bool = False,
) -> LaunchReceipt:
    """Passive verification of the receipt already on file for
    `session_id`. Every probe is read-only and time-boxed at 5 s; nothing
    here starts, stops or reconfigures anything. Raises `LaunchReceiptError`
    if there is no receipt to verify (the route layer turns that into a 404,
    same as `get()` returning `None` does for a plain read).
    """
    _validate_session_id(session_id)
    receipt = get(session_id)
    if receipt is None:
        raise LaunchReceiptError(f"no launch receipt for session {session_id!r}")

    implementation = receipt.engine.implementation
    plan_options = receipt.plan.get("options") if isinstance(receipt.plan, dict) else None
    plan_options = plan_options if isinstance(plan_options, dict) else {}
    model_name = _model_name_from_plan(receipt.plan)

    observed: Dict[str, Any] = {}
    raw_for_receipt: Dict[str, Any] = {}
    reach_error: Optional[str] = None
    model_listed = False

    try:
        async with httpx.AsyncClient() as client:
            if implementation == "llama-server":
                observed, raw_for_receipt, reach_error, model_listed = await _probe_llama(
                    client, base_url, has_props=True,
                )
            elif implementation == "llama_cpp.server":
                observed, raw_for_receipt, reach_error, model_listed = await _probe_llama(
                    client, base_url, has_props=False,
                )
            elif implementation == "ollama":
                observed, raw_for_receipt, reach_error, model_listed = await _probe_ollama(
                    client, base_url, model_name=model_name,
                )
            elif implementation in ("vllm", "sglang"):
                observed, raw_for_receipt, reach_error, model_listed = await _probe_openai_like(
                    client, base_url, try_version=(implementation == "vllm"),
                )
            else:
                reach_error = f"no verification probe defined for implementation {implementation!r}"
    except Exception as e:  # noqa: BLE001 - any transport surprise is "unreachable", not a 500
        reach_error = str(e)

    http_reachable_state = "failed" if reach_error else "passed"
    checks: List[Check] = [Check(name="http_reachable", state=http_reachable_state, detail=reach_error or "")]

    if reach_error:
        # §06: a failed probe never invents values. The receipt keeps its
        # existing assessments untouched (still whatever `assess_options`
        # said pre-launch) and simply cannot add effective-state evidence.
        checks.append(Check(name="model_listed", state="skipped", detail="engine unreachable"))
        checks.append(Check(
            name="chat_probe", state="skipped",
            detail="not authorized" if not authorized_probe else "engine unreachable",
        ))
        updated = replace(
            receipt,
            observed={},
            checks=tuple(checks),
            verified_at=now_iso(),
            verify_state="failed",
        )
        _persist(session_id, updated)
        return updated

    checks.append(Check(name="model_listed", state="passed" if model_listed else "failed",
                         detail="" if model_listed else "requested model not found in probe response"))

    if authorized_probe:
        chat_state, chat_detail = await _run_chat_probe(implementation, base_url, model_name)
        checks.append(Check(name="chat_probe", state=chat_state, detail=chat_detail))
    else:
        checks.append(Check(name="chat_probe", state="skipped", detail="not authorized"))

    verified_at = now_iso()
    new_assessments: List[CapabilityAssessment] = []
    differences: List[Difference] = []
    for assessment in receipt.assessments:
        obs_key = _OPTION_OBSERVATION_KEY.get(assessment.option)
        obs_value = observed.get(obs_key) if obs_key else None
        requested_value = plan_options.get(assessment.option, assessment.requested)
        evidence = assessment.evidence
        if obs_value is None:
            effective = Effective(value=None, state="unconfirmed")
        elif _values_match(requested_value, obs_value):
            effective = Effective(value=obs_value, state="confirmed")
            # A probe actually answered this option now -- that supersedes
            # (does not erase) the pre-launch manifest evidence backing
            # `support`; §06's "evidence" is about the strongest thing that
            # backs the CURRENT claim, and an observed value beats a manifest
            # entry every time.
            evidence = Evidence(kind="engine_probe", observed_at=verified_at)
        else:
            effective = Effective(value=obs_value, state="mismatch")
            evidence = Evidence(kind="engine_probe", observed_at=verified_at)
            differences.append(Difference(
                option=assessment.option, requested=requested_value,
                observed=obs_value, state="mismatch",
            ))
        new_assessments.append(replace(assessment, effective=effective, evidence=evidence))

    updated = replace(
        receipt,
        assessments=tuple(new_assessments),
        observed=_clip_observed(raw_for_receipt),
        differences=tuple(differences),
        checks=tuple(checks),
        verified_at=verified_at,
        verify_state="verified",
    )
    _persist(session_id, updated)
    return updated


async def _run_chat_probe(implementation: str, base_url: str, model_name: str) -> tuple[str, str]:
    """The one path that sends anything resembling a real inference call —
    a single-token completion, only when `authorized_probe=True` was passed
    explicitly for this call. `HTTP 200` here proves the endpoint answers a
    minimal request, nothing about vision/tools/structured output (§06)."""
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
            if implementation == "ollama":
                payload = {"model": model_name, "messages": [{"role": "user", "content": "hi"}],
                           "stream": False, "options": {"num_predict": 1}}
                resp = await client.post(base_url.rstrip("/") + "/api/chat", json=payload)
            else:
                payload = {"model": model_name or "default", "max_tokens": 1,
                           "messages": [{"role": "user", "content": "hi"}]}
                resp = await client.post(base_url.rstrip("/") + "/v1/chat/completions", json=payload)
    except httpx.HTTPError as e:
        return "failed", str(e)
    if resp.status_code != 200:
        return "failed", f"HTTP {resp.status_code}"
    return "passed", ""


def _persist(session_id: str, receipt: LaunchReceipt) -> None:
    raw = _read_raw(session_id) or {}
    history = raw.get("history") or []
    _write_raw(session_id, {"receipt": receipt.to_dict(), "history": history})
