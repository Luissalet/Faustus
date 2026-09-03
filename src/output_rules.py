"""output_rules.py — what a worker's OWN output says about its state.

A worker that is rate-limited, sitting at a `[y/N]` prompt or repeating the
same line forever is not "still working": it is stuck in a way the operator
can name. Today Faustus only learns that after the job ends, from the
transcript. This module reads the state off the output while it runs, with
rule packs instead of per-project configuration:

    classify_output(text) → {"states": [...], "matches": [...], "confidence": …}

The states, in the order they are reported:

``rate_limited``      the provider said so ("rate limit", 429, "quota exceeded")
``waiting_for_input`` the LAST line is a prompt (`[y/N]`, `Password:`, a lone `$`)
``stuck``             the same line repeated at the tail, or no new bytes at all
``auth_error``        401 / 403 / "permission denied" / "not authorized"
``disk_full``         "no space left" / ENOSPC
``oom``               "out of memory" / "CUDA out of memory" / a bare "Killed"
``finished_ok``       an explicit exit marker whose code is 0
``failed``            an explicit exit marker whose code is not 0

**Only the tail is read.** The point of a rule pack is that it costs nothing
to run on every chunk, and re-scanning the whole scrollback on every check is
exactly what makes that false. Everything below looks at the last
``TAIL_BYTES`` characters only — a "rate limit" 400 KB up the log is history,
not the worker's state now — and :func:`tail_delta` hands a caller just the
bytes it has not classified yet.

**Substring first, regex second.** Each literal pack is a set of plain
substrings tested with ``in`` against the lower-cased tail; only a pack whose
substring hit is then confirmed with its regexes (so ``429`` in ``11429 ms``
is not a rate limit). At our scale — a few KB per check, a few dozen
substrings — a set of substrings and ``in`` is faster than building an
Aho-Corasick automaton or a Bloom filter would be, and it is auditable: this
module has no dependency to be right about. The confirming match is returned
as ``literal`` (with the whole line as ``line``) so the UI can show WHY it
thinks a worker is stuck instead of asserting it.

Pure, deterministic and total: same input, same verdict; junk in (bytes, a
dict, None, a 50 MB string) yields the empty verdict, never an exception.
Nothing here does I/O, and nothing here decides to kill anything — a detected
state is reported to the supervisor and to the operator, and that is all.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: How much of the end of the output every rule reads (characters).
TAIL_BYTES = 8192
#: How many times the same line must repeat at the tail to read as `stuck`.
STUCK_REPEATS = 3
#: How many trailing lines the repetition heuristic looks at.
STUCK_TAIL_LINES = 60
#: Longest `line` echoed back in a match.
LINE_CHARS = 200

#: Every state this module can report, in reporting order.
STATES: Tuple[str, ...] = (
    "rate_limited",
    "waiting_for_input",
    "stuck",
    "auth_error",
    "disk_full",
    "oom",
    "finished_ok",
    "failed",
)

#: States that mean "this worker is not going to progress on its own". A
#: caller surfaces these; it never kills a worker for being in one.
BLOCKED_STATES: Tuple[str, ...] = ("rate_limited", "waiting_for_input", "stuck")

# ── the literal packs ───────────────────────────────────────────────────────

def _status_code(code: str) -> Tuple[str, ...]:
    """Regexes that confirm `code` is an HTTP STATUS and not a number that
    happens to read like one. `429` in "built in 11429 ms" never had a word
    boundary; `429` in "compiling module 429" does — so the code must also sit
    where a status sits: alone on its line, after an http/status/error word or
    an arrow, or in front of what the status means. The code itself is the
    capture, so the reported literal stays `429` and not the words around it.
    """
    return (
        rf"(?m)^\s*({code})\b",
        rf"(?:http\S*|status|code|error|response|returned|says|got)\W{{0,12}}({code})\b",
        rf"[→>=:]\s*({code})\b",
        rf"\b({code})\b\s*[:\-–—]?\s*(?:unauthorized|forbidden|too many requests|client error|rate)",
    )


# {state: (substrings tested with `in`, regexes that confirm a hit, confidence)}
_LITERAL_PACKS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...], float]] = {
    "rate_limited": (
        ("rate limit", "rate-limit", "ratelimit", "429", "too many requests",
         "quota exceeded", "usage limit reached", "retry-after"),
        (r"rate[ \-]?limit(?:ed|ing|s)?", r"too many requests", r"quota exceeded",
         r"usage limit reached", r"\bretry-after\b") + _status_code("429"),
        0.8,
    ),
    "auth_error": (
        ("401", "403", "permission denied", "not authorized", "not authorised"),
        (r"permission denied", r"not authori[sz]ed") + _status_code("401") + _status_code("403"),
        0.8,
    ),
    "disk_full": (
        ("no space left", "enospc"),
        (r"no space left", r"\benospc\b"),
        0.9,
    ),
    "oom": (
        ("out of memory", "oomkilled", "killed"),
        (r"cuda out of memory", r"out of memory", r"\boomkilled\b",
         r"(?m)^[ \t]*killed[ \t]*$", r"killed process \d+"),
        0.8,
    ),
}

# A trailing prompt: the substrings are the cheap gate, the regexes confirm
# that the LAST line really ends in a question the worker cannot answer.
_PROMPT_SUBSTRINGS: Tuple[str, ...] = (
    "[y/n]", "(y/n)", "(yes/no)", "yes/no", "y/n", "password:", "passphrase:",
    "press any key", "press enter", "continue?", "proceed?", "overwrite?",
    "? [", "]:",
)
_PROMPT_PATTERNS: Tuple[str, ...] = (
    r"\[y/n\]\s*[:?]?\s*$",
    r"\(y(?:es)?/n(?:o)?\)\s*[:?]?\s*$",
    r"\byes/no\b\s*[:?]?\s*$",
    r"\bpass(?:word|phrase)\s*:\s*$",
    r"press (?:any key|enter|return)",
    r"\b(?:continue|proceed|overwrite|are you sure)\s*\?\s*$",
    r"\?\s*\[[^\]\n]{1,12}\]\s*[:?]?\s*$",
)
_PROMPT_CONFIDENCE = 0.85
#: A shell prompt left alone on the last line.
_LONE_PROMPTS = frozenset({">", ">>", ">>>", "$", "#", "?", ":"})

# An explicit exit marker, e.g. "exit code: 0", "exited with status 2",
# "[exit 1]", "process finished with exit code 0".
_EXIT_SUBSTRINGS: Tuple[str, ...] = ("exit code", "exit status", "exitcode", "exited with", "[exit ")
_EXIT_RE = re.compile(
    r"(?:exit(?:ed)?(?:\s+with)?(?:\s+(?:code|status))?|exitcode)\s*[:=]?\s*(\d{1,4})\b",
    re.IGNORECASE,
)
_EXIT_CONFIDENCE = 0.95
_STUCK_CONFIDENCE = 0.7
_STUCK_SILENT_CONFIDENCE = 0.9

_COMPILED: Dict[str, Tuple[re.Pattern, ...]] = {
    state: tuple(re.compile(p, re.IGNORECASE) for p in patterns)
    for state, (_subs, patterns, _c) in _LITERAL_PACKS.items()
}
_COMPILED_PROMPTS: Tuple[re.Pattern, ...] = tuple(re.compile(p, re.IGNORECASE) for p in _PROMPT_PATTERNS)

_EMPTY: Dict[str, Any] = {"states": [], "matches": [], "confidence": 0.0}


# ── helpers ─────────────────────────────────────────────────────────────────

def _as_text(value: Any) -> str:
    """Anything a caller may hand us as text, never raising."""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - a decoder is not worth an exception
            return ""
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return ""


def tail(text: Any, limit: int = TAIL_BYTES) -> str:
    """The last `limit` characters of `text` — the only part any rule reads."""
    s = _as_text(text)
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = TAIL_BYTES
    if n <= 0 or len(s) <= n:
        return s
    return s[-n:]


def tail_delta(previous_len: Any, text: Any) -> str:
    """The bytes of `text` a caller has NOT classified yet.

    `previous_len` is the length it had already seen. A stream that shrank
    (a restarted command, a rotated buffer) is treated as new from the start,
    which is the safe direction: classify too much once, never miss a state.
    """
    s = _as_text(text)
    try:
        seen = int(previous_len)
    except (TypeError, ValueError):
        seen = 0
    if seen <= 0:
        return s
    if seen > len(s):
        return s
    return s[seen:]


def _line_of(haystack: str, index: int) -> str:
    """The whole line `index` falls on, bounded — the `why` behind a match."""
    if index < 0:
        return ""
    start = haystack.rfind("\n", 0, index) + 1
    end = haystack.find("\n", index)
    line = haystack[start:] if end < 0 else haystack[start:end]
    line = line.strip()
    return line[:LINE_CHARS] if len(line) > LINE_CHARS else line


def _lines_at_tail(text: str, limit: int = STUCK_TAIL_LINES) -> List[str]:
    """The last non-blank lines of `text`, stripped, newest last."""
    rows = [ln.strip() for ln in text.splitlines()[-max(1, limit * 2):]]
    return [ln for ln in rows if ln][-max(1, limit):]


def _match(state: str, literal: str, line: str, confidence: float) -> Dict[str, Any]:
    return {"state": state, "literal": literal[:LINE_CHARS], "line": line, "confidence": round(float(confidence), 3)}


def _wanted(packs: Optional[Iterable[str]]) -> Optional[frozenset]:
    """The subset of STATES a caller asked for, or None for all of them."""
    if packs is None:
        return None
    try:
        names = frozenset(str(p).strip().lower() for p in packs if str(p).strip())
    except TypeError:                       # not iterable
        return None
    return names or None


# ── the rules ───────────────────────────────────────────────────────────────

def _literal_matches(state: str, lowered: str, original: str) -> List[Dict[str, Any]]:
    subs, _patterns, confidence = _LITERAL_PACKS[state]
    if not any(s in lowered for s in subs):
        return []                            # the cheap gate: no regex runs
    out: List[Dict[str, Any]] = []
    for rx in _COMPILED[state]:
        hit = None
        for hit in rx.finditer(original):
            pass                             # the LAST occurrence is the news
        if hit is not None:
            # a pattern that captures reports its capture (the status code),
            # not the words it needed around it to be sure
            literal = hit.group(1) if rx.groups else hit.group(0)
            out.append(_match(state, literal, _line_of(original, hit.start()), confidence))
            break
    return out


def _prompt_match(original: str) -> List[Dict[str, Any]]:
    """A trailing prompt: only the last non-blank line is looked at, so a
    `[y/N]` quoted half a log up the scrollback is not a waiting worker."""
    rows = _lines_at_tail(original, 1)
    if not rows:
        return []
    line = rows[-1]
    if line in _LONE_PROMPTS:
        return [_match("waiting_for_input", line, line, _PROMPT_CONFIDENCE)]
    lowered = line.lower()
    if not any(s in lowered for s in _PROMPT_SUBSTRINGS):
        return []
    for rx in _COMPILED_PROMPTS:
        hit = rx.search(line)
        if hit is not None:
            return [_match("waiting_for_input", hit.group(0).strip() or line,
                           line[:LINE_CHARS], _PROMPT_CONFIDENCE)]
    return []


def _stuck_match(original: str, repeats: int, no_new_bytes: Optional[bool]) -> List[Dict[str, Any]]:
    """The same line repeated at the tail, or a caller telling us the stream
    has produced nothing new since the last check."""
    rows = _lines_at_tail(original)
    if rows:
        last = rows[-1]
        n = 0
        for row in reversed(rows):
            if row != last:
                break
            n += 1
        if n >= max(2, int(repeats)):
            return [_match("stuck", last, f"the same line {n} times at the tail: {last}"[:LINE_CHARS],
                           _STUCK_CONFIDENCE)]
    if no_new_bytes:
        line = rows[-1] if rows else ""
        return [_match("stuck", line, "no new output since the last check", _STUCK_SILENT_CONFIDENCE)]
    return []


def _exit_match(lowered: str, original: str) -> List[Dict[str, Any]]:
    if not any(s in lowered for s in _EXIT_SUBSTRINGS):
        return []
    hit = None
    for hit in _EXIT_RE.finditer(original):
        pass                                  # the last marker is the verdict
    if hit is None:
        return []
    try:
        code = int(hit.group(1))
    except (TypeError, ValueError):
        return []
    state = "finished_ok" if code == 0 else "failed"
    return [_match(state, hit.group(0), _line_of(original, hit.start()), _EXIT_CONFIDENCE)]


def classify_output(text: Any, *, packs: Optional[Iterable[str]] = None,
                    tail_bytes: int = TAIL_BYTES, repeats: int = STUCK_REPEATS,
                    no_new_bytes: Optional[bool] = None) -> Dict[str, Any]:
    """Read the state of a worker off the tail of its output.

    Returns ``{"states": [...], "matches": [...], "confidence": float}``:
    `states` in :data:`STATES` order, `matches` carrying the literal that
    fired and the line it sits on, `confidence` the strongest match's (0.0
    when nothing matched).

    `packs` restricts the rules to those state names; `tail_bytes` how much of
    the end is read; `repeats` how many identical trailing lines read as
    `stuck`; `no_new_bytes` is the caller's own answer to "has this stream
    produced anything since the last check" — the one thing text cannot say
    about itself.

    Never raises. A verdict is a report, never a decision: nothing here kills
    a worker, and a caller must not either on the strength of it.
    """
    try:
        body = tail(text, tail_bytes)
        if not body and not no_new_bytes:
            return {"states": [], "matches": [], "confidence": 0.0}
        lowered = body.lower()
        wanted = _wanted(packs)
        exits: Optional[List[Dict[str, Any]]] = None
        matches: List[Dict[str, Any]] = []
        for state in STATES:
            if wanted is not None and state not in wanted:
                continue
            if state in _LITERAL_PACKS:
                matches.extend(_literal_matches(state, lowered, body))
            elif state == "waiting_for_input":
                matches.extend(_prompt_match(body))
            elif state == "stuck":
                matches.extend(_stuck_match(body, repeats, no_new_bytes))
            else:                             # finished_ok / failed: one marker
                if exits is None:
                    exits = _exit_match(lowered, body)
                matches.extend(m for m in exits if m["state"] == state)
        states = []
        for m in matches:
            if m["state"] not in states:
                states.append(m["state"])
        confidence = max((float(m["confidence"]) for m in matches), default=0.0)
        return {"states": states, "matches": matches, "confidence": round(confidence, 3)}
    except Exception:  # noqa: BLE001 - a classifier never breaks its caller
        return {"states": [], "matches": [], "confidence": 0.0}


def why(verdict: Any, state: Optional[str] = None) -> str:
    """One line saying why `verdict` reports `state` (the first match's line,
    else its literal). Empty when nothing matched — never raises."""
    try:
        matches = (verdict or {}).get("matches") or []
        for m in matches:
            if state is None or m.get("state") == state:
                return str(m.get("line") or m.get("literal") or "")[:LINE_CHARS]
    except Exception:  # noqa: BLE001
        pass
    return ""


def blocked(verdict: Any) -> List[str]:
    """The states in `verdict` that mean a worker will not progress by itself
    (`rate_limited`, `waiting_for_input`, `stuck`). Reported, never acted on."""
    try:
        return [s for s in (verdict or {}).get("states") or [] if s in BLOCKED_STATES]
    except Exception:  # noqa: BLE001
        return []


def known_states(names: Any) -> List[str]:
    """The subset of `names` this module can actually report (for validating a
    wait condition), preserving STATES order."""
    try:
        wanted = {str(n).strip().lower() for n in (names or ())}
    except TypeError:
        return []
    return [s for s in STATES if s in wanted]


__all__ = [
    "BLOCKED_STATES", "LINE_CHARS", "STATES", "STUCK_REPEATS", "STUCK_TAIL_LINES", "TAIL_BYTES",
    "blocked", "classify_output", "known_states", "tail", "tail_delta", "why",
]
