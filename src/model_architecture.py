"""
src/model_architecture.py — INF-01 §A: architecture from metadata, never
from a model's name.

`get_model_architecture(repo, source)` answers the question `serve.ts`'s
`detectModelOptimizations` used to answer by matching substrings in a
repo name (`qwen3.5` → assume MoE): is this model dense, MoE, or unknown,
and does it document MTP (multi-token prediction)? Two passive sources:

  hf_config    — a single, unauthenticated GET of the model's published
                 `config.json` on the Hugging Face Hub. Read-only: no
                 `auto_map`/remote code is ever executed, no other file is
                 fetched, the response is capped in size and time (see
                 `_HF_CONFIG_TIMEOUT_S`/`_HF_CONFIG_MAX_BYTES`).
  ollama_show  — `POST /api/show` on a local Ollama, for a name that looks
                 like an Ollama tag. Same *questions* `ollama.py`'s
                 `record_from_show_payload` already answers about a model
                 (capabilities, context length) — but that module reads a
                 chat-facing capability list, not `model_info`'s
                 architecture/expert fields this needs, so this module
                 reads `/api/show` itself rather than stretching that
                 reader's contract to a second, unrelated shape.

Absence is a first-class result, never coerced into a guess: `kind` is
`"unknown"` (not `"dense"`) when nothing recognizable was read, and `mtp`
is `null` (not `false`) when no MTP field is present — the field's ABSENCE
means "not established", not "no". A network failure, a timeout, or a
privacy profile that blocks the call all produce the same honest shape:
`kind: "unknown", source: "none", note: "<why>"`, HTTP 200 — unknown is a
legitimate answer, not a 5xx.

Cached in memory (per (repo, source) key, 1h TTL) and mirrored to
`data/model_architecture_cache.json` so a restart does not force an
immediate re-read of every recently-looked-up repo; a failed lookup
(`source: "none"`) is never persisted to disk (nothing to cache — the next
call should try again).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from src.constants import DATA_DIR
from src.privacy_policy import PrivacyPolicyError, assert_outbound

logger = logging.getLogger(__name__)

CACHE_FILE = os.path.join(DATA_DIR, "model_architecture_cache.json")
CACHE_TTL_S = 3600

_HF_CONFIG_TIMEOUT_S = 8.0
_HF_CONFIG_MAX_BYTES = 256 * 1024  # 256 KB
_OLLAMA_SHOW_TIMEOUT_S = 8.0

VALID_SOURCES = ("auto", "hf", "ollama", "llamacpp")

_mem_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
_mem_lock = threading.Lock()

# no slash (an HF repo always has one: <org>/<name>); a bare tag or
# tag:variant, matching serve.ts's `looksOllamaTag` regex so the client and
# server agree on what "looks like an Ollama name" means.
_OLLAMA_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?$")


def _now_s() -> float:
    return time.time()


def _iso_age_s(observed_at: Optional[str]) -> Optional[float]:
    if not observed_at:
        return None
    try:
        dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _looks_like_ollama_tag(repo: str) -> bool:
    return "/" not in repo and bool(_OLLAMA_TAG_RE.match(repo))


def _unknown(repo: str, source: str, note: Optional[str]) -> Dict[str, Any]:
    return {
        "repo": repo,
        "kind": "unknown",
        "total_params": None,
        "active_params": None,
        "num_experts": None,
        "mtp": None,
        "architectures": None,
        "source": source,
        "observed_at": _now_iso() if source != "none" else None,
        "note": note,
    }


# ── HF config.json: passive read, classification ────────────────────────────

def _hf_config_url(repo: str) -> str:
    # Every path segment is percent-encoded on its own so a stray "/" in a
    # malformed repo string can't be smuggled into the URL as a path
    # separator (e.g. escaping into a different HF route).
    segments = "/".join(quote(part, safe="") for part in repo.split("/") if part)
    return f"https://huggingface.co/{segments}/raw/main/config.json"


def read_hf_config(
    repo: str,
    *,
    owner: str = "",
    project: Optional[Dict[str, Any]] = None,
    profile: Optional[str] = None,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Passive GET of `repo`'s `config.json` off the HF Hub. Never follows
    `auto_map`, never executes anything, never fetches a second file.
    Returns `(config, note)` — `config` is `None` on any failure, `note`
    explains why (privacy block, HTTP status, timeout, oversized, bad JSON).
    """
    url = _hf_config_url(repo)
    try:
        assert_outbound("model_architecture_hf_config", url, owner=owner, project=project, profile=profile)
    except PrivacyPolicyError:
        return None, "local-only privacy profile: HF metadata read skipped"
    try:
        with httpx.Client(timeout=_HF_CONFIG_TIMEOUT_S, follow_redirects=True) as client:
            with client.stream("GET", url) as r:
                if r.status_code != 200:
                    return None, f"HF config.json: HTTP {r.status_code}"
                content_length = r.headers.get("content-length")
                if content_length and int(content_length) > _HF_CONFIG_MAX_BYTES:
                    return None, "HF config.json exceeds the 256 KB size limit"
                chunks: List[bytes] = []
                total = 0
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if total > _HF_CONFIG_MAX_BYTES:
                        return None, "HF config.json exceeds the 256 KB size limit"
                    chunks.append(chunk)
            raw = b"".join(chunks)
        if not raw.strip():
            return None, "HF config.json: empty response"
        return json.loads(raw.decode("utf-8", "replace")), None
    except httpx.TimeoutException:
        return None, "HF config.json: timed out"
    except json.JSONDecodeError:
        return None, "HF config.json: invalid JSON"
    except Exception as e:  # noqa: BLE001 - any transport failure is "unknown", not a 5xx
        return None, f"HF config.json: {e}"


_MOE_EXPERT_KEYS = ("num_experts", "num_local_experts", "n_routed_experts")
_MTP_LAYERS_KEYS = ("num_nextn_predict_layers", "mtp_num_hidden_layers")


def classify_hf_config(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """kind/mtp/num_experts/architectures out of a raw HF `config.json`.

    `kind`: `"moe"` when an expert-count field is > 1 or `moe_intermediate_size`
    is set; `"dense"` when the config is otherwise recognizable
    (`architectures` or `model_type` present) and neither is; `"unknown"`
    when the config is empty/unparseable, or contradictory (an expert count
    of exactly 1 alongside a MoE-only field).

    `mtp`: `True` when a next-token-prediction layer count is present and
    positive, or a `use_mtp`-style flag is truthy. Absence of every such
    field leaves it `None` — never `False`: this function cannot prove a
    model does NOT do MTP, only that the field wasn't there to read.
    """
    if not isinstance(cfg, dict) or not cfg:
        return {"kind": "unknown", "mtp": None, "num_experts": None, "architectures": None, "note": "config.json missing or empty"}

    num_experts: Optional[int] = None
    for key in _MOE_EXPERT_KEYS:
        value = cfg.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            num_experts = int(value)
            break
    has_moe_size = bool(cfg.get("moe_intermediate_size"))

    architectures = cfg.get("architectures")
    architectures = list(architectures) if isinstance(architectures, list) and architectures else None
    recognizable = bool(architectures or cfg.get("model_type"))

    note: Optional[str] = None
    if num_experts == 1 and has_moe_size:
        kind = "unknown"
        note = "contradictory MoE fields (num_experts=1 but moe_intermediate_size set)"
    elif (num_experts is not None and num_experts > 1) or has_moe_size:
        kind = "moe"
    elif recognizable:
        kind = "dense"
    else:
        kind = "unknown"
        note = "config.json has no recognizable architecture fields"

    mtp: Optional[bool] = None
    for key in _MTP_LAYERS_KEYS:
        value = cfg.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            mtp = True
            break
    if mtp is None and cfg.get("use_mtp"):
        mtp = True

    return {"kind": kind, "mtp": mtp, "num_experts": num_experts, "architectures": architectures, "note": note}


def _try_hf(repo: str, *, owner: str, project: Optional[Dict[str, Any]], profile: Optional[str]) -> Dict[str, Any]:
    cfg, note = read_hf_config(repo, owner=owner, project=project, profile=profile)
    if cfg is None:
        return _unknown(repo, "none", note or "HF config.json unavailable")
    info = classify_hf_config(cfg)
    return {
        "repo": repo,
        "kind": info["kind"],
        # config.json rarely states total/active parameter counts directly
        # (they are usually derived, not declared) — left `null` rather
        # than estimated from hidden_size/layers, which would be a guess
        # dressed up as a measurement.
        "total_params": None,
        "active_params": None,
        "num_experts": info["num_experts"],
        "mtp": info["mtp"],
        "architectures": info["architectures"],
        "source": "hf_config",
        "observed_at": _now_iso(),
        "note": info["note"],
    }


# ── Ollama /api/show ─────────────────────────────────────────────────────────

def _ollama_base_url() -> str:
    base = (os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST") or "").strip()
    if not base:
        host = (os.getenv("LLM_HOST") or "127.0.0.1").strip() or "127.0.0.1"
        base = f"http://{host}:11434"
    if not base.startswith("http"):
        base = "http://" + base
    return base.rstrip("/")


def _int_by_suffix(mapping: Dict[str, Any], suffix: str) -> Optional[int]:
    """The first `*.{suffix}` value in an Ollama `model_info` mapping, e.g.
    `qwen3moe.expert_count` — the architecture prefix varies per model, so
    this matches on the suffix rather than a fixed key list."""
    for key, value in mapping.items():
        if str(key).endswith(f".{suffix}") and isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return None


def _parse_parameter_size(text: Any) -> Optional[int]:
    """Ollama's `details.parameter_size` ("27B", "397.1B", "8.0M") to an
    approximate integer — it is already an approximation as Ollama reports
    it, this only turns the string into a number, no extra precision
    implied."""
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMBT])\s*$", str(text or "").strip(), re.IGNORECASE)
    if not m:
        return None
    scale = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[m.group(2).upper()]
    try:
        return int(round(float(m.group(1)) * scale))
    except (TypeError, ValueError):
        return None


def read_ollama_show(repo: str) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """`POST /api/show` on the local Ollama. Same-machine by construction
    (the endpoint is this process's own configured Ollama, never a URL
    taken from model metadata), so this does not go through
    `assert_outbound` — it is not an egress decision, it's a local IPC one."""
    base = _ollama_base_url()
    try:
        with httpx.Client(timeout=_OLLAMA_SHOW_TIMEOUT_S) as client:
            r = client.post(f"{base}/api/show", json={"model": repo, "name": repo})
    except Exception as e:  # noqa: BLE001 - unreachable Ollama is "unknown", not an error
        return None, f"ollama /api/show unreachable: {e}"
    if r.status_code != 200:
        return None, f"ollama /api/show: HTTP {r.status_code}"
    try:
        payload = r.json()
    except Exception:
        return None, "ollama /api/show: invalid JSON"
    return (payload if isinstance(payload, dict) else {}), None


def _try_ollama(repo: str) -> Dict[str, Any]:
    payload, note = read_ollama_show(repo)
    if payload is None:
        return _unknown(repo, "none", note)
    model_info = payload.get("model_info") if isinstance(payload.get("model_info"), dict) else {}
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    expert_count = _int_by_suffix(model_info, "expert_count")
    arch_name = str(model_info.get("general.architecture") or "").strip() or None
    if expert_count is not None and expert_count > 1:
        kind = "moe"
    elif arch_name:
        kind = "dense"
    else:
        kind = "unknown"
    return {
        "repo": repo,
        "kind": kind,
        "total_params": _parse_parameter_size(details.get("parameter_size")),
        "active_params": None,
        "num_experts": expert_count,
        # /api/show does not report MTP support; absence is unknown, not "no".
        "mtp": None,
        "architectures": [arch_name] if arch_name else None,
        "source": "ollama_show",
        "observed_at": _now_iso(),
        "note": None if kind != "unknown" else "ollama /api/show did not report a recognizable architecture",
    }


def _try_llamacpp(repo: str) -> Dict[str, Any]:
    # No llama.cpp process is necessarily running for `repo` yet — the
    # architecture properties this could read (`GET /props` on a live
    # llama-server) belong to a *running instance*, not a static artifact,
    # and this endpoint answers a passive, pre-launch question. Rather than
    # fake a reading from a file format (GGUF metadata) this module does not
    # parse, source=llamacpp honestly reports "not read", same as any other
    # unresolved case (§17 principle: no fingir soporte).
    return _unknown(repo, "none", "llama.cpp architecture properties require a running server (GET /props); not available before launch")


# ── cache ─────────────────────────────────────────────────────────────────

def _cache_key(repo: str, source: str) -> str:
    return f"{source}\x00{repo}"


def _read_disk_cache() -> Dict[str, Any]:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_disk_cache(data: Dict[str, Any]) -> None:
    try:
        from core.atomic_io import atomic_write_text
        atomic_write_text(CACHE_FILE, json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001 - the in-memory cache still works
        logger.debug("model_architecture: cache write failed: %s", e)


def _get_cached(key: str) -> Optional[Dict[str, Any]]:
    with _mem_lock:
        hit = _mem_cache.get(key)
    if hit is not None and (_now_s() - hit[0]) < CACHE_TTL_S:
        return hit[1]
    disk = _read_disk_cache()
    entry = disk.get(key)
    if isinstance(entry, dict):
        age = _iso_age_s(entry.get("observed_at"))
        if age is not None and age < CACHE_TTL_S:
            with _mem_lock:
                _mem_cache[key] = (_now_s(), entry)
            return entry
    return None


def _set_cached(key: str, result: Dict[str, Any]) -> None:
    if not result.get("observed_at"):
        return  # unresolved ("none") — never cached, in memory or on disk,
        # so the next call retries instead of memoizing a network hiccup.
    with _mem_lock:
        _mem_cache[key] = (_now_s(), result)
    disk = _read_disk_cache()
    disk[key] = result
    _write_disk_cache(disk)


def reset_cache() -> None:
    """Test hook: drop the in-memory cache (the disk file is left alone)."""
    with _mem_lock:
        _mem_cache.clear()


# ── entry point ───────────────────────────────────────────────────────────

def get_model_architecture(
    repo: str,
    source: str = "auto",
    *,
    owner: str = "",
    project: Optional[Dict[str, Any]] = None,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    """`GET /api/models/architecture`'s answer for `repo`. `source` picks
    which reader to trust: `"auto"` (name-shape decides hf vs. ollama),
    `"hf"`, `"ollama"`, or `"llamacpp"` (always `unknown` today — see
    `_try_llamacpp`). Cached 1h per (repo, source); a cache hit never
    re-issues the network call `--profile`/`owner` would have gated anyway.
    """
    repo = (repo or "").strip()
    if not repo:
        return _unknown("", "none", "no repo given")
    src = (source or "auto").strip().lower()
    if src not in VALID_SOURCES:
        src = "auto"

    key = _cache_key(repo, src)
    cached = _get_cached(key)
    if cached is not None:
        return cached

    if src == "llamacpp":
        result = _try_llamacpp(repo)
    elif src == "ollama":
        result = _try_ollama(repo)
    elif src == "hf":
        result = _try_hf(repo, owner=owner, project=project, profile=profile)
    else:  # auto
        if _looks_like_ollama_tag(repo):
            result = _try_ollama(repo)
            # A bare name is usually an Ollama tag, but HF does allow a
            # handful of single-segment repo ids (e.g. "gpt2") — worth one
            # fallback read when Ollama had nothing to say.
            if result["source"] == "none":
                result = _try_hf(repo, owner=owner, project=project, profile=profile)
        else:
            result = _try_hf(repo, owner=owner, project=project, profile=profile)

    _set_cached(key, result)
    return result
