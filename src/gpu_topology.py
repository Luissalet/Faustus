"""src/gpu_topology.py — INF-05 §11 A2: physical GPU identity, not just a
runtime index.

`nvidia-smi` numbers GPUs by enumeration order, and that order is not
stable: unplug and reconnect an eGPU (an AORUS box, say) and "GPU 1" can
become "GPU 0" with nothing about the card itself having changed. Every
other module in this codebase that reasons about "which GPU" — admission,
placement, the budget in `src/memory_budget.py` — has to key off something
that actually identifies the card: `uuid` when the driver reports one,
`bus_id` otherwise, an index never (`src.contracts.inference.identity_key`).

This module is the one place that reads the extra `nvidia-smi` columns that
carry that identity, plus what is known about the PCIe link and the
physical transport it runs over:

  `parse_topology_query`  pure CSV parsing (`TOPOLOGY_QUERY`'s columns);
                          `[N/A]`/`[Not Supported]` become `None`, never 0.
  `snapshot`              the cached, subprocess-backed reading (mirrors
                          `gpu_shared_memory.vram_snapshot`'s cache/never-
                          raise shape). Never starts inference, never reads
                          more than one `nvidia-smi` invocation.
  `heuristic_transport`   a narrow, explicit guess — ONLY for an
                          unusually narrow observed link — never derived
                          from a GPU's commercial name (§11: "no deducir
                          ancho de banda útil de una etiqueta comercial").
  `annotate_transport`/
  `apply_annotations`     a person's manual transport note, stored on a
                          hardware profile and overlaid onto a snapshot —
                          it can win over a `heuristic` guess, never over
                          an `observed` reading.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional

from src import gpu_shared_memory as gsm
from src.contracts.base import ContractError, now_iso
from src.contracts.inference import GpuInfo, HardwareSnapshot, LinkInfo, TransportInfo

logger = logging.getLogger(__name__)

TOPOLOGY_QUERY = (
    "index,name,uuid,pci.bus_id,memory.total,memory.used,driver_version,"
    "pcie.link.gen.current,pcie.link.width.current,pcie.link.gen.max,pcie.link.width.max"
)

_MIB = 1024 * 1024
_CACHE_TTL = 8.0
_TIMEOUT_S = 5.0
# The narrow-link heuristic's threshold (§11: "SOLO marca heuristic... cuando
# el enlace observado es estrecho"). x4 or narrower on a desktop GPU is
# unusual enough to be worth a flag; x8/x16 are ordinary.
_NARROW_WIDTH = 4

_NA_VALUES = {"", "n/a", "[n/a]", "[not supported]", "[unknown error]", "[insufficient permissions]"}

_cache: Dict[str, Any] = {"ts": 0.0, "key": None, "data": None}
_lock = threading.Lock()


# ── pure parsing ─────────────────────────────────────────────────────────────

def _clean(value: str) -> Optional[str]:
    v = (value or "").strip()
    if not v or v.lower() in _NA_VALUES:
        return None
    return v


def _int_field(value: str) -> Optional[int]:
    v = _clean(value)
    if v is None:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def heuristic_transport(gpu: GpuInfo) -> Optional[TransportInfo]:
    """A link observed unusually narrow (`width_current <= 4`, with
    `gen_current` also observed) is worth flagging as "verify manually" —
    everything else returns `None`. Deliberately does not look at
    `gpu.name`: "AORUS"/"eGPU" in a commercial name is not evidence of
    transport (§11)."""
    link = gpu.link
    if link is None or link.source != "observed":
        return None
    if link.gen_current is None or link.width_current is None:
        return None
    if link.width_current > _NARROW_WIDTH:
        return None
    return TransportInfo(
        kind="unknown", source="heuristic",
        note="narrow link (x4): may be an external enclosure; verify manually",
        observed_at=None,
    )


def parse_topology_query(csv_text: str) -> List[GpuInfo]:
    """Rows of `TOPOLOGY_QUERY` (csv, noheader, nounits) -> `[GpuInfo, ...]`.
    Pure: no subprocess, no cache, no clock other than what a caller passes
    in via the GPU's own fields. `bus_id` is upper-cased (nvidia-smi's own
    casing is inconsistent across driver versions); any `[N/A]`/
    `[Not Supported]` cell becomes `None`, never 0 or an empty string typed
    as if it were a real reading. A narrow observed link is annotated with
    `heuristic_transport` right here, before the caller ever sees the row.
    """
    gpus: List[GpuInfo] = []
    for line in (csv_text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 11:
            continue
        try:
            index = int(float(parts[0]))
        except ValueError:
            continue  # header line, or garbage
        # 9 trailing quantity/id fields; the name (which nvidia-smi never
        # quotes) absorbs any stray comma in between, same trick
        # `gpu_shared_memory.parse_vram_query` and `gpu_placement.
        # parse_compute_apps` already use.
        trailing = parts[-9:]
        name = ", ".join(parts[1:-9]).strip()
        (uuid_raw, bus_raw, mem_total_raw, _mem_used_raw, driver_raw,
         gen_cur_raw, width_cur_raw, gen_max_raw, width_max_raw) = trailing

        uuid = _clean(uuid_raw)
        bus_id = _clean(bus_raw)
        bus_id = bus_id.upper() if bus_id else None
        mem_total = _int_field(mem_total_raw)
        vram_bytes = mem_total * _MIB if mem_total is not None else None
        driver = _clean(driver_raw)
        gen_current = _int_field(gen_cur_raw)
        width_current = _int_field(width_cur_raw)
        gen_max = _int_field(gen_max_raw)
        width_max = _int_field(width_max_raw)

        link: Optional[LinkInfo] = None
        if any(v is not None for v in (gen_current, width_current, gen_max, width_max)):
            link = LinkInfo(gen_current=gen_current, width_current=width_current,
                            gen_max=gen_max, width_max=width_max, source="observed")

        gpu = GpuInfo(
            index=index, name=name, uuid=uuid, bus_id=bus_id, vram_bytes=vram_bytes,
            provenance="nvidia-smi", driver=driver, link=link, transport=None,
        )
        heuristic = heuristic_transport(gpu)
        if heuristic is not None:
            gpu = replace(gpu, transport=heuristic)
        gpus.append(gpu)
    return gpus


# ── the cached, subprocess-backed reading ───────────────────────────────────

def _snapshot_uncached(host: str, ssh_port: str) -> HardwareSnapshot:
    label = host or "localhost"
    if host:
        # A remote box's topology needs an SSH-run nvidia-smi, which no
        # other module in this codebase currently wraps generically (unlike
        # `services.hwfit.hardware.detect_system`'s bespoke probes). Rather
        # than half-implement that path and risk a confidently wrong
        # reading for a machine this process cannot directly see, this is
        # the honest "not yet" — `topology: "unknown"` with the reason
        # spelled out, never a guess dressed up as a reading.
        return HardwareSnapshot(
            host=label, gpus=(), topology="unknown",
            provenance="remote GPU topology (host given) is not read by this lote; "
                      "only the local machine's nvidia-smi is queried",
        )
    exe = gsm._nvidia_smi_path()
    if not exe:
        return HardwareSnapshot(host=label, gpus=(), topology="unknown",
                                provenance="nvidia-smi: not found")
    try:
        proc = subprocess.run(
            [exe, f"--query-gpu={TOPOLOGY_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return HardwareSnapshot(host=label, gpus=(), topology="unknown",
                                provenance=f"nvidia-smi: {e}")
    if proc.returncode != 0:
        return HardwareSnapshot(host=label, gpus=(), topology="unknown",
                                provenance=f"nvidia-smi exit {proc.returncode}")
    gpus = parse_topology_query(proc.stdout or "")
    if not gpus:
        return HardwareSnapshot(host=label, gpus=(), topology="unknown",
                                provenance="nvidia-smi reported no usable GPU")
    return HardwareSnapshot(host=label, gpus=tuple(gpus), observed_at=now_iso(),
                            topology="known", provenance="nvidia-smi")


def snapshot(*, host: str = "", ssh_port: str = "") -> HardwareSnapshot:
    """The physical topology of `host` (`""` = this machine), cached 8 s —
    the same TTL `gpu_shared_memory.vram_snapshot` uses, for the same
    reason: this is read on every hardware-panel poll. Never raises; never
    starts a model or a benchmark."""
    key = (host, ssh_port)
    now = time.time()
    with _lock:
        if _cache["data"] is not None and _cache["key"] == key and now - _cache["ts"] < _CACHE_TTL:
            return _cache["data"]
    try:
        data = _snapshot_uncached(host, ssh_port)
    except Exception as e:  # noqa: BLE001 - a topology read must never break the caller
        logger.debug("gpu_topology: snapshot failed: %s", e)
        data = HardwareSnapshot(host=host or "localhost", gpus=(), topology="unknown",
                                provenance=f"unexpected error: {e}")
    with _lock:
        _cache["ts"] = time.time()
        _cache["key"] = key
        _cache["data"] = data
    return data


def reset_cache() -> None:
    with _lock:
        _cache["ts"] = 0.0
        _cache["key"] = None
        _cache["data"] = None


# ── manual annotations, stored on a hardware profile ────────────────────────

def annotate_transport(profile_id: str, gpu_key: str, kind: str, note: str, by: str = "") -> Dict[str, Any]:
    """Save a person's manual transport call for `gpu_key` (an
    `identity_key`, never an index) onto a hardware profile's
    `topology_annotations` map. When `profile_id` names no existing
    profile, a fresh one is captured first — annotating needs SOMETHING to
    annotate onto, but this never starts a benchmark to get one (the same
    rule `hardware_profiles.collect_profile`'s own docstring states).
    Raises `ContractError` for a `kind` outside `TRANSPORT_KINDS` — the
    route layer turns that into a 400, never silently drops the vocabulary
    violation.
    """
    from src import hardware_profiles

    if not gpu_key:
        raise ValueError("gpu_key is required")
    info = TransportInfo.parse(
        {"kind": kind, "source": "manual", "note": note or "", "observed_at": now_iso()},
        "annotation",
    )
    profile = hardware_profiles.get_profile(profile_id) if profile_id else None
    if profile is None:
        profile = hardware_profiles.collect_profile()
    annotations = dict(profile.get("topology_annotations") or {})
    # `by` is bookkeeping (who annotated it), not part of the `TransportInfo`
    # shape itself — kept as a sibling key so `apply_annotations` can hand
    # `["transport"]` straight to `TransportInfo.parse` without first having
    # to strip it back out.
    annotations[gpu_key] = {"transport": info.to_dict(), "by": by or ""}
    profile["topology_annotations"] = annotations
    hardware_profiles.save_profile(profile)
    return profile


def apply_annotations(snapshot_: HardwareSnapshot, profile: Optional[Dict[str, Any]]) -> HardwareSnapshot:
    """Overlay `profile["topology_annotations"]` onto `snapshot_`. A manual
    annotation replaces a `heuristic` guess (or an absent `transport`), but
    an `observed` transport reading is never overridden — manual input
    corrects a guess, it does not contradict a measurement."""
    from src.contracts.inference import identity_key

    annotations = (profile or {}).get("topology_annotations") or {}
    if not annotations:
        return snapshot_
    new_gpus: List[GpuInfo] = []
    changed = False
    for g in snapshot_.gpus:
        key = identity_key(g)
        entry = annotations.get(key) if key else None
        raw = (entry or {}).get("transport") if isinstance(entry, dict) else None
        if raw is None or (g.transport is not None and g.transport.source == "observed"):
            new_gpus.append(g)
            continue
        try:
            manual = TransportInfo.parse(raw, f"topology_annotations[{key}]")
        except ContractError as e:
            logger.debug("gpu_topology: skipping unreadable annotation for %s: %s", key, e)
            new_gpus.append(g)
            continue
        new_gpus.append(replace(g, transport=manual))
        changed = True
    if not changed:
        return snapshot_
    return replace(snapshot_, gpus=tuple(new_gpus))
