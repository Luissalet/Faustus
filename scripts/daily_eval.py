#!/usr/bin/env python3
"""daily_eval.py — the everyday-use battery, end to end against a running
Faustus instance.

`scripts/eval_run.py` drives the agent loop directly; this one goes through
the HTTP app the way a person does (routes, think-mode rule, freshness,
approval gates, tool selection, the answer checks), so a change anywhere in
that path shows up. Every task has a deterministic check — a number, a
weekday, the tool that had to run, no approval card, no raw harness text in
the answer — never a model judging a model.

    python scripts/daily_eval.py --base http://127.0.0.1:7006 \\
        --model qwen3.8-27b-q8-llamacpp --endpoint-url http://127.0.0.1:8081/v1
    python scripts/daily_eval.py --only dates-weekday,iva
    python scripts/daily_eval.py --with-writes      # also the tasks that write (a note)

Credentials come from FAUSTUS_USER / FAUSTUS_PASS. The report goes to
`logs/daily_eval/<timestamp>.json` and `.md` (or `--out PREFIX`).
Standard library only, so it runs from any Python on the machine.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[1]
TASKS_PATH = Path(__file__).with_name("daily_eval_tasks.json")

#: Text that must never reach an answer: harness internals and raw tool lines.
RAW_ARTIFACTS = [
    r"^AI: ", r"Note created: .*\(id: ", r"\[TOOL ", r"Waiting for an exact user approval",
    r"Allow this task to continue\?", r"<tool_call>", r"</?think>", r"UNTRUSTED SOURCE DATA",
]


def check(task: Dict[str, Any], result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every check of `task` against one run's `result`
    ({answer, tools, cards, seconds, error}); pure, so it is unit-tested."""
    answer = str(result.get("answer") or "")
    tools = [str(t) for t in result.get("tools") or []]
    out: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        out.append({"check": name, "ok": bool(ok), "detail": detail})

    if result.get("error"):
        add("no_error", False, str(result["error"])[:200])
    add("answered", bool(answer.strip()), "" if answer.strip() else "empty answer")
    for pattern in task.get("answer_all", []):
        add(f"answer ~ {pattern}", re.search(pattern, answer, re.I | re.S) is not None)
    for pattern in task.get("answer_none", []):
        add(f"answer !~ {pattern}", re.search(pattern, answer, re.I | re.S) is None)
    for pattern in RAW_ARTIFACTS:
        if re.search(pattern, answer, re.M):
            add(f"no raw text {pattern}", False)
    short = [t.split("__")[-1] for t in tools]
    if task.get("tools_any"):
        add("used one of " + ",".join(task["tools_any"]),
            any(t in task["tools_any"] for t in short))
    if task.get("first_tool_any"):
        add("first tool in " + ",".join(task["first_tool_any"]),
            bool(short) and short[0] in task["first_tool_any"], short[0] if short else "no tool")
    for name in task.get("tools_none", []):
        add(f"did not use {name}", name not in short)
    if "max_tools" in task:
        add(f"at most {task['max_tools']} tool calls", len(tools) <= int(task["max_tools"]), str(len(tools)))
    if "max_cards" in task:
        add(f"at most {task['max_cards']} approval cards", int(result.get("cards") or 0) <= int(task["max_cards"]),
            str(result.get("cards") or 0))
    if "max_seconds" in task:
        add(f"within {task['max_seconds']} s", float(result.get("seconds") or 0) <= float(task["max_seconds"]),
            f"{float(result.get('seconds') or 0):.0f} s")
    return out


class Client:
    def __init__(self, base: str, user: str = "", password: str = ""):
        self.base = base.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        if user:
            body = json.dumps({"username": user, "password": password}).encode()
            req = urllib.request.Request(self.base + "/api/auth/login", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            with self.opener.open(req, timeout=30) as r:
                r.read()

    def form(self, path: str, data: Dict[str, Any], timeout: float = 30):
        body = urllib.parse.urlencode({k: v for k, v in data.items() if v is not None}).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        return self.opener.open(req, timeout=timeout)

    def new_session(self, model: str, endpoint_url: str) -> str:
        with self.form("/api/session", {"name": "daily-eval", "endpoint_url": endpoint_url,
                                         "model": model, "skip_validation": "true"}) as r:
            made = json.loads(r.read().decode("utf-8", "replace"))
        return str(made.get("id") or made.get("session_id") or "")

    def turn(self, session: str, message: str, mode: str, model: str, web: bool,
             timeout: float, approve: bool) -> Dict[str, Any]:
        """One turn, approving cards when asked to (and counting them)."""
        form = {"session": session, "message": message, "mode": mode, "model": model,
                "allow_web_search": "true" if web else None}
        answer, tools, cards, error = "", [], 0, ""
        started = time.time()
        for _leg in range(6):
            approval = None
            with self.form("/api/chat_stream", form, timeout=timeout) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        ev = json.loads(payload)
                    except ValueError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    kind = ev.get("type")
                    if isinstance(ev.get("delta"), str) and not ev.get("thinking"):
                        answer += ev["delta"]
                    if kind == "response_replace" and isinstance(ev.get("text"), str):
                        answer = ev["text"]
                    if kind == "tool_start":
                        tools.append(str(ev.get("tool") or ""))
                    if kind == "ask_user" and isinstance(ev.get("data"), dict) \
                            and ev["data"].get("kind") == "tool_approval":
                        cards += 1
                        approval = ev["data"].get("approval_id")
                    status = ev.get("status")
                    if kind == "error" or (isinstance(status, int) and status >= 500):
                        error = str(ev.get("text") or ev.get("error") or "")[:300] or error
            if not (approval and approve):
                break
            form = {"session": session, "message": "", "mode": mode, "model": model,
                    "tool_approval_id": approval, "tool_approval_decision": "approve_task"}
        return {"answer": answer, "tools": tools, "cards": cards, "error": error,
                "seconds": round(time.time() - started, 1)}


def run(args) -> Dict[str, Any]:
    tasks = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    only = {t.strip() for t in (args.only or "").split(",") if t.strip()}
    client = Client(args.base, os.environ.get("FAUSTUS_USER", ""), os.environ.get("FAUSTUS_PASS", ""))
    rows = []
    for task in tasks:
        if only and task["id"] not in only:
            continue
        if task.get("writes") and not args.with_writes:
            continue
        session = client.new_session(args.model, args.endpoint_url)
        result: Dict[str, Any] = {"answer": "", "tools": [], "cards": 0, "seconds": 0.0, "error": ""}
        for index, message in enumerate(task["messages"]):
            # "new_chat_each": every message in a fresh chat (memory recall
            # across chats); the checks read the last one.
            if index and task.get("new_chat_each"):
                session = client.new_session(args.model, args.endpoint_url)
            try:
                result = client.turn(session, message, task.get("mode", "agent"), args.model,
                                     bool(task.get("web")), args.timeout, approve=True)
            except Exception as exc:  # noqa: BLE001 - one task's failure is a result
                result = {"answer": "", "tools": [], "cards": 0, "seconds": 0.0, "error": repr(exc)}
                break
        checks = check(task, result)
        ok = all(c["ok"] for c in checks)
        rows.append({"id": task["id"], "ok": ok, "session": session, **result, "checks": checks})
        print(f"{'PASS' if ok else 'FAIL'} {task['id']:<22} {result['seconds']:>6.0f} s  "
              f"tools={','.join(t.split('__')[-1] for t in result['tools']) or '-'}  cards={result['cards']}",
              flush=True)
        for c in checks:
            if not c["ok"]:
                print(f"      - {c['check']} {c['detail']}", flush=True)
    passed = sum(1 for r in rows if r["ok"])
    return {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "base": args.base, "model": args.model,
            "passed": passed, "total": len(rows),
            "seconds": round(sum(r["seconds"] for r in rows), 1), "tasks": rows}


def markdown(report: Dict[str, Any]) -> str:
    lines = [f"# Batería de uso diario — {report['when']}", "",
             f"{report['passed']}/{report['total']} tareas bien · {report['seconds']:.0f} s · "
             f"{report['model']} en {report['base']}", "",
             "| tarea | resultado | s | herramientas | tarjetas | fallos |", "| --- | --- | --- | --- | --- | --- |"]
    for r in report["tasks"]:
        fails = "; ".join(f"{c['check']} {c['detail']}".strip() for c in r["checks"] if not c["ok"])
        tools = ", ".join(t.split("__")[-1] for t in r["tools"]) or "—"
        lines.append(f"| {r['id']} | {'bien' if r['ok'] else 'MAL'} | {r['seconds']:.0f} | {tools} | "
                     f"{r['cards']} | {fails or '—'} |")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:" + os.environ.get("FAUSTUS_PORT", "7000"))
    ap.add_argument("--model", default=os.environ.get("FAUSTUS_MODEL", ""))
    ap.add_argument("--endpoint-url", default=os.environ.get("FAUSTUS_ENDPOINT_URL", "http://127.0.0.1:11434/v1"))
    ap.add_argument("--only", default="", help="comma-separated task ids")
    ap.add_argument("--with-writes", action="store_true", help="also run tasks that write data (a note)")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--out", default="", help="report path prefix (default logs/daily_eval/<timestamp>)")
    args = ap.parse_args(argv)
    if not args.model:
        ap.error("--model (or FAUSTUS_MODEL) is required")
    report = run(args)
    prefix = Path(args.out) if args.out else REPO / "logs" / "daily_eval" / time.strftime("%Y%m%d-%H%M%S")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    Path(str(prefix) + ".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(str(prefix) + ".md").write_text(markdown(report), encoding="utf-8")
    print(f"\n{report['passed']}/{report['total']} · report: {prefix}.md")
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
