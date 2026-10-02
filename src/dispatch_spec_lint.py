"""Spec lint for delegated jobs: is this task well enough specified to hand to
a local worker that cannot ask a question?

A worker that gets "improve the settings page" burns its rounds guessing what
is meant, touches whatever it finds, and cannot prove it finished. The jobs
that come back clean are the ones whose spec names the files, says what
"done" is, and has one outcome. :func:`validate_task_spec` checks exactly
that, from the text alone (no model call, no filesystem), and returns the
problems so the coordinator can add the missing detail BEFORE the job starts:

* ``no_files``          no file or folder is named or obviously scoped;
* ``no_verify``         no exact command that proves the outcome;
* ``multiple_outcomes`` more than one outcome in one task (split it);
* ``vague_verbs``       "improve" / "clean up" / ... with no criteria;
* ``public_api``        touches a public API with no "do not change" line.

Heuristics, on purpose: a problem is a question to the coordinator, not a
verdict. ``dispatch_spec_lint`` (``warn`` by default) attaches the problems to
the job and runs it; ``enforce`` refuses to start it and answers
``needs_detail``; ``off`` skips the check.

The checklist (name the files, give the exact verify command, one outcome, no
vague verbs) is adapted from the task-spec discipline of an MIT-licensed agent
farm (clodfarm, Duke Security, Inc.).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

MODES = ("off", "warn", "enforce")

_FILE_EXT = (
    "py|pyi|ts|tsx|js|jsx|mjs|cjs|json|jsonl|yml|yaml|toml|ini|cfg|md|mdx|txt|rst|css|scss|html|htm|vue|svelte|"
    "sh|ps1|bat|cmd|go|rs|java|kt|c|h|cpp|hpp|cs|rb|php|sql|csv|xml|env|lock|gradle|swift|dart|lua|ipynb"
)
_PATH_RE = re.compile(
    r"(?<![\w/\\.-])(?:[\w.-]+[\\/])+[\w.-]*(?![\w-])"                       # a/b/c.py, src/foo/
    r"|(?<![\w/\\.-])[\w-]+(?:\.[\w-]+)*\.(?:" + _FILE_EXT + r")\b",           # name.ext
    re.IGNORECASE)
_SYMBOL_RE = re.compile(
    r"`[A-Za-z_][\w.]*(?:\(\))?`|\b(?:function|class|method|module|component|endpoint|route)\s+`?[A-Za-z_][\w.]*`?",
    re.IGNORECASE)

_VERIFY_CMD_RE = re.compile(
    r"(?:^|[\s`'\"(])(?:"
    r"pytest|py\.test|python3?\s+-m\s+\w+|python3?\s+[\w./\\-]+\.py|npm\s+(?:run\s+)?\w+|npx\s+\w+|pnpm\s+(?:run\s+)?\w+|"
    r"yarn\s+\w+|node\s+[\w./\\-]+|cargo\s+(?:test|check|build|clippy)|go\s+(?:test|vet|build)|make(?:\s+\w+)?|"
    r"dotnet\s+test|mvn\s+\w+|gradlew?\s+\w+|tsc|eslint|ruff|mypy|flake8|jest|vitest|mocha|phpunit|rspec|"
    r"bash\s+[\w./\\-]+|sh\s+[\w./\\-]+|powershell\s+-\w+|curl\s+\S+"
    r")(?=[\s`'\").,;:]|$)", re.IGNORECASE)

_ACTION_VERBS = (
    "add|build|change|convert|create|delete|document|extend|extract|fix|implement|inline|introduce|merge|migrate|move|"
    "optimi[sz]e|patch|port|refactor|remove|rename|replace|rewrite|split|support|switch|update|upgrade|write|wire|expose|"
    "improve|clean|cleanup|simplify|tidy|enhance|polish|harden|reorgani[sz]e|restructure"
)
_VERIFY_VERBS = {"run", "verify", "check", "ensure", "confirm", "test", "assert", "make"}
_VAGUE_VERBS = ("improve upon", "improve", "clean up", "cleanup", "clean", "tidy up", "tidy", "polish", "enhance",
                "optimize", "optimise", "simplify", "refine", "make it better", "make better", "fix up", "revamp",
                "modernize", "modernise", "refactor", "review", "look at", "look into", "take care of", "sort out")
_VAGUE_RE = re.compile(r"\b(" + "|".join(re.escape(v).replace(r"\ ", r"\s+") for v in _VAGUE_VERBS) + r")\b",
                       re.IGNORECASE)
_CRITERIA_CUE_RE = re.compile(
    r"\b(?:must|should|shall|so that|such that|until|no longer|returns?|raises?|throws?|outputs?|prints?|exits?|"
    r"expected|assert|equals?|passes?|fails?|at least|at most|less than|more than|exactly|only|never|always|"
    r"all tests|every|each)\b|\d|`[^`]+`", re.IGNORECASE)
_API_RE = re.compile(
    r"\bpublic\s+(?:api|interface|method|function|class|contract)\b|\bapi\b|\bendpoints?\b|\broutes?\b|"
    r"\bsignatures?\b|\bexported?\b|\b__all__\b|\bschema\b|\bcontract\b|\bcli\s+(?:flags?|options?|arguments?)\b|"
    r"\bbreaking\b|\bresponse\s+(?:fields?|shape|format)\b|\bjson\s+shape\b|\bwire\s+format\b", re.IGNORECASE)
_GUARD_RE = re.compile(
    r"\b(?:do\s*n[o']?t|don'?t|must\s+not|should\s+not|shall\s+not|never|without)\s+(?:change|modify|touch|alter|break|"
    r"rename|remove|edit|rewrite|update)\b|\bleave\b[^.\n]{0,60}\b(?:as\s+is|unchanged|alone|untouched)\b|"
    r"\bkeep\b[^.\n]{0,60}\b(?:unchanged|as\s+is|stable|compatible|the\s+same)\b|\bbackwards?[- ]compatible\b|"
    r"\bno\s+breaking\b|\bunchanged\b|\bnot\s+change\b", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^\s*(?:\d+[.)]|[-*\u2022])\s+(.+)$", re.MULTILINE)
_CLAUSE_SPLIT_RE = re.compile(
    r"(?:;|\.\s+|\n+|\s+and\s+then\s+|\s+then\s+|,\s+then\s+|\s+after\s+that\s+|\s+also\s+|"
    r",\s+and\s+(?=(?:" + _ACTION_VERBS + r")\b))", re.IGNORECASE)
_LEAD_RE = re.compile(r"^\W*(?:then|also|and|next|finally|first|second|third|please)?\W*(" + _ACTION_VERBS + r")\b",
                      re.IGNORECASE)

def _text_of(task: Any) -> str:
    if isinstance(task, str):
        return task
    if isinstance(task, Mapping):
        parts = [str(task.get(k) or "") for k in ("instruction", "task", "content", "description")]
        return "\n".join(p for p in parts if p)
    return str(task or "")


def _files_of(task: Any) -> List[str]:
    if isinstance(task, Mapping):
        raw = task.get("files") or task.get("owns") or []
        if isinstance(raw, str):
            raw = [p for p in re.split(r"[,\s]+", raw) if p]
        if isinstance(raw, (list, tuple)):
            return [str(p).strip() for p in raw if str(p).strip()]
    return []


def _criteria_of(task: Any, job: Optional[Mapping[str, Any]]) -> List[str]:
    out: List[str] = []
    for src in (task, job):
        if isinstance(src, Mapping):
            raw = src.get("criteria") or src.get("success_criteria") or src.get("acceptance")
            if isinstance(raw, str):
                raw = [raw]
            if isinstance(raw, (list, tuple)):
                out.extend(str(c).strip() for c in raw if str(c).strip())
    return out


def _problem(code: str, message: str, fix: str) -> Dict[str, str]:
    return {"code": code, "message": message, "fix": fix}


def _has_path(text: str) -> bool:
    for m in _PATH_RE.finditer(text):
        tok = m.group(0)
        if "://" in tok or re.fullmatch(r"\d+(?:\.\d+)+", tok):
            continue
        if re.fullmatch(r"(?:e\.g|i\.e|etc|vs)\.?", tok, re.IGNORECASE):
            continue
        return True
    return False


def _verify_command_of(task: Any, job: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The exact verify command the spec carries, if it does."""
    for src in (task, job):
        if isinstance(src, Mapping):
            for key in ("verify_command", "verify_cmd", "test_command"):
                if str(src.get(key) or "").strip():
                    return str(src[key]).strip()
            raw = src.get("verify")
            if isinstance(raw, str) and raw.strip().lower() not in ("", "auto", "none", "off", "false", "true"):
                return raw.strip()
    text = _text_of(task) + "\n" + "\n".join(_criteria_of(task, job))
    m = _VERIFY_CMD_RE.search(text)
    return m.group(0).strip(" `'\"(") if m else None


def _outcomes(text: str) -> Tuple[List[str], bool]:
    """The change-verbs the text asks for, one per clause, and whether they
    came from a list (every list item is its own outcome)."""
    items = [m.group(1).strip() for m in _LIST_ITEM_RE.finditer(text)]
    from_list = len(items) >= 2
    clauses = items if from_list else [c for c in (s.strip() for s in _CLAUSE_SPLIT_RE.split(text)) if c]
    verbs: List[str] = []
    for clause in clauses:
        m = _LEAD_RE.match(clause)
        if m and m.group(1).lower() not in _VERIFY_VERBS:
            verbs.append(m.group(1).lower())
    return verbs, from_list


def validate_task_spec(task: Any, *, job: Optional[Mapping[str, Any]] = None) -> List[Dict[str, str]]:
    """The problems with one task's spec, ``[]`` when it is ready to run.

    ``task`` is an instruction string or a task dict (``instruction``,
    ``files``, ``criteria``, ...). ``job`` is the request body around it: its
    ``verify`` command and ``criteria`` count for every task in it. Each
    problem is ``{"code", "message", "fix"}``.
    """
    text = _text_of(task).strip()
    if not text:
        return [_problem("empty", "the task has no instruction",
                         "write what to do, in which files, and how to prove it")]
    problems: List[Dict[str, str]] = []
    files = _files_of(task)
    criteria = _criteria_of(task, job)

    # 1. files named or obviously scoped
    if not files and not _has_path(text) and not _SYMBOL_RE.search(text):
        problems.append(_problem(
            "no_files", "no file or folder is named or obviously scoped",
            "list the files the worker may touch (`files: [...]`) or name them in the instruction"))

    # 2. an exact verify command
    if _verify_command_of(task, job) is None:
        raw = job.get("verify") if isinstance(job, Mapping) else None
        off = raw is False or (isinstance(raw, str) and raw.strip().lower() in ("none", "off", "false"))
        problems.append(_problem(
            "no_verify",
            "verification is off and the spec names no command that proves the outcome" if off
            else "no exact verify command (the job would rely on auto-detection)",
            "give the command that proves it, e.g. `verify: \"pytest tests/test_x.py -q\"`"))

    # 3. one outcome
    verbs, from_list = _outcomes(text)
    if len(verbs) >= 2 and (from_list or len(set(verbs)) >= 2):
        shown = ", ".join(dict.fromkeys(verbs).keys())
        problems.append(_problem(
            "multiple_outcomes", f"more than one outcome in one task ({shown})",
            "split it: one task, one outcome, one thing to verify"))

    # 4. vague verbs without criteria
    vague = sorted({re.sub(r"\s+", " ", m.group(1).lower()) for m in _VAGUE_RE.finditer(text)})
    cue_text = re.sub(r"(?m)^\s*\d+[.)]\s+", "", _VAGUE_RE.sub(" ", text))   # list numbers are not criteria
    if vague and not criteria and not _CRITERIA_CUE_RE.search(cue_text):
        problems.append(_problem(
            "vague_verbs", f"vague verb ({', '.join(vague[:3])}) with no criteria",
            "say what must be true afterwards (`criteria: [...]`): a behaviour, a number, a failing test that passes"))

    # 5. public API without a do-not-change line
    if _API_RE.search(text) and not _GUARD_RE.search(text) and not any(_GUARD_RE.search(c) for c in criteria):
        problems.append(_problem(
            "public_api", "touches a public API with no \"do not change\" line",
            "add what must stay as it is, e.g. \"do not change the route paths or response fields\""))
    return problems


def lint_tasks(tasks: Sequence[Any], *, job: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Lint a whole job: per-task problems plus the flat ``needs_detail`` list a
    coordinator can act on."""
    rows: List[Dict[str, Any]] = []
    needs: List[str] = []
    for i, task in enumerate(tasks or [], 1):
        problems = validate_task_spec(task, job=job)
        name = str(task.get("name") or "").strip() if isinstance(task, Mapping) else ""
        rows.append({"index": i, "name": name or f"task {i}", "problems": problems})
        for p in problems:
            needs.append(f"task {i}{(' (' + name + ')') if name else ''}: {p['message']} - {p['fix']}")
    codes = sorted({p["code"] for r in rows for p in r["problems"]})
    return {"ok": not needs, "tasks": rows, "needs_detail": needs, "codes": codes}


def mode(raw: Any) -> str:
    """``off`` / ``warn`` / ``enforce`` from the ``dispatch_spec_lint`` setting."""
    value = str(raw if raw is not None else "warn").strip().lower()
    if value in ("1", "true", "on", "yes"):
        return "warn"
    if value in ("0", "false", "no", "none"):
        return "off"
    return value if value in MODES else "warn"