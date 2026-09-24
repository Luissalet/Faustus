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

#: A `python` call that only opens an image, crops/resizes/enhances it and
#: saves the result as another image is preparing evidence, not advancing
#: the task. Live, 24-09-2026 (exam run 13): the model learnt to crop the
#: page with PIL and hand each crop to `inspect_image`; every crop call
#: broke the evidence streak, so 29 rounds of re-transcribing a page that
#: already had a human transcription never triggered the check.
_IMAGE_OPEN_RE = re.compile(r"\b(?:Image\.open|cv2\.imread|imageio\.imread)\s*\(")
_IMAGE_SAVE_RE = re.compile(r"\.save\s*\(|\bimwrite\s*\(")
_IMAGE_SAVE_TARGET_RE = re.compile(
    r"(?:\.save|\bimwrite)\s*\(\s*(?:f?r?[\"'][^\"']*\.(?:png|jpe?g|gif|webp|bmp|tiff?)[\"']"
    r"|[A-Za-z_][A-Za-z0-9_]*\s*[,)])",
    re.IGNORECASE,
)
#: Anything that writes text, runs processes or reaches the network is doing
#: more than preparing a picture.
_NON_IMAGE_SIDE_EFFECT_RE = re.compile(
    r"\.write\s*\(|write_text|write_bytes|json\.dump\b|to_csv|subprocess|os\.system|"
    r"requests\.|urllib|httpx|open\s*\([^)]*[\"'][wax]\+?b?[\"']",
    re.IGNORECASE,
)


def _python_code(content: str) -> str:
    text = str(content or "")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            import json as _json
            parsed = _json.loads(stripped)
        except Exception:  # noqa: BLE001 - not JSON: it is the code itself
            return text
        if isinstance(parsed, dict):
            return str(parsed.get("code") or parsed.get("content") or "")
    return text


def looks_like_image_prep_code(content: str) -> bool:
    """True when a `python` call only opens image(s) and saves image crops or
    enhanced copies: no text written, no process run, no network."""
    code = _python_code(content)
    if not code.strip() or not _IMAGE_OPEN_RE.search(code):
        return False
    if _NON_IMAGE_SIDE_EFFECT_RE.search(code):
        return False
    saves = len(_IMAGE_SAVE_RE.findall(code))
    if saves == 0:
        return False
    return len(_IMAGE_SAVE_TARGET_RE.findall(code)) >= saves


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
        if name == "python" and looks_like_image_prep_code(str(content or "")):
            continue  # cropping a picture to ask about it is part of the look
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
    "missing and why the next read is needed, then continue. For images: "
    "when the workspace already holds a transcription of a picture, do not "
    "re-transcribe it crop by crop — ask inspect_image action=\"unlisted\" "
    "(with that transcription) for only what it leaves out, and target the "
    "marks, symbols and numbers the puzzle actually depends on."
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


# ---------------------------------------------------------------------------
# escalation: a streak that ignores the first check
# ---------------------------------------------------------------------------
#
# Live, 24-09-2026 (exam run 13): one check at eight rounds was not enough —
# the model acknowledged it and went on for another thirty rounds of
# questions to the vision model. The ladder below keeps it a nudge first and
# only then takes the reading tools away, briefly:
#
#   streak == N   → "nudge"   (the message above)
#   streak == 2N  → "insist"  (write down what you know, attempt the answer)
#   streak == 3N  → "pause"   (evidence tools withheld for PAUSE_ROUNDS
#                              rounds: reason, compute, answer)
#   every N after → "pause" again
#
# N is `agent_web_streak_nudge`; 0 disables the whole ladder.

#: Rounds the evidence tools stay withheld once the ladder reaches "pause".
PAUSE_ROUNDS = 2

#: Exact repeats of earlier vision questions in a row (inspect_image's
#: ledger) that pause the evidence tools right away, whatever the streak.
REPEAT_RUN_PAUSE = 3


def streak_action(streak: int, nudge_at: int) -> str:
    """What the loop should do after a round that left the evidence streak
    at ``streak``. One of ``none``, ``nudge``, ``insist``, ``pause``; each
    step fires exactly on its threshold round, so calling this every round
    never repeats a message within one step."""
    if nudge_at <= 0 or streak <= 0 or streak % nudge_at:
        return "none"
    step = streak // nudge_at
    if step == 1:
        return "nudge"
    if step == 2:
        return "insist"
    return "pause"


_INSIST_TEXT = (
    "[Runtime research check — automatic message, not a new user request] "
    "Second check: {n} rounds in a row only gathering evidence, and the "
    "first check did not change course. Before ANY other read: write the "
    "facts you have established so far as a short numbered list (update "
    "your todo list with them), then try to solve the task from that list — "
    "combine the clues, compute what can be computed, and write a first "
    "answer, even a provisional one. Only if a specific step of that attempt "
    "fails for lack of one precise fact may you read again, and only for "
    "that fact. If the reading tools are used again without that, they will "
    "be paused."
)

_PAUSE_TEXT = (
    "[Runtime research check — automatic message, not a new user request] "
    "{n} rounds in a row only gathering evidence. The evidence tools "
    "({tools}) are paused for the next {rounds} rounds. You already have a "
    "large amount of material in this conversation: reason with it now — "
    "list the clues, connect them, run any calculation with a local tool, "
    "and write your answer (or the best provisional answer, saying exactly "
    "what remains uncertain). The tools come back after the pause."
)


_REPEAT_PAUSE_TEXT = (
    "[Runtime research check — automatic message, not a new user request] "
    "Your last {n} image questions were all exact repeats of questions you "
    "had already asked in this conversation; their answers are already "
    "above. The evidence tools ({tools}) are paused for the next {rounds} "
    "rounds. Scroll back through the answers you already have, list the "
    "clues they give, connect them, compute what can be computed with a "
    "local tool, and write your answer (or the best provisional one, saying "
    "exactly what remains uncertain)."
)


def repeat_pause_message(repeats: int, tools: Sequence[str], rounds: int = PAUSE_ROUNDS) -> dict:
    return {
        "role": "user",
        "_harness_note": True,
        "content": _REPEAT_PAUSE_TEXT.format(n=repeats, tools=", ".join(sorted(tools)), rounds=rounds),
    }


def insist_message(streak: int) -> dict:
    return {"role": "user", "_harness_note": True, "content": _INSIST_TEXT.format(n=streak)}


def pause_message(streak: int, tools: Sequence[str], rounds: int = PAUSE_ROUNDS) -> dict:
    return {
        "role": "user",
        "_harness_note": True,
        "content": _PAUSE_TEXT.format(n=streak, tools=", ".join(sorted(tools)), rounds=rounds),
    }


def evidence_tools_to_pause(available: Sequence[str]) -> Tuple[str, ...]:
    """The evidence tools present in ``available`` (the turn's tool names).
    Shell tools are never paused: they are how the model computes."""
    have = set(available or ())
    return tuple(sorted(name for name in EVIDENCE_READ_TOOL_NAMES if name in have))
