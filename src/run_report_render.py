"""run_report_render.py — plain-text renderings of the run records.

One place for the wording, used by the `run_report` builtin tool, the HTTP
routes (as a ``text`` field, so a coordinator over MCP needs no renderer of its
own) and the CLI-style readers.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable


def effects_text(rows: Iterable[Dict[str, Any]], summary: Dict[str, Any]) -> str:
    rows = list(rows)
    if not rows:
        return "No effects recorded."
    lines = [f"{summary.get('total', len(rows))} effect(s); {summary.get('unresolved', 0)} with an unknown or "
             "partial outcome."]
    for r in rows:
        lines.append(
            f"- {r['id']} {r['kind']} -> {r.get('destination') or '?'}: {r['state']} (certainty "
            f"{r['effect_certainty']}, attempt {r.get('attempt_id') or '?'})"
            + (f" — {r['reason']}" if r.get("reason") else ""))
    return "\n".join(lines)


def ledger_text(state: Dict[str, Any]) -> str:
    lines = [f"Run {state['run_id'][:12]}: {state['state']}"
             + (f" ({state['reason']})" if state.get("reason") else "")
             + f", {state['event_count']} ledger event(s)"]
    for c in state["calls"]:
        line = f"- {c['call_id']} {c['tool'] or '?'}: {c['state']}"
        if c["approval"]:
            line += f" [approval {c['approval']['state']}]"
        if c["resumed"]:
            line += (f" [resumed in run {str(c['resumed']['executed_in_run'])[:12]}, "
                     f"arguments match: {c['resumed']['args_match']}]")
        if c["effect_certainty"] not in ("none", ""):
            line += f" (effect {c['effect_certainty']})"
        lines.append(line)
    if state["pending_approvals"]:
        lines.append(f"Waiting for approval: {', '.join(state['pending_approvals'])}")
    return "\n".join(lines)


def steering_text(receipts: Iterable[Dict[str, Any]]) -> str:
    lines = [f"- {r['receipt_id'][:8]} [{r.get('mode')}] {r['state']}"
             + (f" ({r['reason']})" if r.get("reason") else "") + f": {str(r.get('text') or '')[:80]}"
             for r in receipts]
    return "\n".join(lines) or "No steering messages recorded."


def orphans_text(orphans: Iterable[Dict[str, Any]]) -> str:
    lines = [f"- {o['session_id'] or o['worker_id']} ({o['kind']}, {o['name'] or o['role'] or 'worker'}): "
             f"{o['reason']}; running {o['age_s']:.0f} s" for o in orphans]
    return "\n".join(lines) or "No orphaned workers."
