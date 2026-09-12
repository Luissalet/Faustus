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
import threading
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


# ── Reservations (HW-01) ─────────────────────────────────────────────────────
#
# `assess()` answers "does it fit right now" from a fresh /api/ps + nvidia-smi
# reading. Two jobs asking that question a few milliseconds apart both read
# the same free memory and both get "yes" — neither has loaded anything yet,
# so nothing on the card contradicts either of them. That is exactly the
# 08-09-2026 failure: two 27B models, each individually fine, admitted within
# the same instant. `reserve()`/`try_reserve()` close that window: a
# reservation is bytes provisionally taken out of a budget the instant a job
# is told "proceed", released the moment `assess()` next sees the model
# actually resident (the cheap /api/ps check it already does on every call —
# no extra polling needed) or after `ttl` seconds, whichever comes first, so a
# job that crashes before loading does not starve the budget forever.
#
# Keyed per `(root, device)` — `device=None` is the whole pool, an explicit
# GPU index reserves against that card alone — so a caller that knows which
# card a model will land on (src/gpu_placement.py) can reserve narrower than
# the pool. `assess()`/`admit()` reserve at the pool level, matching the
# budget they already compute at that granularity.
RESERVATION_TTL_SECONDS = 180.0

_RES_LOCK = threading.Lock()
_RESERVATIONS: Dict[str, Dict[str, Any]] = {}


def _reservation_key(root: str, device: Optional[int]) -> str:
    return f"{root}|{'pool' if device is None else f'gpu{device}'}"


def _expire_reservations_locked(now: float) -> None:
    dead = [rid for rid, r in _RESERVATIONS.items() if now - r["created"] > r["ttl"]]
    for rid in dead:
        stale = _RESERVATIONS.pop(rid, None)
        if stale:
            logger.info("vram admission: reservation %s for %s expired after %.0fs unclaimed",
                        rid, stale["model"], stale["ttl"])


def reserved_bytes(root: str, *, device: Optional[int] = None) -> int:
    """Bytes currently set aside against `root`'s pool (or one GPU of it)."""
    key = _reservation_key(root, device)
    with _RES_LOCK:
        _expire_reservations_locked(time.time())
        return sum(r["bytes"] for r in _RESERVATIONS.values() if r["key"] == key)


def try_reserve(root: str, model: str, bytes_needed: int, budget_bytes: int, *,
                device: Optional[int] = None, ttl: float = RESERVATION_TTL_SECONDS) -> Optional[str]:
    """Atomic test-and-set: reserve `bytes_needed` against `budget_bytes` only
    if what is already reserved leaves room for it. Returns the reservation id
    on success, None when another reservation already claims that room — the
    caller then treats this exactly like "does not fit" instead of racing
    whoever got there first for the same memory.

    The read (what is already reserved) and the write (adding this one) happen
    under one lock, which is the whole fix for QA-24: `assess()` alone can
    only ever report a snapshot, and two snapshots taken microseconds apart
    can both be true at the moment they were taken.
    """
    key = _reservation_key(root, device)
    now = time.time()
    with _RES_LOCK:
        _expire_reservations_locked(now)
        already = sum(r["bytes"] for r in _RESERVATIONS.values() if r["key"] == key)
        if already + max(0, int(bytes_needed)) > max(0, int(budget_bytes)):
            return None
        rid = f"rsv-{uuid.uuid4().hex[:12]}"
        _RESERVATIONS[rid] = {"key": key, "root": root, "model": model,
                              "bytes": max(0, int(bytes_needed)), "device": device,
                              "created": now, "ttl": float(ttl)}
        return rid


def release_reservation(reservation_id: Optional[str]) -> None:
    if not reservation_id:
        return
    with _RES_LOCK:
        _RESERVATIONS.pop(reservation_id, None)


def _release_for_model_locked(root: str, model: str) -> None:
    want = str(model).strip().lower()
    dead = [rid for rid, r in _RESERVATIONS.items()
            if r["root"] == root and str(r["model"]).strip().lower() == want]
    for rid in dead:
        _RESERVATIONS.pop(rid, None)


def release_reservations_for_model(root: str, model: str) -> None:
    """The model is confirmed resident (or gone): its reservation, if any, no
    longer protects anything real and would only shrink the budget for the
    next job. Safe to call even when there is nothing to release."""
    with _RES_LOCK:
        _release_for_model_locked(root, model)


def reservations_snapshot() -> List[Dict[str, Any]]:
    """For diagnostics/tests: every reservation currently held, expired ones
    already swept."""
    with _RES_LOCK:
        _expire_reservations_locked(time.time())
        return [dict(r, id=rid) for rid, r in _RESERVATIONS.items()]


# -- Residency: pins and last-active (HW-03) --------------------------------
#
# `assess()`'s `suggestion` used to be pure biggest-first: whichever resident
# frees the shortfall fastest, with no notion of "in the middle of a job" or
# "the person asked to keep this one loaded". That is exactly the failure
# HW-03 names: a quick conversation on a small model suggests evicting the
# big one a slow job is mid-generation on, or a router alternates between two
# models and each `assess()` call proposes unloading whichever is not the
# current pick -- a model that was resident a second ago gets suggested again
# a second later, purely because nothing here remembers it was just in use.
#
# Two protections, both keyed the same way reservations are (`root|model`),
# both advisory to `suggestion` only -- they never change `fits`/`shortfall`,
# so a caller that ignores them still gets a correct fit/no-fit verdict:
#
#   * a PIN never appears in `suggestion` unless every unpinned resident
#     combined still cannot free the shortfall (then it is offered anyway,
#     with `protected_used` naming it -- silently starving a load nobody can
#     ever admit would be worse than one honest pin override);
#   * a model observed resident within `RESIDENCY_GRACE_SECONDS` of "now" is
#     treated the same way: recently-active residents are the last ones
#     picked, not the first, so a job mid-generation on model A does not get
#     A suggested away the instant model B's chat asks whether B fits.
RESIDENCY_GRACE_SECONDS = 20.0

_PIN_LOCK = threading.Lock()
_PINS: Dict[str, set] = {}          # root -> {model, ...}
_LAST_ACTIVE: Dict[str, float] = {}  # "root|model" -> time.time()


def _active_key(root: str, name: str) -> str:
    return f"{root}|{str(name or '').strip().lower()}"


def _note_active(root: str, name: str) -> None:
    if not name:
        return
    with _PIN_LOCK:
        _LAST_ACTIVE[_active_key(root, name)] = time.time()


def _seconds_since_active(root: str, name: str, *, now: Optional[float] = None) -> Optional[float]:
    with _PIN_LOCK:
        seen = _LAST_ACTIVE.get(_active_key(root, name))
    if seen is None:
        return None
    return max(0.0, (now if now is not None else time.time()) - seen)


def pin_model(root: str, model: str) -> None:
    """Keep `model` out of eviction suggestions on `root` until unpinned."""
    name = str(model or "").strip()
    if not name:
        raise ValueError("model is required")
    with _PIN_LOCK:
        _PINS.setdefault(root, set()).add(name.lower())


def unpin_model(root: str, model: str) -> None:
    name = str(model or "").strip().lower()
    with _PIN_LOCK:
        pins = _PINS.get(root)
        if pins:
            pins.discard(name)
            if not pins:
                _PINS.pop(root, None)


def is_pinned(root: str, model: str) -> bool:
    name = str(model or "").strip().lower()
    with _PIN_LOCK:
        return name in _PINS.get(root, set())


def pinned_models(root: str) -> List[str]:
    with _PIN_LOCK:
        return sorted(_PINS.get(root, set()))


def residency_status(root: str, residents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`residents` (as returned inside `assess()`) enriched with pin/last-active
    for a status panel -- never fetched on its own, always derived from a
    reading `assess()` already took, so this never costs an extra /api/ps."""
    now = time.time()
    out = []
    for r in residents:
        name = r.get("name", "")
        age = _seconds_since_active(root, name, now=now)
        out.append({
            **r,
            "pinned": is_pinned(root, name),
            "seconds_since_active": age,
            "in_grace": age is not None and age < RESIDENCY_GRACE_SECONDS,
        })
    return out


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
    # Every chat turn passes through here (10-09-2026). The common case — the
    # model is already inside — must cost one /api/ps and nothing else: no
    # nvidia-smi, no /api/tags.
    try:
        quick = _get(root, "/api/ps", 2.5).get("models") or []
        want = str(model).strip().lower()
        for m in quick:
            names = {str(m.get("name") or "").lower(), str(m.get("model") or "").lower()}
            if want in names or (":" not in want and f"{want}:latest" in names):
                out["fits"] = True
                out["already_resident"] = True
                # It is really in VRAM now: whatever it reserved to get there
                # no longer protects anything and would only starve the next
                # job's budget. This is the "or caduca" clause's twin: release
                # the instant residency is observed, TTL only for the case
                # where this call never comes (the load crashed first).
                release_reservations_for_model(root, model)
                # HW-03: this IS the "every chat turn passes through here"
                # signal the residency grace period is built on -- a model
                # answered from is a model in use, whether or not it shows up
                # in any `suggestion` list computed elsewhere this second.
                _note_active(root, model)
                return out
    except Exception:  # noqa: BLE001 - fall through to the full reading, which reports it
        pass
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
        release_reservations_for_model(root, model)
        _note_active(root, model)  # HW-03: this turn is using it right now
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
    # Nor is what another job has reserved (HW-01): it has not loaded yet
    # either, so /api/ps says nothing about it, but the room is already
    # spoken for. Without this, two assess() calls a moment apart both see
    # the full budget and both say "fits" for memory that only exists once.
    reserved = reserved_bytes(root)
    budget_alongside = max(0, budget_alongside - reserved)

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
        # HW-01: a model whose KV cost was never actually measured (never
        # loaded, so vram_fit.KV_RATES has nothing for it) gets judged
        # against a weights-only floor plus a wide headroom band, not a real
        # KV estimate — labeling it "measured" would claim precision this
        # number does not have. "minimum" says plainly what it is: the least
        # this model could need, not a prediction of what it will.
        "estimate": "measured" if measured else "minimum",
        "size_bytes": size, "kv_bytes": kv_bytes, "kv_ctx": kv_ctx,
        "footprint_bytes": footprint, "headroom_bytes": headroom, "need_bytes": need,
        "budget_alongside_bytes": budget_alongside,
        "budget_if_unloaded_bytes": int(block["budget_bytes"]),
        "reserved_bytes": reserved,
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
        #
        # HW-03: split residents into free-to-suggest and protected (pinned,
        # or seen active within RESIDENCY_GRACE_SECONDS) *before* applying
        # that rule. Protected residents are tried only if the unprotected
        # ones cannot cover the shortfall on their own -- suggesting an
        # unrelated model over one that answered a turn a moment ago is the
        # whole point; refusing to ever suggest it, even as a last resort,
        # would silently strand a load nobody could ever admit.
        now = time.time()

        def _protected(r: Dict[str, Any]) -> bool:
            if is_pinned(root, r["name"]):
                return True
            age = _seconds_since_active(root, r["name"], now=now)
            return age is not None and age < RESIDENCY_GRACE_SECONDS

        free_residents = [r for r in residents if not _protected(r)]
        protected_residents = [r for r in residents if _protected(r)]

        def _fill(pool: List[Dict[str, Any]], already_freed: int) -> tuple:
            freed = already_freed
            picked: List[str] = []
            for r in pool:
                if freed >= shortfall:
                    break
                picked.append(r["name"])
                freed += r["in_vram_bytes"]
            return picked, freed

        pick, freed = _fill(free_residents, 0)
        protected_used: List[str] = []
        if freed < shortfall and protected_residents:
            # Least-recently-active first among the protected set: whichever
            # was NOT just used is the least bad one to name, still biggest
            # first only among ties old enough to matter equally.
            protected_by_age = sorted(
                protected_residents,
                key=lambda r: (_seconds_since_active(root, r["name"], now=now) or 0.0),
                reverse=True,
            )
            more, freed = _fill(protected_by_age, freed)
            pick.extend(more)
            protected_used = more
        out["suggestion"] = pick
        out["suggestion_frees_bytes"] = freed
        out["suggestion_enough"] = freed >= shortfall
        out["suggestion_protected_used"] = protected_used
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
                mode: Optional[str] = None, timeout: Optional[float] = None,
                waited_out: Optional[Dict[str, Any]] = None) -> str:
    """Clear the way for `model` on `endpoint_url`, or refuse.

    Returns "proceed" when the caller may load. Raises AdmissionCancelled when
    it may not â€” the person said cancel, or nobody answered in time. Silence
    never loads a model that would not fit: that is the one rule that would
    have kept the machine up on 08-09.

    INF-03: when `waited_out` is given, this fills in `waited_out["waited_s"]`
    (a monotonic duration) on every return/raise past the point this gate
    actually engaged â€” a real door, even one that opened immediately (`fits`,
    already resident, `mode="auto"`). The two early returns above this
    comment (no local Ollama endpoint at all; admission turned `off`) leave
    it untouched: there was no door, so the caller's execution metrics must
    read this as `absent`, not a measured zero-second wait.
    """
    _gate_t0 = time.monotonic()

    def _mark_wait() -> None:
        if waited_out is not None:
            waited_out["waited_s"] = round(max(0.0, time.monotonic() - _gate_t0), 3)

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
    if a.get("fits") is None or a.get("already_resident"):
        _mark_wait()
        return "proceed"  # unknowable, or already using the memory it would ask for
    if a.get("fits") is True:
        need = int(a.get("need_bytes") or a.get("footprint_bytes") or 0)
        budget = int(a.get("budget_alongside_bytes") or 0)
        if need <= 0:
            _mark_wait()
            return "proceed"  # nothing to reserve for (e.g. a zero-footprint reading)
        reservation_id = try_reserve(root, model, need, budget)
        if reservation_id is not None:
            # Held until assess() next sees `model` resident or RESERVATION_TTL_SECONDS
            # passes (release_reservations_for_model / _expire_reservations_locked).
            _mark_wait()
            return "proceed"
        # QA-24: this read of "it fits" was true a moment ago; another admit()
        # reserved the room in between. Treat it exactly like "does not fit"
        # instead of both of us loading into the same free bytes.
        a = dict(a)
        a["fits"] = False
        a["reason"] = a.get("reason") or f"{model} would fit, but another load just reserved that room"
    logger.info("vram admission: %s needs %.1f GB, %.1f GB free alongside %d resident â€” %s",
                model, a["need_bytes"] / 2**30, a["budget_alongside_bytes"] / 2**30,
                len(a["residents"]), mode)

    if mode == "auto":
        names = list(a.get("suggestion") or [])
        if not names:
            say({"phase": "warning", "message": f"{model} does not fit and nothing can be unloaded to make room."})
            _mark_wait()
            return "proceed"
        left = await unload_and_wait(root, names, on_progress=on_progress)
        if left:
            say({"phase": "warning", "message": f"Still resident after unload: {', '.join(left)}"})
        _mark_wait()
        return "proceed"

    # mode == "ask": the person decides.
    t = open_ticket(root, model, a, owner=owner)
    say({"phase": "vram_blocked", "ticket": t.id, "message": f"{model} does not fit in VRAM", **a})
    try:
        try:
            await asyncio.wait_for(t.event.wait(), timeout=timeout or _timeout())
        except asyncio.TimeoutError:
            say({"phase": "error", "message": f"No answer about VRAM for {model}; the load was cancelled."})
            _mark_wait()
            raise AdmissionCancelled(
                f"{model} does not fit in VRAM next to what is loaded, and nobody chose what "
                "to unload in time. Nothing was loaded.")
        d = t.decision or {}
        action = d.get("action")
        if action == "cancel":
            say({"phase": "error", "message": f"Load of {model} cancelled: it does not fit in VRAM."})
            _mark_wait()
            raise AdmissionCancelled(f"Load of {model} cancelled by {d.get('by') or 'the user'}: "
                                     "it does not fit in VRAM next to what is loaded.")
        if action == "proceed":
            say({"phase": "warning", "message": f"Loading {model} anyway â€” expect it to spill to CPU/PCIe."})
            _mark_wait()
            return "proceed"
        # unload: the new model waits until the chosen ones are really gone.
        names = list(d.get("names") or [])
        left = await unload_and_wait(root, names, on_progress=on_progress)
        if left:
            say({"phase": "error", "message": f"Could not unload {', '.join(left)}; the load was cancelled."})
            _mark_wait()
            raise AdmissionCancelled(f"{', '.join(left)} stayed resident after the unload; "
                                     f"{model} was not loaded.")
        _mark_wait()
        return "proceed"
    finally:
        _forget(t.id)
