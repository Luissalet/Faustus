"""state_mirror/adapters/hardware.py -- the box itself: cores, memory, disk, cards.

`device_state.v1` for the machine Faustus is running on. This is the ONLY
schema in `contracts.SNAPSHOT_SCHEMAS`, and that one line of the contract is
what shapes the whole module.

**What a snapshot costs.** For a snapshot schema, an observation with
`partial=False` means "these are all the fields there are" and the reducer
DELETES every field the observation does not carry. So `partial=False` here is
a claim, not a formality: it says every one of the eight declared fields was
read in this pass. `_state_from` builds the body by asking three readers and
keeping only what each one actually returned, and `partial` is then simply
whether that body came out complete. A box with no NVIDIA card, or one where
psutil is not installed, therefore never publishes a complete snapshot -- it
publishes what it has and deletes nothing. That is the right way round: the
failure to avoid is a machine losing its RAM reading because nvidia-smi went
missing for one sweep.

**Why nvidia-smi is cached and psutil is not.** `nvidia-smi` is a process, and
a sweep every five seconds would fork one every five seconds for numbers that
`freshness` only guarantees for five anyway. So it runs at most once per
`GPU_TTL_SECONDS` and the observation is stamped with the moment it ran.
`psutil.virtual_memory()` and `shutil.disk_usage()` are a syscall each and are
read fresh every time, because caching them would cost more in explanation
than it saves in cycles.

**Why the GPU numbers come through the usage route's own reducers.**
`_collect_gpu` is `parse_gpu_query` plus the nvidia-smi invocation and the
Windows fallback paths for finding the executable, and `gpu_pool` is this
repository's answer to "the cards as one" -- memory summed, utilisation taken
from the worst card. Calling that pair rather than re-deriving the pool here
means `/api/system/usage` and the state mirror cannot disagree about how much
VRAM this box has, which they would the first time either of them changed.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from src.contracts.base import now_iso
from src.state_mirror.adapters.base import (
    Scope,
    ThreadedAdapter,
    entity,
    observation,
)
from src.state_mirror.contracts import StateEntity, StateObservation, entity_id

logger = logging.getLogger(__name__)

__all__ = ["HardwareAdapter", "DEVICE_FIELDS", "SOURCE", "SCHEMA",
           "read_host", "read_disk", "read_gpu"]

SOURCE = "hardware"
SCHEMA = "device_state.v1"

#: Every field `device_state.v1` declares, in one tuple. A complete snapshot
#: is one whose body has all of these, and nothing else decides that -- so the
#: list lives here rather than being counted from the contract at each call,
#: and a field added to the schema without a reader here shows up as this
#: adapter quietly ceasing to publish complete snapshots rather than as one
#: that deletes the new field every sweep.
DEVICE_FIELDS: Tuple[str, ...] = (
    "cpu_percent", "ram_used_bytes", "ram_total_bytes", "disk_free_bytes",
    "gpu_count", "gpu_used_bytes", "gpu_total_bytes", "gpu_utilisation",
)

#: How long one nvidia-smi reading stands. Twice `device_state.v1`'s own 5s
#: TTL, so a card's numbers are never served as `fresh` from a process that ran
#: more than one TTL ago, and a sweep at any rate costs at most one fork every
#: ten seconds.
GPU_TTL_SECONDS = 10.0

_MIB = 1024 * 1024

#: Characters an entity identifier may carry. A hostname with anything else in
#: it becomes dashes rather than an id the contract will refuse.
_UNSAFE_IDENTIFIER = re.compile(r"[^A-Za-z0-9_.@+~-]+")

_lock = threading.RLock()
#: (monotonic time of the read, ISO stamp of the read, pool fields)
_gpu_cache: Optional[Tuple[float, str, Dict[str, Any]]] = None


def reset_cache() -> None:
    """Drop the cached nvidia-smi reading. For a test, and for the doctor."""
    global _gpu_cache
    with _lock:
        _gpu_cache = None


def read_host() -> Dict[str, Any]:
    """CPU and RAM from psutil, or `{}` when psutil is not installed.

    `{}` and not zeros. A box with no psutil has not been measured at 0% CPU;
    nothing measured it, and the difference is the whole reason this schema's
    fields are allowed to be absent.
    """
    import psutil

    memory = psutil.virtual_memory()
    return {
        # `interval=None` compares against the previous call rather than
        # sleeping, which is what makes this safe on a sweep. The first call in
        # a process answers 0.0 by definition, and the next one is real.
        "cpu_percent": float(psutil.cpu_percent(interval=None)),
        "ram_used_bytes": int(memory.used),
        "ram_total_bytes": int(memory.total),
    }


def read_disk() -> Dict[str, Any]:
    """Free bytes where Faustus writes, or `{}` when the path cannot be measured.

    DATA_DIR rather than the root filesystem: the number that matters is the
    headroom on the volume this install actually fills up, and on a box with
    the data directory on a second drive those are different disks.
    """
    import shutil

    from src.constants import DATA_DIR

    usage = shutil.disk_usage(DATA_DIR)
    total = int(getattr(usage, "total", 0) or 0)
    if total <= 0:
        return {}
    return {"disk_free_bytes": int(getattr(usage, "free", 0) or 0)}


def _gpu_uncached() -> Dict[str, Any]:
    """One nvidia-smi run, reduced to the pool by the usage route's own reducer.

    `_collect_gpu` is `parse_gpu_query` with the subprocess and the Windows
    executable search around it; taking the pair rather than re-running
    nvidia-smi with a field list of this module's own is what stops the two
    from drifting -- `parse_gpu_query` reads its columns by position.
    """
    from routes.system_usage_routes import _collect_gpu, gpu_pool

    rows, error = _collect_gpu()
    if error or not rows:
        logger.debug("hardware: no GPU reading (%s)", error or "no cards")
        return {}
    pool = gpu_pool(rows)
    if not pool:
        return {}
    out: Dict[str, Any] = {}
    count = pool.get("count")
    if isinstance(count, int) and count > 0:
        out["gpu_count"] = count
    # nvidia-smi answers in MiB and this schema is in bytes. A unit is not an
    # inference; the number is still the card's own.
    for field, key in (("gpu_used_bytes", "mem_used"),
                       ("gpu_total_bytes", "mem_total")):
        value = pool.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[field] = int(float(value) * _MIB)
    utilisation = pool.get("util")
    if isinstance(utilisation, (int, float)) and not isinstance(utilisation, bool):
        out["gpu_utilisation"] = float(utilisation)
    return out


def read_gpu() -> Tuple[str, Dict[str, Any]]:
    """`(observed_at, pool fields)`, forking nvidia-smi at most once per TTL.

    The stamp is when the process ran. Serving a cached reading under the
    sweep's own clock would make a ten-second-old card reading look like it had
    just been taken, and this schema's fields are guaranteed for five.
    """
    global _gpu_cache
    with _lock:
        hit = _gpu_cache
        if hit is not None and time.monotonic() - hit[0] < GPU_TTL_SECONDS:
            return hit[1], hit[2]
    stamp = now_iso()
    try:
        fields = _gpu_uncached()
    except Exception as exc:                                       # noqa: BLE001
        logger.debug("hardware: GPU reading failed: %s", exc)
        fields = {}
    with _lock:
        _gpu_cache = (time.monotonic(), stamp, fields)
    return stamp, fields


def device_identifier() -> str:
    """This machine's name, sanitised, or `"local"` when it has none.

    A name rather than a constant, so that an install whose state is ever
    replicated -- a paired remote worker, a second box in the same store --
    does not fold two machines' readings into one row.
    """
    import platform

    cleaned = _UNSAFE_IDENTIFIER.sub("-", str(platform.node() or "").strip()).strip("-")
    return cleaned or "local"


class HardwareAdapter(ThreadedAdapter):
    """The host machine, as one `device_state.v1` row."""

    name = SOURCE
    schemas = (SCHEMA,)

    def _target(self, scope: Scope) -> str:
        return entity_id("device", scope.owner, device_identifier(),
                         namespace=scope.namespace)

    def discover(self, scope: Scope) -> List[StateEntity]:
        identifier = self._safe(device_identifier, default="")
        if not identifier:
            return []
        row = self._safe(entity, "device", identifier, scope=scope,
                         schema=SCHEMA, display_name=identifier, default=None)
        return [row] if row is not None else []

    def _reading(self) -> Tuple[str, Dict[str, Any]]:
        """`(observed_at, body)` -- every reader that answered, and nothing else.

        When there are card numbers in the body, the whole observation is
        stamped with the moment nvidia-smi ran, which may be up to a TTL ago.
        That understates how current the CPU and RAM readings beside them are,
        and understating is the direction to be wrong in: one observation
        carries one time, and the alternative is presenting a cached card
        reading as if it had just been taken.
        """
        state: Dict[str, Any] = {}
        for reader in (read_host, read_disk):
            part = self._safe(reader, default=None)
            if isinstance(part, dict):
                state.update({k: v for k, v in part.items()
                              if k in DEVICE_FIELDS and v is not None})
        gpu = self._safe(read_gpu, default=None)
        observed_at = ""
        if gpu:
            stamp, fields = gpu
            cards = {k: v for k, v in (fields or {}).items()
                     if k in DEVICE_FIELDS and v is not None}
            state.update(cards)
            observed_at = stamp if cards else ""
        return observed_at or now_iso(), state

    def observe(self, scope: Scope) -> List[StateObservation]:
        target = self._safe(self._target, scope, default="")
        if not target:
            return []
        observed_at, state = self._safe(self._reading, default=None) or ("", {})
        if not state:
            return []
        # The claim `partial=False` makes on a snapshot schema is that these
        # are ALL the fields there are, and the reducer deletes the rest on
        # the strength of it. So it is made only when every declared field was
        # read this pass: a box with no card, or one where psutil failed,
        # publishes what it has and takes nothing away.
        complete = all(field in state for field in DEVICE_FIELDS)
        obs = observation(
            target, SOURCE, state,
            scope=scope,
            schema=SCHEMA,
            # psutil, statvfs and nvidia-smi are the authorities on their own
            # numbers; the pool figures beside them are those same numbers
            # expressed once for a box that may hold more than one card.
            epistemic="observed",
            observed_at=observed_at,
            partial=not complete,
        )
        return [obs] if obs is not None else []
