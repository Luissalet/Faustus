"""Where the context window actually goes (FAUSTUS).

Roadmap, high priority: *"Agent prompt/context bloat. Agent mode is too heavy
for smaller local models: tool schemas, skills, memory, documents, and
instructions can eat the context before the user request really starts."*

What was missing first was not a trimmer — it was a measurement. Nothing in the
app could tell you that 9k of a 32k window was tool schemas before you typed a
word, so "the local model ignored my instructions" stayed a mystery instead of
a number. This turns the exact message list and tool payload about to be sent
into a per-section token ledger, plus one line of advice per section that is
out of proportion for the window in play.

Deliberately a pure function over the assembled messages: it cannot change what
the model sees, it costs one pass over the list, and it unit-tests with no
model, no endpoint and no network.
"""

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.model_context import estimate_tokens

# Display order — roughly "fixed cost first, your actual question last", which
# is also the order in which the numbers are damning.
SECTIONS: Tuple[Tuple[str, str], ...] = (
    ("system", "System prompt"),
    ("tools", "Tool schemas"),
    ("instructions", "Project instructions"),
    ("skills", "Skills"),
    ("memory", "Memories"),
    ("documents", "Documents & files"),
    ("web", "Web & research"),
    ("attachments", "Attachments"),
    ("retrieved", "Other retrieved context"),
    ("tool_results", "Tool results"),
    ("conversation", "Conversation history"),
    ("user", "Your message"),
)
_LABELS = dict(SECTIONS)

# Untrusted context carries `metadata.source` (see prompt_security). Matched in
# order, first hit wins, so the specific rules come before the broad ones.
_LABEL_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("skills", ("skill",)),
    ("memory", ("memor",)),
    ("instructions", ("instruction", "agents.md", "repository map", "repo map")),
    ("attachments", ("attachment", "image", "upload", "screenshot")),
    ("web", ("web", "search", "page", "url", "youtube", "research", "http")),
    ("documents", ("document", "rag", "file", "note", "editor", "personal",
                   "vault", "email", "@")),
)

# "Retrieved" for the advice rule: everything pulled in on the user's behalf.
_RETRIEVED = ("skills", "memory", "documents", "web", "attachments",
              "retrieved", "instructions")


def classify(message: Dict[str, Any], *, is_last_user: bool = False) -> str:
    """Bucket one assembled message. Unknown shapes land in `conversation`."""
    if not isinstance(message, dict):
        return "conversation"
    role = message.get("role")
    if role == "system":
        return "system"
    if role == "tool":
        return "tool_results"
    meta = message.get("metadata")
    if isinstance(meta, dict) and meta.get("trusted") is False:
        label = str(meta.get("source") or "").lower()
        for key, needles in _LABEL_RULES:
            if any(n in label for n in needles):
                return key
        return "retrieved"
    if is_last_user and role == "user":
        return "user"
    return "conversation"


def _tool_tokens(tool_schemas: Optional[Iterable[Any]]) -> int:
    """Tool schemas are sent beside the messages, so they need their own count."""
    if not tool_schemas:
        return 0
    try:
        blob = json.dumps(list(tool_schemas), default=str)
    except Exception:
        blob = str(tool_schemas)
    return estimate_tokens([{"role": "system", "content": blob}])


def _last_user_index(messages: List[Dict[str, Any]]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        meta = msg.get("metadata")
        if isinstance(meta, dict) and meta.get("trusted") is False:
            continue  # retrieved context wears the user role too
        return i
    return -1


def _pct(part: int, whole: int) -> float:
    return round(part * 100.0 / whole, 1) if whole else 0.0


def _advice(by_key: Dict[str, int], total: int, context_length: int,
            tool_count: int) -> List[Dict[str, str]]:
    """One line per disproportionate section — never a wall of warnings."""
    out: List[Dict[str, str]] = []
    tools = by_key.get("tools", 0)
    retrieved = sum(by_key.get(k, 0) for k in _RETRIEVED)

    if context_length:
        tools_pct = _pct(tools, context_length)
        retrieved_pct = _pct(retrieved, context_length)
        total_pct = _pct(total, context_length)
    else:
        tools_pct = retrieved_pct = total_pct = 0.0

    if (context_length and tools_pct >= 20) or (not context_length and tools >= 4000):
        out.append({"level": "warn", "key": "tools", "text":
                    f"Tool schemas are {tools} tokens"
                    + (f" ({tools_pct}% of the window)" if context_length else "")
                    + f" across {tool_count} tools, spent before you type. Narrow the "
                      "tool set for this chat or turn off MCP servers you are not using."})
    if context_length and retrieved_pct >= 45:
        out.append({"level": "warn", "key": "retrieved", "text":
                    f"Retrieved context (skills, memories, documents, web) is "
                    f"{retrieved_pct}% of the window. Inject fewer skills/documents "
                    f"per turn, or attach the one file you actually mean."})
    if context_length and total_pct >= 85:
        out.append({"level": "warn", "key": "total", "text":
                    f"This round starts at {total_pct}% of the window — the oldest "
                    f"turns are about to be dropped. Compact the chat or start a new one."})
    elif context_length and context_length <= 16384 and total_pct >= 50:
        out.append({"level": "info", "key": "small_window", "text":
                    f"Small window ({context_length} tokens) and {total_pct}% is gone "
                    f"before the answer. Expect this model to lose instructions in "
                    f"agent mode; a longer-context model handles it better."})
    return out


def build_ledger(messages: Optional[List[Dict[str, Any]]],
                 tool_schemas: Optional[Iterable[Any]] = None,
                 *,
                 context_length: int = 0,
                 model: str = "") -> Dict[str, Any]:
    """Per-section token ledger for the request about to be sent.

    `context_length` is the model's real window when known (0 = unknown, which
    only disables the percentage-based advice, never the counts).
    """
    messages = [m for m in (messages or []) if isinstance(m, dict)]
    last_user = _last_user_index(messages)

    by_key: Dict[str, int] = {}
    counts: Dict[str, int] = {}
    for i, msg in enumerate(messages):
        key = classify(msg, is_last_user=(i == last_user))
        by_key[key] = by_key.get(key, 0) + estimate_tokens([msg])
        counts[key] = counts.get(key, 0) + 1

    tool_count = len(list(tool_schemas)) if tool_schemas else 0
    tool_tokens = _tool_tokens(tool_schemas)
    if tool_tokens:
        by_key["tools"] = by_key.get("tools", 0) + tool_tokens
        counts["tools"] = tool_count

    total = sum(by_key.values())
    sections = [
        {"key": key, "label": _LABELS[key], "tokens": by_key.get(key, 0),
         "count": counts.get(key, 0), "pct": _pct(by_key.get(key, 0), total)}
        for key, _label in SECTIONS
        if by_key.get(key, 0) > 0
    ]
    sections.sort(key=lambda s: s["tokens"], reverse=True)

    return {
        "total": total,
        "context_length": int(context_length or 0),
        "context_pct": _pct(total, context_length) if context_length else None,
        "model": model or "",
        "tool_count": tool_count,
        "sections": sections,
        "advice": _advice(by_key, total, int(context_length or 0), tool_count),
    }


def summary_line(ledger: Dict[str, Any], top: int = 3) -> str:
    """Compact one-liner for the log: `12480/32768 41% · tools 4.1k · ...`."""
    if not ledger:
        return ""
    head = f"{ledger.get('total', 0)}"
    if ledger.get("context_length"):
        head += f"/{ledger['context_length']} {ledger.get('context_pct')}%"
    bits = [f"{s['key']} {s['tokens']}" for s in (ledger.get("sections") or [])[:top]]
    return " · ".join([head] + bits)


def should_emit(previous: Optional[Dict[str, Any]],
                ledger: Dict[str, Any],
                *, growth: float = 1.25, pressure_pct: float = 75.0) -> bool:
    """Throttle: always the first round, then only real news.

    Re-emitting an unchanged ledger every round turns a useful card into noise,
    so later rounds report only meaningful growth or genuine pressure.
    """
    if not ledger:
        return False
    if not previous:
        return True
    pct = ledger.get("context_pct")
    if pct is not None and pct >= pressure_pct:
        return True
    prev_total = previous.get("total") or 0
    return bool(prev_total and ledger.get("total", 0) >= prev_total * growth)
