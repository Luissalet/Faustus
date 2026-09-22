"""Fill a design canvas with one tool-less completion, and file it.

Same shape as the other passes that decode under a schema
(`src/auto_review.py`, `src/doubt_review.py`, `src/research_review.py`): one
completion, no tools -- which is also what lets the schema travel, since a
grammar and a tool-call decoder compete for the same output.

The difference is what happens to the answer. A review is read once and thrown
away; a canvas is stored as a concept in the project graph, so the next session
can ask `concepts_understand` what a subsystem was meant to do and get the
design back, with its rejected alternative and its safeguards, instead of
re-deriving it from the code.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from src import design_canvas
from src.design_canvas import DesignCanvasError

logger = logging.getLogger(__name__)

#: A canvas is longer than a review verdict and it is written once per feature,
#: not once per turn, so it can afford room to be specific.
MAX_TOKENS = 1600
DEFAULT_TIMEOUT_S = 240.0


def _setting(key: str, default: Any = None) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


async def _default_model_call(goal: str, context: str, *, owner: Optional[str],
                              model: Optional[str], timeout_s: float) -> str:
    """Resolve a model the same way the sibling passes do and ask once."""
    from src.ai_interaction import _resolve_model
    from src.llm_core import llm_call_async
    spec = (model or str(_setting("agent_design_canvas_model", "auto") or "auto")).strip() or "auto"
    url, resolved_model, headers = await asyncio.to_thread(_resolve_model, spec, owner=owner)
    raw = await llm_call_async(
        url, resolved_model, design_canvas.build_prompt(goal, context=context),
        headers=headers, temperature=0.2, max_tokens=MAX_TOKENS,
        timeout=int(timeout_s), max_retries=1, workload="foreground",
        response_schema=design_canvas.canvas_schema(),
    )
    return raw[0] if isinstance(raw, tuple) else raw


async def draft(goal: str, *, context: str = "", owner: Optional[str] = None,
                model: Optional[str] = None, timeout_s: Optional[float] = None,
                model_call: Optional[Callable[..., Awaitable[str]]] = None,
                ) -> Dict[str, Any]:
    """One canvas pass. Returns `{canvas, markdown, paths, elapsed_ms}`.

    Raises `DesignCanvasError` when no usable canvas came back. Unlike a
    review, this one does NOT fail open: a review that fails open costs a
    missed comment, whereas an empty canvas stored as a design would be a lie
    on record that the next session reads as settled.

    `model_call` is an injection seam for tests, as in the sibling passes.
    """
    started = time.monotonic()
    call = model_call or _default_model_call
    limit = float(timeout_s or _setting("agent_design_canvas_timeout_s", DEFAULT_TIMEOUT_S)
                  or DEFAULT_TIMEOUT_S)
    try:
        raw = await call(goal, context, owner=owner, model=model, timeout_s=limit)
    except DesignCanvasError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DesignCanvasError(f"the model call failed: {exc}") from exc

    canvas = design_canvas.parse_response(raw)
    return {
        "canvas": canvas,
        "markdown": design_canvas.render(canvas),
        "paths": design_canvas.referenced_paths(canvas),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def file_as_concept(canvas: Dict[str, Any], goal: str, *, name: str,
                    project_id: Optional[str] = None,
                    workspace: Optional[str] = None,
                    concept_id: Optional[str] = None) -> Dict[str, Any]:
    """Store a validated canvas in the project graph as a `decision`.

    The paths from `structure` become the concept's refs, which is what makes
    the design answer for itself later: once one of them stops existing,
    `stale_check` reports the concept as obsolete with nobody maintaining it.
    """
    from src.project_concepts import _store

    canvas = design_canvas.validate(canvas)
    store = _store(project_id=project_id, workspace=workspace)
    return store.upsert_concept(
        name=name,
        kind="decision",
        summary=design_canvas.summarise(canvas, goal),
        details=design_canvas.render(canvas),
        refs=design_canvas.referenced_paths(canvas),
        concept_id=concept_id,
    )
