"""Agent loop of one turn: plan -> act -> observe, read from the turn's own stream events.

``agent_runs`` feeds every event a detached run publishes into the ``LoopState`` of
that run. Model text or reasoning is *plan*, a tool call is *act* (with the tool's
name) and a tool result is *observe*. Nothing is inferred from elapsed time: a phase
changes only when an event says so.

Besides the phase, the state keeps the steps the turn took (plan, tools, results,
commit, delivery) and a checklist of work practices marked from what the tool calls
actually ran: tests, a browser check, documentation edited, the commit and its
identity, no evidence files committed, and the Agora delivery. The last state of
each session stays readable after the turn ends, as a summary to review.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

MAX_STEPS = 80
MAX_SESSIONS = 64
EXPECTED_AUTHOR = "Luissalet"

PRACTICES = (
    ("tests", "Tests run"),
    ("browser", "App checked in a browser"),
    ("docs", "Docs updated (FAUSTUS.md / README)"),
    ("commit", "Commit with the right identity"),
    ("no_evidence", "No evidence files committed"),
    ("agora", "Delivered in the Agora (submit / done)"),
)

_TESTS = re.compile(r"\b(pytest|py\.test|unittest|vitest|jest|tox|(?:npm|pnpm|yarn)(?: run)? (?:test|check)|"
                    r"go test|cargo test|node --test)\b", re.I)
_BROWSER = re.compile(r"playwright|puppeteer|chromium|headless|browser", re.I)
_DOCS = re.compile(r"\b(FAUSTUS|README(?:\.es)?|AGENTS)\.md\b", re.I)
_WRITE_TOOL = re.compile(r"write|edit|patch|replace|create_file|str_replace", re.I)
_COMMIT = re.compile(r"\bgit\b[^\n|;&]*\bcommit\b", re.I)
_ADD = re.compile(r"\bgit\b[^\n|;&]*\badd\b", re.I)
_AUTHOR = re.compile(r"--author[= ]+[\"']?([^<\"']+)|user\.name=[\"']?([^\"'\s]+)|GIT_AUTHOR_NAME=[\"']?([^\"'\s]+)")
_TRAILER = re.compile(r"Co-Authored-By:", re.I)
_EVIDENCE = re.compile(r"\.(png|jpe?g|webp|gif|mp4|webm|har)\b|evidenc|screenshot|captura|qa[-_][\w-]*\.json", re.I)
_DELIVERY = re.compile(r"\bagora(?:\.cmd|\.py)?\s[^\n]*\b(?:submit|done)\s+#?\d+|hub_agora_(?:task_)?(?:submit|done)", re.I)


def _brief(value: Any, limit: int = 140) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def judge_practices(calls: list[dict[str, Any]], *, author: str = EXPECTED_AUTHOR) -> list[dict[str, Any]]:
    """Mark each practice from the tool calls of a turn or a session.

    ``calls`` are ``{tool, command, ok}`` records (``ok`` None while a call runs).
    A practice is ``ok`` with the call that proves it, ``fail`` with the call that
    breaks it, ``unknown`` when the evidence exists but does not settle it, and
    ``pending`` when nothing seen touches it yet.
    """
    found: dict[str, dict[str, Any]] = {}

    def mark(pid: str, state: str, evidence: str) -> None:
        if found.get(pid, {}).get("state") == "fail" and state != "fail":
            return
        found[pid] = {"state": state, "evidence": _brief(evidence)}

    committed = False
    for call in calls:
        tool = str(call.get("tool") or "")
        command = str(call.get("command") or "")
        text = f"{tool} {command}"
        ok = call.get("ok")
        if _TESTS.search(command) and ok is not None:
            mark("tests", "ok" if ok else "fail", command)
        if _BROWSER.search(text) and ok is not False:
            mark("browser", "ok", text)
        if _DOCS.search(command) and (_WRITE_TOOL.search(tool) or _ADD.search(command) or _COMMIT.search(command)):
            mark("docs", "ok", text)
        if (_ADD.search(command) or _COMMIT.search(command)) and _EVIDENCE.search(command):
            mark("no_evidence", "fail", command)
        if _COMMIT.search(command) and ok is not False:
            committed = True
            who = next((g for m in _AUTHOR.finditer(command) for g in m.groups() if g), "").strip()
            if _TRAILER.search(command):
                mark("commit", "fail", "trailer in the message: " + command)
            elif who:
                mark("commit", "ok" if who.lower().startswith(author.lower()) else "fail", "author " + who)
            else:
                mark("commit", "unknown", "commit without an explicit author (repository identity): " + command)
        if _DELIVERY.search(text) and ok is not False:
            mark("agora", "ok", text)
    if committed and "no_evidence" not in found:
        mark("no_evidence", "ok", "no image, capture or QA file staged")
    return [{"id": pid, "label": label, **found.get(pid, {"state": "pending", "evidence": ""})}
            for pid, label in PRACTICES]


class LoopState:
    """The loop of one run. Not thread-safe by itself; the registry below guards it."""

    def __init__(self, session_id: str, run_id: str, started_at: float):
        self.session_id = session_id
        self.run_id = run_id
        self.started_at = started_at
        self.phase = "plan"
        self.phase_since = started_at
        self.lap = 1
        self.tool = ""
        self.tool_since: Optional[float] = None
        self.input_tokens: Optional[int] = None
        self.output_tokens: Optional[int] = None
        self.ended_at: Optional[float] = None
        self.outcome = ""
        self.steps: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def _step(self, kind: str, label: str, now: float, **extra: Any) -> None:
        self.steps.append({"kind": kind, "label": _brief(label), "at": now, "lap": self.lap, **extra})
        if len(self.steps) > MAX_STEPS:
            del self.steps[0]

    def _enter(self, phase: str, now: float) -> None:
        if phase != self.phase:
            self.phase = phase
            self.phase_since = now

    def feed(self, payload: dict[str, Any], now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        kind = str(payload.get("type") or "")
        if not kind and "delta" in payload:
            if not str(payload.get("delta") or "").strip():
                return                     # a separator between rounds is not the model writing
            if self.phase != "plan" or not self.steps:
                self._step("plan", "Reasoning" if payload.get("thinking") else "Writing", now)
            self._enter("plan", now)
            return
        round_ = _int(payload.get("round"))
        if round_ and round_ > self.lap:
            self.lap = round_
        if kind == "tool_start":
            self.tool = _brief(payload.get("tool"), 64) or "tool"
            self.tool_since = now
            command = str(payload.get("full_command") or payload.get("command") or "")
            self.calls.append({"tool": self.tool, "command": command[:2000], "ok": None})
            self._step("tool", f"{self.tool} {command}".strip(), now, tool=self.tool)
            self._enter("act", now)
        elif kind == "tool_output":
            tool = _brief(payload.get("tool"), 64) or self.tool
            exit_code = payload.get("exit_code")
            ok = exit_code in (None, 0) and str(payload.get("result_status") or "ok") not in ("error", "failed")
            call = next((c for c in reversed(self.calls) if c["ok"] is None and c["tool"] == tool), None)
            if call is None:
                call = {"tool": tool, "command": "", "ok": None}
                self.calls.append(call)
            call["ok"] = ok
            call["command"] = call["command"] or str(payload.get("command") or "")[:2000]
            command = call["command"]
            label = tool + (" ok" if ok else f" failed ({exit_code})" if exit_code not in (None, 0) else " failed")
            self._step("result", label, now, tool=tool, ok=ok)
            if ok and _COMMIT.search(command):
                self._step("commit", command, now)
            if ok and _DELIVERY.search(f"{tool} {command}"):
                self._step("submit", command, now)
            self.tool = ""
            self.tool_since = None
            self._enter("observe", now)
        elif kind == "agent_step" and self.phase != "observe":
            # A new round after a tool result is the model reading that result: still observe until it writes.
            self._enter("plan", now)
        elif kind == "metrics":
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            self.input_tokens = _int(data.get("input_tokens")) if data.get("input_tokens") is not None else self.input_tokens
            self.output_tokens = _int(data.get("output_tokens")) if data.get("output_tokens") is not None else self.output_tokens

    def finish(self, outcome: str, now: Optional[float] = None) -> None:
        if self.ended_at is not None:
            return
        self.ended_at = time.time() if now is None else now
        self.outcome = outcome
        self.tool = ""
        self._step("end", outcome, self.ended_at)
        self._enter("done", self.ended_at)

    def snapshot(self, now: Optional[float] = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        end = self.ended_at or now
        practices = judge_practices(self.calls)
        return {
            "session_id": self.session_id, "run_id": self.run_id, "running": self.ended_at is None,
            "phase": self.phase, "phase_since": self.phase_since, "lap": self.lap,
            "tool": self.tool or None, "tool_since": self.tool_since,
            "started_at": self.started_at, "ended_at": self.ended_at, "outcome": self.outcome or None,
            "elapsed_s": round(max(0.0, end - self.started_at), 1),
            "tokens": {"input": self.input_tokens, "output": self.output_tokens},
            "tool_calls": len(self.calls), "failed_calls": sum(1 for c in self.calls if c["ok"] is False),
            "steps": list(self.steps), "practices": practices,
            "practices_summary": {"ok": sum(p["state"] == "ok" for p in practices),
                                  "fail": sum(p["state"] == "fail" for p in practices),
                                  "open": sum(p["state"] in ("pending", "unknown") for p in practices)},
        }


_LOCK = threading.Lock()
_STATES: "OrderedDict[str, LoopState]" = OrderedDict()


def begin(session_id: str, run_id: str, started_at: Optional[float] = None) -> LoopState:
    state = LoopState(session_id, run_id, time.time() if started_at is None else started_at)
    with _LOCK:
        _STATES.pop(session_id, None)
        _STATES[session_id] = state
        while len(_STATES) > MAX_SESSIONS:
            _STATES.popitem(last=False)
    return state


def feed(state: Optional[LoopState], payload: dict[str, Any]) -> None:
    if state is None:
        return
    with _LOCK:
        state.feed(payload)


def finish(state: Optional[LoopState], outcome: str) -> None:
    if state is None:
        return
    with _LOCK:
        state.finish(outcome)


def snapshot(session_id: str) -> Optional[dict[str, Any]]:
    """The running or last loop of a session; None when this process has not run one."""
    with _LOCK:
        state = _STATES.get(session_id)
        return state.snapshot() if state is not None else None
