"""Whole-turn "no progress" watch: rounds of tools that fix nothing.

The other stall guards each watch one shape: `loop_breaker` the same call
with the same result, the web-read streak remote fetches, the image-view
nudge vision questions, the plan check a todo step that never moves. None of
them sees a turn that alternates sources -- read a file, grep, run a probe,
read another file, ask the vision model -- for fifty rounds and never writes
anything down. That is how an exam run ended after four and a half hours with
no deliverable.

This watch keeps a fingerprint of what the turn has *fixed*:

* a successful mutation or effect (a file written, an edit, a shell command
  that writes, a tool with an outside effect, a worker's verified change);
* a plan step closed (the count of completed todos went up);
* a question put to the user.

Every round with tool calls where that fingerprint did not change adds one
to the streak; any change resets it. At `rounds` the runtime asks, once, for
a first version now; at twice that it insists, once. It never blocks a tool
or ends the turn: that is the loop breaker's job, for the narrower case it
can prove. A plan-only round (a todowrite that closes nothing) is neutral.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

#: Tool calls that only maintain the plan: neutral rounds.
PLAN_TOOLS = frozenset({"todowrite", "update_plan"})

#: Ledger kinds that fix something.
PROGRESS_KINDS = frozenset({"mutation", "effect"})


@dataclass
class ProgressWatch:
    rounds: int = 15
    streak: int = 0
    completed: int = 0
    nudged: bool = False
    insisted: bool = False
    history: List[int] = field(default_factory=list)

    @classmethod
    def from_settings(cls, get_setting) -> "ProgressWatch":
        try:
            n = int(get_setting("agent_no_progress_rounds", 15) or 0)
        except (TypeError, ValueError):
            n = 15
        return cls(rounds=max(0, n))

    def observe(self, round_tools: Iterable[str], round_events: Iterable[Dict[str, Any]],
                todos: Optional[List[Dict[str, Any]]] = None, asked_user: bool = False) -> str:
        """Fold one finished round in; return "none", "nudge" or "insist"."""
        tools = [str(t) for t in round_tools if t]
        if not tools:
            return "none"
        done = sum(1 for t in (todos or []) if isinstance(t, dict) and str(t.get("status")) == "completed")
        closed_step = done > self.completed
        self.completed = max(self.completed, done)
        fixed = asked_user or closed_step or any(
            isinstance(e, dict) and e.get("ok") and e.get("kind") in PROGRESS_KINDS for e in round_events
        )
        if fixed:
            if self.streak:
                self.history.append(self.streak)
            self.streak = 0
            self.nudged = self.insisted = False
            return "none"
        if all(t in PLAN_TOOLS for t in tools):
            return "none"
        self.streak += 1
        if self.rounds <= 0:
            return "none"
        if self.streak >= 2 * self.rounds and not self.insisted:
            self.insisted = self.nudged = True
            return "insist"
        if self.streak >= self.rounds and not self.nudged:
            self.nudged = True
            return "nudge"
        return "none"

    def longest(self) -> int:
        return max([self.streak, *self.history]) if (self.history or self.streak) else 0


def message(action: str, streak: int) -> str:
    head = "[Runtime progress check — automatic message, not a new user request] "
    if action == "insist":
        return head + (
            f"{streak} rounds of tool calls and still nothing written, no plan step closed and no "
            "question asked. Stop gathering now. In this round either write the deliverable the task "
            "asks for (or, if it asks for none, your answer) with what you have, marking open points "
            "as open, or ask the user the one question that blocks you. Further reading only after that, "
            "and only for a named gap.")
    return head + (
        f"The last {streak} rounds used tools but fixed nothing: no file written, no plan step "
        "closed, no question to the user. Put down what you have now: if the task asks for a file, "
        "write a first version of it (gaps marked as gaps); if it only asks for an answer and you "
        "have enough, answer; if you are blocked on a decision, ask. Then continue with the specific "
        "gaps left.")
