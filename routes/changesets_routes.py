"""Change sets over HTTP — "prove it" as an endpoint.

Nothing here stores anything. A change set is assembled from records Faustus
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


def _answer(changeset) -> dict:
    proof = changesets.judge(changeset)
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
                changes=payload.get("changes") or {},
                verification=payload.get("verification") or {},
                claims=payload.get("claims") or [],
                commands=payload.get("commands") or [],
                review=payload.get("review") or {},
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
        compact = dispatch.compact(job)
        try:
            changeset = changesets.from_dispatch(
                compact, intent=intent,
                workspace=str(getattr(job, "workspace", "") or ""))
        except ContractError as e:
            return {"ok": False, "field": e.path, "reason": e.message,
                    "job_id": job_id}
        answer = _answer(changeset)
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
        try:
            changeset = changesets.build(
                intent=str(payload.get("intent") or "implement"),
                workspace=str(payload.get("workspace") or ""),
                checkpoint=str(payload.get("checkpoint") or ""),
                changes=payload.get("changes") or {})
        except ContractError as e:
            return {"ok": False, "field": e.path, "reason": e.message}
        return changesets.diff_of(changeset, path=str(payload.get("path") or ""),
                                  max_chars=int(payload.get("max_chars") or 400_000))

    return router
