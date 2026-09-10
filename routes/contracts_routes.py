"""Read-only view of the Phase 0 contracts: the backend catalogue and a
manifest validator.

Both endpoints are pure. They read no database, write no file, start no
process and hold no secret — a manifest goes in, a verdict comes out. That is
deliberate: the point of exposing them is that a skill author (or the model
drafting a manifest) can find out *why* a manifest is refused without anything
being installed, and an endpoint that could install something would need an
approval card, which Phase 2 is where that belongs.

The verdict format follows the module's own rule: a refusal names the field
and what was seen, so the response carries `path` separately from `message`
instead of one prose blob a UI has to regex.
"""

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import capability_registry as registry
from src.contracts import ContractError, SkillManifest
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)

#: The v2 spec's own JSON Schema files, read fresh off disk rather than
#: baked into this module — the schema this endpoint answers with and the
#: schema `src/contracts/task.py`/`tool.py` were written against can never
#: drift apart when there is only one copy on disk. Repo root the same way
#: `codex_routes.py` finds `integrations/`: two `.parent`s up from this file.
_SPEC_V2_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "docs" / "spec" / "v2" / "schemas"

#: name -> version, for the eight schemas Lote 2 gave code to
#: (`src/contracts/{tool,task,errors}.py`). Versions are read out of each
#: file rather than hard-coded twice.
_SPEC_V2_SCHEMA_NAMES = (
    "tool_descriptor", "tool_invocation", "tool_result", "evidence_ref",
    "task_state", "question_request", "approval_request", "event_envelope",
)


def setup_contracts_routes():
    router = APIRouter(prefix="/api/contracts")

    @router.get("/backends")
    def backends(request: Request):
        """The catalogue, with intent and observation kept apart.

        `declared` is design; `observed` is what could be seen just now. A
        backend that is declared and not implemented reports `unavailable`
        with that as its evidence, rather than being hidden — "why is there no
        GPU option" is a question this page should answer."""
        require_admin(request)
        observations = {o.backend_id: o.to_dict() for o in registry.observe_all()}
        return {
            "checked_at": now_iso(),
            "backends": [
                {"declared": d.to_dict(), "observed": observations[d.id]}
                for d in registry.declarations()
            ],
            "docker": registry.docker_evidence(),
            "note": "declarations are durable intent; observations are disposable facts",
        }


    @router.post("/skill/validate")
    async def validate_skill(request: Request):
        """Parse a manifest and say what it would be allowed to do.

        A rejection is a 200 with `ok: false`, not a 4xx: the caller asked a
        question ("is this manifest valid?") and got an answer. Reserving the
        error status for a malformed request body keeps the two apart."""
        require_admin(request)
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Body must be JSON")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")

        manifest_body = payload.get("manifest", payload)
        try:
            manifest = SkillManifest.parse(manifest_body)
        except ContractError as e:
            return {
                "ok": False,
                "error": {"path": e.path, "message": e.message, "detail": str(e)},
            }
        except Exception as e:                       # a shape parse could not reach
            logger.debug("manifest validation failed", exc_info=True)
            return {"ok": False, "error": {"path": "<root>", "message": str(e),
                                           "detail": str(e)}}

        rows = registry.candidates(manifest)
        return {
            "ok": True,
            "manifest": manifest.to_dict(),
            "fingerprint": manifest.fingerprint(),
            "approvals": {
                "declared": list(manifest.approval_required_when),
                "implied": list(manifest.implied_approvals()),
                "effective": list(manifest.effective_approvals()),
            },
            "required_capabilities": list(registry.required_capabilities(manifest)),
            "candidates": rows,
            "runnable": any(r["ok"] for r in rows),
            "why_not": registry.why_no_backend(manifest),
        }

    @router.post("/skill/plan")
    async def plan_skill(request: Request):
        """Where would this manifest run, and under what spec?

        Answers the question a coordinator asks before dispatching, and
        answers it without running anything, creating a scratch directory or
        touching a backend beyond asking whether it is up."""
        require_admin(request)
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Body must be JSON")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")

        try:
            manifest = SkillManifest.parse(payload.get("manifest", payload))
        except ContractError as e:
            return {"ok": False, "error": {"path": e.path, "message": e.message,
                                           "detail": str(e)}}

        from src import execution_router

        decision = execution_router.choose(
            manifest,
            workspace=str(payload.get("workspace") or ""),
            artifacts_root=str(payload.get("artifacts_root") or ""),
            run_id=str(payload.get("run_id") or "plan"),
            attended_ack=bool(payload.get("attended_ack") or False),
            prefer=(payload.get("prefer") or None),
            create_dirs=False,
        )
        return {"ok": True, "decision": decision.to_dict(),
                "skill": {"id": manifest.id, "version": manifest.version},
                "approvals": list(manifest.effective_approvals())}

    @router.get("/skills/audit")
    def skills_audit(request: Request, workspace: str = ""):
        """Which skills can describe themselves as capabilities, and which of
        those could actually run.

        Two questions, deliberately not collapsed. Almost every skill written
        before the bridge existed is *valid* and *not runnable*: it declares
        no permissions, so no backend may take it. That is the deny-by-default
        state rather than a fault, and an audit that painted both red would
        teach people to ignore it."""
        require_admin(request)
        from services.memory.skills import SkillsManager
        from src.constants import DATA_DIR
        from src.skills_runtime import bridge, discovery

        manager = SkillsManager(DATA_DIR)
        stored = [s for s in (manager._read_skill(p) for p in manager._iter_skill_files())
                  if s is not None]
        results = bridge.survey(stored)

        local, roots_reason = [], ""
        if workspace and os.path.isdir(workspace):
            roots, roots_reason = discovery.roots_for(workspace)
            local = [d.to_dict() for d in discovery.discover(workspace)]

        return {
            "checked_at": now_iso(),
            "stored": [r.to_dict() for r in results],
            "totals": {
                "skills": len(results),
                "valid_manifest": sum(1 for r in results if r.ok),
                "runnable_now": sum(1 for r in results if r.runnable),
            },
            "workspace_skills": local,
            "workspace_search": roots_reason,
            "note": "valid and not runnable is the deny-by-default state, not a fault: "
                    "a skill that declares no backend may not run anywhere",
        }

    @router.get("/schemas")
    def list_schemas(request: Request):
        """The eight spec v2 schemas this package gives code to, by name and
        version. A discovery step, the same one `/backends` already offers
        for execution targets: what exists, before fetching the shape of any
        one of them."""
        require_admin(request)
        rows = []
        for name in _SPEC_V2_SCHEMA_NAMES:
            schema_path = _SPEC_V2_SCHEMAS_DIR / f"{name}.schema.json"
            try:
                body = json.loads(schema_path.read_text(encoding="utf-8"))
            except OSError:
                logger.warning("spec v2 schema file missing on disk: %s", schema_path)
                continue
            except json.JSONDecodeError:
                logger.warning("spec v2 schema file is not valid JSON: %s", schema_path)
                continue
            rows.append({"name": name, "version": body.get("$id", "").rsplit(":", 1)[-1] or None,
                        "title": body.get("title", name)})
        return {"checked_at": now_iso(), "schemas": rows}

    @router.get("/schemas/{name}")
    def get_schema(name: str, request: Request):
        """The JSON Schema document itself, byte-identical to the file in
        `docs/spec/v2/schemas` — no baked-in copy to fall out of sync with
        it. A name outside the eight this package knows, or a file that
        went missing since `/schemas` listed it, is a clean 404 rather than
        a stack trace."""
        require_admin(request)
        if name not in _SPEC_V2_SCHEMA_NAMES:
            raise HTTPException(status_code=404, detail=f"No spec v2 schema named {name!r}")
        schema_path = _SPEC_V2_SCHEMAS_DIR / f"{name}.schema.json"
        try:
            raw = schema_path.read_text(encoding="utf-8")
        except OSError:
            raise HTTPException(
                status_code=404, detail=f"Schema file for {name!r} is missing on disk")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=500, detail=f"Schema file for {name!r} is not valid JSON: {e}")

    return router
