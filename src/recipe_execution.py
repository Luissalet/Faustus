"""recipe_execution.py — run a learned recipe for real on a new case and check
what it delivered (proposal 34, second half).

`recipe_learning.try_recipe` renders a recipe on a second case without running
anything (a structural dry run). This module is the explicit other mode:

* **steps run through the normal dispatcher.** Each rendered step is one tool
  call through ``src.code_mode.bridge.dispatch_call`` — the same
  ``execute_tool_block`` path the agent loop and Code Mode use, with the
  caller's owner, workspace, session, security context and tool policy. No
  other runner, conversation or agent is started and no permission is added:
  a call the policy blocks comes back blocked and the run says so.
* **steps are derivation-graph nodes.** One graph per recipe and case
  (``recipe-run-<recipe>-<case hash>``), one node per step, chained, with the
  case's existing input files as inputs and the recipe's fingerprint as the
  operation version. ``derivation_graph.rebuild`` drives them, so a node that
  completed is never run again while its record matches (resume after a
  failure, or a second identical request), and an edited input file or a
  changed recipe makes the affected steps stale again. The node's idempotency
  key is the call id handed to the dispatcher, so the execution ledger
  carries it.
* **a step counts only when it finished.** Each step is classified ``ok`` /
  ``error`` (an error object, a non-zero exit code, a blocked call, a process
  that exited non-zero) / ``queued`` (a job that returned an id and had not
  finished when the wait ran out, or one this module has no way to wait on) /
  ``exception``. Processes started with ``process_start`` (and ``bg:`` jobs)
  are waited on through ``process_read`` until they exit or ``job_timeout``
  passes. Anything but ``ok`` stops the chain: later steps are ``skipped``.
* **exit criteria are concrete and checked independently.** The recipe
  declares them (file exists, contains, regex, row count, columns, a number in
  the file equal to a value computed from another file...); after the steps,
  the produced files are opened here and checked, not asked of the tool that
  made them. ``verified`` is true only when every step is ``ok`` (or reused
  from a completed run of the same case) *and* at least one criterion was
  declared *and* every criterion passed. No criteria means
  ``executed_unverified``; this module promises nothing semantic.
* **a verification goes stale.** Each run is stored on the recipe with the
  recipe fingerprint, the criteria and the hashes of the input files and the
  checked files. ``verification_status`` re-measures them: a changed recipe,
  criteria, input or delivered file marks the earlier verification stale.

Limits: a step left ``running`` by a crash (the process died between the call
and its record) is offered again by the graph with ``interrupted: true``; this
module re-runs it and reports that, because only the tool can know whether its
side effect happened. Values a tool returned are not threaded into later
calls (the recipe keeps them literal, see recipe_learning).
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src import derivation_graph as dg
from src import recipe_learning as rl
from src import recipes as recipes_store

logger = logging.getLogger(__name__)

MODE_DRY_RUN = "dry_run"
MODE_EXECUTE = "execute"
MODES = (MODE_DRY_RUN, MODE_EXECUTE)
DEFAULT_JOB_TIMEOUT = 120
MAX_JOB_TIMEOUT = 1800
MAX_CRITERIA = 40
MAX_EXECUTIONS_KEPT = 20
_READ_LIMIT = 8 * 1024 * 1024
_OUTPUT_KEEP = 1200
_RUNNING_STATES = frozenset({"starting", "running"})
_QUEUED_STATES = frozenset({"queued", "pending", "running", "started", "submitted", "in_progress", "waiting",
                            "starting", "scheduled", "accepted"})
_ID_KEYS = ("handle", "job_id", "task_id", "run_id", "request_id", "artifact_id", "attempt_id", "effect_id", "id")
CRITERIA_KINDS = ("file_exists", "contains", "regex", "row_count", "columns", "number")

Executor = Callable[[str, Dict[str, Any], str], Any]


class StepFailed(Exception):
    pass


# ---------------------------------------------------------------------------
# the executor: the existing dispatcher, with the caller's context
# ---------------------------------------------------------------------------

def make_dispatch_executor(owner: str, *, workspace: Optional[str], session_id: Optional[str] = None,
                           security_context: Any = None, tool_policy: Any = None,
                           disabled_tools: Optional[Sequence[str]] = None,
                           loop: Optional[asyncio.AbstractEventLoop] = None,
                           call_timeout: float = 3600.0) -> Executor:
    """A synchronous ``executor(tool, args, call_id)`` over
    ``code_mode.bridge.dispatch_call``. With ``loop`` (the server's running
    loop) each call is scheduled on it from the worker thread the rebuild runs
    in, so tools that keep loop-bound state (MCP sessions, processes) see the
    loop they live on; without it each call gets its own ``asyncio.run``."""
    disabled = set(disabled_tools or ()) | {"recipe_learn"}   # a recipe never runs recipes

    def call(tool: str, args: Dict[str, Any], call_id: str) -> Any:
        from src.code_mode.bridge import dispatch_call
        coro = dispatch_call(tool, args, session_id=session_id, owner=owner, workspace=workspace,
                             workspace_roots=[workspace] if workspace else None, disabled_tools=disabled,
                             call_id=call_id, tool_policy=tool_policy, security_context=security_context)
        if loop is not None and loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=call_timeout)
        return asyncio.run(coro)

    return call


# ---------------------------------------------------------------------------
# step results
# ---------------------------------------------------------------------------

def _ids(result: Mapping[str, Any]) -> Dict[str, str]:
    return {k: str(result[k]) for k in _ID_KEYS if isinstance(result.get(k), (str, int)) and str(result[k])}


def _compact(result: Any) -> Any:
    if not isinstance(result, Mapping):
        text = str(result)
        return text[:_OUTPUT_KEEP]
    out: Dict[str, Any] = {}
    for k in ("output", "error", "exit_code", "process_exit_code", "state", "status", "termination_reason",
              "blocked", "policy", "outcome_unknown"):
        if k in result:
            v = result[k]
            out[k] = v[:_OUTPUT_KEEP] if isinstance(v, str) else v
    return out


def classify(tool: str, result: Any) -> Tuple[str, str, Optional[Dict[str, str]]]:
    """``(status, detail, wait)`` for one call's result. ``wait`` names a job
    to wait on (``{"handle": ...}``) when the call returned one still running."""
    if not isinstance(result, Mapping):
        return "ok", "", None
    if result.get("blocked") is True:
        return "error", f"blocked by policy {result.get('policy') or ''}".strip(), None
    if result.get("error"):
        return "error", str(result.get("error"))[:500], None
    code = result.get("exit_code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return "error", f"exit code {code}", None
    if result.get("ok") is False or result.get("success") is False:
        return "error", "the tool reported failure", None
    if str(result.get("status") or "").lower() in ("error", "failed", "failure"):
        return "error", f"status {result.get('status')}", None
    if result.get("outcome_unknown"):
        return "error", "outcome unknown (the tool could not tell whether its effect happened)", None
    state = str(result.get("state") or "").lower()
    if result.get("handle") and (state in _RUNNING_STATES):
        return "pending", f"{result['handle']} {state}", {"handle": str(result["handle"])}
    if result.get("handle") and state:
        pcode = result.get("process_exit_code")
        if state != "exited":
            return "error", f"process {state} ({result.get('termination_reason') or 'no exit code'})", None
        if pcode is None:
            return "error", "process exited without a known exit code", None
        if pcode != 0:
            return "error", f"process exited with code {pcode}", None
        return "ok", "", None
    job = result.get("job_id") or result.get("task_id")
    st = str(result.get("status") or result.get("state") or "").lower()
    if job and st in _QUEUED_STATES:
        if tool == "bash" and result.get("job_id"):
            return "pending", f"bg job {job} {st}", {"handle": f"bg:{job}"}
        return "queued", f"job {job} is {st}; no waiter for {tool} jobs, it is not counted as finished", None
    return "ok", "", None


def _wait(executor: Executor, wait: Mapping[str, str], call_id: str, timeout: float) -> Tuple[Any, bool]:
    """Read the job through ``process_read`` until it leaves the running state.
    Returns (last result, finished)."""
    deadline = time.monotonic() + max(0.0, timeout)
    last: Any = None
    n = 0
    while True:
        remaining = deadline - time.monotonic()
        wait_ms = int(max(0, min(5.0, remaining)) * 1000)
        n += 1
        last = executor("process_read", {"handle": wait["handle"], "wait_ms": wait_ms, "max_bytes": 4096},
                        f"{call_id}:wait{n}")
        state = str((last or {}).get("state") or "").lower() if isinstance(last, Mapping) else ""
        if isinstance(last, Mapping) and last.get("error"):
            return last, True
        if state not in _RUNNING_STATES:
            return last, True
        if time.monotonic() >= deadline:
            return last, False


# ---------------------------------------------------------------------------
# exit criteria — an independent check of what was delivered
# ---------------------------------------------------------------------------

def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def _resolve(path: Any, workspace: str) -> str:
    p = str(path or "").strip()
    if not p:
        raise ValueError("a criterion needs a path")
    full = os.path.normpath(p if os.path.isabs(p) else os.path.join(workspace, p))
    if not _inside(full, workspace):
        raise ValueError(f"{p} is outside the workspace")
    return full


def _read_text(full: str) -> str:
    with open(full, "rb") as fh:
        data = fh.read(_READ_LIMIT + 1)
    if len(data) > _READ_LIMIT:
        raise ValueError("file too large to check")
    # Files written on Windows end lines with CRLF; normalise so ``^...$`` criteria
    # behave the same on every platform.
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


def _md_table(text: str) -> Tuple[List[str], List[List[str]]]:
    lines = text.splitlines()
    for i in range(len(lines) - 1):
        head, sep = lines[i].strip(), lines[i + 1].strip()
        if head.startswith("|") and re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", sep):
            cells = lambda ln: [c.strip() for c in ln.strip().strip("|").split("|")]  # noqa: E731
            rows = []
            for ln in lines[i + 2:]:
                if not ln.strip().startswith("|"):
                    break
                rows.append(cells(ln))
            return cells(head), rows
    raise ValueError("no Markdown table found")


def _html_table(text: str) -> Tuple[List[str], List[List[str]]]:
    strip = lambda s: re.sub(r"<[^>]+>", "", s).strip()  # noqa: E731
    header = [strip(c) for c in re.findall(r"<th[^>]*>(.*?)</th>", text, re.I | re.S)]
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.I | re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.I | re.S)
        if tds:
            rows.append([strip(c) for c in tds])
    if not header and not rows:
        raise ValueError("no HTML table found")
    return header, rows


def read_table(full: str) -> Tuple[List[str], List[List[str]]]:
    """(header, body rows) of a CSV/TSV file, the first Markdown table of a
    .md/.txt file or the first table of an HTML file."""
    ext = os.path.splitext(full)[1].lower()
    text = _read_text(full)
    if ext in (".csv", ".tsv"):
        rows = list(csv.reader(io.StringIO(text), delimiter="\t" if ext == ".tsv" else ","))
        rows = [r for r in rows if any(c.strip() for c in r)]
        if not rows:
            return [], []
        return [c.strip() for c in rows[0]], rows[1:]
    if ext in (".md", ".markdown", ".txt"):
        return _md_table(text)
    if ext in (".html", ".htm"):
        return _html_table(text)
    raise ValueError(f"cannot read a table from {ext or 'a file without extension'}")


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).strip().replace(",", "."))


def _expected(spec: Any, workspace: str) -> Any:
    """A literal, or a value computed independently from a file:
    {"csv_sum": {"path", "column"}}, {"rows_of": path}, {"columns_of": path}."""
    if isinstance(spec, Mapping):
        if "csv_sum" in spec:
            s = spec["csv_sum"] or {}
            header, rows = read_table(_resolve(s.get("path"), workspace))
            col = str(s.get("column") or "")
            if col not in header:
                raise ValueError(f"column {col!r} not in {header}")
            i = header.index(col)
            return sum(_number(r[i]) for r in rows if i < len(r) and str(r[i]).strip())
        if "rows_of" in spec:
            return len(read_table(_resolve(spec["rows_of"], workspace))[1])
        if "columns_of" in spec:
            return read_table(_resolve(spec["columns_of"], workspace))[0]
        raise ValueError(f"unknown expected value {dict(spec)!r}")
    return spec


def _check_one(c: Mapping[str, Any], workspace: str) -> Dict[str, Any]:
    kind = str(c.get("kind") or "")
    row: Dict[str, Any] = {"kind": kind, "path": c.get("path"), "passed": False}
    if c.get("label"):
        row["label"] = str(c["label"])
    try:
        full = _resolve(c.get("path"), workspace)
        exists = os.path.isfile(full)
        if kind == "file_exists":
            row.update(passed=exists, observed="exists" if exists else "missing")
            if exists:
                row["sha256"] = dg.sha256_file(full)
            return row
        if not exists:
            row["observed"] = "missing"
            return row
        row["sha256"] = dg.sha256_file(full)
        if kind == "contains":
            text = str(c.get("text") or "")
            if not text:
                raise ValueError("contains needs text")
            row.update(expected=text, passed=text in _read_text(full))
        elif kind == "regex":
            pattern = re.compile(str(c.get("pattern") or ""), re.M)
            n = len(pattern.findall(_read_text(full)))
            want = int(c.get("min_count", 1))
            row.update(expected=f">= {want} match(es)", observed=n, passed=n >= want)
        elif kind == "row_count":
            n = len(read_table(full)[1])
            row["observed"] = n
            if "equals" in c:
                want = int(_number(_expected(c["equals"], workspace)))
                row.update(expected=want, passed=n == want)
            else:
                lo, hi = c.get("min"), c.get("max")
                if lo is None and hi is None:
                    raise ValueError("row_count needs equals, min or max")
                row.update(expected={"min": lo, "max": hi},
                           passed=(lo is None or n >= int(lo)) and (hi is None or n <= int(hi)))
        elif kind == "columns":
            header = read_table(full)[0]
            row["observed"] = header
            if "equals" in c:
                want = [str(x) for x in _expected(c["equals"], workspace)]
                row.update(expected=want, passed=header == want)
            else:
                want = [str(x) for x in (c.get("includes") or [])]
                if not want:
                    raise ValueError("columns needs equals or includes")
                row.update(expected={"includes": want}, passed=all(w in header for w in want))
        elif kind == "number":
            m = re.search(str(c.get("pattern") or ""), _read_text(full), re.M)
            if not m or not m.groups():
                row["observed"] = "pattern not found (it needs one capture group)"
                return row
            got = _number(m.group(1))
            want = _number(_expected(c.get("equals"), workspace))
            tol = float(c.get("tolerance", 1e-9))
            row.update(observed=got, expected=want, passed=abs(got - want) <= tol)
        else:
            raise ValueError(f"unknown criterion kind {kind!r}; use one of {', '.join(CRITERIA_KINDS)}")
    except (ValueError, OSError, re.error, IndexError, TypeError) as exc:
        row["error"] = str(exc)
        row["passed"] = False
    return row


def check_criteria(criteria: Sequence[Mapping[str, Any]], workspace: str) -> List[Dict[str, Any]]:
    return [_check_one(c, workspace) for c in criteria]


def normalise_criteria(raw: Any) -> List[Dict[str, Any]]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or not all(isinstance(c, Mapping) for c in raw):
        raise rl.RecipeLearningError("recipes.bad_criteria", "criteria must be a list of objects {kind, path, ...}")
    if len(raw) > MAX_CRITERIA:
        raise rl.RecipeLearningError("recipes.bad_criteria", f"at most {MAX_CRITERIA} criteria")
    out = []
    for c in raw:
        if str(c.get("kind") or "") not in CRITERIA_KINDS:
            raise rl.RecipeLearningError("recipes.bad_criteria",
                                         f"criterion kind must be one of {', '.join(CRITERIA_KINDS)}")
        out.append(json.loads(json.dumps(dict(c), default=str)))
    return out


# ---------------------------------------------------------------------------
# fingerprints and staleness
# ---------------------------------------------------------------------------

def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def case_key(recipe_id: str, values: Mapping[str, Any]) -> str:
    return _hash([recipe_id, dict(values)])[:12]


def graph_id_for(recipe_id: str, values: Mapping[str, Any]) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", f"recipe-run-{recipe_id}-{case_key(recipe_id, values)}")[:120]


def _file_inputs(values: Mapping[str, Any], workspace: str, exclude: set,
                 explicit: Optional[Sequence[Any]]) -> List[str]:
    """Input files of the case: those named explicitly, else every parameter
    value that is an existing file inside the workspace and is not a checked
    deliverable."""
    if explicit:
        return [str(_resolve(p, workspace)) for p in explicit if str(p or "").strip()]
    out = []
    for v in values.values():
        if not isinstance(v, str) or not v.strip() or len(v) > 1024:
            continue
        try:
            full = _resolve(v, workspace)
        except ValueError:
            continue
        if os.path.isfile(full) and full not in exclude and full not in out:
            out.append(full)
    return out


def _measure(paths: Sequence[str]) -> Dict[str, Optional[str]]:
    return {p: dg.sha256_file(p) for p in paths}


def staleness(recipe: recipes_store.Recipe, record: Mapping[str, Any]) -> List[str]:
    reasons: List[str] = []
    if record.get("recipe_fingerprint") != rl._fingerprint(recipe):
        reasons.append("recipe_changed")
    if record.get("criteria_hash") != _hash(recipe.exit_criteria):
        reasons.append("criteria_changed")
    for p, h in (record.get("input_hashes") or {}).items():
        if dg.sha256_file(p) != h:
            reasons.append(f"input_changed:{p}")
    for p, h in (record.get("artifact_hashes") or {}).items():
        if dg.sha256_file(p) != h:
            reasons.append(f"delivered_file_changed:{p}")
    return reasons


def verification_status(recipe_id: str, owner: str) -> Dict[str, Any]:
    recipe = recipes_store.get_recipe(recipe_id, owner)
    if recipe is None or not recipe.step_templates:
        raise rl.RecipeLearningError("recipes.not_found", f"no learned recipe {recipe_id!r}")
    latest: Dict[str, Dict[str, Any]] = {}
    for rec in recipe.executions:
        latest[str(rec.get("case_key"))] = rec
    rows = []
    for rec in latest.values():
        reasons = staleness(recipe, rec)
        rows.append({"case_key": rec.get("case_key"), "params": rec.get("params"), "at": rec.get("at"),
                     "status": rec.get("status"), "verified": bool(rec.get("verified")),
                     "fresh": bool(rec.get("verified")) and not reasons, "stale": bool(reasons),
                     "stale_reasons": reasons, "graph": rec.get("graph")})
    return {"recipe_id": recipe.id, "fingerprint": rl._fingerprint(recipe), "exit_criteria": recipe.exit_criteria,
            "cases": rows,
            "note": "A verification is fresh only while the recipe, its criteria, the input files and the "
                    "delivered files are unchanged since that run."}


def latest_fresh_verification(recipe: recipes_store.Recipe) -> Optional[Dict[str, Any]]:
    for rec in reversed(recipe.executions):
        if rec.get("verified") and not staleness(recipe, rec):
            return {"case_key": rec.get("case_key"), "params": rec.get("params"), "at": rec.get("at")}
    return None


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

def _sync_graph(owner: str, graph: str, workspace: str, recipe: recipes_store.Recipe,
                rendered: Sequence[Mapping[str, Any]], inputs: Sequence[str], rerun: bool) -> List[str]:
    fp = rl._fingerprint(recipe)
    existing = dg.get_graph(owner, graph).get("nodes") or {}
    wanted = [f"s{i:02d}" for i in range(1, len(rendered) + 1)]
    stale_ids = sorted((n for n in existing if n not in wanted), reverse=True)
    if rerun:
        stale_ids = sorted(existing, reverse=True)
    for nid in stale_ids:
        dg.remove_node(owner, graph, nid)
    prev = None
    for nid, step in zip(wanted, rendered):
        node_inputs: List[Any] = list(inputs)
        if prev:
            node_inputs.append({"node": prev})
        dg.define_node(owner, graph, nid, op={"kind": "tool", "name": step["tool"], "version": fp},
                       inputs=node_inputs, params={"index": step["index"], "tool": step["tool"], "args": step["args"]},
                       base_dir=workspace, provenance={"recipe": recipe.id, "kind": "recipe_step"})
        prev = nid
    return wanted


def execute(recipe_id: str, owner: str, params: Mapping[str, Any], *, executor: Executor, workspace: str,
            criteria: Any = None, inputs: Optional[Sequence[Any]] = None,
            job_timeout: Any = None, rerun: bool = False) -> Dict[str, Any]:
    """Run ``recipe_id`` for real on the case ``params`` (see the module docstring)."""
    if not workspace or not os.path.isdir(workspace):
        raise rl.RecipeLearningError("recipes.no_workspace", "a real execution needs an existing workspace folder")
    workspace = os.path.realpath(workspace)
    timeout = DEFAULT_JOB_TIMEOUT if job_timeout in (None, "") else max(1, min(MAX_JOB_TIMEOUT, int(job_timeout)))
    recipe = recipes_store.get_recipe(recipe_id, owner)
    if recipe is None or not recipe.step_templates:
        raise rl.RecipeLearningError("recipes.not_found", f"no learned recipe {recipe_id!r}")
    new_criteria = normalise_criteria(criteria)
    if new_criteria and new_criteria != recipe.exit_criteria:
        recipe.exit_criteria = new_criteria
        recipes_store.save_recipe(owner, recipe)

    dry = rl.try_recipe(recipe.id, owner, params, record=False)
    base = {"recipe_id": recipe.id, "mode": MODE_EXECUTE, "version": recipe.version, "fingerprint": dry["fingerprint"],
            "params": dry["params"], "unknown_params": dry["unknown_params"], "workspace": workspace,
            "exit_criteria": recipe.exit_criteria}
    blockers = list(dry["problems"])
    blockers += [f"step {s['index']} unresolved: {s['reason']}" for s in dry["steps"] if s["status"] != "resolved"]
    blockers += [f"literal of the first case still in step {l['step']} ({l['param']})" for l in dry["leaks"]]
    if blockers:
        return {**base, "executed": False, "verified": False, "status": "not_runnable", "why": blockers,
                "steps": dry["steps"]}

    values = dry["params"]
    rendered_criteria = [rl.render(c, values, set()) for c in recipe.exit_criteria]
    deliverables = set()
    for c in rendered_criteria:
        try:
            deliverables.add(_resolve(c.get("path"), workspace))
        except ValueError:
            pass
    input_files = _file_inputs(values, workspace, deliverables, inputs)
    graph = graph_id_for(recipe.id, values)
    rendered = [{"index": s["index"], "tool": s["tool"], "args": s["args"]} for s in dry["steps"]]
    node_ids = _sync_graph(owner, graph, workspace, recipe, rendered, input_files, rerun)

    reports: Dict[str, Dict[str, Any]] = {}

    def runner(view: Dict[str, Any]) -> Dict[str, Any]:
        p = view["params"]
        tool, args = str(p["tool"]), p["args"] if isinstance(p["args"], dict) else {"content": p["args"]}
        call_id = f"recipe:{view['idempotency_key']}"
        rep: Dict[str, Any] = {"index": p["index"], "node": view["id"], "tool": tool, "args": args,
                               "idempotency_key": view["idempotency_key"], "why_run": view["reasons"],
                               "interrupted_rerun": bool(view.get("interrupted")), "started_at": time.time()}
        reports[view["id"]] = rep
        try:
            result = executor(tool, args, call_id)
        except Exception as exc:  # noqa: BLE001 - recorded as the step's outcome
            rep.update(status="exception", detail=f"{type(exc).__name__}: {exc}")
            raise StepFailed(rep["detail"]) from exc
        status, detail, wait = classify(tool, result)
        ids = _ids(result) if isinstance(result, Mapping) else {}
        if status == "pending" and wait:
            rep["waited_for"] = wait["handle"]
            try:
                final, finished = _wait(executor, wait, call_id, timeout)
            except Exception as exc:  # noqa: BLE001
                rep.update(status="exception", detail=f"waiting for {wait['handle']}: {type(exc).__name__}: {exc}",
                           ids=ids, output=_compact(result))
                raise StepFailed(rep["detail"]) from exc
            if not finished:
                status, detail = "queued", f"{wait['handle']} still running after {timeout}s"
            else:
                status, detail, _ = classify("process_read", final)
                if status == "pending":
                    status, detail = "queued", f"{wait['handle']} did not finish"
            result = final
        rep.update(status=status, detail=detail, ids=ids, output=_compact(result), finished_at=time.time())
        if status != "ok":
            raise StepFailed(f"{tool}: {status}: {detail}")
        return {"value": {"status": "ok", "ids": ids, "output_sha": _hash(rep["output"])[:16]}}

    rb = dg.rebuild(owner, graph, node_ids[-1] if node_ids else None, runner=runner, atlas=False)
    if rb.get("status") == "busy":
        return {**base, "executed": False, "verified": False, "status": "busy", "why": [rb.get("detail")]}

    steps: List[Dict[str, Any]] = []
    gstate = dg.get_graph(owner, graph).get("nodes") or {}
    for nid, step in zip(node_ids, rendered):
        if nid in reports:
            steps.append(reports[nid])
        elif nid in rb.get("reused", []):
            st = (gstate.get(nid) or {}).get("state") or {}
            steps.append({"index": step["index"], "node": nid, "tool": step["tool"], "args": step["args"],
                          "status": "reused", "idempotency_key": st.get("idempotency_key"),
                          "detail": "completed in an earlier run of this case; not executed again",
                          "completed_at": st.get("completed_at")})
        else:
            steps.append({"index": step["index"], "node": nid, "tool": step["tool"], "args": step["args"],
                          "status": "skipped", "detail": "not run: an earlier step did not finish"})
    executed_now = [s for s in steps if s["status"] not in ("reused", "skipped")]
    steps_ok = all(s["status"] in ("ok", "reused") for s in steps)
    checks = check_criteria(rendered_criteria, workspace)
    criteria_ok = bool(checks) and all(c["passed"] for c in checks)
    verified = steps_ok and criteria_ok
    if not steps_ok:
        bad = next(s for s in steps if s["status"] not in ("ok", "reused"))
        status = {"queued": "incomplete"}.get(bad["status"], "step_failed")
    elif not checks:
        status = "executed_unverified"
    else:
        status = "verified" if criteria_ok else "criteria_failed"

    artifacts = {}
    for c in checks:
        try:
            full = _resolve(c.get("path"), workspace)
        except ValueError:
            continue
        artifacts[full] = dg.sha256_file(full)
    record = {"at": time.time(), "case_key": case_key(recipe.id, values), "params": values, "graph": graph,
              "status": status, "verified": verified, "recipe_fingerprint": rl._fingerprint(recipe),
              "criteria_hash": _hash(recipe.exit_criteria), "input_hashes": _measure(input_files),
              "artifact_hashes": artifacts,
              "steps": [{"index": s["index"], "tool": s["tool"], "status": s["status"], "detail": s.get("detail", ""),
                         "ids": s.get("ids", {})} for s in steps],
              "criteria": [{"kind": c["kind"], "path": c.get("path"), "passed": c["passed"]} for c in checks]}
    recipe = recipes_store.get_recipe(recipe.id, owner) or recipe
    recipe.executions = (recipe.executions + [record])[-MAX_EXECUTIONS_KEPT:]
    recipes_store.save_recipe(owner, recipe)

    out = {**base, "executed": any(s["status"] != "skipped" for s in steps), "verified": verified, "status": status,
           "steps": steps, "criteria": checks, "inputs": record["input_hashes"], "graph": graph,
           "ran_now": len(executed_now), "reused": sum(1 for s in steps if s["status"] == "reused"),
           "skipped": sum(1 for s in steps if s["status"] == "skipped")}
    if not checks:
        out["why_unverified"] = ["no exit criteria declared for this recipe: the steps ran, nothing was checked"]
    elif not verified:
        out["why_unverified"] = (
            [f"step {s['index']} ({s['tool']}) {s['status']}: {s.get('detail', '')}" for s in steps
             if s["status"] not in ("ok", "reused")]
            + [f"criterion {c['kind']} on {c.get('path')} failed"
               + (f": {c['error']}" if c.get("error") else f" (expected {c.get('expected')}, observed {c.get('observed')})")
               for c in checks if not c["passed"]])
    return out
