"""Replay `CriteriaBook.auto_link` over a recorded LLM trace (read-only eval).

    python scripts/replay_contract_auto_link.py data/llm_traces/<session>.jsonl [--line N]
                                                 [--mutating-shell] [--end-only]

Takes the request of one model call (default: the one with the most messages)
and replays its tool calls into a fresh contract-criteria book built from the
task statement, as the agent loop does: every call is recorded in order, a
mutation (`write_file` / `edit_file` / `apply_patch`; with --mutating-shell also
a shell command that looks like it writes) withdraws what the harness proved
before it, and after each non-mutation call `auto_link` tries the open criteria
against that call. Reports how many criteria the harness proves by itself, by
kind, and how long the slowest `auto_link` invocation took.

The trace is only read; nothing is written. Tool output is the text the model
was shown (the fenced block of the tool message); `exit_code` is taken from the
tool message's data block when present, else assumed 0.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import contract_criteria as cc  # noqa: E402
from src.agent_harness import FILE_MUTATION_TOOLS, SHELL_TOOLS, shell_command_looks_mutating  # noqa: E402

_OUTPUT_RE = re.compile(r"^###[^\n]*\n(?:[^\n]*\n)?```\n?(.*?)\n?```(?:\n\*\*data:\*\*|\s*$)", re.S)
_EXIT_RE = re.compile(r'"exit_code":\s*(-?\d+)')


def _pick_line(path: str, wanted: int | None) -> dict:
    best, best_n = None, -1
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if wanted is not None and i != wanted:
                continue
            data = json.loads(line)
            n = len((data.get("request") or {}).get("messages") or [])
            if n > best_n:
                best, best_n = data, n
    if best is None:
        raise SystemExit("no such line")
    return best


def _task_text(messages: list) -> str:
    for m in messages:
        if m.get("role") == "user" and not m.get("_agent_injected") and not m.get("_harness_note"):
            content = m.get("content")
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            return str(content or "")
    return ""


def _tool_result(content: str) -> dict:
    m = _OUTPUT_RE.match(content or "")
    output = m.group(1) if m else (content or "")
    exit_code = _EXIT_RE.search(content or "")
    return {"output": output, "exit_code": int(exit_code.group(1)) if exit_code else 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace")
    ap.add_argument("--line", type=int, default=None)
    ap.add_argument("--mutating-shell", action="store_true")
    ap.add_argument("--end-only", action="store_true", help="one pass at the end instead of after each call")
    ap.add_argument("--lenient-words", action="store_true",
                    help="what-if: one shared content word proves (the model-link tier)")
    ap.add_argument("--any-tool", action="store_true",
                    help="what-if: reads and searches count as runs too")
    ap.add_argument("--explain", action="store_true",
                    help="list each proven criterion with the call that proved it")
    ap.add_argument("--budget", type=float, default=cc.AUTO_BUDGET_S, help="seconds per auto_link pass")
    args = ap.parse_args()
    if args.lenient_words:
        from src import verification
        verification.AUTO_MIN_WORDS = 1
    if args.any_tool:
        cc.is_run_call = lambda tool, command: True

    messages = (_pick_line(args.trace, args.line).get("request") or {}).get("messages") or []
    task = _task_text(messages)
    book = cc.CriteriaBook.from_message(task, min_chars=0, max_items=cc.SAFETY_CAP_MAX)
    if book is None:
        print("no contract criteria in the task statement")
        return 1
    total = len(book.criteria)
    by_id = {m["tool_call_id"]: m for m in messages if m.get("role") == "tool"}
    slowest, passes, calls, mutations = 0.0, 0, 0, 0
    for m in messages:
        for tc in m.get("tool_calls") or []:
            name = tc["function"]["name"]
            arguments = tc["function"].get("arguments") or "{}"
            reply = by_id.get(tc["id"])
            result = _tool_result(reply["content"]) if reply else {"output": "", "exit_code": None}
            if name in FILE_MUTATION_TOOLS:
                kind = "mutation"
            elif name in SHELL_TOOLS:
                kind = "mutation" if (args.mutating_shell and shell_command_looks_mutating(arguments)) else "shell"
            else:
                kind = "read"
            try:
                path_args = json.loads(arguments)
            except ValueError:
                path_args = {}
            paths = [str(path_args[k]) for k in ("path", "file_path", "filename") if isinstance(path_args, dict)
                     and path_args.get(k)]
            call = book.record_call(tc["id"], name, arguments, result, kind=kind, ok=True, paths=paths)
            calls += 1
            mutations += kind == "mutation"
            if call is not None and kind != "mutation" and not args.end_only:
                t0 = time.perf_counter()
                book.auto_link([call], budget_s=args.budget)
                slowest = max(slowest, time.perf_counter() - t0)
                passes += 1
    t0 = time.perf_counter()
    book.auto_link(budget_s=args.budget)
    final_s = time.perf_counter() - t0
    slowest = max(slowest, final_s)
    states = Counter(c["state"] for c in book.criteria)
    proven = [c for c in book.criteria if c["state"] == "proven"]
    print(f"criteria: {total}; calls replayed: {calls} ({mutations} mutations); "
          f"fresh after the last mutation: {len(book.auto_candidates())}")
    print(f"proven by the harness: {len(proven)} / {total}   states: {dict(states)}")
    print("by kind: " + ", ".join(
        f"{k} {sum(1 for c in proven if c['kind'] == k)}/{sum(1 for c in book.criteria if c['kind'] == k)}"
        for k in sorted({c["kind"] for c in book.criteria})))
    print(f"auto_link invocations: {passes} per-call + 1 final; slowest {slowest:.2f}s (final {final_s:.2f}s)")
    if args.explain:
        calls_by_id = {c["call_id"]: c for c in book.calls}
        for c in proven:
            ref = (c.get("evidence_refs") or [""])[0]
            call = calls_by_id.get(ref) or {}
            print(f"  {c['id']} [{c['kind']}] {str(c.get('quote'))[:80]!r}\n"
                  f"      <- {ref}: {str(call.get('command'))[:100]!r} exit={call.get('exit_code')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
