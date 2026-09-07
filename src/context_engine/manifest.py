"""
context_engine/manifest.py — the packet, explained to a person.

`ContextPacket.manifest()` already answers the machine's question: one row per
injected item, with provenance, scores and transformation.  This module answers
the three questions people actually arrive with, and it answers them from the
packet alone, months later, with none of the sources still around:

* `summarize()` / `render()` — "where did the window go?"  A table of sections
  with tokens and share, then the omissions grouped by reason.  Plain text, no
  colour, no emoji, meant to be pasted into an issue.
* `explain()` — "why did it not read that file?"  One source ref in, one
  verdict out: injected here in this form, or left out for this reason, or
  never a candidate at all.  The third answer is a real answer and the most
  common one people are missing when they ask.
* `compare()` — "what would the engine have done differently?"  This is the
  Phase 1 instrument.  Shadow mode compiles a packet, does not use it, and this
  function puts it beside the messages the app really sent so the difference
  can be counted before anything is switched over.

`compare()` classifies the real messages with `src.context_ledger.classify` and
nothing else.  A second classifier written here would drift from the one the
ledger card shows the user within a release, and then the shadow report and the
ledger would disagree about the same prompt — which is precisely the kind of
discrepancy that gets a migration cancelled for the wrong reason.

Two smaller decisions worth stating.  Percentages are of the packet, not of the
window, because the question a section table answers is "what crowded out
what"; the window appears once, on its own line.  And `compare()` decides
whether a packet item was already present in the sent prompt by looking for its
`source_ref` in the text — a heuristic, labelled as one in the output, because
the alternative is pretending the app's ad-hoc prompt assembly recorded
provenance it never recorded.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Mapping, Sequence

from src.context_ledger import classify

from .budgets import TokenEstimator
from .contracts import SECTION_KINDS, ContextPacket

logger = logging.getLogger(__name__)

#: Which section a ledger bucket would have become, had the engine compiled the
#: prompt the app actually sent.  Several buckets map to one section on
#: purpose: the engine's job is to merge lanes the app kept apart.
LEDGER_SECTIONS: Dict[str, str] = {
    "system": "system_constraints",
    "tools": "tool_guidance",
    "skills": "tool_guidance",
    "instructions": "project_rules",
    "memory": "retrieved_memory",
    "documents": "retrieved_documents",
    "web": "retrieved_documents",
    "attachments": "retrieved_documents",
    "retrieved": "retrieved_documents",
    "tool_results": "current_state",
    "conversation": "recent_messages",
    "user": "recent_messages",
}


def _pct(part: int, whole: int) -> float:
    return round(part * 100.0 / whole, 1) if whole else 0.0


# ── summary ────────────────────────────────────────────────────────────────

def summarize(packet: ContextPacket) -> Dict[str, Any]:
    """Everything `render()` prints, as data, for an API or a test.

    Derived from the packet on every call rather than stored anywhere: a
    summary that can drift from the packet it describes is worse than none,
    because it will be believed."""
    from src.memory_view import from_context_packet, explain as explain_memory
    memory_view = from_context_packet(packet)
    total = packet.tokens()
    budget = packet.window.input_budget
    items = packet.items()

    sections: List[Dict[str, Any]] = []
    for section in packet.sections:
        tokens = section.tokens()
        sections.append({
            "kind": section.kind,
            "priority": section.priority,
            "items": len(section.items),
            "tokens": tokens,
            "pct": _pct(tokens, total),
            "budget_tokens": section.budget_tokens,
            "over_budget": bool(section.budget_tokens
                                and tokens > section.budget_tokens),
            "trim_policy": section.trim_policy,
            "mandatory": section.is_mandatory(),
        })

    by_source: Dict[str, Dict[str, int]] = {}
    by_transformation: Dict[str, int] = {}
    for item in items:
        bucket = by_source.setdefault(item.source_type, {"items": 0, "tokens": 0})
        bucket["items"] += 1
        bucket["tokens"] += item.tokens
        by_transformation[item.transformation] = (
            by_transformation.get(item.transformation, 0) + 1)

    by_reason: Dict[str, int] = {}
    for omission in packet.omissions:
        by_reason[omission.reason] = by_reason.get(omission.reason, 0) + 1

    return {
        "packet_id": packet.packet_id,
        "memory_view": memory_view.to_dict(),
        "memory_explanation": explain_memory(memory_view),
        "request_id": packet.request_id,
        "identity": packet.identity(),
        "model": packet.model,
        "intent": packet.intent,
        "phase": packet.phase,
        "consumer": packet.consumer,
        "degraded": packet.degraded,
        "warnings": list(packet.warnings),
        "tokens": total,
        "input_budget": budget,
        "budget_pct": _pct(total, budget),
        "fits": packet.fits(),
        "estimator": packet.window.estimator,
        "window_known": packet.window.window_known,
        "items": len(items),
        "generated_items": sum(1 for i in items if i.is_generated()),
        "degraded_items": sum(1 for i in items if i.degraded),
        "sections": sections,
        "by_source_type": by_source,
        "by_transformation": by_transformation,
        "omissions": {
            "total": len(packet.omissions),
            "recoverable": sum(1 for o in packet.omissions if o.recoverable),
            "by_reason": by_reason,
        },
    }


# ── rendering ──────────────────────────────────────────────────────────────

def _row(cells: Sequence[str], widths: Sequence[int]) -> str:
    out: List[str] = []
    for index, cell in enumerate(cells):
        width = widths[index]
        out.append(cell.ljust(width) if index == 0 else cell.rjust(width))
    return "  ".join(out).rstrip()


def render(packet: ContextPacket) -> str:
    """The packet as plain text a person can read and paste into a report.

    ASCII only and fixed-width columns: this ends up in issues, logs and
    terminal output, and a table that only lines up in one font is a table
    nobody keeps."""
    data = summarize(packet)
    lines: List[str] = []

    lines.append(f"Context packet {packet.packet_id or '(unsaved)'}")
    if packet.request_id:
        lines.append(f"  request   {packet.request_id}")
    lines.append(f"  model     {packet.model or '(unknown)'}  "
                 f"intent={packet.intent}  phase={packet.phase}  "
                 f"consumer={packet.consumer}")
    window = (f"{data['tokens']} of {data['input_budget']} tokens "
              f"({data['budget_pct']}%)" if data["input_budget"]
              else f"{data['tokens']} tokens (no budget recorded)")
    lines.append(f"  window    {window}  estimator={data['estimator']}"
                 f"{'' if data['window_known'] else ' (window not proven)'}")
    lines.append(f"  items     {data['items']}  "
                 f"generated={data['generated_items']}  "
                 f"omitted={data['omissions']['total']}")
    if packet.degraded:
        lines.append("  status    DEGRADED")
    for warning in packet.warnings:
        lines.append(f"  warning   {warning}")

    lines.append("")
    lines.append("Sections")
    widths = (22, 7, 6, 8, 6)
    lines.append("  " + _row(("section", "tokens", "pct", "budget", "items"), widths))
    for row in data["sections"]:
        lines.append("  " + _row((
            row["kind"],
            str(row["tokens"]),
            f"{row['pct']:.1f}",
            str(row["budget_tokens"] or "-"),
            str(row["items"]),
        ), widths))
    lines.append("  " + _row((
        "TOTAL", str(data["tokens"]), "100.0",
        str(data["input_budget"] or "-"), str(data["items"]),
    ), widths))

    if packet.omissions:
        lines.append("")
        lines.append(f"Omissions ({len(packet.omissions)})")
        grouped: Dict[str, List[Any]] = {}
        for omission in packet.omissions:
            grouped.setdefault(omission.reason, []).append(omission)
        for reason in sorted(grouped):
            rows = grouped[reason]
            lines.append(f"  {reason} ({len(rows)})")
            for omission in rows:
                flag = "recoverable" if omission.recoverable else "not recoverable"
                detail = f" - {omission.detail}" if omission.detail else ""
                lines.append(f"    {omission.source_type}  {omission.source_ref}  "
                             f"[{flag}]{detail}")
    return "\n".join(lines)


# ── one source ─────────────────────────────────────────────────────────────

def explain(packet: ContextPacket, source_ref: str) -> Dict[str, Any]:
    """What happened to one source in this packet.

    `status` is `included`, `omitted`, `both` (some chunks in, some out) or
    `unknown`.  `unknown` is not an error: it means no lane ever produced this
    ref as a candidate, which is the true answer to most "why did it not read
    that file?" questions and the one a packet that only listed its contents
    could never give."""
    wanted = (source_ref or "").strip()
    rows = [row for row in packet.manifest() if row["source_ref"] == wanted]
    omissions = [o.to_dict() for o in packet.omissions if o.source_ref == wanted]

    if rows and omissions:
        status = "both"
        detail = ("some of this source was injected and some of it was left out; "
                  "see items and omissions")
    elif rows:
        status = "included"
        detail = (f"injected in {', '.join(sorted({r['section'] for r in rows}))} "
                  f"as {', '.join(sorted({r['transformation'] for r in rows}))}")
    elif omissions:
        status = "omitted"
        detail = "; ".join(f"{o['reason']}: {o['detail']}" if o["detail"]
                           else str(o["reason"]) for o in omissions)
    else:
        status = "unknown"
        detail = ("this source was never a candidate for this packet: no lane "
                  "returned it, so it was neither ranked nor rejected")

    return {
        "source_ref": wanted,
        "status": status,
        "detail": detail,
        "items": rows,
        "omissions": omissions,
        "tokens": sum(int(row["tokens"]) for row in rows),
        "packet_id": packet.packet_id,
    }


# ── shadow-mode comparison ─────────────────────────────────────────────────

def _last_user_index(messages: Sequence[Mapping[str, Any]]) -> int:
    """Which message is the user's actual question.

    The same rule `context_ledger` uses, restated over an immutable sequence:
    the last `user` message that is not retrieved context wearing the user
    role.  It is a position, not a classification — the classification itself
    stays in `classify()`, which is imported and never reimplemented."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        meta = message.get("metadata")
        if isinstance(meta, Mapping) and meta.get("trusted") is False:
            continue
        return index
    return -1


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: List[str] = []
        for block in content:
            if isinstance(block, Mapping):
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _mentions(haystack: str, source_ref: str) -> bool:
    """Was this source already in the prompt the app sent?

    A heuristic and labelled as one: the ref itself, or its basename when the
    ref is a path.  The app's prompt assembly records no provenance, so there
    is nothing exact to compare against — which is, in one sentence, the reason
    the Context Engine exists."""
    ref = (source_ref or "").strip()
    if not ref:
        return False
    if ref in haystack:
        return True
    base = os.path.basename(ref.replace("\\", "/")).strip()
    return bool(base) and len(base) > 3 and base in haystack


def compare(packet: ContextPacket, messages: Sequence[Mapping[str, Any]], *,
            estimator: TokenEstimator) -> Dict[str, Any]:
    """Shadow mode (Phase 1): the packet the engine would have compiled, beside
    the prompt the app really sent.

    Answers three things and no more: what would have come in that did not,
    what went out that the engine would not have sent, and how many tokens
    apart the two are.  Both sides are measured with the *same* estimator,
    because a difference produced by two different rulers is not a difference.
    """
    rows = [m for m in (messages or ()) if isinstance(m, Mapping)]
    last_user = _last_user_index(rows)

    sent_by_bucket: Dict[str, Dict[str, int]] = {}
    sent_text: List[str] = []
    for index, message in enumerate(rows):
        bucket = classify(dict(message), is_last_user=(index == last_user))
        try:
            tokens = int(estimator.count_messages([message]))
        except Exception:  # noqa: BLE001 - a shadow report may never break a turn
            tokens = 0
        entry = sent_by_bucket.setdefault(bucket, {"tokens": 0, "messages": 0})
        entry["tokens"] += tokens
        entry["messages"] += 1
        sent_text.append(_message_text(message))
    haystack = "\n".join(sent_text)

    sent_by_section: Dict[str, Dict[str, int]] = {}
    for bucket, entry in sent_by_bucket.items():
        kind = LEDGER_SECTIONS.get(bucket, "retrieved_documents")
        target = sent_by_section.setdefault(kind, {"tokens": 0, "messages": 0})
        target["tokens"] += entry["tokens"]
        target["messages"] += entry["messages"]

    packet_by_section: Dict[str, Dict[str, int]] = {}
    for section in packet.sections:
        packet_by_section[section.kind] = {"tokens": section.tokens(),
                                           "items": len(section.items)}

    by_section: List[Dict[str, Any]] = []
    for kind in SECTION_KINDS:
        left = packet_by_section.get(kind, {"tokens": 0, "items": 0})
        right = sent_by_section.get(kind, {"tokens": 0, "messages": 0})
        if not left["tokens"] and not right["tokens"]:
            continue
        by_section.append({
            "section": kind,
            "packet_tokens": left["tokens"],
            "packet_items": left["items"],
            "sent_tokens": right["tokens"],
            "sent_messages": right["messages"],
            "delta_tokens": left["tokens"] - right["tokens"],
        })

    would_add = [
        {"source_type": item.source_type, "source_ref": item.source_ref,
         "tokens": item.tokens, "transformation": item.transformation}
        for item in packet.items() if not _mentions(haystack, item.source_ref)
    ]
    would_drop = [
        {"section": kind, "tokens": entry["tokens"], "messages": entry["messages"]}
        for kind, entry in sorted(sent_by_section.items())
        if not packet_by_section.get(kind, {}).get("tokens")
    ]

    packet_tokens = packet.tokens()
    sent_tokens = sum(entry["tokens"] for entry in sent_by_bucket.values())
    return {
        "packet_id": packet.packet_id,
        "estimator": getattr(estimator, "name", "unknown"),
        "packet_tokens": packet_tokens,
        "sent_tokens": sent_tokens,
        "delta_tokens": packet_tokens - sent_tokens,
        "messages": len(rows),
        "by_section": by_section,
        "sent_by_bucket": {k: dict(v) for k, v in sorted(sent_by_bucket.items())},
        "would_add": would_add,
        "would_drop": would_drop,
        "omitted": [o.to_dict() for o in packet.omissions],
        "mention_check": "heuristic: source_ref or its basename found in the sent text",
    }


__all__ = ["LEDGER_SECTIONS", "summarize", "render", "explain", "compare"]
