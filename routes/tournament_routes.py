"""Tournament API — /api/tournament/* (src/tournament.py).

The same prompt to N models, blind and in parallel, then rounds where every
model sees all the previous answers anonymised and is told to weave the
complementary parts into a hybrid; then a judged, ranked table. The
side-by-side comparator (/api/compare) is unchanged — this is the other half
of that idea, not a replacement for it.

  POST /api/tournament                   {prompt, models: [...], rounds?,
                                          judge_model?, seed?} → the run (started
                                          in the background, same pattern as
                                          POST /api/dispatch)
  GET  /api/tournament                   recent runs (no result bodies)
  GET  /api/tournament/config            what a run would use right now
  GET  /api/tournament/{id}              status + result (+ progress while running)
  GET  /api/tournament/{id}/wait?timeout= long-poll, then the same as GET
  GET  /api/tournament/{id}/events       the events so far; `?stream=1` for SSE
  POST /api/tournament/{id}/cancel

Admin-only: a run spends every GPU on the box for as long as it takes, and the
models it names are the owner's own endpoints.

Robot mode (src/robot_envelope.py): `GET /api/tournament/{id}` and
`/{id}/events` also take `?robot=1` (the standard envelope, JSON) or
`?format=toon` (the same envelope as compact TOON text), carrying a LEAN
projection — the answers and the ranked finals are exactly the uniform-rows
case TOON exists for, so they project to one header plus a line per row
instead of a key set per line. Without a query parameter the answers are
exactly what they always were. The projections live here rather than in
src/robot_projection.py so this feature owns its own shape; they follow the
same rules (uniform all-scalar rows, one-line text cells, nothing raises).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.middleware import require_admin
from src import robot_envelope as robot
from src import tournament
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)

_MAX_WAIT_S = 900.0
_STREAM_MAX_S = 900.0
_STREAM_TICK_S = 0.5
_TEXT_CELL = 400


# ── the lean projections (robot mode) ───────────────────────────────────────

def _one_line(value: Any, limit: int = _TEXT_CELL) -> str:
    s = " ".join(str(value if value is not None else "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _scalar(value: Any) -> Any:
    return value if isinstance(value, (int, float, bool)) or value is None else _one_line(value)


def lean_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    """A run as two tables — the answers and the ranked finals — plus scalars.

    Dropped: the prompt the coordinator itself sent (its length is the fact
    worth keeping), the per-model convergence blocks behind the one score, the
    judge's raw answer, and the events (they have their own endpoint).
    """
    if not isinstance(payload, dict):
        return payload
    try:
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        judge = result.get("judge") if isinstance(result.get("judge"), dict) else None
        out: Dict[str, Any] = {
            "id": _one_line(payload.get("id"), 60),
            "status": _one_line(payload.get("status"), 40),
            "error": _one_line(payload.get("error")),
            "models": _one_line(", ".join(str(m) for m in payload.get("models") or [])),
            "rounds": _scalar(payload.get("rounds")),
            "rounds_run": _scalar(result.get("rounds_run")),
            "stopped_by": _one_line(result.get("stopped_by"), 40),
            "duration_s": _scalar(payload.get("duration_s")),
            "prompt_chars": len(str(payload.get("prompt") or "")),
            "ranking": _one_line(result.get("ranking"), 40),
            "ranking_note": _one_line(result.get("ranking_note")),
            "degraded": bool(result.get("degraded")),
            "final": [_final_row(r) for r in result.get("final") or []],
            "answers": [_answer_row(r) for r in result.get("answers") or []],
        }
        conv = result.get("convergence")
        if isinstance(conv, dict):
            out["convergence"] = {"score": _scalar(conv.get("score")),
                                  "converged": bool(conv.get("converged")),
                                  "models_assessed": _scalar(conv.get("models_assessed")),
                                  "reason": _one_line(conv.get("reason"))}
        if judge is not None:
            out["judge"] = {"model": _one_line(judge.get("model"), 80),
                            "ok": bool(judge.get("ok")), "attempts": _scalar(judge.get("attempts")),
                            "error": _one_line(judge.get("error"))}
        for key in ("errors", "cancelled"):
            rows = result.get(key) or []
            if rows:
                out[key] = [{"model": _one_line((r or {}).get("model"), 80),
                             "round": _scalar((r or {}).get("round")),
                             "detail": _one_line((r or {}).get("error") or (r or {}).get("reason"))}
                            for r in rows if isinstance(r, dict)]
        progress = payload.get("progress")
        if isinstance(progress, list) and progress:
            out["progress"] = [{"model": _one_line((p or {}).get("model"), 80),
                                "round": _scalar((p or {}).get("round")),
                                "state": _one_line((p or {}).get("state"), 40),
                                "chars": _scalar((p or {}).get("chars"))}
                               for p in progress if isinstance(p, dict)]
            out["wait_again"] = True
        return out
    except Exception as e:  # noqa: BLE001 - robot mode may lose the compaction, never the read
        logger.debug("tournament: lean projection failed: %s", e)
        return payload


def _final_row(row: Any) -> Dict[str, Any]:
    r = row if isinstance(row, dict) else {}
    scores = r.get("scores") if isinstance(r.get("scores"), dict) else {}
    return {"rank": _scalar(r.get("rank")), "model": _one_line(r.get("model"), 80),
            "entry": _scalar(r.get("entry")), "round": _scalar(r.get("round")),
            "correctness": _scalar(scores.get("correctness")),
            "completeness": _scalar(scores.get("completeness")),
            "sophistication": _scalar(scores.get("sophistication")),
            "total": _scalar(r.get("total")), "tiebreak": _scalar(r.get("tiebreak")),
            "outcome": _one_line(r.get("outcome"), 40),
            "chars": len(str(r.get("text") or "")),
            "text": _one_line(r.get("text"))}


def _answer_row(row: Any) -> Dict[str, Any]:
    r = row if isinstance(row, dict) else {}
    return {"round": _scalar(r.get("round")), "model": _one_line(r.get("model"), 80),
            "entry": _scalar(r.get("entry")), "chars": len(str(r.get("text") or "")),
            "elapsed_s": _scalar(r.get("elapsed_s")), "tokens": _scalar(r.get("tokens")),
            "tokens_source": _one_line(r.get("tokens_source"), 20)}


def lean_events(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The events as uniform rows: one header instead of a key set per line."""
    if not isinstance(payload, dict):
        return payload
    try:
        return {
            "id": _one_line(payload.get("id"), 60),
            "status": _one_line(payload.get("status"), 40),
            "events": [{"ts": _scalar((e or {}).get("ts")),
                        "event": _one_line((e or {}).get("event"), 40),
                        "model": _one_line((e or {}).get("model"), 80),
                        "round": _scalar((e or {}).get("round")),
                        "detail": _one_line((e or {}).get("error") or (e or {}).get("reason")
                                            or (e or {}).get("ranking") or (e or {}).get("score"))}
                       for e in payload.get("events") or [] if isinstance(e, dict)],
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("tournament: lean events projection failed: %s", e)
        return payload


# ── the routes ──────────────────────────────────────────────────────────────

def _owner(request: Request) -> str:
    try:
        return str(effective_user(request) or "")
    except Exception:  # noqa: BLE001 - attribution must not 500 the route
        return ""


async def _body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "a JSON body is required")
    if not isinstance(body, dict):
        raise HTTPException(400, "the body must be a JSON object")
    return body


def setup_tournament_routes() -> APIRouter:
    router = APIRouter(prefix="/api/tournament", tags=["tournament"])

    def _get(request: Request, run_id: str) -> tournament.TournamentRun:
        owner = _owner(request)
        run_obj = tournament.get(run_id)
        if run_obj is None or not tournament.visible_to(run_obj, owner or None):
            raise HTTPException(404, "no such tournament")
        return run_obj

    @router.post("")
    async def create(request: Request, _admin: None = Depends(require_admin)):
        if not tournament.enabled():
            raise HTTPException(400, "the tournament is switched off "
                                     "(Settings → Agent & automation → Tournament)")
        body = await _body(request)
        try:
            run_obj = await tournament.start(_owner(request) or None, body)
        except tournament.TournamentError as e:
            raise HTTPException(400, str(e))
        return tournament.summary(run_obj)

    @router.get("")
    async def index(request: Request, limit: int = 50,
                    _admin: None = Depends(require_admin)):
        try:
            n = max(1, min(int(limit or 50), 200))
        except (TypeError, ValueError):
            n = 50
        return {"runs": tournament.list_runs(_owner(request) or None, limit=n),
                "enabled": tournament.enabled(),
                "max_models": tournament.max_models(),
                "max_rounds": tournament.MAX_ROUNDS,
                "min_models": tournament.MIN_MODELS}

    @router.get("/config")
    async def config(_admin: None = Depends(require_admin)):
        """What a run would use right now — so the page can say it before Run."""
        return {"enabled": tournament.enabled(), "max_models": tournament.max_models(),
                "min_models": tournament.MIN_MODELS, "max_rounds": tournament.MAX_ROUNDS,
                "default_rounds": tournament.DEFAULT_ROUNDS, "axes": list(tournament.AXES),
                "fusion_instruction": tournament.FUSION_INSTRUCTION}

    @router.get("/{run_id}")
    async def status(request: Request, run_id: str,
                     _admin: None = Depends(require_admin)):
        if robot.wants(request):
            return await robot.reply(
                request, lambda: lean_status(tournament.summary(_get(request, run_id))))
        return tournament.summary(_get(request, run_id))

    @router.get("/{run_id}/wait")
    async def wait(request: Request, run_id: str, timeout: float = 60.0,
                   _admin: None = Depends(require_admin)):
        run_obj = _get(request, run_id)
        try:
            t = float(timeout)
        except (TypeError, ValueError):
            t = 60.0
        await tournament.wait(run_obj, max(0.0, min(t, _MAX_WAIT_S)))
        return tournament.summary(run_obj)

    @router.get("/{run_id}/events")
    async def events(request: Request, run_id: str, stream: int = 0,
                     _admin: None = Depends(require_admin)):
        run_obj = _get(request, run_id)

        def payload() -> Dict[str, Any]:
            return {"id": run_obj.id, "status": run_obj.status,
                    "events": list(run_obj.events)}
        if stream:
            return StreamingResponse(_event_stream(run_obj), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no"})
        if robot.wants(request):
            return await robot.reply(request, lambda: lean_events(payload()))
        return payload()

    @router.post("/{run_id}/cancel")
    async def cancel(request: Request, run_id: str,
                     _admin: None = Depends(require_admin)):
        run_obj = _get(request, run_id)
        return {"id": run_obj.id, "cancelled": tournament.cancel(run_obj),
                "status": run_obj.status}

    return router


async def _event_stream(run_obj: "tournament.TournamentRun"):
    """`?stream=1`: the events already recorded, then the new ones as they
    land, then one `end` frame. Bounded in time so a forgotten tab cannot
    hold a worker forever; the polling shape (no `stream`) says the same thing.

    Frame naming matches `/api/dispatch/{id}/events?stream=1`, and it matters:
    an event with a `event: <name>` line does NOT reach `EventSource.onmessage`,
    only a listener registered for that exact name. Two SSE endpoints in one app
    disagreeing about that is a trap — a page written against one silently
    receives nothing from the other. So progress frames are unnamed (they arrive
    on `onmessage`) and only the terminal frame is named `end`, exactly as
    dispatch does it.
    """
    started = time.monotonic()
    sent = 0
    try:
        while True:
            events = list(run_obj.events)
            for ev in events[sent:]:
                yield _sse("", ev)
            sent = len(events)
            if run_obj.status not in ("queued", "running", "judging", "cancelling"):
                yield _sse("end", {"id": run_obj.id, "status": run_obj.status})
                return
            if time.monotonic() - started > _STREAM_MAX_S:
                yield _sse("end", {"id": run_obj.id, "status": run_obj.status,
                                   "timeout": True})
                return
            await asyncio.sleep(_STREAM_TICK_S)
    except asyncio.CancelledError:      # the browser went away
        raise
    except Exception as e:  # noqa: BLE001 - a stream never 500s a finished run
        logger.debug("tournament: event stream ended: %s", e)
        yield _sse("end", {"id": getattr(run_obj, "id", ""), "status": "error"})


def _sse(name: str, data: Any) -> str:
    """One SSE frame. An empty `name` means an unnamed frame, which is the one
    shape `EventSource.onmessage` receives; a name is only for the terminal
    `end` frame a listener opts into."""
    try:
        body = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        body = "{}"
    head = f"event: {name}\n" if name else ""
    return f"{head}data: {body}\n\n"
