"""Change sets over HTTP — "prove it" as an endpoint.

The preview endpoints store nothing. A change set is assembled from records Faustus
already keeps, judged by `prove`, and handed back; asking twice about the same
job gives the same fingerprint, which is what makes it a report rather than a
new measurement.

`/build` is pure — it takes the blocks and answers with the verdict and the
refusals — so a caller can check a report it is about to make before making
it. `/from-dispatch/{job_id}` does the same for a job that already ran, which
is the one somebody actually reaches for: it answers "can I believe what that
worker said" with the file list Faustus saw rather than the one the worker
sent.
"""

import logging
import asyncio
import sqlite3

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import changesets
from src.contracts import ContractError
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)


async def _json_object(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return payload


def _answer(changeset, proof=None) -> dict:
    proof = proof if proof is not None else changesets.judge(changeset)
    return {
        "ok": True, "checked_at": now_iso(),
        "changeset": changeset.to_dict(),
        "fingerprint": changeset.fingerprint(),
        "proof": proof,
        "unsupported_claims": [dict(p) for p in changeset.unsupported_claims()],
        "unclaimed_changes": list(changeset.unclaimed_changes()),
        "gaps": [dict(g) for g in changeset.evidence_gaps()],
        "rendered": changesets.render(changeset, proof),
    }


def setup_changesets_routes():
    router = APIRouter(prefix="/api/changesets", tags=["changesets"])

    @router.get("/receipts")
    def receipts(request: Request, project_id: str | None = None,
                 workspace: str | None = None, run_id: str | None = None,
                 verified_only: bool = False, limit: int = 50, before: int | None = None):
        """Immutable evidence from completed execution, never client previews."""
        require_admin(request)
        from routes.dispatch_routes import _owner
        from src.changeset_store import Store, ReceiptError
        owner = _owner(request) or ""
        if not 1 <= limit <= 200 or (before is not None and before < 1):
            raise HTTPException(400, "invalid receipt pagination")
        try:
            rows = Store().list(owner=owner, project_id=project_id, workspace=workspace,
                                run_id=run_id, verified_only=verified_only, limit=limit, before=before)
        except (ReceiptError, OSError, sqlite3.Error):
            raise HTTPException(503, "Evidence history is unavailable; no records were changed")
        return {"ok": True, "receipts": rows,
                "next_cursor": rows[-1]["cursor"] if len(rows) == limit else None}

    @router.get("/receipts/{receipt_id}")
    def receipt(receipt_id: str, request: Request):
        require_admin(request)
        from routes.dispatch_routes import _owner
        from src.changeset_store import Store, ReceiptError
        owner = _owner(request) or ""
        try:
            saved = Store().get(receipt_id, owner=owner)
        except (ReceiptError, OSError, sqlite3.Error):
            raise HTTPException(503, "Evidence history is unavailable; no records were changed")
        if saved is None:
            raise HTTPException(404, "no such evidence receipt")
        return {"ok": True, **saved}

    @router.post("/build")
    async def build(request: Request):
        """Pure. Assemble, judge, and say what does not add up — before
        anybody publishes the summary that goes with it."""
        require_admin(request)
        payload = await _json_object(request)
        try:
            changeset = changesets.build(
                intent=str(payload.get("intent") or "implement"),
                workspace=str(payload.get("workspace") or ""),
                checkpoint=str(payload.get("checkpoint") or ""),
                changes=payload.get("changes"),
                verification=payload.get("verification"),
                claims=payload.get("claims"),
                commands=payload.get("commands"),
                review=payload.get("review"),
                plan=str(payload.get("plan") or ""),
                title=str(payload.get("title") or ""),
                run_id=str(payload.get("run_id") or ""),
                owner=str(payload.get("owner") or ""),
                project_id=str(payload.get("project_id") or ""),
                artifact_ids=payload.get("artifact_ids") or [])
        except ContractError as e:
            # A refusal is the answer, not an error: "an explore that wrote to
            # four files" is exactly what somebody asked this endpoint about.
            return {"ok": False, "field": e.path, "reason": e.message,
                    "checked_at": now_iso()}
        return _answer(changeset)

    @router.get("/from-dispatch/{job_id}")
    def from_dispatch(job_id: str, request: Request, intent: str = "implement"):
        """The one somebody reaches for: can I believe what that worker said.

        The file list is the one Faustus SAW on disk — dispatch already
        overwrites the workers' claims with it and keeps the difference — so
        the claim check has something real to be wrong about."""
        require_admin(request)
        from src import dispatch

        job = dispatch.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no dispatched job {job_id}")
        # Use exactly the same owner/token-scope policy as the job's own route.
        # An admin identity is not a grant to read another admin's work.
        from routes.dispatch_routes import _owner
        owner = _owner(request)
        if not dispatch.visible_to(job, owner or None):
            raise HTTPException(status_code=404, detail="no such dispatch job")
        compact = dispatch.compact(job)
        try:
            changeset = changesets.from_dispatch(
                compact, intent=intent,
                workspace=str(getattr(job, "workspace", "") or ""),
                owner=str(getattr(job, "owner", "") or ""))
        except ContractError as e:
            return {"ok": False, "field": e.path, "reason": e.message,
                    "job_id": job_id}
        measured = compact.get("result") or {}
        job_proof = measured.get("proof") if isinstance(measured, dict) else None
        answer = _answer(changeset, job_proof if isinstance(job_proof, dict) else None)
        # The job's own one-line verdict travels alongside, unchanged. It is a
        # sentence for a person; the proof is the part with the doubts in it,
        # and showing both keeps the difference visible.
        answer["job_verdict"] = compact.get("verdict") or ""
        return answer

    @router.post("/diff")
    async def diff(request: Request):
        """The actual diff, fetched now rather than stored then.

        Separate from everything else on purpose: a change set holds the
        checkpoint sha so that reading one costs nothing, and four hundred
        kilobytes of text nobody asked for is the cost this avoids."""
        require_admin(request)
        payload = await _json_object(request)
        max_chars = payload.get("max_chars", 400_000)
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 400_000:
            raise HTTPException(400, "max_chars must be an integer between 1 and 400000")
        path = payload.get("path", "")
        if not isinstance(path, str) or len(path) > 2000:
            raise HTTPException(400, "path must be text of at most 2000 characters")
        try:
            changeset = changesets.build(
                intent=str(payload.get("intent") or "implement"),
                workspace=str(payload.get("workspace") or ""),
                checkpoint=str(payload.get("checkpoint") or ""),
                changes=payload.get("changes"))
        except ContractError as e:
            return {"ok": False, "field": e.path, "reason": e.message}
        return await asyncio.to_thread(changesets.diff_of, changeset, path=path, max_chars=max_chars)

    return router


def setup_doctor_routes():
    """`/api/doctor` — what this machine can actually do, asked rather than
    assumed. Its own tiny router so it can be mounted (and read) on its own."""
    router = APIRouter(prefix="/api/doctor", tags=["doctor"])

    @router.get("")
    def check(request: Request, area: str = "", verbose: bool = False):
        require_admin(request)
        from src import doctor

        areas = [a.strip() for a in area.split(",") if a.strip()] or None
        report = doctor.run(areas=areas)
        report["rendered"] = doctor.render(report, verbose=verbose)
        return report

    return router
