"""remote_worker_registry.py — HW-06: remote nodes as a controlled service.

A registry of PAIRED machines, kept apart from inference itself (the backend
declaration this feeds, `remote_worker` in src/capability_registry.py, is
"administration of the node", not "run this job there" — see that module's
own separation note). Three things this module refuses to reimplement:

  * pairing is `src.ssh_trust` (`is_paired`/`pair_host`/`ssh_argv`) — a host
    that is not already SSH-trusted is refused registration outright, so a
    remote node can never be added behind the person's back, and a health
    probe is the same `ssh_argv` every other SSH call in this codebase goes
    through, never a second SSH implementation;
  * a node's own capabilities/hardware are `src.hardware_profiles.
    collect_profile(host=...)` (HW-05) — this module does not detect GPUs on
    its own, it asks the same detector the picker uses;
  * "administration vs inference" is `src.capability_registry`'s
    responsibility — this module answers "is there a healthy paired node",
    that module turns that into "can I route work to remote_worker".

QA-27 ("Caída de nodo"): `reconcile()` is the acceptance criterion, almost
verbatim — for every node that does not answer a fresh health probe, it
releases whatever reservations it held (nothing stays "reserved forever" for
a machine that is gone) and returns a diagnostic row naming what happened,
never a silent/false success. `check_health`'s `probe` parameter is how a
test (or this function) exercises "the node vanished mid-generation" without
a real second machine -- COMUN rule "no real loads in tests".
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

NODE_STATES = ("healthy", "unreachable", "unknown")
#: A node not confirmed healthy within this long is treated as gone for
#: `reconcile()` purposes even if nobody called `check_health` on it since.
HEALTH_TIMEOUT_SECONDS = 45.0
_PROBE_TIMEOUT_SECONDS = 10

_LOCK = threading.Lock()
_NODES: Dict[str, Dict[str, Any]] = {}
_RESERVATIONS: Dict[str, Dict[str, Any]] = {}  # reservation id -> {id, node_id, job_id, created}

ProbeFn = Callable[[str, Optional[str]], bool]


def register_node(host: str, *, ssh_port: str = "", label: str = "",
                  capabilities: Optional[List[str]] = None) -> Dict[str, Any]:
    """Register an already-paired machine. Refuses anything `ssh_trust` does
    not already trust -- pairing stays a deliberate, separate step (Settings
    → Remote nodes → Pair), never implied by registering a name here."""
    from src import ssh_trust
    host = str(host or "").strip()
    if not host:
        raise ValueError("host is required")
    if not ssh_trust.is_paired(host, ssh_port or None):
        raise ValueError(f"{host} is not SSH-paired yet — pair it before registering it as a node")
    node_id = uuid.uuid4().hex[:12]
    node = {
        "id": node_id, "host": host, "ssh_port": ssh_port or None,
        "label": label or host, "capabilities": list(capabilities or ()),
        "registered_at": time.time(), "last_seen": None, "status": "unknown",
        "last_error": None,
    }
    with _LOCK:
        _NODES[node_id] = node
    return dict(node)


def list_nodes() -> List[Dict[str, Any]]:
    with _LOCK:
        return [dict(n) for n in _NODES.values()]


def get_node(node_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        node = _NODES.get(node_id)
        return dict(node) if node else None


def forget_node(node_id: str) -> bool:
    """Remove a node and release anything it held reserved -- a forgotten
    node must not leave a job believing it still has a machine."""
    with _LOCK:
        if node_id not in _NODES:
            return False
        _NODES.pop(node_id, None)
        stale = [rid for rid, r in _RESERVATIONS.items() if r["node_id"] == node_id]
        for rid in stale:
            _RESERVATIONS.pop(rid, None)
    return True


def _default_probe(host: str, ssh_port: Optional[str]) -> bool:
    """A trivial SSH round-trip (`true`) — the same `ssh_argv` wiring every
    other SSH call in this codebase goes through, not a second one."""
    from src import ssh_trust
    import subprocess
    try:
        argv = ssh_trust.ssh_argv(host, ssh_port, "true", connect_timeout=_PROBE_TIMEOUT_SECONDS)
        r = subprocess.run(argv, capture_output=True, timeout=_PROBE_TIMEOUT_SECONDS + 5)
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001 - a probe never raises, it answers "not healthy"
        logger.debug("remote_worker_registry: probe failed for %s: %s", host, e)
        return False


def check_health(node_id: str, *, probe: Optional[ProbeFn] = None) -> Dict[str, Any]:
    """Probe one node now and update its stored status. `probe` is swappable
    so a test (or a scripted "the node just vanished" drill) never needs a
    real second machine to exercise the unreachable path."""
    probe = probe or _default_probe
    with _LOCK:
        node = _NODES.get(node_id)
        if node is None:
            raise KeyError(node_id)
        host, ssh_port = node["host"], node["ssh_port"]
    try:
        ok = bool(probe(host, ssh_port))
        err = None
    except Exception as e:  # noqa: BLE001 - a broken probe still yields a verdict
        ok, err = False, str(e)[:200]
    with _LOCK:
        node = _NODES.get(node_id)
        if node is None:
            raise KeyError(node_id)
        node["status"] = "healthy" if ok else "unreachable"
        node["last_error"] = None if ok else (err or "health probe failed")
        if ok:
            node["last_seen"] = time.time()
        return dict(node)


def is_stale(node: Dict[str, Any], *, now: Optional[float] = None, timeout: float = HEALTH_TIMEOUT_SECONDS) -> bool:
    last_seen = node.get("last_seen")
    if last_seen is None:
        return True
    return (now if now is not None else time.time()) - float(last_seen) > timeout


# ── reservations: a node held for one job at a time ─────────────────────────


def reserve_node(node_id: str, job_id: str) -> str:
    """Reserve `node_id` for `job_id`. Refuses a node already known
    unreachable -- reserving a machine that just failed its last probe would
    hand a job a resource that cannot start it, exactly what QA-27 exists to
    prevent from the other end."""
    with _LOCK:
        node = _NODES.get(node_id)
        if node is None:
            raise KeyError(node_id)
        if node["status"] == "unreachable":
            raise RuntimeError(f"{node['label']} is unreachable — not reserving it")
        reservation_id = uuid.uuid4().hex[:12]
        _RESERVATIONS[reservation_id] = {
            "id": reservation_id, "node_id": node_id, "job_id": job_id, "created": time.time(),
        }
        return reservation_id


def release_reservation(reservation_id: str) -> bool:
    with _LOCK:
        return _RESERVATIONS.pop(reservation_id, None) is not None


def reservations_for_node(node_id: str) -> List[Dict[str, Any]]:
    with _LOCK:
        return [dict(r) for r in _RESERVATIONS.values() if r["node_id"] == node_id]


def reservations_snapshot() -> List[Dict[str, Any]]:
    with _LOCK:
        return [dict(r) for r in _RESERVATIONS.values()]


# ── QA-27: a node gone mid-job ───────────────────────────────────────────────


def reconcile(*, probe: Optional[ProbeFn] = None) -> List[Dict[str, Any]]:
    """Probe every registered node fresh; for each one that is NOT healthy,
    release whatever it held reserved and return a diagnostic row. The
    acceptance text this answers to, almost verbatim: "Diagnóstico,
    liberación/reconciliación y resultado parcial/incierto; no éxito falso."

    A node with no reservation held still gets a row (diagnosed, nothing to
    release) — silence about an unreachable node is exactly the false
    confidence this exists to avoid; only healthy nodes are left out."""
    reports: List[Dict[str, Any]] = []
    for node in list_nodes():
        health = check_health(node["id"], probe=probe)
        if health["status"] == "healthy":
            continue
        held = reservations_for_node(node["id"])
        for r in held:
            release_reservation(r["id"])
        reports.append({
            "node_id": node["id"],
            "label": node["label"],
            "status": health["status"],
            "released_job_ids": [r["job_id"] for r in held],
            "result": "uncertain" if held else "unreachable_idle",
            "diagnosis": (
                f"{node['label']} did not answer a health probe; released "
                f"{len(held)} reservation(s) — any job holding one sees an "
                f"uncertain result, never a false success."
                if held else
                f"{node['label']} did not answer a health probe; it was not "
                f"holding any reservation."
            ),
        })
    return reports
