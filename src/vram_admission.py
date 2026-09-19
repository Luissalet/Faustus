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
from typing import Any, Callable, Dict, List, Optional, Sequence, Union
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

# INF-05 B1/T12: "una reserva marcada loading nunca caduca antes del hard
# cap" — a 27B model loading over PCIe can take well past the ordinary TTL.
# Once `mark_loading()` flips a reservation's `loading` flag, it survives
# past `ttl` unconditionally until this many seconds since it was CREATED —
# only past that does `_expire_reservations_locked` give up on it (with a
# warning: this is the last-resort path, not the normal one).
HARD_CAP_SECONDS = 900.0

_RES_LOCK = threading.Lock()
_RESERVATIONS: Dict[str, Dict[str, Any]] = {}


def _canonical_root(root: str) -> str:
    """T14: `localhost`/`127.0.0.1`/`::1`/`0.0.0.0` (and the other aliases
    `ollama_root` already recognizes) all name the SAME loopback Ollama —
    collapse them to one string so a reservation or pin made against one
    alias also protects/applies against another. This is a private
    normalization used only for the reservation/pin/last-active BOOKKEEPING
    below; `ollama_root()` itself keeps returning whatever alias the
    caller's endpoint URL actually used (existing callers key off that
    verbatim string), so nothing about its public return value changes.
    Falls back to the raw string, unparsed, for anything that is not an
    http(s) URL at all (defensive — every real caller passes an
    `ollama_root()` result)."""
    raw = str(root or "")
    try:
        p = urlparse(raw)
    except ValueError:
        return raw
    host = (p.hostname or "").lower()
    if not host:
        return raw
    if host in _LOCAL_HOSTS:
        host = "127.0.0.1"
    scheme = p.scheme or "http"
    return f"{scheme}://{host}:{p.port}" if p.port else f"{scheme}://{host}"


def _reservation_key(root: str, device: Optional[Union[int, str]]) -> str:
    croot = _canonical_root(root)
    return f"{croot}|{'pool' if device is None else f'gpu{device}'}"


def _expire_reservations_locked(now: float) -> None:
    dead: List[tuple] = []
    for rid, r in _RESERVATIONS.items():
        last_heartbeat = r.get("last_heartbeat", r["created"])
        if r.get("loading"):
            if now - r["created"] <= HARD_CAP_SECONDS:
                continue  # a live load never expires before the hard cap
            dead.append((rid, "hard_cap"))
            continue
        if now - last_heartbeat > r["ttl"]:
            dead.append((rid, "ttl"))
    for rid, why in dead:
        stale = _RESERVATIONS.pop(rid, None)
        if not stale:
            continue
        if why == "hard_cap":
            logger.warning(
                "vram admission: reservation %s for %s was still marked loading past the "
                "%.0fs hard cap with no confirmation it finished — releasing it anyway",
                rid, stale["model"], HARD_CAP_SECONDS)
        else:
            logger.info("vram admission: reservation %s for %s expired after %.0fs unclaimed",
                        rid, stale["model"], stale["ttl"])


def reserved_bytes(root: str, *, device: Optional[Union[int, str]] = None,
                    include_devices: bool = False, exclude_model: str = "") -> int:
    """Bytes currently set aside against `root`'s pool (or one GPU of it).

    `include_devices=True` (opt-in; default False so nothing changes for
    current callers) additionally folds in every per-device reservation
    under the same root when asking about the pool (`device=None`) — a
    device-scoped reservation eats into the same physical capacity the pool
    figure describes, even though it is bucketed separately for the plain
    per-device query."""
    croot = _canonical_root(root)
    key = _reservation_key(root, device)
    with _RES_LOCK:
        _expire_reservations_locked(time.time())
        # `exclude_model`: a reservation this same model left behind (it
        # loaded, was unloaded between turns, and nothing re-assessed while it
        # was resident to release the slip) is our own room, not another
        # job's — counting it made a 27B "not fit" next to nothing at all.
        rows = [r for r in _RESERVATIONS.values()
                if not (exclude_model and r.get("model") == exclude_model)]
        total = sum(r["bytes"] for r in rows if r["key"] == key)
        if device is None and include_devices:
            total += sum(r["bytes"] for r in rows
                        if r["key"] != key and r["key"].startswith(f"{croot}|gpu"))
        return total


def try_reserve(root: str, model: str, bytes_needed: int, budget_bytes: int, *,
                device: Optional[Union[int, str]] = None,
                ttl: float = RESERVATION_TTL_SECONDS) -> Optional[str]:
    """Atomic test-and-set: reserve `bytes_needed` against `budget_bytes` only
    if what is already reserved leaves room for it. Returns the reservation id
    on success, None when another reservation already claims that room — the
    caller then treats this exactly like "does not fit" instead of racing
    whoever got there first for the same memory.

    The read (what is already reserved) and the write (adding this one) happen
    under one lock, which is the whole fix for QA-24: `assess()` alone can
    only ever report a snapshot, and two snapshots taken microseconds apart
    can both be true at the moment they were taken.

    `device` (T14) accepts either an index (today's usage) or a physical
    identity string (a GPU uuid) — both are just opaque key material here,
    `src/memory_budget.py`/`src/gpu_topology.py` decide which one is the
    right identity to reserve against for a given caller.
    """
    key = _reservation_key(root, device)
    now = time.time()
    with _RES_LOCK:
        _expire_reservations_locked(now)
        # A stale slip for the same model on the same key is superseded by
        # this one (see reserved_bytes): it never competes with itself.
        for rid_old in [rid for rid, r in _RESERVATIONS.items()
                        if r["key"] == key and r.get("model") == model and not r.get("loading")]:
            _RESERVATIONS.pop(rid_old, None)
        already = sum(r["bytes"] for r in _RESERVATIONS.values() if r["key"] == key)
        if already + max(0, int(bytes_needed)) > max(0, int(budget_bytes)):
            return None
        rid = f"rsv-{uuid.uuid4().hex[:12]}"
        _RESERVATIONS[rid] = {"key": key, "root": root, "model": model,
                              "bytes": max(0, int(bytes_needed)), "device": device,
                              "created": now, "ttl": float(ttl),
                              "last_heartbeat": now, "loading": False}
        return rid


def release_reservation(reservation_id: Optional[str]) -> None:
    if not reservation_id:
        return
    with _RES_LOCK:
        _RESERVATIONS.pop(reservation_id, None)


def heartbeat(reservation_id: Optional[str]) -> bool:
    """Renew a reservation's clock while its load is actually in flight.
    Returns False when the reservation is already gone (expired, force-
    capped past `HARD_CAP_SECONDS`, or released because the model was
    observed resident) — the caller then knows its protection is gone and
    must not assume the memory is still spoken for."""
    if not reservation_id:
        return False
    now = time.time()
    with _RES_LOCK:
        _expire_reservations_locked(now)
        r = _RESERVATIONS.get(reservation_id)
        if r is None:
            return False
        r["last_heartbeat"] = now
        return True


def mark_loading(reservation_id: Optional[str]) -> bool:
    """§12/T12: from this call on, `reservation_id` protects a load that is
    ACTIVELY in flight — `_expire_reservations_locked` will not sweep it on
    TTL alone before `HARD_CAP_SECONDS` has passed since it was created,
    regardless of whether anything calls `heartbeat()`. Returns False if the
    reservation is already gone."""
    if not reservation_id:
        return False
    now = time.time()
    with _RES_LOCK:
        _expire_reservations_locked(now)
        r = _RESERVATIONS.get(reservation_id)
        if r is None:
            return False
        r["loading"] = True
        r["last_heartbeat"] = now
        return True


async def heartbeat_while_loading(reservation_id: Optional[str], *, interval: float = 25.0,
                                  max_seconds: float = HARD_CAP_SECONDS - 30.0) -> None:
    """Fire-and-forget background renewal for a `loading=True` reservation —
    `asyncio.create_task(heartbeat_while_loading(rid))` right after
    `mark_loading(rid)` and forget it: it is self-terminating, stopping the
    moment `heartbeat()` reports the reservation is gone (released once the
    model is observed resident, or force-expired past the hard cap) or
    after `max_seconds` of wall-clock time, whichever comes first — never an
    unbounded task. A caller that CAN pinpoint the exact moment loading
    ended (e.g. `src/bench/runner.py` sees the first streamed token) should
    still cancel the task there instead of waiting for the bound."""
    if not reservation_id:
        return
    elapsed = 0.0
    while elapsed < max_seconds:
        await asyncio.sleep(interval)
        elapsed += interval
        if not heartbeat(reservation_id):
            return


def _release_for_model_locked(root: str, model: str) -> None:
    want = str(model).strip().lower()
    croot = _canonical_root(root)
    dead = [rid for rid, r in _RESERVATIONS.items()
            if _canonical_root(r["root"]) == croot and str(r["model"]).strip().lower() == want]
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
    return f"{_canonical_root(root)}|{str(name or '').strip().lower()}"


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
    """Keep `model` out of eviction suggestions on `root` until unpinned.
    T14: keyed by the CANONICAL root, so pinning via one loopback alias
    protects the model no matter which alias later asks."""
    name = str(model or "").strip()
    if not name:
        raise ValueError("model is required")
    with _PIN_LOCK:
        _PINS.setdefault(_canonical_root(root), set()).add(name.lower())


def unpin_model(root: str, model: str) -> None:
    name = str(model or "").strip().lower()
    croot = _canonical_root(root)
    with _PIN_LOCK:
        pins = _PINS.get(croot)
        if pins:
            pins.discard(name)
            if not pins:
                _PINS.pop(croot, None)


def is_pinned(root: str, model: str) -> bool:
    name = str(model or "").strip().lower()
    with _PIN_LOCK:
        return name in _PINS.get(_canonical_root(root), set())


def pinned_models(root: str) -> List[str]:
    with _PIN_LOCK:
        return sorted(_PINS.get(_canonical_root(root), set()))


def last_active_seconds(root: str, model: str) -> Optional[float]:
    """Public wrapper around `_seconds_since_active` for callers outside this
    module (`src/model_warmup.py`'s residency keeper) that need to know
    whether a resident model is genuinely in use, without reaching into a
    private helper directly."""
    return _seconds_since_active(root, model)


def is_default_model(root: str, model: str) -> bool:
    """X-D: is `(root, model)` the owner's default chat model? Delegates to
    `src.model_warmup.is_default` — the one place that knows how "default"
    is resolved (Settings → Default AI) — so this module never re-derives
    that answer on its own. Lazy import: `model_warmup` never needs to
    import this module at load time, but keeping the import local avoids
    tying either module's import order to the other's."""
    try:
        from src import model_warmup
        return model_warmup.is_default(root, model)
    except Exception:  # noqa: BLE001
        return False


def _default_yield_plan(root: str, residents: List[Dict[str, Any]],
                        shortfall: int) -> "tuple[List[str], int, bool]":
    """X-D, the owner's rule verbatim: the default model stays resident
    unless the person explicitly picked something else that does not fit
    next to it — in which case the default yields on its own, no card.

    Biggest-first fill across every resident that is NOT a user-pinned
    (Settings → Local models) model OTHER than the default itself — i.e.
    freely-evictable residents plus the default, never another person's
    pin. Returns `(names_to_unload, bytes_freed, uses_default)`; a caller
    only auto-yields when `uses_default` is True AND `bytes_freed >=
    shortfall` — the default moving for nothing (free residents alone
    would already have covered it) is not this rule, and neither is the
    default moving without covering the shortfall."""
    def _other_pin(r: Dict[str, Any]) -> bool:
        return is_pinned(root, r["name"]) and not is_default_model(root, r["name"])

    pool = sorted((r for r in residents if not _other_pin(r)),
                 key=lambda r: r["in_vram_bytes"], reverse=True)
    picked: List[str] = []
    freed = 0
    for r in pool:
        if freed >= shortfall:
            break
        picked.append(r["name"])
        freed += r["in_vram_bytes"]
    uses_default = any(is_default_model(root, n) for n in picked)
    return picked, freed, uses_default


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
            # X-D: the Studio card tags this row "default — kept loaded by
            # Faustus" instead of just another pinned model, so the person
            # understands why it is protected and can still tick it when
            # the automatic yield below was not enough on its own.
            "default": is_default_model(root, n),
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
    # A self-hosted OpenAI-compatible runner (llama.cpp's llama-server) is
    # physically on the same card nvidia-smi just read, so its bytes are
    # already inside `others` above -- this load will be refused/asked
    # exactly as it would for an Ollama resident, with no separate
    # accounting needed. What is missing without this is only the WORDING:
    # `others` alone reads as "something", not "the llama.cpp endpoint
    # holding 47 GB" -- named here so a refusal can say so instead of
    # leaving the person to guess, and never as a target to unload (there is
    # no unload call for it; see src/runner_providers.py).
    try:
        from src import runner_providers
        out["external_runners"] = runner_providers.external_runner_snapshot(same_machine_only=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("vram admission: external runner snapshot failed: %s", e)
        out["external_runners"] = []
    placements = gpu_placement.placement(root, loaded, vram.get("gpus"))
    block = vram_fit.pool_budgets(vram, held_by_runner_bytes=held, others_bytes=others,
                                  placements=placements)
    # `budget_bytes` treats what the runner holds as free. Alongside, it is not.
    budget_alongside = max(0, int(block["budget_bytes"]) - held)
    # Nor is what another job has reserved (HW-01): it has not loaded yet
    # either, so /api/ps says nothing about it, but the room is already
    # spoken for. Without this, two assess() calls a moment apart both see
    # the full budget and both say "fits" for memory that only exists once.
    reserved = reserved_bytes(root, exclude_model=model)
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
    if shortfall and out["external_runners"]:
        # Nothing here can free that memory -- llama-server has no unload
        # call -- so the dialog is told plainly instead of only suggesting
        # Ollama residents that, even all unloaded, would still leave the
        # shortfall this occupancy causes.
        names = ", ".join(
            f"{r.get('model') or '?'} on {r.get('endpoint_name') or 'a local runner'}"
            for r in out["external_runners"]
        )
        out["reason"] = (out.get("reason") or "") or f"blocked in part by {names}, which cannot be unloaded from here"
        out["external_occupancy_note"] = f"{names} cannot be unloaded from here"
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
    # INF-05 B2: "load" is a chat/research/Load-button ticket (`assess()`
    # against one Ollama model); "serve" is `admit_bytes`'s engine-agnostic
    # gate for Cookbook. Both resolve through the exact same route/click —
    # `kind` is metadata for the caller/UI, never branched on here.
    kind: str = "load"

    def public(self) -> Dict[str, Any]:
        return {"id": self.id, "model": self.model, "owner": self.owner,
                "created": self.created, "resolved": self.decision is not None,
                "decision": self.decision, "kind": self.kind, **self.assessment}


_PENDING: Dict[str, Ticket] = {}


def open_ticket(root: str, model: str, assessment: Dict[str, Any], *, owner: str = "",
                kind: str = "load") -> Ticket:
    t = Ticket(id=f"va-{uuid.uuid4().hex[:12]}", model=model, root=root, owner=owner or "",
               assessment=assessment, kind=kind)
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
                waited_out: Optional[Dict[str, Any]] = None,
                grant_out: Optional[Dict[str, Any]] = None) -> str:
    """Clear the way for `model` on `endpoint_url`, or refuse.

    Returns "proceed" when the caller may load. Raises AdmissionCancelled when
    it may not â€” the person said cancel, or nobody answered in time. Silence
    never loads a model that would not fit: that is the one rule that would
    have kept the machine up on 08-09.

    INF-05 B1/T12: when a reservation is actually made (the `fits=True`
    branch below), `grant_out["reservation_id"]` is filled in with its id —
    the caller then owns keeping it alive for as long as the load is really
    running: `mark_loading(reservation_id)` once it starts, and either
    `heartbeat_while_loading(reservation_id)` as a background task or its
    own precise `heartbeat()` calls once it knows exactly when the load
    ends (first streamed token, typically). Left untouched (not even set to
    `{}`) when no reservation was made — an absent key, not a `None` value,
    is the caller's signal there is nothing to keep alive.

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
            if grant_out is not None:
                grant_out["reservation_id"] = reservation_id
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

    # X-D correction (the owner's rule, verbatim): the default model stays
    # resident whenever Faustus is open and no other model was explicitly
    # asked for; when the person picks a model that does not fit next to it,
    # the default yields ON ITS OWN — no ticket, no card, in EVERY mode
    # including "ask" — and comes back once the other one is gone (the
    # residency keeper in src/model_warmup.py). Only when even unloading the
    # default (together with whatever else is freely evictable) still would
    # not free enough room does this fall through to the normal ticket/auto
    # path below, where the default appears in the card tagged `"default"`.
    if not is_default_model(root, model):
        plan_names, plan_freed, uses_default = _default_yield_plan(
            root, a["residents"], a["shortfall_bytes"])
        if uses_default and plan_freed >= a["shortfall_bytes"]:
            default_row = next((r for r in a["residents"] if r.get("default")), None)
            default_name = default_row["name"] if default_row else "the default model"
            say({"phase": "yielding", "message": f"{default_name} steps aside for {model}"})
            left = await unload_and_wait(root, plan_names, on_progress=on_progress)
            if not left:
                # Same contract the person-driven "unload" answer in "ask"
                # mode below has: once the chosen models are confirmed gone
                # from `/api/ps`, the caller proceeds — no second fit check
                # against a card reading that a mock (or a slower card under
                # real load) cannot be expected to reflect the unload on yet.
                _mark_wait()
                return "proceed"
            say({"phase": "error", "message": f"Could not unload {', '.join(left)}; the load was cancelled."})
            _mark_wait()
            raise AdmissionCancelled(f"{', '.join(left)} stayed resident after the unload; "
                                     f"{model} was not loaded.")

    if mode == "auto":
        # "auto" acts with nobody watching, so it may only take what `assess()`
        # offered as freely evictable — never a pinned resident (the default
        # model, or anything a person pinned from Settings → Local models),
        # even the ones `suggestion` names as a last resort when nothing else
        # would free the shortfall. Those only ever go through the person
        # explicitly naming them in "ask" mode below.
        protected = set(a.get("suggestion_protected_used") or [])
        names = [n for n in (a.get("suggestion") or []) if n not in protected]
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
        # Wait for the answer in short slices and look at the server between
        # them: when the model turns up resident meanwhile (the startup
        # warm-up loaded it, or another chat did), there is nothing left to
        # ask — seen live: a turn from an API caller sat ten minutes on a
        # card nobody could see while the model had been loaded seconds later,
        # and every later turn of that owner queued behind it.
        deadline = time.time() + float(timeout or _timeout())
        answered = False
        while True:
            slice_s = max(0.0, min(5.0, deadline - time.time()))
            try:
                await asyncio.wait_for(t.event.wait(), timeout=slice_s)
                answered = True
                break
            except asyncio.TimeoutError:
                pass
            try:
                again = await asyncio.to_thread(assess, root, model)
            except Exception:  # noqa: BLE001
                again = {}
            if again.get("already_resident") or again.get("fits") is True:
                _forget(t.id)
                say({"phase": "resident", "message": f"{model} is loaded now; carrying on."})
                _mark_wait()
                return "proceed"
            if time.time() >= deadline:
                break
        if not answered:
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


# ── admit_bytes: the engine-agnostic gate (INF-05 B2) ───────────────────────
#
# `admit()`/`assess()` above answer "does THIS OLLAMA MODEL fit" — they need
# a model name to ask Ollama's own /api/ps and /api/tags. Cookbook `serve`
# has neither: it launches an arbitrary engine (llama-server, vllm, ...)
# against a weights-bytes figure the client already knows (`size_bytes` off
# the catalogue) and a set of GPU indices, not an Ollama root. §12 is
# explicit that this does not earn an exemption — "un benchmark no recibe
# permiso para saltarse la puerta porque sea interno", and neither does a
# serve. `admit_bytes` is the SAME authority (same reservation table, same
# ticket table) reached through a byte count and a physical GPU set instead
# of a model name.

def _physical_budget_for(gpu_indices: Sequence[int]) -> Dict[str, Any]:
    """Real per-GPU capacity for `admit_bytes`, built from INF-05 Lote A's
    own authorities — `gpu_topology.snapshot()` (physical identity) and
    `src.memory_budget.physical_budgets()` (the desegregated per-GPU
    reading) — rather than a second implementation of the same budget
    arithmetic. This module stays the only CALLER that decides admission;
    A3 stays the only place that turns raw readings into a `MemoryBudget`.

    Returns `{budget_bytes, total_bytes, used_bytes, stale, source,
    gpu_count, gpu_name, reason}`, aggregated over `gpu_indices` (every
    physical GPU this reading covers when `gpu_indices` is empty — the
    pool). `source == "absent"` (with `stale=True` and a `reason`) for
    anything this cannot honestly answer: no `nvidia-smi`, no reading for
    the requested indices, or a budget module that failed to import —
    never a fabricated number standing in for "unknown".
    """
    def _absent(reason: str) -> Dict[str, Any]:
        return {"budget_bytes": 0, "total_bytes": None, "used_bytes": None,
               "stale": True, "source": "absent", "gpu_count": 0, "gpu_name": "",
               "reason": reason}

    try:
        from src import gpu_topology
        from src import gpu_shared_memory
        from src import memory_budget as mb
    except Exception as e:  # noqa: BLE001
        return _absent(f"budget modules unavailable: {e}")
    try:
        snap = gpu_topology.snapshot()
    except Exception as e:  # noqa: BLE001
        return _absent(f"topology reading failed: {e}")
    try:
        vram = gpu_shared_memory.vram_snapshot()
    except Exception as e:  # noqa: BLE001
        return _absent(f"vram reading failed: {e}")
    if not vram or not vram.get("supported"):
        return _absent(str((vram or {}).get("reason") or "no GPU reading"))
    try:
        budgets = mb.physical_budgets(snapshot=snap, vram=vram)
    except Exception as e:  # noqa: BLE001
        return _absent(f"budget computation failed: {e}")
    if not budgets:
        return _absent("no GPU budget rows")
    wanted = {int(i) for i in (gpu_indices or ())}
    rows = [b for b in budgets.values() if not wanted or b.gpu_index in wanted]
    if not rows:
        return _absent(f"GPU indices {sorted(wanted)} were not observed in this reading")
    total_known = all(r.total_bytes is not None for r in rows)
    free_known = all(r.components.free.bytes is not None for r in rows)
    if not total_known or not free_known:
        return _absent("VRAM total/free was not observed for one or more of the requested GPUs")
    total = sum(int(r.total_bytes) for r in rows)
    free = sum(int(r.components.free.bytes) for r in rows)
    used = max(0, total - free)
    stale = any(r.stale for r in rows)
    return {"budget_bytes": max(0, free), "total_bytes": total, "used_bytes": used,
           "stale": stale, "source": "observed", "gpu_count": len(rows),
           "gpu_name": rows[0].gpu_name, "reason": ""}


def _physical_reservation_root(gpu_indices: Sequence[int]) -> str:
    """A dedicated reservation namespace for `admit_bytes`, separate from
    any Ollama root's `root|pool`/`root|gpuN` keys — a serve candidate and
    an Ollama load are different consumers of the same physical card, and
    each has its own bytes to reserve, so they must not collide on the same
    key by accident. Same GPU SET -> same key, regardless of order."""
    idx = sorted({int(i) for i in (gpu_indices or ())})
    return "physical:" + (",".join(str(i) for i in idx) if idx else "pool")


def _ollama_suggestion_candidates(
    ollama_roots: Sequence[str], gpu_indices: Sequence[int], shortfall: int,
) -> "tuple[List[str], int, List[Dict[str, Any]]]":
    """§12: the ONLY eviction candidates `admit_bytes` ever offers are
    Ollama models resident on the requested physical GPUs — never a foreign
    process (a browser, ComfyUI, someone else's job): those stay folded
    into the budget's own "used" figure, reported, never acted on. Returns
    `(suggestion_names_biggest_first, bytes_freed, residents)`; `residents`
    carries each candidate's `root` so a later `resolve(..., action=
    "unload")` knows which Ollama server to actually call."""
    wanted = {int(i) for i in (gpu_indices or ())}
    residents: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []
    if not ollama_roots:
        return [], 0, []
    from src import gpu_placement, gpu_shared_memory
    for root in ollama_roots:
        try:
            loaded = _get(root, "/api/ps", 2.5).get("models") or []
        except Exception as e:  # noqa: BLE001
            logger.debug("admit_bytes: /api/ps failed for %s: %s", root, e)
            continue
        try:
            vram = gpu_shared_memory.vram_snapshot()
            gpus = vram.get("gpus") if vram.get("supported") else None
        except Exception:  # noqa: BLE001
            gpus = None
        try:
            placements = gpu_placement.placement(root, loaded, gpus)
        except Exception:  # noqa: BLE001
            placements = {}
        for m in loaded:
            name = str(m.get("name") or m.get("model") or "")
            if not name:
                continue
            in_vram = int(m.get("size_vram") or 0)
            info = placements.get(name) or {}
            idxs = {int(i) for i in (info.get("gpus") or [])}
            if wanted and not (idxs & wanted):
                continue  # resident on a different card than the one requested
            is_default = is_default_model(root, name)
            row = {"name": name, "root": root, "in_vram_bytes": in_vram, "gpus": sorted(idxs),
                  "default": is_default}
            # A serve launch never has a person naming which model to unload
            # the way the Local models screen's own Unload button does — this
            # path either acts on its own (`auto`) or offers a ticket whose
            # `names` must come from `residents`. A model a PERSON pinned
            # from the settings screen stays out of both entirely (nothing
            # here counts as the explicit human action that is the only
            # thing allowed to evict it). The default is different (X-D): it
            # always stays VISIBLE in `residents` — a serve request that
            # only has the default resident must not present as "0 models
            # loaded" — and it is offered as a `candidate` too, since the
            # owner's rule is that the default yields on its own once it is
            # what stands between the picked model and fitting.
            if is_pinned(root, name) and not is_default:
                residents.append(row)
                continue
            residents.append(row)
            candidates.append(row)
    # Biggest first: one large model freed beats several small ones (same
    # arithmetic reasoning as `assess()`'s own suggestion builder).
    candidates.sort(key=lambda c: c["in_vram_bytes"], reverse=True)
    picked: List[str] = []
    freed = 0
    for c in candidates:
        if freed >= shortfall:
            break
        picked.append(c["name"])
        freed += c["in_vram_bytes"]
    return picked, freed, residents


async def admit_bytes(*, label: str, bytes_needed: Optional[int], gpu_indices: Sequence[int],
                      owner: str = "", mode: Optional[str] = None, timeout: Optional[float] = None,
                      ollama_roots: Sequence[str] = ()) -> Dict[str, Any]:
    """The capacity gate for a Cookbook `serve` launch (§12: a serve gets no
    exemption from the single capacity authority any more than a benchmark
    does). Returns `{decision, reservation_id, ticket, assessment}`:

      decision == "proceed"  the caller may launch; `reservation_id` (when
                             set) must be kept alive the same way `admit()`
                             callers do: `mark_loading()` + either
                             `heartbeat_while_loading()` or precise
                             `heartbeat()` calls until the launch is
                             confirmed resident.
      decision == "blocked"  does not fit; `ticket` names a pending
                             `Ticket` the caller's route returns as a 409
                             for the Studio's `VramAdmissionDialog` to
                             resolve (`POST /api/local-models/admission/
                             {ticket}`) — mirrors `admit()`'s `mode="ask"`
                             path, but NEVER awaits a person here (`ask` and
                             any shortfall this cannot auto-clear both take
                             this branch): a serve request is one HTTP call,
                             not a held-open stream a ticket can block
                             inside of the way `admit()` does for chat.
      decision == "unknown"  no reliable reading to judge against — either
                             `bytes_needed is None` (nothing was given to
                             check) or the physical budget reading is
                             stale/absent (`assessment.reason` says which)
                             — never treated as a block; ignorance does not
                             stop a launch, it is reported on the receipt
                             instead (§12: "lo que no se observa es absent/
                             unknown ... pero la ignorancia nunca bloquea").
      decision == "off"      admission is disabled; nothing was checked.

    Deadlock note (T16, §12 "evitar deadlocks de delegación"): this
    coroutine never imports or touches `routes.cookbook_routes.
    _LOCAL_MODEL_LOCK` (or any other cross-request lock) and its only
    `await`s are `asyncio.to_thread` for the budget/placement reads and,
    in `mode="auto"`, `unload_and_wait` for Ollama residents it is itself
    unloading — nothing here can hold a lock a child run would need to
    finish and then wait on that same child. The parent/child *scheduling*
    question (should a parent yield capacity to a blocked child at all) is
    the subagent orchestrator's decision, out of this lote's scope.
    """
    mode = mode or _mode()
    root_for_ticket = (ollama_roots[0] if ollama_roots else None) or "physical"
    gpu_indices = list(gpu_indices or ())

    if bytes_needed is None:
        # §12: ignorance never blocks, but it is reported, not silently
        # treated as a checked "proceed" — the caller (`model_serve`)
        # continues the launch either way, and records `decision: "unknown"`
        # on the receipt rather than pretending a check happened.
        return {"decision": "unknown", "reservation_id": None, "ticket": None,
               "assessment": {"model": label, "root": root_for_ticket, "fits": None,
                              "reason": "weights size unknown: nothing to check against",
                              "kind": "serve", "gpu_indices": gpu_indices}}
    if mode == "off":
        return {"decision": "off", "reservation_id": None, "ticket": None,
               "assessment": {"model": label, "root": root_for_ticket, "fits": None,
                              "reason": "vram admission is off", "kind": "serve",
                              "gpu_indices": gpu_indices}}

    budget = await asyncio.to_thread(_physical_budget_for, gpu_indices)
    if budget["source"] == "absent" or budget["stale"]:
        return {"decision": "unknown", "reservation_id": None, "ticket": None,
               "assessment": {"model": label, "root": root_for_ticket, "fits": None,
                              "reason": budget.get("reason") or "no reliable GPU reading",
                              "stale": bool(budget["stale"]), "kind": "serve",
                              "gpu_indices": gpu_indices}}

    phys_key = _physical_reservation_root(gpu_indices)
    reserved = reserved_bytes(phys_key)
    total = int(budget["total_bytes"])
    used = int(budget["used_bytes"])
    budget_alongside = max(0, total - used - reserved)
    headroom = HEADROOM_MEASURED
    need = int(bytes_needed) + headroom
    shortfall = max(0, need - budget_alongside)

    suggestion, suggestion_frees, residents = await asyncio.to_thread(
        _ollama_suggestion_candidates, tuple(ollama_roots), gpu_indices, shortfall)

    assessment: Dict[str, Any] = {
        "model": label, "root": root_for_ticket, "fits": shortfall == 0,
        "residents": residents, "suggestion": suggestion, "measured": True,
        "estimate": "measured", "size_bytes": int(bytes_needed), "kv_bytes": 0, "kv_ctx": 0,
        "footprint_bytes": int(bytes_needed), "headroom_bytes": headroom, "need_bytes": need,
        "budget_alongside_bytes": budget_alongside, "budget_if_unloaded_bytes": max(0, total - used),
        "reserved_bytes": reserved, "held_by_runner_bytes": 0, "others_bytes": used,
        "vram_total_bytes": total, "gpu_count": len(gpu_indices) or int(budget.get("gpu_count") or 1),
        "gpu_name": budget.get("gpu_name") or "", "shortfall_bytes": shortfall,
        "suggestion_frees_bytes": suggestion_frees, "suggestion_enough": suggestion_frees >= shortfall,
        "suggestion_protected_used": [], "kind": "serve", "gpu_indices": gpu_indices,
    }

    if shortfall == 0:
        reservation_id = try_reserve(phys_key, label, need, budget_alongside + reserved)
        if reservation_id is not None:
            return {"decision": "proceed", "reservation_id": reservation_id, "ticket": None,
                   "assessment": assessment}
        # QA-24/T13: fit a moment ago, another admit_bytes just reserved the
        # room — the same race admit() guards against, handled the same way.
        assessment = dict(assessment, fits=False,
                          reason=f"{label} would fit, but another admission just reserved that room")

    if mode == "auto" and suggestion and suggestion_frees >= shortfall:
        # Only ever unloads what `_ollama_suggestion_candidates` already
        # restricted to Ollama residents on these exact GPUs — never a
        # foreign process, never a model this reading could not attribute.
        by_root: Dict[str, List[str]] = {}
        for r in residents:
            if r["name"] in suggestion:
                by_root.setdefault(r["root"], []).append(r["name"])
        still_resident: List[str] = []
        for root, names in by_root.items():
            still_resident.extend(await unload_and_wait(root, names))
        if not still_resident:
            reservation_id = try_reserve(phys_key, label, need, need)
            return {"decision": "proceed", "reservation_id": reservation_id, "ticket": None,
                   "assessment": assessment}
        assessment = dict(assessment, reason=f"could not unload {', '.join(still_resident)}")

    t = open_ticket(root_for_ticket, label, assessment, owner=owner, kind="serve")
    return {"decision": "blocked", "reservation_id": None, "ticket": t.id, "assessment": assessment}


def reconcile_on_start() -> Dict[str, Any]:
    """INF-05 B1/T12: called once from `app.py`'s startup reconciliation
    pass, next to `src.bench.runner.reconcile_on_start` and
    `src.launch_receipts.reconcile_on_start`. Reservations
    (`_RESERVATIONS`), pending tickets (`_PENDING`) and pins/last-active are
    all in-memory only — a restart already loses them, so this is
    bookkeeping, not recovery: it clears them explicitly and logs what it
    found rather than leaving stale entries from a process that no longer
    exists. It does NOT re-derive a budget from nvidia-smi itself — any
    process that survived the restart (an already-running Ollama, an
    externally-managed server) shows up as `used` VRAM the moment the next
    `assess()`/`admit_bytes()` call reads the card fresh, so no ghost
    capacity is silently handed back either way."""
    with _RES_LOCK:
        cleared = len(_RESERVATIONS)
        _RESERVATIONS.clear()
    pending_cleared = len(_PENDING)
    _PENDING.clear()
    if cleared:
        logger.info("vram admission: reconcile_on_start cleared %d in-memory reservation(s)", cleared)
    if pending_cleared:
        logger.info("vram admission: reconcile_on_start cleared %d pending ticket(s)", pending_cleared)
    return {"cleared": cleared, "pending_cleared": pending_cleared}
