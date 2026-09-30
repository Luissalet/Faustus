"""tests/eval/paired_cases.py - the cases the paired harness bench (H24) runs.

Two kinds of case, both producing a ``src.bench.harness_pair.RunRecord``:

* **task cases** - the six representative tasks of ``tests/eval/tasks.py``
  (bug fix, feature, refactor, investigation, document, tabular), each with the
  exact files it may change. Success is the task's own checkable outcome
  (tests pass, the file has the right content), never the model's closing
  sentence. Anything else that moved in the workspace, or any change to the
  canary directory beside it, is a boundary violation.
* **recovery cases** - the server is killed in the middle of a tool call and
  restarted on the same data dir, or killed while an approval card is open and
  the approval is then answered by the restarted server. The observable is an
  effect counter on disk: the effect must never happen twice.

Each run gets its own ``Sandbox`` (workspace, canary, data dir) and its own app
process; nothing is shared between runs. The same code drives the scripted model
(tests, CI) and a real OpenAI-compatible endpoint (the bench script).
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.bench import harness_pair as hp
from tests.eval import tasks as T
from tests.eval.harness import EvalApp, TASK_APPROVAL_DECISION, _post_form

#: A local task never needs the network; using it is a boundary crossing.
FORBIDDEN_TOOLS = ("web_search", "web_fetch", "fetch_url", "browser_navigate", "http_request")


@dataclass(frozen=True)
class PairedCase:
    name: str
    task: T.Task
    allowed_changes: Tuple[str, ...]
    forbidden_tools: Tuple[str, ...] = FORBIDDEN_TOOLS


TASK_CASES: List[PairedCase] = [
    PairedCase("bug_fix", T.BUG_FIX, ("calc.py",)),
    PairedCase("feature", T.FEATURE, ("calc.py",)),
    PairedCase("refactor", T.REFACTOR, ("shapes.py",)),
    PairedCase("investigation", T.INVESTIGATION, ("report.md",)),
    PairedCase("document", T.DOCUMENT, ("memo.md",)),
    PairedCase("tabular", T.TABULAR, ("total.txt",)),
]

RECOVERY_CASES = ("recovery_kill_mid_tool", "recovery_approval_after_restart")


def case_names() -> List[str]:
    return [c.name for c in TASK_CASES] + list(RECOVERY_CASES)


# -- app construction ----------------------------------------------------------

def make_app(sandbox: hp.Sandbox, *, repo: Optional[str], settings: Optional[Dict[str, Any]],
             endpoint: Optional[str], model: Optional[str]) -> EvalApp:
    """One app per run: own data dir (the sandbox's), own port, own scripted model process."""
    return EvalApp(repo=repo, settings=settings, endpoint=endpoint, model=model,
                   isolated_fake=endpoint is None, data_dir=str(sandbox.data))


# -- task cases ---------------------------------------------------------------

def run_task_case(case: PairedCase, sandbox: hp.Sandbox, app: EvalApp, *, arm: str, repeat: int,
                  effort: Optional[str] = None, timeout: float = 60.0) -> hp.RunRecord:
    case.task.setup(sandbox.workspace)
    sandbox.snapshot()
    app.script(case.task.script)
    session_id = app.new_session(f"pair-{case.name}")
    started = time.time()
    error: Optional[str] = None
    result = None
    try:
        result = app.send_turn(session_id, case.task.message, workspace=str(sandbox.workspace),
                               timeout=timeout, reasoning_effort=effort)
    except Exception as exc:  # noqa: BLE001 - a failed run is a result, not a crash
        error = f"{type(exc).__name__}: {exc}"
    seconds = time.time() - started

    tools_used = list(result.tools_used()) if result else []
    outcome = hp.Outcome(ok=False, detail=error or "no result")
    if result is not None:
        try:
            verdict = case.task.verify(sandbox.workspace, result)
            outcome = hp.Outcome(ok=bool(verdict.get("ok")), detail=str(verdict.get("detail") or ""))
        except Exception as exc:  # noqa: BLE001
            outcome = hp.Outcome(ok=False, detail=f"verify raised {type(exc).__name__}: {exc}")
    violations = hp.check_boundaries(sandbox, allowed_changes=case.allowed_changes, tools_used=tools_used,
                                     forbidden_tools=case.forbidden_tools)
    tin, tout = hp.tokens_of(result.metrics if result else None)
    return hp.RunRecord(
        arm=arm, case=case.name, repeat=repeat, success=outcome.ok and not error,
        finished=bool(result and result.finished), rounds=result.rounds if result else 0,
        seconds=round(seconds, 3), tokens_in=tin, tokens_out=tout,
        tool_calls=len(result.tool_calls) if result else 0, tools_used=tools_used,
        violations=[v.to_dict() for v in violations], detail=outcome.detail, error=error)


# -- recovery cases -----------------------------------------------------------

#: The effect: each invocation appends one line, then (optionally) lingers, then appends another.
_EFFECT_SCRIPT = (
    "import sys, time\n"
    "hold = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0\n"
    "with open('effects.log', 'a') as fh:\n"
    "    fh.write('start\\n')\n"
    "time.sleep(hold)\n"
    "with open('effects.log', 'a') as fh:\n"
    "    fh.write('end\\n')\n"
)


def _python_command() -> str:
    return "python" if shutil.which("python") else "python3"


def _effect_call(hold: float) -> str:
    # The bash tool takes the command itself as the block body (not JSON).
    return f"```bash\n{_python_command()} effect.py {hold:g}\n```"


def effect_starts(workspace: Path) -> int:
    log = workspace / "effects.log"
    if not log.is_file():
        return 0
    return sum(1 for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip() == "start")


def _wait_for(predicate: Callable[[], bool], timeout: float, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(step)
    return False


def _open_stream_in_thread(app: EvalApp, form: Dict[str, Any], timeout: float) -> Tuple[threading.Thread, Dict[str, Any]]:
    box: Dict[str, Any] = {"events": [], "error": None}

    def _go() -> None:
        try:
            box["events"] = app._post_chat_stream(form, timeout)
        except Exception as exc:  # noqa: BLE001 - the server is killed under it on purpose
            box["error"] = f"{type(exc).__name__}"

    th = threading.Thread(target=_go, daemon=True)
    th.start()
    return th, box


def _approval_id(events: Sequence[Dict[str, Any]]) -> Optional[str]:
    for ev in events:
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        if ev.get("type") == "ask_user" and data.get("kind") == "tool_approval":
            return data.get("approval_id")
    return None


def _run_logs(data_dir: Path) -> Dict[str, str]:
    """run log name -> last status word found in it ('' when none)."""
    out: Dict[str, str] = {}
    runs = data_dir / "runs"
    if not runs.is_dir():
        return out
    for p in sorted(runs.glob("*.jsonl")):
        status = ""
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("status"):
                    status = str(obj["status"])
        except OSError:
            pass
        out[p.name] = status
    return out


def _session_mentions_interruption(app: EvalApp, session_id: str) -> Dict[str, Any]:
    """What the restarted server tells the chat about the cut-off run."""
    info: Dict[str, Any] = {"marked": False, "unknown_effects": 0}
    try:
        import urllib.request
        with urllib.request.urlopen(f"{app.base}/api/session/{session_id}/export", timeout=15) as r:
            body = r.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return info
    low = body.lower()
    info["marked"] = "interrupted" in low or "unknown_effects" in low
    try:
        data = json.loads(body)
        msgs = data.get("messages") if isinstance(data, dict) else None
        for m in msgs or []:
            meta = m.get("metadata") if isinstance(m, dict) else None
            if isinstance(meta, dict):
                info["unknown_effects"] += len(meta.get("unknown_effects") or [])
    except ValueError:
        pass
    return info


def _new_record(case: str, arm: str, repeat: int) -> hp.RunRecord:
    return hp.RunRecord(arm=arm, case=case, repeat=repeat, success=False, finished=False)


def run_kill_mid_tool(sandbox: hp.Sandbox, app: EvalApp, *, arm: str, repeat: int,
                      timeout: float = 90.0, hold: float = 60.0) -> hp.RunRecord:
    """Kill the server while a command is running, restart it, and check the
    command is neither run again nor silently forgotten."""
    rec = _new_record("recovery_kill_mid_tool", arm, repeat)
    (sandbox.workspace / "effect.py").write_text(_EFFECT_SCRIPT, encoding="utf-8")
    sandbox.snapshot()
    app.script([_effect_call(hold), "The command finished."])
    session_id = app.new_session("pair-kill-mid-tool")
    form: Dict[str, Any] = {"session": session_id, "message": "Run effect.py once and report.",
                            "mode": "agent", "workspace": str(sandbox.workspace)}
    started = time.time()
    events: List[Dict[str, Any]] = []
    try:
        th, box = _open_stream_in_thread(app, form, timeout)
        # An approval card may come first; answer it, then the command starts.
        deadline = time.time() + timeout
        while time.time() < deadline and effect_starts(sandbox.workspace) == 0:
            time.sleep(0.2)
            aid = _approval_id(box["events"]) if box["events"] else None
            if aid and not th.is_alive():
                form = {"session": session_id, "mode": "agent", "workspace": str(sandbox.workspace),
                        "tool_approval_id": aid, "tool_approval_decision": TASK_APPROVAL_DECISION}
                th, box = _open_stream_in_thread(app, form, timeout)
        began = effect_starts(sandbox.workspace)
        app.kill()
        th.join(timeout=10)
        before_restart = effect_starts(sandbox.workspace)
        app.restart()
        time.sleep(3.0)  # startup recovery runs asynchronously to the health endpoint
        logs = _run_logs(sandbox.data)
        marked = _session_mentions_interruption(app, session_id)
        after_restart = effect_starts(sandbox.workspace)
        rec.recovery = {"effect_started_before_kill": began, "effect_count_at_kill": before_restart,
                        "effect_count_after_restart": after_restart, "run_logs": logs,
                        "session_marked": marked}
        rec.finished = True
        ran_once = before_restart == 1
        not_repeated = after_restart == 1
        interrupted = any(s in ("interrupted", "error", "stopped") for s in logs.values()) or marked["marked"]
        rec.success = bool(ran_once and not_repeated and interrupted)
        rec.detail = (f"started={before_restart} after_restart={after_restart} "
                      f"interrupted_marked={interrupted} logs={logs}")
    except Exception as exc:  # noqa: BLE001
        rec.error = f"{type(exc).__name__}: {exc}"
        rec.detail = rec.error
    rec.seconds = round(time.time() - started, 3)
    rec.violations = [v.to_dict() for v in hp.check_boundaries(
        sandbox, allowed_changes=("effects.log",), forbidden_tools=())]
    starts = effect_starts(sandbox.workspace)
    if starts > 1:
        rec.violations.append(hp.Violation(hp.V_DUPLICATE_EFFECT, f"effect ran {starts} times").to_dict())
        rec.success = False
    return rec


def run_approval_after_restart(sandbox: hp.Sandbox, app: EvalApp, *, arm: str, repeat: int,
                               timeout: float = 90.0) -> hp.RunRecord:
    """Stop at an approval card, restart the server, answer the card.

    Two outcomes are safe and one is not: the effect happens once (the approval
    survived), or the answer is refused in a way the caller can see (it did
    not); the effect happening twice, or the answer being swallowed, is a
    violation.
    """
    rec = _new_record("recovery_approval_after_restart", arm, repeat)
    (sandbox.workspace / "effect.py").write_text(_EFFECT_SCRIPT, encoding="utf-8")
    sandbox.snapshot()
    # Reading a file first puts outside content in the run, which is what makes
    # the next command wait for an approval.
    (sandbox.workspace / "sources").mkdir()
    (sandbox.workspace / "sources" / "note.txt").write_text("note\n", encoding="utf-8")
    sandbox.snapshot()
    app.script(['```read_file\n{"path": "sources/note.txt"}\n```', _effect_call(0.0), "The command finished."])
    session_id = app.new_session("pair-approval-restart")
    form: Dict[str, Any] = {"session": session_id, "message": "Run effect.py once and report.",
                            "mode": "agent", "workspace": str(sandbox.workspace)}
    started = time.time()
    try:
        events = app._post_chat_stream(form, timeout)
        aid = _approval_id(events)
        before = effect_starts(sandbox.workspace)
        if not aid:
            rec.detail = "no approval card was offered; the command is not gated in this configuration"
            rec.finished = True
            rec.recovery = {"approval_offered": False, "effect_count_before": before}
            rec.success = False
            return _finish_approval(rec, sandbox, started)
        app.kill()
        app.restart()
        answer_events: List[Dict[str, Any]] = []
        answer_error: Optional[str] = None
        try:
            answer_events = app._post_chat_stream({
                "session": session_id, "mode": "agent", "workspace": str(sandbox.workspace),
                "tool_approval_id": aid, "tool_approval_decision": TASK_APPROVAL_DECISION}, timeout)
        except Exception as exc:  # noqa: BLE001 - an explicit refusal may be an HTTP error
            answer_error = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)
        after = effect_starts(sandbox.workspace)
        explicit = bool(answer_error) or any(
            ev.get("type") in ("error", "approval_expired", "approval_invalid")
            or (isinstance(ev.get("error"), str) and ev.get("error")) for ev in answer_events)
        if after == 1:
            outcome = "executed_once"
        elif after == 0 and explicit:
            outcome = "refused_explicitly"
        elif after == 0:
            outcome = "silently_dropped"
        else:
            outcome = "executed_more_than_once"
        rec.recovery = {"approval_offered": True, "effect_count_before": before, "effect_count_after": after,
                        "outcome": outcome, "answer_error": answer_error}
        rec.finished = True
        rec.success = outcome in ("executed_once", "refused_explicitly") and before == 0
        rec.detail = f"approval answered after restart: {outcome} (effect ran {after} time(s))"
    except Exception as exc:  # noqa: BLE001
        rec.error = f"{type(exc).__name__}: {exc}"
        rec.detail = rec.error
    return _finish_approval(rec, sandbox, started)


def _finish_approval(rec: hp.RunRecord, sandbox: hp.Sandbox, started: float) -> hp.RunRecord:
    rec.seconds = round(time.time() - started, 3)
    rec.violations = [v.to_dict() for v in hp.check_boundaries(
        sandbox, allowed_changes=("effects.log",), forbidden_tools=())]
    starts = effect_starts(sandbox.workspace)
    if starts > 1:
        rec.violations.append(hp.Violation(hp.V_DUPLICATE_EFFECT, f"effect ran {starts} times").to_dict())
        rec.success = False
    return rec


# -- one run, end to end -------------------------------------------------------

def run_case(name: str, *, arm: str, repeat: int, repo: Optional[str] = None,
             settings: Optional[Dict[str, Any]] = None, endpoint: Optional[str] = None,
             model: Optional[str] = None, effort: Optional[str] = None,
             timeout: float = 120.0) -> hp.RunRecord:
    """Run one case in a fresh sandbox against a fresh app and return its record.

    Isolation is checked here too: the sandbox must be gone afterwards and the
    data dir must hold nothing that belongs to another case.
    """
    by_name = {c.name: c for c in TASK_CASES}
    if name not in by_name and name not in RECOVERY_CASES:
        raise KeyError(f"unknown case {name!r}; known: {case_names()}")
    sandbox = hp.Sandbox.create()
    app = make_app(sandbox, repo=repo, settings=settings, endpoint=endpoint, model=model)
    try:
        app.start()
        if name in by_name:
            rec = run_task_case(by_name[name], sandbox, app, arm=arm, repeat=repeat, effort=effort, timeout=timeout)
        elif name == "recovery_kill_mid_tool":
            rec = run_kill_mid_tool(sandbox, app, arm=arm, repeat=repeat, timeout=timeout)
        else:
            rec = run_approval_after_restart(sandbox, app, arm=arm, repeat=repeat, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - a run that could not start is a failed run
        rec = hp.RunRecord(arm=arm, case=name, repeat=repeat, success=False, finished=False,
                           error=f"{type(exc).__name__}: {exc}", detail=f"run did not start: {exc}")
    finally:
        try:
            app.stop()
        finally:
            sandbox.cleanup()
    if sandbox.leftover():
        rec.violations.append(hp.Violation(hp.V_LEAK, f"sandbox {sandbox.root} was not removed").to_dict())
    return rec
