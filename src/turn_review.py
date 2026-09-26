"""What happened in a chat's recent agent turns, read back from what they saved.

A turn that went wrong (a loop over the same tool, a failure retried the
same way, a prompt cache lost every round, an hour without writing
anything) leaves its evidence in the assistant message's metadata: the
tool events with their exit codes and durations, the rounds and the models
that answered them, the prompt-cache counters, the time. This folds that
into a short review the model can read (`turn_review`) to diagnose its own
last turn instead of guessing, and a person can ask for ("why did the last
answer take so long?").
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Optional


def _md(m: Any) -> Dict[str, Any]:
    md = m.get("metadata") if isinstance(m, dict) else getattr(m, "metadata", None)
    return md if isinstance(md, dict) else {}


def _role(m: Any) -> str:
    return str(m.get("role") if isinstance(m, dict) else getattr(m, "role", "") or "")


def _content(m: Any) -> str:
    c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
    return c if isinstance(c, str) else ""


def _failed(ev: Dict[str, Any]) -> bool:
    code = ev.get("exit_code")
    return (isinstance(code, int) and not isinstance(code, bool) and code != 0) or bool(ev.get("argument_errors"))


def review_turn(md: Dict[str, Any], question: str = "", answer: str = "") -> Dict[str, Any]:
    events = [e for e in (md.get("tool_events") or []) if isinstance(e, dict)]
    tools = Counter(str(e.get("tool") or "?") for e in events)
    failures = [e for e in events if _failed(e)]
    fail_sig = Counter((str(e.get("tool") or "?"), str(e.get("output") or "")[:80]) for e in failures)
    writes = [e for e in events if str(e.get("tool") or "") in ("write_file", "edit_file", "apply_patch",
                                                               "create_document", "update_document", "edit_document")]
    rounds = md.get("agent_rounds")
    if not isinstance(rounds, int) and isinstance(md.get("round_models"), list):
        rounds = len(md["round_models"])
    pc = md.get("prompt_cache") if isinstance(md.get("prompt_cache"), dict) else None
    slow = sorted((e for e in events if isinstance(e.get("duration_ms"), (int, float))),
                  key=lambda e: -float(e["duration_ms"]))[:3]
    findings: List[str] = []
    for (tool, out), n in fail_sig.items():
        if n >= 2:
            findings.append(f"`{tool}` failed {n} times the same way ({out.strip() or 'no output'}): the retry did not change the approach")
    for tool, n in tools.items():
        if n >= 8 and tool not in ("write_file", "edit_file", "apply_patch"):
            findings.append(f"`{tool}` called {n} times: check for a loop or work that one call could batch")
    if events and not writes and isinstance(rounds, int) and rounds >= 12:
        findings.append(f"{rounds} rounds and nothing written: a draft early would have kept the progress")
    if pc and pc.get("rounds"):
        seen = (pc.get("processed") or 0) + (pc.get("cached") or 0)
        share = (pc.get("cached") or 0) / seen if seen else 0
        # One lost round is usually an image or the tool list changing once;
        # it is worth a finding when it repeats or when little was reused.
        if pc.get("lost_rounds") and (pc["lost_rounds"] >= 2 or share < 0.7):
            findings.append(f"the prompt cache was lost in {pc['lost_rounds']} of {pc['rounds']} rounds "
                            f"({round(100 * share)} % reused): something rewrote earlier messages or another chat took the slot")
    if md.get("stop_reason") and md.get("stop_reason") not in ("done", "stop", "answered"):
        findings.append(f"the turn stopped: {md.get('stop_reason')}")
    return {
        "question": question[:160], "answer_chars": len(answer),
        "model": md.get("model") or md.get("requested_model"),
        "rounds": rounds, "time_s": md.get("total_time") or md.get("response_time"),
        "tool_calls": len(events), "tools": dict(tools.most_common(8)),
        "failures": [{"tool": e.get("tool"), "round": e.get("round"), "exit_code": e.get("exit_code"),
                      "output": str(e.get("output") or "")[:200]} for e in failures[:6]],
        "writes": len(writes),
        "slowest": [{"tool": e.get("tool"), "round": e.get("round"), "seconds": round(float(e["duration_ms"]) / 1000, 1)}
                    for e in slow],
        "prompt_cache": pc, "findings": findings,
    }


def review(history: Iterable[Any], turns: int = 1) -> List[Dict[str, Any]]:
    """The last `turns` assistant turns that carry metrics, newest first."""
    items = list(history or [])
    out: List[Dict[str, Any]] = []
    for i in range(len(items) - 1, -1, -1):
        m = items[i]
        md = _md(m)
        if _role(m) != "assistant" or not md or not any(k in md for k in ("tool_events", "agent_rounds", "response_time")):
            continue
        question = ""
        for j in range(i - 1, -1, -1):
            if _role(items[j]) == "user":
                question = _content(items[j])
                break
        out.append(review_turn(md, question, _content(m)))
        if len(out) >= max(1, turns):
            break
    return out


def render(reviews: List[Dict[str, Any]]) -> str:
    if not reviews:
        return "No agent turn with saved metrics in this chat yet."
    lines: List[str] = []
    for n, r in enumerate(reviews, 1):
        head = f"### Turn -{n}: {r['question'] or '(no question)'}"
        lines.append(head)
        lines.append(f"- model {r['model'] or '?'} · {r['rounds'] or '?'} rounds · {r['tool_calls']} tool calls"
                     f" · {r['writes']} writes · {r['time_s'] or '?'} s · answer {r['answer_chars']} chars")
        if r["tools"]:
            lines.append("- tools: " + ", ".join(f"{k}×{v}" for k, v in r["tools"].items()))
        for f in r["failures"]:
            lines.append(f"- failed: `{f['tool']}` (round {f['round']}, exit {f['exit_code']}): {f['output'][:140]}")
        if r["slowest"]:
            lines.append("- slowest: " + ", ".join(f"{s['tool']} {s['seconds']} s" for s in r["slowest"]))
        pc = r.get("prompt_cache")
        if pc and pc.get("rounds"):
            lines.append(f"- prompt cache: {pc.get('cached', 0)} reused / {pc.get('processed', 0)} processed,"
                         f" lost in {pc.get('lost_rounds', 0)} of {pc['rounds']} rounds")
        for f in r["findings"]:
            lines.append(f"- **finding**: {f}")
        lines.append("")
    return "\n".join(lines).strip()
