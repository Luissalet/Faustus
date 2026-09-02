# routes/local_models_routes.py
"""Local model manager for Ollama endpoints (Settings → Local models).

What LM Studio calls My Models / Discover / loaded models, for the Ollama
servers this install is configured against:

  GET    /api/local-models?endpoint_id=     installed (/api/tags + cached
                                            /api/show), loaded (/api/ps, with
                                            the card(s) each one sits on), the
                                            card(s) — `vram` is the pool plus
                                            one entry per card, `gpus` the
                                            list the Options form pins to —
                                            and a fit verdict per model
  POST   /api/local-models/pull             {endpoint_id, name} → SSE progress
                                            (?stream=false → {id} only)
  GET    /api/local-models/pulls            active + recent pulls (re-attach)
  GET    /api/local-models/pulls/{id}/events  SSE for one pull (EventSource)
  DELETE /api/local-models/pulls/{id}       cancel
  POST   /api/local-models/load|unload      {endpoint_id, name}
  GET    /api/local-models/discover?q=      curated offline catalogue + fit
  GET/PUT /api/local-models/{name}/options  per-model num_ctx/num_gpu/keep_alive/main_gpu
  DELETE /api/local-models/{name}           ollama /api/delete

Reads are for any signed-in user, mutations admin-only. A pull runs in a
background thread that outlives the browser tab: the SSE stream is a view on
the job, not the job itself, so closing the tab and reopening Settings
re-attaches through /pulls.

The fit arithmetic is routes/model_routes.py's (`_fit_state`, the reserve
and the tight band) so the picker and this page never disagree about a
model. /api/show is slow (it reads the GGUF header), so its answer is cached
per blob digest — the digest changes exactly when the answer would.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from core.database import SessionLocal, ModelEndpoint
from core.log_safety import redact_url as _redact_url_for_log
from core.middleware import require_admin
from src import gpu_placement, gpu_shared_memory, vram_fit
from src import local_model_catalog as catalog
from src import model_load_options as mlo
from src.auth_helpers import effective_user, owner_filter, require_user
from src.endpoint_resolver import normalize_base as _normalize_base
from src.llm_core import _host_match
from src.tls_overrides import llm_verify
from routes.model_routes import (
    _FIT_RESERVE_BYTES,
    _LOCAL_HOSTS,
    _fit_note,
    _fit_state,
    _is_ollama_base,
)

logger = logging.getLogger(__name__)

MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._/:-]+$")
MODEL_NAME_MAX = 200

_TAGS_TIMEOUT = 4.0
_PS_TIMEOUT = 3.0
_SHOW_TIMEOUT = 10.0
_MUTATE_TIMEOUT = 30.0
_LOAD_TIMEOUT = 600.0

# Finished pulls stay listed this long so a reopened Settings page can still
# show "done" / the error, then they are pruned.
_PULL_KEEP_SECONDS = 3600.0
_PULL_KEEP_MAX = 50


def _client_factory(timeout: float = 10.0) -> httpx.Client:
    """One place to build the client so tests can swap in a MockTransport."""
    return httpx.Client(timeout=timeout, verify=llm_verify())


# ── model names ─────────────────────────────────────────────────────────────

def validate_model_name(name: Any) -> str:
    """The name as Ollama accepts it, or HTTPException(400)."""
    text = str(name or "").strip()
    if not text:
        raise HTTPException(400, "Model name is required")
    if len(text) > MODEL_NAME_MAX:
        raise HTTPException(400, f"Model name is too long (max {MODEL_NAME_MAX} characters)")
    if not MODEL_NAME_RE.match(text):
        raise HTTPException(400, "Model name may only contain letters, digits, '.', '_', '-', '/' and ':'")
    if text.startswith(("/", ":", ".", "-")) or "//" in text or "/." in text:
        raise HTTPException(400, "Model name must look like `family:tag` or `hf.co/user/repo:tag`")
    return text


def _same_model(a: str, b: str) -> bool:
    def _canon(name: str) -> str:
        name = str(name or "")
        return name if ":" in name.split("/")[-1] else f"{name}:latest"
    return _canon(a) == _canon(b)


# ── endpoints ───────────────────────────────────────────────────────────────

def ollama_root(base_url: str) -> str:
    """`http://127.0.0.1:11434/v1` → `http://127.0.0.1:11434` (the /api root)."""
    base = _normalize_base(base_url or "").rstrip("/")
    for suffix in ("/v1", "/api"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break
    return base


def _is_same_machine(root: str) -> bool:
    try:
        host = (urlparse(root).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOCAL_HOSTS


def _default_ollama_root() -> str:
    return ollama_root(mlo._default_ollama_base())


def list_ollama_endpoints(include_default: bool = True, *, owner: str = "",
                          is_admin: bool = True) -> List[Dict[str, Any]]:
    """Configured endpoints that are Ollama servers (by URL: port 11434 or
    an "ollama" host), Ollama Cloud excluded — it has no card to fit on and
    no /api/pull. Falls back to the env-configured local Ollama when nothing
    is configured, so a fresh install still gets a page.

    Same visibility rule as /api/models: admins see every endpoint, a regular
    user sees the shared (owner-less) ones plus their own."""
    out: List[Dict[str, Any]] = []
    seen_roots: set = set()
    try:
        db = SessionLocal()
        try:
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
            if owner and not is_admin:
                q = owner_filter(q, ModelEndpoint, owner)
            rows = q.all()
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.debug("local models: endpoint query failed: %s", e)
        rows = []
    for ep in rows:
        base = str(getattr(ep, "base_url", "") or "")
        if not base or not _is_ollama_base(base) or _host_match(base, "ollama.com"):
            continue
        root = ollama_root(base)
        if not root or root in seen_roots:
            continue
        seen_roots.add(root)
        out.append({
            "id": str(getattr(ep, "id", "") or ""),
            "name": str(getattr(ep, "name", "") or "Ollama"),
            "base_url": base,
            "root": root,
            "same_machine": _is_same_machine(root),
        })
    if not out and include_default:
        root = _default_ollama_root()
        out.append({
            "id": mlo.DEFAULT_ENDPOINT_ID,
            "name": "Ollama (this machine)",
            "base_url": root + "/v1",
            "root": root,
            "same_machine": _is_same_machine(root),
        })
    return out


def _pick_endpoint(endpoint_id: Optional[str], endpoints: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The endpoint `endpoint_id` names in `endpoints` (the request's own
    visible list — every route passes it), or the first one."""
    if not endpoints:
        raise HTTPException(404, "No Ollama endpoint is configured")
    if endpoint_id:
        for ep in endpoints:
            if ep["id"] == endpoint_id:
                return ep
        raise HTTPException(404, f"Unknown Ollama endpoint: {endpoint_id}")
    return endpoints[0]


# ── Ollama reads ────────────────────────────────────────────────────────────

def _get_json(root: str, path: str, timeout: float) -> Any:
    with _client_factory(timeout) as client:
        r = client.get(root + path)
        r.raise_for_status()
        return r.json()


def _post_json(root: str, path: str, body: Dict[str, Any], timeout: float) -> httpx.Response:
    with _client_factory(timeout) as client:
        return client.post(root + path, json=body)


def _tags(root: str) -> List[Dict[str, Any]]:
    data = _get_json(root, "/api/tags", _TAGS_TIMEOUT)
    return [m for m in ((data or {}).get("models") or []) if isinstance(m, dict)]


def _ps(root: str) -> List[Dict[str, Any]]:
    data = _get_json(root, "/api/ps", _PS_TIMEOUT)
    return [m for m in ((data or {}).get("models") or []) if isinstance(m, dict)]


_show_cache: Dict[str, Dict[str, Any]] = {}
_show_cache_lock = threading.Lock()
_SHOW_CACHE_MAX = 400


def _summarize_show(data: Dict[str, Any]) -> Dict[str, Any]:
    """The few facts the page needs out of the (large) /api/show answer."""
    caps = {str(c) for c in (data.get("capabilities") or [])}
    info = data.get("model_info") or {}
    details = data.get("details") or {}
    arch = str(info.get("general.architecture") or "")
    ctx = 0
    if arch:
        try:
            ctx = int(info.get(f"{arch}.context_length") or 0)
        except (TypeError, ValueError):
            ctx = 0
    embedding_len = 0
    if arch:
        try:
            embedding_len = int(info.get(f"{arch}.embedding_length") or 0)
        except (TypeError, ValueError):
            embedding_len = 0
    license_text = str(data.get("license") or "").strip()
    license_line = ""
    for line in license_text.splitlines():
        line = line.strip().strip("#").strip()
        if line:
            license_line = line[:80]
            break
    return {
        "capabilities": {
            "vision": "vision" in caps,
            "tools": "tools" in caps,
            "thinking": "thinking" in caps,
            "embedding": "embedding" in caps,
            "completion": "completion" in caps or not caps,
        },
        "capability_list": sorted(caps),
        "architecture": arch,
        "context_length": ctx,
        "embedding_length": embedding_len,
        "license": license_line,
        "family": str(details.get("family") or ""),
        "families": [str(f) for f in (details.get("families") or [])],
        "parameter_size": str(details.get("parameter_size") or ""),
        "quantization": str(details.get("quantization_level") or ""),
    }


def _show(root: str, name: str, digest: str) -> Dict[str, Any]:
    key = f"{root}\x00{digest or name}"
    with _show_cache_lock:
        hit = _show_cache.get(key)
    if hit is not None:
        return hit
    try:
        r = _post_json(root, "/api/show", {"model": name, "name": name}, _SHOW_TIMEOUT)
        r.raise_for_status()
        summary = _summarize_show(r.json() or {})
    except Exception as e:  # noqa: BLE001 — the row still renders without it
        logger.debug("local models: /api/show failed for %s: %s", name, e)
        return {}
    with _show_cache_lock:
        if len(_show_cache) >= _SHOW_CACHE_MAX:
            _show_cache.clear()
        _show_cache[key] = summary
    return summary


def reset_show_cache() -> None:
    with _show_cache_lock:
        _show_cache.clear()


def _disk(root: str, same_machine: bool) -> Dict[str, Any]:
    """Free space where Ollama stores blobs — only meaningful on this box."""
    if not same_machine:
        return {}
    path = (os.getenv("OLLAMA_MODELS") or "").strip() or os.path.join(os.path.expanduser("~"), ".ollama", "models")
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            probe = ""
            break
        probe = parent
    if not probe:
        return {}
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return {}
    return {"path": path, "free_bytes": int(usage.free), "total_bytes": int(usage.total)}


def _vram_block(same_machine: bool, held_by_runner: int,
                placements: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The card(s) as the fit arithmetic sees them. Mirrors model_routes'
    _collect_fit_hints: what the runner holds is about to be freed by a
    switch, everything else on the card is not. With two cards the budget is
    the pool's (Ollama splits what fits no single card) and each card gets
    its own budget too, so `fit_verdict` can say "split"."""
    if not same_machine:
        return {"supported": False, "reason": "Models on this endpoint run on another machine."}
    vram = gpu_shared_memory.vram_snapshot()
    if not vram.get("supported"):
        return {"supported": False, "reason": vram.get("reason", "")}
    used = int(vram.get("used") or 0)
    others = max(0, used - held_by_runner)
    return vram_fit.pool_budgets(
        vram, held_by_runner_bytes=held_by_runner, others_bytes=others,
        placements=placements, reserve_per_card=_FIT_RESERVE_BYTES,
    )


def _gpu_list(same_machine: bool) -> List[Dict[str, Any]]:
    """`[{index, name, total_bytes}]` — the cards the Options form can pin a
    model to (`main_gpu`). Empty off this machine: we cannot see its cards."""
    if not same_machine:
        return []
    vram = gpu_shared_memory.vram_snapshot()
    if not vram.get("supported"):
        return []
    cards = [g for g in (vram.get("gpus") or []) if isinstance(g, dict)]
    if not cards:
        cards = [{"index": 0, "name": vram.get("name"), "total": vram.get("total")}]
    return [{"index": int(g.get("index") or 0), "name": str(g.get("name") or ""),
             "total_bytes": int(g.get("total") or 0)} for g in cards]


def fit_verdict(size_bytes: int, vram: Dict[str, Any], *, clean: bool = False) -> Dict[str, Any]:
    """`{state, headroom_bytes, split, note}` against the pool budget (the
    clean one for Discover); `split` when the weights exceed every single
    card but not the pool — Ollama loads it across the cards."""
    if not vram.get("supported"):
        return {}
    budget = int(vram.get("clean_budget_bytes" if clean else "budget_bytes") or 0)
    state = _fit_state(int(size_bytes or 0), budget)
    if not state:
        return {}
    count = int(vram.get("count") or 1)
    split = vram_fit.needs_split(int(size_bytes), vram, clean=clean)
    return {
        "state": state,
        "headroom_bytes": budget - int(size_bytes),
        "split": split,
        "note": _fit_note(int(size_bytes), budget, int(vram.get("total_bytes") or 0), state,
                          count=count, pool_name=str(vram.get("name") or ""), split=split),
    }


def collect_local_models(ep: Dict[str, Any]) -> Dict[str, Any]:
    root = ep["root"]
    same = bool(ep.get("same_machine"))
    out: Dict[str, Any] = {
        "endpoint": ep,
        "reachable": False,
        "error": None,
        "models": [],
        "loaded": [],
        "gpus": _gpu_list(same),
    }
    try:
        tags = _tags(root)
        out["reachable"] = True
    except Exception as e:  # noqa: BLE001
        out["error"] = f"Ollama unreachable at {root}: {str(e)[:160]}"
        out["vram"] = _vram_block(same, 0)
        out["disk"] = _disk(root, same)
        return out
    try:
        running = _ps(root)
    except Exception as e:  # noqa: BLE001
        logger.debug("local models: /api/ps failed for %s: %s", _redact_url_for_log(root), e)
        running = []

    # Where each loaded model went (which card, how many bytes on each) — only
    # answerable for the Ollama on this machine, and only with something loaded.
    placements: Dict[str, Dict[str, Any]] = {}
    if same and running:
        snap = gpu_shared_memory.vram_snapshot()
        placements = gpu_placement.placement(root, running, snap.get("gpus") if snap.get("supported") else [])

    loaded_by_name: Dict[str, Dict[str, Any]] = {}
    held = 0
    for m in running:
        name = str(m.get("name") or m.get("model") or "")
        size = int(m.get("size") or 0)
        vram_bytes = int(m.get("size_vram") or 0)
        held += vram_bytes
        details = m.get("details") or {}
        where = placements.get(name) or {}
        row = {
            "name": name,
            "size": size,
            "size_vram": vram_bytes,
            "size_cpu": max(0, size - vram_bytes),
            "gpu_pct": round(100.0 * vram_bytes / size) if size else 0,
            "expires_at": m.get("expires_at"),
            "context_length": m.get("context_length"),
            "digest": str(m.get("digest") or ""),
            "parameter_size": str(details.get("parameter_size") or ""),
            "quantization": str(details.get("quantization_level") or ""),
            "gpus": list(where.get("gpus") or []),
            "placement": where.get("placement") or ("cpu" if not vram_bytes else "unknown"),
            "per_gpu": [dict(p) for p in (where.get("per_gpu") or [])],
        }
        out["loaded"].append(row)
        if name:
            loaded_by_name[name] = row

    vram = _vram_block(same, held, placements)
    out["vram"] = vram
    out["disk"] = _disk(root, same)
    saved = mlo.options_for_endpoint(ep["id"])

    def _enrich(m: Dict[str, Any]) -> Dict[str, Any]:
        name = str(m.get("name") or m.get("model") or "")
        digest = str(m.get("digest") or "")
        details = m.get("details") or {}
        size = int(m.get("size") or 0)
        show = _show(root, name, digest) if name else {}
        row: Dict[str, Any] = {
            "name": name,
            "size": size,
            "digest": digest,
            "modified_at": m.get("modified_at"),
            "family": show.get("family") or str(details.get("family") or ""),
            "families": show.get("families") or [str(f) for f in (details.get("families") or [])],
            "parameter_size": show.get("parameter_size") or str(details.get("parameter_size") or ""),
            "quantization": show.get("quantization") or str(details.get("quantization_level") or ""),
            "capabilities": show.get("capabilities") or {},
            "context_length": int(show.get("context_length") or 0),
            "license": show.get("license") or "",
            "architecture": show.get("architecture") or "",
            "fit": fit_verdict(size, vram),
            "loaded": any(_same_model(name, k) for k in loaded_by_name),
            "options": next((v for k, v in saved.items() if _same_model(k, name)), {}),
        }
        return row

    if tags:
        with ThreadPoolExecutor(max_workers=min(4, len(tags))) as pool:
            out["models"] = list(pool.map(_enrich, tags))
    out["models"].sort(key=lambda r: (not r["loaded"], r["name"].lower()))
    return out


# ── pulls ───────────────────────────────────────────────────────────────────

class PullJob:
    """One `ollama pull`, driven from a daemon thread.

    The state is a plain dict snapshot (`.snapshot()`) that the SSE stream
    and the /pulls listing read; `version` bumps on every change so a
    stream only sends what moved."""

    def __init__(self, endpoint: Dict[str, Any], name: str):
        self.id = uuid.uuid4().hex[:12]
        self.endpoint_id = endpoint["id"]
        self.endpoint_name = endpoint.get("name", "")
        self.root = endpoint["root"]
        self.same_machine = bool(endpoint.get("same_machine"))
        self.name = name
        self.status = "queued"          # queued | pulling | done | error | cancelled
        self.status_text = "queued"
        self.completed = 0
        self.total = 0
        self.digest = ""
        self.error = ""
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.version = 0
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._layers: Dict[str, tuple] = {}

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def active(self) -> bool:
        return self.status in ("queued", "pulling")

    @property
    def cancelling(self) -> bool:
        """Cancel was asked for but the stream has not noticed yet (it only
        checks the flag between NDJSON lines). Still `active` for the page —
        the row shows until it ends — but not a pull to deduplicate against."""
        return self._cancel.is_set()

    def _bump(self) -> None:
        self.version += 1

    def update(self, **fields: Any) -> None:
        with self._lock:
            for k, v in fields.items():
                setattr(self, k, v)
            self._bump()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            total = self.total
            completed = self.completed
            return {
                "id": self.id,
                "endpoint_id": self.endpoint_id,
                "endpoint_name": self.endpoint_name,
                "name": self.name,
                "status": self.status,
                "status_text": self.status_text,
                "completed": completed,
                "total": total,
                "percent": round(100.0 * completed / total, 1) if total else 0.0,
                "digest": self.digest,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "active": self.status in ("queued", "pulling"),
                "version": self.version,
            }

    def _finish(self, status: str, text: str = "", error: str = "") -> None:
        self.update(status=status, status_text=text or status, error=error, finished_at=time.time())

    def run(self) -> None:
        """Consume Ollama's NDJSON /api/pull stream until done / cancel."""
        self.update(status="pulling", status_text="connecting")
        disk_free = None
        if self.same_machine:
            disk_free = (_disk(self.root, True) or {}).get("free_bytes")
        try:
            with _client_factory(timeout=None) as client:
                with client.stream("POST", self.root + "/api/pull",
                                   json={"model": self.name, "name": self.name, "stream": True},
                                   timeout=httpx.Timeout(30.0, read=600.0)) as r:
                    if r.status_code >= 400:
                        body = r.read().decode("utf-8", errors="replace")[:300]
                        self._finish("error", "failed", f"HTTP {r.status_code}: {body}")
                        return
                    for line in r.iter_lines():
                        if self._cancel.is_set():
                            self._finish("cancelled", "cancelled")
                            return
                        line = (line or "").strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(ev, dict):
                            continue
                        if ev.get("error"):
                            self._finish("error", "failed", str(ev["error"])[:300])
                            return
                        status = str(ev.get("status") or "")
                        digest = str(ev.get("digest") or "")
                        total = int(ev.get("total") or 0)
                        completed = int(ev.get("completed") or 0)
                        if digest and total:
                            self._layers[digest] = (completed, total)
                        if disk_free is not None and total and total > disk_free:
                            self._finish(
                                "error", "failed",
                                f"Not enough free disk for this model: a layer needs "
                                f"{total / 1e9:.1f} GB and only {disk_free / 1e9:.1f} GB are free.",
                            )
                            return
                        agg_total = sum(t for _, t in self._layers.values())
                        agg_done = sum(c for c, _ in self._layers.values())
                        self.update(
                            status_text=status or self.status_text,
                            digest=digest or self.digest,
                            total=agg_total or total,
                            completed=agg_done if agg_total else completed,
                        )
                        if status == "success":
                            self.update(completed=self.total or self.completed)
                            self._finish("done", "success")
                            return
            if self._cancel.is_set():
                self._finish("cancelled", "cancelled")
            elif self.status == "pulling":
                # Stream ended without a `success` line.
                self._finish("error", "failed", "Ollama closed the stream before finishing")
        except Exception as e:  # noqa: BLE001
            if self._cancel.is_set():
                self._finish("cancelled", "cancelled")
            else:
                self._finish("error", "failed", str(e)[:300])


class PullManager:
    def __init__(self) -> None:
        self._jobs: Dict[str, PullJob] = {}
        self._lock = threading.Lock()

    def _prune(self) -> None:
        now = time.time()
        finished = [j for j in self._jobs.values() if not j.active]
        for j in finished:
            if j.finished_at and now - j.finished_at > _PULL_KEEP_SECONDS:
                self._jobs.pop(j.id, None)
        finished = sorted((j for j in self._jobs.values() if not j.active),
                          key=lambda j: j.finished_at or 0)
        while len(finished) > _PULL_KEEP_MAX:
            self._jobs.pop(finished.pop(0).id, None)

    def start(self, endpoint: Dict[str, Any], name: str) -> tuple:
        """(job, created). An active pull of the same model on the same
        server is returned instead of starting a duplicate — unless it is
        being cancelled: cancel + pull again used to hand back the dying
        job, and the page watched it end as "cancelled" with no new pull."""
        with self._lock:
            self._prune()
            for j in self._jobs.values():
                if j.active and not j.cancelling and j.endpoint_id == endpoint["id"] and _same_model(j.name, name):
                    return j, False
            job = PullJob(endpoint, name)
            self._jobs[job.id] = job
        threading.Thread(target=job.run, name=f"ollama-pull-{job.id}", daemon=True).start()
        return job, True

    def get(self, job_id: str) -> Optional[PullJob]:
        return self._jobs.get(job_id)

    def list(self, endpoint_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            self._prune()
            jobs = list(self._jobs.values())
        if endpoint_id:
            jobs = [j for j in jobs if j.endpoint_id == endpoint_id]
        jobs.sort(key=lambda j: (not j.active, -j.started_at))
        return [j.snapshot() for j in jobs]

    def cancel(self, job_id: str) -> Optional[PullJob]:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.active:
            job.cancel()
            if job.status == "queued":
                job._finish("cancelled", "cancelled")
        return job

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()


pulls = PullManager()


async def _pull_events(job: PullJob, poll: float = 0.25):
    """SSE view on a job: a snapshot now, then one event per change."""
    last = -1
    while True:
        snap = job.snapshot()
        if snap["version"] != last:
            last = snap["version"]
            yield f"data: {json.dumps(snap)}\n\n"
        if not snap["active"]:
            yield "event: end\ndata: {}\n\n"
            return
        await asyncio.sleep(poll)


# ── load / unload / delete ──────────────────────────────────────────────────

def _ollama_error(r: httpx.Response) -> str:
    try:
        body = r.json()
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])[:300]
    except ValueError:
        pass
    return (r.text or f"HTTP {r.status_code}")[:300]


def _set_keep_alive(root: str, name: str, keep_alive: Any, is_embedding: bool = False,
                    options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load (keep_alive > 0) or evict (0) a model. `/api/generate` with no
    prompt does exactly that; embedding models only answer `/api/embed`.

    `options` are the saved load options (num_ctx / num_gpu / main_gpu):
    they decide the context the runner is built with and the card it lands
    on, so the Load button must send them — otherwise the model came up
    with the server defaults and the next chat request reloaded it."""
    paths = ["/api/embed", "/api/generate"] if is_embedding else ["/api/generate", "/api/embed"]
    last = ""
    load_opts = {k: v for k, v in (options or {}).items() if k != "keep_alive" and v not in (None, "")}
    for path in paths:
        body: Dict[str, Any] = {"model": name, "keep_alive": keep_alive}
        if load_opts and keep_alive not in (0, "0"):
            body["options"] = load_opts
        if path == "/api/embed":
            body["input"] = ""
        r = _post_json(root, path, body, _LOAD_TIMEOUT)
        if r.status_code < 400:
            return {"ok": True, "via": path}
        last = _ollama_error(r)
        if r.status_code == 404:
            raise HTTPException(404, last or f"{name} is not installed")
        # "does not support generate" → try the other path.
        if "support" not in last.lower():
            break
    raise HTTPException(502, last or "Ollama refused the request")


def _keep_alive_for(ep_id: str, name: str) -> Any:
    saved = mlo.get_options(ep_id, name)
    return saved.get("keep_alive") or "5m"


# ── discover ────────────────────────────────────────────────────────────────

def discover(q: str, vram: Dict[str, Any], installed: List[str]) -> List[Dict[str, Any]]:
    """The catalogue filtered by `q`, each tag with a fit verdict against
    the card with nothing loaded (a pull is a decision about later)."""
    out = []
    for entry in catalog.search(q):
        tags = []
        for tag in entry["tags"]:
            name = catalog.full_name(entry, tag)
            size = catalog.tag_size_bytes(tag["gb"])
            tags.append({
                "tag": tag["tag"],
                "name": name,
                "params": tag["params"],
                "gb": tag["gb"],
                "size_bytes": size,
                "fit": fit_verdict(size, vram, clean=True),
                "installed": any(_same_model(name, i) for i in installed),
            })
        out.append({
            "name": entry["name"],
            "family": entry["family"],
            "vendor": entry["vendor"],
            "blurb": entry["blurb"],
            "capabilities": entry["capabilities"],
            "default_tag": entry["default_tag"],
            "tags": tags,
        })
    return out


# ── router ──────────────────────────────────────────────────────────────────

def setup_local_models_routes() -> APIRouter:
    router = APIRouter(prefix="/api/local-models", tags=["local-models"])

    def _endpoints_for(request: Request) -> List[Dict[str, Any]]:
        owner = effective_user(request) or ""
        is_admin = True
        try:
            auth_mgr = getattr(request.app.state, "auth_manager", None)
            if owner and auth_mgr is not None and getattr(auth_mgr, "is_admin", None):
                is_admin = bool(auth_mgr.is_admin(owner))
        except Exception:  # noqa: BLE001
            is_admin = False
        return list_ollama_endpoints(owner=owner, is_admin=is_admin)

    async def _body(request: Request) -> Dict[str, Any]:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "JSON body required")
        if not isinstance(data, dict):
            raise HTTPException(400, "JSON object required")
        return data

    @router.get("")
    async def api_list(request: Request, endpoint_id: Optional[str] = Query(None)):
        require_user(request)
        endpoints = _endpoints_for(request)
        ep = _pick_endpoint(endpoint_id, endpoints) if endpoints else None
        if ep is None:
            return {"endpoints": [], "endpoint_id": "", "reachable": False,
                    "error": "No Ollama endpoint is configured", "models": [],
                    "loaded": [], "gpus": [], "vram": {"supported": False}, "disk": {}, "pulls": []}
        data = await asyncio.to_thread(collect_local_models, ep)
        data["endpoints"] = endpoints
        data["endpoint_id"] = ep["id"]
        data["pulls"] = pulls.list(ep["id"])
        data["ts"] = time.time()
        return data

    @router.get("/discover")
    async def api_discover(request: Request, q: str = Query(""),
                           endpoint_id: Optional[str] = Query(None)):
        require_user(request)
        endpoints = _endpoints_for(request)
        ep = _pick_endpoint(endpoint_id, endpoints) if endpoints else None
        same = bool(ep and ep.get("same_machine"))
        installed: List[str] = []
        if ep is not None:
            try:
                installed = [str(m.get("name") or m.get("model") or "")
                             for m in await asyncio.to_thread(_tags, ep["root"])]
            except Exception:  # noqa: BLE001
                installed = []
        vram = await asyncio.to_thread(_vram_block, same, 0)
        return {
            "q": q,
            "endpoint_id": ep["id"] if ep else "",
            "vram": vram,
            "items": discover(q, vram, installed),
            "approximate": True,
        }

    @router.get("/pulls")
    async def api_pulls(request: Request, endpoint_id: Optional[str] = Query(None)):
        require_user(request)
        return {"pulls": pulls.list(endpoint_id)}

    @router.get("/pulls/{job_id}/events")
    async def api_pull_events(request: Request, job_id: str):
        require_user(request)
        job = pulls.get(job_id)
        if job is None:
            raise HTTPException(404, "Unknown pull")
        return StreamingResponse(_pull_events(job), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.delete("/pulls/{job_id}")
    async def api_pull_cancel(request: Request, job_id: str):
        require_admin(request)
        job = pulls.cancel(job_id)
        if job is None:
            raise HTTPException(404, "Unknown pull")
        return {"ok": True, "pull": job.snapshot()}

    @router.post("/pull")
    async def api_pull(request: Request, stream: bool = Query(True)):
        require_admin(request)
        body = await _body(request)
        name = validate_model_name(body.get("name"))
        ep = _pick_endpoint(body.get("endpoint_id"), _endpoints_for(request))
        job, created = pulls.start(ep, name)
        if not stream:
            return {"ok": True, "created": created, "pull": job.snapshot()}
        return StreamingResponse(_pull_events(job), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/load")
    async def api_load(request: Request):
        require_admin(request)
        body = await _body(request)
        name = validate_model_name(body.get("name"))
        ep = _pick_endpoint(body.get("endpoint_id"), _endpoints_for(request))
        keep_alive = body.get("keep_alive")
        if keep_alive in (None, ""):
            keep_alive = _keep_alive_for(ep["id"], name)
        else:
            try:
                keep_alive = mlo.sanitize_options({"keep_alive": keep_alive}).get("keep_alive", "5m")
            except ValueError as e:
                raise HTTPException(400, str(e))
        is_embedding = bool(body.get("embedding"))
        saved = mlo.get_options(ep["id"], name)
        result = await asyncio.to_thread(_set_keep_alive, ep["root"], name, keep_alive, is_embedding, saved)
        result["keep_alive"] = keep_alive
        if saved.get("main_gpu") is not None:
            result["main_gpu"] = saved["main_gpu"]
        return result

    @router.post("/unload")
    async def api_unload(request: Request):
        require_admin(request)
        body = await _body(request)
        name = validate_model_name(body.get("name"))
        ep = _pick_endpoint(body.get("endpoint_id"), _endpoints_for(request))
        is_embedding = bool(body.get("embedding"))
        return await asyncio.to_thread(_set_keep_alive, ep["root"], name, 0, is_embedding)

    @router.get("/{name:path}/options")
    async def api_get_options(request: Request, name: str, endpoint_id: Optional[str] = Query(None)):
        require_user(request)
        name = validate_model_name(name)
        ep = _pick_endpoint(endpoint_id, _endpoints_for(request))
        return {"endpoint_id": ep["id"], "name": name, "options": mlo.get_options(ep["id"], name)}

    @router.put("/{name:path}/options")
    async def api_put_options(request: Request, name: str, endpoint_id: Optional[str] = Query(None)):
        require_admin(request)
        name = validate_model_name(name)
        body = await _body(request)
        ep = _pick_endpoint(endpoint_id or body.get("endpoint_id"), _endpoints_for(request))
        raw = body.get("options", body)
        if isinstance(raw, dict):
            raw = {k: v for k, v in raw.items() if k != "endpoint_id"}
        try:
            saved = mlo.set_options(ep["id"], name, raw)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "endpoint_id": ep["id"], "name": name, "options": saved}

    @router.delete("/{name:path}")
    async def api_delete(request: Request, name: str, endpoint_id: Optional[str] = Query(None)):
        require_admin(request)
        name = validate_model_name(name)
        ep = _pick_endpoint(endpoint_id, _endpoints_for(request))

        def _do() -> Dict[str, Any]:
            with _client_factory(_MUTATE_TIMEOUT) as client:
                r = client.request("DELETE", ep["root"] + "/api/delete", json={"model": name, "name": name})
            if r.status_code == 404:
                raise HTTPException(404, f"{name} is not installed on {ep['name']}")
            if r.status_code >= 400:
                raise HTTPException(502, _ollama_error(r))
            return {"ok": True, "name": name}

        result = await asyncio.to_thread(_do)
        # The saved load options belong to the blob that just went away.
        try:
            mlo.set_options(ep["id"], name, {})
        except Exception:  # noqa: BLE001
            pass
        return result

    return router
