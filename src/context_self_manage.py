"""The model managing its own context during a long agent run.

Mid-turn pressure (``context_compactor.apply_midturn_pressure``) keeps a turn
under the soft ceiling with fixed rules: old/large results spill, history
folds. Those rules cannot know which result the model still needs and which
one it is done with. The five context tools let the model say so itself:

    context_status   used/window/% plus the largest items still in context,
                     each with a stable handle, and what is pinned/spilled
    context_pin      never fold/spill these items (compaction_pins, model pins
    context_unpin    capped per session by ``agent_context_tools_max_pins``)
    context_drop     move results out: body to context_overflow, the standard
                     overflow stub in its place (read_overflow restores it)
    context_note     replace a set of results with the model's own summary;
                     originals go to overflow, the note lists their ids

Tool handlers never see ``messages``: each returns an *intent*
(``{"context_intent": {...}}``) and the agent loop applies it right after
the call, through ``apply_intent`` below, before the round's own results are
appended — so a handle can only name a result the model has already read.

Handles:
    native routes   the tool_call_id of the result (``call_...``)
    fenced routes   ``r<round>.<i>``: the i-th result of that round inside the
                    one "tool execution results" message
                    (src/tool_result_segments.py); ``r<round>`` names them all
    anything else   ``m<8 hex>``: a fenced results message whose per-result
                    offsets are unknown (built before segments existed)

What is never touched: the user's messages, system messages, the assistant's
own turns, and pinned items (unpin first). ``context_drop`` refuses a result
that carries a pending approval id; ``context_note`` keeps those ids (and
constraints/sources) in the note, the way ``CompactionPreserve`` does.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from src import tool_result_segments as trs

logger = logging.getLogger(__name__)

CONTEXT_TOOL_NAMES: Tuple[str, ...] = (
    "context_status", "context_pin", "context_unpin", "context_drop", "context_note",
)
INTENT_KEY = "context_intent"
MODEL_PIN_PREFIX = "[model] "
DEFAULT_MAX_PINS = 20
DEFAULT_OFFER_PCT = 0.45
DEFAULT_OFFER_ROUND = 12
MAX_NOTE_CHARS = 4000
MIN_SNIPPET_CHARS = 12
STATUS_TOP_N = 8
_DROP_HEAD_CHARS = 160
_HANDLE_ROUND_RE = re.compile(r"^r(\d+)$")

# The user naming the thing: "free up your context", "libera contexto",
# "context window", or a tool name. Narrow on purpose — this is what adds the
# tools on round 1, before any usage threshold.
_USER_ASK_RE = re.compile(
    r"\bcontext_(?:status|pin|unpin|drop|note)\b"
    r"|\b(?:free|manage|clean|trim|check|compact|reduce|watch)\s+(?:up\s+)?(?:your\s+|the\s+|its\s+)?(?:own\s+)?context\b"
    r"|\bcontext\s+window\b"
    r"|\b(?:libera|liberar|gestiona|gestionar|limpia|limpiar|revisa|revisar|compacta|compactar|reduce|reducir|vigila|vigilar)\s+(?:el\s+|tu\s+|su\s+)?(?:propio\s+)?contexto\b"
    r"|\bventana\s+de\s+contexto\b",
    re.IGNORECASE,
)


def user_asked(text: str) -> bool:
    return bool(_USER_ASK_RE.search(text or ""))


def offer_decision(*, enabled: bool, used_tokens: int, context_length: int,
                   round_num: int, offer_pct: float = DEFAULT_OFFER_PCT,
                   offer_round: int = DEFAULT_OFFER_ROUND,
                   asked: bool = False) -> bool:
    """Whether the context tools belong in this round's tool set."""
    if not enabled:
        return False
    if asked:
        return True
    if offer_round > 0 and round_num >= offer_round:
        return True
    if context_length > 0 and offer_pct > 0 and used_tokens >= offer_pct * context_length:
        return True
    return False


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


def offer_note(used_tokens: int, context_length: int) -> str:
    """One-time note for a fenced-call route, whose system prompt was built
    before the tools were offered: the call syntax, short."""
    pct = f"{(used_tokens / context_length) * 100:.0f}%" if context_length else "?"
    return (
        "[Runtime note — not a new user request] Context "
        f"{pct} used ({_k(used_tokens)}/{_k(context_length)} tokens). You can manage it: "
        "```context_status``` {} lists the largest results with handles; "
        "```context_drop``` {\"handles\": [\"r3.0\"]} moves results out "
        "(read_overflow restores them); "
        "```context_note``` {\"handles\": [...], \"note\": \"what you keep\"} replaces "
        "them with your summary; ```context_pin``` {\"handles\": [...]} keeps one "
        "from being compacted."
    )


def nudge_note(used_tokens: int, context_length: int) -> str:
    pct = f"{(used_tokens / context_length) * 100:.0f}%" if context_length else "?"
    return (
        f"[Runtime note — not a new user request] Context is at {pct} "
        f"({_k(used_tokens)}/{_k(context_length)} tokens). context_status, context_drop "
        "and context_note can free results you no longer need; context_pin keeps "
        "one you do."
    )


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


@dataclass
class Item:
    handle: str
    kind: str                      # "tool" | "segment" | "fenced"
    msg_index: int
    seg_index: int = -1
    tool: str = ""
    call_id: str = ""
    hint: str = ""
    chars: int = 0
    tokens: int = 0
    round: Optional[int] = None
    overflow_id: str = ""
    pinned: bool = False

    def to_dict(self) -> Dict[str, Any]:
        out = {"handle": self.handle, "tool": self.tool, "tokens": self.tokens,
               "chars": self.chars}
        if self.round is not None:
            out["round"] = self.round
        if self.hint:
            out["hint"] = self.hint
        if self.overflow_id:
            out["overflow_id"] = self.overflow_id
        if self.pinned:
            out["pinned"] = True
        return out


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text") or "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return "" if content is None else str(content)


def _tokens(text: str, model: Optional[str]) -> int:
    try:
        from src.token_calibration import estimate_tokens_for
        return max(0, estimate_tokens_for([{"role": "tool", "content": text}], model) - 4)
    except Exception:  # noqa: BLE001
        return int(len(text) * 0.3)


def fingerprint(msg: Dict[str, Any]) -> str:
    from src.context_compactor import _row_fingerprint
    return _row_fingerprint(msg.get("role"), msg.get("content"))


def _overflow_id(text: str) -> str:
    from src.context_compactor import _OVERFLOW_ID_RE
    m = _OVERFLOW_ID_RE.search(text or "")
    return m.group(1) if m else ""


def _call_names(messages: List[Dict[str, Any]]) -> Dict[str, Tuple[str, str]]:
    """tool_call_id -> (function name, short argument preview)."""
    out: Dict[str, Tuple[str, str]] = {}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or ():
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            args = str(fn.get("arguments") or "")
            out[str(tc.get("id") or "")] = (str(fn.get("name") or ""), " ".join(args.split())[:80])
    return out


def _segment_hint(text: str) -> str:
    first = (text or "").lstrip().split("\n", 1)[0]
    if first.startswith("### "):
        first = first[4:]
    return " ".join(first.split())[:80]


def list_items(messages: Sequence[Dict[str, Any]], *, pinned: Iterable[str] = (),
               model: Optional[str] = None) -> List[Item]:
    """Every tool result still in ``messages``, oldest first."""
    pinned = set(pinned or ())
    names = _call_names(list(messages))
    items: List[Item] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            text = _text(msg.get("content"))
            call_id = str(msg.get("tool_call_id"))
            tool, args = names.get(call_id, ("", ""))
            items.append(Item(
                handle=call_id, kind="tool", msg_index=i, tool=tool, call_id=call_id,
                hint=args, chars=len(text), tokens=_tokens(text, model),
                round=msg.get(trs.ROUND_KEY) if isinstance(msg.get(trs.ROUND_KEY), int) else None,
                overflow_id=_overflow_id(text),
                pinned=fingerprint(msg) in pinned,
            ))
            continue
        if not trs.is_fenced_results(msg):
            continue
        is_pinned = fingerprint(msg) in pinned
        rnd = msg.get(trs.ROUND_KEY) if isinstance(msg.get(trs.ROUND_KEY), int) else None
        segs = trs.segments(msg)
        if segs:
            for j, seg in enumerate(segs):
                text = trs.segment_text(msg, seg)
                items.append(Item(
                    handle=str(seg.get("handle") or f"r{rnd or 0}.{j}"), kind="segment",
                    msg_index=i, seg_index=j, tool=str(seg.get("tool") or ""),
                    call_id=str(seg.get("call_id") or ""), hint=_segment_hint(text),
                    chars=len(text), tokens=_tokens(text, model), round=rnd,
                    overflow_id=_overflow_id(text), pinned=is_pinned,
                ))
            continue
        span = trs.body_span(msg)
        text = _text(msg.get("content"))
        body = text[span[0]:span[1]] if span else text
        items.append(Item(
            handle="m" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8],
            kind="fenced", msg_index=i, tool="", hint=_segment_hint(body),
            chars=len(body), tokens=_tokens(body, model), round=rnd,
            overflow_id=_overflow_id(body), pinned=is_pinned,
        ))
    return items


def resolve(items: Sequence[Item], handles: Iterable[str]) -> Tuple[List[Item], List[str]]:
    """Items named by ``handles`` (``r<N>`` expands to every result of round
    N), in context order, deduplicated; plus the handles that matched nothing."""
    by_handle = {it.handle: it for it in items}
    found: Dict[Tuple[int, int], Item] = {}
    missing: List[str] = []
    for raw in handles or ():
        h = str(raw or "").strip().strip("`")
        if not h:
            continue
        if h in by_handle:
            it = by_handle[h]
            found[(it.msg_index, it.seg_index)] = it
            continue
        m = _HANDLE_ROUND_RE.match(h)
        if m:
            rnd = int(m.group(1))
            hits = [it for it in items if it.kind in ("segment", "fenced") and it.round == rnd]
            if hits:
                for it in hits:
                    found[(it.msg_index, it.seg_index)] = it
                continue
        missing.append(h)
    return [found[k] for k in sorted(found)], missing


def find_snippet(messages: Sequence[Dict[str, Any]], snippet: str) -> Tuple[Optional[int], str]:
    """Index of the ONE message containing ``snippet`` (system messages
    excluded), or ``(None, reason)``."""
    snippet = (snippet or "").strip()
    if len(snippet) < MIN_SNIPPET_CHARS:
        return None, f"snippet must be at least {MIN_SNIPPET_CHARS} characters"
    hits = [i for i, m in enumerate(messages)
            if isinstance(m, dict) and m.get("role") != "system"
            and snippet in _text(m.get("content"))]
    if not hits:
        return None, "snippet not found in the current context"
    if len(hits) > 1:
        return None, f"snippet is not unique (found in {len(hits)} messages); quote more of it"
    return hits[0], ""


# ---------------------------------------------------------------------------
# Applying an intent
# ---------------------------------------------------------------------------


@dataclass
class OpContext:
    session_id: str
    owner: str = "system"
    run_id: str = ""
    round_num: int = 0
    durable: bool = True
    model: str = ""
    context_length: int = 0
    soft_pct: float = 0.70
    max_pins: int = DEFAULT_MAX_PINS
    pins: Any = None  # module-like: pin_fragment/unpin_fragment/list_pins (tests inject)


def _pins_api(octx: OpContext):
    if octx.pins is not None:
        return octx.pins
    from src.context_engine import compaction_pins
    return compaction_pins


def _pin_rows(octx: OpContext) -> List[Dict[str, Any]]:
    try:
        return list(_pins_api(octx).list_pins(octx.owner, octx.session_id) or [])
    except Exception:  # noqa: BLE001
        logger.debug("context_self_manage: pins unreadable", exc_info=True)
        return []


def pinned_fingerprints(octx: OpContext) -> Set[str]:
    return {str(r.get("fingerprint") or "") for r in _pin_rows(octx)}


def _estimate(messages: Sequence[Dict[str, Any]], model: str) -> int:
    try:
        from src.token_calibration import estimate_tokens_for
        return int(estimate_tokens_for(list(messages), model))
    except Exception:  # noqa: BLE001
        from src.model_context import estimate_tokens
        return int(estimate_tokens(list(messages)))


def _persist(octx: OpContext, text: str, it: Item) -> Tuple[str, int, bool]:
    """(sha256, bytes, durable) — memory-only digest when overflow is off."""
    nbytes = len(text.encode("utf-8", "replace"))
    if octx.durable:
        try:
            from src import context_overflow
            rec = context_overflow.persist(
                session_id=octx.session_id or "session", content=text, tool=it.tool,
                call_id=it.call_id, role="tool", run_id=octx.run_id,
                round_num=octx.round_num, durable=True,
            )
            return rec["content_sha256"], int(rec.get("bytes") or nbytes), True
        except Exception as e:  # noqa: BLE001
            logger.warning("context_self_manage: overflow persist failed: %s", e)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(), nbytes, False


def _item_text(messages: Sequence[Dict[str, Any]], it: Item) -> str:
    msg = messages[it.msg_index]
    if it.kind == "segment":
        segs = trs.segments(msg)
        return trs.segment_text(msg, segs[it.seg_index]) if it.seg_index < len(segs) else ""
    text = _text(msg.get("content"))
    if it.kind == "fenced":
        span = trs.body_span(msg)
        return text[span[0]:span[1]] if span else text
    return text


def _replace_item(messages: List[Dict[str, Any]], it: Item, new_text: str,
                  meta_extra: Dict[str, Any], octx: OpContext, pinned: Set[str]) -> None:
    """Rewrite one item in place in ``messages`` (list copy owned by caller)."""
    old = messages[it.msg_index]
    if it.kind == "segment":
        new = trs.replace_segment(old, it.seg_index, new_text)
    elif it.kind == "fenced":
        new = trs.replace_body(old, new_text)
    else:
        new = dict(old)
        new["content"] = new_text
        meta = dict(old.get("metadata") or {}) if isinstance(old.get("metadata"), dict) else {}
        meta.update(meta_extra)
        new["metadata"] = meta
    messages[it.msg_index] = new
    # A fenced message whose OTHER result is pinned keeps its pin across the
    # rewrite (the pin identity is the whole message's fingerprint).
    old_fp = fingerprint(old)
    if old_fp in pinned:
        try:
            api = _pins_api(octx)
            excerpt = next((str(r.get("excerpt") or "") for r in _pin_rows(octx)
                            if r.get("fingerprint") == old_fp), MODEL_PIN_PREFIX)
            api.unpin_fragment(octx.owner, octx.session_id, old_fp)
            new_fp = fingerprint(new)
            api.pin_fragment(octx.owner, octx.session_id, new_fp, excerpt)
            pinned.discard(old_fp)
            pinned.add(new_fp)
        except Exception:  # noqa: BLE001
            logger.debug("context_self_manage: pin carry failed", exc_info=True)


def _pending_approvals(text: str) -> List[Dict[str, str]]:
    try:
        from src.context_compactor import _find_pending_approvals
        return _find_pending_approvals(text)
    except Exception:  # noqa: BLE001
        return []


def _stub_line(digest: str, it: Item, nbytes: int, durable: bool) -> str:
    where = "disk" if durable else "memory-only (incognito)"
    return (f"[overflow id={digest} tool={it.tool or 'unknown'} bytes={nbytes}"
            f" call_id={it.call_id or '-'} storage={where}]")


def _result(op: str, *, ok: bool, text: str, **kw) -> Dict[str, Any]:
    out = {"op": op, "ok": ok, "text": text, "handles": [], "tokens_freed": 0,
           "overflow_ids": [], "refused": []}
    out.update(kw)
    return out


def _handles_arg(intent: Dict[str, Any]) -> List[str]:
    raw = intent.get("handles")
    if raw is None:
        raw = intent.get("handle")
    if isinstance(raw, str):
        raw = [h for h in re.split(r"[,\s]+", raw) if h]
    if not isinstance(raw, list):
        return []
    return [str(h) for h in raw if str(h or "").strip()]


def status(messages: Sequence[Dict[str, Any]], octx: OpContext, *, top: int = STATUS_TOP_N) -> Dict[str, Any]:
    pinned = pinned_fingerprints(octx)
    items = list_items(messages, pinned=pinned, model=octx.model)
    used = _estimate(messages, octx.model)
    window = int(octx.context_length or 0)
    pct = round(used / window * 100, 1) if window else None
    live = [it for it in items if not it.overflow_id]
    largest = sorted(live, key=lambda it: -it.tokens)[:max(1, top)]
    stubs = [it for it in items if it.overflow_id]
    pinned_items = [it for it in items if it.pinned]
    model_pins = [r for r in _pin_rows(octx) if str(r.get("excerpt") or "").startswith(MODEL_PIN_PREFIX)]
    lines = [
        f"context: {used:,} / {window:,} tokens ({pct}%)" if window
        else f"context: {used:,} tokens (window unknown)",
    ]
    if window:
        lines[0] += f"; compaction starts at {int(octx.soft_pct * 100)}%"
    lines.append(f"results in context: {len(live)} live, {len(stubs)} already spilled")
    if largest:
        lines.append("largest (handle · tool · tokens · round · call):")
        for it in largest:
            rnd = f"r{it.round}" if it.round is not None else "-"
            pin = " [pinned]" if it.pinned else ""
            lines.append(f"- {it.handle} · {it.tool or '?'} · {it.tokens:,} · {rnd} · {it.hint}{pin}")
    if pinned_items:
        lines.append("pinned: " + ", ".join(it.handle for it in pinned_items))
    lines.append(f"model pins: {len(model_pins)}/{octx.max_pins}")
    if stubs:
        lines.append("spilled (restore with read_overflow): " + ", ".join(
            f"{it.handle}={it.overflow_id[:12]}…" for it in stubs[-10:]))
    return _result(
        "status", ok=True, text="\n".join(lines),
        used_tokens=used, context_length=window, percent=pct,
        largest=[it.to_dict() for it in largest],
        pinned=[it.handle for it in pinned_items],
        overflow_ids=[it.overflow_id for it in stubs],
    )


def pin(messages: Sequence[Dict[str, Any]], intent: Dict[str, Any], octx: OpContext,
        *, unpin: bool = False) -> Dict[str, Any]:
    op = "unpin" if unpin else "pin"
    api = _pins_api(octx)
    pinned = pinned_fingerprints(octx)
    items = list_items(messages, pinned=pinned, model=octx.model)
    targets: List[Tuple[str, int]] = []   # (label, msg_index)
    refused: List[Dict[str, str]] = []
    handles = _handles_arg(intent)
    found, missing = resolve(items, handles)
    for h in missing:
        refused.append({"handle": h, "reason": "no such item in context"})
    for it in found:
        targets.append((it.handle, it.msg_index))
    snippet = str(intent.get("snippet") or "")
    if snippet:
        idx, why = find_snippet(messages, snippet)
        if idx is None:
            refused.append({"handle": "snippet", "reason": why})
        else:
            label = next((it.handle for it in items if it.msg_index == idx), f"msg{idx}")
            targets.append((label, idx))
    if unpin and intent.get("all"):
        n = 0
        for r in _pin_rows(octx):
            if str(r.get("excerpt") or "").startswith(MODEL_PIN_PREFIX):
                if api.unpin_fragment(octx.owner, octx.session_id, r.get("fingerprint")):
                    n += 1
        return _result(op, ok=True, text=f"unpinned {n} item(s) you had pinned", count=n)
    if not targets:
        return _result(op, ok=False, text=f"{op}: nothing to {op} (" + "; ".join(
            f"{r['handle']}: {r['reason']}" for r in refused) + ")" if refused
            else f"{op}: pass handles (from context_status) or a unique snippet", refused=refused)
    model_pin_count = len([r for r in _pin_rows(octx)
                           if str(r.get("excerpt") or "").startswith(MODEL_PIN_PREFIX)])
    done: List[str] = []
    seen_idx: Set[int] = set()
    for label, idx in targets:
        if idx in seen_idx:
            done.append(label)
            continue
        seen_idx.add(idx)
        fp = fingerprint(messages[idx])
        if unpin:
            if api.unpin_fragment(octx.owner, octx.session_id, fp):
                done.append(label)
            else:
                refused.append({"handle": label, "reason": "was not pinned"})
            continue
        if fp in pinned:
            done.append(label)
            continue
        if model_pin_count >= octx.max_pins:
            refused.append({"handle": label, "reason": f"pin limit reached ({octx.max_pins}); unpin something first"})
            continue
        try:
            excerpt = MODEL_PIN_PREFIX + label + " " + " ".join(_text(messages[idx].get("content")).split())[:120]
            api.pin_fragment(octx.owner, octx.session_id, fp, excerpt)
            pinned.add(fp)
            model_pin_count += 1
            done.append(label)
        except ValueError as e:
            refused.append({"handle": label, "reason": str(e)})
    verb = "unpinned" if unpin else "pinned"
    text = f"{verb}: {', '.join(done) or 'nothing'}"
    if refused:
        text += "; refused: " + "; ".join(f"{r['handle']} ({r['reason']})" for r in refused)
    if not unpin and done:
        text += ". Pinned items are never folded or spilled by compaction."
    return _result(op, ok=bool(done), text=text, handles=done, refused=refused)


def drop(messages: List[Dict[str, Any]], intent: Dict[str, Any], octx: OpContext
         ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    pinned = pinned_fingerprints(octx)
    items = list_items(messages, pinned=pinned, model=octx.model)
    found, missing = resolve(items, _handles_arg(intent))
    refused = [{"handle": h, "reason": "no such item in context"} for h in missing]
    if not found:
        return messages, _result("drop", ok=False, text="context_drop: nothing dropped (" + (
            "; ".join(f"{r['handle']}: {r['reason']}" for r in refused) or "pass handles from context_status")
            + ")", refused=refused)
    out = list(messages)
    before = _estimate(out, octx.model)
    done: List[str] = []
    ids: List[str] = []
    for it in found:
        if it.pinned:
            refused.append({"handle": it.handle, "reason": "pinned; unpin it first"})
            continue
        if it.overflow_id:
            refused.append({"handle": it.handle, "reason": f"already out of context (overflow id={it.overflow_id})"})
            continue
        text = _item_text(out, it)
        if _pending_approvals(text):
            refused.append({"handle": it.handle, "reason": "carries a pending approval"})
            continue
        digest, nbytes, durable = _persist(octx, text, it)
        from src.context_compactor import _overflow_stub, extract_protected_strings, _dedupe_preserve_order
        head_bits = []
        ids_in = extract_protected_strings(text)
        if ids_in:
            head_bits.append("ids: " + ", ".join(_dedupe_preserve_order(ids_in)[:8]))
        head_bits.append(text[:_DROP_HEAD_CHARS])
        stub = _overflow_stub(content_sha256=digest, tool=it.tool, call_id=it.call_id,
                              nbytes=nbytes, head="\n".join(head_bits), durable=durable)
        stub = stub.replace("Full tool output spilled out of the live prompt.",
                            "Dropped from context by the model.", 1)
        _replace_item(out, it, stub, {"overflow_id": digest, "overflow_spilled": True,
                                      "context_op": "drop"}, octx, pinned)
        done.append(it.handle)
        ids.append(digest)
    after = _estimate(out, octx.model)
    freed = max(0, before - after)
    text = f"dropped {len(done)} result(s): {', '.join(done) or 'none'}; ~{freed:,} tokens freed."
    if ids:
        text += " Each is now an overflow stub; read_overflow {\"content_sha256\": \"<id>\"} restores the full text."
    if refused:
        text += " Refused: " + "; ".join(f"{r['handle']} ({r['reason']})" for r in refused)
    return (out if done else messages), _result(
        "drop", ok=bool(done), text=text, handles=done, tokens_freed=freed,
        overflow_ids=ids, refused=refused)


def note(messages: List[Dict[str, Any]], intent: Dict[str, Any], octx: OpContext
         ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    body = str(intent.get("note") or "").strip()
    if not body:
        return messages, _result("note", ok=False, text="context_note: `note` is required")
    body = body[:MAX_NOTE_CHARS]
    pinned = pinned_fingerprints(octx)
    items = list_items(messages, pinned=pinned, model=octx.model)
    found, missing = resolve(items, _handles_arg(intent))
    refused = [{"handle": h, "reason": "no such item in context"} for h in missing]
    targets: List[Item] = []
    for it in found:
        if it.pinned:
            refused.append({"handle": it.handle, "reason": "pinned; unpin it first"})
        elif it.overflow_id:
            refused.append({"handle": it.handle, "reason": f"already out of context (overflow id={it.overflow_id})"})
        else:
            targets.append(it)
    if not targets:
        return messages, _result("note", ok=False, text="context_note: nothing replaced (" + (
            "; ".join(f"{r['handle']}: {r['reason']}" for r in refused) or "pass handles from context_status")
            + ")", refused=refused)
    out = list(messages)
    before = _estimate(out, octx.model)
    texts = [(it, _item_text(out, it)) for it in targets]
    persisted = [(it, *_persist(octx, text, it)) for it, text in texts]
    ids = [d for _it, d, _n, _dur in persisted]
    first = targets[0]
    # What must survive the fold verbatim — the same block compaction keeps.
    preserve_block = ""
    try:
        from src.context_compactor import build_compaction_preserve
        pres = build_compaction_preserve(
            [{"role": "tool", "content": t} for _it, t in texts], objective="")
        pres.objective = ""
        pres.source_refs = pres.source_refs[:20]
        preserve_block = pres.to_block()
    except Exception:  # noqa: BLE001
        logger.debug("context_self_manage: preserve block skipped", exc_info=True)
    handles = [it.handle for it in targets]
    lines = [f"[context note — the model replaced {len(targets)} result(s): {', '.join(handles)}]",
             body]
    for it, digest, nbytes, durable in persisted:
        lines.append(_stub_line(digest, it, nbytes, durable))
    if preserve_block:
        lines.append(preserve_block)
    lines.append("Originals: read_overflow {\"content_sha256\": \"<id>\"} restores any of them.")
    note_text = "\n".join(lines)
    # Rewrite from the LAST target back to the first so a segment edit never
    # shifts the offsets of a target still to be rewritten in the same message.
    for it, digest, nbytes, durable in reversed(persisted):
        if it is first:
            new_text = note_text
        else:
            new_text = _stub_line(digest, it, nbytes, durable) + f" summarized in the context note at {first.handle}."
        _replace_item(out, it, new_text, {"overflow_id": digest, "overflow_spilled": True,
                                          "context_op": "note"}, octx, pinned)
    after = _estimate(out, octx.model)
    freed = max(0, before - after)
    text = (f"noted {len(targets)} result(s) into one note at {first.handle}; ~{freed:,} tokens freed. "
            "Originals are in overflow (ids listed in the note).")
    if refused:
        text += " Refused: " + "; ".join(f"{r['handle']} ({r['reason']})" for r in refused)
    return out, _result("note", ok=True, text=text, handles=handles, tokens_freed=freed,
                        overflow_ids=ids, refused=refused)


def apply_intent(messages: List[Dict[str, Any]], intent: Dict[str, Any], octx: OpContext
                 ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Apply one tool intent. Returns ``(messages, outcome)``; ``messages``
    is returned unchanged (same list) when nothing changed. Never raises."""
    op = str((intent or {}).get("op") or "")
    try:
        if op == "status":
            top = intent.get("top")
            try:
                top = max(1, min(30, int(top))) if top is not None else STATUS_TOP_N
            except (TypeError, ValueError):
                top = STATUS_TOP_N
            return messages, status(messages, octx, top=top)
        if op in ("pin", "unpin"):
            return messages, pin(messages, intent, octx, unpin=(op == "unpin"))
        if op == "drop":
            return drop(messages, intent, octx)
        if op == "note":
            return note(messages, intent, octx)
    except Exception as e:  # noqa: BLE001 - a context op never costs the turn
        logger.warning("context_self_manage: %s failed: %s", op, e, exc_info=True)
        return messages, _result(op or "unknown", ok=False, text=f"context_{op}: failed ({e})")
    return messages, _result(op or "unknown", ok=False, text=f"unknown context op {op!r}")


def outcome_to_tool_result(outcome: Dict[str, Any]) -> Dict[str, Any]:
    """The dict the model reads for a context tool call."""
    res: Dict[str, Any] = {"output": outcome.get("text") or "", "exit_code": 0 if outcome.get("ok") else 1}
    return res


def summarize_ops(counts: Dict[str, Any]) -> Dict[str, Any]:
    return {k: counts.get(k, 0) for k in ("status", "pins", "unpins", "drops", "notes", "tokens_freed")}


def parse_args(content: str) -> Optional[Dict[str, Any]]:
    text = (content or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None
