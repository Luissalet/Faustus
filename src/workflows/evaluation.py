"""
workflows/evaluation.py — does a saved workflow still do what it should?

A workflow with a model in it changes behaviour when the model, a prompt or a
threshold changes, and nobody notices until a wrong answer ships. An
*evaluation* is the cheap way to notice: keep a **set of cases** per saved
workflow (inputs, plus what a good output looks like), run the workflow against
each, **score** what came back, and keep the **report**.

**Two modes, and the safe one is the default.**

* `simulate` — every case goes through `dry_run.py`: no model, no skill, no
  sender. It proves the wiring: inputs satisfy the declared schema, templates
  resolve, branches and loops behave, placeholders satisfy the schemas the
  nodes promise. A case can `mock` a node (say, "the classifier answers
  billing") to drive a particular path. It cannot say whether a real model
  answers well, and the report says `mode: simulate` so nobody reads it as if
  it could.
* `real` — every case starts a real run of the saved definition and waits for
  it. Real runs reach models, skills and senders, so this needs the caller to
  say so twice (`mode: "real"` *and* `allow_real: true`), a case that stops to
  wait for a person ends as `error` (an evaluation does not answer approval
  cards), and there is a smaller ceiling on cases per run.

**Scorers** are small and deterministic: `exact`, `contains`, `regex`,
`json_schema`, `numeric` (a tolerance, absolute or relative). The one
non-deterministic scorer, `judge` (a model grades the output against written
criteria), is behind the `workflow_eval_model_judge` setting, off by default;
with it off a `judge` scorer reports itself unavailable and the case fails
rather than passing on nothing. A scorer reads a **path** into the run's
outputs — `{node_id: result}` for the nodes nothing else consumes — such as
`reply.data.total`; no path scores the whole object.

A case passes when the run completed and every scorer passed. A case with no
scorers passes when the run completed: that is a smoke test, and the report
marks it as one.

Everything is owner-scoped, like the library it hangs off.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.contracts import WorkflowDefinition
from src.contracts.base import now_iso

from . import schema_check
from .dry_run import dry_run
from .library import LibraryError, WorkflowLibrary

logger = logging.getLogger(__name__)

__all__ = ["EvaluationError", "SCORER_TYPES", "MAX_CASES", "MAX_REAL_CASES", "JUDGE_SETTING",
           "validate_set", "score_case", "aggregate", "EvaluationStore", "run_evaluation",
           "start_evaluation", "dig"]

SCORER_TYPES = ("exact", "contains", "regex", "json_schema", "numeric", "judge")
MAX_CASES = 100
MAX_REAL_CASES = 20
MAX_SETS_PER_WORKFLOW = 20
MAX_SCORERS_PER_CASE = 10
JUDGE_SETTING = "workflow_eval_model_judge"
DEFAULT_CASE_TIMEOUT_S = 120.0
MAX_CASE_TIMEOUT_S = 900.0
_MAX_OUTPUT_CHARS = 6000
_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
_SET_NAME = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,47}$")


class EvaluationError(ValueError):
    """The request cannot be honoured; the message says what to change.
    `problems` lists every individual thing wrong when there are several."""

    def __init__(self, message: str, problems: Optional[List[str]] = None, code: str = "bad_request"):
        super().__init__(message)
        self.problems = list(problems or [])
        self.code = code


# ── reading a path out of the outputs ─────────────────────────────────────

_MISSING = object()


def dig(value: Any, path: str) -> Any:
    """`a.b.0.c` into nested dicts and lists; `_MISSING` when any step is absent."""
    if not path:
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, (list, tuple)) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


# ── validating what a person saved ────────────────────────────────────────

def _scorer_problems(spec: Any, where: str) -> List[str]:
    if not isinstance(spec, Mapping):
        return [f"{where}: a scorer is an object with a `type`"]
    kind = spec.get("type")
    if kind not in SCORER_TYPES:
        return [f"{where}: `type` must be one of {list(SCORER_TYPES)}"]
    out: List[str] = []
    path = spec.get("path", "")
    if not isinstance(path, str) or len(path) > 200:
        out.append(f"{where}: `path` must be text of at most 200 characters")
    known = {"type", "path", "expected", "case_sensitive", "pattern", "flags", "schema", "tolerance",
             "relative", "criteria", "threshold", "id"}
    unknown = sorted(set(spec) - known)
    if unknown:
        out.append(f"{where}: unknown field(s) {unknown}")
    if kind in ("exact", "contains", "numeric") and "expected" not in spec:
        out.append(f"{where}: a '{kind}' scorer needs `expected`")
    if kind == "numeric":
        expected = spec.get("expected")
        if isinstance(expected, bool) or not isinstance(expected, (int, float)) or not math.isfinite(expected):
            out.append(f"{where}: `expected` must be a number")
        tol = spec.get("tolerance", 0)
        if isinstance(tol, bool) or not isinstance(tol, (int, float)) or not math.isfinite(tol) or tol < 0:
            out.append(f"{where}: `tolerance` must be a number of at least 0")
    if kind == "regex":
        pattern = spec.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            out.append(f"{where}: a 'regex' scorer needs `pattern`")
        else:
            try:
                re.compile(pattern, _flags(spec))
            except (re.error, ValueError) as exc:
                out.append(f"{where}: `pattern` is not a valid regular expression ({exc})")
    if kind == "json_schema":
        problems = schema_check.schema_problems(spec.get("schema"))
        out.extend(f"{where}: `schema`: {p}" for p in problems[:3])
    if kind == "judge":
        criteria = spec.get("criteria")
        if not isinstance(criteria, str) or not criteria.strip() or len(criteria) > 2000:
            out.append(f"{where}: a 'judge' scorer needs `criteria` (text of at most 2000 characters)")
        threshold = spec.get("threshold", 0.7)
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
            out.append(f"{where}: `threshold` must be a number from 0 to 1")
    return out


_FLAG_BITS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}


def _flags(spec: Mapping[str, Any]) -> int:
    flags = 0
    for ch in str(spec.get("flags") or ""):
        if ch not in _FLAG_BITS:
            raise ValueError(f"unknown regex flag {ch!r} (use i, m, s)")
        flags |= _FLAG_BITS[ch]
    return flags


def validate_set(payload: Any, definition: Optional[WorkflowDefinition] = None) -> Dict[str, Any]:
    """The normalised `{scorers, cases}` of a set, or :class:`EvaluationError`
    naming every problem at once."""
    if not isinstance(payload, Mapping):
        raise EvaluationError("a set is an object with `cases` (and optionally `scorers`)")
    unknown = sorted(set(payload) - {"cases", "scorers", "name", "description"})
    problems: List[str] = [f"unknown field(s) {unknown}"] if unknown else []
    defaults = payload.get("scorers") or []
    if not isinstance(defaults, list) or len(defaults) > MAX_SCORERS_PER_CASE:
        problems.append(f"`scorers` must be a list of at most {MAX_SCORERS_PER_CASE}")
        defaults = []
    for i, spec in enumerate(defaults):
        problems.extend(_scorer_problems(spec, f"scorers[{i}]"))
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvaluationError("a set needs at least one case", problems)
    if len(cases) > MAX_CASES:
        raise EvaluationError(f"a set holds at most {MAX_CASES} cases", problems)
    node_ids = {n.id for n in definition.nodes} if definition is not None else None
    seen: set = set()
    clean: List[Dict[str, Any]] = []
    for i, case in enumerate(cases):
        where = f"cases[{i}]"
        if not isinstance(case, Mapping):
            problems.append(f"{where}: a case is an object")
            continue
        extra = sorted(set(case) - {"id", "name", "inputs", "mocks", "scorers"})
        if extra:
            problems.append(f"{where}: unknown field(s) {extra}")
        cid = str(case.get("id") or f"case-{i + 1}")
        if not _CASE_ID.match(cid):
            problems.append(f"{where}: `id` {cid!r} is not usable (letters, digits, `_.-`, at most 64)")
        if cid in seen:
            problems.append(f"{where}: `id` {cid!r} is used twice")
        seen.add(cid)
        inputs = case.get("inputs", {})
        if not isinstance(inputs, Mapping):
            problems.append(f"{where}: `inputs` must be an object")
            inputs = {}
        mocks = case.get("mocks", {})
        if not isinstance(mocks, Mapping) or any(not isinstance(v, Mapping) for v in mocks.values()):
            problems.append(f"{where}: `mocks` must be an object of {{node_id: {{...}}}}")
            mocks = {}
        elif node_ids is not None:
            for node in mocks:
                if node not in node_ids:
                    problems.append(f"{where}: mock for {node!r}, which this workflow has no node named")
        scorers = case.get("scorers")
        if scorers is None:
            scorers = []
        elif not isinstance(scorers, list) or len(scorers) > MAX_SCORERS_PER_CASE:
            problems.append(f"{where}: `scorers` must be a list of at most {MAX_SCORERS_PER_CASE}")
            scorers = []
        for j, spec in enumerate(scorers):
            problems.extend(_scorer_problems(spec, f"{where}.scorers[{j}]"))
        name = str(case.get("name") or "")[:120]
        clean.append({"id": cid, "name": name, "inputs": dict(inputs), "mocks": {k: dict(v) for k, v in mocks.items()},
                      "scorers": [dict(s) for s in scorers if isinstance(s, Mapping)]})
    if problems:
        raise EvaluationError("the set was refused: " + " | ".join(problems[:6]), problems)
    return {"scorers": [dict(s) for s in defaults], "cases": clean}


# ── scoring ───────────────────────────────────────────────────────────────

def _judge(value: Any, spec: Mapping[str, Any], models: Any, owner: str,
           judge_enabled: bool) -> Dict[str, Any]:
    if not judge_enabled:
        return {"passed": False, "unavailable": True,
                "detail": f"the model judge is off; turn on the `{JUDGE_SETTING}` setting to use a 'judge' scorer"}
    if models is None:
        return {"passed": False, "unavailable": True, "detail": "no model is wired to grade this output"}
    threshold = float(spec.get("threshold", 0.7))
    prompt = ("Criteria:\n" + str(spec["criteria"]).strip() + "\n\nOutput to grade:\n\"\"\"\n"
              + _as_text(value)[:_MAX_OUTPUT_CHARS] + "\n\"\"\"")
    try:
        reply = models.complete(
            [{"role": "system", "content": "You grade an output against written criteria. Reply with ONE JSON "
                                           "object {\"score\": a number from 0 (fails) to 1 (fully meets), "
                                           "\"reason\": one short sentence} and nothing else."},
             {"role": "user", "content": prompt}],
            owner=owner, purpose="utility", timeout_s=60.0, max_tokens=300, temperature=0.0)
    except Exception as exc:  # noqa: BLE001 - a judge that could not answer is not a pass
        return {"passed": False, "unavailable": True, "detail": f"the judge could not answer: {exc}"}
    parsed, why = schema_check.extract_json(reply)
    score = parsed.get("score") if isinstance(parsed, Mapping) else None
    if why or isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        return {"passed": False, "unavailable": True, "detail": "the judge's reply had no usable score"}
    score = min(1.0, max(0.0, float(score)))
    return {"passed": score >= threshold, "score": round(score, 3), "threshold": threshold,
            "detail": str(parsed.get("reason") or "")[:300]}


def score_case(outputs: Mapping[str, Any], scorers: Sequence[Mapping[str, Any]], *, models: Any = None,
               owner: str = "", judge_enabled: bool = False) -> List[Dict[str, Any]]:
    """One verdict per scorer: `{type, path, passed, detail, ...}`. Never raises:
    a scorer that cannot be evaluated says why and fails."""
    results: List[Dict[str, Any]] = []
    for index, spec in enumerate(scorers):
        kind = spec.get("type")
        path = str(spec.get("path") or "")
        head = {"id": str(spec.get("id") or f"{kind}-{index + 1}"), "type": kind, "path": path}
        value = dig(outputs, path)
        if value is _MISSING:
            results.append({**head, "passed": False, "detail": f"nothing at `{path}` in the outputs"})
            continue
        try:
            results.append({**head, **_apply(kind, value, spec, models, owner, judge_enabled)})
        except Exception as exc:  # noqa: BLE001 - see the docstring
            results.append({**head, "passed": False, "detail": f"the scorer failed: {type(exc).__name__}: {exc}"})
    return results


def _apply(kind: str, value: Any, spec: Mapping[str, Any], models: Any, owner: str,
           judge_enabled: bool) -> Dict[str, Any]:
    if kind == "exact":
        expected = spec["expected"]
        ok = value == expected
        return {"passed": ok, "detail": "equal" if ok else f"expected {_short(expected)}, got {_short(value)}"}
    if kind == "contains":
        expected = spec["expected"]
        if isinstance(value, (list, tuple)):
            ok = expected in value
        elif isinstance(value, Mapping) and isinstance(expected, str) and expected in value:
            ok = True                                   # a key of the object
        else:
            needle, hay = _as_text(expected), _as_text(value)
            if not spec.get("case_sensitive", False):
                needle, hay = needle.lower(), hay.lower()
            ok = needle in hay
        return {"passed": ok, "detail": "found" if ok else f"{_short(expected)} is not in {_short(value)}"}
    if kind == "regex":
        ok = re.search(str(spec["pattern"]), _as_text(value), _flags(spec)) is not None
        return {"passed": ok, "detail": "matches" if ok else f"{_short(value)} does not match {spec['pattern']!r}"}
    if kind == "json_schema":
        candidate = value
        if isinstance(value, str):
            parsed, why = schema_check.extract_json(value)
            if why:
                return {"passed": False, "detail": f"not JSON: {why}"}
            candidate = parsed
        issues = schema_check.validate(candidate, spec["schema"])
        return {"passed": not issues, "detail": "valid" if not issues else "; ".join(issues[:3])}
    if kind == "numeric":
        number = value
        if isinstance(value, str):
            try:
                number = float(value.strip())
            except ValueError:
                return {"passed": False, "detail": f"{_short(value)} is not a number"}
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            return {"passed": False, "detail": f"{_short(value)} is not a number"}
        expected = float(spec["expected"])
        tolerance = float(spec.get("tolerance", 0))
        allowed = abs(expected) * tolerance if spec.get("relative") else tolerance
        gap = abs(float(number) - expected)
        return {"passed": gap <= allowed + 1e-12,
                "detail": f"{number} is {gap:g} from {expected:g} (allowed {allowed:g})"}
    if kind == "judge":
        return _judge(value, spec, models, owner, judge_enabled)
    return {"passed": False, "detail": f"unknown scorer {kind!r}"}


def _short(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


# ── the report ────────────────────────────────────────────────────────────

def aggregate(results: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Pass rate, counts, duration, and pass rate per scorer type."""
    total = len(results)
    counts = {"passed": 0, "failed": 0, "error": 0}
    for r in results:
        counts[r["status"] if r["status"] in counts else "error"] += 1
    durations = sorted(int(r.get("duration_ms") or 0) for r in results)
    by_scorer: Dict[str, Dict[str, int]] = {}
    for r in results:
        for s in r.get("scores") or []:
            slot = by_scorer.setdefault(str(s.get("type")), {"passed": 0, "total": 0})
            slot["total"] += 1
            slot["passed"] += 1 if s.get("passed") else 0
    p95 = durations[min(len(durations) - 1, int(math.ceil(0.95 * len(durations))) - 1)] if durations else 0
    return {"total": total, **counts,
            "pass_rate": round(counts["passed"] / total, 4) if total else 0.0,
            "duration_ms_total": sum(durations),
            "duration_ms_avg": int(sum(durations) / total) if total else 0,
            "duration_ms_p95": p95,
            "unscored": sum(1 for r in results if not r.get("scores")),
            "by_scorer": by_scorer}


# ── persistence ───────────────────────────────────────────────────────────

class EvaluationStore:
    """Sets and reports, owner-scoped rows in and plain dicts out."""

    def save_set(self, owner: str, workflow_name: str, name: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        from core.database import SessionLocal, WorkflowEvalSetRow
        if not _SET_NAME.match(name or ""):
            raise EvaluationError(f"{name!r} is not a usable set name: lower-case letters, digits, `_` and `-`, "
                                  "at most 48 characters")
        db = SessionLocal()
        try:
            row = (db.query(WorkflowEvalSetRow)
                   .filter(WorkflowEvalSetRow.owner == owner, WorkflowEvalSetRow.workflow_name == workflow_name,
                           WorkflowEvalSetRow.name == name).first())
            if row is None:
                count = (db.query(WorkflowEvalSetRow).filter(WorkflowEvalSetRow.owner == owner,
                                                             WorkflowEvalSetRow.workflow_name == workflow_name).count())
                if count >= MAX_SETS_PER_WORKFLOW:
                    raise EvaluationError(f"a workflow keeps at most {MAX_SETS_PER_WORKFLOW} sets; delete one first")
                row = WorkflowEvalSetRow(id=f"wes_{uuid.uuid4().hex[:20]}", owner=owner,
                                         workflow_name=workflow_name, name=name, schema_version=1)
                db.add(row)
            row.cases_json = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
            db.commit()
            db.refresh(row)
            return self._set_view(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _set_view(row: Any, *, full: bool = True) -> Dict[str, Any]:
        body = json.loads(row.cases_json or "{}")
        out = {"id": row.id, "name": row.name, "workflow": row.workflow_name,
               "cases": len(body.get("cases") or []), "scorers": len(body.get("scorers") or []),
               "updated_at": row.updated_at.isoformat() + "Z" if getattr(row, "updated_at", None) else ""}
        if full:
            out["set"] = body
        return out

    def get_set(self, owner: str, workflow_name: str, name: str) -> Optional[Dict[str, Any]]:
        from core.database import SessionLocal, WorkflowEvalSetRow
        db = SessionLocal()
        try:
            row = (db.query(WorkflowEvalSetRow)
                   .filter(WorkflowEvalSetRow.owner == owner, WorkflowEvalSetRow.workflow_name == workflow_name,
                           WorkflowEvalSetRow.name == name).first())
            return self._set_view(row) if row else None
        finally:
            db.close()

    def list_sets(self, owner: str, workflow_name: str) -> List[Dict[str, Any]]:
        from core.database import SessionLocal, WorkflowEvalSetRow
        db = SessionLocal()
        try:
            rows = (db.query(WorkflowEvalSetRow)
                    .filter(WorkflowEvalSetRow.owner == owner, WorkflowEvalSetRow.workflow_name == workflow_name)
                    .order_by(WorkflowEvalSetRow.name.asc()).all())
            return [self._set_view(r, full=False) for r in rows]
        finally:
            db.close()

    def delete_set(self, owner: str, workflow_name: str, name: str) -> bool:
        from core.database import SessionLocal, WorkflowEvalSetRow
        db = SessionLocal()
        try:
            n = (db.query(WorkflowEvalSetRow)
                 .filter(WorkflowEvalSetRow.owner == owner, WorkflowEvalSetRow.workflow_name == workflow_name,
                         WorkflowEvalSetRow.name == name).delete(synchronize_session=False))
            db.commit()
            return bool(n)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def delete_sets_of(self, owner: str, workflow_name: str) -> int:
        from core.database import SessionLocal, WorkflowEvalSetRow
        db = SessionLocal()
        try:
            n = (db.query(WorkflowEvalSetRow)
                 .filter(WorkflowEvalSetRow.owner == owner, WorkflowEvalSetRow.workflow_name == workflow_name)
                 .delete(synchronize_session=False))
            db.commit()
            return int(n)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # reports

    def open_report(self, owner: str, workflow_name: str, set_row: Mapping[str, Any], mode: str) -> str:
        from core.database import SessionLocal, WorkflowEvalReportRow
        db = SessionLocal()
        try:
            rid = f"wer_{uuid.uuid4().hex[:20]}"
            db.add(WorkflowEvalReportRow(
                id=rid, owner=owner, workflow_name=workflow_name, set_id=set_row.get("id"),
                set_name=set_row.get("name"), status="running", started_at=now_iso(), schema_version=1,
                summary_json=json.dumps({"mode": mode}), report_json=None))
            db.commit()
            return rid
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def close_report(self, report_id: str, status: str, summary: Mapping[str, Any],
                     cases: Sequence[Mapping[str, Any]]) -> None:
        from core.database import SessionLocal, WorkflowEvalReportRow
        db = SessionLocal()
        try:
            row = db.get(WorkflowEvalReportRow, report_id)
            if row is None:
                return
            row.status = status
            row.ended_at = now_iso()
            row.summary_json = json.dumps(dict(summary), ensure_ascii=False, sort_keys=True, default=str)
            row.report_json = json.dumps({"cases": list(cases)}, ensure_ascii=False, sort_keys=True, default=str)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _report_view(row: Any, *, full: bool) -> Dict[str, Any]:
        summary = json.loads(row.summary_json or "{}")
        out = {"id": row.id, "workflow": row.workflow_name, "set": row.set_name or "", "status": row.status,
               "started_at": row.started_at or "", "ended_at": row.ended_at or "", "summary": summary}
        if full:
            out["cases"] = (json.loads(row.report_json) if row.report_json else {}).get("cases", [])
        return out

    def get_report(self, owner: str, report_id: str) -> Optional[Dict[str, Any]]:
        from core.database import SessionLocal, WorkflowEvalReportRow
        db = SessionLocal()
        try:
            row = db.get(WorkflowEvalReportRow, report_id)
            if row is None or row.owner != owner:
                return None
            return self._report_view(row, full=True)
        finally:
            db.close()

    def list_reports(self, owner: str, workflow_name: str, limit: int = 20) -> List[Dict[str, Any]]:
        from core.database import SessionLocal, WorkflowEvalReportRow
        db = SessionLocal()
        try:
            rows = (db.query(WorkflowEvalReportRow)
                    .filter(WorkflowEvalReportRow.owner == owner, WorkflowEvalReportRow.workflow_name == workflow_name)
                    .order_by(WorkflowEvalReportRow.started_at.desc()).limit(max(1, min(100, limit))).all())
            return [self._report_view(r, full=False) for r in rows]
        finally:
            db.close()

    def fail_stale(self, owner: str) -> int:
        """Reports left `running` by a process that died: they will never
        finish, and a list that says "running" forever is a lie."""
        from core.database import SessionLocal, WorkflowEvalReportRow
        db = SessionLocal()
        try:
            n = (db.query(WorkflowEvalReportRow)
                 .filter(WorkflowEvalReportRow.owner == owner, WorkflowEvalReportRow.status == "running",
                         WorkflowEvalReportRow.id.notin_(list(_ACTIVE)))
                 .update({"status": "interrupted", "ended_at": now_iso()}, synchronize_session=False))
            db.commit()
            return int(n)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


_ACTIVE: set = set()
_ACTIVE_LOCK = threading.Lock()


# ── running ───────────────────────────────────────────────────────────────

def judge_is_enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting(JUDGE_SETTING, False))
    except Exception:  # noqa: BLE001 - settings unreadable: the conservative answer is off
        return False


def _with_defaults(schema: Mapping[str, Any], inputs: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(inputs)
    for key, spec in (schema.get("properties") or {}).items():
        if key not in merged and isinstance(spec, Mapping) and "default" in spec:
            merged[key] = spec["default"]
    return merged


def _run_one(case: Mapping[str, Any], definition: WorkflowDefinition, *, mode: str, owner: str,
             set_scorers: Sequence[Mapping[str, Any]], store: Any, engine: Any, models: Any,
             judge_enabled: bool, timeout_s: float) -> Dict[str, Any]:
    started = time.monotonic()
    result: Dict[str, Any] = {"id": case["id"], "name": case.get("name") or "", "status": "error",
                              "run_status": "", "scores": [], "output": {}, "error": ""}

    def finish() -> Dict[str, Any]:
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        return result

    inputs = _with_defaults(definition.inputs, case.get("inputs") or {})
    if definition.inputs:
        issues = schema_check.validate(inputs, definition.inputs)
        if issues:
            result["error"] = "the case's inputs do not match the workflow's declared inputs: " + "; ".join(issues[:4])
            return finish()
    scorers = list(case.get("scorers") or set_scorers)
    try:
        if mode == "simulate":
            outcome = dry_run(definition, inputs, mocks=case.get("mocks") or {})
            result["run_status"] = outcome["status"]
            outputs = outcome.get("outputs") or {}
            failure = "; ".join(f"{f['node']}: {f['reason']}" for f in outcome.get("failed") or [])
            waiting = outcome.get("paused_on") or []
        else:
            from . import published
            started_run = published.start_run(store, engine, owner, {"definition": definition, "inputs": inputs,
                                                                     "dedupe_key": ""})
            published.wait_for(started_run["future"], timeout_s)
            status = published.run_status(store, owner, started_run["run_id"]) or {}
            result["run_id"] = started_run["run_id"]
            result["run_status"] = status.get("status", "")
            outputs = status.get("result") or {}
            failure = "; ".join(f"{f['node']}: {f['reason']}" for f in status.get("failed") or [])
            waiting = [w["node"] for w in status.get("waiting_on") or []]
            if not status.get("finished") and not waiting and not failure:
                result["error"] = f"the run did not finish within {timeout_s:g}s; it keeps going (run {result['run_id']})"
                return finish()
    except Exception as exc:  # noqa: BLE001 - one case's crash must not take the report with it
        logger.exception("evaluation case %s raised", case.get("id"))
        result["error"] = f"{type(exc).__name__}: {exc}"
        return finish()

    if waiting:
        result["error"] = f"the run stopped to wait on {waiting}; an evaluation does not answer approvals or waits"
        return finish()
    if result["run_status"] != "completed":
        result["status"] = "failed"
        result["error"] = failure or f"the run ended {result['run_status']}"
        return finish()
    result["output"] = _bounded(outputs)
    result["scores"] = score_case(outputs, scorers, models=models, owner=owner, judge_enabled=judge_enabled)
    result["status"] = "passed" if all(s["passed"] for s in result["scores"]) else "failed"
    if not scorers:
        result["note"] = "no scorers: this case only checks that the run completes"
    return finish()


def _bounded(value: Any) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= 20_000:
        return value
    return {"truncated": True, "preview": text[:4000]}


def run_evaluation(owner: str, workflow_name: str, *, set_name: str, mode: str = "simulate",
                   allow_real: bool = False, case_ids: Optional[Sequence[str]] = None,
                   timeout_s: float = DEFAULT_CASE_TIMEOUT_S, store: Any = None, engine: Any = None,
                   models: Any = None, library: Optional[WorkflowLibrary] = None,
                   evals: Optional[EvaluationStore] = None, report_id: Optional[str] = None) -> Dict[str, Any]:
    """Run a saved set against a saved workflow and persist the report.
    Synchronous; :func:`start_evaluation` is the background form."""
    prepared = _prepare(owner, workflow_name, set_name, mode, allow_real, case_ids, timeout_s, store, engine,
                        library, evals)
    return _execute(prepared, models=models, report_id=report_id)


def _prepare(owner, workflow_name, set_name, mode, allow_real, case_ids, timeout_s, store, engine, library, evals):
    library = library or WorkflowLibrary()
    evals = evals or EvaluationStore()
    if mode not in ("simulate", "real"):
        raise EvaluationError("`mode` must be 'simulate' or 'real'")
    if mode == "real" and not allow_real:
        raise EvaluationError("real mode starts real runs (models, skills, senders); pass `allow_real: true` "
                              "to confirm, or use mode 'simulate'", code="real_not_confirmed")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not 1 <= timeout_s <= MAX_CASE_TIMEOUT_S:
        raise EvaluationError(f"`timeout_s` must be a number from 1 to {MAX_CASE_TIMEOUT_S:g}")
    try:
        definition = library.definition(owner, workflow_name)
    except LibraryError as exc:
        raise EvaluationError(str(exc), code="bad_workflow")
    if definition is None:
        raise EvaluationError(f"no saved workflow named {workflow_name!r}", code="not_found")
    row = evals.get_set(owner, workflow_name, set_name)
    if row is None:
        raise EvaluationError(f"no evaluation set named {set_name!r} for {workflow_name!r}", code="not_found")
    body = validate_set(row["set"], definition)
    cases = body["cases"]
    if case_ids:
        wanted = [str(c) for c in case_ids]
        missing = [c for c in wanted if c not in {x["id"] for x in cases}]
        if missing:
            raise EvaluationError(f"the set has no case(s) {missing}", code="not_found")
        cases = [c for c in cases if c["id"] in wanted]
    if mode == "real":
        if len(cases) > MAX_REAL_CASES:
            raise EvaluationError(f"real mode runs at most {MAX_REAL_CASES} cases at a time (this has {len(cases)}); "
                                  "pick some with `cases`")
        if store is None or engine is None:
            raise EvaluationError("real mode needs the workflow engine", code="unavailable")
    return {"owner": owner, "workflow_name": workflow_name, "definition": definition, "set": row, "cases": cases,
            "scorers": body["scorers"], "mode": mode, "timeout_s": float(timeout_s), "store": store,
            "engine": engine, "evals": evals}


def _execute(prepared: Mapping[str, Any], *, models: Any, report_id: Optional[str] = None) -> Dict[str, Any]:
    evals: EvaluationStore = prepared["evals"]
    owner, name, mode = prepared["owner"], prepared["workflow_name"], prepared["mode"]
    rid = report_id or evals.open_report(owner, name, prepared["set"], mode)
    with _ACTIVE_LOCK:
        _ACTIVE.add(rid)
    started = time.monotonic()
    judge = judge_is_enabled()
    results: List[Dict[str, Any]] = []
    status = "completed"
    try:
        for case in prepared["cases"]:
            results.append(_run_one(case, prepared["definition"], mode=mode, owner=owner,
                                    set_scorers=prepared["scorers"], store=prepared["store"],
                                    engine=prepared["engine"], models=models, judge_enabled=judge,
                                    timeout_s=prepared["timeout_s"]))
    except BaseException:
        status = "failed"
        raise
    finally:
        summary = {"mode": mode, "workflow": name, "set": prepared["set"]["name"],
                   "workflow_version": prepared["definition"].version,
                   "fingerprint": prepared["definition"].fingerprint(),
                   "judge_enabled": judge, "wall_ms": int((time.monotonic() - started) * 1000),
                   **aggregate(results)}
        if mode == "simulate":
            summary["note"] = ("simulated: no model, skill or sender ran; this checks wiring, schemas and "
                               "branches, not the quality of a real answer")
        try:
            evals.close_report(rid, status, summary, results)
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(rid)
    return {"id": rid, "status": status, "summary": summary, "cases": results}


_POOL: Optional[ThreadPoolExecutor] = None
_POOL_LOCK = threading.Lock()


def start_evaluation(owner: str, workflow_name: str, *, set_name: str, mode: str = "simulate",
                     allow_real: bool = False, case_ids: Optional[Sequence[str]] = None,
                     timeout_s: float = DEFAULT_CASE_TIMEOUT_S, store: Any = None, engine: Any = None,
                     models: Any = None, library: Optional[WorkflowLibrary] = None,
                     evals: Optional[EvaluationStore] = None) -> Tuple[str, "Future[Dict[str, Any]]"]:
    """Check the request, open the report and run the cases on a background
    thread. Returns `(report_id, future)`."""
    global _POOL
    prepared = _prepare(owner, workflow_name, set_name, mode, allow_real, case_ids, timeout_s, store, engine,
                        library, evals)
    rid = prepared["evals"].open_report(owner, workflow_name, prepared["set"], mode)
    with _ACTIVE_LOCK:
        _ACTIVE.add(rid)
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wf-eval")
        pool = _POOL
    return rid, pool.submit(_execute, prepared, models=models, report_id=rid)
