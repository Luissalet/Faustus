"""``graph_ask``: answer a multi-hop question from the Cerebro.

Pipeline (all steps are recorded in the result, none is hidden):

1. **Plan** (:mod:`src.brain.graph_plan`): the model splits the question into
   atomic steps with one operator each - ``Retrieval(s,p,o)``, ``Math``,
   ``Deduce``, ``Output`` - bound by aliases (``o1`` of one step is ``s`` of the
   next). The plan is parsed and validated; a bad plan gets one repair attempt.
2. **Run by level**: steps with no dependency between them form a level and run
   together (``asyncio.gather``); a level starts when the previous one is done.
3. **Rewrite**: before a step that depends on others runs, its sub-question is
   rewritten with the results those steps obtained (the model, with a
   deterministic fallback), so retrieval sees "Where does Ana live?" and not
   "Where does the person from Step1 live?".
4. **Retrieve over both sides** (:mod:`src.brain.graph_index`): graph edges
   around the bound entities give triples and the fragments that evidence them;
   BM25 over the entity-linked fragments gives text hits; the two score tables
   are min-max normalised and fused with a weight (``alpha``).
5. **Answer**: ``Output`` gathers the values of its aliases and one model call
   writes the answer from the retrieved facts only, with the sources.

The planner/executor/merger split follows the static solver of the KAG project
(OpenSPG, Apache-2.0: ``kag/solver/pipeline/kag_static_pipeline.py`` and
``kag_component/kag_merger.py``); the implementation here is Faustus' own and
reads the existing Cerebro tables. See THIRD_PARTY_NOTICES.md.

Nothing here writes to the Cerebro.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import operator
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

from src.brain import entities, graph_plan
from src.brain.db import fold
from src.brain.graph_index import MutualIndex, tokens, weighted_merge
from src.brain.graph_plan import PlanError, Step

logger = logging.getLogger(__name__)

LlmFn = Callable[..., Awaitable[str]]


class GraphAskRequest(BaseModel):
    """The arguments of the `graph_ask` tool and of `POST /api/brain/ask`."""
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=1200)
    plan_only: bool = False
    max_steps: int = Field(default=6, ge=1, le=8)
    top_k: int = Field(default=6, ge=1, le=12)
    alpha: float = Field(default=0.5, ge=0.0, le=1.0)

DEFAULT_TOP_K = 6
DEFAULT_ALPHA = 0.5
SNIPPET_CHARS = 420
MAX_VALUES = 12
#: A bullet or "1." / "2)" at the start of a line; a bare number such as 2012 is an answer, not a marker.
_LIST_MARK = re.compile(r"^\s*(?:[-*\u2022]\s+|\d{1,2}[.)]\s+)")


# -- the model ------------------------------------------------------------------------------------

def default_llm(owner: Any, *, workload: str = "foreground") -> Optional[LlmFn]:
    """The model the Cerebro questions run on: the ``brain_ask`` endpoint if one is
    configured, else the utility endpoint, else the global default one."""
    try:
        from src.endpoint_resolver import resolve_endpoint
        url, model, headers = resolve_endpoint("brain_ask", owner=str(owner or ""))
    except Exception:  # noqa: BLE001
        return None
    if not url or not model:
        return None

    async def call(prompt: str, *, max_tokens: int = 700) -> str:
        from src.llm_core import llm_call_async
        raw = await llm_call_async(url=url, model=model, messages=[{"role": "user", "content": prompt}],
                                   headers=headers, temperature=0.0, max_tokens=max_tokens, timeout=150,
                                   max_retries=1, workload=workload)
        return raw[0] if isinstance(raw, tuple) else str(raw or "")
    call.model = model  # type: ignore[attr-defined]
    return call


class _Counted:
    """Counts the model calls of one question."""

    def __init__(self, fn: LlmFn):
        self.fn = fn
        self.calls = 0
        self.seconds = 0.0

    async def __call__(self, prompt: str, **kw: Any) -> str:
        self.calls += 1
        started = time.monotonic()
        try:
            return str(await self.fn(prompt, **kw) or "")
        finally:
            self.seconds += time.monotonic() - started


# -- values ---------------------------------------------------------------------------------------

def _value(name: str, *, entity_id: str = "", kind: str = "entity", etype: str = "") -> Dict[str, Any]:
    return {"name": name, "id": entity_id, "kind": kind, "type": etype}


def _names(values: Sequence[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for v in values:
        if v["name"] and v["name"] not in out:
            out.append(v["name"])
    return out


class _Run:
    def __init__(self, index: MutualIndex, llm: _Counted, *, top_k: int, alpha: float):
        self.index, self.llm, self.top_k, self.alpha = index, llm, top_k, alpha
        self.values: Dict[str, List[Dict[str, Any]]] = {}
        self.records: Dict[int, Dict[str, Any]] = {}
        self.sources: List[str] = []

    def bound(self, alias: str) -> List[Dict[str, Any]]:
        return self.values.get(alias, [])

    def note_sources(self, refs: Sequence[str]) -> None:
        for ref in refs:
            if ref and ref not in self.sources:
                self.sources.append(ref)


# -- rewriting ------------------------------------------------------------------------------------

def _needs_text(run: _Run, step: Step) -> List[Tuple[str, List[str]]]:
    return [(alias, _names(run.bound(alias))) for alias in sorted(step.needs)]


async def _rewrite(run: _Run, step: Step) -> str:
    """Put the results obtained by earlier steps into this step's sub-question."""
    question = step.question or ""
    held = [(a, v) for a, v in _needs_text(run, step) if v]
    if not question or not held:
        return question
    facts = "\n".join(f"- {alias} = {', '.join(vals[:MAX_VALUES])}" for alias, vals in held)
    prompt = ("Rewrite the sub-question so that it no longer refers to earlier steps: replace each reference "
              "(\"the person from Step1\", \"the result above\", an alias such as o1) with the value obtained for it. "
              "Keep the language of the sub-question. Answer with the rewritten question only.\n\n"
              f"Sub-question: {question}\nValues obtained:\n{facts}")
    try:
        text = (await run.llm(prompt, max_tokens=120)).strip().strip("`\"' ")
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph_ask: rewrite failed (%s)", exc)
        text = ""
    if text and len(text) <= 400 and "\n" not in text:
        return text
    shown = "; ".join(a + ": " + ", ".join(v[:4]) for a, v in held)
    return f"{question} ({shown})"


# -- Retrieval ------------------------------------------------------------------------------------

def _rel_tokens(label: Any) -> List[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(label or ""))
    return [t for t in re.split(r"[\s_\-:]+", fold(spaced)) if t and t not in ("by", "of", "in", "at", "to", "de", "en")]


_INVERSE_HINT = re.compile(r"(_by|_of|by$|owned|used_by|created_by|made_by|^has_|^is_|^tiene|^es_)", re.IGNORECASE)


#: Predicates a planner words one way and the extractor another ("member of" asked, "part of" stored).
#: Four-letter stems, so every inflection of a word lands in the same group.
_SYNONYM_STEMS = (
    frozenset({"memb", "part", "belo", "join", "form", "affi"}),
    frozenset({"live", "resi", "loca", "base", "home", "dwel"}),
    frozenset({"crea", "foun", "auth", "buil", "deve", "make", "made", "star"}),
    frozenset({"stud", "atte", "grad", "lear", "trai"}),
    frozenset({"uses", "use", "util"}),
)


def _same_meaning(w: set, r: set) -> bool:
    ws, rs = {x[:4] for x in w}, {x[:4] for x in r}
    return any(ws & g and rs & g for g in _SYNONYM_STEMS)


def _predicate_score(wanted: str, rel: str) -> float:
    if not wanted:
        return 0.55
    canon = entities.canonical_relation(wanted)
    if canon and canon == rel:
        return 1.0
    if fold(wanted).replace(" ", "_") == fold(rel).replace(" ", "_"):
        return 1.0
    w, r = set(_rel_tokens(wanted)), set(_rel_tokens(rel))
    if not w or not r:
        return 0.2
    # shared stems ("created" / "creator" / "create")
    stems = {x[:5] for x in w} & {x[:5] for x in r}
    if w & r:
        return 0.8
    if stems:
        return 0.7
    if _same_meaning(w, r):
        return 0.65
    return 0.15


def _seeds(run: _Run, step: Step) -> Tuple[List[str], str]:
    """Entity ids this Retrieval starts from, and a reason when there are none."""
    s = step.s
    ids: List[str] = []
    if s is not None and s.ref:
        for v in run.bound(s.ref):
            if v.get("id") and v["id"] in run.index.entities:
                ids.append(v["id"])
            elif v["name"]:
                ids.extend(run.index.find(v["name"], limit=2))
    elif s is not None and s.name:
        ids = run.index.find(s.name, type=s.type, limit=3)
        if not ids:
            return [], f"no entity called `{s.name}`"
    out: List[str] = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out, ("" if out else "no entity to start from")


def _retrieve(run: _Run, step: Step, question: str) -> Dict[str, Any]:
    idx = run.index
    s, o = step.s, step.o
    seeds, why = _seeds(run, step)
    inverse_seeds = False
    if not seeds and o is not None and o.name:  # only the object is named: walk the edges backwards
        seeds = idx.find(o.name, type=o.type, limit=3)
        inverse_seeds = bool(seeds)
        why = "" if seeds else f"no entity called `{o.name}`"
    record: Dict[str, Any] = {"seeds": [idx.name_of(e) for e in seeds], "seed_ids": list(seeds), "triples": [],
                              "fragments": [], "error": why}
    wanted_p = step.p
    candidates: List[Tuple[float, Dict[str, Any], str, str]] = []  # score, relation, seed, other-end id/'' for literal
    for seed in seeds:
        edges = [(e, False) for e in idx.out_edges.get(seed, [])] + [(e, True) for e in idx.in_edges.get(seed, [])]
        for edge, incoming in edges:
            sim = _predicate_score(wanted_p, edge["rel"])
            # the edge points the way the question asks when the seed is its source
            # (or, walking backwards from a named object, its target); "created_by" asks for the opposite
            asks_inverse = bool(wanted_p and _INVERSE_HINT.search(wanted_p))
            natural = incoming == inverse_seeds
            if wanted_p and sim >= 0.7 and (natural == asks_inverse):
                sim -= 0.1
            other_id = edge["src"] if incoming else (edge.get("dst") or "")
            score = sim + 0.15 * float(edge.get("confidence") or 0.5)
            if o is not None and o.type and other_id:
                score += 0.2 if idx.entities[other_id]["type"] == o.type else -0.2
            candidates.append((score, edge, seed, other_id))
    candidates.sort(key=lambda c: -c[0])
    strong = [c for c in candidates if c[0] >= 0.6]
    chosen = strong[:MAX_VALUES]
    # edges around the seed that do not say what the step asks stay out of the values; the best
    # few are reported so a reader can see what the graph does hold there
    record["near"] = [{"s": idx.name_of(e["src"]), "p": e["rel"],
                       "o": idx.name_of(e["dst"]) if e.get("dst") else str(e.get("dst_value") or "")}
                      for _sc, e, _seed, _other in candidates[:3]] if not strong else []

    graph_scores: Dict[str, float] = {}
    values: List[Dict[str, Any]] = []
    seen_vals = set()
    for score, edge, seed, other_id in chosen:
        if other_id:
            val = _value(idx.name_of(other_id), entity_id=other_id, etype=idx.entities[other_id]["type"])
        else:
            val = _value(str(edge.get("dst_value") or ""), kind="value")
        if val["name"] and (val["id"] or val["name"]) not in seen_vals:
            seen_vals.add(val["id"] or val["name"])
            values.append(val)
        record["triples"].append({"s": idx.name_of(edge["src"]),
                                  "p": edge["rel"], "o": idx.name_of(edge["dst"]) if edge.get("dst") else str(edge.get("dst_value") or ""),
                                  "score": round(score, 3), "evidence": list(edge.get("evidence") or [])[:4],
                                  "confidence": edge.get("confidence")})
        for ref in edge.get("evidence") or []:
            graph_scores[ref] = max(graph_scores.get(ref, 0.0), score)
    # a loose literal that only restates an entity already found ("by Mara" next to Mara) adds nothing
    named = [fold(v["name"]) for v in values if v["id"] and len(v["name"]) >= 3]
    values = [v for v in values if v["id"] or not any(n in fold(v["name"]) for n in named)]
    reach = set(seeds) | {v["id"] for v in values if v["id"]}
    for eid in reach:
        for ref in idx.entity_fragments(eid):
            graph_scores.setdefault(ref, 0.3)
    # text side: fragments of the reached entities and their neighbours, else every linked fragment
    pool = set(graph_scores)
    for eid in list(reach):
        for edge in idx.out_edges.get(eid, []) + idx.in_edges.get(eid, []):
            for ref in edge.get("evidence") or []:
                pool.add(ref)
    text_query = f"{question} {' '.join(idx.name_of(e) for e in seeds)}"
    text_scores = idx.text_scores(text_query, refs=pool if pool else None)
    if pool and not text_scores:
        text_scores = idx.text_scores(text_query)
    fused = weighted_merge(graph_scores, text_scores, run.alpha)
    ranked = sorted(fused.items(), key=lambda kv: -kv[1])[:run.top_k]
    for ref, score in ranked:
        text = idx.fragment_text(ref)
        if not text:
            continue
        record["fragments"].append({"ref": ref, "score": round(score, 3), "graph": round(graph_scores.get(ref, 0.0), 3),
                                    "text_score": round(text_scores.get(ref, 0.0), 3),
                                    "entities": [idx.name_of(e) for e in idx.fragment_entities(ref)][:6],
                                    "text": text[:SNIPPET_CHARS]})
    record["values"] = values
    return record


async def _run_retrieval(run: _Run, step: Step, question: str) -> Dict[str, Any]:
    record = await asyncio.to_thread(_retrieve, run, step, question)
    values = record.pop("values")
    run.note_sources([f["ref"] for f in record["fragments"]])
    if not values and record["fragments"] and not record["error"].startswith("no entity called"):
        # the graph has no edge for this hop: read the fused fragments
        values, summary = await _read_fragments(run, step, question, record["fragments"])
        record["summary"] = summary
        record["via"] = "text"
    elif values:
        record["via"] = "graph"
        if record["triples"]:
            record["summary"] = "; ".join(f"{t['s']} {t['p']} {t['o']}" for t in record["triples"][:6])
    else:
        record["via"] = "none"
        record["error"] = record["error"] or "nothing found for this step"
    seed_ids = record.pop("seed_ids")
    if step.o is not None and step.o.alias:
        run.values[step.o.alias] = values
    if step.s is not None and step.s.alias and not step.s.ref:
        run.values[step.s.alias] = [_value(run.index.name_of(i), entity_id=i, etype=run.index.entities[i]["type"])
                                    for i in seed_ids]
    if step.p_alias:
        run.values[step.p_alias] = [_value(t["p"], kind="text") for t in record["triples"][:3]]
    record["values"] = _names(values)
    return record


async def _read_fragments(run: _Run, step: Step, question: str, fragments: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    body = "\n".join(f"[{i + 1}] {f['text']}" for i, f in enumerate(fragments[:run.top_k]))
    prompt = ("Answer the question using only the numbered texts. Give the answer as a short list of values, one per line, "
              "without explanations. If the texts do not contain it, answer exactly NONE.\n\n"
              f"Question: {question}\n\nTexts:\n{body}")
    try:
        text = (await run.llm(prompt, max_tokens=200)).strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph_ask: fragment read failed (%s)", exc)
        return [], ""
    lines = [_LIST_MARK.sub("", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and ln.upper() != "NONE"]
    values = []
    for ln in lines[:MAX_VALUES]:
        found = run.index.find(ln, limit=1)
        values.append(_value(run.index.name_of(found[0]) if found and fold(run.index.name_of(found[0])) == fold(ln) else ln,
                             entity_id=found[0] if found and fold(run.index.name_of(found[0])) == fold(ln) else "",
                             kind="entity" if found and fold(run.index.name_of(found[0])) == fold(ln) else "text"))
    return values, text[:400]


# -- Math / Deduce / Output -----------------------------------------------------------------------------------

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg,
        ast.UAdd: operator.pos}


def safe_eval(expr: str) -> Optional[float]:
    """Evaluate plain arithmetic (numbers, + - * / // % **, parentheses); None otherwise."""
    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 12:
                raise ValueError("exponent too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.operand))
        raise ValueError("not arithmetic")
    if not re.fullmatch(r"[\d\s.+\-*/%()]+", expr or ""):
        return None
    try:
        return walk(ast.parse(expr.strip(), mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, RecursionError):
        return None


def _resolve_content(run: _Run, content: Sequence[str]) -> List[str]:
    out: List[str] = []
    for item in content:
        if re.fullmatch(r"[A-Za-z_]\w*", item) and item in run.values:
            out.extend(_names(run.bound(item)))
        else:
            out.append(item)
    return out


async def _run_reasoning(run: _Run, step: Step) -> Dict[str, Any]:
    items = _resolve_content(run, step.content)
    record: Dict[str, Any] = {"content": items, "error": ""}
    if step.op == "math":
        expr = " ".join(items) if step.content and all(re.fullmatch(r"[\d\s.+\-*/%()]+", i) for i in items) else ""
        number = safe_eval(expr) if expr else None
        if number is not None:
            text = str(int(number)) if float(number).is_integer() else f"{number:.6g}"
            record["via"] = "arithmetic"
            run.values[step.alias] = [_value(text, kind="text")]
            record["values"] = [text]
            return record
    instruction = {
        "math": "Do the calculation, count, sort or comparison the target asks for. Use only the content. Give the result only.",
        "judgement": "Decide whether the statement is true or false from the content. Start with Yes or No, then one short reason.",
        "entailment": "Infer the answer from the facts in the content. Give the conclusion only.",
        "extract": "Extract what the target asks for from the content. Give it only, one value per line.",
        "choice": "Choose the option the target asks for from the content. Give the choice only.",
        "multichoice": "Choose every option that applies. Give them one per line.",
    }[step.op if step.op == "math" else (step.deduce_op or "extract")]
    shown = "\n".join(f"- {i}" for i in items) or "(nothing was retrieved)"
    prompt = f"{instruction}\n\nTarget: {step.target or step.question}\nContent:\n{shown}"
    try:
        text = (await run.llm(prompt, max_tokens=250)).strip()
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"model: {type(exc).__name__}"
        text = ""
    record["via"] = "model"
    lines = [_LIST_MARK.sub("", ln).strip() for ln in text.splitlines() if ln.strip()]
    vals = []
    for ln in lines[:MAX_VALUES]:
        found = run.index.find(ln, limit=1)
        exact = bool(found) and fold(run.index.name_of(found[0])) == fold(ln)
        vals.append(_value(run.index.name_of(found[0]) if exact else ln, entity_id=found[0] if exact else "",
                           kind="entity" if exact else "text"))
    run.values[step.alias] = vals
    record["values"] = _names(vals)
    return record


async def _compose(run: _Run, question: str, outputs: Sequence[str]) -> Tuple[str, List[str]]:
    final: List[str] = []
    for alias in outputs:
        for name in _names(run.bound(alias)):
            if name not in final:
                final.append(name)
    findings: List[str] = []
    for n in sorted(run.records):
        rec = run.records[n]
        if rec.get("op") == "output":
            continue
        got = ", ".join(rec.get("values") or []) or ("nothing" if rec.get("error") else "")
        findings.append(f"Step{n} ({rec.get('rewritten') or rec.get('question')}): {got}")
        for frag in (rec.get("fragments") or [])[:2]:
            findings.append(f"  evidence {frag['ref']}: {frag['text'][:240]}")
    if not final:
        return "", final
    prompt = ("Answer the question in one or two sentences, in the language of the question, using ONLY the findings below. "
              "Name the values found. If the findings are insufficient, say what is missing instead of guessing.\n\n"
              f"Question: {question}\nValues for the answer: {', '.join(final)}\nFindings:\n" + "\n".join(findings))
    try:
        text = (await run.llm(prompt, max_tokens=300)).strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph_ask: compose failed (%s)", exc)
        text = ""
    return (text or ", ".join(final)), final


# -- driver ------------------------------------------------------------------------------------------------

async def _run_step(run: _Run, step: Step) -> None:
    started = time.monotonic()
    rewritten = await _rewrite(run, step) if step.needs and step.op == "retrieval" else step.question
    record: Dict[str, Any] = {"op": step.op, "question": step.question, "rewritten": rewritten if rewritten != step.question else "",
                              "level": step.level}
    try:
        if step.op == "retrieval":
            record.update(await _run_retrieval(run, step, rewritten or step.question))
        elif step.op in ("math", "deduce"):
            record.update(await _run_reasoning(run, step))
        else:
            record["values"] = [n for a in step.outputs for n in _names(run.bound(a))]
    except Exception as exc:  # noqa: BLE001 - a failing step is a recorded result, not a crash
        logger.debug("graph_ask: step %s failed", step.n, exc_info=True)
        record["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    record["ms"] = int((time.monotonic() - started) * 1000)
    run.records[step.n] = record


async def _flat(run: _Run, question: str) -> Dict[str, Any]:
    """No usable plan: one retrieval over every linked fragment, read by the model."""
    idx = run.index
    seeds = idx.entities_in(question, limit=4)
    graph_scores: Dict[str, float] = {}
    for eid in seeds:
        for ref in idx.entity_fragments(eid):
            graph_scores[ref] = 1.0
    text_scores = idx.text_scores(question)
    fused = weighted_merge(graph_scores, text_scores, run.alpha)
    ranked = sorted(fused.items(), key=lambda kv: -kv[1])[:run.top_k]
    frags = [{"ref": r, "score": round(sc, 3), "text": idx.fragment_text(r)[:SNIPPET_CHARS],
              "entities": [idx.name_of(e) for e in idx.fragment_entities(r)][:6]} for r, sc in ranked if idx.fragment_text(r)]
    run.note_sources([f["ref"] for f in frags])
    answer = ""
    if frags:
        body = "\n".join(f"[{i + 1}] {f['text']}" for i, f in enumerate(frags))
        try:
            answer = (await run.llm("Answer the question in one or two sentences using only the numbered texts; "
                                    "if they do not contain it, say so.\n\nQuestion: " + question + "\n\nTexts:\n" + body,
                                    max_tokens=300)).strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("graph_ask: flat answer failed (%s)", exc)
    return {"answer": answer, "fragments": frags, "seeds": [idx.name_of(e) for e in seeds]}


async def ask(owner: Any, question: str, *, llm: Optional[LlmFn] = None, index: Optional[MutualIndex] = None,
              max_steps: int = graph_plan.MAX_STEPS, top_k: int = DEFAULT_TOP_K, alpha: float = DEFAULT_ALPHA,
              plan_only: bool = False, timeout_s: float = 240.0) -> Dict[str, Any]:
    """Answer `question` from `owner`'s Cerebro. Never raises for an ordinary
    failure: the result's ``status`` is ``ok``, ``degraded`` (no usable plan: a
    flat search answered), ``no_model``, ``empty`` (the graph has nothing) or
    ``error``."""
    started = time.monotonic()
    question = str(question or "").strip()
    out: Dict[str, Any] = {"question": question, "status": "ok", "answer": "", "values": [], "plan": [], "steps": [],
                           "levels": [], "sources": [], "model_calls": 0, "ms": 0, "alpha": alpha, "untrusted_content": True}
    if not question:
        out.update(status="error", error="the question is empty")
        return out
    fn = llm or default_llm(owner)
    if fn is None:
        out.update(status="no_model", error="no model endpoint is configured for Cerebro questions")
        return out
    counted = _Counted(fn)
    try:
        index = index or await asyncio.to_thread(MutualIndex.build, owner)
    except Exception as exc:  # noqa: BLE001
        out.update(status="error", error=f"the Cerebro could not be read: {type(exc).__name__}")
        return out
    out["graph"] = {"entities": len(index.entities), "relations": len(index.relations), "fragments": len(index.ref_entities)}
    if not index.entities:
        out.update(status="empty", error="the Cerebro has no entities yet")
        return out
    run = _Run(index, counted, top_k=top_k, alpha=alpha)
    model_down: List[bool] = []

    async def work() -> None:
        try:
            steps, info = await graph_plan.plan(question, index.schema(), counted, max_steps=max_steps)
            out["planner"] = {"attempts": info["attempts"], "errors": info["errors"]}
        except PlanError as exc:
            out["planner"] = {"attempts": 2, "errors": [str(exc)]}
            steps = []
        except Exception as exc:  # noqa: BLE001 - the model did not answer at all
            out["planner"] = {"attempts": 1, "errors": [f"model: {type(exc).__name__}"]}
            steps = []
            model_down.append(True)
        if steps:
            out["plan"] = [s.public() for s in steps]
            out["levels"] = [[s.n for s in lvl] for lvl in graph_plan.levels(steps)]
        if plan_only:
            return
        if not steps:
            flat = await _flat(run, question)
            out.update(status="degraded", answer=flat["answer"], fragments=flat["fragments"],
                       error="no usable plan; answered from a flat search")
            if model_down and not flat["answer"]:
                out.update(status="error", error="the model did not answer")
            return
        for lvl in graph_plan.levels(steps):
            await asyncio.gather(*[_run_step(run, s) for s in lvl])
        output = next((s for s in steps if s.op == "output"), None)
        answer, final = await _compose(run, question, output.outputs if output else [])
        out["answer"] = answer
        out["values"] = final
        if not final:
            out["status"] = "ok"
            out["answer"] = out["answer"] or "No he encontrado en el Cerebro lo necesario para responder."
            out["found"] = False
        else:
            out["found"] = True
        out["steps"] = [dict(run.records[n], n=n) for n in sorted(run.records)]

    try:
        await asyncio.wait_for(work(), timeout=timeout_s)
    except asyncio.TimeoutError:
        out.update(status="error", error=f"timed out after {int(timeout_s)} s")
        out["steps"] = [dict(run.records[n], n=n) for n in sorted(run.records)]
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph_ask failed", exc_info=True)
        out.update(status="error", error=f"{type(exc).__name__}: {str(exc)[:200]}")
    out["sources"] = list(run.sources)[:40]
    out["model_calls"] = counted.calls
    out["ms"] = int((time.monotonic() - started) * 1000)
    return out
