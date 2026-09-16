"""src/creator/resources.py — WP30: physical resources and the conservative
admission gate for Creator media jobs (ADR-08, RES01/RES02/RES03, MOD09).

**What exists already, reused, not reinvented.** `src.gpu_topology` and
`src.gpu_shared_memory` are the actual GPU readers (nvidia-smi, PDH); this
module never calls a subprocess itself. `src.memory_budget.physical_budgets`
already deduplicates two endpoints that share one physical card into ONE
budget keyed by `identity_key` (uuid/bus_id, never an index) and already
turns "we could not identify this card" into an honest `gpu_key: None`
component rather than free capacity — the exact WP30 acceptance criteria
("dos endpoints de la misma GPU no duplican capacidad", "un desconocido no
se convierte en capacidad libre") are properties of THAT module, inherited
here rather than re-implemented. `src.vram_fit.DEFAULT_RESERVE_BYTES` is the
same CUDA-context/compute-buffer reserve `vram_fit.plan()` subtracts before
anything else — reused as the fixed overhead added to an unmeasured
footprint, for the same reason it exists there: something is always gone
before the first byte of a real workload lands. `src.resource_admission`
(ADP-32) is the admission primitive itself — `admit()`/`release()` below are
built entirely on its `acquire()`/`release()`; this module adds none of its
own locking around "is a slot free", only around "how many bytes would this
job need" and "does the measured picture already say no".

**Conservative by construction, not by convention.** `creator_resource_mode`
(setting, default `"serial"`) is read fresh on every `admit()` call:

* `"serial"` — one heavy load at a time per device, full stop. This maps
  onto `resource_admission`'s own `max_concurrent=1` default: the gate does
  no byte arithmetic at all, because none is needed to prove safety.
* `"measured"` — a second job may run alongside the first IF the sum of
  every admitted footprint on that device plus the new one still fits
  under the device's live measured free bytes minus
  `creator_vram_margin_mb` (setting). A device whose free bytes are not
  known (the engine never reported a capacity, or the reading is stale)
  is NEVER treated as having room — it falls back to the serial rule for
  that one admission, which is what "a device does not become free capacity
  just because we could not measure it" means operationally, not just in
  the inventory view.

**Footprint estimation.** `DATA_DIR/creator/resources.db` (sqlite, WAL,
`BEGIN IMMEDIATE`, connection closed every call — the same pattern
`src/budget_account.py` uses) persists the largest VRAM footprint actually
observed for an operation, keyed by workflow id, version, engine and a
*shape* (width/height/frames when the caller has them — never seed or
prompt, which do not change what a render costs). A measurement taken at
one shape is never reused as proof of fit at a different one (MOD09): a
512px still and a 1080p video of the same workflow get different keys, and
an unmeasured shape falls back to a flat conservative estimate rather than
borrowing a smaller shape's number. `record_measurement()` is called once a
job's real GPU delta is known (see `src/media_runs.py`'s wiring: a pool-wide
VRAM snapshot taken immediately before `submit()` and again once the run
settles, the same before/after discipline `vram_fit`'s docstring recommends
over any formula) and always widens the stored footprint, never narrows it
on a single small sample — a workflow that occasionally spikes should keep
being estimated at its spike, not its average.

Everything here is best-effort and never raises out of `admit()`/`release()`
into a caller that did not ask for that: a reader that fails, a device this
process cannot see, or a sqlite file that cannot be opened all degrade to
the safe answer (serial, or "wait"), never to "go ahead, it's probably
fine".
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from src import gpu_shared_memory as gsm
from src import gpu_topology
from src import memory_budget
from src import resource_admission
from src import vram_fit

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MIB = 1024 * 1024
GIB = 1024 * MIB

#: A workflow/shape this process has never measured gets this flat, generous
#: budget rather than a number nobody backed with a reading — deliberately
#: conservative (most SDXL-class renders fit well under it) so an
#: unmeasured job still gets to run in serial mode and teach the table its
#: real cost, instead of being refused for a shortfall we made up.
DEFAULT_UNMEASURED_VRAM_BYTES = 6 * GIB

DEFAULT_VRAM_MARGIN_MB = 512

RESOURCE_MODES = ("serial", "measured")


# ── settings (read fresh every call — see module docstring) ────────────────

def resource_mode() -> str:
    from src.settings import get_setting
    mode = str(get_setting("creator_resource_mode", "serial") or "serial").strip().lower()
    return mode if mode in RESOURCE_MODES else "serial"


def vram_margin_bytes() -> int:
    from src.settings import get_setting
    try:
        mb = int(get_setting("creator_vram_margin_mb", DEFAULT_VRAM_MARGIN_MB) or DEFAULT_VRAM_MARGIN_MB)
    except (TypeError, ValueError):
        mb = DEFAULT_VRAM_MARGIN_MB
    return max(0, mb) * MIB


# ── physical inventory (GPUs/RAM/disk/CPU) ──────────────────────────────────

def _data_dir() -> str:
    from src.constants import DATA_DIR
    return DATA_DIR


def _work_dir() -> str:
    d = os.path.join(_data_dir(), "creator")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def gpu_inventory() -> Dict[str, Any]:
    """One entry per PHYSICAL GPU, deduplicated by `memory_budget.
    physical_budgets` — never by index, never by engine endpoint. Returns
    `{"gpus": [MemoryBudget.to_dict(), ...], "reading_ok": bool}`; an empty
    list with `reading_ok: False` means no reader could see a card, not
    "zero GPUs installed"."""
    try:
        snap = gpu_topology.snapshot()
    except Exception as e:  # noqa: BLE001 - a reader must never break inventory
        logger.debug("creator.resources: gpu_topology.snapshot failed: %s", e)
        snap = None
    try:
        vram = gsm.vram_snapshot()
    except Exception as e:  # noqa: BLE001
        logger.debug("creator.resources: vram_snapshot failed: %s", e)
        vram = None
    try:
        budgets = memory_budget.physical_budgets(snapshot=snap, vram=vram)
    except Exception as e:  # noqa: BLE001
        logger.debug("creator.resources: physical_budgets failed: %s", e)
        budgets = {}
    return {
        "gpus": [b.to_dict() for b in budgets.values()],
        "reading_ok": bool(budgets),
    }


def ram_inventory() -> Dict[str, Any]:
    try:
        return dict(memory_budget.system_memory())
    except Exception as e:  # noqa: BLE001
        logger.debug("creator.resources: system_memory failed: %s", e)
        return {"ram_total": None, "ram_available": None, "source": "absent"}


def disk_inventory() -> Dict[str, Any]:
    """Free/total on the work volume media jobs actually write into
    (`DATA_DIR/creator`, same filesystem as the artifact store in the
    ordinary single-disk setup)."""
    try:
        usage = shutil.disk_usage(_work_dir())
        return {"total_bytes": int(usage.total), "free_bytes": int(usage.free),
                "used_bytes": int(usage.used), "path": _work_dir()}
    except OSError as e:
        return {"total_bytes": None, "free_bytes": None, "used_bytes": None,
                "path": _work_dir(), "reason": str(e)}


def cpu_inventory() -> Dict[str, Any]:
    count = os.cpu_count()
    return {"logical_cpus": int(count) if count else None}


def device_inventory() -> Dict[str, Any]:
    """The full physical picture `GET /api/creator/resources` shows."""
    return {
        "schema_version": SCHEMA_VERSION,
        "gpus": gpu_inventory(),
        "ram": ram_inventory(),
        "disk": disk_inventory(),
        "cpu": cpu_inventory(),
        "mode": resource_mode(),
        "vram_margin_bytes": vram_margin_bytes(),
    }


# ── footprint estimation/measurement (DATA_DIR/creator/resources.db) ───────

def _db_path() -> Path:
    return Path(_work_dir()) / "resources.db"


@contextmanager
def _db(*, write: bool = False):
    path = _db_path()
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        if write:
            conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            _create_schema(conn)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            if write:
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS footprints (
        op_key TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL,
        version TEXT NOT NULL,
        engine_key TEXT NOT NULL,
        shape TEXT NOT NULL DEFAULT '',
        vram_bytes INTEGER NOT NULL,
        ram_bytes INTEGER NOT NULL DEFAULT 0,
        samples INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'measured',
        updated_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS inflight (
        run_id TEXT PRIMARY KEY,
        op_key TEXT NOT NULL,
        device TEXT NOT NULL,
        vram_before_bytes INTEGER,
        footprint_vram_bytes INTEGER NOT NULL,
        created_at REAL NOT NULL)""")


def shape_key(values: Optional[Mapping[str, Any]]) -> str:
    """A stable, coarse descriptor of what makes a footprint bigger or
    smaller — never the seed or the prompt, which do not. Anything the
    template did not resolve is simply absent from the key, so a workflow
    with no notion of width/height/frames gets one shape, total."""
    values = values or {}
    parts: List[str] = []
    for field_name in ("width", "height", "frames", "duration", "fps", "steps"):
        v = values.get(field_name)
        if v is not None:
            parts.append(f"{field_name}={v}")
    return ",".join(parts)


def op_key(workflow_id: str, version: str, engine_key: str, shape: str = "") -> str:
    return f"{workflow_id}|{version}|{engine_key}|{shape}"


@dataclass(frozen=True)
class Footprint:
    vram_bytes: int
    ram_bytes: int = 0
    source: str = "estimated"  # "estimated" | "measured"

    def to_dict(self) -> Dict[str, Any]:
        return {"vram_bytes": self.vram_bytes, "ram_bytes": self.ram_bytes,
                "source": self.source}


def estimate_footprint(workflow_id: str, version: str, engine_key: str,
                        shape: str = "") -> Footprint:
    """The largest footprint measured for this exact (workflow, version,
    engine, shape) so far, or a flat conservative estimate when nothing has
    ever been measured for it. Never returns a *smaller* number than the
    last real measurement — `record_measurement` only ever widens."""
    key = op_key(workflow_id, version, engine_key, shape)
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT vram_bytes, ram_bytes FROM footprints WHERE op_key=?",
                (key,)).fetchone()
    except sqlite3.Error as e:  # noqa: BLE001 - a corrupt/missing db degrades, not raises
        logger.debug("creator.resources: estimate_footprint read failed: %s", e)
        row = None
    if row is not None:
        return Footprint(vram_bytes=int(row["vram_bytes"]), ram_bytes=int(row["ram_bytes"]),
                          source="measured")
    return Footprint(vram_bytes=DEFAULT_UNMEASURED_VRAM_BYTES + vram_fit.DEFAULT_RESERVE_BYTES,
                      ram_bytes=0, source="estimated")


def record_measurement(workflow_id: str, version: str, engine_key: str, *,
                        vram_bytes: int, ram_bytes: int = 0, shape: str = "") -> None:
    """Widen (never narrow on one sample) the persisted footprint for this
    op. `vram_bytes` <= 0 is ignored — a reading that could not be taken
    (no GPU visible, engine unreachable) must never overwrite a real one
    with zero."""
    if vram_bytes is None or vram_bytes <= 0:
        return
    key = op_key(workflow_id, version, engine_key, shape)
    now = time.time()
    try:
        with _db(write=True) as conn:
            row = conn.execute(
                "SELECT vram_bytes, ram_bytes, samples FROM footprints WHERE op_key=?",
                (key,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO footprints (op_key, workflow_id, version, engine_key, shape, "
                    "vram_bytes, ram_bytes, samples, source, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,1,'measured',?)",
                    (key, workflow_id, version, engine_key, shape,
                     int(vram_bytes), int(ram_bytes or 0), now))
            else:
                new_vram = max(int(row["vram_bytes"]), int(vram_bytes))
                new_ram = max(int(row["ram_bytes"]), int(ram_bytes or 0))
                conn.execute(
                    "UPDATE footprints SET vram_bytes=?, ram_bytes=?, samples=samples+1, "
                    "source='measured', updated_at=? WHERE op_key=?",
                    (new_vram, new_ram, now, key))
    except sqlite3.Error as e:  # noqa: BLE001
        logger.debug("creator.resources: record_measurement write failed: %s", e)


def note_inflight(run_id: str, workflow_id: str, version: str, engine_key: str, *,
                  device: str, footprint: Footprint, vram_before_bytes: Optional[int],
                  shape: str = "") -> None:
    """Durable "a job is running and here is what we saw before it started"
    row, so the real delta can be recorded even across a process restart."""
    try:
        with _db(write=True) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO inflight (run_id, op_key, device, vram_before_bytes, "
                "footprint_vram_bytes, created_at) VALUES (?,?,?,?,?,?)",
                (run_id, op_key(workflow_id, version, engine_key, shape), device,
                 vram_before_bytes, footprint.vram_bytes, time.time()))
    except sqlite3.Error as e:  # noqa: BLE001
        logger.debug("creator.resources: note_inflight failed: %s", e)


def settle_inflight(run_id: str, *, vram_after_bytes: Optional[int]) -> None:
    """Compute the real delta for a finished run and fold it into the
    footprint table, then drop the inflight row either way."""
    try:
        with _db(write=True) as conn:
            row = conn.execute(
                "SELECT op_key, device, vram_before_bytes, footprint_vram_bytes FROM inflight "
                "WHERE run_id=?", (run_id,)).fetchone()
            if row is not None:
                conn.execute("DELETE FROM inflight WHERE run_id=?", (run_id,))
    except sqlite3.Error as e:  # noqa: BLE001
        logger.debug("creator.resources: settle_inflight failed: %s", e)
        return
    if row is None:
        return
    before = row["vram_before_bytes"]
    if before is None or vram_after_bytes is None:
        return
    delta = int(vram_after_bytes) - int(before)
    if delta <= 0:
        # A pool-wide delta cannot go negative from one job's own weights —
        # it means something else on the same pool freed memory at the same
        # time, which makes this sample noise, not a measurement.
        return
    workflow_id, version, engine_key, shape = row["op_key"].split("|", 3) if row["op_key"].count("|") >= 3 else (row["op_key"], "", "", "")
    record_measurement(workflow_id, version, engine_key, vram_bytes=delta, shape=shape)


# ── admission gate (built on resource_admission.acquire/release) ───────────

@dataclass(frozen=True)
class Admission:
    ok: bool
    wait_for: Optional[str] = None
    reason: str = ""
    lease_id: Optional[str] = None
    pool_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "wait_for": self.wait_for, "reason": self.reason,
                "lease_id": self.lease_id, "pool_id": self.pool_id}


#: run_id -> {"lease_id", "pool_id", "device", "footprint": Footprint}
_ACTIVE: Dict[str, Dict[str, Any]] = {}
_LEDGER_LOCK = threading.RLock()

#: measured mode never lets acquire()'s own counter be the real gate — the
#: byte arithmetic above is. This cap only bounds how many leases can be
#: outstanding at once as a sanity ceiling, not a capacity claim.
_MEASURED_MAX_CONCURRENT = 32


def _run_sync(coro):
    """Drive one `resource_admission` coroutine to completion from ordinary
    sync code (this module's callers run in a worker thread via
    `asyncio.to_thread`, not inside an event loop). Every call here uses
    `timeout=0`, so it never actually awaits across an event-loop boundary
    — see the module docstring for why that makes a fresh loop per call
    safe against the pool state's lazily-created `asyncio.Condition`."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(
        "creator.resources: admit()/release() must be called from sync code "
        "(e.g. via asyncio.to_thread), never from inside a running event loop")


def _device_id(device: Union[str, Mapping[str, Any]]) -> str:
    if isinstance(device, Mapping):
        return str(device.get("id") or "")
    return str(device or "")


def _device_capacity_bytes(device: Union[str, Mapping[str, Any]]) -> Optional[int]:
    if not isinstance(device, Mapping):
        return None
    free = device.get("free_bytes")
    if isinstance(free, (int, float)) and not isinstance(free, bool):
        return int(free)
    total = device.get("total_bytes")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        return int(total)
    return None


def _pool_id_for(device_id: str) -> str:
    normalized = resource_admission.normalize_endpoint(device_id)
    return f"creator:{normalized or device_id}"


def _committed_bytes(device_id: str, *, exclude_run_id: str = "") -> int:
    with _LEDGER_LOCK:
        return sum(
            entry["footprint"].vram_bytes
            for run_id, entry in _ACTIVE.items()
            if entry["device"] == device_id and run_id != exclude_run_id
        )


def _oldest_active_on(device_id: str) -> Optional[str]:
    with _LEDGER_LOCK:
        candidates = [(entry["acquired_at"], run_id) for run_id, entry in _ACTIVE.items()
                     if entry["device"] == device_id]
    if not candidates:
        return None
    return min(candidates)[1]


def admit(op_footprint: Footprint, device: Union[str, Mapping[str, Any]], *,
          run_id: str, priority: str = "background", mode: Optional[str] = None) -> Admission:
    """The gate. `device` is either a plain id (capacity unknown — always
    falls back to serial for this admission) or a mapping carrying `id` and
    `free_bytes`/`total_bytes` from a live reading.

    Returns immediately either way — this is a non-blocking check, never a
    wait. A caller that is refused (`ok=False`) is expected to leave its job
    queued and ask again later (see `src/media_runs.py`), not to block a
    thread on it."""
    device_id = _device_id(device)
    if not device_id:
        return Admission(ok=False, reason="no device given; nothing to admit against")
    if not run_id:
        return Admission(ok=False, reason="admit() requires a run_id to track the lease")

    effective_mode = mode if mode in RESOURCE_MODES else resource_mode()
    pool_id = _pool_id_for(device_id)
    capacity = _device_capacity_bytes(device) if effective_mode == "measured" else None

    if effective_mode == "measured" and capacity is not None:
        margin = vram_margin_bytes()
        with _LEDGER_LOCK:
            committed = _committed_bytes(device_id)
            fits = capacity - margin - committed >= op_footprint.vram_bytes
            if not fits:
                wait_for = _oldest_active_on(device_id)
                return Admission(
                    ok=False, wait_for=wait_for, pool_id=pool_id,
                    reason=(f"measured mode: {op_footprint.vram_bytes // MIB} MiB requested, "
                            f"only {max(0, capacity - margin - committed) // MIB} MiB free on "
                            f"{device_id} after {committed // MIB} MiB already committed and a "
                            f"{margin // MIB} MiB margin"))
            try:
                resource_admission.define_pool(pool_id, "gpu", [device_id]
                                               if resource_admission.normalize_endpoint(device_id) else [],
                                               max_concurrent=_MEASURED_MAX_CONCURRENT)
                lease = _run_sync(resource_admission.acquire(
                    pool_id, priority=priority, owner=run_id, timeout=0))
            except resource_admission.AdmissionTimeout:
                return Admission(ok=False, wait_for=_oldest_active_on(device_id), pool_id=pool_id,
                                 reason="measured mode: pool saturated (too many concurrent leases)")
            except resource_admission.PoolError as e:
                return Admission(ok=False, pool_id=pool_id, reason=f"pool error: {e}")
            _ACTIVE[run_id] = {"lease_id": lease.id, "pool_id": pool_id, "device": device_id,
                               "footprint": op_footprint, "acquired_at": time.time()}
            return Admission(ok=True, lease_id=lease.id, pool_id=pool_id)

    # Serial mode, or measured mode with no known capacity for this device —
    # "unknown never becomes free capacity": fall back to one at a time.
    try:
        resource_admission.define_pool(pool_id, "gpu", [device_id]
                                       if resource_admission.normalize_endpoint(device_id) else [],
                                       max_concurrent=1)
        lease = _run_sync(resource_admission.acquire(pool_id, priority=priority, owner=run_id,
                                                      timeout=0))
    except resource_admission.AdmissionTimeout:
        with _LEDGER_LOCK:
            wait_for = _oldest_active_on(device_id)
        return Admission(
            ok=False, wait_for=wait_for, pool_id=pool_id,
            reason=(f"{effective_mode} mode: {device_id} is busy with another heavy job"
                    + (f" ({wait_for})" if wait_for else "")))
    except resource_admission.PoolError as e:
        return Admission(ok=False, pool_id=pool_id, reason=f"pool error: {e}")
    with _LEDGER_LOCK:
        _ACTIVE[run_id] = {"lease_id": lease.id, "pool_id": pool_id, "device": device_id,
                           "footprint": op_footprint, "acquired_at": time.time()}
    return Admission(ok=True, lease_id=lease.id, pool_id=pool_id)


def release(run_id: str) -> bool:
    """Free whatever `admit()` handed out for `run_id`, if anything. A
    run that was never admitted (refused, or never asked) is a harmless
    no-op — the same "duplicate/late release is not an over-free" contract
    `resource_admission.release` already gives, extended to "never admitted
    at all" as another shape of nothing-to-do."""
    with _LEDGER_LOCK:
        entry = _ACTIVE.pop(run_id, None)
    if entry is None:
        return False
    try:
        return bool(_run_sync(resource_admission.release(entry["lease_id"])))
    except Exception as e:  # noqa: BLE001 - releasing must never raise into a caller's finally
        logger.warning("creator.resources: release(%s) failed: %s", run_id, e)
        return False


def active_admissions() -> List[Dict[str, Any]]:
    """For `GET /api/creator/resources` — every run currently holding a
    lease, and what it is costing."""
    with _LEDGER_LOCK:
        return [
            {"run_id": run_id, "device": entry["device"], "pool_id": entry["pool_id"],
             "footprint": entry["footprint"].to_dict(), "acquired_at": entry["acquired_at"]}
            for run_id, entry in _ACTIVE.items()
        ]


def reset_for_tests() -> None:
    """Tests only: forget every in-process lease this module is tracking.
    Does NOT touch `resource_admission`'s own state — call
    `resource_admission.reset_all()` alongside this when a test needs both
    clean."""
    with _LEDGER_LOCK:
        _ACTIVE.clear()


__all__ = [
    "SCHEMA_VERSION", "RESOURCE_MODES", "DEFAULT_UNMEASURED_VRAM_BYTES",
    "DEFAULT_VRAM_MARGIN_MB", "resource_mode", "vram_margin_bytes",
    "gpu_inventory", "ram_inventory", "disk_inventory", "cpu_inventory",
    "device_inventory", "shape_key", "op_key", "Footprint",
    "estimate_footprint", "record_measurement", "note_inflight",
    "settle_inflight", "Admission", "admit", "release", "active_admissions",
    "reset_for_tests",
]
