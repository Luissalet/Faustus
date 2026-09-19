"""routes/typed_choice_routes.py — HTTP surface for typed choice decisions
(src/typed_choice.py). Same auth style as the neighbouring doc-claims route
(`routes/doc_claims_routes.py`): `require_admin` on the endpoint.

POST /api/typed-choice
    {"question": str, "options": [str, ...], "context"?: str, "url"?: str,
     "model"?: str, "compare"?: bool}

With ``compare: true`` also runs the unconstrained-generation baseline
(``generated_choice``) so the two can be compared side by side.
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src import typed_choice

logger = logging.getLogger(__name__)


class TypedChoiceRequest(BaseModel):
    question: str
    options: list
    context: str = ""
    url: str | None = None
    model: str | None = None
    system: str | None = None
    timeout: float = 30
    compare: bool = False


def setup_typed_choice_routes():
    router = APIRouter(prefix="/api/typed-choice")

    @router.post("")
    async def post_typed_choice(request: Request, body: TypedChoiceRequest):
        require_admin(request)
        options = [str(o) for o in (body.options or []) if str(o).strip()]
        if not (body.question or "").strip() or not options:
            raise HTTPException(status_code=400, detail="'question' and a non-empty 'options' list are required")
        try:
            result = await typed_choice.typed_choice(
                body.question, options, context=body.context or "",
                url=body.url, model=body.model, system=body.system,
                timeout=body.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("typed_choice route failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

        if not body.compare:
            return result

        try:
            baseline = await typed_choice.generated_choice(
                body.question, options, context=body.context or "",
                url=body.url, model=body.model, system=body.system,
                timeout=body.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("typed_choice compare (generated_choice) failed: %s", exc)
            baseline = {"error": str(exc)}

        return {
            "typed_choice": result,
            "generated_choice": baseline,
            "agree": bool(result.get("choice") is not None
                          and result.get("choice") == baseline.get("choice")),
        }

    return router
