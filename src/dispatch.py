"""Dispatch: local workers for an outside coordinator (Fable / Claude / any
API caller), so the expensive model plans and reviews and never reads a tool
transcript.

    job = start(owner, {"tasks": [...], "workspace": "D:/proj", ...})
    ...
    compact(job)  →  a few hundred tokens: per task status, what changed,
                     the verification, the worker's last words; never the
                     transcript.

Each job runs the SAME machinery as `/agents` in a chat (delegate_agents:
src/agent_tools/subagent_tools.py — file locks, watchdog, supervisor, the
machine-wide GPU semaphore, lean toolset, the control board), inside a
"Workers" chat of its own so the human can open the board, steer or stop a
worker, and read the transcripts. The model is `dispatch_endpoint_id` /
`dispatch_model` from settings (falls back to the utility, then the default
chat model), or the request's `model` on that endpoint; the request never
names a URL.

What makes the answer trustworthy (none of it comes from the worker):
  * evidence   — the workspace is checkpointed before the job (the harness's
                 shadow repo) and diffed after it: `changes` is what really
                 changed on disk, `claimed_only` what a worker said it changed
                 but did not;
  * verification — Faustus runs the project's tests (or the `verify` command
                 the coordinator gave) in the workspace after the workers,
                 compares failures with the checkpoint (pre-existing vs new),
                 and, when they fail, sends ONE fixer worker with the failure
                 output before verifying again (`fix_rounds`);
  * honest status — `done` only when every worker finished and the
                 verification passed (or could not run); otherwise `partial`
                 with the reason in `verdict`; a cancelled job still reports
                 what changed.
Jobs in the same workspace run one at a time (a second one waits, `queued`);
a retried POST with the same `Idempotency-Key` returns the first job.

Jobs live in memory with a JSON mirror under DATA_DIR/dispatch/ (rotated at
MAX_JOBS_KEPT) so a finished job can still be read after a restart (a
running one is reported as `interrupted`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_TASKS = 4                 # delegate_agents' own cap (MAX_SUBAGENTS)
MAX_JOBS_KEPT = 200
SUMMARY_CHARS = 1200          # the worker's last words, per task
EVENTS_KEPT = 400
CHANGES_LISTED = 60           # paths listed per change kind in the compact result
_DEFAULT_TIMEOUT_S = 900
_DEFAULT_MAX_ROUNDS = 20
_DEFAULT_FIX_ROUNDS = 1
_MAX_FIX_ROUNDS = 2
_VERIFY_TIMEOUT_S = 300
_VERIFY_CMD_CHARS = 500
_IDEMPOTENCY_TTL_S = 3600
_LIVE = ("queued", "running", "verifying", "cancelling")
_SNAPSHOT_MAX_FILES = 60_000
_SNAPSHOT_SKIP = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".mypy_cache", ".pytest_cache",
                            ".ruff_cache", ".tox", "dist", "build", ".idea", ".vscode", "target", ".next", ".cache",
                            ".odysseus_checkpoints", ".faustus"})

_jobs: Dict[str, "DispatchJob"] = {}
_lock = asyncio.Lock()
_idempotent: Dict[Tuple[str, str], Tuple[str, float]] = {}   # (owner, key) → (job id, ts)
_loaded_all_at = 0.0


def _data_dir() -> str:
    try:
        from src.constants import DATA_DIR
        return os.path.join(DATA_DIR, "dispatch")
    except Exception:  # pragma: no cover
        return os.path.join(os.getcwd(), "data", "dispatch")


class DispatchJob:
    def __init__(self, owner: Optional[str], args: Dict[str, Any], workspace: Optional[str],
                 endpoint_url: str, model: str, headers: Optional[Dict[str, str]],
                 title: str, gen_overrides: Optional[Dict[str, Any]] = None,
                 verify: str = "auto", verify_scope: str = "related", fix_rounds: int = _DEFAULT_FIX_ROUNDS,
                 verify_timeout_s: float = _VERIFY_TIMEOUT_S):
        self.id = uuid.uuid4().hex[:12]
        self.owner = owner
        self.args = args
        self.workspace = workspace
        self.endpoint_url = endpoint_url
        self.model = model
        self.headers = headers
        self.title = title
        self.gen_overrides = gen_overrides
        self.verify = verify                    # "auto" | "none" | a shell command
        self.verify_scope = verify_scope        # "related" | "all"
        self.fix_rounds = fix_rounds
        self.verify_timeout_s = verify_timeout_s
        self.created = time.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        # queued | running | verifying | done | partial | error | cancelling | cancelled | interrupted
        self.status = "queued"
        self.error: Optional[str] = None
        self.verdict: Optional[str] = None
        self.session_id: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.changes: Optional[Dict[str, Any]] = None        # observed by Faustus, not claimed by a worker
        self.verification: Optional[Dict[str, Any]] = None
        self.checkpoint: Optional[str] = None
        self.events: Deque[Dict[str, Any]] = deque(maxlen=EVENTS_KEPT)
        self.task: Optional[asyncio.Task] = None
        self._waiters: List[asyncio.Event] = []
        self._entered = False                 # _run has begun (its finally will settle the job)

    # ── views ────────────────────────────────────────────────────────────

    def to_dict(self, *, include_result: bool = True, brief: bool = False) -> Dict[str, Any]:
        d = {
            "id": self.id, "owner": self.owner, "title": self.title, "status": self.status,
            "error": self.error, "verdict": self.verdict, "workspace": self.workspace, "model": self.model,
            "session_id": self.session_id, "chat_url": f"/#{self.session_id}" if self.session_id else None,
            "created": self.created, "started": self.started, "finished": self.finished,
            "duration_s": round((self.finished or time.time()) - (self.started or self.created), 1),
            "tasks": [{"name": t.get("name"), "instruction": _squash(t.get("instruction"), 200) if brief else t.get("instruction"),
                       "files": t.get("files") or [], "model": t.get("model") or None}
                      for t in self.args.get("tasks") or []],
            "parallel": bool(self.args.get("parallel")), "reviewer": bool(self.args.get("reviewer")),
            "max_rounds": self.args.get("max_rounds"), "timeout_s": self.args.get("timeout_s"),
            "verify": self.verify, "verify_scope": self.verify_scope, "fix_rounds": self.fix_rounds,
        }
        if include_result:
            d["result"] = self.result
            d["changes"] = self.changes
            d["verification"] = self.verification
            d["checkpoint"] = self.checkpoint
        return d

    def ceiling_s(self) -> int:
        """The most wall-clock the job can take: every worker's timeout in
        turn at the configured parallelism, a reviewer, the verification and
        the fix loop — so a coordinator knows how long to keep waiting."""
        tasks = len(self.args.get("tasks") or [])
        try:
            from src.agent_tools.subagent_tools import _setting
            par = max(1, int(_setting("agent_subagent_max_parallel", 2) or 1))
        except Exception:
            par = 2
        per = int(self.args.get("timeout_s") or _DEFAULT_TIMEOUT_S)
        waves = -(-tasks // par) if self.args.get("parallel") else tasks
        n = per * max(1, waves) + (per if self.args.get("reviewer") else 0)
        if self.verify != "none":
            n += int(self.verify_timeout_s) * (1 + self.fix_rounds) + per * self.fix_rounds
        return int(n)

    def _notify(self) -> None:
        for ev in self._waiters:
            ev.set()
        self._waiters.clear()

    def _persist(self) -> None:
        try:
            d = _data_dir()
            os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, f".{self.id}.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(d, f"{self.id}.json"))
        except Exception as e:  # noqa: BLE001 — a mirror, never load-bearing
            logger.debug("dispatch: persist failed: %s", e)

    def _event(self, **ev: Any) -> None:
        ev.setdefault("ts", time.time())
        ev.setdefault("name", "job")
        self.events.append(ev)


# ── the compact answer ──────────────────────────────────────────────────────

_WS_RE = re.compile(r"\s+")


def _squash(text: Any, limit: int) -> str:
    s = _WS_RE.sub(" ", str(text or "")).strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def _compact_static_checks(sc: Any) -> Any:
    """Only the failures, bounded — a list of 40 `ok: True` rows per worker is
    what turned the "few hundred tokens" answer into 14k."""
    if isinstance(sc, list):
        bad = [x for x in sc if isinstance(x, dict) and x.get("ok") is False]
        return {"checked": len(sc), "failed": [{"path": str(x.get("path"))[:120], "error": _squash(x.get("error"), 200)}
                                              for x in bad[:10]]}
    if isinstance(sc, dict):
        return {k: (_squash(v, 200) if isinstance(v, str) else v) for k, v in list(sc.items())[:8]}
    return _squash(sc, 200)


def compact_from_result(result: Optional[Dict[str, Any]], *, summary_chars: int = SUMMARY_CHARS) -> Dict[str, Any]:
    """What an outside coordinator needs and nothing more: per worker the
    status, the files it says it changed, its tool/round/token counts, the
    static-check failures and its last words — never the transcript. The
    per-worker `git` snapshot is dropped: it was the WHOLE tree's status
    repeated once per worker; the job's `changes` block is the honest one."""
    out: Dict[str, Any] = {"workers": [], "files_changed": [], "totals": {
        "tool_calls": 0, "failed_calls": 0, "rounds": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0}}
    if not isinstance(result, dict):
        return out
    changed: List[str] = []
    for r in result.get("subagents") or []:
        if not isinstance(r, dict):
            continue
        w = {
            "name": r.get("name"), "role": r.get("role") or "worker", "status": r.get("status"),
            "stop_reason": r.get("stop_reason"), "error": _squash(r.get("error"), 300) or None,
            "rounds": int(r.get("rounds") or 0), "tool_calls": int(r.get("tool_calls") or 0),
            "failed_calls": int(r.get("failed_calls") or 0),
            "files_changed": [str(p) for p in list(r.get("mutations") or [])[:40]],
            "input_tokens": int(r.get("input_tokens") or 0), "output_tokens": int(r.get("output_tokens") or 0),
            "duration_s": r.get("duration_s"), "model": r.get("model"),
            "summary": _squash(r.get("final_text"), summary_chars),
            "session_id": r.get("session_id"),
        }
        sc = r.get("static_checks")
        if sc:
            w["static_checks"] = _compact_static_checks(sc)
        if r.get("supervisor"):
            w["supervisor"] = [_squash(x, 160) for x in list(r.get("supervisor") or [])[:4]]
        out["workers"].append(w)
        for p in w["files_changed"]:
            if p not in changed:
                changed.append(p)
        t = out["totals"]
        t["tool_calls"] += w["tool_calls"]
        t["failed_calls"] += w["failed_calls"]
        t["rounds"] += w["rounds"]
        t["input_tokens"] += w["input_tokens"]
        t["output_tokens"] += w["output_tokens"]
        # any worker that did not finish its task counts — stalled, stopped
        # and timed-out workers have no `error` and used to be 0 errors
        if w["error"] or (w["status"] not in ("done", None)):
            t["errors"] += 1
    out["files_changed"] = changed
    if result.get("lock_conflicts"):
        out["lock_conflicts"] = [f"{c.get('worker')} → {c.get('path')}" for c in list(result["lock_conflicts"])[:10]
                                 if isinstance(c, dict)]
    if result.get("dropped_tasks"):
        out["dropped_tasks"] = int(result["dropped_tasks"])
    out["exit_code"] = result.get("exit_code")
    return out


def _seed_progress(job: DispatchJob) -> Dict[str, Dict[str, Any]]:
    """Every task appears in `progress` from the start (`queued`), so a
    4-task job at max_parallel 2 never looks like a 2-worker job."""
    latest: Dict[str, Dict[str, Any]] = {}
    for i, t in enumerate(job.args.get("tasks") or []):
        name = str(t.get("name") or f"worker-{i + 1}")
        latest[name] = {"last_event": "queued"}
    return latest


def compact(job: DispatchJob) -> Dict[str, Any]:
    d = job.to_dict(include_result=False)
    res = compact_from_result(job.result)
    if job.changes is not None:
        # what Faustus SAW on disk beats what a worker SAID it wrote
        observed = list(job.changes.get("added") or []) + list(job.changes.get("modified") or []) + list(job.changes.get("deleted") or [])
        claimed = list(res["files_changed"])
        res["files_changed"] = observed[: CHANGES_LISTED * 3]
        res["claimed_only"] = [p for p in claimed if not _observed(p, job.changes)][:20]
        res["changes"] = job.changes
    if job.verification is not None:
        res["verification"] = job.verification
    if job.status not in _LIVE:
        res["exit_code"] = 0 if job.status == "done" else 1
    d["result"] = res
    # a running job: the board's latest tick per worker, so a poller sees
    # progress without the event stream
    if job.status in _LIVE:
        latest = _seed_progress(job)
        for ev in job.events:
            name = str(ev.get("name") or ev.get("id") or "")
            if not name or name == "job":
                continue
            kind = ev.get("event")
            if kind in ("queued", "started", "round", "tool", "tick", "steer", "supervisor", "done", "error", "guard", "harness"):
                cur = latest.setdefault(name, {})
                cur["last_event"] = kind
                for k in ("round", "elapsed_s", "idle_s", "last_tool", "tool", "stalled", "stall_reason", "status"):
                    if k in ev:
                        cur[k] = ev[k]
        d["progress"] = latest
        d["wait_again"] = True
        d["ceiling_s"] = job.ceiling_s()
        for ev in reversed(job.events):
            if ev.get("name") == "job" and ev.get("message"):
                d["phase"] = ev["message"]
                break
    return d


def _observed(path: str, changes: Dict[str, Any]) -> bool:
    p = str(path or "").replace("\\", "/").strip("/").lower()
    for kind in ("added", "modified", "deleted"):
        for q in changes.get(kind) or []:
            qq = str(q).replace("\\", "/").strip("/").lower()
            if qq == p or qq.endswith("/" + p) or p.endswith("/" + qq):
                return True
    return bool(changes.get("truncated"))


# ── evidence: what changed on disk ──────────────────────────────────────────

def _snapshot(workspace: str) -> Tuple[Dict[str, Tuple[int, int]], bool]:
    """rel path → (mtime_ns, size) for the workspace tree (bounded, skips the
    usual generated folders). The fallback when the harness's checkpoints are
    unavailable (no git on the box)."""
    files: Dict[str, Tuple[int, int]] = {}
    truncated = False
    root = os.path.realpath(workspace)
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in _SNAPSHOT_SKIP:
                                stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            rel = os.path.relpath(entry.path, root).replace("\\", "/")
                            files[rel] = (int(st.st_mtime_ns), int(st.st_size))
                            if len(files) >= _SNAPSHOT_MAX_FILES:
                                return files, True
                    except OSError:
                        continue
        except OSError:
            continue
    return files, truncated


def _diff_snapshots(before: Dict[str, Tuple[int, int]], after: Dict[str, Tuple[int, int]], truncated: bool) -> Dict[str, Any]:
    added = sorted(p for p in after if p not in before)
    deleted = sorted(p for p in before if p not in after)
    modified = sorted(p for p in after if p in before and after[p] != before[p])
    return _changes_block(added, modified, deleted, "mtime", truncated)


def _changes_block(added: List[str], modified: List[str], deleted: List[str], source: str, truncated: bool = False) -> Dict[str, Any]:
    n = len(added) + len(modified) + len(deleted)
    return {"source": source, "count": n, "added": added[:CHANGES_LISTED], "modified": modified[:CHANGES_LISTED],
            "deleted": deleted[:CHANGES_LISTED],
            "truncated": bool(truncated or n > CHANGES_LISTED * 3 or max(len(added), len(modified), len(deleted)) > CHANGES_LISTED)}


def _checkpoint(workspace: str, label: str) -> Optional[str]:
    try:
        from src import workspace_checkpoints as wc
        if not wc.enabled():
            return None
        cp = wc.checkpoint(workspace, label=label)
        return str(cp.get("sha")) if cp and cp.get("sha") else None
    except Exception as e:  # noqa: BLE001
        logger.debug("dispatch: checkpoint failed: %s", e)
        return None


def _changes_since(workspace: str, sha: str) -> Optional[Dict[str, Any]]:
    try:
        from src import workspace_checkpoints as wc
        rows = wc.changed_since(workspace, sha)
    except Exception as e:  # noqa: BLE001
        logger.debug("dispatch: changed_since failed: %s", e)
        return None
    if rows is None:
        return None
    added = sorted(r["path"] for r in rows if r.get("status") == "A")
    deleted = sorted(r["path"] for r in rows if r.get("status") == "D")
    modified = sorted(r["path"] for r in rows if r.get("status") not in ("A", "D"))
    block = _changes_block(added, modified, deleted, "checkpoint")
    block["checkpoint"] = sha[:12]
    return block


def _git_facts(workspace: str) -> Optional[Dict[str, Any]]:
    """The user's own repo, once per job: is it one, how dirty is it now."""
    try:
        from src.agent_harness import git_change_summary
        g = git_change_summary(workspace)
    except Exception:
        return None
    if not g:
        return None
    return {"repo": True, "dirty_count": int(g.get("changed_count") or 0), "shortstat": _squash(g.get("shortstat"), 200)}


class _Evidence:
    """Before/after the job: a checkpoint (content-exact, via the harness's
    shadow repo) or an mtime snapshot of the tree."""

    def __init__(self, workspace: Optional[str], job_id: str):
        self.workspace = workspace
        self.sha: Optional[str] = None
        self.snap: Optional[Dict[str, Tuple[int, int]]] = None
        self.snap_truncated = False
        self.label = f"dispatch {job_id}"

    def before(self) -> None:
        if not self.workspace:
            return
        self.sha = _checkpoint(self.workspace, self.label)
        if not self.sha:
            self.snap, self.snap_truncated = _snapshot(self.workspace)

    def after(self) -> Optional[Dict[str, Any]]:
        if not self.workspace:
            return None
        block = _changes_since(self.workspace, self.sha) if self.sha else None
        if block is None and self.snap is not None:
            now, trunc = _snapshot(self.workspace)
            block = _diff_snapshots(self.snap, now, self.snap_truncated or trunc)
        if block is not None:
            git = _git_facts(self.workspace)
            if git:
                block["git"] = git
        return block


# ── verification: Faustus runs the proof, not the worker ────────────────────

def _verification_spec(workspace: str, verify: str) -> Tuple[Optional[Dict[str, Any]], str]:
    from src import project_tests as pt
    if verify == "none":
        return None, "off"
    if verify and verify != "auto":
        return pt.detect_test_command(workspace, override=verify), "command"
    override = ""
    try:
        from src.settings import get_setting
        override = str(get_setting("agent_project_test_command", "") or "").strip()
    except Exception:
        override = ""
    return pt.detect_test_command(workspace, override=override or None), "auto"


def run_verification(workspace: Optional[str], verify: str, changed: List[str], *, scope: str = "related",
                     timeout_s: float = _VERIFY_TIMEOUT_S, checkpoint_sha: Optional[str] = None) -> Dict[str, Any]:
    """Run the project's tests (or `verify`) in the workspace, bounded, and
    return a compact verdict. `ok` is None when nothing could be run — that is
    "not verified", never "passed"."""
    if not workspace:
        return {"mode": "off", "ran": False, "ok": None, "summary": "no workspace"}
    try:
        spec, mode = _verification_spec(workspace, verify)
    except Exception as e:  # noqa: BLE001
        return {"mode": "auto", "ran": False, "ok": None, "summary": f"verification unavailable: {e}"[:200]}
    if mode == "off":
        return {"mode": "off", "ran": False, "ok": None, "summary": "verification disabled by the request (verify: none)"}
    if not spec:
        return {"mode": mode, "ran": False, "ok": None,
                "summary": "no test runner detected in the workspace (give `verify` a command that proves the task)"}
    from src import project_tests as pt
    res = pt.run_tests(workspace, spec, changed=list(changed or []), scope=scope, timeout_s=timeout_s)
    if res.get("ran") and res.get("ok") is False and not res.get("inconclusive") and checkpoint_sha:
        try:
            res = pt.compare_with_baseline(workspace, checkpoint_sha, spec, res, changed=list(changed or []))
        except Exception as e:  # noqa: BLE001
            logger.debug("dispatch: baseline comparison failed: %s", e)
    out = {
        "mode": mode, "ran": bool(res.get("ran")), "ok": res.get("ok"), "inconclusive": bool(res.get("inconclusive")),
        "kind": res.get("kind"), "command": _squash(res.get("command"), 300), "scope": res.get("scope"),
        "exit_code": res.get("exit_code"), "timed_out": bool(res.get("timed_out")), "duration_s": res.get("duration_s"),
        "summary": _squash(res.get("summary"), 300), "failures": [_squash(f, 200) for f in (res.get("failures") or [])[:10]],
        "output_tail": str(res.get("output_tail") or "")[-1500:],
    }
    if res.get("related_files"):
        out["related_files"] = [str(p) for p in res["related_files"][:12]]
    for k in ("new_failures", "pre_existing"):
        if res.get(k):
            out[k] = [_squash(f, 200) for f in res[k][:10]]
    if res.get("pre_existing_only"):
        out["pre_existing_only"] = True
    return out


def verification_failed(v: Optional[Dict[str, Any]]) -> bool:
    """A verdict that should block `done`: it ran, it failed, it is
    conclusive, and the failures are not all pre-existing."""
    return bool(v and v.get("ran") and v.get("ok") is False and not v.get("inconclusive") and not v.get("pre_existing_only"))


def _fixer_instruction(job: DispatchJob, v: Dict[str, Any], attempt: int) -> str:
    tasks = "\n".join(f"{i}. {_squash(t.get('instruction'), 400)}" for i, t in enumerate(job.args.get("tasks") or [], 1))
    lines = [
        f"[Verification failed after the workers' changes — fix round {attempt}]",
        f"The command `{v.get('command') or v.get('summary')}` FAILED in the workspace: {v.get('summary') or 'failed'}.",
    ]
    pre = set(v.get("pre_existing") or [])
    for f in (v.get("failures") or [])[:8]:
        lines.append(f"- {f}" + (" (already failed before the workers' change)" if f in pre else ""))
    tail = (v.get("output_tail") or "").strip()
    if tail:
        lines.append("Output (tail):")
        lines.append(tail[-2000:])
    lines.append("")
    lines.append("The workers were asked to:")
    lines.append(tasks)
    if job.changes and job.changes.get("count"):
        touched = (job.changes.get("added") or []) + (job.changes.get("modified") or [])
        lines.append("Files they changed: " + ", ".join(touched[:30]))
    lines.append("")
    lines.append("Read the failing test and the code it exercises, fix the CAUSE (not the test, unless the test itself is "
                 "wrong per the task), run the same command yourself until it passes, then report in two sentences what "
                 "was wrong and what you changed. Do not start unrelated work.")
    return "\n".join(lines)


# ── running a job ───────────────────────────────────────────────────────────

def _parse_tasks(raw: Any) -> List[Dict[str, Any]]:
    from src.agent_tools.subagent_tools import parse_delegation_args
    args = parse_delegation_args(json.dumps({"tasks": raw}) if not isinstance(raw, str) else raw)
    return list(args.get("tasks") or [])


def build_args(body: Dict[str, Any]) -> Dict[str, Any]:
    """The delegate_agents payload for a dispatch request (validated by the
    tool's own parser so a job and a chat delegation cannot disagree)."""
    from src.agent_tools.subagent_tools import parse_delegation_args
    payload: Dict[str, Any] = {"tasks": body.get("tasks")}
    for k in ("parallel", "reviewer", "max_rounds", "timeout_s", "reviewer_model"):
        if body.get(k) is not None:
            payload[k] = body[k]
    if body.get("context"):
        payload["context"] = str(body["context"])[:8000]
    args = parse_delegation_args(json.dumps(payload))
    if not args.get("tasks"):
        raise ValueError("tasks is required: a list of instructions or {instruction, files?, model?, name?}")
    if not body.get("max_rounds"):
        args["max_rounds"] = _DEFAULT_MAX_ROUNDS
    if not body.get("timeout_s"):
        args["timeout_s"] = _DEFAULT_TIMEOUT_S
    return args


def _verify_options(body: Dict[str, Any]) -> Tuple[str, str, int, float]:
    raw = body.get("verify")
    if raw is None or raw is True:
        verify = "auto"
    elif raw is False:
        verify = "none"
    else:
        verify = str(raw).strip() or "auto"
        if verify.lower() in ("auto", "none", "off", "false"):
            verify = "none" if verify.lower() in ("none", "off", "false") else "auto"
        elif len(verify) > _VERIFY_CMD_CHARS:
            raise ValueError(f"verify: the command is longer than {_VERIFY_CMD_CHARS} characters")
    scope = str(body.get("verify_scope") or "related").strip().lower()
    if scope not in ("related", "all"):
        raise ValueError("verify_scope must be 'related' or 'all'")
    try:
        fix = int(body.get("fix_rounds", _DEFAULT_FIX_ROUNDS) if body.get("fix_rounds") is not None else _DEFAULT_FIX_ROUNDS)
    except (TypeError, ValueError):
        raise ValueError("fix_rounds must be an integer")
    fix = max(0, min(_MAX_FIX_ROUNDS, fix))
    try:
        vt = float(body.get("verify_timeout_s") or _VERIFY_TIMEOUT_S)
    except (TypeError, ValueError):
        raise ValueError("verify_timeout_s must be a number")
    return verify, scope, fix, max(10.0, min(vt, 3600.0))


_ALLOWED_GEN = frozenset({"temperature", "top_p", "top_k", "repeat_penalty", "seed", "num_ctx", "max_tokens", "num_predict"})


def _clean_gen(raw: Any) -> Optional[Dict[str, Any]]:
    """Sampling knobs only: `main_gpu` / `num_gpu` / `keep_alive` would let a
    request override the GPU placement policy the admin chose."""
    if not isinstance(raw, dict):
        return None
    out = {k: v for k, v in raw.items() if k in _ALLOWED_GEN and isinstance(v, (int, float, str))}
    return out or None


def resolve_route(owner: Optional[str], model: Optional[str] = None) -> tuple[str, str, Optional[Dict[str, str]]]:
    """(endpoint_url, model, headers) for the workers: the dispatch endpoint
    from settings (→ utility → default chat model); `model` picks another
    model ON that endpoint (never another server)."""
    from src.endpoint_resolver import resolve_endpoint
    url, resolved_model, headers = resolve_endpoint("dispatch", owner=owner)
    if not url:
        raise ValueError("no model endpoint is configured for dispatch (Settings → Agent & automation → Fable workers, or a default chat model)")
    # resolve_endpoint only honours `dispatch_model` together with
    # `dispatch_endpoint_id`; a model chosen without an endpoint id (the
    # common case: "the workers use qwen3.5:9b on the usual server") must
    # still win over the utility / default chat model — seen live: the
    # 29 GB q8_0 default model picked up a dispatched job.
    configured = ""
    try:
        from src.settings import get_setting
        configured = str(get_setting("dispatch_model", "") or "").strip()
    except Exception:
        configured = ""
    m = str(model or "").strip() or configured or str(resolved_model or "")
    if not m:
        raise ValueError("no model configured for dispatch")
    return url, m, headers


def _title(args: Dict[str, Any]) -> str:
    tasks = args.get("tasks") or []
    first = _squash((tasks[0] or {}).get("instruction"), 60) if tasks else "workers"
    return f"Workers · {first}" if len(tasks) == 1 else f"Workers ({len(tasks)}) · {first}"


# one job at a time per workspace (nested folders count as the same one)
_running_ws: Dict[str, str] = {}          # workspace key → job id
_ws_waiters: List[asyncio.Event] = []


def _ws_key(path: Optional[str]) -> str:
    if not path:
        return ""
    k = os.path.realpath(path).replace("\\", "/").rstrip("/") + "/"
    return k.lower() if os.name == "nt" else k


def _ws_busy(key: str) -> Optional[str]:
    for other, jid in _running_ws.items():
        if other.startswith(key) or key.startswith(other):
            return jid
    return None


async def _acquire_workspace(job: DispatchJob) -> str:
    key = _ws_key(job.workspace)
    while True:
        holder = _ws_busy(key)
        if holder is None:
            _running_ws[key] = job.id
            return key
        if not job.events or job.events[-1].get("message") != f"waiting for job {holder} in the same workspace":
            job._event(event="job", message=f"waiting for job {holder} in the same workspace")
        ev = asyncio.Event()
        _ws_waiters.append(ev)
        try:
            await asyncio.wait_for(ev.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        finally:
            if ev in _ws_waiters:
                _ws_waiters.remove(ev)


def _release_workspace(key: str, job_id: str) -> None:
    if _running_ws.get(key) == job_id:
        _running_ws.pop(key, None)
    for ev in list(_ws_waiters):
        ev.set()


async def _delegate(job: DispatchJob, args: Dict[str, Any], cb: Callable) -> Dict[str, Any]:
    from src.agent_tools.subagent_tools import DelegateAgentsTool
    tool = DelegateAgentsTool()
    ctx = {"session_id": job.session_id, "owner": job.owner, "progress_cb": cb,
           "gen_overrides": job.gen_overrides or None, "model": job.model}
    result = await tool.execute(json.dumps(args), ctx)
    return result if isinstance(result, dict) else {"output": str(result)}


def _worker_statuses(result: Optional[Dict[str, Any]]) -> List[str]:
    return [str(r.get("status") or "") for r in (result or {}).get("subagents") or [] if isinstance(r, dict)]


def _settle(job: DispatchJob) -> None:
    """The honest top-level answer, from the worker set and the verification —
    never from `exit_code` alone (a stalled or stopped worker has no error)."""
    statuses = _worker_statuses(job.result)
    if job.status in ("cancelled", "interrupted"):
        pass
    elif not statuses:
        job.status = "error"
        job.error = job.error or _squash((job.result or {}).get("error"), 500) or "no worker ran"
    else:
        bad = [s for s in statuses if s != "done"]
        v = job.verification
        if bad or verification_failed(v):
            job.status = "partial"
        else:
            job.status = "done"
    parts = []
    n = len(statuses)
    if n:
        parts.append(f"{n - len([s for s in statuses if s != 'done'])}/{n} workers done"
                     + (" (" + ", ".join(sorted({s for s in statuses if s != 'done'})) + ")" if any(s != "done" for s in statuses) else ""))
    if job.changes is not None:
        n_ch = int(job.changes.get("count") or 0)
        parts.append(f"{n_ch} file{'s' if n_ch != 1 else ''} changed on disk")
    v = job.verification
    if v:
        if v.get("ran") and v.get("ok") is True:
            parts.append(f"verification passed ({v.get('summary') or v.get('command')})")
        elif verification_failed(v):
            parts.append(f"verification FAILED ({v.get('summary')})")
        elif v.get("ran"):
            parts.append(f"verification inconclusive ({v.get('summary')})")
        else:
            parts.append(f"not verified: {v.get('summary')}")
    if job.status == "cancelled":
        parts.insert(0, "cancelled")
    elif job.status == "error":
        parts.insert(0, f"error: {job.error}")
    job.verdict = " · ".join(parts)[:400] or job.status


async def _run(job: DispatchJob) -> None:
    ws_key: Optional[str] = None
    token = roots_token = None
    evidence = _Evidence(job.workspace, job.id)
    job._entered = True
    try:
        from src import tool_execution as te
        ws_key = await _acquire_workspace(job)
        job.started = time.time()
        job.status = "running"
        job._event(event="job", message="checkpointing the workspace")
        job._persist()
        await asyncio.to_thread(evidence.before)
        job.checkpoint = evidence.sha
        job._event(event="job", message="workers running")

        async def _cb(payload: Dict[str, Any]) -> None:
            ev = payload.get("subagent") if isinstance(payload, dict) else None
            if isinstance(ev, dict):
                job.events.append(dict(ev))

        token = te._active_workspace.set(job.workspace or None)
        roots_token = te._active_workspace_roots.set((job.workspace,) if job.workspace else ())
        job.result = await _delegate(job, job.args, _cb)
        if job.result.get("error") and not job.result.get("subagents"):
            job.error = _squash(job.result.get("error"), 500)
        # evidence + verification, then the bounded fix loop
        job.status = "verifying"
        job._event(event="job", message="checking what changed on disk")
        job.changes = await asyncio.to_thread(evidence.after)
        if job.verify != "none":
            job._event(event="job", message="running the verification")
            job.verification = await _verify(job)
            attempt = 0
            while verification_failed(job.verification) and attempt < job.fix_rounds and job.result.get("subagents"):
                attempt += 1
                job._event(event="job", message=f"verification failed — fix round {attempt}")
                fixer_args = dict(job.args)
                fixer_args.update({"tasks": [{"name": f"fixer-{attempt}", "instruction": _fixer_instruction(job, job.verification, attempt),
                                              "files": [], "model": ""}],
                                   "parallel": False, "reviewer": False, "dropped_tasks": 0})
                fix = await _delegate(job, fixer_args, _cb)
                for r in fix.get("subagents") or []:
                    if isinstance(r, dict):
                        r["role"] = "fixer"
                        job.result.setdefault("subagents", []).append(r)
                for c in fix.get("lock_conflicts") or []:
                    job.result.setdefault("lock_conflicts", []).append(c)
                job.changes = await asyncio.to_thread(evidence.after)
                job._event(event="job", message=f"verifying again after fix round {attempt}")
                again = await _verify(job)
                again["attempts"] = attempt + 1
                again["previous"] = [{"summary": job.verification.get("summary"), "failures": job.verification.get("failures")}] \
                    + list(job.verification.get("previous") or [])
                job.verification = again
        else:
            job.verification = {"mode": "off", "ran": False, "ok": None,
                                "summary": "verification disabled by the request (verify: none)"}
    except asyncio.CancelledError:
        job.status = "cancelled"
        try:
            job.changes = evidence.after()
        except Exception:
            pass
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("dispatch %s failed", job.id)
        job.status = "error"
        job.error = str(e)[:500]
    finally:
        try:
            from src import tool_execution as te
            if token is not None:
                te._active_workspace.reset(token)
            if roots_token is not None:
                te._active_workspace_roots.reset(roots_token)
        except Exception:
            pass
        if ws_key is not None:
            _release_workspace(ws_key, job.id)
        job.finished = time.time()
        _settle(job)
        _record_turn(job)
        job._persist()
        job._notify()


async def _verify(job: DispatchJob) -> Dict[str, Any]:
    changed = []
    if job.changes:
        changed = list(job.changes.get("added") or []) + list(job.changes.get("modified") or [])
    return await asyncio.to_thread(run_verification, job.workspace, job.verify, changed, scope=job.verify_scope,
                                   timeout_s=job.verify_timeout_s, checkpoint_sha=job.checkpoint)


def _record_turn(job: DispatchJob) -> None:
    """Write the job into its Workers chat the way a chat turn would: one
    assistant message whose tool_event carries the delegate_agents evidence,
    so the control board is rebuilt from history when the chat is opened
    (the same `subagents` shape src/agent_loop.py persists)."""
    if not job.session_id:
        return
    try:
        from core.models import ChatMessage
        from src.ai_interaction import get_session_manager
        from src.agent_loop import _compact_subagent_reports
        sm = get_session_manager()
        result = job.result if isinstance(job.result, dict) else {}
        reports = result.get("subagents") if isinstance(result.get("subagents"), list) else []
        comp = compact(job)["result"]
        lines = [f"Dispatched job {job.id}: {job.status}" + (f" — {job.verdict}" if job.verdict else "")]
        for w in comp.get("workers") or []:
            lines.append(f"- {w.get('name')}: {w.get('status')}"
                         + (f" — changed {', '.join(w.get('files_changed') or [])}" if w.get("files_changed") else ""))
        if job.changes is not None:
            lines.append(f"Changed on disk: {job.changes.get('count', 0)}"
                         + (" — " + ", ".join(((job.changes.get('added') or []) + (job.changes.get('modified') or []))[:20]) if job.changes.get("count") else ""))
        v = job.verification
        if v:
            lines.append(f"Verification: {v.get('summary')}" + (f" (`{v.get('command')}`)" if v.get("command") else ""))
        ev = {
            "round": 1, "model": job.model, "tool": "delegate_agents",
            "desc": f"{len(job.args.get('tasks') or [])} worker(s) dispatched from outside Faustus",
            "command": json.dumps({"tasks": [t.get("instruction", "")[:300] for t in job.args.get("tasks") or []],
                                   "parallel": bool(job.args.get("parallel"))}, ensure_ascii=False),
            "output": str(result.get("output") or job.error or job.status)[:4000],
            "exit_code": 0 if job.status == "done" else 1,
            "subagents": _compact_subagent_reports(reports) if reports else [],
            "dispatch_id": job.id,
        }
        # with tool_events present the renderer takes the bubbles from
        # round_texts (never from content): the verdict is the text of a
        # second, tool-less round, i.e. it appears under the board
        meta = {"tool_events": [ev], "model": job.model, "source": "dispatch", "dispatch_id": job.id,
                "round_texts": ["", "\n".join(lines)], "round_models": [job.model, job.model]}
        # The same `harness` block a chat turn persists (chatRenderer reads
        # metadata.harness): the 🛡 badge, the edited-file chips with "diff vs
        # before this turn" against the job's checkpoint, the tests line.
        hz: Dict[str, Any] = {
            "stop_reason": "complete" if job.status == "done" else job.status,
            "mutations": list(comp.get("files_changed") or []),
            "tool_calls": int((comp.get("totals") or {}).get("tool_calls") or 0),
            "failed_calls": int((comp.get("totals") or {}).get("failed_calls") or 0),
            "rejections": 0, "workspace": job.workspace, "checkpoint": job.checkpoint,
            "notes": [job.verdict] if job.verdict else [],
        }
        if job.changes and job.changes.get("git"):
            hz["git"] = {"changed_count": job.changes["git"].get("dirty_count"), "shortstat": job.changes["git"].get("shortstat"), "changed": []}
        if v and v.get("ran"):
            hz["tests"] = {k: v.get(k) for k in ("ran", "ok", "summary", "command", "failures", "kind", "scope", "output_tail",
                                                 "inconclusive", "related_files", "duration_s", "pre_existing", "pre_existing_only",
                                                 "new_failures", "timed_out") if k in v}
            hz["tests"]["label"] = v.get("command") or v.get("kind")
            hz["tests_fix_rounds"] = max(0, int(v.get("attempts") or 1) - 1)
        meta["harness"] = hz
        # the footer needs the usual metrics to render at all (and the 🛡
        # badge hangs off that footer): the job's duration and local tokens
        tot = comp.get("totals") or {}
        meta.update({"response_time": round((job.finished or time.time()) - (job.started or job.created), 2),
                     "input_tokens": int(tot.get("input_tokens") or 0), "output_tokens": int(tot.get("output_tokens") or 0),
                     "usage_source": "real"})
        sm.add_message(job.session_id, ChatMessage("assistant", "\n".join(lines), metadata=meta))
        try:
            sm.save_sessions()
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001 — the board is a courtesy; the job's answer is the compact result
        logger.debug("dispatch %s: could not record the turn: %s", job.id, e)


def _make_session(job: DispatchJob) -> str:
    """The parent chat every dispatched job runs in: the board lives there,
    the transcripts land there, the human can steer or stop from there."""
    from src.ai_interaction import get_session_manager
    sm = get_session_manager()
    sid = str(uuid.uuid4())
    session = sm.create_session(session_id=sid, name=job.title, endpoint_url=job.endpoint_url,
                                model=job.model, rag=False, owner=job.owner)
    if job.headers:
        try:
            session.headers = dict(job.headers)
        except Exception:
            pass
    try:
        from core.models import ChatMessage
        # The coordinator's words are NOT the human's: marked the way the
        # repo marks any text that did not come from the user (the tool
        # gate reads `trusted: False` + provenance), so a human who opens
        # the board and continues the chat does not lend them their own
        # standing.
        sm.add_message(sid, ChatMessage(role="user", content=_dispatch_note(job),
                                        metadata={"source": "dispatch", "dispatch_id": job.id, "trusted": False,
                                                  "provenance_origin": "external", "tool_gate_untrusted": True,
                                                  "display": "system_card"}))
    except Exception:
        pass
    return sid


def _dispatch_note(job: DispatchJob) -> str:
    lines = [f"Dispatched from outside Faustus (job {job.id}) — {len(job.args.get('tasks') or [])} task(s), "
             f"workspace: {job.workspace or '—'}, model: {job.model}. "
             "(External instructions: not written by the user of this chat.)"]
    for i, t in enumerate(job.args.get("tasks") or [], 1):
        lines.append(f"{i}. {_squash(t.get('instruction'), 300)}")
    if job.verify != "none":
        lines.append(f"Verification: {'auto-detected test runner' if job.verify == 'auto' else job.verify}"
                     f" ({job.verify_scope}), fix rounds: {job.fix_rounds}")
    return "\n".join(lines)


def _idempotent_get(owner: Optional[str], key: str) -> Optional["DispatchJob"]:
    now = time.time()
    for k, (jid, ts) in list(_idempotent.items()):
        if now - ts > _IDEMPOTENCY_TTL_S:
            _idempotent.pop(k, None)
    hit = _idempotent.get((owner or "", key))
    return get(hit[0]) if hit else None


async def start(owner: Optional[str], body: Dict[str, Any], *, runner: Optional[Callable] = None,
                idempotency_key: Optional[str] = None) -> DispatchJob:
    """Validate, create the Workers chat and launch the job in the background.
    With an idempotency key already seen for this owner, the job it started
    is returned instead of a second one."""
    key = str(idempotency_key or body.get("client_request_id") or "").strip()[:200]
    if key:
        existing = _idempotent_get(owner, key)
        if existing is not None:
            return existing
    args = build_args(body)
    raw_ws = str(body.get("workspace") or "").strip()
    if not raw_ws:
        # without one the workers' cwd is Faustus's own data dir (sessions,
        # settings, auth) — never a place for a worker
        raise ValueError("workspace is required: the absolute folder the workers are confined to")
    from src.tool_execution import vet_workspace
    workspace = vet_workspace(raw_ws)
    if not workspace:
        raise ValueError(f"workspace is not a usable directory: {raw_ws}")
    verify, scope, fix_rounds, verify_timeout = _verify_options(body)
    url, model, headers = resolve_route(owner, body.get("model"))
    gen = _clean_gen(body.get("gen_overrides"))
    job = DispatchJob(owner, args, workspace, url, model, headers, _title(args), gen,
                      verify=verify, verify_scope=scope, fix_rounds=fix_rounds, verify_timeout_s=verify_timeout)
    job.session_id = _make_session(job)
    async with _lock:
        _jobs[job.id] = job
        if key:
            _idempotent[(owner or "", key)] = (job.id, time.time())
        job._persist()
        _evict()
    job.task = asyncio.create_task((runner or _run)(job))
    return job


def _evict() -> None:
    """Keep MAX_JOBS_KEPT finished jobs — in memory AND on disk (the mirrors
    used to come straight back through list_jobs → _load_all)."""
    if len(_jobs) > MAX_JOBS_KEPT:
        for old in sorted(_jobs.values(), key=lambda j: j.created)[: len(_jobs) - MAX_JOBS_KEPT]:
            if old.status not in _LIVE:
                _jobs.pop(old.id, None)
    try:
        d = _data_dir()
        names = [n for n in os.listdir(d) if n.endswith(".json")]
        if len(names) <= MAX_JOBS_KEPT:
            return
        paths = sorted((os.path.getmtime(os.path.join(d, n)), n) for n in names)
        for _, n in paths[: len(paths) - MAX_JOBS_KEPT]:
            jid = n[:-5]
            live = _jobs.get(jid)
            if live is not None and live.status in _LIVE:
                continue
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass
    except OSError:
        pass


def get(job_id: str) -> Optional[DispatchJob]:
    job = _jobs.get(str(job_id or ""))
    if job is not None:
        return job
    return _load(job_id)


def _load(job_id: str) -> Optional[DispatchJob]:
    if not re.fullmatch(r"[0-9a-f]{12}", str(job_id or "")):
        return None
    path = os.path.join(_data_dir(), f"{job_id}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    job = DispatchJob(d.get("owner"), {"tasks": d.get("tasks") or [], "parallel": d.get("parallel"),
                                       "reviewer": d.get("reviewer"), "max_rounds": d.get("max_rounds"),
                                       "timeout_s": d.get("timeout_s")},
                      d.get("workspace"), "", d.get("model") or "", None, d.get("title") or "Workers",
                      verify=str(d.get("verify") or "auto"), verify_scope=str(d.get("verify_scope") or "related"),
                      fix_rounds=int(d.get("fix_rounds") or 0))
    job.id = d.get("id") or job_id
    job.created = float(d.get("created") or 0)
    job.started = d.get("started")
    job.finished = d.get("finished")
    job.session_id = d.get("session_id")
    job.error = d.get("error")
    job.verdict = d.get("verdict")
    job.result = d.get("result")
    job.changes = d.get("changes")
    job.verification = d.get("verification")
    job.checkpoint = d.get("checkpoint")
    # a job that was running when the server stopped never finished
    job.status = "interrupted" if d.get("status") in _LIVE else (d.get("status") or "done")
    if job.status == "interrupted" and not job.verdict:
        job.verdict = "interrupted by a restart of Faustus — re-dispatch the remaining work"
    _jobs[job.id] = job
    return job


def visible_to(job: DispatchJob, owner: Optional[str]) -> bool:
    """One predicate for the list and the by-id read: a named owner sees
    only their jobs; single-user / anonymous mode (owner "" or None) sees
    everything. A job with no owner is nobody's in multi-user mode."""
    if not owner:
        return True
    return job.owner == owner


def list_jobs(owner: Optional[str], limit: int = 50) -> List[Dict[str, Any]]:
    _load_all()
    rows = [j for j in _jobs.values() if visible_to(j, owner)]
    rows.sort(key=lambda j: j.created, reverse=True)
    return [j.to_dict(include_result=False, brief=True) for j in rows[:limit]]


def _load_all() -> None:
    """The newest MAX_JOBS_KEPT mirrors, at most once every 2 s (the Workers
    page polls every 3 s; a listdir per poll is fine, a stat per file is not)."""
    global _loaded_all_at
    if time.time() - _loaded_all_at < 2.0:
        return
    _loaded_all_at = time.time()
    try:
        d = _data_dir()
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return
    missing = [n for n in names if n[:-5] not in _jobs]
    if len(missing) > MAX_JOBS_KEPT:
        try:
            missing = [n for _, n in sorted(((os.path.getmtime(os.path.join(d, n)), n) for n in missing), reverse=True)[:MAX_JOBS_KEPT]]
        except OSError:
            missing = missing[:MAX_JOBS_KEPT]
    for name in missing:
        _load(name[:-5])
    if len(_jobs) > MAX_JOBS_KEPT:
        for old in sorted(_jobs.values(), key=lambda j: j.created)[: len(_jobs) - MAX_JOBS_KEPT]:
            if old.status not in _LIVE:
                _jobs.pop(old.id, None)


async def wait(job: DispatchJob, timeout: float) -> bool:
    """True when the job is finished (possibly already). A cancelled job
    counts as finished only once its workers have unwound."""
    if job.status not in _LIVE:
        return True
    ev = asyncio.Event()
    job._waiters.append(ev)
    try:
        await asyncio.wait_for(ev.wait(), timeout=max(0.0, timeout))
        return True
    except asyncio.TimeoutError:
        return job.status not in _LIVE
    finally:
        if ev in job._waiters:
            job._waiters.remove(ev)


def cancel(job: DispatchJob) -> bool:
    """Stop the job. The status is `cancelling` until the worker tasks have
    unwound (their commands killed, the transcripts saved) and `_run`'s
    finally has recorded what changed on disk; a waiter returns then, never
    while the workspace is still being written."""
    if job.task is not None and not job.task.done():
        if not job._entered:
            # cancelled before its first step: _run never runs its finally
            job.status = "cancelled"
            job.verdict = "cancelled before it started"
            job.finished = time.time()
            job.task.cancel()
            _record_turn(job)
            job._persist()
            job._notify()
            return True
        job.status = "cancelling"
        job._event(event="job", message="cancelling")
        job.task.cancel()
        job._persist()
        return True
    return False


COORDINATOR_GUIDE = """\
# Using Faustus workers (for the coordinating model)

You are the planner and the reviewer. The workers are local models on the
user's machine: cheap, tireless, good at mechanical steps, weaker at judgement.
Your own tokens are the scarce resource — spend them on deciding WHAT to do
and on checking the result, not on reading files or running tests yourself.

## When to dispatch
- Editing or creating files, running tests/linters/builds, fixing what fails,
  refactors with a clear spec, searching a codebase, converting formats,
  writing boilerplate or docs from a spec: dispatch.
- Deciding the design, judging trade-offs, anything ambiguous, anything the
  user must decide, the final answer to the user: keep.

## How to write a task (each task = one worker)
1. Self-contained: name the files, the function/class, the behaviour, and the
   exact command that proves it ("`pytest -q` in the workspace must pass").
   The worker does not see this conversation — everything it needs goes in
   the instruction or in `context`.
2. One outcome per task. Two changes that touch the same file go in ONE task
   (parallel workers lock files against each other).
3. Say what NOT to do when it matters ("do not touch the public API",
   "keep Python 3.11 compatibility").
4. 1–4 tasks per job. Independent tasks → `parallel: true`; dependent ones →
   `parallel: false` (they run in order, and a later task may edit what an
   earlier one wrote). Jobs in the same workspace run one at a time.
5. `workspace` is required: the folder the workers are confined to.
6. `verify` is the command that proves the job is done (`pytest -q`,
   `npm test`, `make check`…). Faustus runs it ITSELF after the workers, in
   the workspace — the workers' own claims are never the proof. Without it
   the project's test runner is auto-detected (`verify: "auto"`);
   `verify_scope: "all"` runs the whole suite instead of the tests related
   to the changed files. `fix_rounds` (default 1, max 2): when the
   verification fails, one fixer worker gets the failure output and the
   verification runs again.

## Reading the result
`status`: `done` = every worker finished AND the verification passed (or
could not run — read `verification.summary`); `partial` = some worker ended
`error` / `timeout` / `stalled` / `stopped`, or the verification failed;
`error` = nothing ran; `cancelled` / `interrupted` = stopped early (the
evidence is still there). `verdict` says it in one line.
`result.changes` is what Faustus SAW change on disk (checkpoint diff) —
`result.files_changed` is that list; `result.claimed_only` names files a
worker said it changed but did not. `result.verification` is the run Faustus
made: `ok`, `summary`, `failures`, `pre_existing` (failed before the job
too), `command`, `output_tail`; `attempts` > 1 means a fix round ran.
Per worker: status, files it claims, tool/round/token counts and its last
words (≤ 1200 chars) — never the transcript. Trust `changes` + `verification`
over the prose.
A `running` answer carries `progress` per worker, `phase`, `ceiling_s` (the
most it can still take) and `wait_again: true` — call `workers_wait` again;
do NOT re-dispatch the same task because one wait returned early.

## Loop
plan → dispatch → wait (again if still running) → read verdict + changes +
verification → (dispatch a narrower fix if needed) → answer the user.
Do not re-do a worker's work yourself; send a narrower task instead. Tell the
user which changes came from the workers and point them at the board
(`chat_url`) if they want the details.
"""


def reset_for_tests() -> None:
    global _loaded_all_at
    _jobs.clear()
    _idempotent.clear()
    _running_ws.clear()
    _ws_waiters.clear()
    _loaded_all_at = 0.0
