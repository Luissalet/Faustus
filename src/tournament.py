"""tournament.py — the same prompt to N models, blind, then explicit fusion.

Faustus already has a side-by-side comparator (routes/compare/compare_routes.py):
two models, one prompt, a human vote. This is the other half of the idea, from
the `llm_multi_round_coding_tournament` report:

  * **Round 0** — every model answers the prompt **blind and in parallel**. No
    model sees another's answer, so nothing anchors on whoever answered first.
  * **Rounds 1..n** — every model is shown **all** of the previous round's
    answers, anonymised as ``Solution A / B / C`` (never the model names — a
    name is a reputation, and a reputation biases the fusion), with the
    instruction to *take the best ideas from all of them where they are
    complementary, not conflicting, and weave a hybrid*.
  * **Judging** — one model scores every final answer 0-100 on correctness,
    completeness and sophistication, in strict JSON.

Three properties are load-bearing, and all three are about honesty:

1. **The parallelism is real only across DISTINCT models.** Measured on this
   machine (FAUSTUS.md §20): two requests to the SAME model serialize behind
   one llama-server slot, while two DIFFERENT models genuinely generate at the
   same time. So the scheduler holds a per-model lock — two entries naming the
   same model run one after the other — inside the machine-wide GPU semaphore
   the workers already share (``subagent_tools.shared_slots``). The order is
   never the other way round: a task holding a scarce GPU slot while it waits
   for a model lock held by a task waiting for a slot is a deadlock.
2. **A judge score is never invented.** A malformed judgement is retried once
   and then that answer's score is ``null``. Alongside it there is always a
   deterministic, model-free tiebreak (``tiebreak_scores``) so the ranking is
   still ordered when judging is unavailable — clearly labelled as such, never
   passed off as a judgement.
3. **A model that fails does not fail the tournament.** It keeps its last good
   answer, is retired from the later rounds, and the rest continue. A model the
   user STOPPED is ``cancelled``, not an error (the four-value outcomes of
   src/tool_outcome.py).

``rounds`` is a MAXIMUM, exactly like a dispatched job's ``fix_rounds``: the
loop also ends itself as soon as the rounds stop changing anything, read from
each model's successive answers by ``src/convergence.py`` — then
``stopped_by`` is ``"convergence"`` and the score is in the result.

``llm_call`` is injected, so every one of these paths is testable without a
model. Pure stdlib; ``src.convergence`` and the GPU semaphore are imported
lazily and their absence degrades instead of raising.

The result::

    {"prompt", "models", "rounds", "rounds_run", "stopped_by", "convergence",
     "answers": [{"entry", "model", "round", "text", "elapsed_s", "tokens",
                  "tokens_source"}...],
     "final":   [{"entry", "model", "round", "text", "scores"|null, "total"|null,
                  "tiebreak", "rank", "outcome"}...],
     "judge": {...}|null, "ranking": "judge"|"mixed"|"deterministic",
     "ranking_note": str, "merge_prompt": str,
     "events": [...], "errors": [...], "cancelled": [...], "degraded": bool}
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import random
import re
import time
import uuid
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ── shape of a tournament ───────────────────────────────────────────────────

MIN_MODELS = 2
DEFAULT_MAX_MODELS = 4            # the `agent_tournament_max_models` default
HARD_MAX_MODELS = 8               # a ceiling a setting cannot climb past
DEFAULT_ROUNDS = 3                # round 0 + two fusion rounds
MAX_ROUNDS = 6
PROMPT_CHARS = 20_000             # the task, bounded
PEER_CHARS = 6_000                # one peer answer inside a fusion prompt
JUDGE_PEER_CHARS = 6_000
EVENTS_KEPT = 600
MAX_RUNS_KEPT = 100
JUDGE_RAW_CHARS = 2_000
MIN_SCRUB_NAME = 3                # names shorter than this are not scrubbed

# The three axes, in the order they are asked for and reported.
AXES = ("correctness", "completeness", "sophistication")

# The fusion instruction, quoted close to the report it comes from.
FUSION_INSTRUCTION = (
    "Take the best ideas from all of them where they are complementary, not "
    "conflicting, and weave a hybrid that is better than any single one."
)

# The deterministic tiebreak: how much of the OTHER answers' key terms an
# answer covers, and where its length falls among them. Coverage weighs more —
# it is the one that says something about the content.
W_COVERAGE = 0.65
W_LENGTH = 0.35
KEY_TERM_MIN = 4                  # a "key term" is at least this many characters

_LABELS = "ABCDEFGH"
_LIVE = ("queued", "running", "judging", "cancelling")
_WS_RE = re.compile(r"\s+")
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)

# Function words carry no evidence; the same idea as expert_review._terms, kept
# small on purpose — this is a tiebreak, not a search engine.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "your", "you",
    "are", "was", "were", "have", "has", "had", "not", "but", "its", "their",
    "there", "here", "when", "what", "which", "than", "then", "them", "should",
    "would", "could", "must", "make", "makes", "made", "more", "most", "very",
    "such", "each", "also", "because", "about", "over", "under", "will", "can",
    "any", "all", "one", "two", "how", "why", "use", "used", "using", "does",
})


class TournamentError(ValueError):
    """Unusable input — the routes map it to a 400."""


class ModelCancelled(Exception):
    """One model was stopped by the user. Raised by an injected ``llm_call``,
    it retires that model as ``cancelled`` — never as an error — and the rest
    of the tournament carries on."""


# ── small helpers (none of these raise) ─────────────────────────────────────

def _squash(text: Any, limit: int) -> str:
    s = _WS_RE.sub(" ", str(text or "")).strip()
    return s if len(s) <= limit else s[: max(0, limit - 1)].rstrip() + "…"


def _clip(text: Any, limit: int) -> str:
    """Keep the shape of an answer (newlines and all), bounded."""
    s = str(text or "")
    return s if len(s) <= limit else s[:limit].rstrip() + "\n… (truncated)"


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001 - a run never fails over a settings read
        return default


def enabled() -> bool:
    """`agent_tournament`. Off = the routes refuse to START a run; everything
    already recorded stays readable."""
    return bool(_setting("agent_tournament", True))


def max_models() -> int:
    try:
        n = int(_setting("agent_tournament_max_models", DEFAULT_MAX_MODELS) or DEFAULT_MAX_MODELS)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_MODELS
    return max(MIN_MODELS, min(HARD_MAX_MODELS, n))


def _num(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return default if n != n else n            # NaN


def label_for(index: int) -> str:
    """``A``, ``B``, … and ``S9`` past the alphabet we cap at anyway."""
    i = int(index) if isinstance(index, int) else 0
    return _LABELS[i] if 0 <= i < len(_LABELS) else f"S{i}"


def strongest(models: Sequence[Any]) -> str:
    """The default judge: the biggest model by the parameter count in its own
    name (``qwen3.5:32b`` → 32), ties and unreadable names keeping the caller's
    order. No model is called to decide this."""
    best, best_size = "", -1.0
    for raw in models or []:
        name = str(raw or "").strip()
        if not name:
            continue
        size = -1.0
        for m in _SIZE_RE.finditer(name):
            size = max(size, _num(m.group(1), -1.0))
        if size > best_size:
            best, best_size = name, size
    return best


# ── the scheduler: same model serialises, different models overlap ──────────

_MODEL_LOCKS: Dict[Tuple[int, str], asyncio.Lock] = {}


def model_lock(model: str) -> asyncio.Lock:
    """The one llama-server slot per model, as a lock.

    Keyed by the running event loop too — asyncio primitives bind to the loop
    that first waits on them and the test suite runs one loop per test, the
    same reason ``subagent_tools.shared_slots`` is keyed that way.
    """
    try:
        loop_key = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_key = 0
    key = (loop_key, str(model or ""))
    lock = _MODEL_LOCKS.get(key)
    if lock is None:
        if len(_MODEL_LOCKS) > 256:            # forget the idle ones, never a held one
            for k, v in [(k, v) for k, v in _MODEL_LOCKS.items() if not v.locked()]:
                _MODEL_LOCKS.pop(k, None)
        lock = _MODEL_LOCKS[key] = asyncio.Lock()
    return lock


def gpu_slots(endpoint_url: str = "", override: Any = None) -> Any:
    """The machine-wide GPU semaphore the workers already share. `override` is
    what the tests inject; ``None`` asks ``subagent_tools.shared_slots`` and
    degrades to no semaphore at all if it cannot be had."""
    if override is not None:
        return override
    try:
        from src.agent_tools.subagent_tools import shared_slots
        n = int(_setting("agent_subagent_max_parallel", 2) or 0)
        return shared_slots(endpoint_url, max(1, n)) if n > 0 else None
    except Exception as e:  # noqa: BLE001 - a tournament runs without it
        logger.debug("tournament: shared GPU slots unavailable: %s", e)
        return None


class _Gate:
    """One model call's turn on the machine.

    ORDER MATTERS: the per-model lock is taken FIRST and the GPU slot second.
    The other way round, a task holding the last GPU slot while it waits for a
    model lock held by a task that is itself waiting for a slot deadlocks the
    whole round.
    """

    def __init__(self, model: str, slots: Any):
        self._lock = model_lock(model)
        self._slots = slots
        self._have_slot = False

    async def __aenter__(self) -> "_Gate":
        await self._lock.acquire()
        if self._slots is not None:
            try:
                await self._slots.acquire()
                self._have_slot = True
            except BaseException:
                self._lock.release()
                raise
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        if self._have_slot:
            self._have_slot = False
            try:
                self._slots.release()
            except Exception as e:  # noqa: BLE001 - releasing must not mask the call's own error
                logger.debug("tournament: releasing the GPU slot failed: %s", e)
        try:
            self._lock.release()
        except RuntimeError:
            pass
        return False


# ── calling the injected model function ─────────────────────────────────────

def _binder(llm_call: Callable) -> Callable[[List[Dict[str, str]], str], Any]:
    """How to call the injected function. A tournament drives SEVERAL models
    through one callable, so the preferred shape is ``llm_call(messages,
    model)``; a one-argument function (the expert_review shape) still works and
    simply always talks to the model it was bound to."""
    try:
        params = list(inspect.signature(llm_call).parameters.values())
    except (TypeError, ValueError):
        return lambda messages, model: llm_call(messages, model)
    kinds = {p.kind for p in params}
    positional = [p for p in params
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if inspect.Parameter.VAR_POSITIONAL in kinds or len(positional) >= 2:
        return lambda messages, model: llm_call(messages, model)
    if (inspect.Parameter.VAR_KEYWORD in kinds
            or any(p.kind == p.KEYWORD_ONLY and p.name == "model" for p in params)):
        return lambda messages, model: llm_call(messages, model=model)
    return lambda messages, model: llm_call(messages)


def _answer_of(result: Any) -> Tuple[str, Optional[int]]:
    """(text, tokens) out of whatever the injected call returned: a string, or
    a dict carrying the text under one of the usual keys plus a token count."""
    if isinstance(result, dict):
        text = result.get("content")
        if text is None:
            text = result.get("text")
        if text is None:
            text = result.get("answer") or ""
        tokens = result.get("tokens")
        if tokens is None:
            tokens = result.get("output_tokens")
        try:
            tokens = int(tokens) if tokens is not None else None
        except (TypeError, ValueError):
            tokens = None
        return str(text or ""), tokens
    if isinstance(result, (list, tuple)) and result:
        return str(result[0] or ""), None
    return str(result or ""), None


def estimate_tokens(text: Any) -> int:
    """A rough count when the endpoint reported none. Reported as an ESTIMATE
    in the answer (`tokens_source`), never as a measurement."""
    return max(0, len(str(text or "")) // 4)


# ── the prompts ─────────────────────────────────────────────────────────────

_SYSTEM_BLIND = (
    "You are one of several independent models answering the same task. Answer it "
    "completely and concretely, on your own. Do not ask questions back."
)
_SYSTEM_FUSION = (
    "You are one of several models improving an answer to the same task. You are "
    "shown every candidate answer from the previous round, anonymised. Judge them on "
    "their content alone."
)


def build_round0_messages(prompt: str) -> List[Dict[str, str]]:
    """Round 0 is BLIND: nothing but the task itself goes in."""
    return [{"role": "system", "content": _SYSTEM_BLIND},
            {"role": "user", "content": _clip(prompt, PROMPT_CHARS)}]


def build_fusion_messages(prompt: str, peers: Sequence[Dict[str, Any]],
                          round_no: int) -> List[Dict[str, str]]:
    """A fusion round: the task, then every previous answer under a neutral
    label, then the instruction to weave the complementary parts together."""
    lines = ["The task was:", "", _clip(prompt, PROMPT_CHARS), "",
             f"Round {int(round_no)}. These are the candidate answers from the previous "
             f"round, in no particular order and with their authors withheld:", ""]
    for peer in peers or []:
        label = str((peer or {}).get("label") or "?")
        lines.append(f"--- Solution {label} ---")
        lines.append(_clip((peer or {}).get("text"), PEER_CHARS))
        lines.append("")
    lines.append(FUSION_INSTRUCTION)
    lines.append(
        "Where two solutions genuinely conflict, choose the one you can defend and say "
        "in one line why. Do not name or guess at the authors. Answer with the complete "
        "improved solution, not with a diff or a commentary.")
    return [{"role": "system", "content": _SYSTEM_FUSION},
            {"role": "user", "content": "\n".join(lines)}]


def build_judge_messages(prompt: str, solutions: Sequence[Dict[str, Any]], *,
                         retry: bool = False) -> List[Dict[str, str]]:
    """The rubric. It asks for strict JSON and says, in as many words, that a
    solution it cannot score must be OMITTED rather than given a number."""
    labels = ", ".join(str((s or {}).get("label") or "?") for s in solutions or [])
    lines = [
        "You are judging candidate answers to the same task. They are anonymised; judge "
        "the content, nothing else.", "",
        "The task was:", "", _clip(prompt, PROMPT_CHARS), "",
    ]
    for sol in solutions or []:
        lines.append(f"--- Solution {str((sol or {}).get('label') or '?')} ---")
        lines.append(_clip((sol or {}).get("text"), JUDGE_PEER_CHARS))
        lines.append("")
    lines += [
        "Score EVERY solution on three axes, each a whole number from 0 to 100:",
        "  correctness    — is what it says true, and does it actually solve the task",
        "  completeness   — does it cover the whole task, edge cases included",
        "  sophistication — depth, structure and craft beyond the obvious", "",
        "Answer with STRICT JSON and nothing else — no prose, no code fence:",
        '{"scores": [{"solution": "A", "correctness": 0, "completeness": 0, '
        '"sophistication": 0, "note": "one sentence"}]}',
        f"One row per solution ({labels}), each exactly once.",
        "If you cannot score a solution, LEAVE IT OUT. Never invent a number.",
    ]
    if retry:
        lines += ["",
                  "Your previous answer was not valid JSON. Send the JSON object only: "
                  "it must start with { and end with } and contain nothing else."]
    return [{"role": "system", "content": "You are a strict, terse judge. You answer in JSON only."},
            {"role": "user", "content": "\n".join(lines)}]


def synthesis_prompt(prompt: str, finals: Sequence[Dict[str, Any]]) -> str:
    """The "Merge" prompt: the finalists, ranked, with the same fusion
    instruction — ready to drop into the composer. Empty when there is nothing
    to merge: a merge prompt with no solutions in it is worse than no button."""
    usable = [r for r in (finals or []) if str((r or {}).get("text") or "").strip()]
    if not usable:
        return ""
    lines = ["Here are the final answers from a model tournament on this task.", "",
             "The task was:", "", _clip(prompt, PROMPT_CHARS), ""]
    for i, row in enumerate(usable):
        text = str((row or {}).get("text") or "")
        rank = (row or {}).get("rank")
        head = f"--- Solution {label_for(i)}"
        if isinstance(rank, int):
            head += f" (ranked {rank}"
            total = (row or {}).get("total")
            if isinstance(total, (int, float)):
                head += f", judged {int(total)}/300"
            head += ")"
        lines.append(head + " ---")
        lines.append(_clip(text, PEER_CHARS))
        lines.append("")
    lines.append(FUSION_INSTRUCTION)
    lines.append("Write the final answer. Where the solutions conflict, pick one and say why "
                 "in a line.")
    return "\n".join(lines)


# ── anonymising: a name is a reputation, and a reputation biases the fusion ──

def _scrub(text: Any, names: Sequence[str]) -> str:
    """Take the contestants' own names out of an answer before another model
    reads it. Anonymised labels are pointless if an answer opens with "As
    Qwen, I…". Names shorter than three characters are left alone — they match
    ordinary words."""
    out = str(text or "")
    seen = set()
    for raw in names or []:
        for part in {str(raw or ""), str(raw or "").split(":")[0], str(raw or "").split("/")[-1]}:
            part = part.strip()
            if len(part) < MIN_SCRUB_NAME or part.lower() in seen:
                continue
            seen.add(part.lower())
            try:
                out = re.sub(re.escape(part), "the model", out, flags=re.IGNORECASE)
            except re.error:                    # pragma: no cover - escape cannot fail
                continue
    return out


def anonymize(entries: Sequence[Dict[str, Any]], names: Sequence[str],
              *, seed: Any = None, salt: str = "") -> List[Dict[str, Any]]:
    """``[{"entry", "label", "text"}]`` for every entry that has an answer, in
    a deterministic order that does not follow the caller's model list when a
    seed is given (so the labels carry no information either)."""
    rows = [e for e in entries or [] if isinstance(e, dict) and (e.get("answers") or [])]
    order = list(range(len(rows)))
    if seed is not None:
        try:
            random.Random(f"{seed}|{salt}").shuffle(order)
        except Exception:  # noqa: BLE001 - a shuffle never breaks a round
            order = list(range(len(rows)))
    out: List[Dict[str, Any]] = []
    for label_index, row_index in enumerate(order):
        row = rows[row_index]
        out.append({"entry": row.get("entry"), "label": label_for(label_index),
                    "text": _scrub((row.get("answers") or [])[-1].get("text"), names)})
    return out


# ── the deterministic, model-free tiebreak ──────────────────────────────────

def key_terms(text: Any) -> set:
    """The content words of an answer: the convergence tokenizer, minus the
    short ones and the function words."""
    try:
        from src import convergence
        tokens = convergence.tokenize(text)
    except Exception:  # noqa: BLE001 - the tiebreak still has to produce a number
        tokens = set(re.findall(r"[A-Za-z0-9_]+", str(text or "").lower()))
    return {t for t in tokens if len(t) >= KEY_TERM_MIN and t not in _STOPWORDS}


def tiebreak_scores(texts: Sequence[Any]) -> List[Dict[str, float]]:
    """A ranking signal with no model in it: how much of the OTHER answers' key
    terms each answer covers, and where its length falls among them.

    Coverage is the honest half — an answer that contains what the others
    contain has absorbed them. Length is the cheap half, and weighs less. The
    result is only ever presented as a tiebreak, never as a judgement.
    """
    items = [str(t or "") for t in (texts or [])]
    n = len(items)
    if n == 0:
        return []
    terms = [key_terms(t) for t in items]
    lengths = [len(t) for t in items]
    out: List[Dict[str, float]] = []
    for i in range(n):
        others: set = set()
        for j in range(n):
            if j != i:
                others |= terms[j]
        coverage = (len(terms[i] & others) / len(others)) if others else 1.0
        below = sum(1 for j in range(n) if j != i and lengths[j] < lengths[i])
        equal = sum(1 for j in range(n) if j != i and lengths[j] == lengths[i])
        pct = ((below + 0.5 * equal) / (n - 1)) if n > 1 else 0.5
        score = W_COVERAGE * coverage + W_LENGTH * pct
        out.append({"score": round(score, 4), "coverage": round(coverage, 4),
                    "length_percentile": round(pct, 4), "chars": lengths[i]})
    return out


# ── reading the judge's answer ──────────────────────────────────────────────

def _balanced(text: str, opener: str, closer: str) -> List[str]:
    """Every balanced ``{...}`` / ``[...]`` run, quote-aware."""
    out: List[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            if depth == 0:
                start = i
            depth += 1
        elif ch == closer and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start:i + 1])
    return out


def _json_candidates(raw: str) -> List[Any]:
    text = str(raw or "").strip()
    if not text:
        return []
    tries = [text]
    tries += [m.group(1) for m in _FENCE_RE.finditer(text)]
    tries += _balanced(text, "{", "}")
    tries += _balanced(text, "[", "]")
    out: List[Any] = []
    for candidate in tries:
        for attempt in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
            try:
                out.append(json.loads(attempt))
                break
            except (ValueError, TypeError):
                continue
    return out


def _score_value(value: Any) -> Optional[int]:
    """0-100, or None. A string of digits counts; anything else does not, and
    an out-of-range number is clamped, never dropped and never invented."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n:
        return None
    return int(round(max(0.0, min(100.0, n))))


def _row_label(row: Dict[str, Any], known: Sequence[str]) -> str:
    for key in ("solution", "label", "id", "name", "answer"):
        raw = row.get(key)
        if raw is None:
            continue
        token = str(raw).strip().upper()
        token = token.replace("SOLUTION", "").strip(" :.#-")
        if token in known:
            return token
        if len(token) > 1 and token[0] in known:
            return token[0]
    return ""


def parse_judgement(answer: Any, labels: Sequence[str]) -> Optional[Dict[str, Dict[str, Any]]]:
    """Strict JSON in, a per-label score map out. ``None`` when the answer is
    not a judgement at all — the caller retries once and then reports ``null``
    scores. A label the judge omitted stays out of the map; it never gets a
    number this function made up."""
    known = [str(x) for x in labels or []]
    for parsed in _json_candidates(answer):
        rows: List[Any] = []
        if isinstance(parsed, dict):
            for key in ("scores", "solutions", "results", "judgement", "judgments"):
                inner = parsed.get(key)
                if isinstance(inner, list):
                    rows = list(inner)
                    break
                if isinstance(inner, dict):
                    rows = [dict(v, solution=k) for k, v in inner.items()
                            if isinstance(v, dict)]
                    break
            if not rows:
                # {"A": {...}, "B": {...}} — a mapping is a judgement too
                rows = [dict(v, solution=k) for k, v in parsed.items()
                        if isinstance(v, dict) and str(k).strip().upper() in known]
        elif isinstance(parsed, list):
            rows = list(parsed)
        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = _row_label(row, known)
            if not label or label in out:
                continue
            scores = {axis: _score_value(row.get(axis)) for axis in AXES}
            total = (sum(scores[a] for a in AXES)
                     if all(scores[a] is not None for a in AXES) else None)
            out[label] = {**scores, "total": total,
                          "note": _squash(row.get("note") or row.get("reason"), 240)}
        if out:
            return out
    return None


# ── convergence: the rounds stop when they stop changing anything ───────────

def assess_convergence(entries: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """``src.convergence.assess`` over EACH model's successive answers, folded
    into one verdict: the ensemble has converged only when every model that has
    a trend to read has converged — one settled model must not stop a round the
    others are still using.
    """
    try:
        from src import convergence
    except Exception as e:  # noqa: BLE001 - the tournament runs without it
        logger.debug("tournament: convergence unavailable: %s", e)
        return None
    per: Dict[str, Any] = {}
    scores: List[float] = []
    for entry in entries or []:
        answers = [a.get("text") for a in (entry or {}).get("answers") or []]
        if len(answers) < 2:
            continue
        try:
            verdict = convergence.assess(answers)
        except Exception as e:  # noqa: BLE001 - a scorer never breaks the round
            logger.debug("tournament: assess failed: %s", e)
            continue
        key = f"{entry.get('entry')}:{entry.get('model')}"
        per[key] = verdict
        scores.append(_num(verdict.get("score")))
    if not per:
        return None
    mean = sum(scores) / len(scores)
    converged = all(bool(v.get("converged")) for v in per.values())
    if converged:
        reason = (f"every model's last rounds changed almost nothing "
                  f"(mean score {mean:.2f}) — another round is unlikely to")
    else:
        still = [k.split(":", 1)[-1] for k, v in per.items() if not v.get("converged")]
        reason = (f"still changing: {', '.join(still[:4])} (mean score {mean:.2f})")
    return {"score": round(mean, 3), "converged": converged, "per_model": per,
            "models_assessed": len(per), "reason": reason}


# ── the run ─────────────────────────────────────────────────────────────────

def _clean_models(raw: Any, cap: int) -> List[str]:
    if isinstance(raw, str):
        items = [p for p in re.split(r"[,\n]", raw)]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise TournamentError("models must be a list of 2 or more model names")
    names = [str(m or "").strip() for m in items]
    names = [m for m in names if m]
    if len(names) < MIN_MODELS:
        raise TournamentError(f"a tournament needs at least {MIN_MODELS} models")
    if len(names) > cap:
        raise TournamentError(f"at most {cap} models (Settings → Agent & automation → Tournament)")
    return names


async def _emit(on_event: Optional[Callable], events: Deque[Dict[str, Any]],
                **payload: Any) -> None:
    """One state change. The sink is a courtesy: it never breaks a round."""
    payload.setdefault("ts", time.time())
    events.append(payload)
    if on_event is None:
        return
    try:
        result = on_event(dict(payload))
        if inspect.isawaitable(result):
            await result
    except Exception as e:  # noqa: BLE001 - the UI's stream is not the tournament
        logger.debug("tournament: on_event failed: %s", e)


def _cancelled(cancel_event: Any) -> bool:
    try:
        return bool(cancel_event is not None and cancel_event.is_set())
    except Exception:  # noqa: BLE001
        return False


async def run(prompt: str, models: Sequence[str], *, rounds: int = DEFAULT_ROUNDS,
              judge_model: Optional[str] = None, llm_call: Optional[Callable] = None,
              on_event: Optional[Callable] = None, seed: Any = None,
              slots: Any = None, endpoint_url: str = "",
              state: Optional[Dict[str, Any]] = None,
              cancel_event: Any = None,
              model_cap: Optional[int] = None) -> Dict[str, Any]:
    """Run one tournament and return the result.

    ``rounds`` is the MAXIMUM number of rounds INCLUDING the blind round 0, and
    the loop also ends itself when the rounds stop changing anything.
    ``state``, when given, is the very dict this returns — filled in as the run
    proceeds, so a caller holding it can read a partial result at any moment
    (that is how the HTTP job answers while it is still running).
    """
    body = str(prompt or "")
    if not body.strip():
        raise TournamentError("There is no prompt to run")
    if not callable(llm_call):
        raise TournamentError("run() needs an llm_call to talk to the models")
    names = _clean_models(models, int(model_cap or max_models()))
    try:
        rounds = int(rounds)
    except (TypeError, ValueError):
        rounds = DEFAULT_ROUNDS
    rounds = max(1, min(MAX_ROUNDS, rounds))
    judge = str(judge_model or "").strip() or strongest(names) or names[0]
    call = _binder(llm_call)
    pool = gpu_slots(endpoint_url, slots)
    events: Deque[Dict[str, Any]] = deque(maxlen=EVENTS_KEPT)

    entries: List[Dict[str, Any]] = [
        {"entry": i, "model": name, "answers": [], "outcome": "success", "error": None}
        for i, name in enumerate(names)]
    result: Dict[str, Any] = state if isinstance(state, dict) else {}
    result.clear()
    result.update({
        "prompt": body, "models": list(names), "rounds": rounds, "rounds_run": 0,
        "stopped_by": None, "convergence": None, "answers": [], "final": [],
        "judge": None, "ranking": "deterministic", "ranking_note": "",
        "merge_prompt": "", "events": [], "errors": [], "cancelled": [],
        "degraded": False, "seed": seed, "judge_model": judge,
    })

    def _live() -> List[Dict[str, Any]]:
        return [e for e in entries if e["outcome"] == "success"]

    async def _one(entry: Dict[str, Any], messages: List[Dict[str, str]], rnd: int) -> None:
        """One model, one round: its turn on the machine, then the call."""
        model = entry["model"]
        await _emit(on_event, events, event="model_start", entry=entry["entry"],
                    model=model, round=rnd)
        started = time.monotonic()
        try:
            async with _Gate(model, pool):
                if _cancelled(cancel_event):
                    raise ModelCancelled("the tournament was cancelled")
                answer = call(messages, model)
                if inspect.isawaitable(answer):
                    answer = await answer
        except ModelCancelled as e:
            entry["outcome"] = "cancelled"
            entry["error"] = _squash(e, 300) or "stopped"
            result["cancelled"].append({"entry": entry["entry"], "model": model,
                                        "round": rnd, "reason": entry["error"]})
            result["degraded"] = True
            await _emit(on_event, events, event="model_cancelled", entry=entry["entry"],
                        model=model, round=rnd, outcome="cancelled")
            return
        except asyncio.CancelledError:
            # The whole run is unwinding. Record what happened to this model —
            # a stopped model is `cancelled`, never a failure — and let the
            # cancellation through, so a caller that really cancelled us is
            # obeyed instead of silently continued.
            entry["outcome"] = "cancelled"
            entry["error"] = "cancelled"
            result["cancelled"].append({"entry": entry["entry"], "model": model,
                                        "round": rnd, "reason": "cancelled"})
            result["degraded"] = True
            raise
        except Exception as e:  # noqa: BLE001 - one model is not the tournament
            entry["outcome"] = "error"
            entry["error"] = _squash(e, 300) or e.__class__.__name__
            result["errors"].append({"entry": entry["entry"], "model": model, "round": rnd,
                                     "error": entry["error"], "outcome": "expected_error"})
            result["degraded"] = True
            logger.warning("tournament: %s failed in round %s: %s", model, rnd, e)
            await _emit(on_event, events, event="model_error", entry=entry["entry"],
                        model=model, round=rnd, error=entry["error"], outcome="expected_error")
            return
        text, tokens = _answer_of(answer)
        record = {"entry": entry["entry"], "model": model, "round": rnd, "text": text,
                  "elapsed_s": round(max(0.0, time.monotonic() - started), 3),
                  "tokens": tokens if tokens is not None else estimate_tokens(text),
                  "tokens_source": "reported" if tokens is not None else "estimated"}
        entry["answers"].append(record)
        result["answers"].append(record)
        await _emit(on_event, events, event="answer", entry=entry["entry"], model=model,
                    round=rnd, chars=len(text), elapsed_s=record["elapsed_s"],
                    tokens=record["tokens"])

    await _emit(on_event, events, event="start", models=list(names), rounds=rounds,
                judge_model=judge)

    rounds_run = 0
    try:
        for rnd in range(rounds):
            if _cancelled(cancel_event):
                result["stopped_by"] = "cancelled"
                break
            live = _live()
            if not live:
                break
            if rnd == 0:
                plans = [(e, build_round0_messages(body)) for e in live]
            else:
                peers = anonymize(entries, names, seed=seed, salt=f"round{rnd}")
                plans = [(e, build_fusion_messages(body, peers, rnd)) for e in live]
            await _emit(on_event, events, event="round_start", round=rnd,
                        models=[e["model"] for e in live], blind=(rnd == 0))
            # Round 0 is blind and parallel; every round after it is parallel
            # too. The gate inside _one is what makes two entries naming the
            # SAME model take their turns one after the other.
            await asyncio.gather(*[_one(e, m, rnd) for e, m in plans])
            rounds_run = rnd + 1
            result["rounds_run"] = rounds_run
            await _emit(on_event, events, event="round_end", round=rnd,
                        answered=len([e for e in live if e["answers"]]))
            if _cancelled(cancel_event):
                result["stopped_by"] = "cancelled"
                break
            if rnd >= 1:
                verdict = assess_convergence(entries)
                if verdict is not None:
                    result["convergence"] = verdict
                    await _emit(on_event, events, event="convergence", round=rnd,
                                score=verdict.get("score"), converged=verdict.get("converged"))
                    if verdict.get("converged") and rnd < rounds - 1:
                        result["stopped_by"] = "convergence"
                        break
        if not result["stopped_by"]:
            result["stopped_by"] = "cancelled" if _cancelled(cancel_event) else "rounds"
    finally:
        result["rounds_run"] = rounds_run
        result["events"] = list(events)

    finals = _finalists(entries)
    await _judge_and_rank(result, body, finals, entries, judge, call, pool, on_event,
                          events, seed, cancel_event)
    result["events"] = list(events)
    return result


def _finalists(entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every entry that produced at least one answer, with its LAST one — a
    model that failed in round 2 still competes with what it wrote in round 1."""
    out: List[Dict[str, Any]] = []
    for entry in entries:
        answers = entry.get("answers") or []
        if not answers:
            continue
        last = answers[-1]
        out.append({"entry": entry["entry"], "model": entry["model"],
                    "round": last.get("round"), "text": last.get("text") or "",
                    "outcome": entry.get("outcome") or "success",
                    "error": entry.get("error")})
    return out


async def _judge_and_rank(result: Dict[str, Any], prompt: str, finals: List[Dict[str, Any]],
                          entries: Sequence[Dict[str, Any]], judge: str, call: Callable,
                          pool: Any, on_event: Optional[Callable],
                          events: Deque[Dict[str, Any]], seed: Any,
                          cancel_event: Any) -> None:
    """Score the finalists, tiebreak them deterministically, rank them. A judge
    that cannot be read leaves every score ``null`` and the ranking falls back
    to the tiebreak, said out loud."""
    ties = tiebreak_scores([f["text"] for f in finals])
    for row, tie in zip(finals, ties):
        row["tiebreak"] = tie["score"]
        row["tiebreak_detail"] = tie
        row["scores"] = None
        row["total"] = None

    scored: Optional[Dict[str, Dict[str, Any]]] = None
    judge_block: Optional[Dict[str, Any]] = None
    solutions: List[Dict[str, Any]] = []
    if finals and not _cancelled(cancel_event):
        order = list(range(len(finals)))
        if seed is not None:
            try:
                random.Random(f"{seed}|judge").shuffle(order)
            except Exception:  # noqa: BLE001
                order = list(range(len(finals)))
        names = [str(e.get("model") or "") for e in entries]
        solutions = [{"label": label_for(i), "entry": finals[j]["entry"],
                      "text": _scrub(finals[j]["text"], names)}
                     for i, j in enumerate(order)]
        judge_block = await _judge(prompt, solutions, judge, call, pool, on_event, events)
        scored = judge_block.get("scores") if judge_block else None

    by_label = {s["label"]: s["entry"] for s in solutions}
    graded = 0
    if scored:
        for label, row in scored.items():
            entry_id = by_label.get(label)
            for final in finals:
                if final["entry"] == entry_id:
                    final["scores"] = {a: row.get(a) for a in AXES}
                    final["total"] = row.get("total")
                    final["note"] = row.get("note") or ""
                    if final["total"] is not None:
                        graded += 1
                    break

    if graded and graded == len(finals):
        ranking, note = "judge", ""
    elif graded:
        ranking = "mixed"
        note = (f"the judge scored {graded} of {len(finals)} answers — the rest are ranked by "
                "the deterministic tiebreak (key-term coverage and answer length)")
    else:
        ranking = "deterministic"
        note = ("no judge available — ranked by a deterministic tiebreak (how much of the other "
                "answers' key terms each one covers, and its length among them)")
    finals.sort(key=lambda r: (0 if r.get("total") is not None else 1,
                               -_num(r.get("total")), -_num(r.get("tiebreak")),
                               int(r.get("entry") or 0)))
    for i, row in enumerate(finals, 1):
        row["rank"] = i
    result["final"] = finals
    result["judge"] = judge_block
    result["ranking"] = ranking
    result["ranking_note"] = note
    result["merge_prompt"] = synthesis_prompt(prompt, finals)
    if ranking != "judge":
        result["degraded"] = True
    await _emit(on_event, events, event="ranked", ranking=ranking,
                winner=(finals[0]["model"] if finals else None))


async def _judge(prompt: str, solutions: List[Dict[str, Any]], judge: str, call: Callable,
                 pool: Any, on_event: Optional[Callable],
                 events: Deque[Dict[str, Any]]) -> Dict[str, Any]:
    """One judging pass, retried ONCE on a malformed answer. Never invents a
    score: an unreadable judgement comes back with ``scores: None``."""
    labels = [s["label"] for s in solutions]
    block: Dict[str, Any] = {"model": judge, "ok": False, "attempts": 0,
                             "scores": None, "error": None, "raw": "",
                             "labels": {s["label"]: s["entry"] for s in solutions},
                             "axes": list(AXES)}
    await _emit(on_event, events, event="judge_start", model=judge, solutions=len(solutions))
    for attempt in (1, 2):
        block["attempts"] = attempt
        messages = build_judge_messages(prompt, solutions, retry=attempt > 1)
        try:
            async with _Gate(judge, pool):
                answer = call(messages, judge)
                if inspect.isawaitable(answer):
                    answer = await answer
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - an unavailable judge is not a failed run
            block["error"] = _squash(e, 300) or e.__class__.__name__
            logger.warning("tournament: the judge (%s) failed: %s", judge, e)
            break
        text, _ = _answer_of(answer)
        block["raw"] = _squash(text, JUDGE_RAW_CHARS)
        scores = parse_judgement(text, labels)
        if scores:
            block["ok"] = True
            block["scores"] = scores
            block["error"] = None
            missing = [x for x in labels if x not in scores]
            if missing:
                block["missing"] = missing
            break
        block["error"] = "the judge did not answer with the JSON the rubric asked for"
    await _emit(on_event, events, event="judge", model=judge, ok=block["ok"],
                attempts=block["attempts"], error=block["error"])
    return block


# ── the model function the app injects ──────────────────────────────────────

def default_llm_call(owner: Optional[str] = None, *, session_id: Optional[str] = None,
                     timeout: Optional[int] = None) -> Callable:
    """The real model function: each contestant resolved by name on the
    endpoints this owner can see. Injected rather than imported by ``run`` so
    the whole tournament stays testable without a model."""
    async def _call(messages: List[Dict[str, str]], model: str) -> str:
        from src.ai_interaction import AI_CHAT_TIMEOUT, _resolve_model
        from src.llm_core import llm_call_async
        url, resolved, headers = await asyncio.to_thread(
            _resolve_model, str(model or "auto"), owner=owner)
        return await llm_call_async(url, resolved, messages, headers=headers,
                                    timeout=timeout or AI_CHAT_TIMEOUT,
                                    session_id=session_id)
    return _call


# ── the background job, its JSON mirror and its rotation ────────────────────

_runs: Dict[str, "TournamentRun"] = {}
_lock = asyncio.Lock()
_loaded_all_at = 0.0


def _data_dir() -> str:
    try:
        from src.constants import DATA_DIR
        return os.path.join(DATA_DIR, "tournament")
    except Exception:  # pragma: no cover
        return os.path.join(os.getcwd(), "data", "tournament")


class TournamentRun:
    def __init__(self, owner: Optional[str], prompt: str, models: List[str], rounds: int,
                 judge_model: str, seed: Any = None):
        self.id = uuid.uuid4().hex[:12]
        self.owner = owner
        self.prompt = prompt
        self.models = models
        self.rounds = rounds
        self.judge_model = judge_model
        self.seed = seed
        self.created = time.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.status = "queued"      # queued|running|done|error|cancelling|cancelled|interrupted
        self.error: Optional[str] = None
        self.result: Dict[str, Any] = {}
        self.events: Deque[Dict[str, Any]] = deque(maxlen=EVENTS_KEPT)
        self.cancel_event = asyncio.Event()
        self.task: Optional[asyncio.Task] = None
        self._waiters: List[asyncio.Event] = []
        self._entered = False

    # ── views ───────────────────────────────────────────────────────────
    def to_dict(self, *, include_result: bool = True, brief: bool = False) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id, "owner": self.owner, "status": self.status, "error": self.error,
            "prompt": _squash(self.prompt, 200) if brief else self.prompt,
            "models": list(self.models), "rounds": self.rounds,
            "judge_model": self.judge_model, "seed": self.seed,
            "created": self.created, "started": self.started, "finished": self.finished,
            "duration_s": round((self.finished or time.time()) - (self.started or self.created), 1),
        }
        if include_result:
            d["result"] = self.result or {}
        return d

    def _notify(self) -> None:
        for ev in self._waiters:
            ev.set()
        self._waiters.clear()

    def _persist(self) -> None:
        try:
            d = _data_dir()
            os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, f".{self.id}.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(d, f"{self.id}.json"))
        except Exception as e:  # noqa: BLE001 - a mirror, never load-bearing
            logger.debug("tournament: persist failed: %s", e)


def summary(run_obj: TournamentRun) -> Dict[str, Any]:
    """What the page and a coordinating model read: the job plus its result,
    and, while it is still running, one progress row per model."""
    d = run_obj.to_dict()
    result = d.get("result") or {}
    if run_obj.status in _LIVE:
        seen: Dict[str, Dict[str, Any]] = {}
        for i, name in enumerate(run_obj.models):
            seen[f"{i}:{name}"] = {"entry": i, "model": name, "round": None,
                                   "state": "queued", "chars": 0}
        for ev in run_obj.events:
            entry = ev.get("entry")
            if entry is None:
                continue
            key = f"{entry}:{ev.get('model')}"
            row = seen.setdefault(key, {"entry": entry, "model": ev.get("model")})
            kind = str(ev.get("event") or "")
            if kind in ("model_start", "answer", "model_error", "model_cancelled"):
                row["state"] = {"model_start": "running", "answer": "answered",
                                "model_error": "error", "model_cancelled": "cancelled"}[kind]
                row["round"] = ev.get("round")
                if kind == "answer":
                    row["chars"] = ev.get("chars")
        d["progress"] = list(seen.values())
        d["wait_again"] = True
    d["events"] = list(run_obj.events)
    d["enabled"] = enabled()
    return d


async def start(owner: Optional[str], body: Dict[str, Any], *,
                runner: Optional[Callable] = None,
                llm_call: Optional[Callable] = None) -> TournamentRun:
    """Validate the request, create the run and launch it in the background."""
    if not isinstance(body, dict):
        raise TournamentError("the body must be a JSON object")
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise TournamentError("prompt is required")
    models = _clean_models(body.get("models"), max_models())
    try:
        rounds = int(body.get("rounds") or DEFAULT_ROUNDS)
    except (TypeError, ValueError):
        raise TournamentError("rounds must be an integer")
    rounds = max(1, min(MAX_ROUNDS, rounds))
    judge = str(body.get("judge_model") or "").strip() or strongest(models) or models[0]
    seed = body.get("seed")
    run_obj = TournamentRun(owner, prompt[:PROMPT_CHARS], models, rounds, judge, seed)
    async with _lock:
        _runs[run_obj.id] = run_obj
        run_obj._persist()
        _evict()
    call = llm_call or default_llm_call(owner)
    run_obj.task = asyncio.create_task((runner or _run_job)(run_obj, call))
    return run_obj


async def _run_job(run_obj: TournamentRun, llm_call: Callable) -> None:
    run_obj._entered = True
    run_obj.started = time.time()
    run_obj.status = "running"
    run_obj._persist()

    async def _sink(event: Dict[str, Any]) -> None:
        run_obj.events.append(dict(event))

    try:
        await run(run_obj.prompt, run_obj.models, rounds=run_obj.rounds,
                  judge_model=run_obj.judge_model, llm_call=llm_call, on_event=_sink,
                  seed=run_obj.seed, state=run_obj.result,
                  cancel_event=run_obj.cancel_event)
        if run_obj.cancel_event.is_set() or run_obj.status == "cancelling":
            run_obj.status = "cancelled"
        else:
            run_obj.status = "done"
    except asyncio.CancelledError:
        run_obj.status = "cancelled"
        raise
    except TournamentError as e:
        run_obj.status = "error"
        run_obj.error = str(e)[:500]
    except Exception as e:  # noqa: BLE001
        logger.exception("tournament %s failed", run_obj.id)
        run_obj.status = "error"
        run_obj.error = str(e)[:500]
    finally:
        run_obj.finished = time.time()
        if run_obj.result:
            run_obj.result["events"] = list(run_obj.events)
        run_obj._persist()
        run_obj._notify()


def get(run_id: str) -> Optional[TournamentRun]:
    found = _runs.get(str(run_id or ""))
    if found is not None:
        return found
    return _load(run_id)


def _load(run_id: str) -> Optional[TournamentRun]:
    if not re.fullmatch(r"[0-9a-f]{12}", str(run_id or "")):
        return None
    try:
        with open(os.path.join(_data_dir(), f"{run_id}.json"), encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    run_obj = TournamentRun(d.get("owner"), str(d.get("prompt") or ""),
                            [str(m) for m in (d.get("models") or [])],
                            int(d.get("rounds") or DEFAULT_ROUNDS),
                            str(d.get("judge_model") or ""), d.get("seed"))
    run_obj.id = d.get("id") or run_id
    run_obj.created = _num(d.get("created"))
    run_obj.started = d.get("started")
    run_obj.finished = d.get("finished")
    run_obj.error = d.get("error")
    run_obj.result = d.get("result") or {}
    for ev in (run_obj.result.get("events") or [])[-EVENTS_KEPT:]:
        if isinstance(ev, dict):
            run_obj.events.append(ev)
    status = str(d.get("status") or "done")
    run_obj.status = "interrupted" if status in _LIVE else status
    if run_obj.status == "interrupted" and not run_obj.error:
        run_obj.error = "interrupted by a restart of Faustus — run it again"
    _runs[run_obj.id] = run_obj
    return run_obj


def visible_to(run_obj: TournamentRun, owner: Optional[str]) -> bool:
    """A named owner sees only their own runs; single-user / anonymous mode
    (owner "" or None) sees everything."""
    if not owner:
        return True
    return run_obj.owner == owner


def list_runs(owner: Optional[str], limit: int = 50) -> List[Dict[str, Any]]:
    _load_all()
    rows = [r for r in _runs.values() if visible_to(r, owner)]
    rows.sort(key=lambda r: r.created, reverse=True)
    out = []
    for r in rows[:limit]:
        d = r.to_dict(include_result=False, brief=True)
        final = (r.result or {}).get("final") or []
        d["winner"] = (final[0] or {}).get("model") if final else None
        d["ranking"] = (r.result or {}).get("ranking")
        d["stopped_by"] = (r.result or {}).get("stopped_by")
        out.append(d)
    return out


def _load_all() -> None:
    global _loaded_all_at
    if time.time() - _loaded_all_at < 2.0:
        return
    _loaded_all_at = time.time()
    try:
        d = _data_dir()
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return
    missing = [n for n in names if n[:-5] not in _runs]
    if len(missing) > MAX_RUNS_KEPT:
        try:
            missing = [n for _, n in sorted(
                ((os.path.getmtime(os.path.join(d, n)), n) for n in missing), reverse=True)[:MAX_RUNS_KEPT]]
        except OSError:
            missing = missing[:MAX_RUNS_KEPT]
    for name in missing:
        _load(name[:-5])
    _evict_memory()


def _evict_memory() -> None:
    if len(_runs) <= MAX_RUNS_KEPT:
        return
    for old in sorted(_runs.values(), key=lambda r: r.created)[: len(_runs) - MAX_RUNS_KEPT]:
        if old.status not in _LIVE:
            _runs.pop(old.id, None)


def _evict() -> None:
    """Keep MAX_RUNS_KEPT finished runs, in memory AND on disk."""
    _evict_memory()
    try:
        d = _data_dir()
        names = [n for n in os.listdir(d) if n.endswith(".json")]
        if len(names) <= MAX_RUNS_KEPT:
            return
        paths = sorted((os.path.getmtime(os.path.join(d, n)), n) for n in names)
        for _, n in paths[: len(paths) - MAX_RUNS_KEPT]:
            live = _runs.get(n[:-5])
            if live is not None and live.status in _LIVE:
                continue
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass
    except OSError:
        pass


async def wait(run_obj: TournamentRun, timeout: float) -> bool:
    if run_obj.status not in _LIVE:
        return True
    ev = asyncio.Event()
    run_obj._waiters.append(ev)
    try:
        await asyncio.wait_for(ev.wait(), timeout=max(0.0, timeout))
        return True
    except asyncio.TimeoutError:
        return run_obj.status not in _LIVE
    finally:
        if ev in run_obj._waiters:
            run_obj._waiters.remove(ev)


def cancel(run_obj: TournamentRun) -> bool:
    """Stop the run. The cancel flag is set FIRST so the loop can end itself
    between calls and keep the partial result; the task is cancelled only if it
    never started."""
    if run_obj.status not in _LIVE:
        return False
    try:
        run_obj.cancel_event.set()
    except Exception:  # noqa: BLE001
        pass
    if run_obj.task is not None and not run_obj.task.done() and not run_obj._entered:
        run_obj.status = "cancelled"
        run_obj.finished = time.time()
        run_obj.task.cancel()
        run_obj._persist()
        run_obj._notify()
        return True
    run_obj.status = "cancelling"
    run_obj._persist()
    return True


def reset_for_tests() -> None:
    global _loaded_all_at
    _runs.clear()
    _MODEL_LOCKS.clear()
    _loaded_all_at = 0.0
