#!/usr/bin/env python3
"""Freshness battery: does Faustus search the web for time-sensitive
questions instead of refusing or asking permission first?

Sends a fixed list of questions (12 time-sensitive Spanish/English
questions about generic, well-known subjects, plus 4 timeless controls) to a
running Faustus instance in agent mode over POST /api/chat_stream, consumes
the SSE stream, and reports per question:

  - searched:  a `web_search` tool step appeared in the stream
  - refused:   the final answer text matched a "no live access" refusal regex
  - seconds:   wall-clock time for the turn
  - sources:   how many sources came back on the `web_sources` event

Exit code is non-zero when any time-sensitive question was NOT searched, was
refused, or ran but errored; or when any timeless control question WAS
searched (over-triggering the freshness heuristic on questions that don't
need it).

Usage:
    python3 scripts/eval_freshness.py --base http://127.0.0.1:7000

Reads FAUSTUS_USER / FAUSTUS_PASSWORD from the environment for login
(falls back to --user/--password). Writes a dated report to
docs/evals/freshness-<date>.md.

This performs real network calls against a running server; it is not run as
part of the unit test suite. tests/test_eval_freshness.py covers the SSE
parsing and verdict logic with canned events instead.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Iterable, Iterator, Optional

try:
    import requests
except ImportError:  # pragma: no cover - exercised only when requests is missing
    requests = None  # type: ignore

REPO_ROOT = Path(__file__).resolve().parent.parent
EVALS_DIR = REPO_ROOT / "docs" / "evals"

# Generic, well-known subjects on purpose: this battery tests whether the
# MODEL searches, not whether one specific team/coin/repo is trendy today.
TIME_SENSITIVE_QUESTIONS = [
    "¿Ganó el Real Madrid su último partido?",
    "Who won the last Champions League final?",
    "What's the weather like in Madrid right now?",
    "¿Qué tiempo hace hoy en Buenos Aires?",
    "What is the current price of Bitcoin?",
    "¿Cuál es el precio actual del bitcoin?",
    "What is the latest version of the Linux kernel?",
    "¿Cuál es la última versión de Python?",
    "Who is the current head coach of Manchester United?",
    "¿Quién es el entrenador actual del FC Barcelona?",
    "What's in the news today about artificial intelligence?",
    "Dame las noticias de hoy sobre inteligencia artificial",
]

TIMELESS_CONTROL_QUESTIONS = [
    "How do I sort a list in Python?",
    "¿Qué es una derivada en matemáticas?",
    "Explain how a hash map works.",
    "Escribe una función que sume dos números en Python.",
]

_REFUSAL_RE = re.compile(
    r"no tengo acceso|no puedo acceder|i don'?t have access|cannot access|"
    r"real-?time|no dispongo de acceso|sin acceso a internet",
    re.IGNORECASE,
)


@dataclasses.dataclass
class QuestionResult:
    question: str
    control: bool
    searched: bool
    refused: bool
    seconds: float
    sources: int
    error: Optional[str] = None


def iter_sse_events(lines: Iterable[str]) -> Iterator[dict]:
    """Parse an SSE byte/line stream (as `requests` iter_lines gives it,
    already decoded to str) into event dicts. `data: [DONE]` yields
    `{"done": True}`; a `delta`-only frame (no "type" key) is passed through
    as-is; anything that isn't valid JSON is skipped.
    """
    for line in lines:
        if not line:
            continue
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            yield {"done": True}
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def evaluate_stream(events: Iterable[dict]) -> tuple[bool, bool, int, str]:
    """Given the events of one turn, return (searched, refused, sources_count,
    full_answer_text). Pure function over parsed events — no network here —
    so it is unit-testable with canned event lists (see
    tests/test_eval_freshness.py).
    """
    searched = False
    sources_count = 0
    text_parts: list[str] = []
    for ev in events:
        if ev.get("done"):
            break
        ev_type = ev.get("type")
        if ev_type is None and "delta" in ev:
            text_parts.append(str(ev.get("delta") or ""))
        elif ev_type == "tool_start":
            if ev.get("tool") == "web_search":
                searched = True
        elif ev_type == "web_sources":
            data = ev.get("data")
            if isinstance(data, list):
                sources_count = max(sources_count, len(data))
    full_text = "".join(text_parts)
    refused = bool(_REFUSAL_RE.search(full_text))
    return searched, refused, sources_count, full_text


class FaustusClient:
    def __init__(self, base: str, user: str, password: str, timeout: float = 60.0):
        if requests is None:
            raise RuntimeError("the 'requests' package is required to run this battery")
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.timeout = timeout
        self._login(user, password)

    def _login(self, user: str, password: str) -> None:
        resp = self.session.post(
            f"{self.base}/api/auth/login",
            json={"username": user, "password": password},
            timeout=self.timeout,
        )
        resp.raise_for_status()

    def _default_route(self) -> dict:
        """The server's default model as the composer would send it
        (endpoint_url + model + endpoint_id), cached after the first call;
        empty when the settings or the model list cannot be read, in which
        case the server decides."""
        cached = getattr(self, "_route", None)
        if cached is not None:
            return cached
        route: dict = {}
        try:
            settings = self.session.get(f"{self.base}/api/auth/settings", timeout=self.timeout).json() or {}
            wanted_model = str(settings.get("default_model") or "")
            wanted_endpoint = str(settings.get("default_endpoint_id") or "")
            items = (self.session.get(f"{self.base}/api/models?background=false", timeout=self.timeout).json() or {}).get("items") or []
            for item in items:
                if not isinstance(item, dict) or item.get("model_type") not in (None, "llm"):
                    continue
                models = [str(m) for m in (item.get("models") or [])]
                endpoint_id = str(item.get("endpoint_id") or "")
                if wanted_model in models and (not wanted_endpoint or endpoint_id == wanted_endpoint):
                    route = {"endpoint_url": str(item.get("url") or ""), "model": wanted_model, "endpoint_id": endpoint_id}
                    break
        except Exception:  # noqa: BLE001 - the server default is the fallback
            route = {}
        self._route = route
        return route

    def _new_session(self, name: str) -> str:
        """A real chat session per question: the stream endpoint answers 404
        for an id it has never seen. Endpoint and model are left empty with
        validation skipped so the server's default model answers."""
        resp = self.session.post(
            f"{self.base}/api/session",
            data={"name": name, "skip_validation": "true", **self._default_route()},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        session_id = (resp.json() or {}).get("id")
        if not session_id:
            raise RuntimeError("/api/session returned no id")
        return str(session_id)

    def run_question(self, message: str, control: bool) -> QuestionResult:
        started = time.monotonic()
        try:
            session_id = self._new_session(f"eval freshness {uuid.uuid4().hex[:8]}")
            resp = self.session.post(
                f"{self.base}/api/chat_stream",
                json={"message": message, "session": session_id, "mode": "agent"},
                stream=True,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            events = iter_sse_events(resp.iter_lines(decode_unicode=True))
            searched, refused, sources_count, _text = evaluate_stream(events)
            elapsed = time.monotonic() - started
            return QuestionResult(
                question=message, control=control, searched=searched,
                refused=refused, seconds=elapsed, sources=sources_count,
            )
        except Exception as e:  # noqa: BLE001 - one bad question must not kill the battery
            elapsed = time.monotonic() - started
            return QuestionResult(
                question=message, control=control, searched=False,
                refused=False, seconds=elapsed, sources=0, error=str(e),
            )


def verdict_for(result: QuestionResult) -> tuple[bool, str]:
    """(ok, reason) for one question's result, per the battery's rules:
    a time-sensitive question must be searched and not refused; a control
    question must NOT be searched."""
    if result.error:
        return False, f"error: {result.error}"
    if result.control:
        if result.searched:
            return False, "control question triggered a search"
        return True, "ok (no search, as expected)"
    if not result.searched:
        return False, "not searched"
    if result.refused:
        return False, "answer looked like a refusal"
    return True, "ok"


def build_report(results: list[QuestionResult]) -> str:
    date = datetime.date.today().isoformat()
    lines = [
        f"# Freshness battery — {date}",
        "",
        "Automatic check that Faustus searches the web for time-sensitive "
        "questions instead of refusing or asking permission, and does NOT "
        "search for timeless control questions. See docs/evals/freshness.md.",
        "",
        "| # | Question | Control | Searched | Refused | Sources | Seconds | Verdict |",
        "|---|----------|---------|----------|---------|---------|---------|---------|",
    ]
    for i, r in enumerate(results, 1):
        ok, reason = verdict_for(r)
        verdict = "PASS" if ok else f"FAIL ({reason})"
        q = r.question.replace("|", "\\|")
        lines.append(
            f"| {i} | {q} | {'yes' if r.control else 'no'} | "
            f"{'yes' if r.searched else 'no'} | {'yes' if r.refused else 'no'} | "
            f"{r.sources} | {r.seconds:.1f} | {verdict} |"
        )
    total = len(results)
    failures = [r for r in results if not verdict_for(r)[0]]
    lines.append("")
    lines.append(f"**{total - len(failures)}/{total} passed.**")
    if failures:
        lines.append("")
        lines.append("Failures:")
        for r in failures:
            _, reason = verdict_for(r)
            lines.append(f"- {r.question!r}: {reason}")
    return "\n".join(lines) + "\n"


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the freshness battery against a running Faustus instance.",
    )
    parser.add_argument("--base", default="http://127.0.0.1:7000",
                         help="Base URL of the running Faustus server (default: %(default)s)")
    parser.add_argument("--user", default=os.environ.get("FAUSTUS_USER", ""),
                         help="Login username (default: $FAUSTUS_USER)")
    parser.add_argument("--password", default=os.environ.get("FAUSTUS_PASSWORD", ""),
                         help="Login password (default: $FAUSTUS_PASSWORD)")
    parser.add_argument("--out", default=None,
                         help="Report output path (default: docs/evals/freshness-<date>.md)")
    parser.add_argument("--timeout", type=float, default=60.0,
                         help="Per-question timeout in seconds (default: %(default)s)")
    args = parser.parse_args(argv)

    if not args.user or not args.password:
        print("error: --user/--password (or FAUSTUS_USER/FAUSTUS_PASSWORD) are required", file=sys.stderr)
        return 2

    client = FaustusClient(args.base, args.user, args.password, timeout=args.timeout)

    results: list[QuestionResult] = []
    for q in TIME_SENSITIVE_QUESTIONS:
        results.append(client.run_question(q, control=False))
    for q in TIMELESS_CONTROL_QUESTIONS:
        results.append(client.run_question(q, control=True))

    report = build_report(results)
    out_path = Path(args.out) if args.out else EVALS_DIR / f"freshness-{datetime.date.today().isoformat()}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"Report written to {out_path}")

    failures = [r for r in results if not verdict_for(r)[0]]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
