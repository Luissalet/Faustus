#!/usr/bin/env python3
"""faustus_run.py — one turn of the Faustus agent from a terminal or a script.

Talks to a running Faustus server (the same `/api/chat_stream` the app uses),
so the turn gets everything the app gives it: the tool gates, the harness
checks, memory, skills, the model router, sub-agents.

    python scripts/faustus_run.py -p "Fix the failing test in tests/test_x.py" \\
        --workspace D:/code/proj --json > run.ndjson

    echo "summarise README.md" | python scripts/faustus_run.py -p - --workspace .

Output:
  * default: the answer text on stdout, a one-line summary on stderr.
  * --json: NDJSON on stdout. First record `{"type": "run_start", ...}`, then
    every event of the turn as the app receives it (`--events` narrows them),
    then `{"type": "run_summary", ...}` with the answer, the tools run, the
    rounds, the usage (prompt cache included) and why it stopped.

Approvals: without --approve a tool that needs approval stops the run (exit 2)
and the summary carries the card, so a script can decide and resume it with
--session S --resume-approval ID. With --approve every card of the run is
approved for the task (the app's "allow for this task"), up to --max-legs.

Exit codes: 0 answered, 1 error, 2 stopped on an approval, 3 timed out.

Environment: FAUSTUS_URL (default http://127.0.0.1:7000), FAUSTUS_API_TOKEN
(a token with the `sessions` scope) or FAUSTUS_USER/FAUSTUS_PASS (sign-in),
FAUSTUS_MODEL, FAUSTUS_ENDPOINT_URL.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

EXIT_OK, EXIT_ERROR, EXIT_APPROVAL, EXIT_TIMEOUT = 0, 1, 2, 3

#: `--events tools`: the events a script usually wants, without the token
#: deltas.
TOOL_EVENTS = {"tool_start", "tool_output", "tool_progress", "ask_user", "harness_check", "round_info",
               "error", "cancelled", "metrics", "usage", "subagent", "harness_summary"}


class Client:
    def __init__(self, base: str, user: Optional[str] = None, password: Optional[str] = None,
                 token: Optional[str] = None):
        self.base = base.rstrip("/")
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        if token:
            # An API token with the `sessions` scope (Settings › API tokens):
            # no password in the environment.
            self._opener.addheaders = [("Authorization", f"Bearer {token}")]
        elif user:
            self.post_json("/api/auth/login", {"username": user, "password": password or ""})

    def post_json(self, path: str, body: Dict[str, Any], timeout: float = 30) -> Any:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with self._opener.open(req, timeout=timeout) as r:
            return _json_or_raw(r.read())

    def post_form(self, path: str, form: Dict[str, Any], timeout: float = 30) -> Any:
        body = urllib.parse.urlencode({k: v for k, v in form.items() if v is not None}).encode()
        with self._opener.open(urllib.request.Request(self.base + path, data=body, method="POST"),
                               timeout=timeout) as r:
            return _json_or_raw(r.read())

    def stream(self, form: Dict[str, Any], timeout: float) -> Iterable[Dict[str, Any]]:
        body = urllib.parse.urlencode({k: v for k, v in form.items() if v is not None}).encode()
        req = urllib.request.Request(self.base + "/api/chat_stream", data=body, method="POST")
        with self._opener.open(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                try:
                    ev = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    yield ev


def _json_or_raw(raw: bytes) -> Any:
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text}


def read_prompt(value: str) -> str:
    if value == "-":
        return sys.stdin.read().strip()
    if value.startswith("@"):
        with open(value[1:], encoding="utf-8-sig") as f:
            return f.read().strip()
    return value


class Turn:
    """Folds the event stream into the run summary."""

    def __init__(self) -> None:
        self.text = ""
        self.tools: List[str] = []
        self.rounds = 0
        self.metrics: Dict[str, Any] = {}
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
        self.approval: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.cancelled = False

    def feed(self, ev: Dict[str, Any]) -> None:
        t = ev.get("type")
        if isinstance(ev.get("delta"), str) and not ev.get("thinking") and not t:
            self.text += ev["delta"]
        elif t == "response_replace" and isinstance(ev.get("text"), str):
            self.text = ev["text"]
        elif t == "tool_start" and ev.get("tool"):
            self.tools.append(str(ev["tool"]))
        elif t == "metrics" and isinstance(ev.get("data"), dict):
            self.metrics = ev["data"]
        elif t == "usage" and isinstance(ev.get("data"), dict):
            for k in self.usage:
                v = ev["data"].get(k)
                if isinstance(v, int) and not isinstance(v, bool):
                    self.usage[k] += v
        elif t == "ask_user" and isinstance(ev.get("data"), dict):
            self.approval = ev["data"]
        elif t == "error" or ("error" in ev and not t):
            self.error = str(ev.get("error") or ev.get("message") or ev)[:500]
        elif t == "cancelled":
            self.cancelled = True
        try:
            if t in ("tool_start", "agent_step", "round_info"):
                self.rounds = max(self.rounds, int(ev.get("round") or 0))
        except (TypeError, ValueError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run one Faustus agent turn headless.")
    ap.add_argument("-p", "--prompt", required=True, help="the message; '-' reads stdin, '@file' reads a file")
    ap.add_argument("--url", default=os.environ.get("FAUSTUS_URL", "http://127.0.0.1:7000"))
    ap.add_argument("--workspace", default=None, help="folder the agent works in")
    ap.add_argument("--session", default=None, help="continue this chat instead of opening a new one")
    ap.add_argument("--model", default=os.environ.get("FAUSTUS_MODEL"))
    ap.add_argument("--endpoint-url", default=os.environ.get("FAUSTUS_ENDPOINT_URL"))
    ap.add_argument("--effort", default=None, help="reasoning: auto|off|low|medium|high|max")
    ap.add_argument("--plan", action="store_true", help="plan mode: read and propose, no changes")
    ap.add_argument("--web", action="store_true", help="allow web search for the turn")
    ap.add_argument("--approve", action="store_true", help="approve every tool card of the run for the task")
    ap.add_argument("--resume-approval", default="", help="resume a stopped run by approving this card id")
    ap.add_argument("--max-legs", type=int, default=12, help="approval resumes allowed with --approve")
    ap.add_argument("--timeout", type=float, default=3600.0, help="seconds for the whole run")
    ap.add_argument("--json", action="store_true", help="NDJSON events and a final summary record")
    ap.add_argument("--events", choices=("all", "tools", "none"), default="all",
                    help="with --json: which events to print")
    args = ap.parse_args(argv)

    out = sys.stdout
    started = time.time()
    try:
        client = Client(args.url, os.environ.get("FAUSTUS_USER"), os.environ.get("FAUSTUS_PASS"),
                        os.environ.get("FAUSTUS_API_TOKEN"))
        session = args.session
        if not session:
            made = client.post_form("/api/session", {
                "name": "faustus run", "endpoint_url": args.endpoint_url, "model": args.model,
                "skip_validation": "true",
            })
            session = (made or {}).get("id") or (made or {}).get("session_id")
            if not session:
                raise RuntimeError(f"could not open a session: {made}")
        message = read_prompt(args.prompt)
    except (urllib.error.URLError, OSError, RuntimeError) as exc:
        _emit_summary(out, args, None, Turn(), started, "error", str(exc))
        return EXIT_ERROR

    def base_form() -> Dict[str, Any]:
        form: Dict[str, Any] = {"session": session, "mode": "agent"}
        if args.model:
            form["model"] = args.model
        if args.workspace:
            form["workspace"] = os.path.abspath(args.workspace)
        if args.effort:
            form["reasoning_effort"] = args.effort
        if args.plan:
            form["plan_mode"] = "true"
        if args.web:
            form["allow_web_search"] = "true"
        return form

    if args.json:
        _write(out, {"type": "run_start", "session": session, "url": args.url, "model": args.model,
                     "workspace": args.workspace, "plan": args.plan, "approve": args.approve,
                     "started": round(started, 3)})
    form = base_form()
    if args.resume_approval:
        form.update({"message": "", "tool_approval_id": args.resume_approval,
                     "tool_approval_decision": "approve_task"})
    else:
        form["message"] = message

    turn = Turn()
    stop = "answered"
    try:
        for _leg in range(max(1, args.max_legs)):
            turn.approval = None
            left = args.timeout - (time.time() - started)
            if left <= 0:
                stop = "timeout"
                break
            for ev in client.stream(form, timeout=left):
                turn.feed(ev)
                if args.json and args.events != "none":
                    if args.events == "all" or ev.get("type") in TOOL_EVENTS:
                        _write(out, ev)
            if turn.approval and turn.approval.get("kind") == "tool_approval":
                if not args.approve:
                    stop = "approval_required"
                    break
                form = base_form()
                form.update({"message": "", "tool_approval_id": turn.approval.get("approval_id"),
                             "tool_approval_decision": "approve_task"})
                continue
            break
        else:
            stop = "max_legs"
    except (socket.timeout, TimeoutError):
        stop = "timeout"
    except (urllib.error.URLError, OSError) as exc:
        turn.error = str(exc)
    if turn.error and stop == "answered":
        stop = "error"
    if turn.cancelled and stop == "answered":
        stop = "cancelled"
    _emit_summary(out, args, session, turn, started, stop, turn.error)
    return {"answered": EXIT_OK, "approval_required": EXIT_APPROVAL, "timeout": EXIT_TIMEOUT}.get(stop, EXIT_ERROR)


def _write(out, obj: Dict[str, Any]) -> None:
    out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    out.flush()


def _emit_summary(out, args, session, turn: Turn, started: float, stop: str, error: Optional[str]) -> None:
    elapsed = round(time.time() - started, 2)
    usage = dict(turn.usage)
    for key in ("input_tokens", "output_tokens"):
        if not usage.get(key) and isinstance(turn.metrics.get(key), int):
            usage[key] = turn.metrics[key]
    pc = turn.metrics.get("prompt_cache") if isinstance(turn.metrics, dict) else None
    summary = {
        "type": "run_summary", "session": session, "stop": stop, "text": turn.text.strip(),
        "tools": turn.tools, "tool_calls": len(turn.tools), "rounds": turn.rounds, "elapsed_s": elapsed,
        "model": turn.metrics.get("model") or args.model, "usage": usage,
        "prompt_cache": pc if isinstance(pc, dict) else None,
        "approval": turn.approval if stop == "approval_required" else None, "error": error,
    }
    if args.json:
        _write(out, summary)
        return
    if summary["text"]:
        out.write(summary["text"] + "\n")
    sys.stderr.write(f"[faustus] {stop} · {elapsed}s · {len(turn.tools)} tools · {turn.rounds} rounds"
                     f" · session {session}" + (f" · approval {turn.approval.get('approval_id')}"
                                                if stop == "approval_required" and turn.approval else "")
                     + (f" · {error}" if error else "") + "\n")


if __name__ == "__main__":
    sys.exit(main())
