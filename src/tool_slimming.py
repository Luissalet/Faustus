"""Make tool schemas fit a small context window (FAUSTUS).

Second half of the roadmap's "agent prompt/context bloat": once the ledger
shows that tool schemas are a third of a 16k window, something has to give.

The obvious move — drop tools until they fit — is the wrong one: the tool the
model needed is exactly the one it can no longer see, and the turn fails in a
way nobody can debug. So this never removes a tool. It shortens *prose*:
long `description` strings (MCP servers routinely ship 500+ characters per
tool, with examples) and per-parameter descriptions, keeping every tool name,
every parameter name and the whole type structure intact. The model keeps the
same capability surface; it just stops reading an essay about each one.

Truncation is progressive — the widest limit that fits wins, so a window with
room keeps the full text. Schemas are deep-copied before editing because the
originals are module-level singletons shared by every request.
"""

import copy
import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Share of the model's context window tool schemas may occupy before slimming.
DEFAULT_SHARE = 0.15
# Widest first: the first limit that fits is the one used.
_LIMITS = (400, 240, 160, 120, 80)
# Windows above this are roomy enough that slimming buys nothing worth the risk.
ROOMY_CONTEXT = 32768
_ELLIPSIS = "…"


def _tokens(schemas: Iterable[Any]) -> int:
    try:
        blob = json.dumps(list(schemas), default=str)
    except Exception:
        blob = str(schemas)
    return int(len(blob) * 0.3) + 4


def _clip(text: Any, limit: int) -> Any:
    if not isinstance(text, str) or len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    # Prefer a sentence end so the description still reads like one.
    dot = cut.rfind(". ")
    if dot >= limit * 0.6:
        cut = cut[:dot + 1]
    return cut + _ELLIPSIS


def _slim_params(node: Any, limit: int) -> None:
    """Walk a JSON-Schema fragment, clipping every `description` in place."""
    if isinstance(node, dict):
        if isinstance(node.get("description"), str):
            node["description"] = _clip(node["description"], limit)
        for key, value in node.items():
            if key != "description":
                _slim_params(value, limit)
    elif isinstance(node, list):
        for item in node:
            _slim_params(item, limit)


def _apply(schemas: List[Any], limit: int) -> List[Any]:
    out = copy.deepcopy(schemas)
    param_limit = max(40, limit // 2)
    for schema in out:
        if not isinstance(schema, dict):
            continue
        fn = schema.get("function") if isinstance(schema.get("function"), dict) else schema
        if isinstance(fn.get("description"), str):
            fn["description"] = _clip(fn["description"], limit)
        params = fn.get("parameters")
        if params is not None:
            _slim_params(params, param_limit)
    return out


def slim_tool_schemas(schemas: Optional[List[Any]],
                      *,
                      context_length: int = 0,
                      share: float = DEFAULT_SHARE,
                      enabled: bool = True) -> Tuple[Optional[List[Any]], Dict[str, Any]]:
    """Return (schemas, report). Original list returned untouched when it fits.

    Only acts on genuinely small windows: with no known context length, or one
    above ROOMY_CONTEXT, the prompt is left exactly as the rest of the app built
    it — this is a rescue for 4k/8k/16k models, not a global style policy.
    """
    report: Dict[str, Any] = {"slimmed": False}
    if not schemas or not enabled:
        return schemas, report
    if not context_length or context_length > ROOMY_CONTEXT:
        return schemas, report

    before = _tokens(schemas)
    budget = int(context_length * share)
    report.update({"before": before, "budget": budget, "tools": len(schemas)})
    if before <= budget:
        return schemas, report

    for limit in _LIMITS:
        candidate = _apply(schemas, limit)
        after = _tokens(candidate)
        if after <= budget or limit == _LIMITS[-1]:
            report.update({"slimmed": True, "after": after, "limit": limit,
                           "fits": after <= budget,
                           "saved": max(before - after, 0)})
            logger.info("[tool-slim] %s tools %s -> %s tokens (limit=%s budget=%s)",
                        len(schemas), before, after, limit, budget)
            return candidate, report
    return schemas, report
