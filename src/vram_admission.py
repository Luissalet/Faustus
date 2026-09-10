"""Ask before loading a local model that does not fit next to what is resident.

Ollama never says no. A model that does not fit is loaded anyway, with layers
on the CPU and weights paged over PCIe, and the only symptom is a job that runs
ten times slower with no explanation. With another large model already inside
it does not even get that far: `cudaMalloc failed: out of memory` in the
middle of a run. The night of 08-09-2026 that was the first domino â€” two 27B
models resident at once, and the whole machine went down with them.

So the decision is made here, before the load, by the person: this is what is
inside, this is how much each holds, this is what the new one needs â€” pick
what to unload. The new model is loaded only once the picked ones are gone
from `/api/ps`. Nothing is inferred about a model we have never measured: a
footprint we do not know is reported as a floor, never dressed up as a number.

Reuses, does not reimplement: src/vram_fit.py (budgets, KV_RATES),
src/gpu_shared_memory.py (the card), src/gpu_placement.py (whose bytes are on
which card). The unload is the same `keep_alive: 0` the Local models screen
sends.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MODES = ("ask", "auto", "off")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}
# Same margins the picker uses (routes/model_routes.py): a measured footprint
# only needs room for compute buffers; a weights-only figure hides a whole KV
# cache behind it, so it needs a band wide enough to stand in for one.
HEADROOM_MEASURED = 512 * 1024 * 1024
HEADROOM_WEIGHTS_ONLY = 1536 * 1024 * 1024
# After an unload, how long we give Ollama to actually drop the runner.
UNLOAD_WAIT_SECONDS = 120


# â”€â”€ Where the model would load â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def ollama_root(endpoint_url: str) -> Optional[str]:
    """`http://127.0.0.1:11434/v1/chat/completions` â†’ `http://127.0.0.1:11434`,
    and only for an Ollama on THIS machine. A LAN or tailnet Ollama has its
    own card; a verdict against our nvidia-smi reading would be a confident
    lie, so those get no gate at all."""
    url = str(endpoint_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if (p.hostname or "").lower() not in _LOCAL_HOSTS:
        return None
    if (p.port or 80) != 11434 and "ollama" not in (p.hostname or "").lower():
        # llama.cpp on 8080 has no /api/ps to ask; it is not ours to gate.
        return None
    return f"{p.scheme}://{p.hostname}:{p.port or 11434}"


def _get(root: str, path: str, timeout: float) -> Dict[str, Any]:
    import httpx
    from src.tls_overrides import llm_verify
    r = httpx.get(root + path, timeout=timeout, verify=llm_verify())
    r.raise_for_status()
    return r.json() or {}


def _evict(root: str, name: str) -> bool:
    """`keep_alive: 0` â€” the same request the Local models screen sends.
    Embedding models only answer /api/embed, hence the second try."""
    import httpx
    from src.tls_overrides import llm_verify
    for path, extra in (("/api/generate", {}), ("/api/embed", {"input": ""})):
        try:
            r = httpx.post(root + path, json={"model": name, "keep_alive": 0, **extra},
                           timeout=30.0, verify=llm_verify())
        except Exception as e:  # noqa: BLE001
            logger.warning("vram admission: evict %s via %s failed: %s", name, path, e)
            continue
        if r.status_code < 400:
            return True
        if r.status_code == 404:
            return True  # not installed any more: nothing to evict
    return False


# â”€â”€ The verdict â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def assess(root: str, model: str) -> Dict[str, Any]:
    """Does `model` fit NEXT TO what is resident right now?

    Returns a dict the dialog can render as-is. `fits` is True, False, or None
    when we cannot tell (no card, no nvidia-smi, model not installed) â€” None
    never blocks a load; ignorance is not a reason to stop anyone.

    The budget question here is the opposite of the picker's. The picker
    counts what the runner holds as free, because switching models frees it.
    Admission asks whether the new one fits *alongside*, so what the runner
    holds is taken back out of the budget â€” that is the whole point.
    """
    from src import gpu_placement, gpu_shared_memory, vram_fit

    out: Dict[str, Any] = {"model": model, "root": root, "fits": None, "residents": [],
                           "suggestion": [], "measured": False}
    vram = gpu_shared_memory.vram_snapshot()
    if not vram.get("supported"):
        out["reason"] = str(vram.get("reason") or "no GPU reading")
        return out

    try:
        tags = _get(root, "/api/tags", 3.0).get("models") or []
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"/api/tags: {e}"
        return out
    sizes: Dict[str, int] = {}
    digests: Dict[str, str] = {}
    for m in tags:
        n = str(m.get("name") or m.get("model") or "")
        if n:
            sizes[n] = int(m.get("size") or 0)
            if m.get("digest"):
                digests[n] = str(m["digest"])

    try:
        loaded = _get(root, "/api/ps", 2.5).get("models") or []
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"/api/ps: {e}"
        return out

    residents: List[Dict[str, Any]] = []
    held = 0
    for m in loaded:
        n = str(m.get("name") or m.get("model") or "")
        if not n:
            continue
        total = int(m.get("size") or 0)
        in_vram = int(m.get("size_vram") or 0)
        ctx = int(m.get("context_length") or 0)
        held += in_vram
        key = str(m.get("digest") or "") or digests.get(n) or n
        # Learn the footprint while it is visible â€” the only time it ever is.
        rate = vram_fit.kv_bytes_per_token_measured(total, sizes.get(n) or 0, ctx)
        if rate:
            vram_fit.remember_kv_rate(key, rate, ctx)
        residents.append({
            "name": n, "key": key, "total_bytes": total, "in_vram_bytes": in_vram,
            "spill_bytes": max(0, total - in_vram) if in_vram else 0, "ctx": ctx,
            "expires_at": str(m.get("expires_at") or ""),
        })
    residents.sort(key=lambda r: r["in_vram_bytes"], reverse=True)
    out["residents"] = residents

    if any(r["name"] == model or r["key"] == digests.get(model) for r in residents):
        out["fits"] = True
        out["already_resident"] = True
        return out

    size = sizes.get(model) or 0
    if size <= 0:
        out["reason"] = f"{model} is not installed on this Ollama"
        return out

    total = int(vram.get("total") or 0)
    used = int(vram.get("used") or 0)
    others = max(0, used - held)  # a browser, ComfyUI: not ours to evict
    placements = gpu_placement.placement(root, loaded, vram.get("gpus"))
    block = vram_fit.pool_budgets(vram, held_by_runner_bytes=held, others_bytes=others,
                                  placements=placements)
    # `budget_bytes` treats what the runner holds as free. Alongside, it is not.
    budget_alongside = max(0, int(block["budget_bytes"]) - held)

    rate = vram_fit.KV_RATES.get(digests.get(model) or model) or {}
    kv_ctx = int(rate.get("ctx") or 0)
    kv_bytes = int(rate.get("per_token", 0.0) * kv_ctx)
    measured = kv_bytes > 0
    footprint = size + kv_bytes
    headroom = HEADROOM_MEASURED if measured else HEADROOM_WEIGHTS_ONLY
    need = footprint + headroom
    shortfall = max(0, need - budget_alongside)

    out.update({
        "fits": shortfall == 0,
        "measured": measured,
        "size_bytes": size, "kv_bytes": kv_bytes, "kv_ctx": kv_ctx,
        "footprint_bytes": footprint, "headroom_bytes": headroom, "need_bytes": need,
        "budget_alongside_bytes": budget_alongside,
        "budget_if_unloaded_bytes": int(block["budget_bytes"]),
        "held_by_runner_bytes": held, "others_bytes": others,
        "vram_total_bytes": total, "gpu_count": int(block.get("count") or 1),
        "gpu_name": str(block.get("name") or ""),
        "shortfall_bytes": shortfall,
    })
    if shortfall:
        # "Load anyway" means the shortfall lands in system RAM. On 08-09-2026
        # that is exactly what ran the machine out of commit memory, so the
        # dialog is told how much RAM is really free for it and whether the
        # spill would eat it. psutil is optional; without it we say nothing.
        try:
            import psutil
            vm = psutil.virtual_memory()
            ram_available = int(vm.available)
            out["ram_available_bytes"] = ram_available
            out["ram_total_bytes"] = int(vm.total)
            out["spill_if_forced_bytes"] = shortfall
            # Leave the OS and everything else a quarter of what is free.
            out["forced_load_dangerous"] = shortfall > ram_available * 0.75
        except Exception:  # noqa: BLE001
            pass
        # Smallest set of residents, biggest first, that frees the shortfall.
        # Biggest first is not a preference, it is arithmetic: one 33 GB model
        # gone beats three small ones and leaves the small ones for later.
        freed = 0
        pick: List[str] = []
        for r in residents:
            if freed >= shortfall:
                break
            pick.append(r["name"])
            freed += r["in_vram_bytes"]
        out["suggestion"] = pick
        out["suggestion_frees_bytes"] = freed
        out["suggestion_enough"] = freed >= shortfall
    return out


# â”€â”€ The open question â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#
# One row per load that is waiting on a person. The loader holds the Event;
# the route that receives the click resolves it. Owner-scoped: you answer
# your own questions, an admin answers anyone's.

ACTIONS = ("unload", "proceed", "cancel")


@dataclass
class Ticket:
    id: str
    model: str
    root: str
    owner: str
    assessment: Dict[str, Any]
    created: float = field(default_factory=time.time)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    decision: Optional[Dict[str, Any]] = None

    def public(self) -> Dict[str, Any]:
        return {"id": self.id, "model": self.model, "owner": self.owner,
                "created": self.created, "resolved": self.decision is not None,
                "decision": self.decision, **self.assessment}


_PENDING: Dict[str, Ticket] = {}


def open_ticket(root: str, model: str, assessment: Dict[str, Any], *, owner: str = "") -> Ticket:
    t = Ticket(id=f"va-{uuid.uuid4().hex[:12]}", model=model, root=root, owner=owner or "",
               assessment=assessment)
    _PENDING[t.id] = t
    return t


def get_ticket(ticket_id: str) -> Optional[Ticket]:
    return _PENDING.get(ticket_id)


def pending(*, owner: Optional[str] = None) -> List[Dict[str, Any]]:
    return [t.public() for t in _PENDING.values()
            if t.decision is None and (owner is None or t.owner == owner)]


def resolve(ticket_id: str, *, action: str, names: Optional[List[str]] = None,
            by: str = "") -> bool:
    """The click. Returns False for an unknown or already-answered ticket."""
    t = _PENDING.get(ticket_id)
    if t is None or t.decision is not None:
        return False
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}")
    resident = {r["name"] for r in t.assessment.get("residents") or []}
    chosen = [n for n in (names or []) if n in resident]
    if action == "unload" and not chosen:
        raise ValueError("unload needs at least one resident model")
    t.decision = {"action": action, "names": chosen, "by": by, "at": time.time()}
    t.event.set()
    return True


def _forget(ticket_id: str) -> None:
    _PENDING.pop(ticket_id, None)


# â”€â”€ Unload, and mean it â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def unload_and_wait(root: str, names: List[str], *, timeout: float = UNLOAD_WAIT_SECONDS,
                          on_progress: Optional[Callable[[Dict[str, Any]], None]] = None) -> List[str]:
    """Evict `names` and wait until `/api/ps` no longer lists them.

    `keep_alive: 0` is a request, not a fact: the runner takes a moment to
    let go, and loading the next model into memory that is still being freed
    is how we got `cudaMalloc failed`. Returns the names still resident when
    the wait ran out (empty on success)."""
    def say(msg: str) -> None:
        if on_progress:
            try:
                on_progress({"phase": "unloading_model", "message": msg, "names": names})
            except Exception:  # noqa: BLE001
                pass

    say(f"Unloading {', '.join(names)}â€¦")
    for n in names:
        ok = await asyncio.to_thread(_evict, root, n)
        if not ok:
            logger.warning("vram admission: Ollama did not accept unload of %s", n)
    deadline = time.time() + timeout
    still = list(names)
    while time.time() < deadline:
        try:
            loaded = (await asyncio.to_thread(_get, root, "/api/ps", 2.5)).get("models") or []
        except Exception:  # noqa: BLE001
            loaded = None
        if loaded is not None:
            live = {str(m.get("name") or m.get("model") or "") for m in loaded}
            still = [n for n in names if n in live]
            if not still:
                logger.info("vram admission: %s gone from /api/ps", ", ".join(names))
                return []
        await asyncio.sleep(1.5)
    logger.warning("vram admission: still resident after %ss: %s", timeout, ", ".join(still))
    return still


# â”€â”€ The gate â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class AdmissionCancelled(RuntimeError):
    """The load was not allowed: nobody freed room, or someone said no."""


def _mode() -> str:
    try:
        from src.settings import get_setting
        m = str(get_setting("vram_admission", "ask") or "ask").strip().lower()
    except Exception:  # noqa: BLE001
        m = "ask"
    return m if m in MODES else "ask"


def _timeout() -> float:
    try:
        from src.settings import get_setting
        return float(max(30, min(7200, int(get_setting("vram_admission_timeout_seconds", 600)))))
    except Exception:  # noqa: BLE001
        return 600.0


async def admit(endpoint_url: str, model: str, *, owner: str = "",
                on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
                mode: Optional[str] = None, timeout: Optional[float] = None) -> str:
    """Clear the way for `model` on `endpoint_url`, or refuse.

    Returns "proceed" when the caller may load. Raises AdmissionCancelled when
    it may not â€” the person said cancel, or nobody answered in time. Silence
    never loads a model that would not fit: that is the one rule that would
    have kept the machine up on 08-09.
    """
    def say(event: Dict[str, Any]) -> None:
        if on_progress:
            try:
                on_progress(event)
            except Exception:  # noqa: BLE001
                pass

    root = ollama_root(endpoint_url)
    if not root:
        return "proceed"
    mode = (mode or _mode())
    if mode == "off":
        return "proceed"

    a = await asyncio.to_thread(assess, root, model)
    if a.get("fits") is not False:
        return "proceed"  # fits, already resident, or unknowable
    logger.info("vram admission: %s needs %.1f GB, %.1f GB free alongside %d resident â€” %s",
                model, a["need_bytes"] / 2**30, a["budget_alongside_bytes"] / 2**30,
                len(a["residents"]), mode)

    if mode == "auto":
        names = list(a.get("suggestion") or [])
        if not names:
            say({"phase": "warning", "message": f"{model} does not fit and nothing can be unloaded to make room."})
            return "proceed"
        left = await unload_and_wait(root, names, on_progress=on_progress)
        if left:
            say({"phase": "warning", "message": f"Still resident after unload: {', '.join(left)}"})
        return "proceed"

    # mode == "ask": the person decides.
    t = open_ticket(root, model, a, owner=owner)
    say({"phase": "vram_blocked", "ticket": t.id, "message": f"{model} does not fit in VRAM", **a})
    try:
        try:
            await asyncio.wait_for(t.event.wait(), timeout=timeout or _timeout())
        except asyncio.TimeoutError:
            say({"phase": "error", "message": f"No answer about VRAM for {model}; the load was cancelled."})
            raise AdmissionCancelled(
                f"{model} does not fit in VRAM next to what is loaded, and nobody chose what "
                "to unload in time. Nothing was loaded.")
        d = t.decision or {}
        action = d.get("action")
        if action == "cancel":
            say({"phase": "error", "message": f"Load of {model} cancelled: it does not fit in VRAM."})
            raise AdmissionCancelled(f"Load of {model} cancelled by {d.get('by') or 'the user'}: "
                                     "it does not fit in VRAM next to what is loaded.")
        if action == "proceed":
            say({"phase": "warning", "message": f"Loading {model} anyway â€” expect it to spill to CPU/PCIe."})
            return "proceed"
        # unload: the new model waits until the chosen ones are really gone.
        names = list(d.get("names") or [])
        left = await unload_and_wait(root, names, on_progress=on_progress)
        if left:
            say({"phase": "error", "message": f"Could not unload {', '.join(left)}; the load was cancelled."})
            raise AdmissionCancelled(f"{', '.join(left)} stayed resident after the unload; "
                                     f"{model} was not loaded.")
        return "proceed"
    finally:
        _forget(t.id)
