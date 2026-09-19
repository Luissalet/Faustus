"""
skills_runtime/disclosure.py — explicit progressive disclosure for skills.

The prompt has always loaded skills in three stages — `src.agent_loop`
already calls its always-injected index block "Level-0 skill index", and
`manage_skills` already separates `view` (the full SKILL.md) from `view_ref`
(one referenced file at a time). This module gives those three stages
names, a token budget each, and a place that logs which level a skill
contributed at, instead of leaving the shape implicit in `agent_loop.py`'s
prompt-assembly code:

  Level 0  `render_level0`   name + one-line description + when-to-use
                              trigger. Always injected; what the agent uses
                              to DECIDE which skills are worth pulling in.
                              Budget: `skill_list_budget_tokens` (400).

  Level 1  `render_level1`   the procedure body (description, when_to_use,
                              numbered steps, pitfalls) — pulled ONLY for
                              the skills a turn actually selected as
                              relevant, never for the whole index.
                              Budget: `skill_body_budget_tokens` (1500).

  Level 2  (unchanged)       the files a procedure references, read one at
                              a time via `manage_skills view_ref` — already
                              the cheapest possible unit, so it carries no
                              budget here.

Both budgets are enforced by DROPPING whole entries in the order given
(never truncating an entry's description/steps mid-sentence) — the same
"drop, do not mutilate" posture `memory_block_max_chars` (Job B) uses for
the memory block. `contributed_at` records, per skill name, which level(s)
a render call actually used, for the caller to log.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence

logger = logging.getLogger(__name__)

DEFAULT_LEVEL0_BUDGET_TOKENS = 400
DEFAULT_LEVEL1_BUDGET_TOKENS = 1500

#: Rough, deterministic token estimate — no tokenizer dependency here, the
#: same "about 4 chars/token" heuristic `context_engine.budgets` documents
#: as its own fallback estimator. Good enough for a drop decision; nobody
#: reads this number as an exact count.
_CHARS_PER_TOKEN = 4


def _tokens(text: str) -> int:
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


@dataclass
class DisclosureResult:
    """What a `render_level*` call produced, plus the accounting the caller
    logs: which skills made it in, which were dropped for budget, and how
    many tokens the rendered text is estimated to cost."""

    text: str
    included: List[str] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)
    tokens: int = 0
    level: int = 0

    def log_line(self) -> str:
        parts = [f"level {self.level}: {len(self.included)} skill(s) ~{self.tokens} tok"]
        if self.dropped:
            parts.append(f"{len(self.dropped)} dropped for budget ({', '.join(self.dropped)})")
        return "; ".join(parts)


def _level0_line(entry: Mapping[str, Any]) -> str:
    name = str(entry.get("name") or "?")
    desc = str(entry.get("description") or "").strip()
    trigger = str(entry.get("when_to_use") or "").strip()
    badge = " *(draft)*" if entry.get("status") == "draft" else ""
    line = f"- `{name}` — {desc}{badge}"
    if trigger:
        line += f" _(when: {trigger})_"
    return line


def render_level0(
    index: Sequence[Mapping[str, Any]],
    *,
    budget_tokens: int = DEFAULT_LEVEL0_BUDGET_TOKENS,
) -> DisclosureResult:
    """Level 0: the always-injected candidate list — name, one-line
    description and when-to-use trigger, grouped by category exactly as
    `SkillsManager.index_for()` orders them (nearer-priority ordering is
    that manager's job, not this one's). Entries beyond the budget are
    dropped whole, in the given order, never truncated mid-line."""
    budget_tokens = max(1, int(budget_tokens or DEFAULT_LEVEL0_BUDGET_TOKENS))
    by_cat: Dict[str, List[Mapping[str, Any]]] = {}
    for entry in index or ():
        by_cat.setdefault(str(entry.get("category") or "general"), []).append(entry)

    lines: List[str] = []
    included: List[str] = []
    dropped: List[str] = []
    used = 0
    for cat in sorted(by_cat):
        header = f"\n**{cat}**"
        header_cost = _tokens(header)
        cat_lines: List[str] = []
        cat_names: List[str] = []
        for entry in by_cat[cat]:
            line = _level0_line(entry)
            cost = _tokens(line)
            if used + header_cost + sum(_tokens(l) for l in cat_lines) + cost > budget_tokens:
                dropped.append(str(entry.get("name") or "?"))
                continue
            cat_lines.append(line)
            cat_names.append(str(entry.get("name") or "?"))
        if not cat_lines:
            continue
        lines.append(header)
        lines.extend(cat_lines)
        included.extend(cat_names)
        used += header_cost + sum(_tokens(l) for l in cat_lines)

    text = "\n".join(lines)
    result = DisclosureResult(text=text, included=included, dropped=dropped,
                              tokens=_tokens(text) if text else 0, level=0)
    if dropped:
        logger.info("skills disclosure: %s", result.log_line())
    return result


def _level1_section(skill: Mapping[str, Any]) -> str:
    lines: List[str] = []
    src_tag = ""
    if skill.get("source") == "teacher-escalation":
        tm = skill.get("teacher_model") or "teacher"
        src_tag = f" _(learned from {tm})_"
    lines.append(f"### {skill.get('name', '?')}{src_tag}")
    if skill.get("description"):
        lines.append(str(skill["description"]))
    if skill.get("when_to_use"):
        lines.append(f"_When to use:_ {skill['when_to_use']}")
    proc = skill.get("procedure") or []
    if proc:
        lines.append("Procedure:")
        for i, step in enumerate(proc, 1):
            lines.append(f"  {i}. {step}")
    pitfalls = skill.get("pitfalls") or []
    if pitfalls:
        lines.append("Pitfalls: " + "; ".join(str(p) for p in pitfalls))
    return "\n".join(lines)


def render_level1(
    skills: Sequence[Mapping[str, Any]],
    *,
    budget_tokens: int = DEFAULT_LEVEL1_BUDGET_TOKENS,
) -> DisclosureResult:
    """Level 1: the procedure body, for the skills a turn actually selected
    (`SkillsManager.get_relevant_skills`) — never for the whole index. Whole
    skills are dropped, in the given (relevance) order, when the running
    total would exceed the budget; a skill that is kept is rendered in
    full, never mid-sentence."""
    budget_tokens = max(1, int(budget_tokens or DEFAULT_LEVEL1_BUDGET_TOKENS))
    sections: List[str] = []
    included: List[str] = []
    dropped: List[str] = []
    used = 0
    for skill in skills or ():
        section = _level1_section(skill)
        cost = _tokens(section)
        if used + cost > budget_tokens and included:
            # At least one skill already fits; further ones are dropped
            # whole rather than shrinking what is already committed.
            dropped.append(str(skill.get("name") or "?"))
            continue
        sections.append(section)
        included.append(str(skill.get("name") or "?"))
        used += cost
    text = "\n\n".join(sections)
    result = DisclosureResult(text=text, included=included, dropped=dropped,
                              tokens=_tokens(text) if text else 0, level=1)
    if dropped:
        logger.info("skills disclosure: %s", result.log_line())
    return result


def log_level2_use(skill_name: str, ref_path: str) -> None:
    """Level 2: a referenced file, read one at a time via `manage_skills
    view_ref` — call this where that action succeeds so the same "which
    level contributed" log line covers all three levels."""
    logger.info("skills disclosure: level 2: %s <- %s", skill_name, ref_path)
