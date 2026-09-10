"""plan_state.py — structured steps behind the plan's free-text checklist.

`UpdatePlanTool` (src/agent_tools/interaction_tools.py) used to hard-truncate
the plan markdown at 8,192 characters (TASK-01) — silently, so a long plan
lost its tail with no error and no trace in the transcript. That cap is gone;
this module is what replaces "the frontend just re-renders whatever markdown
arrived" with something a caller can reason about: stable step identity,
status, dependency and evidence, without ever discarding the checklist the
model actually wrote.

Two directions, and neither is allowed to lose information:

* `from_markdown` reads a GitHub-style checklist (`- [ ]`, `- [x]`, numbered,
  nested by indentation) into `PlanStep`s. A line that doesn't parse becomes a
  warning, never a dropped step — the raw `plan` markdown field the caller
  already has is untouched regardless, so nothing here can make a plan worse
  than free text already was.
* `to_markdown` reconstructs a checklist from structured steps (used when the
  model sends the newer `{"steps": [...]}` form instead of markdown, and by
  anything that edits steps directly). It must round-trip through
  `from_markdown` without losing `verified`: a step the model itself marked
  `[x]` is `status: done` but `verified: false` until real evidence lands, and
  that distinction is carried as an HTML-comment suffix
  (`<!-- unverified -->`) so an old renderer that only understands checkboxes
  still sees a checked, readable line.

Step ids are a hash of the normalized title plus its position, not a random
UUID: the same checklist parsed twice (e.g. once live, once from history)
produces the same ids, which is what lets `evidence_refs` or `depends_on`
collected against one parse still mean something against the next.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

STATUSES = ("pending", "done", "blocked")

#: Marks a `[x]` step whose `verified` flag is False, so `to_markdown` never
#: silently drops it — an old renderer just sees a checked box with a comment.
UNVERIFIED_MARKER = "<!-- unverified -->"
_BLOCKED_PREFIX = "[blocked]"

_CHECKBOX_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:[-*]|\d+[.)])\s+\[(?P<mark>[ xX])\]\s*(?P<title>.*?)\s*$"
)
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def stable_step_id(title: str, order: int) -> str:
    """A step id stable across re-parses of the same checklist: same text at
    the same position always hashes to the same id, so external references
    (evidence, dependencies, a frontend's collapsed/expanded state) survive a
    round-trip through markdown instead of resetting every turn."""
    digest = hashlib.sha1(f"{order}:{_normalize(title)}".encode("utf-8")).hexdigest()
    return f"step_{digest[:12]}"


@dataclass
class PlanStep:
    id: str
    title: str
    status: str = "pending"
    depends_on: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    notes: str = ""
    # True only for a DONE step whose completion something other than the
    # model confirmed (evidence, a verifier, a person). A pending or blocked
    # step is never "verified": the first live plan (12 pending steps, all
    # `verified: true`, 10-09-2026) read as if the work were already checked.
    verified: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "evidence_refs": list(self.evidence_refs),
            "notes": self.notes,
            "verified": bool(self.verified),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], *, order: int) -> "PlanStep":
        title = str(data.get("title", "")).strip()
        status = str(data.get("status", "pending")).strip().lower()
        if status not in STATUSES:
            status = "pending"
        step_id = str(data.get("id") or "").strip() or stable_step_id(title, order)
        # Only a done step can carry the flag; structured input that says
        # `verified: true` on a pending step is corrected, not trusted.
        verified = bool(data.get("verified")) and status == "done"
        depends_on = [str(x).strip() for x in (data.get("depends_on") or []) if str(x).strip()]
        evidence_refs = [str(x).strip() for x in (data.get("evidence_refs") or []) if str(x).strip()]
        notes = str(data.get("notes", "")).strip()
        return cls(id=step_id, title=title, status=status, depends_on=depends_on,
                    evidence_refs=evidence_refs, notes=notes, verified=verified)


@dataclass
class Plan:
    steps: List[PlanStep] = field(default_factory=list)
    revision: int = 1
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "revision": int(self.revision),
            "warnings": list(self.warnings),
        }

    def counts(self) -> Tuple[int, int]:
        done = sum(1 for s in self.steps if s.status == "done")
        return done, len(self.steps)


def from_markdown(text: str, *, revision: int = 1) -> Plan:
    """Parse a checklist into structured steps. Never raises and never drops
    a checklist line: anything that looks like a bullet/numbered item but
    doesn't match the `[ ]`/`[x]` shape is recorded as a warning instead of
    silently vanishing (the whole point of TASK-01)."""
    steps: List[PlanStep] = []
    warnings: List[str] = []
    stack: List[Tuple[int, str]] = []  # (indent width, step id) — open ancestors
    order = 0
    for lineno, raw_line in enumerate((text or "").splitlines(), start=1):
        if not raw_line.strip():
            continue
        match = _CHECKBOX_RE.match(raw_line)
        if not match:
            if _BULLET_RE.match(raw_line):
                warnings.append(f"line {lineno}: could not parse as a checklist item")
            continue
        indent = len(match.group("indent").expandtabs(2))
        mark = match.group("mark").lower()
        title = match.group("title")

        unverified = False
        if title.endswith(UNVERIFIED_MARKER):
            unverified = True
            title = title[: -len(UNVERIFIED_MARKER)].rstrip()

        blocked = title.lower().startswith(_BLOCKED_PREFIX)
        if blocked:
            title = title[len(_BLOCKED_PREFIX):].strip()

        if not title:
            warnings.append(f"line {lineno}: checklist item has no text")
            continue

        status = "blocked" if blocked else ("done" if mark == "x" else "pending")
        step_id = stable_step_id(title, order)

        while stack and stack[-1][0] >= indent:
            stack.pop()
        depends_on = [stack[-1][1]] if stack else []
        # A `[x]` parsed straight off markdown is the MODEL's own claim, not
        # evidence: it is always `verified: false` here, marker or not — the
        # marker only round-trips the flag through `to_markdown`'s own
        # output. `verified: true` is only reachable via the structured
        # `{"steps": [...]}` input (parse_steps_input), never by checkbox
        # text alone; that keeps "done, unverified" the honest default this
        # tool started with, instead of the marker's mere absence quietly
        # meaning "trust it".
        del unverified  # detected only to strip the marker from the title
        verified = False  # nothing parsed off a checklist is confirmed by that checklist

        steps.append(PlanStep(id=step_id, title=title, status=status,
                               depends_on=depends_on, verified=verified))
        stack.append((indent, step_id))
        order += 1

    return Plan(steps=steps, revision=max(1, int(revision)), warnings=warnings)


def to_markdown(plan: Plan) -> str:
    """Reconstruct checklist markdown from structured steps. Nesting is
    derived from single-parent `depends_on` chains (the only shape markdown
    indentation can represent); a step with more than one dependency, or one
    whose parent isn't in this plan, is rendered at the top level rather than
    guessed at."""
    by_id = {s.id: s for s in plan.steps}

    def depth(step: PlanStep, seen: frozenset) -> int:
        if len(step.depends_on) != 1:
            return 0
        parent = by_id.get(step.depends_on[0])
        if parent is None or parent.id in seen:
            return 0
        return 1 + depth(parent, seen | {step.id})

    lines: List[str] = []
    for step in plan.steps:
        mark = "x" if step.status == "done" else " "
        indent = "  " * depth(step, frozenset())
        title = f"{_BLOCKED_PREFIX} {step.title}" if step.status == "blocked" else step.title
        suffix = f" {UNVERIFIED_MARKER}" if (step.status == "done" and not step.verified) else ""
        lines.append(f"{indent}- [{mark}] {title}{suffix}")
    return "\n".join(lines)


def parse_steps_input(data: Any, *, default_revision: int = 1) -> Optional[Plan]:
    """The newer `{"steps": [...]}` form some models send instead of markdown.
    Returns None (never raises) when `data` isn't shaped like this at all, so
    a caller can fall back to markdown parsing without a try/except."""
    if not isinstance(data, dict) or "steps" not in data or not isinstance(data["steps"], list):
        return None
    steps: List[PlanStep] = []
    for i, raw in enumerate(data["steps"]):
        if isinstance(raw, dict):
            step = PlanStep.from_dict(raw, order=i)
            if step.title:
                steps.append(step)
        elif isinstance(raw, str) and raw.strip():
            title = raw.strip()
            steps.append(PlanStep(id=stable_step_id(title, i), title=title))
    try:
        revision = int(data.get("revision", default_revision))
    except (TypeError, ValueError):
        revision = default_revision
    return Plan(steps=steps, revision=max(1, revision))
