"""
workflows/model_calls.py — how the model-using nodes reach a model.

The nodes (`classify`, `extract`, `guard`, and the repair step of `agent`) never
name a URL. They ask for a *purpose* — `utility` by default, the same helper
model the brain extractor and the typed decisions use — and
`endpoint_resolver.resolve_endpoint` answers with whatever the owner configured
(falling back to the default chat model). So a workflow runs on the local
llama.cpp model with no extra setup, and follows the owner if they move it.

Two calls, because the two jobs are different:

* :func:`complete_text` — a normal completion, for extraction and repair.
* :func:`decide` — `src.typed_decision.decide_sync`: one token, read as a
  probability over the allowed answers. This is what `classify` uses, because
  a label plus a confidence is a better thing to route on than prose.

`ModelCalls` bundles the two so `default_handlers(models=...)` takes one
argument and a test can hand in a fake without patching module globals. With
nothing wired the nodes refuse by name, like `deliver` with no sender: a
workflow that "classified" with no model behind it would route every ticket
down the first branch and say it was sure.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = ["ModelCalls", "ModelUnavailable", "complete_text", "decide", "production",
           "run_coroutine"]

DEFAULT_PURPOSE = "utility"


class ModelUnavailable(RuntimeError):
    """No model could be reached for this node — a configuration or runtime
    fact, reported as the node's failure reason."""


@dataclass(frozen=True)
class ModelCalls:
    """`complete(messages, **opts) -> str` and
    `decide(context, fields, **opts) -> {field: Decision}`."""

    complete: Callable[..., str]
    decide: Callable[..., Mapping[str, Any]]


def run_coroutine(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine to completion from sync code, whether or not this
    thread already has a running event loop (a route handler on the loop
    thread does; the scheduler's worker thread does not)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: Dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=runner, name="workflow-model-call", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def complete_text(messages: List[Dict[str, str]], *, owner: str = "",
                  purpose: str = DEFAULT_PURPOSE, timeout_s: float = 60.0,
                  max_tokens: int = 1200, temperature: float = 0.0) -> str:
    """One non-streaming completion on the model resolved for `purpose`."""
    from src.endpoint_resolver import resolve_endpoint
    try:
        url, model, headers = resolve_endpoint(purpose or DEFAULT_PURPOSE, owner=owner or None)
    except Exception as exc:  # noqa: BLE001
        raise ModelUnavailable(f"could not resolve a model endpoint ({type(exc).__name__}: {exc})")
    if not url or not model:
        raise ModelUnavailable(
            f"no model endpoint is configured for {purpose or DEFAULT_PURPOSE!r}; set the "
            "utility or default chat model in Settings")

    # Advanced from a request (Studio's Run), a user is waiting: a background
    # call would queue behind that request. Read here, before run_coroutine
    # hands the call to another loop.
    from src.interactive_gate import workload_for
    workload = workload_for("background")

    async def call() -> str:
        from src.llm_core import llm_call_async
        raw = await llm_call_async(
            url=url, model=model, messages=messages, headers=headers,
            temperature=temperature, max_tokens=max_tokens,
            timeout=max(1, int(timeout_s)), max_retries=1, workload=workload,
            _spend_purpose="workflow_model")
        return raw[0] if isinstance(raw, tuple) else raw

    try:
        return str(run_coroutine(asyncio.wait_for(call(), timeout=max(1.0, float(timeout_s)))) or "")
    except asyncio.TimeoutError:
        raise ModelUnavailable(f"the model did not answer within {timeout_s:g}s")


def decide(context: str, fields: Sequence[Any], **kwargs: Any) -> Mapping[str, Any]:
    from src import typed_decision
    return typed_decision.decide_sync(context, fields, **kwargs)


def production() -> ModelCalls:
    return ModelCalls(complete=complete_text, decide=decide)
