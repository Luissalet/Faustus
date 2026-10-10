"""routes/cookbook_logs_routes.py - read the command, exit code and output of a
Cookbook job (see src/cookbook_job_logs.py). Admin only, read only.

  GET /api/cookbook/jobs                       recent jobs (no output)
  GET /api/cookbook/jobs/{id}/log              ?stream=combined|stdout|stderr &tail=N [&format=text]
  GET /api/cookbook/jobs/{id}/download         full log of one stream as a text attachment
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from core.middleware import require_admin
from src import cookbook_job_logs as jl


def _error(status: int, message: str, error_class: str) -> JSONResponse:
    return JSONResponse({"error": message, "error_class": error_class}, status_code=status)


def setup_cookbook_logs_routes() -> APIRouter:
    router = APIRouter(tags=["cookbook"])

    @router.get("/api/cookbook/jobs")
    async def list_jobs(request: Request, limit: int = 50):
        require_admin(request)
        return {"jobs": jl.list_jobs(limit)}

    @router.get("/api/cookbook/jobs/{job_id}/log")
    async def job_log(request: Request, job_id: str, stream: str = "combined", tail: int = jl.DEFAULT_TAIL,
                      format: str = "json"):
        require_admin(request)
        try:
            if format == "text":
                return PlainTextResponse(jl.render_text(job_id, stream, tail))
            data = jl.read_log(job_id, stream, tail)
        except jl.JobLogError as exc:
            return _error(400, str(exc), "cookbook.bad_request")
        except KeyError:
            return _error(404, f"no log recorded for job {job_id!r}", "cookbook.job_not_found")
        data["header"] = jl.render_header(jl.get_meta(job_id) or {})
        return data

    @router.get("/api/cookbook/jobs/{job_id}/download")
    async def job_download(request: Request, job_id: str, stream: str = "combined"):
        require_admin(request)
        try:
            path = jl.download_path(job_id, stream)
        except jl.JobLogError as exc:
            return _error(400, str(exc), "cookbook.bad_request")
        except KeyError:
            return _error(404, f"no log recorded for job {job_id!r}", "cookbook.job_not_found")
        if path is None:
            return _error(404, f"stream {stream!r} is not available for job {job_id!r}", "cookbook.stream_unavailable")
        header = jl.render_header(jl.get_meta(job_id) or {}) + f"\nstream: {stream}\n" + "-" * 40 + "\n"

        def body():
            yield jl.redact_text(header).encode("utf-8")
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 16)
                    if not chunk:
                        break
                    yield chunk

        fname = f"{job_id}-{stream}.log"
        return StreamingResponse(body(), media_type="text/plain; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    return router
