"""hardware_profiles.py — HW-05: measured hardware profiles, never guessed.

A profile is an AGGREGATION of things this codebase already measures — GPU /
VRAM / RAM from ``services.hwfit.hardware.detect_system`` (the same cached
detector the model picker and hwfit ranker already use), disk from
``shutil.disk_usage``, and per-model decode / KV-cache speed learned from
real replies (``src.llm_core.remember_local_speed``, ``src.vram_fit.
KV_RATES``) — never a benchmark started here. Building a profile costs one
(cached) hardware probe and a couple of dict reads; nothing in this module
loads a model, starts a generation, or runs a timed probe of its own. That is
deliberate: HW-05's acceptance criterion is "picking a model must not launch
a heavy benchmark that blocks the desktop", and the way this module keeps
that true is by never being ABLE to start one, not by a flag someone could
forget to check.

Saved profiles live in one small JSON file under DATA_DIR so the selector can
compare "this machine, right now" against a profile captured earlier, or one
exported from another machine and imported here — measured numbers only: a
model this process never decoded a reply from has no entry, never a guessed
one (see ``compare_profiles``' explicit ``a_measured``/``b_measured`` flags).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()


def _path() -> str:
    try:
        from src.constants import DATA_DIR
    except Exception:  # pragma: no cover - mirrors src/scorecard.py's fallback
        DATA_DIR = os.path.join(os.getcwd(), "data")
    return os.path.join(DATA_DIR, "hardware_profiles.json")


def _disk_snapshot() -> Optional[Dict[str, Any]]:
    """Local disk only — a remote host's disk is not something
    ``shutil.disk_usage`` can read, and profiling one over SSH is not this
    module's job (HW-04's per-endpoint free-space check already does that
    for the Ollama models directory specifically)."""
    try:
        from src.constants import DATA_DIR
        target = DATA_DIR if os.path.isdir(DATA_DIR) else os.getcwd()
    except Exception:  # noqa: BLE001
        target = os.getcwd()
    try:
        usage = shutil.disk_usage(target)
        return {"path": target, "total_bytes": usage.total, "used_bytes": usage.used,
               "free_bytes": usage.free}
    except OSError as e:  # noqa: BLE001 - a profile with no disk info beats a crash
        return {"path": target, "error": str(e)[:200]}


def _measured_model_speeds() -> List[Dict[str, Any]]:
    """Only models THIS process has actually decoded a reply from — reused,
    not reimplemented: ``llm_core.remember_local_speed`` is the only writer
    of this figure anywhere in the codebase."""
    try:
        from src import llm_core
        speeds = dict(getattr(llm_core, "_LOCAL_SPEED", {}) or {})
    except Exception as e:  # noqa: BLE001
        logger.debug("hardware_profiles: local speed unavailable: %s", e)
        speeds = {}
    return [
        {"model": model, "tokens_per_second": data.get("tps"), "samples": int(data.get("samples") or 0)}
        for model, data in sorted(speeds.items())
    ]


def _measured_kv_rates() -> List[Dict[str, Any]]:
    """Same reuse for the KV-cache-bytes-per-token figure HW-01/HW-03's
    admission gate learns while a model is resident (``vram_fit.
    remember_kv_rate``)."""
    try:
        from src import vram_fit
        rates = dict(vram_fit.KV_RATES or {})
    except Exception as e:  # noqa: BLE001
        logger.debug("hardware_profiles: kv rates unavailable: %s", e)
        rates = {}
    return [
        {"key": key, "bytes_per_token": data.get("per_token"), "measured_ctx": data.get("ctx")}
        for key, data in sorted(rates.items())
    ]


def collect_profile(*, host: str = "", ssh_port: str = "", label: str = "") -> Dict[str, Any]:
    """This machine's measured profile. `host`/`ssh_port` build one for a
    REMOTE machine the exact same way the hwfit picker already can — no
    second detector, no code path here that touches the network beyond what
    ``detect_system`` already does (and caches)."""
    from services.hwfit.hardware import detect_system
    system = detect_system(host=host, ssh_port=ssh_port)
    return {
        "id": uuid.uuid4().hex[:12],
        "label": label or (host or "this machine"),
        "host": host or None,
        "captured_at": time.time(),
        "system": system,
        # Per-model speed/KV figures are per-process knowledge, meaningless
        # for a machine this process never ran a model on.
        "disk": _disk_snapshot() if not host else None,
        "model_speeds": _measured_model_speeds() if not host else [],
        "kv_rates": _measured_kv_rates() if not host else [],
    }


# ── storage: one small JSON file, same shape as src/scorecard.py's log ─────


def _load_all() -> Dict[str, Dict[str, Any]]:
    path = _path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        logger.debug("hardware_profiles: load failed: %s", e)
        return {}


def _save_all(data: Dict[str, Dict[str, Any]]) -> bool:
    path = _path()
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        return True
    except OSError as e:  # noqa: BLE001
        logger.debug("hardware_profiles: save failed: %s", e)
        return False
    finally:
        try:
            os.unlink(tmp)  # no-op after a successful os.replace
        except OSError:
            pass


def save_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a captured profile under its id so it can be compared later,
    or exported and imported on another machine."""
    all_profiles = _load_all()
    all_profiles[profile["id"]] = profile
    _save_all(all_profiles)
    return profile


def list_profiles() -> List[Dict[str, Any]]:
    return sorted(_load_all().values(), key=lambda p: p.get("captured_at", 0), reverse=True)


def get_profile(profile_id: str) -> Optional[Dict[str, Any]]:
    return _load_all().get(str(profile_id or ""))


def delete_profile(profile_id: str) -> bool:
    all_profiles = _load_all()
    if str(profile_id or "") not in all_profiles:
        return False
    all_profiles.pop(str(profile_id), None)
    return _save_all(all_profiles)


def export_profile(profile_id: str) -> Optional[Dict[str, Any]]:
    """The stored profile, JSON-ready for download/import elsewhere. Kept as
    its own name (identical today to ``get_profile``) so a future export
    envelope — a checksum, a schema tag — does not have to change what
    storage looks like too."""
    return get_profile(profile_id)


def import_profile(data: Dict[str, Any]) -> Dict[str, Any]:
    """A profile exported from this or another machine, re-saved with a
    fresh id so importing twice never collides with (or silently
    overwrites) the original."""
    if not isinstance(data, dict) or not data.get("system"):
        raise ValueError("not a hardware profile export")
    profile = dict(data)
    profile["id"] = uuid.uuid4().hex[:12]
    profile["imported_at"] = time.time()
    return save_profile(profile)


def compare_profiles(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Side-by-side diff for the selector: GPU/VRAM/RAM/disk headline
    numbers, plus which models have a MEASURED tok/s on each side. Never
    fabricates a number for the side that has none — a model absent from a
    profile's ``model_speeds`` stays ``None``/``measured: False`` on that
    side, exactly HW-05's "warn about missing measurements, never invent
    tokens/s" acceptance criterion."""
    def headline(p: Dict[str, Any]) -> Dict[str, Any]:
        sysinfo = p.get("system") or {}
        return {
            "gpu_name": sysinfo.get("gpu_name"),
            "gpu_vram_gb": sysinfo.get("gpu_vram_gb"),
            "gpu_count": sysinfo.get("gpu_count") or (1 if sysinfo.get("has_gpu") else 0),
            "total_ram_gb": sysinfo.get("total_ram_gb"),
            "disk_free_bytes": (p.get("disk") or {}).get("free_bytes"),
        }

    a_speeds = {row["model"]: row.get("tokens_per_second") for row in a.get("model_speeds") or []}
    b_speeds = {row["model"]: row.get("tokens_per_second") for row in b.get("model_speeds") or []}
    models = sorted(set(a_speeds) | set(b_speeds))
    return {
        "a": {"id": a.get("id"), "label": a.get("label"), "headline": headline(a)},
        "b": {"id": b.get("id"), "label": b.get("label"), "headline": headline(b)},
        "models": [
            {
                "model": m,
                "a_tokens_per_second": a_speeds.get(m), "a_measured": m in a_speeds,
                "b_tokens_per_second": b_speeds.get(m), "b_measured": m in b_speeds,
            }
            for m in models
        ],
    }
