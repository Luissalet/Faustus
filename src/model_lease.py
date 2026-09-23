"""src/model_lease.py — a shared lease so several instances of this app,
each with its own data dir and port, agree on one resident model instead of
fighting over the one local Ollama (and the one GPU pool) underneath them.

The problem, verified in code: `src/vram_admission.py` keeps its pins,
last-active clock and VRAM reservations in a plain in-process dict, and
`src/model_warmup.py`'s residency keeper polls `/api/ps` and re-pins its own
default with `keep_alive: -1` every few seconds. Both are correct for ONE
process. Run a second instance (a different port, a different data dir)
against the SAME Ollama and neither can see the other: instance B's keeper
pins its own default over instance A's, admission in A can tell a load
"fits" for VRAM instance B just reserved a moment ago, and A's idle sweep
can unload a model B is actively using because nothing recorded that
anywhere A could read.

This module is the one thing every instance CAN read without asking any of
the others: each writes its own small JSON file to a machine-wide directory
(independent of `DATA_DIR`, which is per-instance) and lists that directory
to see its siblings. One file per instance means no locking is needed for
the read side — a process only ever writes its own file — and a stale file
(the owning process died) is filtered out by heartbeat age and a PID check,
never acted on as if it were live.

Everything here is additive and off by default in the sense that matters
most: until `start()` has run, every `note_*` write is a no-op and every
read still answers (empty, never raising) — so every OTHER module's own
unit tests, which never call `start()`, never touch the real shared
directory. The callers in `src/vram_admission.py`, `src/model_warmup.py`
and `src/run_model_pin.py` additionally gate on `enabled()` before doing any
of the sibling lookups below, so with the lease off (or never started)
their behaviour is exactly what it was before this module existed.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}


def _canon_root(root: str) -> str:
    """Same collapsing `model_warmup._canon_root` / `vram_admission.
    _canonical_root` already do for a loopback Ollama root — a private copy
    so this module stays import-order independent of either."""
    try:
        p = urlparse(str(root or ""))
    except Exception:  # noqa: BLE001
        return str(root or "")
    host = (p.hostname or "").lower()
    if host in _LOCAL_HOSTS:
        host = "127.0.0.1"
    scheme = p.scheme or "http"
    port = p.port or 11434
    return f"{scheme}://{host}:{port}"


def _norm_model(name: str) -> str:
    m = str(name or "").strip().lower()
    return m[:-7] if m.endswith(":latest") else m


def _get_setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


# ── where the leases live ───────────────────────────────────────────────────

def shared_dir() -> str:
    """Machine-wide, independent of `DATA_DIR` (each instance has its own).
    `FAUSTUS_SHARED_DIR`/`ODYSSEUS_SHARED_DIR` wins when set (tests use this
    to point at a tmp dir); otherwise the platform's own per-user data
    location."""
    override = os.environ.get("FAUSTUS_SHARED_DIR") or os.environ.get("ODYSSEUS_SHARED_DIR")
    if override:
        return override
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Faustus", "shared")
    xdg = os.environ.get("XDG_DATA_HOME")
    base = xdg if xdg else os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "faustus", "shared")


def _leases_dir() -> str:
    return os.path.join(shared_dir(), "leases")


def _lease_path(instance_id: str) -> str:
    return os.path.join(_leases_dir(), f"{instance_id}.json")


def _read_port() -> int:
    raw = str(os.environ.get("APP_PORT", "7000") or "7000").strip()
    try:
        return int(raw)
    except ValueError:
        return 7000


def _current_id() -> str:
    """The instance id this process would use — the frozen one from `start()`
    once it has run, else computed fresh (identity is deterministic: data
    dir + port), so `siblings()` can always exclude "myself" even before
    (or without ever) starting."""
    with _LOCK:
        if _state["started"]:
            return str(_state["record"].get("instance_id") or "")
    try:
        from src.constants import DATA_DIR
    except Exception:  # noqa: BLE001
        DATA_DIR = ""
    return hashlib.sha1(f"{DATA_DIR}|{_read_port()}".encode("utf-8")).hexdigest()[:12]


# ── lifecycle ────────────────────────────────────────────────────────────────

_LOCK = threading.Lock()
_state: Dict[str, Any] = {"started": False, "record": {}, "last_write": 0.0}
_HEARTBEAT_TASK: Optional[asyncio.Task] = None


def enabled() -> bool:
    """`model_lease_enabled` (default True) AND this process actually
    called `start()`. Every integration point below checks this first —
    with it False, nothing here is ever consulted."""
    if not _state["started"]:
        return False
    return bool(_get_setting("model_lease_enabled", True))


def start() -> None:
    """Register this instance and begin heartbeating (idempotent)."""
    global _HEARTBEAT_TASK
    with _LOCK:
        if _state["started"]:
            return
        try:
            from src.constants import DATA_DIR
        except Exception:  # noqa: BLE001
            DATA_DIR = ""
        port = _read_port()
        data_dir = str(DATA_DIR or "")
        instance_id = hashlib.sha1(f"{data_dir}|{port}".encode("utf-8")).hexdigest()[:12]
        now = time.time()
        _state["record"] = {
            "instance_id": instance_id, "pid": os.getpid(), "port": port,
            "data_dir": data_dir, "started": now, "heartbeat": now,
            "default": {}, "pinned": [], "active": {}, "reservations": [],
            "adopted_model": "", "leader": False,
        }
        _state["started"] = True
        _state["last_write"] = 0.0
    _flush(force=True)
    try:
        loop = asyncio.get_running_loop()
        _HEARTBEAT_TASK = loop.create_task(_heartbeat_loop(), name="model-lease-heartbeat")
    except RuntimeError:
        # No running loop (e.g. a sync test calling start() directly) — the
        # record was still written once above; heartbeating simply never
        # starts, same as any other best-effort background task here.
        _HEARTBEAT_TASK = None


async def _heartbeat_loop() -> None:
    while True:
        interval = float(_get_setting("model_lease_heartbeat_seconds", 10) or 10)
        await asyncio.sleep(max(1.0, interval))
        if not _state["started"]:
            return
        with _LOCK:
            _state["record"]["heartbeat"] = time.time()
        _flush(force=True)


async def stop() -> None:
    """Cancel the heartbeat task and delete this instance's own file."""
    global _HEARTBEAT_TASK
    task = _HEARTBEAT_TASK
    _HEARTBEAT_TASK = None
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    path = None
    with _LOCK:
        if _state["started"]:
            path = _lease_path(str(_state["record"].get("instance_id") or ""))
        _state["started"] = False
        _state["record"] = {}
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def reset_for_tests() -> None:
    """Test-only: drop all in-memory state without touching disk or a real
    heartbeat task (tests that started one manage it themselves)."""
    global _HEARTBEAT_TASK
    with _LOCK:
        _state["started"] = False
        _state["record"] = {}
        _state["last_write"] = 0.0
    _HEARTBEAT_TASK = None


# ── the write side (own file only) ──────────────────────────────────────────

def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-lease-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _flush(force: bool = False) -> None:
    with _LOCK:
        if not _state["started"]:
            return
        now = time.time()
        if not force and now - float(_state["last_write"] or 0.0) < 1.0:
            return
        record = copy.deepcopy(_state["record"])
        _state["last_write"] = now
    try:
        _atomic_write(_lease_path(str(record.get("instance_id") or "")), record)
    except Exception as exc:  # noqa: BLE001
        logger.debug("model_lease: write failed: %s", exc)


def note_default(root: str, model: str) -> None:
    if not _state["started"] or not root or not model:
        return
    with _LOCK:
        _state["record"]["default"] = {"root": _canon_root(root), "model": str(model)}
    _flush()


def note_pin(root: str, model: str) -> None:
    if not _state["started"] or not root or not model:
        return
    croot, nmodel = _canon_root(root), _norm_model(model)
    with _LOCK:
        pinned = _state["record"].setdefault("pinned", [])
        if not any(_canon_root(p.get("root") or "") == croot and _norm_model(p.get("model") or "") == nmodel
                   for p in pinned):
            pinned.append({"root": croot, "model": str(model), "since": time.time()})
    _flush()


def note_unpin(root: str, model: str) -> None:
    if not _state["started"] or not root or not model:
        return
    croot, nmodel = _canon_root(root), _norm_model(model)
    with _LOCK:
        pinned = _state["record"].get("pinned") or []
        _state["record"]["pinned"] = [
            p for p in pinned
            if not (_canon_root(p.get("root") or "") == croot and _norm_model(p.get("model") or "") == nmodel)
        ]
    _flush()


def note_active(root: str, model: str) -> None:
    if not _state["started"] or not root or not model:
        return
    key = f"{_canon_root(root)}|{_norm_model(model)}"
    with _LOCK:
        _state["record"].setdefault("active", {})[key] = time.time()
    _flush()


def note_reservation(root: str, model: str, bytes_needed: int, ttl_s: float) -> None:
    if not _state["started"] or not root or not model:
        return
    croot, nmodel = _canon_root(root), _norm_model(model)
    now = time.time()
    with _LOCK:
        reservations = [
            r for r in (_state["record"].get("reservations") or [])
            if not (_canon_root(r.get("root") or "") == croot and _norm_model(r.get("model") or "") == nmodel)
        ]
        reservations.append({
            "root": croot, "model": str(model), "bytes": max(0, int(bytes_needed or 0)),
            "since": now, "until": now + max(0.0, float(ttl_s or 0.0)),
        })
        _state["record"]["reservations"] = reservations
    _flush()


def clear_reservation(root: str, model: str) -> None:
    if not _state["started"] or not root or not model:
        return
    croot, nmodel = _canon_root(root), _norm_model(model)
    with _LOCK:
        reservations = _state["record"].get("reservations") or []
        _state["record"]["reservations"] = [
            r for r in reservations
            if not (_canon_root(r.get("root") or "") == croot and _norm_model(r.get("model") or "") == nmodel)
        ]
    _flush()


def note_adopted(model: str) -> None:
    if not _state["started"]:
        return
    with _LOCK:
        _state["record"]["adopted_model"] = str(model or "")
    _flush()


# ── the read side (any instance's files) ────────────────────────────────────

def self_record() -> Dict[str, Any]:
    with _LOCK:
        return copy.deepcopy(_state["record"]) if _state["started"] else {}


def _pid_alive(pid: Any) -> bool:
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_i <= 0:
        return False
    try:
        import psutil
        return bool(psutil.pid_exists(pid_i))
    except Exception:  # noqa: BLE001
        pass
    if sys.platform.startswith("win"):
        return True  # no psutil on Windows here: assume alive rather than orphan a live lease
    try:
        os.kill(pid_i, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except OSError:
        return False


def _read_all_lease_files() -> List[Dict[str, Any]]:
    d = _leases_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                rec = json.load(f)
            if isinstance(rec, dict):
                out.append(rec)
        except Exception:  # noqa: BLE001 - a half-written file from another process
            continue
    return out


def siblings() -> List[Dict[str, Any]]:
    """Other instances that are actually alive right now: heartbeat within
    `model_lease_stale_seconds` AND a live PID, never my own id. Tolerates a
    missing directory or a broken file — never raises."""
    stale_s = float(_get_setting("model_lease_stale_seconds", 45) or 45)
    my_id = _current_id()
    now = time.time()
    out: List[Dict[str, Any]] = []
    for rec in _read_all_lease_files():
        try:
            if str(rec.get("instance_id") or "") == my_id:
                continue
            if now - float(rec.get("heartbeat") or 0.0) > stale_s:
                continue
            if not _pid_alive(rec.get("pid")):
                continue
            out.append(rec)
        except Exception:  # noqa: BLE001
            continue
    return out


def sibling_pins(root: str) -> Set[str]:
    croot = _canon_root(root)
    out: Set[str] = set()
    for sib in siblings():
        for p in (sib.get("pinned") or []):
            if isinstance(p, dict) and p.get("model") and _canon_root(p.get("root") or "") == croot:
                out.add(_norm_model(p["model"]))
    return out


def sibling_defaults(root: str) -> Set[str]:
    croot = _canon_root(root)
    out: Set[str] = set()
    for sib in siblings():
        d = sib.get("default") or {}
        if isinstance(d, dict) and d.get("model") and _canon_root(d.get("root") or "") == croot:
            out.add(_norm_model(d["model"]))
    return out


def sibling_active(root: str) -> Dict[str, float]:
    """`{model: last_active_epoch}` from every fresh sibling's own activity
    on `root`, the newest epoch winning when more than one sibling used the
    same model."""
    croot = _canon_root(root)
    out: Dict[str, float] = {}
    for sib in siblings():
        for key, epoch in (sib.get("active") or {}).items():
            try:
                k_root, k_model = str(key).split("|", 1)
            except ValueError:
                continue
            if k_root != croot:
                continue
            try:
                epoch_f = float(epoch)
            except (TypeError, ValueError):
                continue
            if k_model not in out or epoch_f > out[k_model]:
                out[k_model] = epoch_f
    return out


def sibling_reserved_bytes(root: str) -> int:
    croot = _canon_root(root)
    now = time.time()
    total = 0
    for sib in siblings():
        for r in (sib.get("reservations") or []):
            if not isinstance(r, dict) or _canon_root(r.get("root") or "") != croot:
                continue
            try:
                until = float(r.get("until") or 0.0)
            except (TypeError, ValueError):
                continue
            if until <= now:
                continue
            total += max(0, int(r.get("bytes") or 0))
    return total


def holders(root: str, model: str) -> List[Dict[str, Any]]:
    """Who — self included — holds `model` on `root` right now, and how
    (`pinned`/`default`/`active`/`reserved`; one entry per matching kind)."""
    croot, nmodel = _canon_root(root), _norm_model(model)
    now = time.time()
    out: List[Dict[str, Any]] = []

    def _check(rec: Dict[str, Any]) -> None:
        port, iid = rec.get("port"), rec.get("instance_id")
        d = rec.get("default") or {}
        if isinstance(d, dict) and _canon_root(d.get("root") or "") == croot and _norm_model(d.get("model") or "") == nmodel:
            out.append({"port": port, "instance_id": iid, "kind": "default"})
        for p in (rec.get("pinned") or []):
            if isinstance(p, dict) and _canon_root(p.get("root") or "") == croot and _norm_model(p.get("model") or "") == nmodel:
                out.append({"port": port, "instance_id": iid, "kind": "pinned"})
                break
        if f"{croot}|{nmodel}" in (rec.get("active") or {}):
            out.append({"port": port, "instance_id": iid, "kind": "active"})
        for r in (rec.get("reservations") or []):
            if not isinstance(r, dict):
                continue
            if _canon_root(r.get("root") or "") == croot and _norm_model(r.get("model") or "") == nmodel:
                try:
                    until = float(r.get("until") or 0.0)
                except (TypeError, ValueError):
                    until = 0.0
                if until > now:
                    out.append({"port": port, "instance_id": iid, "kind": "reserved"})
                break

    self_rec = self_record()
    if self_rec:
        _check(self_rec)
    for sib in siblings():
        _check(sib)
    return out


# ── residency leadership + adoption ─────────────────────────────────────────

def residency_leader(root: str, model: str) -> Dict[str, Any]:
    """Among self + fresh siblings whose own `default` is `(root, model)`,
    the one with the smallest `started` (ties: lowest port). `{}` when
    nobody's default matches."""
    croot, nmodel = _canon_root(root), _norm_model(model)
    candidates: List[tuple] = []

    def _consider(rec: Dict[str, Any]) -> None:
        d = rec.get("default") or {}
        if isinstance(d, dict) and _canon_root(d.get("root") or "") == croot and _norm_model(d.get("model") or "") == nmodel:
            candidates.append((float(rec.get("started") or 0.0), int(rec.get("port") or 0),
                              str(rec.get("instance_id") or "")))

    self_rec = self_record()
    if self_rec:
        _consider(self_rec)
    for sib in siblings():
        _consider(sib)
    if not candidates:
        return {}
    candidates.sort(key=lambda c: (c[0], c[1]))
    started, port, iid = candidates[0]
    return {"instance_id": iid, "port": port}


def is_residency_leader(root: str, model: str) -> bool:
    """True when this instance should be the one driving the residency
    keeper for `(root, model)` — no lease record of my own (never started,
    or nobody claims this default yet) reads as "yes, act as usual"."""
    self_rec = self_record()
    if not self_rec:
        return True
    leader = residency_leader(root, model)
    if not leader:
        return True
    return leader.get("instance_id") == self_rec.get("instance_id")


def _resident_from_ps(root: str) -> List[str]:
    try:
        from src import vram_admission
        data = vram_admission._get(root, "/api/ps", 2.5) or {}
        return [str(m.get("name") or m.get("model") or "")
               for m in (data.get("models") or []) if isinstance(m, dict)]
    except Exception as exc:  # noqa: BLE001
        logger.debug("model_lease: /api/ps failed: %s", exc)
        return []


def adopt_resident_default(root: str, my_default: str, resident_models: Set[str]) -> str:
    """When `model_lease_adopt_resident` is on and MY default is not
    resident but a fresh sibling's own default IS (on this same root), that
    model — the oldest such sibling's, when more than one qualifies — else
    "". Pure function of its inputs plus `siblings()`."""
    if not bool(_get_setting("model_lease_adopt_resident", True)):
        return ""
    croot = _canon_root(root)
    resident_norm = {_norm_model(m) for m in (resident_models or ())}
    if _norm_model(my_default) in resident_norm:
        return ""
    candidates: List[tuple] = []
    for sib in siblings():
        d = sib.get("default") or {}
        smodel = d.get("model") if isinstance(d, dict) else None
        if not smodel or _canon_root(d.get("root") or "") != croot:
            continue
        if _norm_model(smodel) not in resident_norm:
            continue
        candidates.append((float(sib.get("started") or 0.0), str(smodel)))
    if not candidates:
        return ""
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def adopted_model_for(root: str) -> str:
    """The hook `src.endpoint_resolver.resolve_endpoint` calls for the plain
    "default" lookup: my own record's `adopted_model`, but only when my own
    `default.root` is `root` (an adoption never leaks across two different
    Ollama roots this instance might otherwise resolve)."""
    rec = self_record()
    if not rec:
        return ""
    d = rec.get("default") or {}
    if _canon_root(d.get("root") or "") != _canon_root(root):
        return ""
    return str(rec.get("adopted_model") or "")


def _is_leader_record(rec: Dict[str, Any]) -> bool:
    d = rec.get("default") or {}
    if not isinstance(d, dict) or not d.get("root") or not d.get("model"):
        return False
    leader = residency_leader(d["root"], d["model"])
    return bool(leader) and leader.get("instance_id") == rec.get("instance_id")


def snapshot(root: str) -> Dict[str, Any]:
    """`GET /api/local-models/instances`: self + siblings (port, pid,
    default, pinned, adopted, leader flag, age) and every resident model on
    `root` (a fresh `/api/ps`) with its holders."""
    now = time.time()
    self_rec = self_record()
    records = ([self_rec] if self_rec else []) + siblings()

    def _view(rec: Dict[str, Any]) -> Dict[str, Any]:
        started = rec.get("started")
        return {
            "instance_id": rec.get("instance_id"), "port": rec.get("port"), "pid": rec.get("pid"),
            "data_dir": rec.get("data_dir"),
            "default": rec.get("default") or {},
            "pinned": [p.get("model") for p in (rec.get("pinned") or []) if isinstance(p, dict)],
            "adopted_model": rec.get("adopted_model") or "",
            "leader": _is_leader_record(rec),
            "is_self": self_rec is not None and rec.get("instance_id") == self_rec.get("instance_id"),
            "age_seconds": max(0.0, now - float(started)) if started else None,
        }

    resident_names = [m for m in _resident_from_ps(root) if m]
    resident = [{"model": m, "holders": holders(root, m)} for m in resident_names]
    return {
        "root": _canon_root(root),
        "self_instance_id": self_rec.get("instance_id") if self_rec else None,
        "instances": [_view(r) for r in records],
        "resident": resident,
    }
