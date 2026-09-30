"""turn_cost.py — where the time and tokens of one turn went, by cause.

A turn is one detached run. Everything the run did is in the execution ledger
(``src/exec_ledger.py``): its model calls (tagged with the phase that made
them), its tool calls with their attempts and durations, and what its delegated
workers reported when they finished. This module groups those rows causally:

========== ===========================================================
main_rounds  the loop's own streamed model calls
tools        tool calls (every attempt after the first is in ``retries``)
compaction   model calls made to compact the context
recovery     model calls made to recover a failed or empty answer
advisor      the second-opinion model
workers      delegated workers: the parent's wait on them, and what they report
retries      failed model calls, and tool attempts after the first
other_model  model calls no scope labelled (a judge, a router, a verifier)
========== ===========================================================

The rule that governs every number here: **unknown stays unknown.** A phase's
total is the sum of the calls that reported the value; ``*_unknown_calls``
counts the ones that did not, and a total nothing reported is ``null``, never
``0``. Token counts an engine did not report but Faustus estimated are counted
separately (``estimated_calls``). ``unaccounted_ms`` is the run's wall clock
minus the phases' known time: queue wait, model loading and prompt processing
that no call reported sit there.

Not itemized, on purpose: retries *inside* one model call (the transport retries
before it answers) add to that call's duration and are not split out.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from src import exec_ledger

#: Tools whose duration is the parent waiting for delegated workers.
DELEGATING_TOOLS = frozenset({"delegate_agents", "dispatch_workers", "mcp__workers__dispatch_workers"})

PHASE_ORDER = ("main_rounds", "tools", "compaction", "recovery", "advisor", "workers", "retries", "other_model")

_IN_KEYS = ("input_tokens", "prompt_tokens")
_OUT_KEYS = ("output_tokens", "completion_tokens")


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _first(usage: Dict[str, Any], keys: Iterable[str]) -> Optional[float]:
    for key in keys:
        value = _num(usage.get(key))
        if value is not None:
            return value
    return None


def _sum(values: List[Optional[float]]) -> Optional[float]:
    known = [v for v in values if v is not None]
    return round(sum(known), 3) if known else None


def _model_group(key: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    usages = [(r.get("usage") or {}) for r in rows]
    durations = [_num(r.get("duration_ms")) for r in rows]
    ins = [_first(u, _IN_KEYS) for u in usages]
    outs = [_first(u, _OUT_KEYS) for u in usages]
    costs = [_num(u.get("cost_usd")) if u.get("cost_state") == "known" else None for u in usages]
    known_costs = [c for c in costs if c is not None]
    return {
        "key": key, "calls": len(rows),
        "duration_ms": _sum(durations), "duration_unknown_calls": sum(1 for d in durations if d is None),
        "input_tokens": _sum(ins), "input_tokens_unknown_calls": sum(1 for v in ins if v is None),
        "output_tokens": _sum(outs), "output_tokens_unknown_calls": sum(1 for v in outs if v is None),
        "estimated_calls": sum(1 for u in usages if u.get("usage_source") == "estimated"),
        "cost_usd": round(sum(known_costs), 6) if known_costs else None,
        "cost_state": ("unknown" if not known_costs else "known" if len(known_costs) == len(rows) else "partial"),
        "errors": sum(1 for r in rows if r.get("error")),
        "models": sorted({str(r.get("model")) for r in rows if r.get("model")}),
    }


def _events(run_id: str) -> List[Dict[str, Any]]:
    return exec_ledger.events(run_id, limit=20000)


def resolve_run(session_id: str, run_id: Optional[str] = None, turn: int = 0) -> Optional[str]:
    """The run a cost view is about: the one named, else the ``turn``-th most
    recent run of the session (0 = the latest) that did any model or tool work."""
    if run_id:
        return run_id
    seen = 0
    for rid in exec_ledger.run_ids_for_session(session_id, limit=200):
        kinds = {e["kind"] for e in _events(rid)}
        if kinds & {"model_call", "attempt_result", "child_result"}:
            if seen == max(0, int(turn)):
                return rid
            seen += 1
    return None


def build(session_id: str, run_id: Optional[str] = None, *, turn: int = 0) -> Dict[str, Any]:
    rid = resolve_run(session_id, run_id, turn)
    if rid is None:
        return {"found": False, "session_id": session_id, "reason": "no recorded run with model or tool work"}
    events = _events(rid)
    if not events:
        return {"found": False, "session_id": session_id, "run_id": rid, "reason": "the ledger has no rows for this run"}
    state = exec_ledger.replay(rid)

    model_rows = [{**e["payload"], "ts": e["ts"]} for e in events if e["kind"] == "model_call"]
    by_key: Dict[str, List[Dict[str, Any]]] = {k: [] for k in PHASE_ORDER}
    retry_model: List[Dict[str, Any]] = []
    for row in model_rows:
        phase = row.get("phase") or ""
        if phase in ("compaction", "recovery", "advisor"):
            by_key[phase].append(row)
        elif row.get("error"):
            retry_model.append(row)
        elif row.get("transport") == "stream":
            by_key["main_rounds"].append(row)
        else:
            by_key["other_model"].append(row)

    phases: List[Dict[str, Any]] = []
    for key in ("main_rounds",):
        phases.append(_model_group(key, by_key[key]))

    # tools: first attempts only; delegating tools are the parent's wait on workers
    tool_items: Dict[str, Dict[str, Any]] = {}
    wait_ms: List[Optional[float]] = []
    retry_tool: List[Optional[float]] = []
    retry_tool_count = 0
    tool_durations: List[Optional[float]] = []
    tool_calls = 0
    for call in state["calls"]:
        attempts = [a for a in call["attempts"] if a.get("state") != "running"]
        if not attempts:
            continue
        first = attempts[0]
        duration = _num(first.get("duration_ms"))
        if call["tool"] in DELEGATING_TOOLS:
            wait_ms.append(duration)
        else:
            tool_calls += 1
            tool_durations.append(duration)
            item = tool_items.setdefault(call["tool"], {"tool": call["tool"], "calls": 0, "durations": [], "failed": 0,
                                                       "unknown_effect": 0})
            item["calls"] += 1
            item["durations"].append(duration)
            item["failed"] += 1 if first.get("state") in ("failed", "cancelled", "interrupted") else 0
            item["unknown_effect"] += 1 if first.get("effect_certainty") in ("unknown", "partial") else 0
        for extra in attempts[1:]:
            retry_tool_count += 1
            retry_tool.append(_num(extra.get("duration_ms")))
    tools_group = {
        "key": "tools", "calls": tool_calls, "duration_ms": _sum(tool_durations),
        "duration_unknown_calls": sum(1 for d in tool_durations if d is None),
        "items": sorted(({"tool": i["tool"], "calls": i["calls"], "duration_ms": _sum(i["durations"]),
                          "failed": i["failed"], "unknown_effect": i["unknown_effect"]} for i in tool_items.values()),
                        key=lambda i: -(i["duration_ms"] or 0))[:15],
    }
    phases.append(tools_group)
    for key in ("compaction", "recovery", "advisor"):
        phases.append(_model_group(key, by_key[key]))

    children = [e["payload"] for e in events if e["kind"] == "child_result"]
    c_in = [_num(c.get("input_tokens")) for c in children]
    c_out = [_num(c.get("output_tokens")) for c in children]
    c_dur = [_num(c.get("duration_ms")) for c in children]
    phases.append({
        "key": "workers", "calls": len(children), "duration_ms": _sum(wait_ms),
        "duration_unknown_calls": sum(1 for d in wait_ms if d is None),
        "children_duration_ms_sum": _sum(c_dur),
        "input_tokens": _sum(c_in), "input_tokens_unknown_calls": sum(1 for v in c_in if v is None),
        "output_tokens": _sum(c_out), "output_tokens_unknown_calls": sum(1 for v in c_out if v is None),
        "cost_usd": None, "cost_state": "unknown",
        "items": [{"worker_id": c.get("worker_id"), "name": c.get("name"), "role": c.get("role"),
                   "child_session_id": c.get("child_session_id"), "outcome": c.get("outcome"),
                   "stop_reason": c.get("stop_reason"), "duration_ms": c.get("duration_ms"),
                   "input_tokens": c.get("input_tokens"), "output_tokens": c.get("output_tokens"),
                   "tool_calls": c.get("tool_calls")} for c in children][:20],
    })
    retries = _model_group("retries", retry_model)
    retries["calls"] += retry_tool_count
    retries["tool_attempts"] = retry_tool_count
    if retry_tool:
        retries["duration_ms"] = _sum([retries["duration_ms"]] + retry_tool)
    phases.append(retries)
    phases.append(_model_group("other_model", by_key["other_model"]))

    started = next((e for e in events if e["kind"] == "run_started"), None)
    ended = next((e for e in reversed(events) if e["kind"] == "run_state"
                  and e["payload"].get("state") in ("done", "stopped", "error", "interrupted")), None)
    total_ms = None
    if started and ended:
        total_ms = _wall_ms(started["ts"], ended["ts"])
    accounted = _sum([p.get("duration_ms") for p in phases])
    notes: List[str] = []
    unaccounted = None
    if total_ms is not None and accounted is not None:
        unaccounted = round(total_ms - accounted, 1)
        if unaccounted < -max(50.0, total_ms * 0.05):
            notes.append("phases add up to more than the run's wall clock: some of their time overlapped")
            unaccounted = None
    if total_ms is None:
        notes.append("the run has not ended or its start was not recorded: no wall-clock total")
    if not model_rows:
        notes.append("no model call was recorded for this run (it predates the ledger, or made none)")
    if any(p.get("estimated_calls") for p in phases):
        notes.append("some token counts are Faustus's estimate, not reported by the engine")
    notes.append("time inside one model call includes the transport's own retries, queueing and model loading")
    return {
        "found": True, "session_id": session_id, "run_id": rid, "state": state["state"],
        "started_at": state.get("started_at", ""), "total_ms": total_ms, "accounted_ms": accounted,
        "unaccounted_ms": unaccounted, "phases": phases, "notes": notes,
        "sources": {"ledger_events": len(events), "model_calls": len(model_rows), "tool_calls": tool_calls + len(wait_ms)},
    }


def _wall_ms(start: str, end: str) -> Optional[float]:
    from datetime import datetime
    try:
        a = datetime.strptime(start, "%Y-%m-%dT%H:%M:%S.%fZ")
        b = datetime.strptime(end, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return None
    return round(max(0.0, (b - a).total_seconds() * 1000.0), 1)


def render_text(view: Dict[str, Any]) -> str:
    """The same view as compact text, for a model or a terminal."""
    if not view.get("found"):
        return f"No cost view: {view.get('reason', 'nothing recorded')}."

    def ms(v: Any) -> str:
        return "unknown" if v is None else (f"{v / 1000:.1f} s" if v >= 1000 else f"{v:.0f} ms")

    def tok(v: Any, unknown: int) -> str:
        if v is None:
            return "unknown"
        return f"{int(v)}" + (f" (+{unknown} calls unreported)" if unknown else "")

    lines = [f"Turn {view['run_id'][:12]} ({view['state']}): total {ms(view['total_ms'])}, "
             f"accounted {ms(view['accounted_ms'])}, unaccounted {ms(view['unaccounted_ms'])}"]
    for p in view["phases"]:
        if not p.get("calls"):
            continue
        extra = ""
        if "input_tokens" in p:
            extra = f"; tokens in {tok(p['input_tokens'], p.get('input_tokens_unknown_calls', 0))}, " \
                    f"out {tok(p['output_tokens'], p.get('output_tokens_unknown_calls', 0))}; cost {p.get('cost_state')}"
            if p.get("cost_usd") is not None:
                extra += f" ({p['cost_usd']} USD)"
        lines.append(f"- {p['key']}: {p['calls']} call(s), {ms(p['duration_ms'])}"
                     + (f" ({p['duration_unknown_calls']} without duration)" if p.get("duration_unknown_calls") else "")
                     + extra)
        for item in (p.get("items") or [])[:5]:
            label = item.get("tool") or item.get("name") or item.get("worker_id")
            lines.append(f"    · {label}: {item.get('calls', 1)} call(s), {ms(item.get('duration_ms'))}")
    for note in view.get("notes", []):
        lines.append(f"Note: {note}.")
    return "\n".join(lines)


__all__ = ["DELEGATING_TOOLS", "PHASE_ORDER", "build", "render_text", "resolve_run"]
