"""Canonical message history and one projection per wire protocol (H12).

Every renderer that turns the conversation into a provider request used to
repair the history on its own: ``llm_core._sanitize_llm_messages`` pruned
unanswered tool calls, ``context_compactor._sanitize_tool_messages`` did it
again after trimming, and the Anthropic and native-chat builders re-read the
OpenAI-shaped list with their own assumptions. Each repair edited the list it
was handed, so a call whose result had not been recorded simply stopped
existing for the next model round.

This module keeps the causal record and renders from it:

* ``build_canonical`` reads an OpenAI-style message list once and produces a
  ``CanonicalHistory``: plain messages, and *exchanges* (one assistant turn
  with its tool calls, real call ids, and the result matched to each call
  together with how certain that result is). Nothing is removed from this
  form. A call with no recorded result keeps its place and carries an explicit
  "outcome unknown" result; a result whose call is not in the history is kept
  as an orphan entry.
* Every repair is written down as a ``Repair`` receipt: what changed, where,
  and why. The receipts travel with the canonical history and are kept in a
  bounded in-process log (``recent_receipts``).
* One function per protocol renders the canonical history: ``render_openai_chat``,
  ``render_anthropic``, ``render_ollama`` and ``render_text_fence`` (for models
  that receive tools as fenced blocks in text instead of native tool calls).
  ``project`` is the single entry point.

The input list is never modified. Rendering is idempotent: projecting a
projection gives the same messages and no further repairs.

Intended differences from the previous repair, all visible as receipts:

* an unanswered call is projected as a call plus an explicit unknown result
  (it used to be deleted);
* a result that sits after other messages but before the next assistant turn is
  matched to its call and moved next to it (it used to be dropped as an orphan,
  which also made its call look unanswered);
* a call without an id gets a synthetic id and an unknown result (it used to be
  deleted).

``llm_projection_mode`` selects ``canonical`` (default), ``legacy`` (the
previous code, kept for rollback) or ``shadow`` (legacy output is sent, the
canonical projection is computed beside it and any difference is recorded).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

PROTOCOLS: Tuple[str, ...] = ("openai_chat", "anthropic", "ollama", "text_fence")
MODES: Tuple[str, ...] = ("canonical", "legacy", "shadow")
DEFAULT_MODE = "canonical"

#: Text of the result projected for a call whose outcome was never recorded.
OMITTED_RESULT_TEXT = (
    "[The result of this tool call is not part of this context window; "
    "its content is not available here.]"
)

UNKNOWN_RESULT_TEXT = (
    "[No result was recorded for this tool call. It may or may not have run; "
    "its outcome is unknown. Check the current state before repeating it.]"
)

#: Synthetic assistant separator / legacy marker. The literals live in
#: ``llm_core``; they are injected so this module has no import cycle.
BOUNDARY = "<<faustus_ctx_ack>>"
LEGACY_BOUNDARY = "Reference context received."
BOUNDARY_ALIASES = (BOUNDARY, LEGACY_BOUNDARY)

_ALLOWED_KEYS = frozenset({
    "role", "content", "name", "tool_call_id", "tool_calls", "function_call",
    "reasoning_content",
})
_STRICT_ALTERNATION_PROVIDERS = frozenset({"anthropic"})
#: Fields a protocol's own renderer reads besides the common ones.
_PROTOCOL_EXTRA_KEYS = {"ollama": ("images", "tool_name")}
_ARGS_PREVIEW_CHARS = 160

# Receipt kinds (stable identifiers; tests and diagnostics match on them).
R_UNKNOWN_RESULT_ADDED = "unknown_result_added"
R_CALL_ID_ASSIGNED = "call_id_assigned"
R_RESULT_MOVED = "result_moved_next_to_call"
R_ORPHAN_RESULT = "orphan_result_not_projected"
R_DUPLICATE_RESULT = "duplicate_result_not_projected"
R_MALFORMED_CALL = "malformed_call_not_projected"
R_LEGACY_MARKER = "legacy_marker_rewritten"
R_MESSAGE_OMITTED = "message_omitted"
R_USERS_MERGED = "user_messages_merged"
R_BOUNDARY_INSERTED = "boundary_inserted"
R_SHADOW_DIFFERENCE = "shadow_difference"


@dataclass
class Repair:
    """One change made while projecting, with the reason for it."""

    kind: str
    reason: str
    call_id: str = ""
    tool: str = ""
    index: int = -1
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"kind": self.kind, "reason": self.reason}
        if self.call_id:
            out["call_id"] = self.call_id
        if self.tool:
            out["tool"] = self.tool
        if self.index >= 0:
            out["index"] = self.index
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


@dataclass
class CanonicalResult:
    call_id: str
    message: Dict[str, Any]
    #: "recorded" for a stored result (or the producer's own status when the
    #: message carried one, for example "partial" or "outcome_unknown");
    #: "unknown" for the explicit placeholder of a call with no result.
    certainty: str = "recorded"
    synthetic: bool = False


@dataclass
class CanonicalCall:
    id: str
    name: str
    raw: Dict[str, Any]
    synthetic_id: bool = False
    result: Optional[CanonicalResult] = None

    def arguments_text(self) -> str:
        fn = self.raw.get("function") if isinstance(self.raw, dict) else None
        args = (fn or {}).get("arguments")
        if isinstance(args, str):
            return args
        try:
            return json.dumps(args if args is not None else {}, ensure_ascii=False)
        except (TypeError, ValueError):
            return "{}"


@dataclass
class Plain:
    message: Dict[str, Any]
    source_index: int = -1


@dataclass
class Exchange:
    """One assistant turn, its calls, and the result matched to each call."""

    assistant: Dict[str, Any]          # cleaned message without ``tool_calls``
    calls: List[CanonicalCall]
    results: List[CanonicalResult]     # emission order (answered first)
    source_index: int = -1


@dataclass
class Orphan:
    """A tool result whose call is not in the history. Kept, not projected."""

    message: Dict[str, Any]
    source_index: int = -1
    duplicate: bool = False


Entry = Union[Plain, Exchange, Orphan]


@dataclass
class CanonicalHistory:
    entries: List[Entry]
    repairs: List[Repair] = field(default_factory=list)

    def calls(self) -> List[CanonicalCall]:
        return [c for e in self.entries if isinstance(e, Exchange) for c in e.calls]

    def to_dict(self) -> Dict[str, Any]:
        """Stable, JSON-safe summary (no message bodies)."""
        rows: List[Dict[str, Any]] = []
        for entry in self.entries:
            if isinstance(entry, Plain):
                rows.append({"kind": "message", "role": entry.message.get("role"),
                             "source_index": entry.source_index})
            elif isinstance(entry, Orphan):
                rows.append({"kind": "orphan_result",
                             "call_id": str(entry.message.get("tool_call_id") or ""),
                             "duplicate": entry.duplicate, "source_index": entry.source_index})
            else:
                rows.append({
                    "kind": "exchange", "source_index": entry.source_index,
                    "calls": [{
                        "id": c.id, "name": c.name, "synthetic_id": c.synthetic_id,
                        "certainty": c.result.certainty if c.result else "unknown",
                        "result_synthetic": bool(c.result and c.result.synthetic),
                    } for c in entry.calls],
                })
        return {"entries": rows, "repairs": [r.to_dict() for r in self.repairs]}


@dataclass
class Projection:
    messages: List[Dict[str, Any]]
    receipts: List[Repair]
    protocol: str = "openai_chat"
    #: Anthropic only: system text parts in order (messages carry no system).
    system_parts: List[str] = field(default_factory=list)
    canonical: Optional[CanonicalHistory] = None

    def receipt_dicts(self) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self.receipts]


# ── receipts log ─────────────────────────────────────────────────────────────

_LOG_LOCK = threading.Lock()
_LOG: Deque[Dict[str, Any]] = deque(maxlen=200)
_COUNTS: Dict[str, int] = {}


def _record_receipts(protocol: str, repairs: Sequence[Repair]) -> None:
    if not repairs:
        return
    now = time.time()
    with _LOG_LOCK:
        for repair in repairs:
            _COUNTS[repair.kind] = _COUNTS.get(repair.kind, 0) + 1
            row = repair.to_dict()
            row["protocol"] = protocol
            row["at"] = now
            _LOG.append(row)
    kinds: Dict[str, int] = {}
    for repair in repairs:
        kinds[repair.kind] = kinds.get(repair.kind, 0) + 1
    logger.info("[history_projection] %s repaired the prompt: %s", protocol, kinds)


def recent_receipts(limit: int = 50) -> List[Dict[str, Any]]:
    """Most recent repair receipts, newest last."""
    with _LOG_LOCK:
        rows = list(_LOG)
    return rows[-max(0, int(limit)):] if limit else []


def receipt_counts() -> Dict[str, int]:
    with _LOG_LOCK:
        return dict(_COUNTS)


def reset_receipts_for_tests() -> None:
    with _LOG_LOCK:
        _LOG.clear()
        _COUNTS.clear()


# ── settings ─────────────────────────────────────────────────────────────────

def projection_mode() -> str:
    try:
        from src.settings import get_setting
        raw = str(get_setting("llm_projection_mode", DEFAULT_MODE) or DEFAULT_MODE).strip().lower()
    except Exception:  # noqa: BLE001 - a settings hiccup keeps the default
        return DEFAULT_MODE
    return raw if raw in MODES else DEFAULT_MODE


def fence_history_without_tools() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("llm_fence_history_without_tools", False))
    except Exception:  # noqa: BLE001
        return False


# ── helpers ──────────────────────────────────────────────────────────────────

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _has_content(content: Any) -> bool:
    if isinstance(content, list):
        return bool(content)
    return bool(_text_of(content).strip())


def _digest(text: Any) -> str:
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()[:12]


def _certainty_of(msg: Dict[str, Any]) -> str:
    meta = msg.get("metadata")
    status = None
    if isinstance(meta, dict):
        status = meta.get("effect_status")
    status = status or msg.get("_effect_status")
    if isinstance(status, str) and status.strip():
        return status.strip()
    if _text_of(msg.get("content")) == UNKNOWN_RESULT_TEXT:
        return "unknown"
    return "recorded"


def _clean(messages: Sequence[Any], repairs: List[Repair],
           extra_keys: Sequence[str] = (),
           keep_empty_system: bool = False) -> List[Tuple[int, Dict[str, Any], str]]:
    """Step 1: keep only wire fields, drop messages no provider could use.

    Returns ``(source_index, message, certainty)`` rows. Dropped messages are
    receipted; none of them is a call or a result with content.
    """
    cleaned: List[Tuple[int, Dict[str, Any], str]] = []
    allowed = _ALLOWED_KEYS | frozenset(extra_keys)
    for idx, msg in enumerate(messages or []):
        if not isinstance(msg, dict):
            continue
        item = {k: v for k, v in msg.items() if k in allowed and v is not None}
        role = item.get("role")
        if not role:
            repairs.append(Repair(R_MESSAGE_OMITTED, "message has no role", index=idx))
            continue
        certainty = _certainty_of(msg) if role == "tool" else "recorded"
        if role == "assistant":
            content = item.get("content")
            text = _text_of(content).strip()
            if isinstance(content, str) and text in BOUNDARY_ALIASES and not item.get("tool_calls"):
                if content != BOUNDARY:
                    repairs.append(Repair(
                        R_LEGACY_MARKER, "context boundary text normalised so a model does not learn to echo it",
                        index=idx))
                item = dict(item)
                item["content"] = BOUNDARY
            if "content" not in item and item.get("tool_calls"):
                item["content"] = None
            if "content" in item or item.get("tool_calls"):
                cleaned.append((idx, item, certainty))
            else:
                repairs.append(Repair(R_MESSAGE_OMITTED, "assistant message has no content and no tool calls", index=idx))
        elif role == "tool":
            if "content" in item and "tool_call_id" in item:
                cleaned.append((idx, item, certainty))
            else:
                repairs.append(Repair(
                    R_MESSAGE_OMITTED, "tool message has no content or no tool_call_id", index=idx,
                    call_id=str(msg.get("tool_call_id") or "")))
        elif "content" in item:
            cleaned.append((idx, item, certainty))
        elif keep_empty_system and role == "system":
            # The Messages API gets system text from a separate field; an empty
            # system message is joined as empty text, as before.
            cleaned.append((idx, dict(item, content=""), certainty))
        else:
            repairs.append(Repair(R_MESSAGE_OMITTED, f"{role} message has no content", index=idx))
    return cleaned


def _unknown_result(call: CanonicalCall, text: str = UNKNOWN_RESULT_TEXT) -> CanonicalResult:
    return CanonicalResult(
        call_id=call.id,
        message={"role": "tool", "tool_call_id": call.id, "content": text},
        certainty="unknown", synthetic=True,
    )


def build_canonical(messages: Sequence[Any], extra_keys: Sequence[str] = (),
                    keep_empty_system: bool = False) -> CanonicalHistory:
    """Read an OpenAI-style message list into the canonical form.

    Pure: ``messages`` is not modified and no I/O happens. ``extra_keys`` are
    wire fields a particular protocol carries besides the common ones.
    """
    repairs: List[Repair] = []
    cleaned = _clean(messages, repairs, extra_keys, keep_empty_system)
    return _canonicalize(cleaned, repairs)


def _canonicalize(cleaned: List[Tuple[int, Dict[str, Any], str]], repairs: List[Repair],
                  missing_result_text: str = UNKNOWN_RESULT_TEXT) -> CanonicalHistory:
    """Match results to calls and build the entries (see ``build_canonical``)."""
    # Pass A: match results to calls. A result belongs to the nearest
    # preceding assistant turn that asked for its id, as long as no other
    # assistant turn sits in between.
    consumed: Dict[int, int] = {}        # cleaned position of a tool msg -> assistant position
    matches: Dict[int, List[int]] = {}   # assistant position -> matched tool positions (in order)
    for pos, (_src, msg, _c) in enumerate(cleaned):
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        expected = {
            str(tc.get("id")) for tc in msg["tool_calls"]
            if isinstance(tc, dict) and tc.get("id")
        }
        answered: List[str] = []
        found: List[int] = []
        j = pos + 1
        while j < len(cleaned) and cleaned[j][1].get("role") != "assistant":
            other = cleaned[j][1]
            if other.get("role") == "tool" and j not in consumed:
                tid = str(other.get("tool_call_id") or "")
                if tid in expected and tid not in answered:
                    answered.append(tid)
                    found.append(j)
                    consumed[j] = pos
            j += 1
        matches[pos] = found

    # A result that no window claimed can still belong to an earlier call that
    # is unanswered (it arrived after a later assistant turn). Attach it to the
    # nearest such call rather than treating it as an orphan and then
    # reporting the call as unknown.
    call_ids_at: Dict[int, set] = {
        pos: {str(tc.get("id")) for tc in msg["tool_calls"] if isinstance(tc, dict) and tc.get("id")}
        for pos, (_s, msg, _c) in enumerate(cleaned)
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    }
    for j, (_src, msg, _c) in enumerate(cleaned):
        if msg.get("role") != "tool" or j in consumed:
            continue
        tid = str(msg.get("tool_call_id") or "")
        for pos in sorted((p for p in call_ids_at if p < j), reverse=True):
            answered_here = {str(cleaned[k][1].get("tool_call_id") or "") for k in matches.get(pos, [])}
            if tid in call_ids_at[pos] and tid not in answered_here:
                consumed[j] = pos
                matches.setdefault(pos, []).append(j)
                matches[pos].sort()
                break

    # Pass B: emit in source order.
    entries: List[Entry] = []
    seen_answered: Dict[str, int] = {}
    for pos, (src, msg, _cert) in enumerate(cleaned):
        role = msg.get("role")
        if role == "tool":
            if pos in consumed:
                continue
            tid = str(msg.get("tool_call_id") or "")
            duplicate = tid in seen_answered
            entries.append(Orphan(message=msg, source_index=src, duplicate=duplicate))
            repairs.append(Repair(
                R_DUPLICATE_RESULT if duplicate else R_ORPHAN_RESULT,
                ("a second result for a call that already has one" if duplicate
                 else "the result's call is not in this history, so no provider accepts it as a tool message"),
                call_id=tid, index=src,
                detail={"content_sha": _digest(_text_of(msg.get("content"))),
                        "content_chars": len(_text_of(msg.get("content")))}))
            continue
        tool_calls = msg.get("tool_calls") if role == "assistant" else None
        if not tool_calls:
            entries.append(Plain(message=msg, source_index=src))
            continue

        calls: List[CanonicalCall] = []
        for n, tc in enumerate(tool_calls):
            if not isinstance(tc, dict):
                repairs.append(Repair(R_MALFORMED_CALL, "tool call entry is not an object", index=src,
                                      detail={"position": n}))
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = str((fn or {}).get("name") or "")
            call_id = str(tc.get("id")) if tc.get("id") else ""
            synthetic = False
            raw = dict(tc)
            if not call_id:
                call_id = f"faustus_noid_{src}_{n}"
                raw["id"] = call_id
                synthetic = True
                repairs.append(Repair(
                    R_CALL_ID_ASSIGNED, "the call had no id, so its result could not be matched",
                    call_id=call_id, tool=name, index=src,
                    detail={"arguments_preview": str(
                        (fn or {}).get("arguments") or "")[:_ARGS_PREVIEW_CHARS]}))
            calls.append(CanonicalCall(id=call_id, name=name, raw=raw, synthetic_id=synthetic))

        assistant = {k: v for k, v in msg.items() if k != "tool_calls"}
        if not calls:
            # Every entry was malformed: nothing real was asked for.
            if _has_content(assistant.get("content")):
                entries.append(Plain(message=assistant, source_index=src))
            else:
                repairs.append(Repair(R_MESSAGE_OMITTED,
                                      "assistant message had only malformed tool calls and no content", index=src))
            continue

        by_id = {c.id: c for c in calls if not c.synthetic_id}
        results: List[CanonicalResult] = []
        for tpos in matches.get(pos, []):
            tmsg = cleaned[tpos][1]
            tid = str(tmsg.get("tool_call_id") or "")
            call = by_id.get(tid)
            if call is None or call.result is not None:
                continue
            result = CanonicalResult(call_id=tid, message=tmsg, certainty=cleaned[tpos][2])
            call.result = result
            results.append(result)
            seen_answered[tid] = src
            if any(cleaned[k][1].get("role") != "tool" for k in range(pos + 1, tpos)
                   if k not in consumed):
                repairs.append(Repair(
                    R_RESULT_MOVED, "the result was separated from its call by other messages",
                    call_id=tid, tool=call.name, index=cleaned[tpos][0]))
        for call in calls:
            if call.result is None:
                placeholder = _unknown_result(call, missing_result_text)
                call.result = placeholder
                results.append(placeholder)
                repairs.append(Repair(
                    R_UNKNOWN_RESULT_ADDED,
                    "the call has no recorded result; it stays in the history with an explicit unknown outcome",
                    call_id=call.id, tool=call.name, index=src,
                    detail={"arguments_preview": call.arguments_text()[:_ARGS_PREVIEW_CHARS]}))
        entries.append(Exchange(assistant=assistant, calls=calls, results=results, source_index=src))
    return CanonicalHistory(entries=entries, repairs=repairs)


# ── renderers ────────────────────────────────────────────────────────────────

def _exchange_assistant_message(exchange: Exchange) -> Dict[str, Any]:
    msg = dict(exchange.assistant)
    msg["tool_calls"] = [c.raw for c in exchange.calls]
    if "content" not in msg:
        msg["content"] = None
    return msg


def _flatten_openai(history: CanonicalHistory) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for entry in history.entries:
        if isinstance(entry, Plain):
            out.append(entry.message)
        elif isinstance(entry, Exchange):
            out.append(_exchange_assistant_message(entry))
            out.extend(r.message for r in entry.results)
        # Orphans are never projected; their receipt carries a digest.
    return out


def _as_blocks(content: Any) -> List[Any]:
    if isinstance(content, list):
        return content
    if content:
        return [{"type": "text", "text": str(content)}]
    return []


def _is_untrusted_context(content: Any) -> bool:
    if isinstance(content, str):
        return (content.startswith("UNTRUSTED SOURCE DATA\n")
                or "<<<UNTRUSTED_SOURCE_DATA>>>" in content)
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "text"
                   and _is_untrusted_context(b.get("text") or "") for b in content)
    return False


def _merge_untrusted_and_user(last: Dict[str, Any], item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    lc, ic = last.get("content"), item.get("content")
    if isinstance(lc, list) or isinstance(ic, list):
        return None
    last_str = str(lc) if lc is not None else ""
    item_str = str(ic) if ic is not None else ""
    if not last_str and not item_str:
        return None
    merged = dict(last)
    merged["content"] = (f"{last_str}\n\n--- Your message ---\n\n{item_str}" if item_str else last_str)
    return merged


def _merge_adjacent_users(messages: List[Dict[str, Any]], provider: Optional[str],
                          repairs: List[Repair]) -> List[Dict[str, Any]]:
    """Strict role alternation: fold consecutive user messages together."""
    merged: List[Dict[str, Any]] = []
    for item in messages:
        if not merged:
            merged.append(item)
            continue
        last = merged[-1]
        if not (last.get("role") == "user" and item.get("role") == "user"):
            merged.append(item)
            continue
        if _is_untrusted_context(last.get("content")):
            folded = _merge_untrusted_and_user(last, item)
            if folded is not None:
                merged[-1] = folded
                repairs.append(Repair(R_USERS_MERGED, "untrusted context and the user turn folded into one message"))
                continue
            if provider in _STRICT_ALTERNATION_PROVIDERS:
                merged.append({"role": "assistant", "content": BOUNDARY})
                repairs.append(Repair(R_BOUNDARY_INSERTED,
                                      "strict alternation needs a separator between two user messages"))
            merged.append(item)
            continue
        last_copy = dict(last)
        lc, ic = last_copy.get("content"), item.get("content")
        if isinstance(lc, list) or isinstance(ic, list):
            blocks = _as_blocks(lc) + _as_blocks(ic)
            if blocks:
                last_copy["content"] = blocks
            else:
                last_copy.pop("content", None)
        else:
            last_str = str(lc) if lc is not None else ""
            item_str = str(ic) if ic is not None else ""
            new_content = "\n\n".join(p for p in (last_str, item_str) if p)
            if new_content:
                last_copy["content"] = new_content
            else:
                last_copy.pop("content", None)
        merged[-1] = last_copy
        repairs.append(Repair(R_USERS_MERGED, "consecutive user messages folded into one"))
    return merged


def render_openai_chat(history: CanonicalHistory, *, provider: Optional[str] = None,
                       merge_users: bool = True) -> Tuple[List[Dict[str, Any]], List[Repair]]:
    flat = _flatten_openai(history)
    extra: List[Repair] = []
    if merge_users:
        flat = _merge_adjacent_users(flat, provider, extra)
    return flat, extra


def render_ollama(history: CanonicalHistory) -> List[Dict[str, Any]]:
    """Native chat shape: the repaired list with call arguments as objects and
    images split out of block content."""
    from src import llm_core
    return llm_core._ollama_normalize_messages(_flatten_openai(history))


def render_anthropic(history: CanonicalHistory) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Messages API shape: ``(system_parts, messages)``.

    Each result becomes its own user message with one ``tool_result`` block, as
    before; the API combines consecutive user messages.
    """
    from src import llm_core
    system_parts: List[str] = []
    chat: List[Dict[str, Any]] = []

    def assistant_blocks(msg: Dict[str, Any], calls: List[Dict[str, Any]]) -> Dict[str, Any]:
        content = list(llm_core._anthropic_thinking_from_tool_calls(calls))
        if msg.get("content"):
            if isinstance(msg["content"], list):
                content.extend(llm_core._convert_openai_content_to_anthropic(msg["content"]))
            else:
                content.append({"type": "text", "text": msg["content"]})
        for tc in calls:
            fn = tc.get("function") or {}
            args_str = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except (json.JSONDecodeError, TypeError):
                args = {}
            content.append({"type": "tool_use", "id": tc.get("id", ""),
                            "name": fn.get("name", ""), "input": args})
        return {"role": "assistant", "content": content}

    for entry in history.entries:
        if isinstance(entry, Plain):
            m = entry.message
            if m.get("role") == "system":
                system_parts.append(m.get("content") or "")
            elif m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list):
                chat.append(assistant_blocks(m, list(m["tool_calls"])))
            else:
                chat.append({"role": m["role"],
                             "content": llm_core._convert_openai_content_to_anthropic(m["content"])})
        elif isinstance(entry, Exchange):
            chat.append(assistant_blocks(entry.assistant, [c.raw for c in entry.calls]))
            for r in entry.results:
                chat.append({"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": r.message.get("tool_call_id", ""),
                    "content": r.message.get("content", ""),
                }]})
    return system_parts, chat


def _fence_text(call: CanonicalCall) -> str:
    args = call.arguments_text().strip() or "{}"
    return f"```{call.name}\n{args}\n```"


def render_text_fence(history: CanonicalHistory) -> List[Dict[str, Any]]:
    """For models that receive tools as fenced blocks in text.

    Each exchange becomes an assistant message (its prose plus one fenced block
    per call) followed by ONE user message holding every result inside the
    untrusted-context wrapper. A call without a recorded result appears there
    as an explicit unknown outcome, never as silence.
    """
    from src.prompt_security import untrusted_context_message
    out: List[Dict[str, Any]] = []
    for entry in history.entries:
        if isinstance(entry, Plain):
            out.append(entry.message)
            continue
        if not isinstance(entry, Exchange):
            continue
        fences = "\n\n".join(_fence_text(c) for c in entry.calls)
        content = entry.assistant.get("content")
        msg = {k: v for k, v in entry.assistant.items() if k not in ("content", "tool_calls")}
        if isinstance(content, list):
            msg["content"] = list(content) + [{"type": "text", "text": fences}]
        else:
            text = str(content).rstrip() if content else ""
            msg["content"] = f"{text}\n\n{fences}" if text else fences
        out.append(msg)
        parts = []
        for call in entry.calls:
            res = call.result
            body = _text_of(res.message.get("content")) if res else UNKNOWN_RESULT_TEXT
            status = res.certainty if res else "unknown"
            header = f"[{call.name} result, call {call.id}"
            header += "]" if status == "recorded" else f", status: {status}]"
            parts.append(f"{header}\n{body}")
        wrapped = untrusted_context_message("tool execution results", "\n\n".join(parts),
                                            arm_tool_gate=False)
        out.append({"role": "user", "content": wrapped["content"]})
    return out


def repair_tool_pairs(messages: Sequence[Any], *, missing_result_text: str = OMITTED_RESULT_TEXT
                      ) -> Tuple[List[Dict[str, Any]], List[Repair]]:
    """Repair only tool-call adjacency on a list, keeping every message field.

    Used after the history is trimmed to fit a window: results whose call was
    cut are dropped with a receipt, and a call whose result was cut keeps its
    place with ``missing_result_text`` instead of disappearing. Private
    bookkeeping keys on messages (for example the round a result came from)
    are preserved; nothing else is touched.
    """
    repairs: List[Repair] = []
    rows = [(idx, msg, _certainty_of(msg) if msg.get("role") == "tool" else "recorded")
            for idx, msg in enumerate(messages or []) if isinstance(msg, dict)]
    out = _flatten_openai(_canonicalize(rows, repairs, missing_result_text))
    _record_receipts("context_trim", repairs)
    return out, repairs


# ── entry points ─────────────────────────────────────────────────────────────

def project(messages: Sequence[Any], protocol: str = "openai_chat", *,
            provider: Optional[str] = None, record: bool = True) -> Projection:
    """Project a message list for one wire protocol.

    ``openai_chat`` also applies strict-alternation merging for ``provider``;
    the other protocols render exactly the repaired exchanges.
    """
    if protocol not in PROTOCOLS:
        raise ValueError(f"unknown protocol {protocol!r}; expected one of {PROTOCOLS}")
    history = build_canonical(messages, _PROTOCOL_EXTRA_KEYS.get(protocol, ()),
                              keep_empty_system=(protocol == "anthropic"))
    repairs = list(history.repairs)
    system_parts: List[str] = []
    if protocol == "openai_chat":
        rendered, extra = render_openai_chat(history, provider=provider)
        repairs.extend(extra)
    elif protocol == "ollama":
        rendered = render_ollama(history)
    elif protocol == "anthropic":
        system_parts, rendered = render_anthropic(history)
    else:
        rendered = render_text_fence(history)
    if record:
        _record_receipts(protocol, repairs)
    return Projection(messages=rendered, receipts=repairs, protocol=protocol,
                      system_parts=system_parts, canonical=history)


def compare_shadow(legacy: Sequence[Dict[str, Any]], canonical: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """None when equal; otherwise where they first differ."""
    if list(legacy) == list(canonical):
        return None
    n = min(len(legacy), len(canonical))
    first = next((i for i in range(n) if legacy[i] != canonical[i]), n)
    return {"legacy_len": len(legacy), "canonical_len": len(canonical), "first_difference": first,
            "legacy_role": (legacy[first].get("role") if first < len(legacy) else None),
            "canonical_role": (canonical[first].get("role") if first < len(canonical) else None)}


def record_shadow_difference(diff: Dict[str, Any]) -> None:
    _record_receipts("openai_chat", [Repair(
        R_SHADOW_DIFFERENCE, "the canonical projection differs from the legacy one", detail=diff)])
