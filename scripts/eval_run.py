#!/usr/bin/env python3
"""eval_run.py — EVAL-01/BENCH-02 "measure": run the representative-task
suite and, on request, compare it against the saved baseline.

Two modes:

  python scripts/eval_run.py
      Runs `tests/eval/tasks.py` against a scripted, recorded model (the
      same `tests/eval/harness.py` the pytest suite uses) — no live model
      needed, safe to run anywhere. Prints one line per task (ok/fail,
      rounds, tool calls, tokens) and writes the full result as JSON.

  python scripts/eval_run.py --live --endpoint-url URL --model NAME [--api-key KEY]
      Runs the SAME six tasks against a real model endpoint instead of the
      scripted one, and diffs the result against `tests/eval/baseline.json`
      (rounds/tool-calls/tokens per task, plus the verified/ok verdict).
      NOT run by this lot's own verification pass — it needs a real serving
      endpoint the sandbox does not have; see the lot report.

Either mode writes its full result to `--out` (default: a timestamped file
under `logs/eval_runs/`) so a run can be inspected or diffed later without
re-running the suite.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

BASELINE_PATH = REPO / "tests" / "eval" / "baseline.json"


def _task_result(name: str, ok: bool, detail: str, rounds: int, tool_calls: int,
                 tools_used: List[str], tokens: Dict[str, Any]) -> Dict[str, Any]:
    return {"task": name, "ok": ok, "detail": detail, "rounds": rounds,
            "tool_calls": tool_calls, "tools_used": tools_used, "tokens": tokens}


def run_scripted() -> Dict[str, Any]:
    """EVAL-01's "sin modelo" mode: the exact suite `tests/eval/test_representative_tasks.py`
    runs under pytest, driven directly so this script has no pytest dependency."""
    from tests.eval.harness import EvalApp
    from tests.eval import tasks as T

    app = EvalApp()
    print("[eval_run] starting the app + scripted model…")
    app.start()
    results: List[Dict[str, Any]] = []
    try:
        for task in T.ALL_TASKS:
            import tempfile
            ws = Path(tempfile.mkdtemp(prefix=f"eval-run-{task.name}-"))
            task.setup(ws)
            app.script(task.script)
            session_id = app.new_session(f"eval-run-{task.name}")
            result = app.send_turn(session_id, task.message, workspace=str(ws))
            outcome = task.verify(ws, result)
            row = _task_result(task.name, bool(outcome.get("ok")), str(outcome.get("detail") or ""),
                               result.rounds, len(result.tool_calls), list(result.tools_used()),
                               result.metrics)
            results.append(row)
            status = "OK  " if row["ok"] else "FAIL"
            print(f"[eval_run] {status} {task.name:<14} rounds={row['rounds']} "
                 f"tool_calls={row['tool_calls']} tokens={row['tokens'].get('total_tokens')}")
    finally:
        app.stop()
    return {"mode": "scripted", "ts": int(time.time()), "tasks": results,
            "all_ok": all(r["ok"] for r in results)}


def run_live(endpoint_url: str, model: str, api_key: str = "") -> Dict[str, Any]:
    """EVAL-01's `--live` mode: the same six tasks against a real endpoint.

    Deliberately NOT exercised by this lot's own verification (see the lot
    report) — it needs a real serving endpoint the sandbox running this lot
    does not have. Kept here, tested only for the "does it run" shape (see
    `tests/test_eval_baseline.py::test_run_live_is_wired_but_not_exercised`),
    because EVAL-01 explicitly asks for it as the other half of "measure".
    """
    from tests.eval.harness import EvalApp
    from tests.eval import tasks as T
    import urllib.parse
    import urllib.request

    app = EvalApp()
    app.start()
    results: List[Dict[str, Any]] = []
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        ep_body = {"name": "eval-live", "base_url": endpoint_url, "skip_probe": "true",
                  "endpoint_kind": "remote" if api_key else "local"}
        body = urllib.parse.urlencode(ep_body).encode("utf-8")
        req = urllib.request.Request(app.base + "/api/model-endpoints", data=body, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            ep = json.loads(r.read().decode("utf-8"))
        endpoint_id = ep.get("id") or (ep.get("endpoint") or {}).get("id") or ""
        for task in T.ALL_TASKS:
            import tempfile
            ws = Path(tempfile.mkdtemp(prefix=f"eval-live-{task.name}-"))
            task.setup(ws)
            session_body = urllib.parse.urlencode({
                "name": f"eval-live-{task.name}", "endpoint_id": endpoint_id,
                "endpoint_url": endpoint_url, "model": model, "skip_validation": "true",
            }).encode("utf-8")
            req = urllib.request.Request(app.base + "/api/session", data=session_body, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                sess = json.loads(r.read().decode("utf-8"))
            session_id = sess.get("id") or sess.get("session_id")
            result = app.send_turn(session_id, task.message, workspace=str(ws), timeout=180)
            outcome = task.verify(ws, result)
            row = _task_result(task.name, bool(outcome.get("ok")), str(outcome.get("detail") or ""),
                               result.rounds, len(result.tool_calls), list(result.tools_used()),
                               result.metrics)
            results.append(row)
            print(f"[eval_run --live] {'OK  ' if row['ok'] else 'FAIL'} {task.name}")
    finally:
        app.stop()
    return {"mode": "live", "endpoint_url": endpoint_url, "model": model,
            "ts": int(time.time()), "tasks": results, "all_ok": all(r["ok"] for r in results)}


def compare_with_baseline(result: Dict[str, Any], baseline_path: Path) -> Dict[str, Any]:
    if not baseline_path.is_file():
        return {"compared": False, "reason": f"no baseline at {baseline_path}"}
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    base_by_name = {t["task"]: t for t in baseline.get("tasks") or []}
    diffs: List[Dict[str, Any]] = []
    for row in result.get("tasks") or []:
        base = base_by_name.get(row["task"])
        if base is None:
            diffs.append({"task": row["task"], "in_baseline": False})
            continue
        diffs.append({
            "task": row["task"], "in_baseline": True,
            "ok_matches": row["ok"] == base.get("ok"),
            "rounds_delta": row["rounds"] - int(base.get("rounds") or 0),
            "tool_calls_delta": row["tool_calls"] - int(base.get("tool_calls") or 0),
        })
    regressed = [d for d in diffs if d.get("in_baseline") and not d.get("ok_matches")]
    return {"compared": True, "diffs": diffs, "regressed": [d["task"] for d in regressed]}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="run against a real model endpoint")
    parser.add_argument("--endpoint-url", default="", help="required with --live")
    parser.add_argument("--model", default="", help="required with --live")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--out", default="", help="path to write the JSON result to")
    parser.add_argument("--baseline", default=str(BASELINE_PATH))
    args = parser.parse_args(argv)

    if args.live:
        if not args.endpoint_url or not args.model:
            parser.error("--live requires --endpoint-url and --model")
        result = run_live(args.endpoint_url, args.model, args.api_key)
        result["baseline_comparison"] = compare_with_baseline(result, Path(args.baseline))
    else:
        result = run_scripted()

    out_path = Path(args.out) if args.out else (
        REPO / "logs" / "eval_runs" / f"eval_{result['mode']}_{result['ts']}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[eval_run] wrote {out_path}")

    if args.live:
        cmp = result["baseline_comparison"]
        if cmp.get("compared") and cmp.get("regressed"):
            print(f"[eval_run] REGRESSED vs baseline: {cmp['regressed']}")
            return 1
    return 0 if result.get("all_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
