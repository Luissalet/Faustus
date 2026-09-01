"""read_plan.py — read a big file with an index, not with scissors.

`read_file` used to answer a 7691-line module by handing back the first 20000
characters and the note `... [truncated at 20000 chars]`. On src/agent_loop.py
that is 5% of the file — the imports and the constants — with no line count, no
index, and no hint that a range could be asked for. A capable model works around
it by sending `offset`/`limit` unprompted. A 9B one does not: it reads the head,
believes it has seen the module, and edits or invents from there. Rounds get
burnt re-reading the same 5%.

The other end of the same problem: 20000 characters is a third to a half of an
8-16k window. Spending it on the top of a file the model did not ask for is what
pushes the user's own message out of the next trim.

So an oversized read answers with a *map* instead of a slice:

  * the facts — how many lines, how big, what the cap is for this model;
  * the symbol index with line numbers, so the next read is aimed;
  * the first ~80 lines, whole lines only;
  * the literal call that fetches any other part.

Nothing here invents a second symbol extractor. `src/repo_map.py` already parses
Python with `ast` and the other languages with per-language regexes; this module
asks it for the same symbols with their line numbers (`repo_map.symbol_lines`).

Two things are deliberately left exactly as they were:
  * an explicit `offset`/`limit` read — the model already said what it wants;
  * any file that fits under the cap — byte-for-byte the old output.

A file with no symbols to index (a .log, a .csv, a binary) still gets the facts
header and the range instruction, and keeps the whole budget for content — the
index is what buys its space from the body, so where there is no index the body
is as long as it was before.

The cap follows the model's real window, the way `src/tool_slimming.py` scales
tool prose: a proven window sets the budget, an unproven one changes nothing
(`MAX_READ_CHARS`, exactly today's ceiling). Guessing small on an unknown window
would silently truncate a 128k model's reads.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from src.constants import MAX_READ_CHARS
from src.repo_map import CHARS_PER_TOKEN, lang_for_path, symbol_lines

logger = logging.getLogger(__name__)

# Share of the model's context window one un-ranged read may occupy.
DEFAULT_FRACTION = 0.25
# Never plan below this: on a tiny window a read still has to be worth making.
MIN_BUDGET_CHARS = 2_000
# Lines of the file shown under the index.
HEAD_LINES = 80
# `limit` used in the example call; ~120 lines is a screenful the model can act on.
SUGGESTED_LIMIT = 120
# Share of the budget the index may take before the body starts losing lines.
INDEX_SHARE = 0.45
# Decoded characters read to build the index. Above this the head and the index
# cover the first slice only — a multi-MB file is not worth parsing in a read.
MAX_PLAN_CHARS = 4_000_000
# Extra characters scanned just to count lines. Past it the total is unknown and
# said to be unknown, rather than a number that is quietly wrong.
MAX_SCAN_CHARS = 64_000_000


class Plan(NamedTuple):
    """What an un-ranged read of one file should return.

    `output` is the replacement text when the file is oversized, and None when
    the caller should keep doing exactly what it did before.
    """
    path: str
    display_path: str
    size_bytes: int
    total_lines: Optional[int]      # None when the file was too big to count
    budget_chars: int
    window_tokens: int
    oversized: bool
    symbols: List[Tuple[str, int]]
    head_lines: int
    output: Optional[str]


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def budget_chars(window_tokens: int = 0, fraction: Optional[float] = None) -> int:
    """Characters one un-ranged read may return, given the model's window.

    An unknown window (0) returns `MAX_READ_CHARS` — the ceiling that has always
    applied. A proven window scales down from it and never up: `MAX_READ_CHARS`
    stays the single source of truth for how much output a read may produce.
    """
    if fraction is None:
        fraction = _setting("agent_read_window_fraction", DEFAULT_FRACTION)
    try:
        share = float(fraction)
    except (TypeError, ValueError):
        share = DEFAULT_FRACTION
    if not 0 < share <= 1:
        share = DEFAULT_FRACTION
    try:
        window = int(window_tokens or 0)
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        return MAX_READ_CHARS
    scaled = int(window * share) * CHARS_PER_TOKEN
    return max(MIN_BUDGET_CHARS, min(MAX_READ_CHARS, scaled))


def resolve_window_tokens(ctx: Optional[dict] = None) -> int:
    """The model's context window, if it is already known — never a fresh probe.

    A tool call has no route in hand, so this reads what the turn published and
    what `model_context.get_context_length` has already proven in this process.
    It deliberately does not *ask* the endpoint: that call carries a 20 s
    timeout, and a file read is no place to spend it. Unknown (0) is a fine
    answer — it means the cap stays at `MAX_READ_CHARS`.
    """
    try:
        sources: List[Dict[str, Any]] = []
        if isinstance(ctx, dict):
            sources.append(ctx)
        try:
            from src.tool_execution import get_active_turn_options
            sources.append(get_active_turn_options())
        except Exception:
            pass
        for src in sources:
            for key in ("context_length", "window_tokens"):
                try:
                    value = int(src.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    return value
        # Windows already discovered this process, most recent last. Populated by
        # get_context_length() on the route the agent is running on.
        from src import model_context
        cache = getattr(model_context, "_context_cache", None)
        if isinstance(cache, dict) and cache:
            for ctx_len, known in reversed(list(cache.values())):
                if known and int(ctx_len) > 0:
                    return int(ctx_len)
    except Exception as e:      # never let window discovery break a read
        logger.debug("[read-plan] window unknown: %s", e)
    return 0


def outline(abs_path: str) -> List[Tuple[str, int]]:
    """`symbol → line` for one file, ordered by line ('' languages give []).

    Reuses `repo_map`'s extractors (Python via `ast`, the rest via the
    per-language regexes) — this module has no symbol parsing of its own.
    """
    lang = lang_for_path(abs_path)
    if lang == "none":
        return []
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(MAX_PLAN_CHARS)
    except OSError:
        return []
    return _outline_from_text(text, lang)


def _outline_from_text(text: str, lang: str) -> List[Tuple[str, int]]:
    """Line-ordered outline, one row per name.

    A name reassigned later in the file (src/agent_loop.py rebinds
    `_AGENT_PREAMBLE` at 347 and again at 465) gets its first line only: the
    second row costs index space and sends the model to the same place. This is
    the first-wins rule the regex extractor already applies to its own matches.
    """
    try:
        found = symbol_lines(text, lang)
    except Exception as e:      # a pathological file must not break the read
        logger.debug("[read-plan] outline failed: %s", e)
        return []
    seen: set = set()
    unique: List[Tuple[str, int]] = []
    for name, line in sorted(found, key=lambda s: (s[1], s[0])):
        if name in seen:
            continue
        seen.add(name)
        unique.append((name, line))
    return unique


def _read_head_and_count(abs_path: str) -> Tuple[str, Optional[int]]:
    """First `MAX_PLAN_CHARS` characters, and the file's total line count.

    Opened exactly like `read_file` opens it — same encoding, same error
    handling, same universal-newline translation, so a CRLF file is counted and
    shown in the LF form the tool has always returned. The count is None when
    the file is longer than `MAX_SCAN_CHARS`.
    """
    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
        head = f.read(MAX_PLAN_CHARS)
        lines = head.count("\n")
        scanned = len(head)
        tail_ends_newline = head.endswith("\n") if head else True
        while True:
            chunk = f.read(1_000_000)
            if not chunk:
                break
            lines += chunk.count("\n")
            scanned += len(chunk)
            tail_ends_newline = chunk.endswith("\n")
            if scanned > MAX_SCAN_CHARS:
                return head, None
    if not tail_ends_newline:
        lines += 1          # a final line with no trailing newline still counts
    return head, lines


def _render_index(symbols: List[Tuple[str, int]], limit_chars: int) -> str:
    """The symbol index, whole entries only — never an entry cut in half."""
    out: List[str] = []
    used = 0
    for name, line in symbols:
        entry = f"{line:>6}: {name}"
        if used + len(entry) + 1 > limit_chars:
            break
        out.append(entry)
        used += len(entry) + 1
    if len(out) < len(symbols):
        out.append(f"  … +{len(symbols) - len(out)} more symbols further down the file")
    return "\n".join(out)


def plan(abs_path: str,
         window_tokens: int = 0,
         *,
         display_path: Optional[str] = None,
         enabled: Optional[bool] = None,
         fraction: Optional[float] = None) -> Plan:
    """Decide what an un-ranged `read_file` on this path should return.

    `output` is None whenever the caller should keep doing exactly what it did
    before: the file fits under the cap, or the feature is off. OSError is *not*
    caught — a missing, unreadable or directory path has to surface the same
    error `read_file` has always produced.
    """
    display = display_path or abs_path
    budget = budget_chars(window_tokens, fraction)
    size = os.stat(abs_path).st_size
    window = int(window_tokens or 0)
    fits = Plan(abs_path, display, size, None, budget, window, False, [], 0, None)
    if enabled is None:
        enabled = bool(_setting("agent_read_outline", True))
    if not enabled:
        return fits
    # UTF-8 decodes to at most one character per byte, and newline translation
    # only ever shortens the text, so a file this small cannot exceed the cap in
    # characters either: the old path returns it whole. This is the check that
    # keeps small reads byte-for-byte identical.
    if size <= budget:
        return fits

    head, total_lines = _read_head_and_count(abs_path)
    complete = len(head) < MAX_PLAN_CHARS
    if complete and len(head) <= budget:
        # Multi-byte characters (or CRLF) shrank it under the cap after all.
        return fits

    lang = lang_for_path(abs_path)
    symbols = _outline_from_text(head, lang)
    output, head_lines = _render(display, size, total_lines, budget, head, symbols)
    return Plan(abs_path, display, size, total_lines, budget, window,
                True, symbols, head_lines, output)


def _call_example(display: str, offset: int) -> str:
    """The literal tool call the model should copy, with a real offset in it."""
    return ('read_file {"path": "%s", "offset": %d, "limit": %d}'
            % (display.replace("\\", "\\\\").replace('"', '\\"'), offset, SUGGESTED_LIMIT))


def _render(display: str,
            size: int,
            total_lines: Optional[int],
            budget: int,
            head: str,
            symbols: List[Tuple[str, int]]) -> Tuple[str, int]:
    """Assemble the reply. Returns (text, number of file lines shown)."""
    counted = total_lines is not None
    facts = (f"{total_lines} lines, {_human_size(size)}" if counted
             else f"{_human_size(size)} (too large to count its lines)")
    of_total = f" of {total_lines}" if counted else ""
    lines = head.split("\n")
    if lines and lines[-1] == "":
        lines.pop()             # a trailing newline does not open a new line

    index_text = ""
    if symbols:
        index_text = _render_index(symbols, max(400, int(budget * INDEX_SHARE)))

    # The example offset points deep into the file, at a real symbol, so that
    # "a line number is the offset" is demonstrated rather than described — an
    # example one line past the head just looks like "read on" and gets copied
    # as-is. Falls back to the first unshown line when there are no symbols.
    deep = [ln for _, ln in symbols if ln > HEAD_LINES]
    example_offset = deep[len(deep) // 2] if deep else HEAD_LINES + 1

    header = [f"{display}: {facts}. "
              f"Too large to return in full (cap {budget} chars for this model)."]
    if index_text:
        header.append("Below: every symbol in the file with its line number, then the first lines.")
        header.append("Read any other part by passing a line range — a symbol's line is the offset:")
        header.append(f"  {_call_example(display, example_offset)}")
    else:
        header.append("No symbol index: this is not a source file. Read any other part by line range:")
        header.append(f"  {_call_example(display, example_offset)}")
    header_text = "\n".join(header)

    # Reserve room for the header, the index and the closing instruction; what is
    # left is content, cut only at line boundaries so nothing arrives half-shown.
    footer_reserve = 200 + len(display)
    body_budget = max(200, budget - len(header_text) - len(index_text) - footer_reserve)
    shown: List[str] = []
    used = 0
    hit_budget = False
    for line in lines[:HEAD_LINES] if index_text else lines:
        if used + len(line) + 1 > body_budget:
            hit_budget = True
            break
        shown.append(line)
        used += len(line) + 1
    last = len(shown)

    parts = [header_text, ""]
    if index_text:
        parts += [f"=== SYMBOLS ({len(symbols)}) — line: name ===", index_text, ""]
    parts.append(f"=== LINES 1-{last}{of_total} ===")
    parts.append("\n".join(shown))
    remaining = (total_lines - last) if total_lines is not None else None
    if remaining is None or remaining > 0:
        left = f"{remaining} more lines" if remaining is not None else "the rest of the file"
        # The old marker is kept where it is still true — a body that really did
        # run into the character cap. When the body stopped at HEAD_LINES with
        # budget to spare, saying "truncated at N chars" would be a lie about a
        # reply that is a third of N.
        why = (f"... [truncated at {budget} chars] — {left} not shown."
               if hit_budget or not index_text
               else f"... {left} not shown; the index above says what is in them.")
        parts.append(f"\n{why} Continue with {_call_example(display, last + 1)}")
    return "\n".join(parts), last
