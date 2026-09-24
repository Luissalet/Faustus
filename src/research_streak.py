"""PENDIENTES 23-09 noche — a research streak that never asks itself if it has enough.

Live: without its own limit, a local model chained 32 `bash`+`curl` calls
reading a foreign repo's code before ever answering. The turn's round budget
would have stopped it eventually, but late — and a run that is ONLY reading
remote content, round after round, with nothing to show for it (no file
written, no plan/todo update), is a smell the loop can flag long before the
budget does.

This is a nudge, not a limit. `agent_web_streak_nudge` consecutive
"remote-read-only" rounds (default 8; 0 disables it) inject one system note
asking the model to summarise what it has learned, decide whether that is
enough, and either answer now or say exactly what fact is still missing.
Nothing is blocked — a model that genuinely needs a ninth read is free to
keep going, and the streak resets the moment a round writes something,
touches the plan/todo list, or does anything else. The note fires once per
streak: it does not repeat every round the pattern continues, only when the
pattern first crosses the threshold again after having broken.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence, Tuple

from src.agent_harness import SHELL_TOOLS

#: Tools whose entire job is reading something remote.
WEB_READ_TOOL_NAMES = frozenset({"web_search", "web_fetch", "reach_read", "reach_search"})
#: Evidence-gathering tools that cost as much as a web read and advance the
#: task just as little on their own. Live, 24-09-2026: a text-only model
#: asked the vision model 36 rounds of questions about one scanned page and
#: never wrote an answer; the turn ended asking the user whether to go on.
EVIDENCE_READ_TOOL_NAMES = WEB_READ_TOOL_NAMES | frozenset({"inspect_image"})

#: Shell-family tools whose COMMAND, not their name, decides whether a call
#: read only remote content. Shared with agent_harness's own classification
#: so the two never disagree about what counts as "a shell call".
SHELL_TOOL_NAMES = SHELL_TOOLS

#: A command segment counts as a "remote read" when it starts with one of
#: these — the exact family named in the pending item: curl, wget,
#: PowerShell's Invoke-WebRequest (and its `iwr` alias), or cloning a repo to
#: read its code. `git clone` counts as a read even though it writes a
#: directory to disk: its purpose here is fetching someone else's source to
#: look at, not advancing this turn's own workspace.
_REMOTE_READ_START_RE = re.compile(
    r"^(?:curl|wget|iwr|invoke-webrequest|git\s+clone)\b", re.IGNORECASE
)

#: A curl/wget call that saves the response to disk is not just reading it —
#: treat those conservatively as NOT a pure read, so the streak undercounts
#: rather than nudging on a round that actually produced a file.
_WRITES_TO_DISK_RE = re.compile(
    r"(?:^|\s)-o\s|(?:^|\s)-O\b|--output\b|--output-document\b|(?:^|\s)>\s*\S"
)

#: Local, read-only commands that only look at what a prior segment in the
#: SAME pipeline just fetched (`curl ... | jq .`, `curl ... | head -50`) are
#: still "just reading remote content" — piping through a filter is not a
#: local action of its own.
_LOCAL_VIEW_RE = re.compile(
    r"^(?:cat|head|tail|less|more|jq|grep|egrep|fgrep|wc|sort|uniq|tr|cut|awk|echo|python[0-9.]*\s+-c)\b",
    re.IGNORECASE,
)

#: Splits a shell command into its top-level segments on &&, ||, ;, newline
#: and single-pipe (not double-pipe, already split by the `||` alternative
#: ahead of it in the alternation).
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;\n]|\|")


def looks_like_remote_read_shell_command(command: str) -> bool:
    """True when every segment of `command` is a bare remote fetch (or a
    harmless local view piping off one), and none of them persists a
    download to disk.

    False for an empty command, a command with no remote-read segment at
    all (e.g. plain `pytest`), or one mixing in anything else (a local edit,
    a test run, an unrelated command chained on).
    """
    text = (command or "").strip()
    if not text:
        return False
    segments = [s.strip() for s in _SEGMENT_SPLIT_RE.split(text) if s.strip()]
    if not segments:
        return False
    saw_remote_read = False
    for seg in segments:
        if _REMOTE_READ_START_RE.match(seg):
            if seg.lower().startswith(("curl", "wget")) and _WRITES_TO_DISK_RE.search(seg):
                return False
            saw_remote_read = True
            continue
        if _LOCAL_VIEW_RE.match(seg):
            continue
        return False
    return saw_remote_read


_IMAGE_PATH_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|bmp|tiff?)\b", re.IGNORECASE)


def is_remote_read_only_round(calls: Sequence[Tuple[str, str]]) -> bool:
    """True when every one of this round's tool calls only read remote
    content, and the round made at least one call.

    `calls` is `[(tool_name, command_or_query), ...]` for the round's
    executed tool calls, in the shape `(block.tool_type, block.content)`
    already available in the agent loop.
    """
    if not calls:
        return False
    for name, content in calls:
        name = (name or "").strip()
        if name in EVIDENCE_READ_TOOL_NAMES:
            continue
        if name == "read_file" and _IMAGE_PATH_RE.search(str(content or "")):
            continue  # reading a picture is looking at evidence too
        if name in SHELL_TOOL_NAMES and looks_like_remote_read_shell_command(str(content or "")):
            continue
        return False
    return True


_STREAK_NUDGE_TEXT = (
    "[Runtime research check — automatic message, not a new user request] "
    "You have spent {n} rounds in a row only gathering evidence (web "
    "search/fetch, questions to the vision model, or a shell command that "
    "just fetches a URL or clones a repository) — no file written, no "
    "plan/todo update, no progress of your own. Stop and summarise what you "
    "have actually learned so far. Decide: "
    "do you already have enough to answer the original request? If yes, "
    "answer now using what you have. If not, say the ONE specific fact still "
    "missing and why the next read is needed, then continue."
)


def streak_nudge_message(streak: int) -> dict:
    """The (English) harness note for a streak of `streak` remote-read-only
    rounds. Callers wrap `content` with their own reply-language helper —
    this module has no opinion on the user's language."""
    return {
        "role": "user",
        "_harness_note": True,
        "content": _STREAK_NUDGE_TEXT.format(n=streak),
    }
