"""src/engines.py — the local inference engine (llama.cpp's `llama-server`),
managed from the UI instead of an out-of-band script on the owner's machine.

Reuses `src.launch_profiles` for storage/validation/spawn/idempotency rather
than building a second process manager: an "engine" IS a launch profile of
`kind == "process"` whose executable is `llama-server` (or `llama-server.exe`
on Windows) — the same "a local process with an executable, args, cwd and a
health URL" shape that module already models. This layer only adds the
llama.cpp-specific pieces launch_profiles knows nothing about:

  * structured fields (model path, context size, port, host, extra flags)
    read from / written into that profile's `argv` and `readiness.url`,
    so the Settings UI can offer real fields instead of a raw argv editor;
  * a port-busy refusal BEFORE any spawn is attempted, naming whoever
    already holds the port (`process_center.pid_listening_on`) — plain
    `launch_profiles.launch()` would instead read that as "already
    running" and quietly no-op, which is right for its own use cases but
    wrong here: a stale, unrelated process on 8081 must block a start, not
    look like success;
  * a VRAM admission check before spawning (`gpu_shared_memory.vram_
    snapshot`, the same system-wide reading `vram_admission.assess()`
    itself falls back to) — real GPU memory in use already reflects
    whatever another engine or Ollama runner holds, so this never has to
    enumerate other runners itself; unsupported/unknown never blocks
    (ignorance is not a reason to stop anyone, same rule `assess()` uses);
  * status merged from BOTH the process/port reading (`launch_profiles.
    status`) and the real health probe (`runner_providers.probe_llama_
    cpp`) — "running" always means the health probe answered ok, never
    just "a pid exists" or "something listens on the port".

Starting is always explicit and human-initiated (every mutating/launching
function here is only ever reached through `routes/engine_routes.py`,
gated `require_human` exactly like `routes/connector_routes.py`'s launch-
profile routes) — nothing in this module runs on import, on a schedule, or
because Faustus started up.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from src import launch_profiles

logger = logging.getLogger(__name__)

#: Same "is this executable llama.cpp's server" test on every platform this
#: repo targets — a bare basename comparison, case-insensitive, with or
#: without the Windows `.exe` suffix.
_ENGINE_EXE_NAMES = ("llama-server", "llama-server.exe")

_MODEL_FLAGS = ("-m", "--model")
_CTX_FLAGS = ("-c", "--ctx-size", "--n-ctx")
_PORT_FLAGS = ("--port",)
_HOST_FLAGS = ("--host",)
_SPEC_TYPE_FLAGS = ("--spec-type",)
_SPEC_DRAFT_N_MAX_FLAGS = ("--spec-draft-n-max",)
_PARALLEL_FLAGS = ("-np", "--parallel")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_CTX = 4096
DEFAULT_MTP_DRAFT_N_MAX = 2
MTP_DRAFT_N_MAX_MIN = 1
MTP_DRAFT_N_MAX_MAX = 8

#: MTP's draft head (the extra `nextn`/`blk.N.nextn.*` tensors a Qwen3.x
#: GGUF ships) stays resident on top of the base model's own footprint.
#: Observed cost is roughly 1.5-2 GB; use the higher end as a fixed
#: headroom constant so admission_check stays conservative rather than
#: exact (VRAM use also depends on context size / batch, which it already
#: doesn't model precisely for the base weights either).
MTP_VRAM_HEADROOM_BYTES = 2 * 1024 * 1024 * 1024


class EngineValidationError(ValueError):
    pass


# ── argv <-> structured fields ──────────────────────────────────────────────

def _flag_value(argv: List[str], names: tuple) -> Optional[str]:
    for i, tok in enumerate(argv):
        for name in names:
            if tok == name and i + 1 < len(argv):
                return argv[i + 1]
            if tok.startswith(name + "="):
                return tok.split("=", 1)[1]
    return None


def _is_engine_executable(executable: str) -> bool:
    # Either separator: a Windows path must be recognised on any host.
    base = str(executable or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base in _ENGINE_EXE_NAMES


def is_engine_profile(profile: Dict[str, Any]) -> bool:
    return profile.get("kind") == "process" and _is_engine_executable(profile.get("executable") or "")


def _parse_fields(profile: Dict[str, Any]) -> Dict[str, Any]:
    """The engine-shaped view of a launch profile: model path, context size,
    host/port and whatever flags are not one of the four fields the form
    edits directly (those are "extra flags", shown back so nothing the
    owner already had in a hand-written profile is silently dropped)."""
    argv = list(profile.get("argv") or [])
    model_path = _flag_value(argv, _MODEL_FLAGS) or ""
    ctx_raw = _flag_value(argv, _CTX_FLAGS)
    try:
        ctx_size = int(ctx_raw) if ctx_raw is not None else DEFAULT_CTX
    except (TypeError, ValueError):
        ctx_size = DEFAULT_CTX
    host = _flag_value(argv, _HOST_FLAGS) or DEFAULT_HOST
    port_raw = _flag_value(argv, _PORT_FLAGS)
    readiness_url = (profile.get("readiness") or {}).get("url") or ""
    port: Optional[int] = None
    if port_raw is not None:
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            port = None
    if port is None and readiness_url:
        try:
            port = urlsplit(readiness_url).port
        except Exception:  # noqa: BLE001
            port = None

    spec_type = _flag_value(argv, _SPEC_TYPE_FLAGS)
    mtp = spec_type == "draft-mtp"
    n_max_raw = _flag_value(argv, _SPEC_DRAFT_N_MAX_FLAGS)
    try:
        mtp_draft_n_max = int(n_max_raw) if n_max_raw is not None else DEFAULT_MTP_DRAFT_N_MAX
    except (TypeError, ValueError):
        mtp_draft_n_max = DEFAULT_MTP_DRAFT_N_MAX

    # The MTP flags are structured (managed by `mtp`/`mtp_draft_n_max`
    # below) and so excluded from "extra args" the same way model/ctx/port/
    # host are. `-np`/`--parallel` is NOT: it stays a plain extra flag
    # (round-trips through extra_args untouched) — only its current value
    # is surfaced via `parallel` for the UI's "parallel slots cancel most
    # of the MTP gain" hint.
    known = (set(_MODEL_FLAGS) | set(_CTX_FLAGS) | set(_PORT_FLAGS) | set(_HOST_FLAGS)
             | set(_SPEC_TYPE_FLAGS) | set(_SPEC_DRAFT_N_MAX_FLAGS))
    extra: List[str] = []
    skip_next = False
    for tok in argv:
        if skip_next:
            skip_next = False
            continue
        if tok in known or any(tok.startswith(k + "=") for k in known):
            skip_next = tok in known and "=" not in tok
            continue
        extra.append(tok)

    parallel_raw = _flag_value(argv, _PARALLEL_FLAGS)
    parallel: Optional[int] = None
    if parallel_raw is not None:
        try:
            parallel = int(parallel_raw)
        except (TypeError, ValueError):
            parallel = None

    return {
        "model_path": model_path,
        "ctx_size": ctx_size,
        "host": host,
        "port": port,
        "extra_args": extra,
        "mtp": mtp,
        "mtp_draft_n_max": mtp_draft_n_max,
        "parallel": parallel,
    }


def _build_argv(*, model_path: str, ctx_size: int, port: int, host: str,
                 extra_args: Optional[List[str]] = None,
                 mtp: bool = False, mtp_draft_n_max: int = DEFAULT_MTP_DRAFT_N_MAX) -> List[str]:
    argv = ["-m", model_path, "-c", str(int(ctx_size)), "--port", str(int(port)), "--host", host]
    if mtp:
        argv.extend(["--spec-type", "draft-mtp", "--spec-draft-n-max", str(int(mtp_draft_n_max))])
    argv.extend(extra_args or [])
    return argv


def mtp_supported_for(model_path: str) -> Optional[bool]:
    """Does this GGUF ship the `nextn`/MTP draft-head layers MTP needs?
    None when unknown (file missing/unreadable) — unknown never blocks."""
    if not model_path or not os.path.isfile(model_path):
        return None
    from src import gguf_meta
    layers = gguf_meta.mtp_layers(model_path)
    if layers is None:
        return None
    return layers > 0


def _decorate(profile: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(profile)
    fields = _parse_fields(profile)
    out.update(fields)
    out["mtp_supported"] = mtp_supported_for(fields.get("model_path") or "")
    return out


# ── validation ───────────────────────────────────────────────────────────────

def _validate_fields(*, name: str, executable: str, model_path: str, ctx_size: Any,
                      port: Any, host: str, mtp: bool = False,
                      mtp_draft_n_max: Any = DEFAULT_MTP_DRAFT_N_MAX) -> List[str]:
    reasons: List[str] = []
    if not str(name or "").strip():
        reasons.append("name is required")
    if not _is_engine_executable(executable):
        reasons.append(f"executable must be named one of {_ENGINE_EXE_NAMES}")
    if not model_path or not os.path.isabs(str(model_path)):
        reasons.append("model_path must be an absolute path")
    elif not os.path.isfile(model_path):
        reasons.append(f"model file not found: {model_path}")
    try:
        if int(ctx_size) <= 0:
            reasons.append("ctx_size must be a positive integer")
    except (TypeError, ValueError):
        reasons.append("ctx_size must be a positive integer")
    try:
        p = int(port)
        if not (1 <= p <= 65535):
            reasons.append("port must be between 1 and 65535")
    except (TypeError, ValueError):
        reasons.append("port must be an integer")
    if not str(host or "").strip():
        reasons.append("host is required")
    try:
        n_max = int(mtp_draft_n_max)
        if not (MTP_DRAFT_N_MAX_MIN <= n_max <= MTP_DRAFT_N_MAX_MAX):
            reasons.append(f"mtp_draft_n_max must be between {MTP_DRAFT_N_MAX_MIN} and {MTP_DRAFT_N_MAX_MAX}")
    except (TypeError, ValueError):
        reasons.append("mtp_draft_n_max must be an integer")
    if mtp and model_path and os.path.isabs(str(model_path)) and os.path.isfile(model_path):
        supported = mtp_supported_for(model_path)
        if supported is False:
            reasons.append(
                "mtp requires a GGUF with MTP/nextn draft-head layers "
                "(e.g. Qwen3.x); this model does not ship any"
            )
    return reasons


# ── CRUD ─────────────────────────────────────────────────────────────────────

def list_engines() -> List[Dict[str, Any]]:
    return [_decorate(p) for p in launch_profiles.list_profiles() if is_engine_profile(p)]


def get_engine(engine_id: str) -> Optional[Dict[str, Any]]:
    profile = launch_profiles.get_profile(engine_id)
    if profile is None or not is_engine_profile(profile):
        return None
    return _decorate(profile)


def create_engine(*, owner: Optional[str], name: str, executable: str, model_path: str,
                   ctx_size: int = DEFAULT_CTX, port: int, host: str = DEFAULT_HOST,
                   extra_args: Optional[List[str]] = None,
                   mtp: bool = False, mtp_draft_n_max: int = DEFAULT_MTP_DRAFT_N_MAX,
                   description: Optional[str] = None) -> Dict[str, Any]:
    reasons = _validate_fields(name=name, executable=executable, model_path=model_path,
                                ctx_size=ctx_size, port=port, host=host,
                                mtp=mtp, mtp_draft_n_max=mtp_draft_n_max)
    if reasons:
        raise EngineValidationError("; ".join(reasons))
    argv = _build_argv(model_path=model_path, ctx_size=ctx_size, port=port, host=host,
                        extra_args=extra_args, mtp=mtp, mtp_draft_n_max=mtp_draft_n_max)
    cwd = os.path.dirname(executable) or "."
    readiness = {"url": f"http://{host}:{int(port)}/health", "timeout_s": 30}
    try:
        profile = launch_profiles.create_profile(
            owner=owner, name=name, kind="process", executable=executable,
            argv=argv, cwd=cwd, readiness=readiness,
            description=description or "Local inference engine (llama.cpp)",
            desktop=False,
        )
    except launch_profiles.ProfileValidationError as exc:
        raise EngineValidationError(str(exc)) from exc
    return _decorate(profile)


def update_engine(engine_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    current = get_engine(engine_id)
    if current is None:
        return None
    merged = {
        "name": fields.get("name", current["name"]),
        "executable": fields.get("executable", current["executable"]),
        "model_path": fields.get("model_path", current["model_path"]),
        "ctx_size": fields.get("ctx_size", current["ctx_size"]),
        "port": fields.get("port", current["port"]),
        "host": fields.get("host", current["host"]),
        "extra_args": fields.get("extra_args", current["extra_args"]),
        "mtp": fields.get("mtp", current.get("mtp", False)),
        "mtp_draft_n_max": fields.get("mtp_draft_n_max", current.get("mtp_draft_n_max", DEFAULT_MTP_DRAFT_N_MAX)),
        "description": fields.get("description", current.get("description")),
    }
    reasons = _validate_fields(name=merged["name"], executable=merged["executable"],
                                model_path=merged["model_path"], ctx_size=merged["ctx_size"],
                                port=merged["port"], host=merged["host"],
                                mtp=merged["mtp"], mtp_draft_n_max=merged["mtp_draft_n_max"])
    if reasons:
        raise EngineValidationError("; ".join(reasons))
    argv = _build_argv(model_path=merged["model_path"], ctx_size=merged["ctx_size"],
                        port=merged["port"], host=merged["host"], extra_args=merged["extra_args"],
                        mtp=merged["mtp"], mtp_draft_n_max=merged["mtp_draft_n_max"])
    readiness = {"url": f"http://{merged['host']}:{int(merged['port'])}/health", "timeout_s": 30}
    try:
        profile = launch_profiles.update_profile(
            engine_id, name=merged["name"], executable=merged["executable"], argv=argv,
            cwd=os.path.dirname(merged["executable"]) or ".", readiness=readiness,
            description=merged["description"],
        )
    except launch_profiles.ProfileValidationError as exc:
        raise EngineValidationError(str(exc)) from exc
    if profile is None:
        return None
    return _decorate(profile)


def delete_engine(engine_id: str) -> bool:
    if get_engine(engine_id) is None:
        return False
    return launch_profiles.delete_profile(engine_id)


# ── admission (VRAM) ─────────────────────────────────────────────────────────

def _gguf_size(model_path: str) -> Optional[int]:
    try:
        if model_path and os.path.isfile(model_path):
            return os.path.getsize(model_path)
    except OSError:
        return None
    return None


def admission_check(model_path: str, *, mtp: bool = False) -> Dict[str, Any]:
    """Does the model this engine would load fit next to whatever else is
    already resident? Reuses the SAME system-wide VRAM reading
    `vram_admission.assess()` falls back to (`gpu_shared_memory.
    vram_snapshot`) — real nvidia-smi usage already accounts for every other
    engine or runner holding a card, so nothing here has to enumerate them.
    `fits: None` (no GPU reading, or the model file is unreadable so its
    footprint is unknown) never blocks a start — ignorance is not a reason
    to refuse. When `mtp` is on, `MTP_VRAM_HEADROOM_BYTES` is added to the
    needed footprint for the resident draft-head layers."""
    out: Dict[str, Any] = {"fits": None, "needed_bytes": None, "free_bytes": None}
    needed = _gguf_size(model_path)
    if needed and mtp:
        needed += MTP_VRAM_HEADROOM_BYTES
    out["needed_bytes"] = needed
    if not needed:
        return out
    try:
        from src import gpu_shared_memory
        vram = gpu_shared_memory.vram_snapshot()
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"VRAM reading failed: {exc}"
        return out
    if not vram.get("supported"):
        out["reason"] = str(vram.get("reason") or "no GPU reading")
        return out
    free = int(vram.get("free") or 0)
    out["free_bytes"] = free
    out["fits"] = free >= needed
    if not out["fits"]:
        out["reason"] = (
            f"Not enough free VRAM: this model needs ~{needed / 1e9:.1f} GB, "
            f"only {free / 1e9:.1f} GB free — another engine or runner may already hold it."
        )
    return out


# ── start / stop / status ────────────────────────────────────────────────────

async def start_engine(engine_id: str, *, request_client_host: Optional[str] = None) -> Dict[str, Any]:
    engine = get_engine(engine_id)
    if engine is None:
        return {"started": False, "error": f"no such engine: {engine_id}"}
    port = engine.get("port")
    if not port:
        return {"started": False, "error": "engine has no port configured"}

    from src import process_center
    held = process_center.pid_listening_on(int(port))
    if held is not None:
        return {
            "started": False,
            "error": f"port {port} is already in use",
            "held_by": held,
        }

    admission = admission_check(engine.get("model_path") or "", mtp=bool(engine.get("mtp")))
    if admission.get("fits") is False:
        return {"started": False, "error": admission.get("reason"), "admission": admission}

    result = await launch_profiles.launch(engine_id, request_client_host=request_client_host)
    out = dict(result)
    out["started"] = bool(result.get("launched"))
    out["admission"] = admission
    return out


async def stop_engine(engine_id: str) -> Dict[str, Any]:
    engine = get_engine(engine_id)
    if engine is None:
        return {"ok": False, "code": "not_found", "reason": f"no such engine: {engine_id}"}
    port = engine.get("port")
    if not port:
        return {"ok": False, "code": "no_port", "reason": "engine has no port configured"}
    from src import process_center
    import asyncio
    out = await asyncio.to_thread(process_center.stop_port, int(port))
    out["stopped"] = bool(out.get("ok"))
    return out


async def status_engine(engine_id: str) -> Dict[str, Any]:
    engine = get_engine(engine_id)
    if engine is None:
        return {"state": "unknown", "error": f"no such engine: {engine_id}"}
    base = await launch_profiles.status(engine_id)
    root = f"http://{engine.get('host') or DEFAULT_HOST}:{engine.get('port')}"
    from src import runner_providers
    probe = runner_providers.probe_llama_cpp(root, force=True)
    if probe.get("healthy"):
        state = "running"
    elif probe.get("available") or base.get("running"):
        state = "unhealthy"
    else:
        state = "stopped"
    out: Dict[str, Any] = {
        "state": state,
        "root": root,
        "pid": base.get("pid"),
        "pid_command": base.get("pid_command"),
        "model": probe.get("model") or "",
        "context_length": probe.get("context_length") or 0,
        "footprint_bytes": probe.get("footprint_bytes"),
        "footprint_measured": bool(probe.get("footprint_measured")),
        "generating": bool(probe.get("generating")),
        "configured_model_path": engine.get("model_path"),
        "configured_ctx_size": engine.get("ctx_size"),
    }
    return out


async def verify_engine(engine_id: str) -> Dict[str, Any]:
    """`--version` for a quick "this binary runs at all" check, or a health
    probe when the engine is already up. Never starts anything."""
    engine = get_engine(engine_id)
    if engine is None:
        return {"ok": False, "error": f"no such engine: {engine_id}"}
    port = engine.get("port")
    if port:
        from src import process_center
        held = process_center.pid_listening_on(int(port))
        if held is not None:
            root = f"http://{engine.get('host') or DEFAULT_HOST}:{port}"
            from src import runner_providers
            probe = runner_providers.probe_llama_cpp(root, force=True)
            return {"ok": bool(probe.get("healthy")), "mode": "health", "probe": probe, "held_by": held}

    executable = engine.get("executable") or ""
    if not os.path.isfile(executable):
        return {"ok": False, "mode": "version", "error": f"executable not found: {executable}"}
    import asyncio
    import subprocess

    def _run() -> Dict[str, Any]:
        try:
            proc = subprocess.run(
                [executable, "--version"], capture_output=True, text=True, timeout=8,
            )
            output = ((proc.stdout or "") + (proc.stderr or "")).strip()[:2000]
            return {"ok": proc.returncode == 0, "mode": "version", "output": output}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "mode": "version", "error": str(exc)}

    return await asyncio.to_thread(_run)


async def discover_from_port(port: int, *, host: str = DEFAULT_HOST) -> Dict[str, Any]:
    """F1.4 in reverse: something is already listening on `port` — probe
    `/props` for the model path and context size it reports, so the "create
    a config from what's running" flow never asks the owner to retype what
    the running server already knows. Read-only; creates nothing."""
    root = f"http://{host}:{int(port)}"
    import httpx
    from src.tls_overrides import llm_verify
    out: Dict[str, Any] = {"port": port, "host": host, "found": False}
    try:
        async with httpx.AsyncClient(timeout=2.0, verify=llm_verify(), trust_env=False) as client:
            resp = await client.get(root + "/props")
        if resp.status_code == 200:
            body = resp.json()
            out["found"] = True
            out["model_path"] = str(body.get("model_path") or "")
            # Current llama-server builds report the context only inside
            # `default_generation_settings`; older ones at the top level.
            dgs = body.get("default_generation_settings") or {}
            n_ctx = body.get("n_ctx") or (dgs.get("n_ctx") if isinstance(dgs, dict) else None)
            out["ctx_size"] = int(n_ctx or 0) or None
            if body.get("model_alias"):
                out["name"] = str(body["model_alias"])
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    # The server's own command line fills what /props cannot say: the
    # executable and every other flag it was started with, so adopting a
    # server started by hand keeps it running exactly as it was.
    try:
        out.update(_argv_fields(_listening_argv(int(port))))
    except Exception as exc:  # noqa: BLE001
        logger.debug("discover: no command line for port %s: %s", port, exc)
    return out


def _listening_argv(port: int) -> List[str]:
    """Full argv of the process listening on `port`, or []."""
    from src import process_center
    hit = process_center.pid_listening_on(port)
    if not hit:
        return []
    psutil = process_center._psutil()
    if psutil is None:
        return []
    return list(psutil.Process(int(hit["pid"])).cmdline() or [])


def _argv_fields(argv: List[str]) -> Dict[str, Any]:
    """Split a llama-server argv into the structured fields and the rest.

    Only an argv whose program is llama-server counts. The structured flags
    (model, context, port, host) and the MTP pair are taken out; everything
    else is kept in order as `extra_args`, one token per entry, the shape
    `_build_argv` appends back."""
    if not argv or not _is_engine_executable(argv[0]):
        return {}
    out: Dict[str, Any] = {"executable": argv[0]}
    structured = set(_MODEL_FLAGS + _CTX_FLAGS + _PORT_FLAGS + _HOST_FLAGS)
    extra: List[str] = []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in structured or tok in _SPEC_TYPE_FLAGS or tok in _SPEC_DRAFT_N_MAX_FLAGS:
            value = argv[i + 1] if i + 1 < len(argv) else ""
            if tok in _SPEC_TYPE_FLAGS and value == "draft-mtp":
                out["mtp"] = True
            elif tok in _SPEC_DRAFT_N_MAX_FLAGS and value.isdigit():
                out["mtp_draft_n_max"] = int(value)
            elif tok in _CTX_FLAGS and value.isdigit():
                out.setdefault("argv_ctx_size", int(value))
            i += 2
            continue
        extra.append(tok)
        i += 1
    out["extra_args"] = extra
    return out
