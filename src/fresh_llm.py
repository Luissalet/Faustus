"""src/fresh_llm.py -- one bare model call with no conversation behind it.

The loop and review workflows (`src.loop_arms`, `src.goldfish`) need many small,
independent model calls: a proposer that sees only the files it must change, a
reviewer that sees only the document and its reading list, a judge that sees only
two findings. None of them may inherit a chat: that is the whole point. This module
is the one door they use.

* The route is the swarm's (`src.swarm.runner.resolve_route`): an explicit
  endpoint id, the endpoint that serves a named model, or the task model.
* A call to a local server goes through the swarm lane, so it waits while the
  user's chat holds the model and never takes the chat's one-pipe lock.
* `local_models` lists the enabled chat models of the configured endpoints, for
  workflows that want *different* models (a blind panel).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300.0


class FreshLLMError(Exception):
    pass


def _route(owner: str, model: str = "", endpoint_id: str = "") -> Tuple[str, str, Optional[Dict[str, str]], str]:
    from src.swarm import runner
    spec: Dict[str, Any] = {"model": model or "", "endpoint_id": endpoint_id or ""}
    if model and not endpoint_id:
        try:
            from src.endpoint_resolver import endpoint_id_serving
            spec["endpoint_id"] = endpoint_id_serving(model, owner=owner or None) or ""
        except Exception:  # noqa: BLE001 - fall back to the task route with this model name
            logger.debug("fresh_llm: no endpoint found serving %s", model, exc_info=True)
    try:
        return runner.resolve_route(spec, owner)
    except Exception as exc:  # noqa: BLE001
        raise FreshLLMError(str(exc)) from exc


async def ask(owner: str, messages: List[Dict[str, Any]], *, model: str = "", endpoint_id: str = "",
              timeout: float = DEFAULT_TIMEOUT, max_tokens: int = 0, run_id: str = "fresh",
              parallel: int = 1, response_schema: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any]]:
    """(reply text, {model, endpoint, source}) for one stateless call."""
    from src.swarm import capacity, lane, runner
    url, resolved_model, headers, source = _route(owner, model, endpoint_id)
    try:
        cap = await capacity.effective_parallel(url)
        local = bool(cap.get("local"))
    except Exception:  # noqa: BLE001
        local = False
    ctx = lane.enter(url, run_id, max(1, parallel)) if local else contextlib.nullcontext()
    with ctx:
        text = await asyncio.wait_for(
            runner._llm_call(url, resolved_model, messages, headers=headers, timeout=timeout,
                             response_schema=response_schema, max_tokens=max_tokens),
            timeout=timeout + 30)
    text = text if isinstance(text, str) else str(text or "")
    return strip_thinking(text).strip(), {"model": resolved_model, "endpoint": url, "source": source}


def strip_thinking(text: str) -> str:
    """Drop a `<think>...</think>` preamble some local models print before the answer."""
    return re.sub(r"(?is)^\s*<think>.*?</think>\s*", "", text or "")


def local_models(owner: str = "", *, include_remote: bool = False) -> List[Dict[str, str]]:
    """Enabled chat models of the configured endpoints: [{endpoint_id, model, name}].
    Subscription CLIs (faustus-cli://) and, unless asked, remote APIs are left out."""
    out: List[Dict[str, str]] = []
    try:
        from core.database import ModelEndpoint, get_db_session
        from src.endpoint_resolver import _endpoint_enabled_models
        with get_db_session() as db:
            rows = db.query(ModelEndpoint).all()
            for ep in rows:
                if not getattr(ep, "is_enabled", True) or (getattr(ep, "model_type", None) or "llm") != "llm":
                    continue
                owner_of = getattr(ep, "owner", None)
                if owner_of and owner and str(owner_of) != str(owner):
                    continue
                base = str(ep.base_url or "")
                if not base.startswith(("http://", "https://")):
                    continue
                kind = str(getattr(ep, "endpoint_kind", "") or "auto")
                if not include_remote and kind in ("api", "proxy"):
                    continue
                for m in _endpoint_enabled_models(ep):
                    out.append({"endpoint_id": str(ep.id), "model": str(m), "name": str(ep.name or "")})
    except Exception:  # noqa: BLE001 - no database (tests, MCP without app): no list
        logger.debug("fresh_llm: could not list endpoints", exc_info=True)
    return out


def parse_json(text: str) -> Any:
    """The first JSON object or array in a reply (fenced or bare), or None."""
    if not text:
        return None
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    candidates = [fence.group(1)] if fence else []
    candidates.append(body)
    for cand in candidates:
        cand = cand.strip()
        try:
            return json.loads(cand)
        except ValueError:
            pass
        for opener, closer in (("[", "]"), ("{", "}")):
            start, end = cand.find(opener), cand.rfind(closer)
            if 0 <= start < end:
                try:
                    return json.loads(cand[start:end + 1])
                except ValueError:
                    continue
    return None
