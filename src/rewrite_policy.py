"""rewrite_policy.py — deterministic policy against repeated whole-file
rewrites of the same file within one turn (H4).

``src/agent_harness.py`` already NOTICES a whole-file rewrite of an existing
file (``TurnLedger.record`` appends a ``whole_file_rewrite:<path>`` note when
``write_file`` replaces >=5 lines of a file that was not new). It only
records the fact — nothing discourages the model from doing it again, and
the silhouettes forensic analysis (chat ``b781a317``, 14-09) shows the same
file (``vector.py`` / ``trace.py``, both well over 150 lines) rewritten whole
8-10 times inside a single turn: a 27B local model that does not trust its
own last edit and "starts over" from memory every time it sees a new
failure, silently dropping invariants a smaller, targeted diff would have
kept.

This module is that policy, pulled out the same way ``loop_breaker.py`` is:
a pure, dependency-free state machine with no knowledge of the tool-calling
loop, streaming, or an LLM. One instance per TURN (a fresh
:class:`RewritePolicy` at the start of every turn — never shared across
turns, and never across a coordinator and its delegated workers, mirroring
``LoopPolicy``'s own contract). ``H45_wiring.md`` has the exact diff to call
it from ``write_file`` before the write lands; it is not applied here (out of
this lot's file ownership — see ``CONTRATO.md`` rule 2).

Rules (exactly as specced):

  * Only ``write_file`` can trigger the policy. ``edit_file`` and
    ``apply_patch`` are surgical by construction and never count, no matter
    how many times they touch the same file.
  * A file that did not exist before this call (a brand-new file) never
    counts — writing a new file is not a "rewrite" of anything.
  * A file whose PRIOR size was at or under the size floor (default 150
    lines) never counts — the whole point is protecting a large file a small
    model cannot hold in working memory across several full rewrites; a
    75-line script rewritten five times is not that failure mode.
  * The 1st qualifying rewrite of a given (turn, path) is always ``ok``.
  * From the ``require_edit_after``-th qualifying rewrite (default 2nd) the
    verdict becomes ``require_edit``: the integrator makes ``write_file``
    refuse and point at ``edit_file``/``apply_patch`` with a diff instead.
  * From the ``block_after``-th (default 4th) it becomes ``block``: the
    integrator's harness round injects a stronger instruction ("read the
    file and the diff before touching it again") rather than just refusing.
  * The counters reset per turn (a fresh instance, or call :meth:`reset`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

#: Verdicts observe() can return, in the order a repeat streak passes
#: through them. "ok" is the overwhelming majority case.
VERDICTS = ("ok", "require_edit", "block")

DEFAULT_REQUIRE_EDIT_AFTER = 2
DEFAULT_BLOCK_AFTER = 4
DEFAULT_MIN_LINES = 150

#: Only this tool can ever trigger the policy — see the module docstring.
COUNTED_TOOL = "write_file"
#: Tools the contract explicitly excludes forever, regardless of size/count.
NEVER_COUNTED_TOOLS = frozenset({"edit_file", "apply_patch"})


def _norm_path(path: str) -> str:
    return (path or "").replace("\\", "/").strip().lower()


def _line_count(text: Optional[str]) -> int:
    """Lines in `text` the way a human would count them: an empty string is
    0 lines (no file / an empty file), not 1."""
    if not text:
        return 0
    return len(text.splitlines())


@dataclass
class RewritePolicy:
    """One instance per turn. Call :meth:`observe` once per successful
    ``write_file`` (see ``H45_wiring.md`` for exactly where); it never looks
    at anything outside its own running counters, so two turns — or a
    coordinator and a worker it delegated to — never interfere through it.
    """
    require_edit_after: int = DEFAULT_REQUIRE_EDIT_AFTER
    block_after: int = DEFAULT_BLOCK_AFTER
    min_lines: int = DEFAULT_MIN_LINES
    #: "off" disables the policy outright (observe() always returns "ok").
    #: Any other value enables the full ladder above — kept as a string (not
    #: a bool) so a future value can select a different ladder shape without
    #: another setting, matching the contract's `agent_rewrite_policy`
    #: (default "require_edit").
    mode: str = "require_edit"

    _counts: Dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # A misconfigured setting must not make the ladder skip a rung or
        # divide by zero — clamp to a monotonic, at-least-one-apart sequence,
        # the same defensive posture as LoopPolicy.__post_init__.
        self.require_edit_after = max(1, int(self.require_edit_after or DEFAULT_REQUIRE_EDIT_AFTER))
        self.block_after = max(self.require_edit_after + 1, int(self.block_after or DEFAULT_BLOCK_AFTER))
        self.min_lines = max(0, int(self.min_lines if self.min_lines is not None else DEFAULT_MIN_LINES))

    @classmethod
    def from_settings(cls, get_setting=None) -> "RewritePolicy":
        """Build a policy from the project's settings store (falls back to
        the defaults above if `src.settings` cannot be imported), mirroring
        ``LoopPolicy.from_settings``."""
        if get_setting is None:
            try:
                from src.settings import get_setting as _get_setting
                get_setting = _get_setting
            except Exception:
                get_setting = lambda key, default=None: default  # noqa: E731
        return cls(
            mode=str(get_setting("agent_rewrite_policy", "require_edit") or "require_edit"),
            require_edit_after=int(get_setting("agent_rewrite_policy_require_edit_after",
                                                DEFAULT_REQUIRE_EDIT_AFTER) or DEFAULT_REQUIRE_EDIT_AFTER),
            block_after=int(get_setting("agent_rewrite_policy_block_after",
                                         DEFAULT_BLOCK_AFTER) or DEFAULT_BLOCK_AFTER),
            min_lines=int(get_setting("agent_rewrite_policy_min_lines",
                                       DEFAULT_MIN_LINES) or DEFAULT_MIN_LINES),
        )

    @property
    def enabled(self) -> bool:
        return str(self.mode or "").strip().lower() not in ("", "off", "disabled", "false")

    def observe(self, path: str, tool: str, lines_before: int, lines_after: int = 0) -> str:
        """Record one write and return the verdict: one of :data:`VERDICTS`.

        `lines_before` is the line count of the file as it stood immediately
        before this write — 0 (or falsy) means the file did not exist yet.
        `lines_after` is accepted for symmetry with the tool's before/after
        pair and for future ladder shapes; today's rule only needs
        `lines_before` (the size that made the file worth protecting) and
        whether this is a repeat, not how big the new content is.
        """
        if not self.enabled:
            return "ok"
        if tool != COUNTED_TOOL:
            # edit_file / apply_patch (or anything else) never counts, and
            # never even resets a path's streak — a surgical edit in between
            # two whole-file rewrites does not excuse the next rewrite.
            return "ok"
        try:
            before = int(lines_before or 0)
        except (TypeError, ValueError):
            before = 0
        existed = before > 0
        if not existed:
            return "ok"          # brand-new file: never a "rewrite"
        if before <= self.min_lines:
            return "ok"          # too small to be the failure mode this guards against
        key = _norm_path(path)
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        if count >= self.block_after:
            return "block"
        if count >= self.require_edit_after:
            return "require_edit"
        return "ok"

    def observe_lines(self, path: str, tool: str, before_text: Optional[str],
                       after_text: Optional[str] = None) -> str:
        """Convenience wrapper: counts lines in the raw before/after text
        instead of requiring the caller to count them first."""
        return self.observe(path, tool, _line_count(before_text), _line_count(after_text))

    def rewrite_count(self, path: str) -> int:
        """Qualifying rewrites of `path` observed so far this turn (0 if
        never counted, including every new/small-file/non-write_file call)."""
        return self._counts.get(_norm_path(path), 0)

    def reset(self) -> None:
        """Explicit reset for a caller that wants to start a fresh turn
        without allocating a new instance."""
        self._counts.clear()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "mode": self.mode, "enabled": self.enabled,
            "require_edit_after": self.require_edit_after,
            "block_after": self.block_after, "min_lines": self.min_lines,
            "counts": dict(self._counts),
        }


def deny_message(path: str, verdict: str, count: int, min_lines: int = DEFAULT_MIN_LINES) -> str:
    """The text an integrator's `write_file` (or the harness round that
    follows a `block`) shows the model. Bilingual is not required here —
    unlike test_debt's user-facing todo text, this is a tool-error string the
    MODEL reads, and Faustus's tool errors are English-only elsewhere too."""
    if verdict == "block":
        return (
            f"write_file: refused. {path} has been rewritten whole {count} times this turn "
            f"(it was over {min_lines} lines before this write). Stop rewriting it from memory: "
            f"read_file the CURRENT version of {path} and the diff of your last edit_file/apply_patch "
            "to it before touching it again, then make one small, targeted change with edit_file or "
            "apply_patch. Rewriting the whole file again will be refused."
        )
    return (
        f"write_file: refused. {path} already exists and is over {min_lines} lines; this would be "
        f"its {count}th full rewrite this turn. Use edit_file (or apply_patch with a diff) to make a "
        "targeted change instead of replacing the whole file — a full rewrite risks silently dropping "
        "code from parts of the file you are not currently looking at."
    )


def deny_result(path: str, verdict: str, count: int, min_lines: int = DEFAULT_MIN_LINES) -> Dict[str, Any]:
    """The exact `write_file` tool-result payload H45_wiring.md specifies for
    a `require_edit`/`block` verdict."""
    return {
        "error": deny_message(path, verdict, count, min_lines),
        "exit_code": 1,
        "policy": "rewrite_policy",
        "policy_verdict": verdict,
    }
