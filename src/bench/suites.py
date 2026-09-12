"""src/bench/suites.py — INF-04 A2: deterministic benchmark suites.

Three small suites (`config/benchmark_suites/*.json`) covering the product
objectives §09 names: `es_conversation` (interactive), `tools_json`
(coding_agent — strict-format JSON output, the shape a tool-using agent
turn needs), `long_documents` (long_documents — retrieval from a fact
placed at the start/middle/end of an embedded ~3-4k token document, per
§10's "no comparar un perfil con poco contexto... por atribuir hechos
situados en distintas partes del contexto").

Everything here is pure: `load_suite`/`list_suites` only read the small
JSON files shipped with the app (no network, no DB), and `run_checks` only
inspects the text it is given — this module never calls a model, and it is
the ONLY place a `CaseCheck.kind` is interpreted, so the runner never grows
a second, possibly-drifting copy of what "passed" means for a suite.

`language_es` is a heuristic (a stopword-ratio check), not a real language
identifier — documented at its definition, never presented as more certain
than it is (§10's own caution about a benchmark's numbers implying more
confidence than the sample actually supports).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from src.contracts.inference import BenchmarkCase

SUITES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "benchmark_suites",
)


class SuiteNotFound(ValueError):
    """No suite file exists for the requested id."""


# ── loading ──────────────────────────────────────────────────────────────────

def _suite_path(suite_id: str) -> str:
    # A bare filename component only — a suite id from a request must never
    # be able to walk `SUITES_DIR` with `../..`.
    safe = os.path.basename(str(suite_id or "").strip())
    if not safe or safe != str(suite_id or "").strip():
        raise SuiteNotFound(suite_id)
    return os.path.join(SUITES_DIR, f"{safe}.json")


def load_suite(suite_id: str) -> Dict[str, Any]:
    """`{"id", "version", "objective", "cases": (BenchmarkCase, ...)}` for
    one suite file. Raises `SuiteNotFound` for a missing file and whatever
    `BenchmarkCase.parse`/`json.JSONDecodeError` raises for a malformed
    one — a broken suite file must fail loudly, never fall back to an
    empty suite that silently "passes" every budget check."""
    path = _suite_path(suite_id)
    if not os.path.isfile(path):
        raise SuiteNotFound(suite_id)
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if str(raw.get("id") or "") != str(suite_id):
        raise ValueError(f"suite file {path} declares id {raw.get('id')!r}, expected {suite_id!r}")
    cases_raw = raw.get("cases") or []
    cases = tuple(
        BenchmarkCase.parse(c, f"suite[{suite_id}].cases[{i}]") for i, c in enumerate(cases_raw)
    )
    return {
        "id": raw["id"],
        "version": str(raw.get("version") or ""),
        "objective": str(raw.get("objective") or ""),
        "cases": cases,
    }


def list_suites() -> List[Dict[str, Any]]:
    """Every suite under `SUITES_DIR`, sorted by id. A suite file that fails
    to parse is skipped rather than crashing the whole listing — the same
    "one bad row must not hide every good one" rule `hardware_profiles.py`
    follows for its own JSON store."""
    if not os.path.isdir(SUITES_DIR):
        return []
    out = []
    for name in sorted(os.listdir(SUITES_DIR)):
        if not name.endswith(".json"):
            continue
        suite_id = name[:-len(".json")]
        try:
            out.append(load_suite(suite_id))
        except (SuiteNotFound, ValueError, OSError, json.JSONDecodeError):
            continue
    return out


# ── check kinds ──────────────────────────────────────────────────────────────

def _first_balanced_json(text: str) -> Optional[Any]:
    """The first balanced `{...}`/`[...]` span in `text`, parsed as JSON, or
    `None`. A model asked for "only JSON" sometimes still wraps it in a
    sentence or a code fence; this recovers the JSON value without ever
    guessing at malformed input — a bracket mismatch or a parse failure on
    the extracted span is `None`, not a best-effort partial value."""
    opens = {"{": "}", "[": "]"}
    start = None
    for i, ch in enumerate(text):
        if ch in opens:
            start = i
            break
    if start is None:
        return None
    stack: List[str] = []
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
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
            continue
        if ch in opens:
            stack.append(opens[ch])
        elif ch in ("}", "]"):
            if not stack or stack[-1] != ch:
                return None
            stack.pop()
            if not stack:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n(.*)\n```$", re.DOTALL)


def _parse_json_output(output: str) -> Optional[Any]:
    """The JSON value `output` carries, tolerating a surrounding code fence
    or a stray sentence — never a partial parse of malformed JSON."""
    candidate = (output or "").strip()
    fence = _FENCE_RE.match(candidate)
    if fence:
        candidate = fence.group(1).strip()
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        pass
    return _first_balanced_json(candidate)


#: Spanish function words frequent enough that their near-absence from a
#: reply is itself informative — deliberately small and closed-class
#: (articles, common prepositions/conjunctions/pronouns), never inferred
#: from a dictionary or model. This is a HEURISTIC ratio check, not a
#: language identifier: a very short reply, or one that is mostly proper
#: nouns/numbers, can score low despite being genuinely Spanish. Suites
#: that need certainty should pair it with a `contains`/`regex` check on
#: specific Spanish words, as `es_conversation`'s cases already do.
_ES_STOPWORDS = frozenset({
    "el", "la", "los", "las", "de", "del", "que", "y", "en", "un", "una", "unos", "unas",
    "es", "por", "con", "no", "se", "su", "sus", "al", "lo", "como", "mas", "más", "pero",
    "le", "les", "ya", "o", "u", "fue", "este", "esta", "estos", "estas", "ha", "han", "si",
    "sí", "porque", "son", "entre", "cuando", "muy", "sin", "sobre", "también", "me", "te",
    "nos", "hasta", "hay", "donde", "quien", "quienes", "desde", "todo", "toda", "todos",
    "todas", "uno", "una", "ni", "contra", "otros", "otras", "otro", "otra", "ese", "esa",
    "esos", "esas", "eso", "ante", "ellos", "ellas", "e", "esto", "mi", "mí", "antes",
    "algunos", "algunas", "qué", "cual", "cuales", "cuál", "cuáles", "yo", "él", "ella",
    "tanto", "mucho", "mucha", "muchos", "muchas", "poco", "poca", "pocos", "pocas",
    "nada", "algo", "nosotros", "nosotras", "vosotros", "vosotras", "usted", "ustedes",
})

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_ES_STOPWORD_RATIO_MIN = 0.15


def _looks_like_spanish(output: str) -> bool:
    words = [w.lower() for w in _WORD_RE.findall(output or "")]
    if len(words) < 3:
        return False
    hits = sum(1 for w in words if w in _ES_STOPWORDS)
    return (hits / len(words)) >= _ES_STOPWORD_RATIO_MIN


#: Substrings that indicate a tool/function-call artifact leaked into the
#: user-visible text instead of being handled by the harness — the model's
#: own special tokens or a raw tool-call envelope, never something a normal
#: prose reply would contain. Case-insensitive substring search: this is a
#: leak DETECTOR, not a parser, so it deliberately errs toward flagging
#: anything that looks like harness/tool plumbing.
_TOOL_LEAK_MARKERS = (
    "<tool_call>", "</tool_call>", "<|tool_call|>", "[tool_calls]",
    "<function", "</function>", "\"tool_calls\"", "'tool_calls'",
    "<|python_tag|>", "<|start_header_id|>", "<<sys>>",
)


def _no_tool_leak(output: str) -> bool:
    lowered = (output or "").lower()
    return not any(marker in lowered for marker in _TOOL_LEAK_MARKERS)


def _check_one(kind: str, arg: Any, output: str) -> bool:
    """One `CaseCheck` against raw text. Never raises for a malformed
    `arg`/`output` combination — an uncheckable check (e.g. `arg` not the
    shape a kind needs) counts as a FAILURE, not a pass by omission, since a
    check that silently could not run must never be mistaken for one that
    ran and succeeded."""
    text = output or ""
    if kind == "contains":
        return isinstance(arg, str) and arg in text
    if kind == "regex":
        if not isinstance(arg, str):
            return False
        try:
            return re.search(arg, text) is not None
        except re.error:
            return False
    if kind == "json_valid":
        return _parse_json_output(text) is not None
    if kind == "json_has_keys":
        if not isinstance(arg, list) or not arg:
            return False
        parsed = _parse_json_output(text)
        if not isinstance(parsed, dict):
            return False
        return all(str(key) in parsed for key in arg)
    if kind == "max_words":
        if not isinstance(arg, int) or isinstance(arg, bool):
            return False
        return len(text.split()) <= arg
    if kind == "language_es":
        return _looks_like_spanish(text)
    if kind == "no_tool_leak":
        return _no_tool_leak(text)
    return False  # pragma: no cover - CHECK_KINDS is exhaustive; belt and braces


def run_checks(case: BenchmarkCase, output: str) -> Dict[str, Any]:
    """`{"passed": bool, "failed_checks": [str, ...]}` for one sample's raw
    text against `case.checks`. A case with no checks at all `passed=True`
    trivially — nothing to fail — which is a deliberate property of the
    suite author's choice, not a gap this function papers over."""
    failed: List[str] = []
    for check in case.checks:
        if not _check_one(check.kind, check.arg, output):
            failed.append(check.kind)
    return {"passed": len(failed) == 0, "failed_checks": failed}
