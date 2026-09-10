"""src/context_health.py — CTX-06: context health without another LLM.

The backlog is explicit about the shape this has to take: "Medir repeticion,
contradicciones con restricciones y lecturas sin progreso mediante reglas;
usar evaluacion adicional solo cuando aporte valor." Three deterministic
signals, arithmetic over the same assembled-message list
`src/context_ledger.py` already measures for the token ledger — no model
call, so `health()` costs one pass over the messages and is exact and
reproducible the way an LLM-graded verdict never is:

* **repetition** — a tool-output/retrieved-context message whose normalized
  body repeats one already seen this round. A model re-reading the same file
  and getting the same bytes back is "a read without progress" by
  definition, so the two signals share one pass (`duplicate_ratio`).
* **stale evidence** — an `EvidenceRef` stamped on a message
  (`context_ledger.evidence_index`) whose file no longer hashes to what was
  captured (`context_ledger.verify_read_evidence`). This is CTX-03's own
  contract, read here rather than reimplemented.
* **tool output ratio** — `build_ledger()`'s own per-section token counts;
  a context mostly made of tool output is a context running out of room for
  the actual task.

What this module deliberately does NOT do: judge "contradicciones con
restricciones" — deciding whether a stated constraint and a later claim
actually conflict is a semantic question a rule cannot answer honestly, and
guessing wrong would be worse than staying silent (see the report's
Limitaciones). §CTX-06's own text allows for "evaluacion adicional [...]
solo cuando aporte valor" for exactly this reason; this module gives the
deterministic two-thirds and stops rather than pretending the third is
covered.

`reconstruct_task()` is the other half — the frontend's "reconstruir tarea"
button. It never re-derives anything a store already owns; it reads what
survived compaction (`context_compactor.compact_with_integrity` already
guarantees identifiers, the user's own decisions and the current message
survive verbatim) and organises it as goal/constraints/changes/next_action so
a person (or the agent, after a restart) can see the four things the
acceptance criterion names without re-reading the whole transcript.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.context_compactor import _content_as_text, extract_protected_strings
from src.context_ledger import build_ledger, evidence_index, verify_read_evidence
from src.contracts.tool import EvidenceRef

logger = logging.getLogger(__name__)

#: Lines in a system/instruction message that read as a standing rule rather
#: than narration. Kept intentionally small and literal — a false negative
#: here only means a constraint is missing from the summary (recoverable: the
#: original message is still in context), a false positive would fabricate a
#: rule nobody stated.
_CONSTRAINT_RE = re.compile(
    r'\b(must|never|always|do not|don\'t|required|prohibited|nunca|jamás|'
    r'debe|deben|siempre|prohibido|obligatorio)\b', re.IGNORECASE)

STALE_EVIDENCE_THRESHOLD = 1  # any stale ref at all is worth surfacing
DUPLICATE_RATIO_WARN = 0.34
TOOL_OUTPUT_RATIO_WARN = 0.55


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").strip().casefold().encode("utf-8", "replace")).hexdigest()


def _is_measured_role(msg: Dict[str, Any]) -> bool:
    """Tool output and retrieved (untrusted) context — the two shapes a
    re-fetch without progress actually takes. A plain user/assistant message
    repeating itself is not this signal (see the module docstring)."""
    if msg.get("role") == "tool":
        return True
    meta = msg.get("metadata")
    return isinstance(meta, dict) and meta.get("trusted") is False


def duplicate_ratio(messages: Optional[Sequence[Dict[str, Any]]]) -> Dict[str, Any]:
    """Repetition, measured: the fraction of tool-output/retrieved messages
    whose normalized body repeats one already seen earlier in this window.

    Returns ``{"ratio", "repeated_count", "counted", "repeated_indices"}``.
    ``ratio`` is 0.0 for an empty or all-unique window — no signal is not a
    warning.
    """
    seen: Dict[str, int] = {}
    repeated: List[int] = []
    counted = 0
    for i, msg in enumerate(messages or []):
        if not isinstance(msg, dict) or not _is_measured_role(msg):
            continue
        text = _content_as_text(msg.get("content")).strip()
        if not text:
            continue
        counted += 1
        key = _hash_text(text)
        if key in seen:
            repeated.append(i)
        else:
            seen[key] = i
    ratio = round(len(repeated) / counted, 4) if counted else 0.0
    return {"ratio": ratio, "repeated_count": len(repeated), "counted": counted,
            "repeated_indices": repeated}


def _default_read_file(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def stale_evidence(
    messages: Optional[Sequence[Dict[str, Any]]], *,
    read_file: Optional[Callable[[str], Optional[str]]] = None,
) -> Dict[str, Any]:
    """Which `EvidenceRef`s stamped on THIS round's messages no longer match
    the file they point at (CTX-03's own hash contract, `verify_read_evidence`).

    `read_file` is injectable so a test never touches the real filesystem and
    so a caller with a different source of truth (a workspace sandbox, a
    remote FS) can supply its own reader; it defaults to a plain local read.
    A ref whose source is not `"file"`, or whose file cannot be read at all,
    is reported ``"unknown"`` rather than counted as stale — an unreadable
    file is not proof the content changed.
    """
    reader = read_file or _default_read_file
    index = evidence_index(list(messages or []))
    stale: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []
    for evidence_id, entry in index.items():
        raw = entry.get("evidence") or {}
        if raw.get("source_type") != "file":
            continue
        path = str(raw.get("source_ref") or "")
        if not path:
            continue
        try:
            ref = EvidenceRef.from_mapping(raw)
        except Exception:  # noqa: BLE001 - a malformed stamp is unknown, not stale
            unknown.append({"evidence_id": evidence_id, "source_ref": path})
            continue
        content = reader(path)
        if content is None:
            unknown.append({"evidence_id": evidence_id, "source_ref": path})
            continue
        if not verify_read_evidence(ref, content):
            stale.append({"evidence_id": evidence_id, "source_ref": path,
                          "message_index": entry.get("message_index")})
    return {"stale": stale, "unknown": unknown, "stale_count": len(stale)}


def health(
    messages: Optional[List[Dict[str, Any]]],
    tool_schemas: Optional[Sequence[Any]] = None,
    *, context_length: int = 0, model: str = "",
    read_file: Optional[Callable[[str], Optional[str]]] = None,
) -> Dict[str, Any]:
    """One deterministic report: the token ledger plus the three rule-based
    signals, and a single actionable verdict a UI can render as one banner.

    Never raises: a signal that cannot be computed (e.g. the evidence check,
    on a store outage) degrades to its empty/unknown shape rather than
    failing the whole report — the same posture every other context_engine
    module takes on the turn path.
    """
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    ledger = build_ledger(msgs, tool_schemas, context_length=context_length, model=model)
    dup = duplicate_ratio(msgs)
    try:
        stale = stale_evidence(msgs, read_file=read_file)
    except Exception:  # noqa: BLE001 - see docstring
        logger.debug("context_health: stale_evidence check failed", exc_info=True)
        stale = {"stale": [], "unknown": [], "stale_count": 0}

    total = max(1, int(ledger.get("total") or 0))
    tool_tokens = 0
    for section in ledger.get("sections") or ():
        if section.get("key") == "tool_results":
            tool_tokens = int(section.get("tokens") or 0)
    tool_output_ratio = round(tool_tokens / total, 4)

    flags: List[Dict[str, str]] = []
    if dup["ratio"] >= DUPLICATE_RATIO_WARN and dup["counted"] >= 3:
        flags.append({"level": "warn", "key": "repetition",
                      "text": f"{dup['repeated_count']} of {dup['counted']} tool/retrieved "
                              "messages repeat content already in this window — the agent "
                              "may be re-reading without new information."})
    if stale["stale_count"] >= STALE_EVIDENCE_THRESHOLD:
        flags.append({"level": "warn", "key": "stale_evidence",
                      "text": f"{stale['stale_count']} cited file reference(s) no longer "
                              "match the file on disk — evidence has gone stale."})
    if tool_output_ratio >= TOOL_OUTPUT_RATIO_WARN:
        flags.append({"level": "warn", "key": "tool_output",
                      "text": f"Tool output is {round(tool_output_ratio * 100)}% of this "
                              "window — little room is left for the task itself."})

    saturated = bool(ledger.get("context_pct") and ledger["context_pct"] >= 85)
    needs_rebuild = saturated or bool(flags)

    return {
        "ledger": ledger,
        "duplicate": dup,
        "stale_evidence": stale,
        "tool_output_ratio": tool_output_ratio,
        "flags": flags,
        "saturated": saturated,
        "needs_rebuild": needs_rebuild,
    }


# ── reconstruction after compaction ─────────────────────────────────────────

def _tool_call_names(msg: Dict[str, Any]) -> List[str]:
    calls = msg.get("tool_calls") if isinstance(msg, dict) else None
    if not isinstance(calls, list):
        return []
    names: List[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else call
        name = fn.get("name") if isinstance(fn, dict) else None
        if name:
            names.append(str(name))
    return names


def _constraints_from(text: str) -> List[str]:
    out: List[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip(" \t-*••")
        if stripped and _CONSTRAINT_RE.search(stripped):
            out.append(stripped[:300])
    return out


def reconstruct_task(messages: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Goal / constraints / changes / next_action, read from whatever
    survived compaction — the CTX-06 acceptance criterion's own four nouns.

    Deterministic and order-preserving:

    * ``goal`` — the most recent genuine user turn (role="user", not a
      retrieved/untrusted block — the same test `context_ledger.classify`
      uses to tell a real question from injected context).
    * ``constraints`` — lines in system/instruction messages that read as a
      standing rule (see `_CONSTRAINT_RE`); a false negative leaves the
      original message in context (nothing is deleted here), so this stays
      deliberately narrow rather than guessing.
    * ``changes`` — every file path `context_compactor.extract_protected_strings`
      found in an assistant or tool message, deduplicated in first-seen
      order — the closest a rule-only pass can get to "what did this touch".
    * ``next_action`` — the last assistant turn's tool call name(s), or (no
      tool call) its own text, whichever is the most recent sign of
      "what happens next".
    """
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    goal = ""
    constraints: List[str] = []
    changes: List[str] = []
    next_action = ""
    seen_paths: set = set()

    for i, msg in enumerate(msgs):
        role = msg.get("role")
        text = _content_as_text(msg.get("content"))
        if role == "system":
            constraints.extend(_constraints_from(text))
        if role in ("assistant", "tool"):
            for path in extract_protected_strings(text):
                if ("/" in path or "\\" in path) and path not in seen_paths:
                    seen_paths.add(path)
                    changes.append(path)
        if role == "user" and not (isinstance(msg.get("metadata"), dict)
                                   and msg["metadata"].get("trusted") is False):
            goal = text.strip()[:2000] or goal

    for msg in reversed(msgs):
        if msg.get("role") != "assistant":
            continue
        names = _tool_call_names(msg)
        if names:
            next_action = f"tool: {', '.join(names)}"
        else:
            next_action = _content_as_text(msg.get("content")).strip()[:500]
        break

    # de-dupe constraints, order preserved
    dedup_constraints: List[str] = []
    seen_c: set = set()
    for c in constraints:
        key = c.casefold()
        if key not in seen_c:
            seen_c.add(key)
            dedup_constraints.append(c)

    return {
        "goal": goal,
        "constraints": dedup_constraints[:50],
        "changes": changes[:100],
        "next_action": next_action,
    }


__all__ = [
    "duplicate_ratio", "stale_evidence", "health", "reconstruct_task",
    "DUPLICATE_RATIO_WARN", "TOOL_OUTPUT_RATIO_WARN", "STALE_EVIDENCE_THRESHOLD",
]
