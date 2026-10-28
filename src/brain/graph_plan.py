"""Question planner for ``graph_ask``: a plan of operators bound by aliases.

A question over the graph is split into atomic steps, each with exactly one
operator::

    Retrieval(s=s1:person[`Ana`], p=p1:works_at, o=o1:organization)
    Retrieval(s=o1, p=p2:located_in, o=o2:place)      # o1 of step 1 is s of step 2
    Math(content=[`o2`], target=`...`)->math1
    Deduce(op=judgement|entailment|extract|choice, content=[...], target=`...`)->deduce1
    Output(o2)

The operator set, the alias binding between steps and the "Step / Action"
text format come from the static planner of the KAG project (OpenSPG,
Apache-2.0, ``kag/solver/prompt/lf_static_planning_prompt.py``); the prompt
text, the schema grounding (entity types and relation labels that really
exist in this graph), the parser and the validator below are Faustus' own.
See THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from src.brain.db import fold

OPERATORS = ("retrieval", "math", "deduce", "output")
DEDUCE_OPS = ("judgement", "entailment", "extract", "choice", "multichoice")

MAX_STEPS = 8


class PlanError(ValueError):
    """The plan cannot be run; the message says why, for a repair attempt."""


@dataclass
class Term:
    """One slot of a Retrieval: an alias, an optional type, an optional literal
    name, or a reference to an alias another step defined."""
    alias: str = ""
    type: str = ""
    name: str = ""
    ref: str = ""  # the alias of an earlier step this slot takes its value from

    def label(self) -> str:
        if self.ref:
            return self.ref
        out = self.alias
        if self.type:
            out += f":{self.type}"
        if self.name:
            out += f"[{self.name}]"
        return out


@dataclass
class Step:
    n: int
    question: str
    op: str
    s: Optional[Term] = None
    p_alias: str = ""
    p: str = ""
    o: Optional[Term] = None
    content: List[str] = field(default_factory=list)
    target: str = ""
    deduce_op: str = ""
    alias: str = ""
    outputs: List[str] = field(default_factory=list)
    # derived by `bind`
    defines: Set[str] = field(default_factory=set)
    needs: Set[str] = field(default_factory=set)
    level: int = 0

    def public(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"n": self.n, "op": self.op, "question": self.question, "level": self.level}
        if self.op == "retrieval":
            out.update(s=self.s.label() if self.s else "", p=(self.p_alias + (":" + self.p if self.p else "")).strip(":"),
                       o=self.o.label() if self.o else "")
        elif self.op in ("math", "deduce"):
            out.update(content=list(self.content), target=self.target, alias=self.alias)
            if self.op == "deduce":
                out["deduce_op"] = self.deduce_op
        else:
            out["outputs"] = list(self.outputs)
        out["needs"] = sorted(self.needs)
        out["defines"] = sorted(self.defines)
        return out


# -- parsing -------------------------------------------------------------------------------

_STEP = re.compile(r"^\s*\**\s*Step\s*(\d+)\s*[:：.)-]\s*(.*?)\s*$", re.IGNORECASE)
_ACTION = re.compile(r"^\s*\**\s*Action\s*(\d+)\s*[:：.)-]\s*(.*?)\s*$", re.IGNORECASE)
_CALL = re.compile(r"^\s*(retrieval|math|deduce|output)\s*\((.*)\)\s*(?:->\s*([A-Za-z_][\w]*))?\s*$",
                   re.IGNORECASE | re.DOTALL)
_TICKS = re.compile(r"`([^`]*)`")


def _strip_fences(text: str) -> str:
    return text.replace("```markdown", "").replace("```", "")


def _split_args(body: str) -> List[str]:
    """Split on top-level commas (not inside [] or backticks)."""
    out, depth, tick, cur = [], 0, False, []
    for ch in body:
        if ch == "`":
            tick = not tick
        elif not tick and ch in "[(":
            depth += 1
        elif not tick and ch in "])":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0 and not tick:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur))
    return [part.strip() for part in out if part.strip()]


_TERM = re.compile(r"^([A-Za-z_]\w*)\s*(?::\s*([^\[\]`]*?))?\s*(?:\[\s*`?([^`\]]*)`?\s*\])?$")


def _term(raw: str, defined: Set[str]) -> Term:
    raw = raw.strip().strip(",")
    match = _TERM.match(raw)
    if not match:
        raise PlanError(f"cannot read the slot `{raw}`")
    alias, etype, name = match.group(1), (match.group(2) or "").strip(), (match.group(3) or "").strip()
    if alias in defined and not etype and not name:
        return Term(ref=alias)
    if name in defined:  # `s=s2:type[`o1`]`: the bracket names an earlier alias, not an entity
        return Term(alias=alias, type=etype, ref=name)
    return Term(alias=alias, type=etype, name=name)


def parse(text: str) -> List[Step]:
    """Read a ``Step N: ... / Action N: Op(...)`` plan. Raises :class:`PlanError`."""
    text = _strip_fences(str(text or ""))
    questions: Dict[int, str] = {}
    actions: Dict[int, str] = {}
    order: List[int] = []
    for line in text.splitlines():
        action = _ACTION.match(line)
        if action:
            n = int(action.group(1))
            actions[n] = action.group(2)
            if n not in order:
                order.append(n)
            continue
        step = _STEP.match(line)
        if step:
            n = int(step.group(1))
            questions[n] = step.group(2).strip().strip("`")
            if n not in order:
                order.append(n)
    if not actions:
        raise PlanError("no `Action` lines found")
    steps: List[Step] = []
    defined: Set[str] = set()
    for n in sorted(order):
        if n not in actions:
            raise PlanError(f"Step{n} has no Action{n}")
        call = _CALL.match(actions[n].strip().strip("`"))
        if not call:
            raise PlanError(f"Action{n} is not one of Retrieval, Math, Deduce or Output")
        op = call.group(1).lower()
        body = call.group(2)
        result_alias = call.group(3) or ""
        step = Step(n=n, question=questions.get(n, ""), op=op)
        args = _split_args(body)
        if op == "retrieval":
            kv = {}
            for arg in args:
                if "=" not in arg:
                    continue
                key, value = arg.split("=", 1)
                kv[key.strip().lower()] = value.strip()
            if "s" not in kv or "o" not in kv:
                raise PlanError(f"Action{n}: Retrieval needs s= and o=")
            step.s = _term(kv["s"], defined)
            step.o = _term(kv["o"], defined)
            if "p" in kv:
                pm = re.match(r"^([A-Za-z_]\w*)\s*(?::\s*(.+))?$", kv["p"].strip().strip("`"))
                if not pm:
                    raise PlanError(f"Action{n}: cannot read p=`{kv['p']}`")
                step.p_alias, step.p = pm.group(1), (pm.group(2) or "").strip().strip("`")
            if step.o.ref:
                raise PlanError(f"Action{n}: o= must be a new alias, not the earlier `{step.o.ref}`")
            step.defines = {a for a in (step.s.alias, step.o.alias, step.p_alias) if a}
            step.needs = {step.s.ref} if step.s.ref else set()
        elif op in ("math", "deduce"):
            content: List[str] = []
            for arg in args:
                low = arg.lower()
                if low.startswith("content"):
                    inside = arg.split("=", 1)[1] if "=" in arg else arg[len("content"):]
                    inside = inside.strip()
                    ticks = _TICKS.findall(inside)
                    if ticks:
                        content = [t.strip() for t in ticks if t.strip()]
                    else:
                        inside = inside.strip("[]")
                        content = [c.strip().strip("'\"") for c in inside.split(",") if c.strip()]
                elif low.startswith("target"):
                    value = arg.split("=", 1)[1].strip() if "=" in arg else ""
                    step.target = value.strip("`'\" ")
                elif low.startswith("op"):
                    step.deduce_op = arg.split("=", 1)[1].strip().strip("`'\" ").lower() if "=" in arg else ""
            step.content = content
            step.alias = result_alias
            if op == "deduce" and step.deduce_op and step.deduce_op not in DEDUCE_OPS:
                step.deduce_op = "extract"
            if op == "deduce" and not step.deduce_op:
                step.deduce_op = "extract"
            if not step.alias:
                step.alias = f"{op}{n}"
            step.defines = {step.alias}
            step.needs = {c for c in content if c in defined}
        else:  # output
            outs = [a.strip().strip("`") for a in args]
            step.outputs = [o for o in outs if o]
            step.needs = {o for o in step.outputs if o in defined}
        defined |= step.defines
        steps.append(step)
    return steps


# -- validation and levels ---------------------------------------------------------------------

def bind(steps: Sequence[Step], *, max_steps: int = MAX_STEPS) -> List[Step]:
    """Check aliases and give every step its level (0 = no dependency). Adds a
    closing ``Output`` when the plan has none. Raises :class:`PlanError`."""
    steps = list(steps)
    if not steps:
        raise PlanError("the plan is empty")
    if len(steps) > max_steps:
        raise PlanError(f"the plan has {len(steps)} steps; at most {max_steps}")
    owner_of: Dict[str, int] = {}
    for step in steps:
        for alias in step.defines:
            if alias in owner_of:
                raise PlanError(f"alias `{alias}` is defined twice (steps {owner_of[alias]} and {step.n})")
            owner_of[alias] = step.n
    by_n = {s.n: s for s in steps}
    for step in steps:
        wanted = set(step.needs)
        if step.op in ("math", "deduce"):
            wanted |= {c for c in step.content if re.fullmatch(r"[A-Za-z_]\w*", c) and c in owner_of}
        if step.op == "output":
            for out in step.outputs:
                if out not in owner_of:
                    raise PlanError(f"Output names `{out}` which no step defines")
            wanted |= set(step.outputs)
        if step.op == "retrieval" and step.s and step.s.ref and step.s.ref not in owner_of:
            raise PlanError(f"Step{step.n} uses `{step.s.ref}` which no step defines")
        step.needs = wanted
        for alias in wanted:
            if alias not in owner_of:
                raise PlanError(f"Step{step.n} uses `{alias}` which no step defines")
            if owner_of[alias] >= step.n:
                raise PlanError(f"Step{step.n} uses `{alias}` before Step{owner_of[alias]} produces it")
    for step in steps:  # levels, in step order (dependencies always point back)
        step.level = 0
        for alias in step.needs:
            step.level = max(step.level, by_n[owner_of[alias]].level + 1)
    if not any(s.op == "output" for s in steps):
        last = steps[-1]
        alias = (next(iter(sorted(last.defines - {last.p_alias})), "") if last.op != "retrieval"
                 else (last.o.alias if last.o else ""))
        if not alias:
            raise PlanError("the plan has no Output and the last step has no result to output")
        out = Step(n=max(s.n for s in steps) + 1, question="Give the final answer.", op="output",
                   outputs=[alias], needs={alias})
        out.level = by_n[owner_of[alias]].level + 1
        steps.append(out)
    return steps


def levels(steps: Sequence[Step]) -> List[List[Step]]:
    """The steps grouped by level, in order; steps of one level are independent."""
    grouped: Dict[int, List[Step]] = {}
    for step in steps:
        grouped.setdefault(step.level, []).append(step)
    return [grouped[k] for k in sorted(grouped)]


# -- the prompt ---------------------------------------------------------------------------------

_OPERATORS = """Operators (each Step has exactly one Action):
- Retrieval(s=s_alias:type[`name`], p=p_alias:relation, o=o_alias:type)
  Looks facts up in the knowledge graph and its source texts. Use `[name]` only for a name that appears in the question. To start from the result of an earlier Retrieval write `s=o1` (the alias of that earlier `o`). One relation per step: a question with two hops needs two Retrievals.
- Math(content=[`text` or alias], target=`what to compute or compare`)->math_alias
  Arithmetic, counting, sorting or comparing values already retrieved.
- Deduce(op=judgement|entailment|extract|choice, content=[`text` or alias], target=`question`)->deduce_alias
  Reasoning over retrieved results or facts given in the question.
- Output(alias, ...)
  The last Step: returns the results held by those aliases as the answer."""

_CASES = """Example 1
Question: In which city does the person who created Project Alpha live?
Plan:
Step1: Who created Project Alpha?
Action1: Retrieval(s=s1:project[`Project Alpha`], p=p1:created_by, o=o1:person)
Step2: In which city does the person from Step1 live?
Action2: Retrieval(s=o1, p=p2:lives_in, o=o2:place)
Step3: Return the city.
Action3: Output(o2)

Example 2
Question: Which tool used by Project Alpha is also used by Project Beta?
Plan:
Step1: Which tools does Project Alpha use?
Action1: Retrieval(s=s1:project[`Project Alpha`], p=p1:uses, o=o1:tool)
Step2: Which tools does Project Beta use?
Action2: Retrieval(s=s2:project[`Project Beta`], p=p2:uses, o=o2:tool)
Step3: Which tool appears in both results of Step1 and Step2?
Action3: Deduce(op=extract, content=[`o1`, `o2`], target=`Which tool appears in both lists?`)->deduce3
Step4: Return the tool.
Action4: Output(deduce3)"""


def build_prompt(question: str, schema: Dict[str, Any], *, error: str = "", previous: str = "") -> str:
    rels = ", ".join(f"{k} ({v})" for k, v in sorted((schema.get("relations") or {}).items(), key=lambda kv: -kv[1])[:25]) or "none yet"
    types = ", ".join(sorted((schema.get("types") or {}))) or "person, project, organization, place, tool, concept, event, other"
    names = "; ".join(f"{n['name']} ({n['type']})" for n in (schema.get("names") or [])[:40]) or "none"
    parts = [
        "You plan how to answer a question from a personal knowledge graph, as a numbered list of atomic Steps, each with one Action.",
        _OPERATORS,
        f"Entity types that exist in this graph: {types}.",
        f"Relation labels that exist in this graph (with how many edges each has): {rels}. Prefer these labels for `p=`; each fact is stored as (entity, relation, entity or value).",
        f"Some entities that exist: {names}.",
        "Rules:\n- Each Step is an indivisible question and holds exactly one Action.\n- Reuse the alias of an earlier `o` as the `s` of the next hop (`s=o1`); never repeat a name that an earlier step already finds.\n- The question may be in any language; write Steps in the language of the question and keep names as written.\n- Use as few Steps as the question needs (at most %d). The last Step is always Output.\n- Output only the Steps and Actions, one per line, in the format `StepN: ...` and `ActionN: ...`." % MAX_STEPS,
        _CASES,
    ]
    if error:
        parts.append(f"Your previous plan could not be used: {error}\nPrevious plan:\n{previous[:1500]}\nWrite a corrected plan.")
    parts.append(f"Question: {question}\nPlan:")
    return "\n\n".join(parts)


async def plan(question: str, schema: Dict[str, Any], llm: Callable[[str], Any], *, max_steps: int = MAX_STEPS,
               attempts: int = 2) -> Tuple[List[Step], Dict[str, Any]]:
    """Ask the model for a plan, parse and validate it; on a bad plan, ask once
    more with the reason. Returns ``(steps, info)``; raises :class:`PlanError`
    when no attempt produced a runnable plan."""
    error, previous, raw = "", "", ""
    info: Dict[str, Any] = {"attempts": 0, "errors": []}
    for _ in range(max(1, attempts)):
        info["attempts"] += 1
        raw = str(await llm(build_prompt(question, schema, error=error, previous=previous)) or "")
        try:
            steps = bind(parse(raw), max_steps=max_steps)
            info["raw"] = raw
            return steps, info
        except PlanError as exc:
            error, previous = str(exc), raw
            info["errors"].append(error)
    info["raw"] = raw
    raise PlanError("; ".join(info["errors"]) or "no plan")
