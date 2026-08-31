"""scorecard.py — per-model, per-task reliability metrics of agent turns.

Every agent turn that ran with the reliability harness appends one line to
DATA_DIR/scorecard.jsonl: which model answered, how long it took, how many
rounds and tool calls, whether the harness verified the claims, whether the
model asked the user, whether the project's tests passed after the change,
what the reviewer said. `aggregate()` folds those lines into a per-model table
so the user can pick a model with data instead of vibes; the bench
(agent-bench/) gets the same numbers for free because its runs go through the
normal chat API.

Append-only JSONL, one process, best-effort: a write failure is logged and
ignored. Never raises.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_MAX_TASK_CHARS = 120


def _path() -> str:
    try:
        from src.constants import DATA_DIR
    except Exception:  # pragma: no cover
        DATA_DIR = os.path.join(os.getcwd(), "data")
    return os.path.join(DATA_DIR, "scorecard.jsonl")


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def task_label(user_text: str) -> str:
    text = " ".join((user_text or "").split())
    return text[:_MAX_TASK_CHARS]


def build_entry(
    *,
    session_id: Optional[str],
    model: str,
    endpoint_label: Optional[str],
    workspace: Optional[str],
    user_text: str,
    duration_s: float,
    rounds: int,
    harness: Dict[str, Any],
    tests: Optional[Dict[str, Any]] = None,
    review: Optional[Dict[str, Any]] = None,
    tokens_per_second: Optional[float] = None,
    output_tokens: Optional[int] = None,
    asked_user: bool = False,
    task_tag: Optional[str] = None,
) -> Dict[str, Any]:
    stop = str(harness.get("stop_reason") or "complete")
    mutations = list(harness.get("mutations") or [])
    notes = [str(n) for n in (harness.get("notes") or [])]
    static = list(harness.get("static_checks") or [])
    entry: Dict[str, Any] = {
        "ts": int(time.time()),
        "session_id": session_id,
        "model": model,
        "endpoint": endpoint_label,
        "workspace": workspace,
        "task": task_label(user_text),
        "tag": (task_tag or "")[:60] or None,
        "duration_s": round(float(duration_s or 0), 1),
        "rounds": int(rounds or 0),
        "tool_calls": int(harness.get("tool_calls") or 0),
        "failed_calls": int(harness.get("failed_calls") or 0),
        "files_changed": len(mutations),
        "stop_reason": stop,
        "verified": stop == "complete" and not any(n.startswith("unverified") for n in notes),
        "unverified": stop == "complete_unverified",
        "rejections": int(harness.get("rejections") or 0),
        # A real question (ask_user). A stop for the "Allow this task to
        # continue?" gate is not the model asking: that hop is skipped below.
        "asked_user": bool(asked_user),
        "approval_stop": (stop == "awaiting_user" and not asked_user),
        "syntax_errors": len([c for c in static if not c.get("ok")]),
        "whole_file_rewrite": any(n.startswith("whole_file_rewrite") for n in notes),
        "target_substituted": any(n.startswith("target_substituted") for n in notes),
        "tests": None,
        "review": None,
        "tok_s": round(float(tokens_per_second), 1) if tokens_per_second else None,
        "output_tokens": int(output_tokens) if output_tokens else None,
    }
    if tests and tests.get("ran"):
        entry["tests"] = "inconclusive" if tests.get("inconclusive") else ("pass" if tests.get("ok") else "fail")
        entry["tests_fix_rounds"] = int(harness.get("tests_fix_rounds") or 0)
    if review and review.get("verdict") not in (None, "skipped", "error", "unparsed"):
        entry["review"] = review.get("verdict")
        entry["review_errors"] = len([f for f in (review.get("findings") or []) if f.get("severity") == "error"])
        entry["review_model"] = review.get("model")
    return entry


def record(entry: Dict[str, Any]) -> bool:
    if not bool(_setting("agent_scorecard", True)):
        return False
    if entry.get("approval_stop"):
        # The turn paused at the approval gate: the resumed hop records the
        # real outcome. Counting this half would inflate turns and "asks".
        return False
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except (OSError, TypeError, ValueError) as e:
        logger.debug("[scorecard] write failed: %s", e)
        return False


def load(days: Optional[float] = None, limit: int = 20000) -> List[Dict[str, Any]]:
    path = _path()
    if not os.path.isfile(path):
        return []
    cutoff = time.time() - float(days) * 86400 if days else None
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if cutoff and float(e.get("ts") or 0) < cutoff:
                    continue
                out.append(e)
    except OSError:
        return []
    return out[-limit:]


def _rate(num: int, den: int) -> Optional[float]:
    return round(100.0 * num / den, 1) if den else None


def aggregate(entries: Iterable[Dict[str, Any]], *, only_workspace: bool = False) -> List[Dict[str, Any]]:
    """Per-model table. `only_workspace` keeps turns that had a workspace (coding
    work), which is what the scorecard is for."""
    by: Dict[str, List[Dict[str, Any]]] = {}
    for e in entries:
        if only_workspace and not e.get("workspace"):
            continue
        if e.get("approval_stop"):
            continue
        by.setdefault(str(e.get("model") or "?"), []).append(e)
    rows: List[Dict[str, Any]] = []
    for model, items in by.items():
        n = len(items)
        with_changes = [e for e in items if e.get("files_changed")]
        tests_ran = [e for e in items if e.get("tests") in ("pass", "fail")]
        reviewed = [e for e in items if e.get("review") in ("ok", "issues")]
        finished = [e for e in items if e.get("stop_reason") in ("complete", "complete_unverified")]
        rows.append({
            "model": model,
            "turns": n,
            "verified_rate": _rate(len([e for e in finished if e.get("verified")]), len(finished)),
            "unverified": len([e for e in items if e.get("unverified")]),
            "asked_user_rate": _rate(len([e for e in items if e.get("asked_user")]), n),
            "changed_files_rate": _rate(len(with_changes), n),
            "avg_files": round(sum(e.get("files_changed") or 0 for e in with_changes) / len(with_changes), 1) if with_changes else 0,
            "avg_duration_s": round(sum(float(e.get("duration_s") or 0) for e in items) / n, 1),
            "median_duration_s": _median([float(e.get("duration_s") or 0) for e in items]),
            "avg_rounds": round(sum(int(e.get("rounds") or 0) for e in items) / n, 1),
            "avg_tool_calls": round(sum(int(e.get("tool_calls") or 0) for e in items) / n, 1),
            "failed_call_rate": _rate(sum(int(e.get("failed_calls") or 0) for e in items), sum(int(e.get("tool_calls") or 0) for e in items)),
            "rejections": sum(int(e.get("rejections") or 0) for e in items),
            "syntax_errors": sum(int(e.get("syntax_errors") or 0) for e in items),
            "whole_file_rewrites": len([e for e in items if e.get("whole_file_rewrite")]),
            "tests_pass_rate": _rate(len([e for e in tests_ran if e.get("tests") == "pass"]), len(tests_ran)),
            "tests_ran": len(tests_ran),
            "review_ok_rate": _rate(len([e for e in reviewed if e.get("review") == "ok"]), len(reviewed)),
            "reviewed": len(reviewed),
            "stalls": len([e for e in items if e.get("stop_reason") in ("rounds_exhausted", "intent_nudge_exhausted", "loop_breaker", "budget_exceeded")]),
            "avg_tok_s": round(sum(float(e.get("tok_s") or 0) for e in items if e.get("tok_s")) / max(1, len([e for e in items if e.get("tok_s")])), 1) if any(e.get("tok_s") for e in items) else None,
            "last_ts": max(int(e.get("ts") or 0) for e in items),
        })
    rows.sort(key=lambda r: (-(r["verified_rate"] or 0), r["avg_duration_s"]))
    return rows


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return round(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2, 1)


def render_table(rows: List[Dict[str, Any]], language: str = "en") -> str:
    """Markdown table for the /scorecard slash command."""
    if not rows:
        return ("Todavía no hay turnos de agente registrados." if language == "es"
                else "No agent turns recorded yet.")
    es = language == "es"
    head = (["Modelo", "Turnos", "Verificado", "Pregunta", "Tests OK", "Review OK", "Tiempo med.", "Rondas", "Rechazos", "tok/s"] if es
            else ["Model", "Turns", "Verified", "Asks", "Tests OK", "Review OK", "Median time", "Rounds", "Rejections", "tok/s"])
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]

    def pct(v):
        return "—" if v is None else f"{v:.0f}%"
    for r in rows:
        lines.append("| " + " | ".join([
            f"`{r['model']}`", str(r["turns"]), pct(r["verified_rate"]), pct(r["asked_user_rate"]),
            (pct(r["tests_pass_rate"]) + (f" ({r['tests_ran']})" if r["tests_ran"] else "")) if r["tests_ran"] else "—",
            (pct(r["review_ok_rate"]) + f" ({r['reviewed']})") if r["reviewed"] else "—",
            f"{r['median_duration_s']} s" if r["median_duration_s"] is not None else "—",
            str(r["avg_rounds"]), str(r["rejections"]),
            str(r["avg_tok_s"]) if r["avg_tok_s"] else "—",
        ]) + " |")
    return "\n".join(lines)
