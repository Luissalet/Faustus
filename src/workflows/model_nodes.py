"""
workflows/model_nodes.py — the node types that use a model.

`agent`, `classify`, `extract` and `guard` are where a workflow stops being
plumbing and starts deciding things, so every rule in `handlers.py` applies here
harder, not softer:

**Nothing is capable by default.** Each handler is built from a seam — an agent
runner, a `ModelCalls` — and with nothing wired it refuses by name, like
`deliver` with no sender. A `classify` that "chose" a branch with no model
behind it would send every ticket down the first edge and record that it was
sure.

**A model's answer is never trusted as a shape.** Structured output is parsed,
checked against the node's schema, and — once — sent back to the model with the
exact errors (`repair`). After that it is a failed node, with the errors in the
reason, not a result that looks complete and is not.

**A decision carries its receipt.** `classify` records the options it chose
between, the choice, the confidence, whether the fallback fired and which model
answered. A routing decision nobody can audit is the one that gets blamed.

**Effects are claimed the way `deliver` claims them.** An `agent` turn may call
tools, so it runs behind the same idempotency key and `mark_effect` protocol a
`skill` does: the turn is marked pending before it starts and confirmed when it
returns; a worker that dies in between leaves `unknown_effect`, never a silent
second turn. `classify`, `extract` and `guard` only read, so a retry is merely
another model call.
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.contracts.workflow import WorkflowNode

from . import schema_check
from .handlers import _check_fenced
from .model_calls import ModelCalls, ModelUnavailable
from .templating import TemplateError, render

logger = logging.getLogger(__name__)

__all__ = ["agent_handler", "classify_handler", "extract_handler", "guard_handler",
           "model_node_handlers"]

_MAX_PROMPT_CHARS = 100_000


def _failed(reason: str, **extra: Any) -> Dict[str, Any]:
    return {"status": "failed", "reason": reason, **extra}


def _number(config: Mapping[str, Any], key: str, default: float, low: float, high: float) -> Tuple[float, str]:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default, f"config.{key} must be a number"
    if value < low or value > high:
        return default, f"config.{key} must be between {low:g} and {high:g}"
    return float(value), ""


def _whole_number(config: Mapping[str, Any], key: str, default: int, low: int, high: int) -> Tuple[int, str]:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default, f"config.{key} must be a whole number"
    if value < low or value > high:
        return default, f"config.{key} must be between {low} and {high}"
    return value, ""


def _text_config(config: Mapping[str, Any], key: str, *, required: bool = True,
                 limit: int = _MAX_PROMPT_CHARS) -> Tuple[str, str]:
    value = config.get(key)
    if value is None or value == "":
        return "", (f"a model node needs `config.{key}`" if required else "")
    if not isinstance(value, str):
        return "", f"config.{key} must be text"
    if len(value) > limit:
        return "", f"config.{key} is longer than {limit} characters"
    return value, ""


def _mark(context: Mapping[str, Any], state: str) -> bool:
    marker = context.get("mark_effect")
    if not callable(marker):
        return True
    try:
        return bool(marker(state))
    except Exception:  # noqa: BLE001 - a bookkeeping hiccup is not proof nothing happened
        logger.exception("mark_effect(%s) raised", state)
        return False


# ── structured output: parse, check, repair once ─────────────────────────

_REPAIR_SYSTEM = (
    "You repair JSON. You are given a schema, a task and an answer that does not "
    "satisfy the schema, with the exact problems found. Reply with ONE corrected "
    "JSON value that satisfies the schema and nothing else: no prose, no code fence."
)


def structured(text: str, schema: Mapping[str, Any], *, models: Optional[ModelCalls],
               owner: str, purpose: str, task: str, timeout_s: float) -> Dict[str, Any]:
    """`{"ok": True, "data", "repaired"}` or `{"ok": False, "errors": [...]}`.

    One repair retry, never more: a model that cannot fix its own JSON when
    shown the errors is not going to on the third try, and every extra call is
    spent on the owner's GPU."""
    value, why = schema_check.extract_json(text)
    errors = [why] if why else schema_check.validate(value, schema)
    if not errors:
        return {"ok": True, "data": value, "repaired": False}
    first_errors = list(errors)
    if models is None:
        return {"ok": False, "errors": first_errors, "repair": "no model is wired to repair it"}
    prompt = ("Schema:\n" + json.dumps(schema, ensure_ascii=False, sort_keys=True) +
              "\n\nTask:\n" + task[:4000] +
              "\n\nAnswer to fix:\n" + str(text)[:8000] +
              "\n\nProblems:\n- " + "\n- ".join(first_errors[:12]))
    try:
        fixed = models.complete(
            [{"role": "system", "content": _REPAIR_SYSTEM}, {"role": "user", "content": prompt}],
            owner=owner, purpose=purpose, timeout_s=timeout_s, max_tokens=1500, temperature=0.0)
    except ModelUnavailable as exc:
        return {"ok": False, "errors": first_errors, "repair": f"the repair call failed: {exc}"}
    value, why = schema_check.extract_json(fixed)
    errors = [why] if why else schema_check.validate(value, schema)
    if errors:
        return {"ok": False, "errors": errors, "first_errors": first_errors, "repair": "tried once"}
    return {"ok": True, "data": value, "repaired": True, "first_errors": first_errors}


def _schema_config(config: Mapping[str, Any], key: str = "output_schema",
                   required: bool = False) -> Tuple[Optional[Mapping[str, Any]], str]:
    schema = config.get(key)
    if schema is None:
        return None, (f"a model node needs `config.{key}`" if required else "")
    problems = schema_check.schema_problems(schema)
    if problems:
        return None, f"config.{key} cannot be enforced: " + "; ".join(problems[:4])
    return schema, ""


# ── agent ────────────────────────────────────────────────────────────────

def agent_handler(run_turn: Optional[Callable[[Dict[str, Any]], Mapping[str, Any]]] = None, *,
                  models: Optional[ModelCalls] = None) -> Callable:
    """`agent`: one Faustus agent turn as a workflow step.

    Config: `prompt` (template, required), `system` (template), `agent` (profile
    slug), `tools` (allowlist; `[]` = think only, absent = the worker default),
    `output_schema` (JSON schema; checked after the turn, one repair), `timeout_s`
    (default 300), `max_rounds` (default 8)."""

    def handle(node: WorkflowNode, context: Mapping[str, Any]) -> Dict[str, Any]:
        config = node.config or {}
        prompt_template, problem = _text_config(config, "prompt")
        if problem:
            return _failed(problem)
        system_template, problem = _text_config(config, "system", required=False)
        if problem:
            return _failed(problem)
        timeout_s, problem = _number(config, "timeout_s", 300, 5, 3600)
        if problem:
            return _failed(problem)
        max_rounds, problem = _whole_number(config, "max_rounds", 8, 1, 50)
        if problem:
            return _failed(problem)
        tools = config.get("tools")
        if tools is not None and (not isinstance(tools, (list, tuple))
                                  or any(not isinstance(t, str) or not t.strip() for t in tools)):
            return _failed("config.tools must be a list of tool names")
        agent = config.get("agent")
        if agent is not None and (not isinstance(agent, str) or not agent.strip()):
            return _failed("config.agent must name an agent profile")
        schema, problem = _schema_config(config)
        if problem:
            return _failed(problem)
        if run_turn is None:
            return _failed(
                "no agent runner is wired to the 'agent' node type; nothing ran. Pass one to "
                "default_handlers(agent=...) — Faustus will not pretend an agent answered")
        try:
            prompt = render(prompt_template, context)
            system = render(system_template, context) if system_template else ""
        except TemplateError as exc:
            return _failed(str(exc))
        if not prompt.strip():
            return _failed("the prompt rendered to nothing")

        fenced = _check_fenced(context)
        if fenced is not None:
            return fenced

        owner = str(context.get("owner") or "")
        spec = {
            "prompt": prompt, "system": system, "agent": (agent or "").strip(),
            "tools": list(tools) if tools is not None else None,
            "max_rounds": max_rounds, "timeout_s": timeout_s,
            "owner": owner, "project_id": str(context.get("project_id") or ""),
            "run_id": str(context.get("run_id") or ""), "node_id": node.id,
            "idempotency_key": str(context.get("idempotency_key") or ""),
            "cancel_requested": context.get("cancel_requested"),
            "begin_effect": lambda: _mark(context, "pending"),
        }
        if isinstance(config.get("model"), str) and config["model"].strip():
            spec["model"] = config["model"].strip()
        try:
            turn = dict(run_turn(spec) or {})
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "AgentTurnUnavailable":
                # Raised before the turn began: nothing ran, nothing to doubt.
                return _failed(f"the agent turn could not start: {exc}")
            raise                                # the engine records the uncertainty

        text = str(turn.get("text") or "")
        stop = str(turn.get("stop_reason") or "")
        if stop in ("timeout", "cancelled") or turn.get("error") or int(turn.get("approvals_requested") or 0):
            # The turn began and did not end cleanly, so what its tools did is
            # not known: the effect stays `pending` and the engine withholds
            # any automatic retry.
            if int(turn.get("approvals_requested") or 0):
                reason = ("the agent asked for an approval a workflow cannot answer mid-turn; "
                          "remove that tool from `tools`, or do the step in a `skill`/`deliver` "
                          "node behind a `human_approval`")
            elif stop == "timeout":
                reason = f"the agent turn did not finish within {timeout_s:g}s"
            elif stop == "cancelled":
                reason = "the agent turn was stopped because the workflow lost its claim"
            else:
                reason = f"the agent turn failed: {turn.get('error')}"
            return _failed(reason, text=text[:2000], rounds=turn.get("rounds", 0),
                           tool_calls=turn.get("tool_calls", 0), stop_reason=stop or "error")
        _mark(context, "confirmed")
        if not text.strip():
            return _failed("the agent finished without an answer", rounds=turn.get("rounds", 0),
                           tool_calls=turn.get("tool_calls", 0))

        result: Dict[str, Any] = {
            "text": text, "rounds": int(turn.get("rounds") or 0),
            "tool_calls": int(turn.get("tool_calls") or 0),
            "model": str(turn.get("model") or ""), "profile": str(turn.get("profile") or ""),
            "session_id": str(turn.get("session_id") or ""),
            "tool_events": list(turn.get("tool_events") or [])[:40],
            "stop_reason": stop or "completed",
            "idempotency_key": str(context.get("idempotency_key") or ""),
        }
        if schema is not None:
            verdict = structured(text, schema, models=models, owner=owner,
                                 purpose=str(config.get("purpose") or "utility"),
                                 task=prompt, timeout_s=min(timeout_s, 120.0))
            if not verdict["ok"]:
                return _failed("the agent's answer does not satisfy `output_schema`: "
                               + "; ".join(verdict["errors"][:6])
                               + (f" ({verdict['repair']})" if verdict.get("repair") else ""),
                               **result)
            result["data"] = verdict["data"]
            result["repaired"] = bool(verdict["repaired"])
        return result

    return handle


# The other three node types are added alongside their own tests; until then
# they are simply absent from the table.
def model_node_handlers(*, agent: Optional[Callable] = None,
                        models: Optional[ModelCalls] = None) -> Dict[str, Callable]:
    return {"agent": agent_handler(agent, models=models)}
