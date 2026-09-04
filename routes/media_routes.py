"""Media renders over HTTP.

The shape of this router is the phase's argument in miniature: there is an
endpoint to list the approved templates and one to plan a render, and there is
**no endpoint that takes a graph**. A caller picks a template id and fills in
its declared inputs; anything else is a refusal naming the field.

`require_admin` throughout, like the workflow routes. The place a person is
actually needed is a `human_approval` — which for media is the cost and the
publication, and lives on the approvals routes behind `require_human`.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import media_runs, media_workflows
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


def _current_user(request: Request) -> str:
    return str(getattr(request.state, "current_user", "") or "").strip()


def setup_media_routes():
    router = APIRouter(prefix="/api/media", tags=["media"])

    @router.get("/workflows")
    def list_workflows(request: Request):
        """The approved recipes, and any template file that would not parse.

        Broken ones are listed rather than dropped: a recipe that silently
        vanished is how somebody spends an afternoon wondering why the model
        refuses to use it."""
        require_admin(request)
        found = media_workflows.catalogue()
        return {"ok": True, "checked_at": now_iso(),
                "directory": found["directory"],
                "workflows": [w.to_dict() for w in found["workflows"]],
                "broken": found["broken"],
                "note": "a render can only use one of these. There is no endpoint "
                        "that accepts a graph, on purpose."}

    @router.get("/engine")
    def engine_state(request: Request):
        """Every engine, not just the first. On a two-GPU machine the useful
        fact is usually "one of them is down", and a summary that averaged
        them would hide exactly that. A ComfyUI with no checkpoint is not
        ready, and the fix is in the sentence."""
        require_admin(request)
        from src.media_backends import pool

        engines = pool.survey()
        ready = [e for e in engines if e.ok]
        first = ready[0] if ready else (engines[0] if engines else None)
        body = {
            "ok": bool(ready),
            "checked_at": now_iso(),
            "engines": [e.to_dict() for e in engines],
            "ready": len(ready), "configured": len(engines),
            # The single-engine keys stay, so nothing that read them breaks.
            "url": first.url if first else "",
            "reason": (first.reason if first else "not_configured"),
            "detail": (first.detail if first else "no engines are configured"),
        }
        if ready:
            body["checkpoints"] = sorted({c for e in ready for c in e.checkpoints})
        return body

    @router.post("/plan")
    async def plan(request: Request):
        """What a render would be, without queueing it — the resolved values,
        the seed, the models and their licences, and whether this engine can
        actually do it."""
        require_admin(request)
        payload = await _json_object(request)
        workflow_id = str(payload.get("workflow") or "")
        if not workflow_id:
            raise HTTPException(status_code=400,
                                detail="name the template in `workflow`")
        return media_runs.plan(
            workflow_id, payload.get("inputs") or {},
            version=str(payload.get("version") or ""),
            check_engine=bool(payload.get("check_engine", True)))

    @router.post("/runs")
    async def start(request: Request):
        require_admin(request)
        payload = await _json_object(request)
        workflow_id = str(payload.get("workflow") or "")
        if not workflow_id:
            raise HTTPException(status_code=400,
                                detail="name the template in `workflow`")
        out = media_runs.start(
            workflow_id, payload.get("inputs") or {},
            version=str(payload.get("version") or ""),
            owner=str(payload.get("owner") or _current_user(request) or ""),
            project_id=str(payload.get("project_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            approval_id=str(payload.get("approval_id") or ""))
        if not out.get("ok") and out.get("reason") in ("no_such_workflow", "bad_inputs"):
            # A template that does not exist, or an input it does not accept,
            # is the caller's mistake and reads as one.
            raise HTTPException(status_code=400,
                                detail=f"{out.get('field') or out['reason']}: "
                                       f"{out.get('detail')}")
        return out

    @router.get("/runs")
    def list_runs(request: Request, owner: str = "", limit: int = 20):
        require_admin(request)
        rows = media_runs.recent(owner=owner, limit=limit)
        return {"ok": True, "runs": rows, "count": len(rows)}

    @router.get("/runs/{run_id}")
    def one_run(run_id: str, request: Request):
        require_admin(request)
        record = media_runs.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no media run {run_id}")
        return {"ok": True, "run": record}

    @router.post("/runs/{run_id}/poll")
    def poll(run_id: str, request: Request):
        """Ask the engine what happened and write it down. Safe to call as
        often as anyone likes, and safe after a restart — which is why it asks
        the engine rather than trusting the status on the row."""
        require_admin(request)
        out = media_runs.poll(run_id)
        if not out.get("ok"):
            raise HTTPException(status_code=404, detail=out.get("reason"))
        return out

    @router.post("/runs/{run_id}/cancel")
    def cancel(run_id: str, request: Request):
        require_admin(request)
        out = media_runs.cancel(run_id)
        if not out.get("ok") and out.get("reason") == "not_found":
            raise HTTPException(status_code=404, detail=f"no media run {run_id}")
        return out

    return router
