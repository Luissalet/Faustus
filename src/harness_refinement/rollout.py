"""One rollout: repeat a task under a given harness, read-only, and record what happened.

A probe compares the harness as it is (the base) with the same harness plus ONE
candidate edit. To do that without touching the real stores, the harness is
rendered into an overlay (the project's instructions, the related memories, the
skill and the sub-agent spec that were in play) that opens the user message of
the repeat; the candidate arm differs from the base arm only by the edited
component. Everything else is identical: same task, same model, same tools.

The repeat runs in the agent loop with ``autonomy_preset="read_only"`` and
``unattended=True``: only tools that change nothing are offered, nothing is
written or sent, and a question to the person ends the run instead of waiting.
A task whose whole point is a side effect is therefore probed on how the agent
investigates and answers, not on the side effect itself.

The shape of the method (a base arm repeated k times, one run per candidate)
follows google-research/harness2 (Apache-2.0).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, List, Mapping, Optional, Sequence

from src.harness_refinement import signals

logger = logging.getLogger(__name__)

COMPONENT_CHARS = 6000
STEP_OUTPUT_CHARS = 240
MAX_STEPS_KEPT = 60
TEXT_CHARS = 6000
DEFAULT_MAX_ROUNDS = 12
DEFAULT_TIMEOUT_S = 480.0
CONTEXT_MESSAGES = 6
CONTEXT_MSG_CHARS = 700

LoopFn = Callable[..., AsyncIterator[str]]


@dataclass
class Component:
    axis: str
    target: str
    label: str
    content: str


def components_from(candidates: Sequence[Any]) -> List[Component]:
    """The parts of the harness that were in play and have content (new-entry candidates have none)."""
    out: List[Component] = []
    for cand in candidates:
        if getattr(cand, "new_target", False) or not (cand.content or "").strip():
            continue
        out.append(Component(cand.axis, cand.target, cand.label, cand.content))
    return out


def with_edit(components: Sequence[Component], edit: Mapping[str, Any]) -> List[Component]:
    """The harness with ONE edit applied, in memory only."""
    axis, target, op, after = edit["axis"], edit["target"], edit["op"], edit.get("after")
    out: List[Component] = []
    replaced = False
    for comp in components:
        if comp.axis == axis and comp.target == target:
            replaced = True
            if op == "delete":
                continue
            out.append(Component(axis, target, comp.label, str(after or "")))
        else:
            out.append(comp)
    if not replaced and op != "delete":
        out.append(Component(axis, target, "added by the edit", str(after or "")))
    return out


def render_overlay(components: Sequence[Component]) -> str:
    """The harness as text that opens the user message of a repeat."""
    if not components:
        return ""
    sections: List[str] = []
    memories: List[str] = []
    for comp in components:
        body = comp.content.strip()[:COMPONENT_CHARS]
        if comp.axis == "prompt_layer":
            sections.append(f"## Standing instructions of the project\n{body}")
        elif comp.axis == "memory":
            memories.append(f"- {body}")
        elif comp.axis == "skill":
            sections.append(f"## Skill `{comp.target.split(':', 1)[1]}` (its SKILL.md)\n{body}")
        elif comp.axis == "subagent_spec":
            sections.append(f"## Sub-agent `{comp.target.split(':', 1)[1]}` (its AGENT.md)\n{body}")
    if memories:
        sections.insert(0, "## Memories\n" + "\n".join(memories))
    return ("=== YOUR STANDING CONFIGURATION FOR THIS TASK (follow it as you would your own settings) ===\n"
            + "\n\n".join(sections) + "\n=== END OF CONFIGURATION ===\n")


def build_messages(task: str, components: Sequence[Component], context: Sequence[Mapping[str, str]] = ()) -> List[Dict[str, str]]:
    messages = [{"role": m["role"], "content": str(m["content"])[:CONTEXT_MSG_CHARS]}
                for m in list(context)[-CONTEXT_MESSAGES:] if m.get("role") in ("user", "assistant") and m.get("content")]
    overlay = render_overlay(components)
    messages.append({"role": "user", "content": (overlay + "\n" if overlay else "") + task})
    return messages


def _short(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str) if value is not None else ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _default_loop() -> LoopFn:
    from src.agent_loop import stream_agent_loop
    return stream_agent_loop


async def run_rollout(rollout_id: str, arm: str, messages: List[Dict[str, str]], *, url: str, model: str,
                      headers: Optional[Dict[str, str]] = None, owner: str = "", candidate: str = "",
                      max_rounds: int = DEFAULT_MAX_ROUNDS, timeout_s: float = DEFAULT_TIMEOUT_S,
                      loop: Optional[LoopFn] = None, workspace: str = "") -> Dict[str, Any]:
    """Run the agent once and return a compact record. With `workspace` (the project's folder) the run can
    read the project's files, which is what the original task could do; it still cannot write.

    ``{id, arm, candidate, delivered, text, steps[{n, round, tool, args, ok, output}], rounds, seconds, error,
    stop_reason}``. A failure of the model or the loop is an ``error`` in the record, never an exception: a
    rollout that cannot finish is evidence too (it delivered nothing)."""
    started = time.time()
    state: Dict[str, Any] = {"text": "", "steps": [], "rounds": 0, "error": "", "stop_reason": ""}
    call = loop or _default_loop()
    session_id = f"harness-probe-{uuid.uuid4().hex[:12]}"

    async def consume() -> None:
        extra: Dict[str, Any] = {"workspace": workspace} if workspace else {}
        stream = call(
            url, model, messages, headers=headers, temperature=0.3, max_tokens=0, max_rounds=max_rounds,
            session_id=session_id, owner=owner or None, autonomy_preset="read_only", unattended=True,
            workload="background",
            harness_options={"checkpoints": False, "run_tests": False, "review_model": "off"}, **extra)
        try:
            async for chunk in stream:
                _absorb(chunk, state)
        finally:
            try:
                await stream.aclose()
            except Exception:  # noqa: BLE001 - closing a finished generator is best effort
                pass

    try:
        await asyncio.wait_for(consume(), timeout=max(1.0, timeout_s))
    except asyncio.TimeoutError:
        state["error"] = state["error"] or "timeout"
        state["stop_reason"] = "timeout"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed rollout is data, not a crash
        state["error"] = f"{type(exc).__name__}: {exc}"[:300]
    text = state["text"].strip()
    return {
        "id": rollout_id, "arm": arm, "candidate": candidate,
        "delivered": bool(text) and not state["error"] == "timeout",
        "text": text[:TEXT_CHARS], "steps": state["steps"][:MAX_STEPS_KEPT], "rounds": state["rounds"],
        "seconds": round(time.time() - started, 1), "error": state["error"], "stop_reason": state["stop_reason"],
    }


def _absorb(chunk: Any, state: Dict[str, Any]) -> None:
    if not isinstance(chunk, str):
        return
    if chunk.startswith("event: error"):
        state["error"] = state["error"] or _short(chunk, 300)
        return
    if not chunk.startswith("data: "):
        return
    body = chunk[6:].strip()
    if not body or body == "[DONE]":
        return
    try:
        event = json.loads(body)
    except (ValueError, TypeError):
        return
    if not isinstance(event, dict):
        return
    kind = event.get("type")
    if "delta" in event and not kind:
        if isinstance(event["delta"], str) and not event.get("thinking"):
            state["text"] += event["delta"]
    elif kind == "response_replace":
        state["text"] = str(event.get("text") or "")
    elif kind == "tool_output":
        state["text"] = ""                       # narration before a tool result is not the answer
        failed = signals.is_failure({"exit_code": event.get("exit_code"), "output": event.get("output")}) \
            or bool(event.get("blocked"))
        state["steps"].append({
            "n": len(state["steps"]) + 1, "round": state["rounds"] or 1, "tool": str(event.get("tool") or ""),
            "args": _short(event.get("command"), 160), "ok": not failed,
            "output": _short(event.get("output"), STEP_OUTPUT_CHARS)})
    elif kind in ("round_info", "agent_step"):
        try:
            state["rounds"] = max(state["rounds"], int(event.get("round") or 0))
        except (TypeError, ValueError):
            pass
    elif kind == "metrics" and isinstance(event.get("data"), dict):
        stop = ((event["data"].get("harness") or {}).get("stop_reason"))
        if stop:
            state["stop_reason"] = str(stop)
    elif kind == "error" and event.get("message"):
        state["error"] = state["error"] or _short(event.get("message"), 300)
