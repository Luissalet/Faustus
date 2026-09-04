"""
media_backends/pool.py — more than one engine, and a reason for each choice.

This machine has two GPUs. ComfyUI takes exactly one (`--cuda-device N`), so
using both means running two of them — and the moment there are two, something
has to decide which gets a job, and be able to say why.

The rule is **least busy first, then the smallest card that fits**. A 512px
draft does not need 12 GB, and putting it on the small card leaves the big one
free for the render that does. The opposite order looks fairer and is worse:
the big job ends up queued behind three small ones on the only card that could
have taken it.

This is not the LLM side's rule, though both end up keeping the big card
clear. `gpu_placement_prefer` (src/gpu_policy.py) is a number a person sets —
"fill card N first", default -1 meaning Ollama decides — because a language
model is resident for hours and wants to live wholly on one card; pinning one
that does not fit is worse than letting Ollama split it. A render is
transient: seconds, then the card is back. So nobody configures this one, and
nothing here reads that setting.

Three refusals worth naming:

**An engine that does not answer is not a candidate.** Probed, not assumed —
same rule as every other backend here.

**An engine without the model is not a candidate either.** Two ComfyUIs on one
machine usually share a models folder, but they do not have to, and "it failed
on engine B" twenty minutes in is the outcome this avoids.

**A choice always carries its reason.** `why` says which engines were looked
at and what disqualified each, because "no engine available" on a machine with
two of them is the least useful sentence in the system.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .comfyui import DEFAULT_BASE_URL, ComfyUIBackend, ComfyUIError

logger = logging.getLogger(__name__)

#: `COMFYUI_URLS` is the pool; `COMFYUI_URL` remains the single-engine name so
#: nothing that already set it has to change.
POOL_ENV = "COMFYUI_URLS"
SINGLE_ENV = "COMFYUI_URL"


@dataclass(frozen=True)
class Engine:
    """One ComfyUI, and what was actually observed about it."""

    url: str
    ok: bool = False
    reason: str = ""
    detail: str = ""
    gpu: str = ""
    vram_gb: Optional[float] = None
    checkpoints: Sequence[str] = ()
    queued: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"url": self.url, "ok": self.ok, "reason": self.reason,
                "detail": self.detail, "gpu": self.gpu, "vram_gb": self.vram_gb,
                "checkpoints": list(self.checkpoints), "queued": self.queued}


def urls() -> List[str]:
    """Every engine this Faustus knows about, in the order configured.

    `COMFYUI_URLS` is comma-separated. Falls back to `COMFYUI_URL`, then to the
    default — so a machine with one engine needs no configuration at all and a
    machine with two needs one variable."""
    raw = (os.getenv(POOL_ENV) or "").strip()
    if raw:
        found = [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]
        if found:
            return found
    return [(os.getenv(SINGLE_ENV) or DEFAULT_BASE_URL).rstrip("/")]


def survey(*, base_urls: Optional[Sequence[str]] = None) -> List[Engine]:
    """Ask every engine what it is and what it has.

    An engine that raises is reported, not skipped: on a two-GPU machine the
    interesting fact is usually "one of them is down", and a list that quietly
    shrank hides exactly that."""
    found: List[Engine] = []
    for url in (base_urls if base_urls is not None else urls()):
        backend = ComfyUIBackend(url)
        try:
            gate = backend.probe()
        except Exception as e:                      # a probe never takes the page down
            found.append(Engine(url, False, "probe_failed",
                                f"{type(e).__name__}: {e}"))
            continue
        if not gate["ok"]:
            found.append(Engine(url, False, gate["reason"], gate["detail"]))
            continue

        gpu, vram = "", None
        try:
            stats = backend._call("/system_stats")
            devices = stats.get("devices") or []
            if devices:
                gpu = str(devices[0].get("name") or "")
                total = devices[0].get("vram_total")
                if isinstance(total, (int, float)) and total:
                    vram = round(total / (1024 ** 3), 1)
        except ComfyUIError:
            pass

        queued = None
        try:
            queue = backend._call("/queue")
            queued = len(queue.get("queue_running") or []) + \
                len(queue.get("queue_pending") or [])
        except ComfyUIError:
            pass

        found.append(Engine(url, True, "ready", gate["detail"], gpu, vram,
                            tuple(backend.checkpoints()), queued))
    return found


def choose(plan: Mapping[str, Any], *,
           requires_nodes: Optional[Sequence[str]] = None,
           engines: Optional[Sequence[Engine]] = None,
           prefer: str = "") -> Dict[str, Any]:
    """Which engine should run this, and why not the others.

    Returns `{ok, url, engine, why: [...]}`. `why` lists every engine that was
    looked at with what disqualified it — "no engine available" on a machine
    with two of them is the least useful sentence in the system."""
    looked = list(engines if engines is not None else survey())
    wanted_models = {str(m.get("name")) for m in (plan.get("models") or ())
                     if str(m.get("kind") or "checkpoint") == "checkpoint"}

    why: List[Dict[str, Any]] = []
    eligible: List[Engine] = []
    for engine in looked:
        if not engine.ok:
            why.append({"url": engine.url, "reason": engine.reason,
                        "detail": engine.detail})
            continue
        missing = sorted(m for m in wanted_models if m not in set(engine.checkpoints))
        if missing:
            why.append({"url": engine.url, "reason": "missing_models",
                        "detail": f"does not have {', '.join(missing)}",
                        "missing_models": missing})
            continue
        why.append({"url": engine.url, "reason": "eligible",
                    "detail": f"{engine.gpu or 'unknown gpu'}"
                              + (f", {engine.vram_gb} GB" if engine.vram_gb else "")
                              + (f", {engine.queued} queued" if engine.queued is not None else "")})
        eligible.append(engine)

    if not eligible:
        return {"ok": False, "reason": "no_engine", "why": why,
                "detail": _no_engine_detail(why)}

    if prefer:
        wanted = prefer.rstrip("/")
        picked = next((e for e in eligible if e.url == wanted), None)
        if picked is not None:
            return {"ok": True, "url": picked.url, "engine": picked.to_dict(),
                    "why": why, "chosen_because": "asked for by name"}
        why.append({"url": wanted, "reason": "prefer_not_eligible",
                    "detail": "asked for by name, but it is not one of the "
                              "engines that could take this job"})

    picked = _smallest_free(eligible)
    return {"ok": True, "url": picked.url, "engine": picked.to_dict(), "why": why,
            "chosen_because": _reason_for(picked, eligible)}


def _smallest_free(eligible: Sequence[Engine]) -> Engine:
    """Least busy first, then SMALLEST card.

    Filling the small card first is not politeness, it is throughput: a 512px
    draft does not need 12 GB, and leaving the big card free means the job that
    does need it is not queued behind three that did not."""
    return sorted(eligible, key=lambda e: (
        e.queued if e.queued is not None else 0,
        e.vram_gb if e.vram_gb is not None else 9999,
        e.url,
    ))[0]


def _reason_for(picked: Engine, eligible: Sequence[Engine]) -> str:
    if len(eligible) == 1:
        return "the only engine that could take it"
    busy = [e for e in eligible if (e.queued or 0) > (picked.queued or 0)]
    if busy:
        return (f"least busy ({picked.queued} queued) of {len(eligible)} engines")
    smaller = [e for e in eligible
               if (e.vram_gb or 9999) > (picked.vram_gb or 9999)]
    if smaller:
        return (f"smallest card that fits the job ({picked.vram_gb} GB), leaving "
                f"the bigger one free for something that needs it")
    return f"first of {len(eligible)} equal engines"


def _no_engine_detail(why: Sequence[Mapping[str, Any]]) -> str:
    if not why:
        return "no engines are configured at all; set COMFYUI_URL or COMFYUI_URLS"
    parts = [f"{w['url']}: {w.get('detail') or w.get('reason')}" for w in why]
    return "no engine can take this job. " + " · ".join(parts)
