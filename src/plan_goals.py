"""Typed goals of plan tasks (OBJ-47): declared in the plan, run by `plan_done`.

A task of the persisted plan (`src/plan_tracker.py`) can carry typed,
machine-checkable goals instead of prose acceptance only. They are written in
the plan itself, one per line anywhere in the task's section:

    test_passes: pytest tests/test_notes.py -q
    http_ok: https://example.com/health 200
    file_exists: dist/app.js               (kind known, not run: see below)

`extract_goals` turns those lines into `PlanTask.goals` at parse time.
`evaluate_goals` runs them when the model calls `plan_done` for that task and
returns one verdict per goal (`passed` / `failed` / `skipped`, with short
evidence). `plan_done` seals the task only when nothing failed.

Execution rules, all of them deliberate:

* `test_passes` goes through the harness's own test runner
  (`src.project_tests.run_tests`): an argv list (never a shell, so nothing the
  plan says can be interpreted by one), the project's interpreter, the scrubbed
  environment, a process-tree kill on timeout and the shared `cpu_heavy`
  admission. The command must be a recognised test runner (`pytest`,
  `python -m pytest|unittest`, `npm|pnpm|yarn test`, `node --test`,
  `cargo test`, `go test`, `make test`); every path argument is confined to
  the turn's workspace. No workspace bound, a turn policy that forbids shell
  execution (read-only preset, disabled `bash`, guide-only...) or a required
  sandbox (the host runner cannot honour it) means the goal is `skipped`.
* `http_ok` is a bounded GET through the same SSRF-guarded broker `web_fetch`
  uses, under the same profile (public destinations only). A loopback, LAN or
  otherwise non-public URL is refused, so the goal is `skipped` with that
  reason; it is never fetched through a more permissive profile.
* Any other kind is `skipped` with "not checkable automatically": a goal is
  never reported as passed without having been checked.
* Each goal has a time bound, and the whole call has a total budget.

`skipped` is not `passed`: the report counts it, names the reason, and the
tool response lists it. It does not block sealing (a policy refusal must not
make a task unclosable), but it is never silent.

Pure stdlib plus lazy imports of the harness modules above; never raises out
of `evaluate_goals`.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Criterion kinds a plan may declare (the vocabulary of `src.creator.goal`).
GOAL_KINDS = (
    "test_passes", "file_exists", "artifact_present", "http_ok",
    "doc_revision_at_least", "custom_check",
)
#: The kinds this module actually runs. The rest are reported as skipped.
EXECUTABLE_KINDS = ("test_passes", "http_ok")

MAX_GOALS_PER_TASK = 12
DEFAULT_GOAL_TIMEOUT_S = 120.0
MIN_GOAL_TIMEOUT_S = 10.0     # project_tests.run_tests never runs below this
MAX_GOAL_TIMEOUT_S = 600.0
DEFAULT_HTTP_TIMEOUT_S = 10.0
#: All goals of one `plan_done` call share this budget.
TOTAL_BUDGET_S = 900.0

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"

_GOAL_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:\[[ xX]\]\s*)?[`*_]*(" + "|".join(GOAL_KINDS) + r")[`*_]*\s*[:=]\s*[`*_]*\s*(.+?)\s*$",
    re.I,
)
_STATUS_RE = re.compile(r"\b([1-5]\d\d)\b")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def enabled() -> bool:
    """`agent_plan_goals` (default on). Off: `plan_done` behaves as before."""
    return _setting("agent_plan_goals", True) is not False


def goal_timeout_s() -> float:
    try:
        v = float(_setting("agent_plan_goal_timeout_seconds", DEFAULT_GOAL_TIMEOUT_S) or DEFAULT_GOAL_TIMEOUT_S)
    except (TypeError, ValueError):
        v = DEFAULT_GOAL_TIMEOUT_S
    return max(MIN_GOAL_TIMEOUT_S, min(v, MAX_GOAL_TIMEOUT_S))


# ---------------------------------------------------------------------------
# Parsing (called by plan_tracker.parse_plan)
# ---------------------------------------------------------------------------

def _clean(value: str) -> str:
    return (value or "").strip().strip("`").strip()


def _spec_for(kind: str, rest: str) -> Dict[str, Any]:
    rest = _clean(rest)
    if kind == "test_passes":
        return {"cmd": rest}
    if kind == "http_ok":
        parts = rest.split(None, 1)
        url = _clean(parts[0]).strip("<>") if parts else ""
        spec: Dict[str, Any] = {"url": url}
        m = _STATUS_RE.search(parts[1]) if len(parts) > 1 else None
        if m:
            spec["expect"] = int(m.group(1))
        return spec
    return {"text": rest[:300]}


def extract_goals(text: str) -> List[Dict[str, Any]]:
    """Typed goals declared in a task's text, in order, de-duplicated.
    `[{"id": "g1", "kind", "spec", "raw"}]`. Never raises."""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    try:
        for line in (text or "").split("\n"):
            m = _GOAL_LINE_RE.match(line)
            if not m:
                continue
            kind = m.group(1).lower()
            spec = _spec_for(kind, m.group(2))
            if not any(str(v).strip() for v in spec.values()):
                continue
            key = (kind, tuple(sorted((k, str(v)) for k, v in spec.items())))
            if key in seen:
                continue
            seen.add(key)
            out.append({"id": f"g{len(out) + 1}", "kind": kind, "spec": spec, "raw": line.strip()[:300]})
            if len(out) >= MAX_GOALS_PER_TASK:
                break
    except Exception:
        return out
    return out


def has_executable_goals(task: Dict[str, Any]) -> bool:
    return any(str((g or {}).get("kind")) in EXECUTABLE_KINDS for g in (task or {}).get("goals") or [])


def holds_auto_close(task: Dict[str, Any], state_entry: Optional[Dict[str, Any]]) -> bool:
    """True when `plan_tracker.reconcile` must NOT close this task on its own:
    it has goals this module can run and no `plan_done` run has passed them
    yet. Only `plan_done` seals such a task."""
    if not enabled() or not has_executable_goals(task):
        return False
    rep = (state_entry or {}).get("goals") or {}
    return not (rep.get("ok") is True and int(rep.get("passed") or 0) >= 1)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def _policy_refusal(policy: Any, tool: str, content: str) -> Optional[str]:
    """Why the turn's tool policy forbids the equivalent agent tool call
    (`bash` for a test command, `web_fetch` for a URL), else None. A policy
    that cannot be evaluated refuses: a goal never runs on a guess."""
    if policy is None:
        return None
    try:
        blocks = getattr(policy, "blocks_action", None)
        denied = blocks(tool, content) if callable(blocks) else policy.blocks(tool)
        if denied:
            reason = ""
            try:
                reason = str(policy.reason_for(tool) or "")
            except Exception:
                reason = ""
            return reason or f"the turn's tool policy forbids {tool}"
    except Exception as exc:  # noqa: BLE001
        return f"tool policy could not be evaluated ({type(exc).__name__})"
    return None


# ---------------------------------------------------------------------------
# test_passes
# ---------------------------------------------------------------------------

_SHELL_PUNCT = set(";&|<>()")


def _split_command(cmd: str) -> Tuple[Optional[List[str]], str]:
    if "`" in cmd or "$(" in cmd or "\n" in cmd:
        return None, "shell substitution and multi-line commands are not supported"
    if os.name == "nt":
        # posix shlex eats backslashes (C:\proj\tests -> C:projtests); Windows
        # accepts forward slashes everywhere a runner takes a path.
        cmd = cmd.replace("\\", "/")
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError as exc:
        return None, f"could not parse the command: {exc}"
    if any(t and set(t) <= _SHELL_PUNCT for t in toks):
        return None, "shell operators (; & | < > ( )) are not supported: the command runs without a shell"
    if not toks:
        return None, "empty command"
    return toks, ""


def _looks_like_path(arg: str) -> bool:
    if os.path.isabs(arg) or re.match(r"^[A-Za-z]:", arg):
        return True
    if arg in (".", "..") or "/" in arg or "\\" in arg:
        return True
    return ".." in re.split(r"[\\/]", arg)


def _inside_workspace(workspace: str, raw: str) -> bool:
    from src.tool_execution import _is_sensitive_path, _path_is_within_root
    if re.match(r"^[A-Za-z]:", raw) and not re.match(r"^[A-Za-z]:[\\/]", raw):
        return False                      # drive-relative (C:foo): cannot be pinned to the workspace
    base = os.path.realpath(workspace)
    cand = raw if os.path.isabs(raw) else os.path.join(base, raw)
    real = os.path.realpath(cand)
    return _path_is_within_root(real, base) and not _is_sensitive_path(real)


def _confine_args(args: List[str], workspace: str) -> Optional[str]:
    """None when every path-like argument stays in the workspace, else why not."""
    for a in args:
        if a.startswith("-"):
            if "=" not in a:
                continue
            value = a.split("=", 1)[1]
        else:
            value = a
        value = value.split("::", 1)[0]
        if value and _looks_like_path(value) and not _inside_workspace(workspace, value):
            return f"path argument {a!r} is outside the workspace"
    return None


def _which(name: str) -> Optional[str]:
    if os.name == "nt":
        return shutil.which(name + ".cmd") or shutil.which(name + ".exe") or shutil.which(name)
    return shutil.which(name)


def resolve_test_argv(cmd: str, workspace: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """(spec for `project_tests.run_tests`, "") or (None, refusal reason).
    The command is split without a shell, must start with a recognised test
    runner, and all its path arguments must stay inside `workspace`."""
    toks, why = _split_command(cmd)
    if toks is None:
        return None, why
    head = toks[0]
    if "/" in head or "\\" in head:
        return None, f"the runner must be a bare command name, got {head!r}"
    name = re.sub(r"\.(exe|cmd|bat)$", "", head.lower())
    rest = toks[1:]
    kind = ""
    prefix: List[str] = []
    python_module = ""
    if name in ("pytest", "py.test"):
        kind, python_module = "pytest", "pytest"
    elif name in ("python", "python3", "py"):
        if len(rest) >= 2 and rest[0] == "-m" and rest[1] in ("pytest", "unittest"):
            kind, python_module, rest = ("pytest" if rest[1] == "pytest" else "unittest"), rest[1], rest[2:]
        else:
            return None, "only `python -m pytest` and `python -m unittest` are accepted"
    elif name in ("npm", "pnpm", "yarn"):
        if rest[:1] == ["test"]:
            rest = rest[1:]
        elif rest[:2] == ["run", "test"]:
            rest = rest[2:]
        else:
            return None, f"only `{name} test` is accepted"
        exe = _which(name)
        if not exe:
            return None, f"runner not found on PATH: {name}"
        kind, prefix = "npm", [exe, "test"]
    elif name == "node":
        if rest[:1] != ["--test"]:
            return None, "only `node --test` is accepted"
        exe = _which("node")
        if not exe:
            return None, "runner not found on PATH: node"
        kind, prefix, rest = "node", [exe, "--test"], rest[1:]
    elif name in ("cargo", "go"):
        if rest[:1] != ["test"]:
            return None, f"only `{name} test` is accepted"
        exe = _which(name)
        if not exe:
            return None, f"runner not found on PATH: {name}"
        kind, prefix, rest = name, [exe, "test"], rest[1:]
    elif name == "make":
        if rest != ["test"]:
            return None, "only `make test` is accepted"
        exe = _which("make")
        if not exe:
            return None, "runner not found on PATH: make"
        kind, prefix, rest = "make", [exe, "test"], []
    else:
        return None, f"{head!r} is not a recognised test runner"
    confined = _confine_args(rest, workspace)
    if confined:
        return None, confined
    if python_module:
        from src import project_tests
        py = project_tests._python_for(workspace) or project_tests._fallback_python(workspace)
        if not py:
            return None, "no Python interpreter available"
        prefix = [py, "-m", python_module]
        if python_module == "pytest":
            prefix += ["-q", "--no-header", "-p", "no:cacheprovider", "--color=no"]
    argv = prefix + rest
    return {"kind": kind, "argv": argv, "label": " ".join(shlex.quote(a) for a in argv[:8])}, ""


def _run_spec(spec: Dict[str, Any], workspace: str, timeout: float) -> Dict[str, Any]:
    """The seam tests replace: the harness's bounded, no-shell runner."""
    from src import project_tests
    return project_tests.run_tests(workspace, spec, timeout_s=timeout)


def _tail(text: str, lines: int = 4, chars: int = 400) -> str:
    rows = [r.strip() for r in (text or "").strip().splitlines() if r.strip()]
    return " | ".join(rows[-lines:])[-chars:]


def _eval_test(goal: Dict[str, Any], workspace: Optional[str], policy: Any, timeout: float) -> Dict[str, Any]:
    cmd = str((goal.get("spec") or {}).get("cmd") or "").strip()
    if not cmd:
        return _skip(goal, "no command declared")
    if not workspace or not os.path.isdir(workspace):
        return _skip(goal, "refused: no workspace is bound to this turn")
    denied = _policy_refusal(policy, "bash", cmd)
    if denied:
        return _skip(goal, f"refused by the turn's tool policy: {denied}")
    try:
        from src import sandbox_exec
        if sandbox_exec.confinement_required():
            return _skip(goal, "refused: sandbox confinement is required and this runner executes on the host")
    except Exception:  # noqa: BLE001 - an unreadable switch must not run code
        return _skip(goal, "refused: sandbox confinement state could not be read")
    spec, why = resolve_test_argv(cmd, workspace)
    if spec is None:
        return _skip(goal, f"refused: {why}")
    started = time.monotonic()
    try:
        res = _run_spec(spec, workspace, timeout) or {}
    except Exception as exc:  # noqa: BLE001
        return _verdict(goal, FAILED, f"could not run: {type(exc).__name__}: {exc}"[:300],
                        duration_s=round(time.monotonic() - started, 1))
    dur = res.get("duration_s")
    if dur is None:
        dur = round(time.monotonic() - started, 1)
    code = res.get("exit_code")
    tail = _tail(res.get("output_tail") or "")
    if res.get("timed_out"):
        return _verdict(goal, FAILED, f"timed out after {int(timeout)}s (killed)", duration_s=dur, timed_out=True)
    if not res.get("ran") and res.get("inconclusive"):
        return _verdict(goal, FAILED, f"could not run: {res.get('summary') or 'runner unavailable'}"[:300],
                        duration_s=dur)
    if code == 0:
        return _verdict(goal, PASSED, f"exit 0 in {dur}s; {res.get('summary') or 'passed'}"[:300],
                        duration_s=dur, exit_code=0)
    note = f"exit {code} in {dur}s; {res.get('summary') or 'failed'}"
    if tail:
        note += f"; last lines: {tail}"
    return _verdict(goal, FAILED, note[:700], duration_s=dur, exit_code=code)


# ---------------------------------------------------------------------------
# http_ok
# ---------------------------------------------------------------------------

def _http_probe(url: str, timeout: float) -> Dict[str, Any]:
    """The seam tests replace. One bounded GET through the public-profile
    broker `web_fetch` uses. Returns {"status": int} or {"refused": reason}
    or {"error": text}."""
    from src import outbound_fetch as ofx
    try:
        ofx.classify_destination(url, profile=ofx.PUBLIC_UNTRUSTED, allow_local=False)
    except ofx.OutboundPolicyError as exc:
        return {"refused": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        got = ofx.fetch(url, profile=ofx.PUBLIC_UNTRUSTED, timeout=timeout, max_redirects=3)
        return {"status": int(got.status_code)}
    except Exception as exc:  # noqa: BLE001 - any transport failure is "not ok", not a crash
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def _eval_http(goal: Dict[str, Any], policy: Any, timeout: float) -> Dict[str, Any]:
    spec = goal.get("spec") or {}
    url = str(spec.get("url") or "").strip()
    if not url:
        return _skip(goal, "no URL declared")
    if not re.match(r"^https?://", url, re.I):
        return _skip(goal, "refused: only http(s) URLs are checked")
    denied = _policy_refusal(policy, "web_fetch", url)
    if denied:
        return _skip(goal, f"refused by the turn's tool policy: {denied}")
    started = time.monotonic()
    got = _http_probe(url, min(timeout, DEFAULT_HTTP_TIMEOUT_S))
    dur = round(time.monotonic() - started, 1)
    if got.get("refused"):
        return _skip(goal, "refused by the agent's outbound HTTP policy (public hosts only): "
                           + str(got["refused"])[:200])
    if got.get("error"):
        return _verdict(goal, FAILED, f"GET {url} failed: {got['error']}"[:400], duration_s=dur)
    status = int(got.get("status") or 0)
    expect = spec.get("expect")
    ok = (status == int(expect)) if expect else (200 <= status < 400)
    want = str(expect) if expect else "2xx/3xx"
    return _verdict(goal, PASSED if ok else FAILED, f"GET {url} -> {status} (expected {want}) in {dur}s",
                    duration_s=dur, status_code=status)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _label(goal: Dict[str, Any]) -> str:
    spec = goal.get("spec") or {}
    return str(spec.get("cmd") or spec.get("url") or spec.get("text") or goal.get("raw") or "")[:160]


def _verdict(goal: Dict[str, Any], status: str, evidence: str, **extra: Any) -> Dict[str, Any]:
    out = {"id": goal.get("id"), "kind": goal.get("kind"), "target": _label(goal),
           "status": status, "evidence": evidence}
    out.update(extra)
    return out


def _skip(goal: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return _verdict(goal, SKIPPED, reason, reason=reason)


def evaluate_goals(
    goals: List[Dict[str, Any]], *, workspace: Optional[str], policy: Any = None,
    timeout_s: Optional[float] = None, total_budget_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Run every goal. Blocking (call it off the event loop). Never raises:
    a checker that blows up fails its own goal. The report:
    `{ok, total, passed, failed, skipped, results[], checked_at, unverified}`
    where `ok` is "nothing failed" and `unverified` is the skipped count."""
    per_goal = goal_timeout_s() if timeout_s is None else max(1.0, min(float(timeout_s), MAX_GOAL_TIMEOUT_S))
    budget = TOTAL_BUDGET_S if total_budget_s is None else float(total_budget_s)
    deadline = time.monotonic() + budget
    results: List[Dict[str, Any]] = []
    for goal in goals or []:
        if not isinstance(goal, dict):
            continue
        kind = str(goal.get("kind") or "")
        if kind not in EXECUTABLE_KINDS:
            results.append(_skip(goal, f"not checkable automatically: kind {kind!r} is not run by plan_done"
                                       if kind else "not checkable automatically: goal has no kind"))
            continue
        remaining = deadline - time.monotonic()
        if remaining < MIN_GOAL_TIMEOUT_S:
            results.append(_skip(goal, "total time budget for this plan_done call is exhausted"))
            continue
        timeout = min(per_goal, remaining)
        try:
            if kind == "test_passes":
                results.append(_eval_test(goal, workspace, policy, timeout))
            else:
                results.append(_eval_http(goal, policy, timeout))
        except Exception as exc:  # noqa: BLE001
            logger.warning("plan goal %s crashed: %s", goal.get("id"), exc)
            results.append(_verdict(goal, FAILED, f"checker error: {type(exc).__name__}: {exc}"[:300]))
    passed = sum(1 for r in results if r["status"] == PASSED)
    failed = sum(1 for r in results if r["status"] == FAILED)
    skipped = sum(1 for r in results if r["status"] == SKIPPED)
    return {
        "ok": failed == 0, "total": len(results), "passed": passed, "failed": failed,
        "skipped": skipped, "unverified": skipped, "results": results, "checked_at": time.time(),
    }


def format_failures(report: Dict[str, Any]) -> str:
    """The text the model reads when a task is not sealed."""
    lines = []
    for r in report.get("results") or []:
        if r.get("status") == FAILED:
            lines.append(f"- [failed] {r.get('kind')} `{r.get('target')}`: {r.get('evidence')}")
    for r in report.get("results") or []:
        if r.get("status") == SKIPPED:
            lines.append(f"- [skipped, NOT verified] {r.get('kind')} `{r.get('target')}`: {r.get('evidence')}")
    return "\n".join(lines)
