"""Per-result segments inside the fenced-mode tool results message.

A route without native function calling (local models answering with fenced
blocks) gets every result of a round back as ONE user-role
``untrusted_context_message("tool execution results", ...)`` whose body is the
formatted results joined by a blank line. That single message is opaque to
anything that wants to act on ONE result of the round: mid-turn spill, and the
model's own context tools (``context_drop``/``context_note``/``context_pin``).

``_append_tool_results`` (src/agent_loop.py) now records where each result
sits inside that body, as private keys on the message (never sent to a
provider — ``llm_core._sanitize_llm_messages`` keeps only role/content/tool
fields):

    msg["_tool_round"]    = <round number>
    msg["_tool_segments"] = [{"handle": "r7.0", "tool": "bash", "call_id": ...,
                              "start": <offset in content>, "end": <offset>,
                              "sha": <sha256[:16] of content[start:end]>}, ...]

Every edit goes through ``replace_segment``, which rewrites the content and
shifts the offsets of the later segments. A segment whose slice no longer
hashes to its ``sha`` (something else rewrote the message) is treated as
unknown: ``segments()`` then returns nothing and callers fall back to the
whole message body, so a stale offset can never cut a result in half.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.prompt_security import GUARD_CLOSE, _escape_guard_markers

FENCED_RESULTS_SOURCE = "tool execution results"
SEGMENTS_KEY = "_tool_segments"
ROUND_KEY = "_tool_round"
JOINER = "\n\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def escape_results(texts: Sequence[str]) -> Tuple[str, List[Tuple[int, int]]]:
    """Escape each formatted result once and join them.

    Returns ``(joined, spans)`` with ``spans[i]`` the ``(start, end)`` of the
    i-th result inside ``joined``. Escaping is idempotent, so wrapping
    ``joined`` in ``untrusted_context_message`` leaves it byte-identical in
    the common case (checked by ``annotate``, never assumed).
    """
    parts = [_escape_guard_markers("" if t is None else str(t)) for t in texts]
    spans: List[Tuple[int, int]] = []
    pos = 0
    for i, part in enumerate(parts):
        if i:
            pos += len(JOINER)
        spans.append((pos, pos + len(part)))
        pos += len(part)
    return JOINER.join(parts), spans


def annotate(
    message: Dict[str, Any],
    joined: str,
    spans: Sequence[Tuple[int, int]],
    *,
    round_num: int,
    tools: Sequence[str],
    call_ids: Sequence[str],
) -> bool:
    """Stamp segment offsets on a freshly built fenced results message.
    Returns False (and stamps only the round) when the body is not found
    verbatim in the content — then the message stays one opaque unit."""
    message[ROUND_KEY] = int(round_num or 0)
    content = message.get("content")
    if not isinstance(content, str) or not joined:
        return False
    base = content.find(joined)
    if base < 0:
        return False
    segs = []
    for j, (s, e) in enumerate(spans):
        start, end = base + s, base + e
        segs.append({
            "handle": f"r{int(round_num or 0)}.{j}",
            "tool": str(tools[j] if j < len(tools) else "") or "",
            "call_id": str(call_ids[j] if j < len(call_ids) else "") or "",
            "start": start,
            "end": end,
            "sha": _sha(content[start:end]),
        })
    message[SEGMENTS_KEY] = segs
    return True


def is_fenced_results(message: Any) -> bool:
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    meta = message.get("metadata")
    if not isinstance(meta, dict):
        return False
    return str(meta.get("source") or "") == FENCED_RESULTS_SOURCE and meta.get("trusted") is False


def segments(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Valid segments of a fenced results message, or [] when unknown/stale."""
    segs = message.get(SEGMENTS_KEY) if isinstance(message, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(segs, list) or not segs or not isinstance(content, str):
        return []
    out = []
    for seg in segs:
        if not isinstance(seg, dict):
            return []
        try:
            start, end = int(seg["start"]), int(seg["end"])
        except (KeyError, TypeError, ValueError):
            return []
        if not (0 <= start <= end <= len(content)):
            return []
        if _sha(content[start:end]) != seg.get("sha"):
            return []
        out.append(seg)
    return out


def segment_text(message: Dict[str, Any], seg: Dict[str, Any]) -> str:
    return str(message.get("content") or "")[int(seg["start"]):int(seg["end"])]


def replace_segment(message: Dict[str, Any], index: int, new_text: str) -> Dict[str, Any]:
    """A copy of ``message`` with segment ``index`` replaced by ``new_text``
    (escaped, so a stub can never close the guard). Later offsets shift."""
    segs = segments(message)
    if not segs or not (0 <= index < len(segs)):
        raise ValueError("segment not found")
    new_text = _escape_guard_markers(new_text or "")
    content = str(message.get("content") or "")
    seg = segs[index]
    start, end = int(seg["start"]), int(seg["end"])
    new_content = content[:start] + new_text + content[end:]
    delta = len(new_text) - (end - start)
    new_segs = []
    for j, s in enumerate(segs):
        s2 = dict(s)
        if j == index:
            s2["end"] = start + len(new_text)
            s2["sha"] = _sha(new_text)
        elif j > index:
            s2["start"] = int(s["start"]) + delta
            s2["end"] = int(s["end"]) + delta
        new_segs.append(s2)
    out = dict(message)
    out["content"] = new_content
    out[SEGMENTS_KEY] = new_segs
    return out


def body_span(message: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """(start, end) of the guarded body of an untrusted message: after the
    ``Source: <label>`` line, up to the closing guard. None if not found."""
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return None
    marker = "\nSource: "
    i = content.find(marker)
    if i < 0:
        return None
    nl = content.find("\n", i + len(marker))
    if nl < 0:
        return None
    start = nl + 1
    end = content.rfind("\n" + GUARD_CLOSE)
    if end < start:
        return None
    return start, end


def replace_body(message: Dict[str, Any], new_body: str) -> Dict[str, Any]:
    """A copy of ``message`` with its whole guarded body replaced (used when
    the per-result offsets are unknown). Segment offsets are dropped."""
    span = body_span(message)
    if span is None:
        raise ValueError("no guarded body")
    content = str(message.get("content") or "")
    out = dict(message)
    out["content"] = content[:span[0]] + _escape_guard_markers(new_body or "") + content[span[1]:]
    out.pop(SEGMENTS_KEY, None)
    return out
