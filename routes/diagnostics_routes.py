"""Diagnostics routes — /api/db/stats, /api/rag/stats, /api/test/youtube, /api/test-research."""

import asyncio
import logging
import os
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException, Form, Request

from services.youtube.youtube_handler import extract_youtube_id, extract_transcript_async
from core.constants import DEFAULT_HOST, DATA_DIR
from core.middleware import require_admin

logger = logging.getLogger(__name__)


def setup_diagnostics_routes(
    rag_manager,
    rag_available: bool,
    research_handler,
    memory_vector=None,
) -> APIRouter:
    router = APIRouter(tags=["diagnostics"])

    @router.get("/api/diagnostics/services")
    async def get_service_health(request: Request) -> Dict[str, Any]:
        """Consolidated degraded-state report for ChromaDB, SearXNG, email,
        ntfy, and provider endpoints. Non-intrusive probes — safe to poll."""
        require_admin(request)
        from src.service_health import collect_service_health
        from src.service_hints import attach_hints
        return attach_hints(await collect_service_health(rag_manager, memory_vector))

    @router.post("/api/diagnostics/services/reconnect")
    async def reconnect_services(request: Request) -> Dict[str, Any]:
        """Re-establish the ChromaDB-backed stores, then re-probe (FAUSTUS).

        This is the panel's Reconnect button. It recovers the "Docker Desktop
        was closed, so RAG quietly went keyword-only" case in place, without a
        restart. Admin-only, idempotent, and safe to press twice.
        """
        require_admin(request)
        from src.service_health import collect_service_health
        from src.service_hints import attach_hints
        from src.service_recovery import reconnect_vector_stores
        recovery = await asyncio.to_thread(
            reconnect_vector_stores, rag_manager, memory_vector)
        report = attach_hints(
            await collect_service_health(rag_manager, memory_vector))
        report["recovery"] = recovery
        return report

    @router.get("/api/diagnostics/logs")
    async def get_diagnostics_logs(request: Request, limit: int = 200) -> Dict[str, Any]:
        require_admin(request)
        limit = max(1, min(limit, 1000))
        try:
            log_file = os.path.join(DATA_DIR, "logs", "app.log")
            if not os.path.exists(log_file):
                return {"status": "success", "logs": []}

            # Safe tail read of the log file (max 5MB via rotation)
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            tail_lines = lines[-limit:] if len(lines) > limit else lines
            tail_lines = [line.rstrip('\r\n') for line in tail_lines]

            return {
                "status": "success",
                "logs": tail_lines
            }
        except Exception as e:
            logger.error(f"Diagnostics logs retrieval error: {e}")
            raise HTTPException(500, f"Failed to retrieve logs: {str(e)}")

    @router.get("/api/db/stats")
    async def get_database_stats(request: Request) -> Dict[str, Any]:
        require_admin(request)
        try:
            from core.database import get_detailed_stats
            return get_detailed_stats()
        except Exception as e:
            logger.error(f"DB stats error: {e}")
            raise HTTPException(500, "Failed to retrieve database statistics")

    @router.get("/api/rag/stats")
    async def get_rag_stats(request: Request) -> Dict[str, Any]:
        require_admin(request)
        if rag_available and rag_manager:
            return rag_manager.get_stats()
        return {"error": "RAG system not available"}

    @router.get("/api/test/youtube")
    async def test_youtube(request: Request, url: str) -> Dict[str, Any]:
        require_admin(request)
        try:
            video_id = extract_youtube_id(url)
            if not video_id:
                return {"error": "Invalid YouTube URL"}

            data = await extract_transcript_async(url, video_id)
            return {
                "video_id": video_id,
                "transcript_success": data.get("success", False),
                "transcript_length": len(data.get("transcript", "")) if data.get("success") else 0,
                "transcript_preview": (data.get("transcript", "")[:500] + "...")
                    if data.get("success") and len(data.get("transcript", "")) > 500
                    else data.get("transcript", ""),
                "error": data.get("error") if not data.get("success") else None,
            }
        except Exception as e:
            return {"error": str(e)}

    @router.post("/api/test-research")
    async def test_research(request: Request, query: str = Form("What is machine learning?")) -> Dict[str, Any]:
        require_admin(request)
        try:
            endpoint = f"http://{DEFAULT_HOST}:8000/v1/chat/completions"
            model = "gpt-oss-120b"
            result = await research_handler.call_research_service(query, endpoint, model)
            return {
                "status": "success",
                "query": query,
                "result_preview": result[:200] + "..." if len(result) > 200 else result,
                "result_length": len(result),
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "query": query}

    # ── doctor repair (BASE-03 / OPS-01) ─────────────────────────────────
    # `GET /api/doctor` itself already lives in routes/changesets_routes.py
    # and calls src.doctor.run() — every check added there (see src/doctor.py)
    # flows through that existing endpoint automatically. This is only the
    # write side: running one of the narrow, allowlisted repairs a Finding
    # can name in `facts.repair`.

    @router.post("/api/doctor/repair")
    async def doctor_repair(request: Request) -> Dict[str, Any]:
        require_admin(request)
        import asyncio

        from src import doctor
        body = await request.json()
        name = str(body.get("repair") or "")
        if name not in doctor.REPAIRS:
            raise HTTPException(400, f"no such repair {name!r}; available: {sorted(doctor.REPAIRS)}")
        return await asyncio.to_thread(doctor.repair, name)

    # ── first run (SET-01) ───────────────────────────────────────────────

    @router.get("/api/setup/status")
    async def setup_status(request: Request) -> Dict[str, Any]:
        """One recommendation, never a screen with nothing useful on it.

        Admin-only like the rest of Diagnostics: the checks underneath (auth
        config, model endpoints) are not for a signed-out visitor to probe.
        """
        require_admin(request)
        from src import doctor
        return doctor.next_setup_action()

    # ── safe mode (OPS-05 / QA-46) ───────────────────────────────────────

    @router.get("/api/safe-mode/status")
    async def safe_mode_status(request: Request) -> Dict[str, Any]:
        require_admin(request)
        from src import safe_mode
        return safe_mode.status()

    @router.post("/api/safe-mode/reactivate")
    async def safe_mode_reactivate(request: Request) -> Dict[str, Any]:
        """Turn ONE held-back subsystem, or ONE quarantined MCP server, back
        on — never all of them at once. Body: {"subsystem": "..."} or
        {"mcp_server_id": "..."}."""
        require_admin(request)
        from src import safe_mode
        body = await request.json()
        subsystem = body.get("subsystem")
        server_id = body.get("mcp_server_id")
        if subsystem:
            try:
                return safe_mode.reactivate_subsystem(str(subsystem))
            except ValueError as e:
                raise HTTPException(400, str(e))
        if server_id:
            return safe_mode.reactivate_mcp_server(str(server_id))
        raise HTTPException(400, "pass either 'subsystem' or 'mcp_server_id'")

    # ── provider/model change, consciously (SET-05) ─────────────────────

    @router.get("/api/setup/provider-change-preview")
    async def provider_change_preview(request: Request, from_endpoint_id: str = "",
                                      to_endpoint_id: str = "") -> Dict[str, Any]:
        """What changes if the default provider/model moves from one
        endpoint to another: privacy (local vs cloud — never guessed, read
        from `ModelEndpoint.endpoint_kind`), whether a cost is even possible
        (an API key is configured at all), and previously-measured
        capabilities when they exist. Nothing here is invented: an endpoint
        this process has never probed reports its capabilities as
        `not_probed`, the same "unknown, not assumed ok" rule `src.doctor`
        uses everywhere else.
        """
        require_admin(request)
        from core.database import ModelEndpoint, SessionLocal

        def describe(endpoint_id: str) -> Optional[Dict[str, Any]]:
            if not endpoint_id:
                return None
            db = SessionLocal()
            try:
                ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
            finally:
                db.close()
            if ep is None:
                return None
            kind = ep.endpoint_kind or "auto"
            is_local = kind == "local" or "localhost" in (ep.base_url or "") or "127.0.0.1" in (ep.base_url or "")
            capabilities = "not_probed"
            try:
                from src import model_calibration as mcal
                key = mcal.manifest_key(vendor="unknown", model_id="", endpoint_id=endpoint_id)
                manifest = mcal.get_manifest(key)
                if manifest.get("announced") or manifest.get("tested"):
                    capabilities = "probed"
            except Exception:
                pass
            return {
                "id": ep.id, "name": ep.name, "base_url": ep.base_url,
                "endpoint_kind": kind,
                "privacy": "local — requests never leave this machine" if is_local
                          else f"cloud — requests leave this machine to {ep.base_url}",
                "has_api_key": bool(ep.api_key),
                "cost": ("no per-token cost" if is_local else
                         "billed by the provider" if ep.api_key else
                         "cloud endpoint with no API key stored — requests will likely fail"),
                "capabilities": capabilities,
            }

        before = describe(from_endpoint_id)
        after = describe(to_endpoint_id)
        changes: List[str] = []
        if before and after:
            if before["endpoint_kind"] != after["endpoint_kind"] or (before["privacy"] != after["privacy"]):
                changes.append(f"privacy: {before['privacy']} → {after['privacy']}")
            if before["cost"] != after["cost"]:
                changes.append(f"cost: {before['cost']} → {after['cost']}")
        return {"ok": before is not None and after is not None, "before": before, "after": after,
                "changes": changes, "requires_confirmation": True}

    return router
