"""
context_compactor.py

Auto-compacts conversation history when approaching context window limits.
Summarizes older messages via the same LLM, preserving key context.
"""

import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from src.model_context import get_context_length, estimate_tokens
from src.llm_core import llm_call_async
from src.endpoint_resolver import resolve_endpoint
from src.settings import get_setting
from core.models import ChatMessage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool images (FAUSTUS)
#
# A tool result that carries a picture (an MCP browser screenshot, the builtin
# desktop_screenshot) reaches the model as a synthetic user message whose
# metadata.source is "tool result: <tool>" (see `_tool_image_messages` in
# src/agent_loop.py). A desktop-driving run takes one every round, and each is
# ~1200 tokens the model only needs while it is the CURRENT view of the
# screen. So the prompt keeps the last `agent_keep_images` of them and turns
# the older ones into a one-line text marker, in place, so the round structure
# (assistant / tool / image) is untouched. Images the user attached are never
# pruned — they are the user's own words.
# ---------------------------------------------------------------------------

TOOL_IMAGE_SOURCE_PREFIX = "tool result: "
EARLIER_IMAGE_OMITTED = "[earlier image omitted]"
DEFAULT_KEEP_IMAGES = 1


def _is_tool_image_message(msg: Any) -> bool:
    if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
        return False
    metadata = msg.get("metadata")
    if not isinstance(metadata, dict):
        return False
    source = metadata.get("source")
    if not isinstance(source, str) or not source.startswith(TOOL_IMAGE_SOURCE_PREFIX):
        return False
    return any(
        isinstance(block, dict) and block.get("type") in ("image_url", "image", "input_image")
        for block in msg["content"]
    )


def _without_image_blocks(msg: Dict) -> Dict:
    out = dict(msg)
    new_content: List[Dict] = []
    replaced = False
    for block in msg["content"]:
        if isinstance(block, dict) and block.get("type") in ("image_url", "image", "input_image"):
            if not replaced:
                new_content.append({"type": "text", "text": EARLIER_IMAGE_OMITTED})
                replaced = True
            continue
        new_content.append(block)
    out["content"] = new_content
    return out


def keep_images_setting() -> int:
    """`agent_keep_images`: how many tool images stay in the prompt (-1 = all)."""
    try:
        return int(get_setting("agent_keep_images", DEFAULT_KEEP_IMAGES))
    except Exception:  # noqa: BLE001 - settings unavailable / malformed
        return DEFAULT_KEEP_IMAGES


def prune_tool_images(messages: List[Dict], keep: int = DEFAULT_KEEP_IMAGES) -> List[Dict]:
    """Keep only the LAST `keep` tool-sourced image messages; older ones get
    their image blocks replaced by ``EARLIER_IMAGE_OMITTED``.

    Returns the very same list object when nothing changes (callers rely on
    identity to mean "untouched"); otherwise a new list with copied rows for
    the pruned messages only. `keep < 0` disables pruning.
    """
    if keep < 0 or not messages:
        return messages
    indices = [i for i, m in enumerate(messages) if _is_tool_image_message(m)]
    to_prune = indices[: max(0, len(indices) - keep)]
    if not to_prune:
        return messages
    out = list(messages)
    for i in to_prune:
        out[i] = _without_image_blocks(out[i])
    return out


def _content_as_text(content: Any) -> str:
    """Flatten a message's content to plain text.

    Handles the three shapes that flow through history: a plain string, a
    multimodal list of content blocks (vision/image attachments), and None
    (assistant turns that carried only native tool_calls persist content as
    None). Returns "" for anything without text so callers can safely slice
    the result.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("text")
        )
    return ""


# ---------------------------------------------------------------------------
# History correspondence
#
# The prompt sent to the model is NOT ``session.history`` with the system
# messages in front.  routes/chat_helpers.py builds it as
# ``preface + session.get_context_messages()``, and:
#   * the preface contributes system messages *and* user-role blocks — memory,
#     RAG and web context are ``role: "user"`` untrusted-context messages
#     (src/prompt_security.py), so counting system messages does not describe
#     the offset;
#   * a date/time context message is spliced in just before the last turn;
#   * ``get_context_messages()`` filters slash-command chatter out of the
#     history, so history positions and prompt positions drift apart.
#
# No arithmetic on indices can map a position in the prompt back to a row of
# the durable transcript.  Instead the prompt builder stamps every message that
# actually came from the history with its real history index, and compaction
# deletes a row only when it carries that stamp *and* still matches the row it
# names.  Without a provable mapping nothing is deleted: an uncompacted history
# costs context, a wrongly deleted one is gone from the database for good.
# ---------------------------------------------------------------------------

HISTORY_INDEX_KEY = "_history_index"

# Marker left on a message the trimmer shortened, carrying a fingerprint of the
# text it was made from.  It lets callers recognise a shortened rendering as the
# same message instead of comparing text (see ``message_is_truncation_of``).
TRUNCATED_ORIGINAL_KEY = "_truncated_original"


def _text_fingerprint(text: Any) -> str:
    """Stable fingerprint of a piece of message text."""
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()


def _row_fingerprint(role: Any, content: Any) -> str:
    """Stable identity for one history row (role + length + flattened text)."""
    text = _content_as_text(content)
    return _text_fingerprint(f"{role}\x00{len(text)}\x00{text}")


def annotate_history_positions(session, context_messages: List[Dict]) -> int:
    """Stamp prompt messages with the history row each one came from.

    ``context_messages`` is what ``Session.get_context_messages()`` returned:
    by construction an order-preserving subsequence of ``session.history``
    (the filter drops rows, it never reorders them or rewrites role/content).
    Align the two greedily; if the alignment cannot consume every context
    message that assumption no longer holds, so stamp nothing and let
    compaction fall back to "summarize the prompt, touch no history".

    Returns the number of messages stamped (0 when no mapping could be proved).
    """
    history = getattr(session, "history", None)
    if not isinstance(history, list) or not history or not context_messages:
        return 0

    pairs: List[Tuple[Dict, int]] = []
    cursor = 0
    for msg in context_messages:
        if not isinstance(msg, dict):
            return 0
        role = msg.get("role")
        content = msg.get("content")
        while cursor < len(history):
            entry = history[cursor]
            if getattr(entry, "role", None) == role and getattr(entry, "content", None) == content:
                break
            cursor += 1
        if cursor >= len(history):
            return 0
        pairs.append((msg, cursor))
        cursor += 1

    for msg, index in pairs:
        msg[HISTORY_INDEX_KEY] = index
    return len(pairs)


def _history_targets(older: List[Dict], kept: List[Dict]) -> List[Dict[str, Any]]:
    """Rows compaction may delete: summarized AND no longer in the prompt.

    Built from the stamps only — never from index arithmetic — and explicitly
    minus every row still present in the prompt we are about to send, so a
    message the model is being shown right now can never be deleted.
    """
    kept_indices = {
        m.get(HISTORY_INDEX_KEY)
        for m in kept
        if isinstance(m, dict) and isinstance(m.get(HISTORY_INDEX_KEY), int)
    }
    targets: Dict[int, Dict[str, Any]] = {}
    for msg in older:
        if not isinstance(msg, dict):
            continue
        index = msg.get(HISTORY_INDEX_KEY)
        if not isinstance(index, int) or index in kept_indices:
            continue
        targets[index] = {
            "index": index,
            "fingerprint": _row_fingerprint(msg.get("role"), msg.get("content")),
        }
    return [targets[i] for i in sorted(targets)]


COMPACT_THRESHOLD = 0.85  # Trigger compaction at 85% of context window
SUMMARY_MAX_TOKENS = 1024
SMALL_CONTEXT_LIMIT = 8192  # Models with context <= this get aggressive trimming

# Cursor-style self-summarization prompt — produces structured, dense summaries
SELF_SUMMARY_SYSTEM_PROMPT = """You are summarizing a conversation to preserve context after compaction. Produce a structured summary that lets the conversation continue seamlessly.

Use this format:

## Conversation Summary
**Turns summarized:** {count}  |  **Compactions so far:** {n}

### User Goal
One sentence describing what the user is trying to accomplish.

### What Was Done
- Bullet points of completed actions, decisions made, and key outputs
- Include specific file paths, function names, variable names, URLs, and config values
- Note any errors encountered and how they were resolved

### Current State
What is the system/code/task state right now? What was the last thing discussed?

### Pending / Next Steps
- What remains to be done
- Any open questions or blockers

### Key Context
- Important constraints, preferences, or decisions that must not be forgotten
- Specific values: model names, ports, paths, credentials references, versions

Keep the summary under 1000 tokens. Be dense — every token should carry information. Do not include pleasantries or meta-commentary."""


def normalize_compaction_summary(summary: str) -> str:
    """Remove redundant leading title text before adding our wrapper."""
    text = (summary or "").strip()
    text = re.sub(r"^(?:#{1,3}\s*)?Conversation Summary\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\*\*Conversation Summary\*\*\s*", "", text, flags=re.IGNORECASE)
    return text.lstrip()


def _sanitize_tool_messages(msgs: List[Dict]) -> List[Dict]:
    """Drop orphaned `tool` messages and dangling assistant `tool_calls`.

    OpenAI's API requires every `role:"tool"` message to immediately
    follow an assistant message that carries `tool_calls` (or another
    tool message in the same batch). Front-trimming the history can cut
    the assistant `tool_calls` parent while keeping its tool responses,
    which triggers: "messages with role 'tool' must be a response to a
    preceding message with 'tool_calls'". This pass repairs that:
      - drops `tool` messages with no valid preceding tool_calls
      - drops assistant `tool_calls` messages whose tool responses were
        all trimmed away (some providers reject unanswered tool_calls)
    """
    # Pass 1: drop orphan tool messages.
    cleaned: List[Dict] = []
    in_batch = False  # are we right after an assistant tool_calls (or mid-batch)?
    for m in msgs:
        role = m.get("role")
        if role == "tool":
            if in_batch:
                cleaned.append(m)
            # else: orphan — drop
            continue
        if role == "assistant" and m.get("tool_calls"):
            in_batch = True
        else:
            in_batch = False
        cleaned.append(m)

    # Pass 2: drop assistant tool_calls messages that have NO following
    # tool response (dangling) — walk backwards so we know what follows.
    out: List[Dict] = []
    for i, m in enumerate(cleaned):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            nxt = cleaned[i + 1] if i + 1 < len(cleaned) else None
            if not (nxt and nxt.get("role") == "tool"):
                # Dangling tool_calls — keep the message but strip the
                # tool_calls so it's a plain assistant turn (preserves any
                # text content the model produced alongside the calls).
                m = {k: v for k, v in m.items() if k != "tool_calls"}
                if not (m.get("content") or "").strip():
                    continue  # nothing left worth keeping
        out.append(m)
    return out


def _message_text_token_estimate(text: str) -> int:
    if not isinstance(text, str):
        return 4
    return int(len(text) * 0.3) + 4


# The three reasons a message can come back shortened. They are distinct on
# purpose: claiming "the pasted message was too large" when the message was
# perfectly reasonable and merely lost a race for room is a lie the user cannot
# check, and it made large-paste bugs look like user error.
OVERSIZED_NOTICE = (
    "\n\n[Notice: the pasted message was too large for this model's context "
    "window, so Faustus kept the beginning and end.]"
)
ROOM_NOTICE = (
    "\n\n[Notice: this message was shortened to fit the remaining context "
    "window; the beginning and end were kept.]"
)
CONTEXT_NOTICE = "\n\n[Notice: shortened to fit the model's context window.]"
_TRUNCATION_NOTICES = (OVERSIZED_NOTICE, ROOM_NOTICE, CONTEXT_NOTICE)

OMITTED_PLACEHOLDER = "[Current user message omitted: it exceeded the model context window.]"


def truncation_fragments(text: Any) -> Optional[Tuple[str, str]]:
    """Split a trimmer-shortened text back into its (head, tail) fragments.

    Returns None when the text carries no truncation notice.
    """
    if not isinstance(text, str):
        return None
    for notice in _TRUNCATION_NOTICES:
        marker = notice.strip()
        if marker and marker in text:
            head, _, tail = text.partition(marker)
            return head.strip(), tail.strip()
    return None


def truncated_text_matches(shortened: Any, original: Any) -> bool:
    """Is `shortened` a trimmed rendering of `original`?

    Text-level recovery for callers that only have the two strings: the head
    and tail the trimmer kept are a prefix and a suffix of the original, so
    both must still be found in it.
    """
    fragments = truncation_fragments(shortened)
    if not fragments:
        return False
    head, tail = fragments
    if not head:
        return False
    original_text = str(original or "")
    if head not in original_text:
        return False
    return not tail or tail in original_text


def message_is_truncation_of(message: Any, original_text: Any) -> bool:
    """Identity check: was `message` produced by shortening `original_text`?

    Reads the marker the trimmer stamps on a message it shortened, so callers
    do not have to guess from the text — a notice spliced into the middle of a
    message defeats every substring comparison.
    """
    if not isinstance(message, dict):
        return False
    marker = message.get(TRUNCATED_ORIGINAL_KEY)
    if not isinstance(marker, str) or not marker:
        return False
    text = str(original_text or "")
    return marker in (_text_fingerprint(text), _text_fingerprint(text.strip()))


def _truncate_text_to_token_budget(
    text: str,
    token_budget: int,
    notice: str = OVERSIZED_NOTICE,
    placeholder: Optional[str] = None,
) -> str:
    """Trim a too-large current user message instead of dropping it entirely."""
    if token_budget <= 32:
        return OMITTED_PLACEHOLDER if placeholder is None else placeholder

    if not isinstance(text, str):
        # This helper is typed/used as text downstream, so return an empty
        # string rather than the raw non-string (which would move the crash
        # into the caller that concatenates/measures the result).
        return ""
    # Match src.model_context.estimate_tokens' rough chars * 0.3 estimate.
    max_chars = max(200, int((token_budget - 16) / 0.3))
    if len(text) <= max_chars:
        return text

    keep_chars = max(200, max_chars - len(notice))
    head_len = max(100, int(keep_chars * 0.7))
    tail_len = max(80, keep_chars - head_len)
    return text[:head_len].rstrip() + notice + "\n\n" + text[-tail_len:].lstrip()


def _truncate_tool_call_args(msg: Dict[str, Any], token_budget: int) -> Dict[str, Any]:
    """Shrink oversized assistant ``tool_calls`` arguments to fit ``token_budget``.

    A tool-only turn persists ``content=None`` with its whole payload in
    ``tool_calls[].function.arguments`` (e.g. a large create_document body), which
    the text-content truncation can't reach — so the message could stay over
    budget and the upstream call would 400. Replace each argument string that
    overflows its share of the budget with a small valid-JSON placeholder,
    preserving ``id``/``type``/``function.name`` so tool/result pairing and
    provider validation are unaffected. Returns msg unchanged when there is
    nothing oversized.
    """
    tool_calls = msg.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return msg
    # Budget left after whatever content survived (estimate_tokens counts tool
    # arguments too, so measure content alone here).
    content_tokens = estimate_tokens([{"role": msg.get("role", "assistant"), "content": msg.get("content")}])
    per_call = max(16, (max(0, token_budget - content_tokens)) // len(tool_calls))
    new_calls = []
    changed = False
    for tc in tool_calls:
        fn = tc.get("function") if isinstance(tc, dict) else None
        args = fn.get("arguments") if isinstance(fn, dict) else None
        if isinstance(args, str) and int(len(args) * 0.3) > per_call:
            new_fn = dict(fn)
            new_fn["arguments"] = json.dumps({"_truncated_for_context": len(args)})
            new_tc = dict(tc)
            new_tc["function"] = new_fn
            new_calls.append(new_tc)
            changed = True
        else:
            new_calls.append(tc)
    if not changed:
        return msg
    out = dict(msg)
    out["tool_calls"] = new_calls
    return out


def _truncate_message_to_token_budget(
    msg: Dict[str, Any],
    token_budget: int,
    notice: str = OVERSIZED_NOTICE,
    placeholder: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a copy of msg whose text content (and tool-call args) fit token_budget.

    When the text actually changed, the copy is stamped with a fingerprint of
    the text it came from so downstream code can recognise it as the same
    message (see ``message_is_truncation_of``).
    """
    out = dict(msg)
    content = out.get("content", "")
    original_text = _content_as_text(content)
    if isinstance(content, str):
        out["content"] = _truncate_text_to_token_budget(content, token_budget, notice, placeholder)
    elif isinstance(content, list):
        remaining = token_budget
        new_content = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                new_content.append(item)
                continue
            text = item.get("text", "")
            truncated = _truncate_text_to_token_budget(text, remaining, notice, placeholder)
            cloned = dict(item)
            cloned["text"] = truncated
            new_content.append(cloned)
            remaining -= _message_text_token_estimate(truncated)
        out["content"] = new_content
    if original_text and _content_as_text(out.get("content")) != original_text:
        out[TRUNCATED_ORIGINAL_KEY] = _text_fingerprint(original_text)
    # A tool-only turn (content=None) carries its payload in tool_calls args,
    # which the branches above can't shrink — handle it so the message can fit.
    return _truncate_tool_call_args(out, token_budget)


def _shrink_messages_to_budget(msgs: List[Dict], budget: int) -> List[Dict]:
    """Shorten `msgs` (biggest first) until together they fit `budget`."""
    out = list(msgs)
    if estimate_tokens(out) <= budget:
        return out
    order = sorted(range(len(out)), key=lambda i: estimate_tokens([out[i]]), reverse=True)
    for i in order:
        total = estimate_tokens(out)
        if total <= budget:
            break
        own = estimate_tokens([out[i]])
        out[i] = _truncate_message_to_token_budget(
            out[i],
            max(0, own - (total - budget)),
            notice=CONTEXT_NOTICE,
            placeholder="[Omitted: it did not fit the model's context window.]",
        )
    return out


def _force_within_budget(messages: List[Dict], budget: int) -> List[Dict]:
    """Absolute last resort behind ``trim_for_context``'s hard invariant.

    Every structured strategy has already run by the time this is reached; it
    exists so the function can never hand back a prompt the model would
    reject, whatever shape the input had.
    """
    out = list(messages)
    while len(out) > 1 and estimate_tokens(out) > budget:
        out.pop(0)  # the newest turn is the anchor
    if not out or estimate_tokens(out) <= budget:
        return out
    msg = dict(out[0])
    msg.pop("tool_calls", None)
    text = _content_as_text(msg.get("content"))
    msg["content"] = text[: max(0, int((budget - 8) / 0.3))]
    out = [msg]
    while out and estimate_tokens(out) > budget:
        text = out[0].get("content") or ""
        if not text:
            return []
        out = [{**out[0], "content": text[: len(text) // 2]}]
    return out


def _is_essential_system(msg: Dict) -> bool:
    """System messages that must survive trimming, whatever else is dropped.

    Two kinds qualify, for the same reason: nothing else in the prompt carries
    their content.

      * a research-spinoff primer (the seeded report that grounds a "Discuss"
        chat) — it is the conversation's whole knowledge base;
      * a compaction summary — the messages it replaced have already been
        deleted from the transcript, so dropping it loses the conversation
        outright.  It used to land in ``extra_system`` with the same priority
        as a memory or RAG blob, and the blob won.
    """
    metadata = msg.get("metadata") or {}
    if metadata.get("research_spinoff_from") or metadata.get("compacted"):
        return True
    content = msg.get("content")
    return isinstance(content, str) and content.lstrip().startswith("[Conversation summary")


def trim_for_context(messages: List[Dict], context_length: int, reserve_tokens: int = 512) -> List[Dict]:
    """Trim messages to fit within context_length.

    For small-context models, progressively strips:
    1. RAG/memory system messages (keep preset system prompt and essentials)
    2. Older conversation turns — including the recent ones, if that is what
       it takes; a protected window is a preference, not a licence to return
       an over-budget prompt
    3. Room clawed back from the resident system/document payload
    4. Only then, the current turn itself

    Hard invariant: ``estimate_tokens(trim_for_context(m, ctx, r)) <= ctx - r``.
    Callers (src/llm_core.py included) do not re-trim, so anything this returns
    is what the model is asked to accept.
    """
    budget = context_length - reserve_tokens
    # Tool images first, budget or not: only the newest screen view is worth
    # its ~1200 tokens (see `prune_tool_images`). Non-destructive — the loop's
    # own message list keeps every image; this shapes the request only.
    messages = prune_tool_images(messages, keep_images_setting())
    used = estimate_tokens(messages)
    if used <= budget:
        return messages

    logger.info(f"Trimming messages: {used} tokens > {budget} budget (ctx={context_length})")

    # Separate system messages from conversation.
    # Messages marked _protected (e.g. active document) are never dropped, but
    # they are counted against the budget like everything else.
    system_msgs = []
    protected_msgs = []
    convo_msgs = []
    for msg in messages:
        if msg.get("_protected"):
            protected_msgs.append(msg)
        elif msg.get("role") == "system":
            system_msgs.append(msg)
        else:
            convo_msgs.append(msg)

    # Priority: keep the first system msg (preset prompt) plus every essential
    # one (research primer, compaction summary); drop the rest (memory, RAG,
    # memo) first.
    _essential_marked = [m for m in system_msgs if _is_essential_system(m)]
    _plain = [m for m in system_msgs if not _is_essential_system(m)]
    essential_system = (_plain[:1] if _plain else []) + _essential_marked
    extra_system = _plain[1:]

    def _fits(convo, systems=None, protected=None):
        resident = essential_system if systems is None else systems
        held = protected_msgs if protected is None else protected
        return estimate_tokens(resident) + estimate_tokens(held) + estimate_tokens(convo) <= budget

    # 1. Try dropping extra system messages one by one (from the end)
    if _fits(convo_msgs):
        # Dropping extras was enough — try adding back some
        result = list(essential_system)
        for msg in extra_system:
            if _fits(convo_msgs, systems=result + [msg]):
                result.append(msg)
            else:
                break
        return _sanitize_tool_messages(result + protected_msgs + convo_msgs)

    # 2. Still too big — truncate the first system message (but keep more than 500 chars)
    if essential_system:
        sys_text = essential_system[0].get("content", "")
        if isinstance(sys_text, str) and len(sys_text) > 2000:
            truncated_system = dict(essential_system[0])
            truncated_system["content"] = sys_text[:2000] + "\n[System prompt truncated for context limits]"
            essential_system = [truncated_system] + essential_system[1:]
            if _fits(convo_msgs):
                return _sanitize_tool_messages(essential_system + protected_msgs + convo_msgs)

    # 3. Still too big — drop older conversation turns BUT always keep the
    # current user turn.  Hermes-style: recent context matters more than old
    # context, so the last PROTECT_RECENT turns go last — but they do go.
    # Keeping them pinned while the total stayed over budget is what made this
    # function return prompts at ~2x the window and then "compensate" by
    # mangling the user's own message.
    PROTECT_RECENT = 10
    current_msg = convo_msgs[-1:] if convo_msgs else []
    prior_convo = list(convo_msgs[:-1]) if convo_msgs else []
    if len(prior_convo) >= PROTECT_RECENT:
        keep = PROTECT_RECENT - 1
        old_msgs = prior_convo[:-keep]
        recent_msgs = prior_convo[-keep:]
    else:
        old_msgs = prior_convo
        recent_msgs = []
    while old_msgs and not _fits(old_msgs + recent_msgs + current_msg):
        old_msgs.pop(0)
    while recent_msgs and not _fits(old_msgs + recent_msgs + current_msg):
        recent_msgs.pop(0)
    convo_msgs = old_msgs + recent_msgs + current_msg

    # 4. Everything droppable is gone.  Before touching the user's own words,
    # claw back room from what is still resident (system prompt, primer,
    # summary, pinned document) — the current turn is the request itself.
    if current_msg and not _fits(convo_msgs):
        own_tokens = estimate_tokens(current_msg)
        if own_tokens <= budget:
            held = len(essential_system)
            resident = _shrink_messages_to_budget(
                essential_system + protected_msgs, budget - own_tokens
            )
            essential_system, protected_msgs = resident[:held], resident[held:]

    # 5. If the current message still does not fit, shrink only that message.
    # The "too large to paste" notice is reserved for a message that does not
    # fit the budget on its own merits; anything else gets an honest one.
    if current_msg and not _fits(convo_msgs):
        prefix = essential_system + protected_msgs + convo_msgs[:-1]
        available_for_current = budget - estimate_tokens(prefix)
        oversized = estimate_tokens(convo_msgs[-1:]) > budget
        convo_msgs = list(convo_msgs)
        convo_msgs[-1] = _truncate_message_to_token_budget(
            convo_msgs[-1],
            available_for_current,
            notice=OVERSIZED_NOTICE if oversized else ROOM_NOTICE,
        )

    result = _sanitize_tool_messages(essential_system + protected_msgs + convo_msgs)
    # 6. Hard invariant. Nothing downstream re-trims, so a prompt that is still
    # over budget here is a prompt the model is asked to reject.
    if estimate_tokens(result) > budget:
        result = _sanitize_tool_messages(_force_within_budget(result, budget))
    logger.info(f"Trimmed to {estimate_tokens(result)} tokens ({len(result)} messages)")
    return result


def post_compact_reminder(session, owner: Optional[str] = None) -> Optional[Dict]:
    """A system message re-injecting what compaction tends to lose mid-session:
    the project's objectives and a pointer to its standing instructions.

    A summary is lossy by design, and the plan is the first thing it loses —
    the model then "forgets the plan mid-session". This rebuilds the standing
    context from disk right after the summary. Returns None outside a project
    (or when there is nothing to remind about), and swallows every failure:
    a broken objectives file must never break compaction.
    """
    try:
        session_id = getattr(session, "id", None)
        if session_id is None and isinstance(session, dict):
            session_id = session.get("id")
        if not session_id:
            return None
        from services.projects import project_for_session
        project = project_for_session(str(session_id), owner)
        if not project:
            return None

        parts: List[str] = []
        try:
            from services import objectives as _objectives
            obj_block = _objectives.objectives_block(
                project, cap=_objectives.MAX_REMINDER_CHARS
            )
            if obj_block:
                parts.append(obj_block)
        except Exception:  # noqa: BLE001 - best effort
            pass

        workspace = project.get("workspace") or ""
        if workspace:
            try:
                from src import project_instructions as _pinstr
                # Same gate as the system prompt itself (src/agent_loop.py): the
                # post-compact reminder re-injects these rules, so an unapproved
                # folder's file must not come back in through this door either.
                # Defaults to trusted on any failure — never blank the user's own
                # rules because a config file would not parse.
                _trusted = True
                try:
                    from src import workspace_trust as _wtrust
                    _trusted = _wtrust.instructions_trusted(workspace)
                except Exception:  # noqa: BLE001 - import-time only
                    _trusted = True
                info = _pinstr.read(workspace) if _trusted else {}
                if info.get("text"):
                    pointer = (
                        f"The project's standing rules in {info.get('rel')} still apply "
                        "after this compaction."
                    )
                    rules = _pinstr.block(workspace).strip()
                    if rules and len(rules) <= 1500:
                        parts.append(pointer + "\n" + rules)
                    else:
                        parts.append(pointer)
                elif not _trusted:
                    note = _pinstr.block(workspace, trusted=False).strip()
                    if note:
                        parts.append(note)
            except Exception:  # noqa: BLE001 - best effort
                pass

        if not parts:
            return None
        return {
            "role": "system",
            # Marked like the summary so trim_for_context keeps it resident.
            "metadata": {"compacted": True},
            "content": "[Post-compaction reminder]\n" + "\n\n".join(parts),
        }
    except Exception as e:  # noqa: BLE001 - compaction path, never raise
        logger.debug("post_compact_reminder failed: %s", e)
        return None


async def maybe_compact(
    session,
    endpoint_url: str,
    model: str,
    messages: List[Dict],
    headers: Optional[Dict] = None,
    owner: Optional[str] = None,
    *,
    persist: bool = True,
    compaction_state: Optional[Dict[str, Any]] = None,
) -> tuple:
    """Check context usage and compact if above threshold.

    Returns (messages, context_length, was_compacted).
    """
    context_length = get_context_length(endpoint_url, model)
    used = estimate_tokens(messages)
    pct = (used / context_length) * 100 if context_length else 0

    if pct < COMPACT_THRESHOLD * 100:
        return messages, context_length, False

    logger.info(
        f"Context at {pct:.1f}% ({used}/{context_length} tokens) — compacting"
    )

    # Split into system preface and conversation
    system_msgs = []
    convo_msgs = []
    for msg in messages:
        if msg.get("role") == "system":
            system_msgs.append(msg)
        else:
            convo_msgs.append(msg)

    if len(convo_msgs) < 4:
        return messages, context_length, False

    # Split conversation: summarize older half, keep recent half
    split_point = len(convo_msgs) // 2
    older = convo_msgs[:split_point]
    recent = convo_msgs[split_point:]

    # Build the text to summarize
    convo_text = "\n".join(
        f"{msg.get('role', 'user').upper()}: {_content_as_text(msg.get('content'))[:2000]}"
        for msg in older
    )

    # Count prior compactions from existing summary messages
    compaction_count = sum(
        1 for m in system_msgs
        if "[Conversation summary" in m.get("content", "")
    )

    # Use utility model if configured, otherwise fall back to session model
    util_url, util_model, util_headers = resolve_endpoint("utility", owner=owner)
    compact_url = util_url or endpoint_url
    compact_model = util_model or model
    compact_headers = util_headers if util_url else headers

    prompt = SELF_SUMMARY_SYSTEM_PROMPT.replace(
        "{count}", str(len(older))
    ).replace(
        "{n}", str(compaction_count + 1)
    )
    summary_messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": convo_text},
    ]

    try:
        summary = await llm_call_async(
            compact_url,
            compact_model,
            summary_messages,
            temperature=0.2,
            max_tokens=SUMMARY_MAX_TOKENS,
            headers=compact_headers,
            timeout=30,
        )
    except Exception as e:
        logger.error(f"Compaction summary failed: {e}")
        # Degrade gracefully: keep the conversation intact rather than
        # silently dropping the older half. was_compacted=False signals the
        # caller nothing was summarized; trim_for_context handles length.
        return messages, context_length, False
    summary = normalize_compaction_summary(summary)

    summary_msg = {
        "role": "system",
        # Marked so trim_for_context treats it as essential: the messages it
        # stands in for have already been deleted from the transcript.
        "metadata": {"compacted": True},
        "content": f"[Conversation summary — earlier messages were compacted]\n{summary}",
    }

    # Post-compaction reminder (project objectives + standing instructions):
    # the summary is lossy and the plan is the first thing it loses. Never
    # allowed to break compaction — the helper returns None on any failure.
    reminder = post_compact_reminder(session, owner)
    compacted = system_msgs + [summary_msg] + ([reminder] if reminder else []) + recent

    # Update the session history to match. The rows to delete are named
    # individually by the history stamps the prompt builder left on them
    # (see annotate_history_positions), minus everything still present in the
    # prompt we are about to send — never by adding an offset to a prompt
    # index. When the prompt carries no stamps the mapping is unknown, so the
    # transcript is left alone and only the prompt is compacted.
    history_targets = _history_targets(older, system_msgs + recent)
    if compaction_state is not None:
        compaction_state.update({
            "split_point": split_point,
            "summary": summary,
            "system_msg_count": len(system_msgs),
            "history_targets": history_targets,
            "summarized_count": len(older),
            "applied": False,
        })
    if persist:
        _update_session_history(
            session, summary, history_targets, summarized_count=len(older)
        )
        if compaction_state is not None:
            compaction_state["applied"] = True

    new_used = estimate_tokens(compacted)
    logger.info(
        f"Compacted: {used} -> {new_used} tokens "
        f"({len(older)} messages summarized, {len(recent)} kept)"
    )

    return compacted, context_length, True


def apply_compaction_state(session, compaction_state: Optional[Dict[str, Any]]) -> bool:
    """Persist a route-specific compaction after that route commits output.

    Candidate prompts may be compacted speculatively while an explicit
    foreground fallback chain is being tried.  Persisting at construction time
    would let an unavailable route rewrite history before another route answers,
    so callers hold this small plan and apply only the winning route's plan.
    """

    state = compaction_state if isinstance(compaction_state, dict) else None
    if not state or state.get("applied"):
        return False
    summary = state.get("summary")
    split_point = state.get("split_point")
    if not isinstance(summary, str) or not isinstance(split_point, int):
        return False
    summarized_count = state.get("summarized_count")
    _update_session_history(
        session,
        summary,
        state.get("history_targets") or [],
        summarized_count=summarized_count if isinstance(summarized_count, int) else split_point,
    )
    state["applied"] = True
    return True


def apply_compaction_state_for_session(
    session_id: Optional[str],
    compaction_state: Optional[Dict[str, Any]],
) -> bool:
    """Resolve an in-memory session and apply a deferred compaction plan."""

    if not session_id:
        return False
    try:
        from core.models import get_session_manager_instance

        manager = get_session_manager_instance()
        session = manager.get_session(session_id) if manager else None
    except Exception:
        session = None
    return apply_compaction_state(session, compaction_state) if session else False


def _update_session_history(session, summary: str,
                            history_targets: Optional[List[Dict[str, Any]]] = None,
                            summarized_count: int = 0) -> bool:
    """Replace the summarized transcript rows with the compaction summary.

    ``history_targets`` names each row to delete by its real index in
    ``session.history`` together with a fingerprint of the row that occupied
    that index when the prompt was built. Every target is verified before
    anything is written, and one mismatch aborts the whole update: the
    transcript may have been edited, forked or already compacted since, and
    this deletes rows from the database (SessionManager.replace_messages).

    Returns True when the history was rewritten. Refusing is cheap — the
    conversation merely stays uncompacted; deleting the wrong rows is not
    recoverable.
    """
    if not session or not hasattr(session, "history"):
        return False
    history = getattr(session, "history", None)
    if not isinstance(history, list) or not history:
        return False
    if not history_targets:
        logger.info(
            "Compaction: no provable prompt-to-history mapping — "
            "compacting the prompt only, transcript left intact"
        )
        return False

    doomed = set()
    for target in history_targets:
        if not isinstance(target, dict):
            return False
        index = target.get("index")
        if not isinstance(index, int) or not 0 <= index < len(history):
            logger.warning(
                "Compaction: history target %r is out of range (%d rows) — "
                "leaving the transcript intact", index, len(history),
            )
            return False
        entry = history[index]
        if _row_fingerprint(getattr(entry, "role", None), getattr(entry, "content", None)) != target.get("fingerprint"):
            logger.warning(
                "Compaction: history row %d no longer matches the message it was "
                "summarized from — leaving the transcript intact", index,
            )
            return False
        doomed.add(index)

    if not doomed:
        return False
    if max(doomed) >= len(history) - 1:
        # The newest row is never old context; a mapping that says otherwise
        # is a mapping we do not trust.
        logger.warning("Compaction: refusing to summarize away the newest history row")
        return False

    summary = normalize_compaction_summary(summary)
    summary_msg = ChatMessage(
        role="system",
        content=f"[Conversation summary]\n{summary}",
        metadata={"compacted": True, "summarized_count": summarized_count or len(doomed)},
    )
    new_history = []
    inserted = False
    for index, entry in enumerate(history):
        if index in doomed:
            if not inserted:
                new_history.append(summary_msg)
                inserted = True
            continue
        new_history.append(entry)

    try:
        from core.models import get_session_manager_instance
        manager = get_session_manager_instance()
    except Exception:
        manager = None
    if manager and getattr(session, "id", None):
        if manager.replace_messages(session.id, new_history):
            return True
    session.history = new_history
    return True
