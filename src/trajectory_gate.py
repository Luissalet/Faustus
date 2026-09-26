"""src/trajectory_gate.py — declarative CI-style assertions over one recorded
agent run (a "trajectory gate").

`src/agent_runs.py` already persists every detached run's SSE events to
``DATA_DIR/runs/<session_id>.jsonl`` (one file per session, overwritten at
the start of each new run — see ``_RunLog.__init__``, opened with mode
``"w"``: a session's log file always holds exactly its MOST RECENT run) and
``src/llm_trace.py`` separately keeps an append-only per-session log of every
model call. Neither file was ever meant to be asserted against mechanically:
a human reads the replay, or ``src/scorecard.py`` folds many runs into one
reliability number. This module is the missing middle step — load ONE run
into a small normalized shape (an ordered list of steps: a model call, a
tool call, a tool result, an error, or the final answer) and check it
against a declarative spec, the same way a test suite asserts against code.

Why "run id" here means the session id
---------------------------------------
Because a session's log file is overwritten at the start of each run, that
file's contents ARE one run — its own opaque ``run_id`` (a uuid4, minted per
``_Run.__init__``) lives only inside the log's own "running"/finish meta
lines, not in the filename. Every existing agent-run HTTP surface
(``routes/chat_routes.py``'s stop/pause/steer endpoints, ``routes/
observability_routes.py``) already keys off the *session id* for exactly
this reason. So does this module: ``load_trajectory(run_id)`` treats its
argument as a session id first (the common case — "the run I just watched
in this chat"), and only falls back to scanning every persisted log for a
matching internal ``run_id`` meta field when the direct lookup misses (a
caller that copied the opaque id off an SSE event rather than the session
id it came from). Either spelling resolves to the same trajectory.

Steps and timing
-----------------
Tool events in the log (``tool_start``/``tool_output``/``message_saved``,
plus any ``event: error`` frame) carry no per-event wall-clock timestamp —
only the run's start ("running" meta line) and finish (the terminal status
line) do. Model-call steps, read separately from ``src/llm_trace.py``, DO
carry a real ``ts`` and ``duration_ms``, but that log is not scoped per run
(a session's llm_trace file spans every run of that session) — a model-call
record is attributed to this run when it falls inside the run's
[start_ts, finish_ts] window. Given that asymmetry, event-derived steps keep
their exact FILE ORDER (which is exact — the log is append-only, written in
the order things happened) and are given a synthetic timestamp by linear
interpolation across the run's start/finish window purely so model-call
steps (which have a real timestamp) can be merged into the same ordered
list. Checks that only care about tool-step order (``require_observation_
before``, ``no_repeated_identical_calls``) never depend on the synthetic
timestamps — only ``max_duration_s`` and any check mixing model calls with
tool order would, and duration itself prefers the run's own real wall-clock
window over summed synthetic gaps whenever the window is known.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


#: Built-in fallback used by the GET route and the CLI's ``--recent`` mode
#: whenever setting ``trajectory_gate_default_spec`` is unset/empty — loose
#: enough not to fail a normal run, tight enough to catch a genuinely
#: runaway one (a tool-call storm, a silent full failure, no answer at all).
DEFAULT_SPEC: Dict[str, Any] = {
    "max_error_rate": 0.5,
    "max_steps": 300,
    "max_tool_calls": 200,
    "max_blocked_calls": 10,
    "no_repeated_identical_calls": 8,
    "final_answer_required": True,
}


def default_spec() -> Dict[str, Any]:
    configured = _setting("trajectory_gate_default_spec", {}) or {}
    if isinstance(configured, dict) and configured:
        return dict(configured)
    return dict(DEFAULT_SPEC)


# ---------------------------------------------------------------------------
# normalized trajectory
# ---------------------------------------------------------------------------

STEP_KINDS = ("model_call", "tool_call", "tool_result", "error", "final")


@dataclass
class Step:
    kind: str
    tool: str = ""
    ok: Optional[bool] = None
    blocked: Optional[bool] = None
    duration_ms: Optional[float] = None
    tokens: Optional[int] = None
    ts: Optional[float] = None
    detail: str = ""
    #: identity used by no_repeated_identical_calls — tool + a best-effort
    #: signature of its arguments (the "command"/"full_command" the log
    #: already carries for a tool_start; empty for anything else).
    signature: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "tool": self.tool, "ok": self.ok, "blocked": self.blocked,
            "duration_ms": self.duration_ms, "tokens": self.tokens, "ts": self.ts,
            "detail": self.detail,
        }


@dataclass
class Trajectory:
    run_id: str
    session_id: str
    status: str = "unknown"
    start_ts: Optional[float] = None
    finish_ts: Optional[float] = None
    final_text: str = ""
    steps: List[Step] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id, "session_id": self.session_id, "status": self.status,
            "start_ts": self.start_ts, "finish_ts": self.finish_ts,
            "final_text_chars": len(self.final_text),
            "step_count": len(self.steps),
            "steps": [s.as_dict() for s in self.steps],
        }


# ---------------------------------------------------------------------------
# loading — session log (tool/error/final steps)
# ---------------------------------------------------------------------------

def _resolve_session_id(run_id: str) -> Optional[str]:
    """`run_id` as a session id first (the log file exists directly under
    that name); otherwise scan every persisted log for a matching internal
    run_id meta field (mtime-newest first, same bound `agent_runs.
    trace_for_call` already applies to its own session-less scan)."""
    from src import agent_runs
    direct = agent_runs._log_path(run_id)
    if os.path.isfile(direct):
        return run_id
    try:
        d = agent_runs._runs_dir()
        names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
    except OSError:
        return None
    names.sort(key=lambda n: os.path.getmtime(os.path.join(d, n)), reverse=True)
    for name in names[:agent_runs._TRACE_SCAN_MAX_FILES]:
        path = os.path.join(d, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if obj.get("run_id") == run_id:
                        return obj.get("session_id") or name[:-6]
                    break  # only the first ("running" meta) line names run_id
        except OSError:
            continue
    return None


def _tool_signature(payload: Dict[str, Any]) -> str:
    cmd = payload.get("full_command") or payload.get("command") or ""
    return f"{payload.get('tool') or ''}::{str(cmd)[:400]}"


def _event_payload(ev: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """`(is_error_frame, payload)` for one raw SSE chunk from the replay log.
    `data: [DONE]` and anything that is not JSON both come back as
    `(False, None)` — nothing this module cares about."""
    is_error = False
    for line in ev.splitlines():
        if line.startswith("event:"):
            is_error = line[6:].strip() == "error"
            continue
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            return is_error, None
        try:
            data = json.loads(raw)
        except ValueError:
            return is_error, None
        return is_error, data if isinstance(data, dict) else None
    return is_error, None


def _steps_from_events(events: List[str]) -> Tuple[List[Step], str]:
    from src.tool_result import normalize_tool_result

    steps: List[Step] = []
    text_parts: List[str] = []
    for ev in events:
        is_error, payload = _event_payload(ev)
        if payload is None:
            continue
        if is_error or payload.get("error"):
            steps.append(Step(kind="error", detail=str(payload.get("error") or payload.get("text") or "error")[:300]))
            continue
        if "delta" in payload and not payload.get("type"):
            if not payload.get("thinking"):
                text_parts.append(str(payload["delta"]))
            continue
        etype = payload.get("type")
        if etype == "tool_start":
            steps.append(Step(
                kind="tool_call", tool=str(payload.get("tool") or ""),
                detail=str(payload.get("command") or "")[:200],
                signature=_tool_signature(payload),
            ))
        elif etype == "tool_output":
            typed = normalize_tool_result(payload)
            steps.append(Step(
                kind="tool_result", tool=str(payload.get("tool") or ""),
                ok=typed.status == "succeeded",
                blocked=typed.status == "denied",
                detail=typed.status,
                signature=_tool_signature(payload),
            ))
        elif etype == "message_saved":
            steps.append(Step(kind="final", detail=str(payload.get("id") or "")))
    return steps, "".join(text_parts)


def _model_call_steps(
    session_id: str, run_id: Optional[str], start_ts: Optional[float], finish_ts: Optional[float],
) -> List[Step]:
    """Model-call steps for this run.

    `src/llm_trace.py`'s per-session log spans every run of that session, so
    a record is attributed by an exact `run_id` match when the record HAS
    one (`record_call` now threads it through `src.llm_trace.current_run_id`
    — see that module) — a short run that overlaps another run of the same
    session (two tabs, an immediate regeneration) is no longer misattributed
    by the clock window alone. Records with no `run_id` (calls made outside
    a `stream_agent_loop` context, or older traces from before this) still
    fall back to the run's own time window, the honest approximation for
    those."""
    from src import llm_trace

    lo = start_ts if start_ts is not None else 0.0
    hi = finish_ts if finish_ts is not None else time.time() + 1.0
    out: List[Step] = []
    for rec in llm_trace._iter_records(session_id):
        ts = rec.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        rec_run_id = rec.get("run_id")
        if rec_run_id:
            if not run_id or rec_run_id != run_id:
                continue  # exact attribution available and it says "not ours"
        elif not (lo - 1.0 <= ts <= hi + 1.0):
            continue  # no run_id recorded -> fall back to the clock window
        usage = rec.get("usage") or {}
        tokens = usage.get("total_tokens")
        if not isinstance(tokens, int):
            prompt = usage.get("prompt_tokens") or 0
            completion = usage.get("completion_tokens") or 0
            tokens = (prompt + completion) if (prompt or completion) else None
        out.append(Step(
            kind="model_call", tool=str(rec.get("model") or ""),
            ok=not bool(rec.get("error")),
            duration_ms=rec.get("duration_ms"),
            tokens=tokens, ts=float(ts),
            detail=str(rec.get("finish_reason") or ""),
        ))
    return out


def load_trajectory(run_id: str) -> Trajectory:
    """Load and normalize the trajectory for `run_id` (see module docstring
    for the run_id/session_id equivalence). Raises `LookupError` if nothing
    on disk matches."""
    from src import agent_runs

    session_id = _resolve_session_id(run_id)
    if not session_id:
        raise LookupError(f"no run found for {run_id!r}")
    log = agent_runs._read_log(agent_runs._log_path(session_id))
    if log.get("status") == "unreadable" and not log.get("events"):
        raise LookupError(f"no run found for {run_id!r}")

    start_ts = log.get("ts")
    finish_ts = None
    # `_read_log` only surfaces the "running" meta line's fields; the
    # terminal status line's own `ts` (finish time) is read directly here —
    # cheap (one more pass of an already-small file) and avoids reshaping
    # `_read_log`'s return contract for one extra field.
    try:
        with open(agent_runs._log_path(session_id), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("status") and obj["status"] != "running":
                    finish_ts = obj.get("ts")
    except OSError:
        pass

    # Read ahead of `_model_call_steps` (moved up from where this used to be
    # computed, right before the `Trajectory(...)` call below) so the exact
    # `run_id` this log's own "running" meta line names is available for
    # exact `model_call` attribution, not just for the returned `Trajectory`.
    resolved_run_id = None
    if os.path.isfile(agent_runs._log_path(session_id)):
        try:
            with open(agent_runs._log_path(session_id), "r", encoding="utf-8", errors="replace") as f:
                first = f.readline()
            resolved_run_id = json.loads(first).get("run_id") if first.strip() else None
        except (OSError, ValueError):
            pass

    event_steps, final_text = _steps_from_events(log.get("events") or [])
    model_steps = _model_call_steps(session_id, resolved_run_id or run_id, start_ts, finish_ts)

    # Interleave: event steps keep exact file order (that IS chronological —
    # the log is append-only); each is given a synthetic ts by linear
    # interpolation across [start_ts, finish_ts] purely so model_steps (real
    # ts) can be merged in without reordering the event steps relative to
    # each other. See module docstring.
    n = max(1, len(event_steps))
    if start_ts is not None and finish_ts is not None and finish_ts > start_ts:
        for i, s in enumerate(event_steps):
            s.ts = start_ts + (finish_ts - start_ts) * (i / n)
    else:
        base = start_ts or 0.0
        for i, s in enumerate(event_steps):
            s.ts = base + i * 0.001

    all_steps = sorted(event_steps + model_steps, key=lambda s: (s.ts if s.ts is not None else 0.0))

    status = log.get("status") or "unknown"

    return Trajectory(
        run_id=resolved_run_id or run_id, session_id=session_id, status=str(status),
        start_ts=start_ts, finish_ts=finish_ts, final_text=final_text, steps=all_steps,
    )


# ---------------------------------------------------------------------------
# spec checks
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    id: str
    ok: bool
    expected: Any
    actual: Any
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "ok": self.ok, "expected": self.expected, "actual": self.actual, "detail": self.detail}


def _tool_calls(traj: Trajectory) -> List[Step]:
    return [s for s in traj.steps if s.kind == "tool_call"]


def _tool_results(traj: Trajectory) -> List[Step]:
    return [s for s in traj.steps if s.kind == "tool_result"]


def _check_max_error_rate(traj: Trajectory, limit: Any) -> CheckResult:
    total = len(traj.steps) or 1
    errors = sum(1 for s in traj.steps if s.kind == "error" or (s.kind == "tool_result" and s.ok is False and not s.blocked)
                 or (s.kind == "model_call" and s.ok is False))
    rate = errors / total
    return CheckResult("max_error_rate", rate <= float(limit), limit, round(rate, 4),
                        f"{errors}/{total} steps were errors")


def _check_max_steps(traj: Trajectory, limit: Any) -> CheckResult:
    n = len(traj.steps)
    return CheckResult("max_steps", n <= int(limit), limit, n)


def _check_max_tool_calls(traj: Trajectory, limit: Any) -> CheckResult:
    n = len(_tool_calls(traj))
    return CheckResult("max_tool_calls", n <= int(limit), limit, n)


def _check_max_duration_s(traj: Trajectory, limit: Any) -> CheckResult:
    if traj.start_ts is not None and traj.finish_ts is not None and traj.finish_ts >= traj.start_ts:
        actual = traj.finish_ts - traj.start_ts
        detail = "wall-clock run duration"
    else:
        actual = sum((s.duration_ms or 0.0) for s in traj.steps) / 1000.0
        detail = "summed model-call durations (no run wall-clock window)"
    return CheckResult("max_duration_s", actual <= float(limit), limit, round(actual, 3), detail)


def _check_max_tokens(traj: Trajectory, limit: Any) -> CheckResult:
    total = sum((s.tokens or 0) for s in traj.steps if s.kind == "model_call")
    return CheckResult("max_tokens", total <= int(limit), limit, total)


def _check_forbid_tools(traj: Trajectory, patterns: Any) -> CheckResult:
    patterns = list(patterns or [])
    hits = sorted({s.tool for s in _tool_calls(traj) if any(fnmatch.fnmatch(s.tool, p) for p in patterns)})
    return CheckResult("forbid_tools", not hits, patterns, hits,
                        f"forbidden tool(s) called: {', '.join(hits)}" if hits else "")


def _check_require_tools(traj: Trajectory, required: Any) -> CheckResult:
    required = list(required or [])
    called = {s.tool for s in _tool_calls(traj)}
    missing = [r for r in required if not any(fnmatch.fnmatch(c, r) for c in called)]
    return CheckResult("require_tools", not missing, required, sorted(missing),
                        f"never called: {', '.join(missing)}" if missing else "")


def _check_require_observation_before(traj: Trajectory, rules: Any) -> CheckResult:
    """Each rule is one of:
      {"tool": "<pattern>", "requires_before": ["<pattern>", ...]}
        — every call matching `tool` must have an earlier tool_call step
          matching at least one `requires_before` pattern.
      {"after_last_of": "<pattern>", "requires_after": ["<pattern>", ...]}
        — after the LAST call matching `after_last_of` (if any occurred),
          at least one later tool_call step must match a `requires_after`
          pattern (e.g. "tests must run after edits").
    """
    rules = list(rules or [])
    calls = _tool_calls(traj)
    violations: List[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if "tool" in rule:
            pat = rule["tool"]
            before_pats = list(rule.get("requires_before") or [])
            for i, s in enumerate(calls):
                if not fnmatch.fnmatch(s.tool, pat):
                    continue
                earlier = calls[:i]
                if not any(any(fnmatch.fnmatch(e.tool, bp) for bp in before_pats) for e in earlier):
                    violations.append(f"{s.tool} (step {i}) had no prior call matching {before_pats}")
        elif "after_last_of" in rule:
            pat = rule["after_last_of"]
            after_pats = list(rule.get("requires_after") or [])
            last_idx = None
            for i, s in enumerate(calls):
                if fnmatch.fnmatch(s.tool, pat):
                    last_idx = i
            if last_idx is not None:
                later = calls[last_idx + 1:]
                if not any(any(fnmatch.fnmatch(l.tool, ap) for ap in after_pats) for l in later):
                    violations.append(f"no call matching {after_pats} after last {pat} (step {last_idx})")
    return CheckResult("require_observation_before", not violations, rules, violations,
                        "; ".join(violations) if violations else "")


def _check_no_repeated_identical_calls(traj: Trajectory, limit: Any) -> CheckResult:
    counts: Dict[str, int] = {}
    for s in _tool_calls(traj):
        if not s.signature:
            continue
        counts[s.signature] = counts.get(s.signature, 0) + 1
    worst_sig, worst_n = max(counts.items(), key=lambda kv: kv[1]) if counts else ("", 0)
    return CheckResult("no_repeated_identical_calls", worst_n <= int(limit), limit, worst_n,
                        f"{worst_sig.split('::')[0]!r} repeated {worst_n} times identically" if worst_n > int(limit) else "")


def _check_final_answer_required(traj: Trajectory, required: Any) -> CheckResult:
    if not required:
        return CheckResult("final_answer_required", True, required, True, "check disabled")
    has_final = any(s.kind == "final" for s in traj.steps) or bool(traj.final_text.strip())
    return CheckResult("final_answer_required", has_final, True, has_final,
                        "" if has_final else "no message_saved event and no accumulated text")


def _check_max_blocked_calls(traj: Trajectory, limit: Any) -> CheckResult:
    n = sum(1 for s in _tool_results(traj) if s.blocked)
    return CheckResult("max_blocked_calls", n <= int(limit), limit, n)


_CHECKS = {
    "max_error_rate": _check_max_error_rate,
    "max_steps": _check_max_steps,
    "max_tool_calls": _check_max_tool_calls,
    "max_duration_s": _check_max_duration_s,
    "max_tokens": _check_max_tokens,
    "forbid_tools": _check_forbid_tools,
    "require_tools": _check_require_tools,
    "require_observation_before": _check_require_observation_before,
    "no_repeated_identical_calls": _check_no_repeated_identical_calls,
    "final_answer_required": _check_final_answer_required,
    "max_blocked_calls": _check_max_blocked_calls,
}

#: The declarative spec keys this module understands, in a stable order so a
#: report's `checks` list is always in the same order regardless of dict
#: iteration order in the spec the caller supplied.
SPEC_KEYS = tuple(_CHECKS.keys())


def parse_spec(text: str, *, filename: str = "") -> Dict[str, Any]:
    """JSON, or YAML(-lite) when `text` is not valid JSON — accepts a plain
    dict of the keys in `SPEC_KEYS`. `filename` is only used to prefer YAML
    parsing for a `.yml`/`.yaml` extension; either format is tried either
    way."""
    text = text or "{}"
    prefer_yaml = filename.lower().endswith((".yml", ".yaml"))
    if not prefer_yaml:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    try:
        import yaml
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    raise ValueError("spec is neither valid JSON nor valid YAML")


def evaluate(trajectory: Trajectory, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Pure: run every check named in `spec` against an already-loaded
    trajectory. Unknown spec keys are ignored (forward-compatible — a spec
    written for a newer gate still runs its known checks)."""
    checks: List[CheckResult] = []
    for key in SPEC_KEYS:
        if key not in spec:
            continue
        checks.append(_CHECKS[key](trajectory, spec[key]))
    ok = all(c.ok for c in checks)
    return {
        "ok": ok,
        "run_id": trajectory.run_id,
        "session_id": trajectory.session_id,
        "status": trajectory.status,
        "step_count": len(trajectory.steps),
        "checks": [c.as_dict() for c in checks],
    }


def evaluate_run(run_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    return evaluate(load_trajectory(run_id), spec)


def _recent_run_ids(limit: int, since: Optional[float]) -> List[str]:
    from src import agent_runs
    d = agent_runs._runs_dir()
    try:
        names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
    except OSError:
        return []
    rows = []
    for n in names:
        path = os.path.join(d, n)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if since is not None and mtime < since:
            continue
        rows.append((mtime, n[:-6]))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [session_id for _, session_id in rows[:max(0, int(limit))]]


def evaluate_recent(spec: Dict[str, Any], limit: int = 20, since: Optional[float] = None) -> Dict[str, Any]:
    """Aggregate pass rate per check across the `limit` most recently
    touched persisted runs (optionally only those touched at/after `since`,
    a unix timestamp). A run whose log cannot be loaded is counted as
    `errored`, not silently skipped — an unreadable/corrupt log is itself
    a signal a gate should surface."""
    run_ids = _recent_run_ids(limit, since)
    reports: List[Dict[str, Any]] = []
    load_errors = 0
    for rid in run_ids:
        try:
            reports.append(evaluate_run(rid, spec))
        except Exception as exc:
            load_errors += 1
            logger.debug("[trajectory_gate] could not load run %s: %s", rid, exc)

    per_check: Dict[str, Dict[str, int]] = {}
    for rep in reports:
        for c in rep["checks"]:
            bucket = per_check.setdefault(c["id"], {"pass": 0, "fail": 0})
            bucket["pass" if c["ok"] else "fail"] += 1

    per_check_rate = {
        cid: (round(v["pass"] / (v["pass"] + v["fail"]), 4) if (v["pass"] + v["fail"]) else None)
        for cid, v in per_check.items()
    }
    overall_pass = sum(1 for r in reports if r["ok"])
    return {
        "runs_evaluated": len(reports),
        "runs_unreadable": load_errors,
        "runs_passed": overall_pass,
        "runs_failed": len(reports) - overall_pass,
        "pass_rate": round(overall_pass / len(reports), 4) if reports else None,
        "per_check_pass_rate": per_check_rate,
        "reports": reports,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _report_text(report: Dict[str, Any]) -> str:
    lines = [f"run {report['run_id']} (session {report['session_id']}, status={report['status']}, "
             f"{report['step_count']} steps) — {'PASS' if report['ok'] else 'FAIL'}"]
    for c in report["checks"]:
        mark = "PASS" if c["ok"] else "FAIL"
        lines.append(f"  [{mark}] {c['id']}: expected={c['expected']!r} actual={c['actual']!r}"
                     + (f" — {c['detail']}" if c["detail"] else ""))
    return "\n".join(lines)


def _recent_report_text(agg: Dict[str, Any]) -> str:
    lines = [f"{agg['runs_evaluated']} run(s) evaluated ({agg['runs_unreadable']} unreadable), "
             f"{agg['runs_passed']} passed, {agg['runs_failed']} failed "
             f"(pass_rate={agg['pass_rate']})"]
    for cid, rate in agg["per_check_pass_rate"].items():
        lines.append(f"  {cid}: pass_rate={rate}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", help="a run id (or its owning session id) to evaluate")
    parser.add_argument("--recent", type=int, help="evaluate the N most recently touched runs and aggregate")
    parser.add_argument("--since", type=float, default=None, help="with --recent: only runs touched at/after this unix timestamp")
    parser.add_argument("--spec", required=True, help="path to a JSON or YAML spec file")
    parser.add_argument("--json", action="store_true", help="print the machine-readable report instead of text")
    args = parser.parse_args(argv)

    with open(args.spec, "r", encoding="utf-8") as f:
        spec = parse_spec(f.read(), filename=args.spec)

    if args.run:
        try:
            report = evaluate_run(args.run, spec)
        except LookupError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(report, indent=2) if args.json else _report_text(report))
        return 0 if report["ok"] else 1

    if args.recent is not None:
        agg = evaluate_recent(spec, limit=args.recent, since=args.since)
        print(json.dumps(agg, indent=2) if args.json else _recent_report_text(agg))
        return 0 if (agg["runs_failed"] == 0 and agg["runs_unreadable"] == 0) else 1

    parser.error("one of --run or --recent is required")
    return 2  # pragma: no cover - argparse.error exits before this


if __name__ == "__main__":
    sys.exit(main())
