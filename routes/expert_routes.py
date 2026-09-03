"""Specialist experts API — /api/experts/* (services/experts.py).

An expert is a local specialist with its own corpus: a profile with a rubric,
a folder of the user's own PDFs and notes, a chunk index that remembers which
PAGE each chunk came from, and counters that record whether its corrections
were accepted. These endpoints are the human end of that: create one, drop
books into it, reindex, search it, resolve a citation back to the page, and
see the EXACT block the model would be given.

Admin-only, like the rest of the brain: an expert's instructions are standing
orders the agent will follow, and its corpus is the user's private library.

The two reads that a coordinating model would want — the list and the search —
also answer in robot mode (``?robot=1`` / ``?format=toon``,
src/robot_envelope.py) with the LEAN projections at the bottom of this file:
one flat row per expert / per hit, without the excerpt bodies and the
bookkeeping. A call without those query parameters answers exactly as it
otherwise would.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from core.middleware import require_admin
from src import robot_envelope as robot
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)


class ExpertCreate(BaseModel):
    name: str
    description: Optional[str] = None
    instructions: Optional[str] = None
    rubric: Optional[Any] = None            # list[str] or a newline-separated string
    model: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    enabled: Optional[bool] = None


class ExpertPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None
    rubric: Optional[Any] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    enabled: Optional[bool] = None
    owner: Optional[str] = None


def _owner(request: Request) -> str:
    try:
        return str(effective_user(request) or "")
    except Exception:  # noqa: BLE001 - attribution must not 500 the route
        return ""


def setup_expert_routes() -> APIRouter:
    router = APIRouter(prefix="/api/experts", tags=["experts"])

    def _require(slug: str) -> Dict[str, Any]:
        from services import experts
        profile = experts.load_expert(slug)
        if not profile:
            raise HTTPException(status_code=404, detail="no such expert")
        return profile

    # ── the roster ────────────────────────────────────────────────────────

    @router.get("")
    async def list_experts(request: Request,
                           _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Every expert with its corpus size, chunk count and counters."""
        from services import experts

        def payload() -> Dict[str, Any]:
            return {"status": "success", **experts.list_payload(_owner(request))}
        if robot.wants(request):
            return await robot.reply(request, lambda: lean_experts(payload()))
        return payload()

    @router.post("")
    async def create_expert(request: Request, body: ExpertCreate,
                            _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        from services import experts
        try:
            profile = experts.create_expert(
                body.name,
                description=body.description or "",
                instructions=body.instructions or "",
                rubric=_rubric(body.rubric),
                model=body.model or "",
                temperature=(0.2 if body.temperature is None else body.temperature),
                top_p=(1.0 if body.top_p is None else body.top_p),
                owner=_owner(request),
                enabled=(True if body.enabled is None else bool(body.enabled)),
            )
        except experts.ExpertError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "success", "expert": profile,
                "summary": experts.summary(profile["slug"])}

    # `/suggest` is declared BEFORE `/{slug}` so it is not read as a slug.
    @router.get("/suggest")
    async def suggest_experts(request: Request, q: str = Query(default=""),
                              k: int = Query(default=2),
                              seed: Optional[int] = Query(default=None),
                              _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Thompson sampling over the accepted/rejected counters. It SUGGESTS,
        it never imposes — a never-used expert is always reachable."""
        from services import experts
        return {"status": "success",
                "suggestions": experts.suggest(q, _owner(request), k, seed=seed)}

    @router.get("/{slug}")
    async def get_expert(slug: str,
                         _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Full profile plus the corpus file list."""
        from services import experts
        _require(slug)
        return {"status": "success", **(experts.detail_payload(slug) or {})}

    @router.patch("/{slug}")
    async def patch_expert(slug: str, body: ExpertPatch,
                           _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        from services import experts
        _require(slug)
        updates: Dict[str, Any] = {}
        for key in ("name", "description", "instructions", "model",
                    "temperature", "top_p", "enabled", "owner"):
            value = getattr(body, key, None)
            if value is not None:
                updates[key] = value
        if body.rubric is not None:
            updates["rubric"] = _rubric(body.rubric)
        try:
            profile = experts.update_expert(slug, updates)
        except experts.ExpertError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not profile:
            raise HTTPException(status_code=404, detail="no such expert")
        return {"status": "success", "expert": profile,
                "summary": experts.summary(slug)}

    @router.delete("/{slug}")
    async def remove_expert(slug: str,
                            _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        from services import experts
        _require(slug)
        if not experts.delete_expert(slug):
            raise HTTPException(status_code=500, detail="could not delete that expert")
        return {"status": "success", "deleted": True, "slug": slug}

    # ── the corpus ────────────────────────────────────────────────────────

    @router.post("/{slug}/corpus")
    async def upload_corpus(slug: str, files: List[UploadFile] = File(...),
                            _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Drop one or more files into the expert's corpus and reindex.

        Streamed to disk in 1 MiB blocks: nothing leaves the machine and a
        900-page PDF is never held in memory whole. There is no size cap
        beyond the disk — that is the point of the feature.
        """
        from services import experts
        profile = _require(slug)
        slug = profile["slug"]
        stored: List[str] = []
        rejected: List[Dict[str, str]] = []
        for upload in files or []:
            path = None
            try:
                path = experts.corpus_target_path(slug, upload.filename)
                with open(path, "wb") as fh:
                    while True:
                        block = await upload.read(experts.READ_CHUNK_BYTES)
                        if not block:
                            break
                        await run_in_threadpool(fh.write, block)
                stored.append(os.path.basename(path))
            except experts.ExpertError as exc:
                rejected.append({"name": str(upload.filename or ""), "reason": str(exc)})
            except Exception as exc:  # noqa: BLE001 - one bad upload, not the batch
                logger.warning("experts: upload of %s failed: %s", upload.filename, exc)
                rejected.append({"name": str(upload.filename or ""), "reason": str(exc)})
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        result = await run_in_threadpool(experts.reindex, slug)
        return {"status": "success", "uploaded": stored, "rejected": rejected,
                **result, "files": experts.corpus_files(slug)}

    @router.get("/{slug}/corpus/{filename}")
    async def download_corpus_file(slug: str, filename: str,
                                   _admin: None = Depends(require_admin)):
        """The corpus file itself — what a citation's ``file_url`` points at,
        so the user can open the book at the page the model quoted."""
        from services import experts
        profile = _require(slug)
        slug = profile["slug"]
        base = os.path.realpath(experts.corpus_dir(slug))
        target = os.path.realpath(os.path.join(base, experts._safe_filename(filename)))
        if os.path.commonpath([base, target]) != base or not os.path.isfile(target):
            raise HTTPException(status_code=404, detail="no such corpus file")
        return FileResponse(target, filename=os.path.basename(target))

    @router.delete("/{slug}/corpus/{filename}")
    async def delete_corpus_file(slug: str, filename: str,
                                 _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        from services import experts
        profile = _require(slug)
        slug = profile["slug"]
        if not await run_in_threadpool(experts.delete_corpus_file, slug, filename):
            raise HTTPException(status_code=404, detail="no such corpus file")
        return {"status": "success", "deleted": True, "file": filename,
                "files": experts.corpus_files(slug), "chunks": len(experts.load_index(slug))}

    @router.post("/{slug}/reindex")
    async def reindex_corpus(slug: str,
                             _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Incremental: unchanged files are skipped, edited files re-chunked,
        a deleted file's chunks dropped."""
        from services import experts
        profile = _require(slug)
        result = await run_in_threadpool(experts.reindex, profile["slug"])
        return {"status": "success", **result,
                "indexed_at": experts.indexed_at(profile["slug"])}

    # ── retrieval ─────────────────────────────────────────────────────────

    @router.get("/{slug}/search")
    async def search_corpus(request: Request, slug: str, q: str = Query(default=""),
                            k: int = Query(default=6),
                            _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """BM25-lite always; fused with this expert's embedding collection when
        there is one. ``degraded: true`` means the semantic lane is missing —
        never an error."""
        from services import experts
        profile = _require(slug)

        def payload() -> Dict[str, Any]:
            return {"status": "success", "slug": profile["slug"], "query": q,
                    **experts.search(profile["slug"], q, k)}
        if robot.wants(request):
            return await robot.reply(request, lambda: lean_search(payload()))
        return payload()

    @router.get("/{slug}/citation/{chunk_id}")
    async def resolve_citation(slug: str, chunk_id: str,
                               _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Where a ``[Cn]`` marker actually came from. ``page`` is null when
        the file's pages could not be determined — never a guess."""
        from services import experts
        profile = _require(slug)
        found = experts.citation(profile["slug"], chunk_id)
        if not found:
            raise HTTPException(status_code=404, detail="no such chunk")
        return {"status": "success", "citation": found}

    @router.get("/{slug}/page")
    async def render_corpus_page(slug: str, source: str = Query(default=""),
                                 page: int = Query(default=0),
                                 _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """A PNG of one PDF page when a renderer is already installed, else
        ``available: false`` with the reason and a link to the file."""
        from services import experts
        profile = _require(slug)
        return {"status": "success",
                "render": await run_in_threadpool(experts.render_page,
                                                  profile["slug"], source, page)}

    @router.get("/{slug}/block")
    async def preview_block(slug: str, q: str = Query(default=""),
                            chars: Optional[int] = Query(default=None),
                            _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """The EXACT block the model would see — the same function the review
        pipeline calls, nothing regenerated."""
        from services import experts
        profile = _require(slug)
        block = experts.expert_block(profile["slug"], q, chars)
        return {"status": "success", **block, "chars": len(block.get("text") or ""),
                "budget": (chars if chars is not None else experts.context_budget()),
                "enabled": experts.experts_enabled()}

    # ── the learning signal ───────────────────────────────────────────────

    @router.post("/{slug}/feedback")
    async def expert_feedback(slug: str, accepted: int = Query(default=0),
                              rejected: int = Query(default=0),
                              _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Accepted / rejected corrections — the Beta posterior /suggest
        samples, and the dataset phase 2 would need before any fine-tune."""
        from services import experts
        profile = _require(slug)
        return {"status": "success",
                "usage": experts.record_feedback(profile["slug"], accepted, rejected)}

    return router


def _rubric(value: Any) -> List[str]:
    """A rubric may arrive as a list or as a textarea's newline-separated text."""
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip(" -*\t") for line in value.splitlines() if line.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


# ---------------------------------------------------------------------------
# Robot-mode projections (the shared envelope is src/robot_envelope.py; the
# shared projections live in src/robot_projection.py, which is not this
# feature's file — so these two live here, in the module that owns them.)
# ---------------------------------------------------------------------------

_TEXT_CELL = 200


def _cell(value: Any, limit: int = _TEXT_CELL) -> str:
    """One line, bounded — a table row is a line, so a cell cannot hold one."""
    text = " ".join(str(value if value is not None else "").split())
    return text[:limit]


def _int_cell(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def lean_experts(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The roster as rows: who they are and how much corpus each has.

    Dropped: the description's second sentence (squashed to one line), the
    owner and ``updated_at`` — a coordinating model picks an expert by what it
    covers and how well its corrections have landed.
    """
    try:
        rows = []
        for row in payload.get("experts") or []:
            if not isinstance(row, dict):
                continue
            rows.append({
                "slug": _cell(row.get("slug"), 60),
                "name": _cell(row.get("name"), 80),
                "description": _cell(row.get("description")),
                "enabled": bool(row.get("enabled")),
                "files": _int_cell(row.get("corpus_files")),
                "chunks": _int_cell(row.get("chunks")),
                "accepted": _int_cell(row.get("accepted")),
                "rejected": _int_cell(row.get("rejected")),
            })
        return {"experts": rows, "enabled": bool(payload.get("enabled"))}
    except Exception:  # noqa: BLE001 - a projection never costs the answer
        return payload


def lean_search(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The hits as rows: the citation coordinates and a bounded excerpt.

    Kept deliberately: ``page`` (null when unknown, never guessed), ``tier``
    and ``degraded`` — a coordinating model has to be able to SEE that the
    semantic lane was missing.
    """
    try:
        rows = []
        for hit in payload.get("hits") or []:
            if not isinstance(hit, dict):
                continue
            page = hit.get("page")
            rows.append({
                "chunk_id": _cell(hit.get("chunk_id"), 40),
                "source": _cell(hit.get("source"), 80),
                "page": (page if isinstance(page, int) else None),
                "lines": f"{_int_cell(hit.get('start_line'))}-{_int_cell(hit.get('end_line'))}",
                "score": hit.get("score"),
                "text": _cell(hit.get("text"), 400),
            })
        return {"slug": _cell(payload.get("slug"), 60),
                "hits": rows,
                "tier": _cell(payload.get("tier"), 20),
                "degraded": bool(payload.get("degraded"))}
    except Exception:  # noqa: BLE001
        return payload
