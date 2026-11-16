"""Per-thread agent control plane.

A Studio conversation (a session) can be worked by more than Faustus's own
model. This module keeps, per thread:

* **the agent that answers** — ``faustus`` (the thread's own model, the normal
  chat path) or any CLI runner from :mod:`src.agent_runners` (Claude Code,
  Codex, OpenCode…). When the agent is a runner, a message sent with
  :func:`start_turn` runs that runner with the thread's context and its answer
  is written back into the thread as an assistant message. The runner's own
  session id is kept, so the next turn resumes it instead of starting cold,
  and only what other agents said meanwhile is summarised for it;
* **hand-offs** — :func:`start_handoff` passes one concrete task to another
  agent with a summary of the thread, and the result comes back into the same
  thread without changing who answers next;
* **an optional git worktree** — one branch per thread, created from a repo
  the user names. Runner turns work inside it; :func:`worktree_status` gives
  the thread's diff against the commit it started from, and commit, push and
  pull request act on it.

State lives in ``DATA_DIR/thread_agents.json``; runs execute through
:func:`src.external_worker.run_task`, the same runner path dispatch uses.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

FAUSTUS = "faustus"
FAUSTUS_LABEL = "Faustus (modelo del hilo)"
SUMMARY_BUDGET = 6000
SUMMARY_PER_MESSAGE = 900
DIFF_LIMIT = 200_000
MAX_JOBS_KEPT = 20

_LOCK = threading.RLock()
_JOBS: Dict[str, Dict[str, Any]] = {}
_SID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


# ── storage ─────────────────────────────────────────────────────────────────

def _data_dir() -> str:
    from src import constants
    return constants.DATA_DIR


def _store_path() -> str:
    return os.path.join(_data_dir(), "thread_agents.json")


def _load() -> Dict[str, Any]:
    try:
        with open(_store_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: Dict[str, Any]) -> None:
    path = _store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _check_sid(sid: str) -> str:
    sid = str(sid or "")
    if not _SID_RE.match(sid):
        raise ValueError("invalid thread id")
    return sid


def _blank() -> Dict[str, Any]:
    return {"agent": FAUSTUS, "model": None, "workspace": None, "worktree": None,
            "sessions": {}, "jobs": []}


def _thread(data: Dict[str, Any], sid: str) -> Dict[str, Any]:
    row = data.get(sid)
    if not isinstance(row, dict):
        row = _blank()
        data[sid] = row
    for key, value in _blank().items():
        row.setdefault(key, value)
    return row


def _update(sid: str, fn: Callable[[Dict[str, Any]], Any]) -> Any:
    with _LOCK:
        data = _load()
        out = fn(_thread(data, sid))
        _save(data)
        return out


# ── agents ──────────────────────────────────────────────────────────────────

def agents(*, which: Any = None) -> List[Dict[str, Any]]:
    """Faustus plus every CLI runner Faustus knows how to run one task with.
    ``installed`` says whether this machine has the binary."""
    from src import agent_runners as reg
    rows = [{"key": FAUSTUS, "label": FAUSTUS_LABEL, "installed": True, "runner": False}]
    for row in reg.catalogue(which=which):
        if row.get("kind") != "cli" or not row.get("invocation_known"):
            continue
        rows.append({"key": row["key"], "label": row["label"], "installed": bool(row.get("installed")),
                     "runner": True})
    return rows


def _label(agent: str) -> str:
    if agent == FAUSTUS:
        return FAUSTUS_LABEL
    from src import agent_runners as reg
    runner = reg.get(agent)
    return runner.label if runner is not None else agent


def _resolve_agent(agent: Any) -> str:
    key = str(agent or "").strip().lower()
    if key == FAUSTUS:
        return key
    from src import agent_runners as reg
    runner = reg.get(key)
    if runner is None or runner.kind != "cli" or not runner.argv:
        raise ValueError(f"unknown agent {agent!r}: use 'faustus' or a runner from /api/agent-runners")
    return runner.key


def state(sid: str) -> Dict[str, Any]:
    """The thread's agent, model, workspace, worktree and recent jobs."""
    sid = _check_sid(sid)
    with _LOCK:
        row = _thread(_load(), sid)
    jobs = [_public(_JOBS.get(j["id"], j)) for j in row.get("jobs") or []]
    return {"session_id": sid, "agent": row["agent"], "agent_label": _label(row["agent"]),
            "model": row.get("model"), "workspace": row.get("workspace"),
            "worktree": row.get("worktree"), "jobs": jobs[-MAX_JOBS_KEPT:],
            "running": [j for j in jobs if j.get("status") == "running"]}


def set_agent(sid: str, agent: Any, *, model: Optional[str] = None,
              workspace: Optional[str] = None) -> Dict[str, Any]:
    """Choose who answers the next messages of this thread."""
    sid = _check_sid(sid)
    key = _resolve_agent(agent)
    if workspace is not None:
        workspace = str(workspace).strip() or None
        if workspace and not os.path.isdir(workspace):
            raise ValueError(f"workspace is not a folder: {workspace}")

    def apply(row):
        row["agent"] = key
        row["model"] = (str(model).strip() or None) if model is not None else None
        if workspace is not None or key == FAUSTUS:
            row["workspace"] = workspace
    _update(sid, apply)
    return state(sid)


# ── context summary ─────────────────────────────────────────────────────────

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _speaker(message: Any) -> str:
    role = getattr(message, "role", None) or (message.get("role") if isinstance(message, dict) else "")
    meta = getattr(message, "metadata", None) or (message.get("metadata") if isinstance(message, dict) else None) or {}
    if role == "user":
        return "Usuario"
    if isinstance(meta, dict) and meta.get("agent_label"):
        return str(meta["agent_label"])
    return "Faustus"


def summarize(messages: List[Any], *, budget: int = SUMMARY_BUDGET,
              per_message: int = SUMMARY_PER_MESSAGE) -> str:
    """A bounded transcript of the user and assistant messages, newest kept
    first when the budget runs out; each message clipped to ``per_message``."""
    lines: List[str] = []
    used = 0
    for message in reversed(list(messages or [])):
        role = getattr(message, "role", None) or (message.get("role") if isinstance(message, dict) else "")
        if role not in ("user", "assistant"):
            continue
        content = getattr(message, "content", None) if not isinstance(message, dict) else message.get("content")
        text = " ".join(_text_of(content).split())
        if not text:
            continue
        if len(text) > per_message:
            text = text[: per_message - 1] + "…"
        line = f"- {_speaker(message)}: {text}"
        if used + len(line) > budget:
            lines.append("- (mensajes anteriores omitidos)")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(reversed(lines))


def turn_prompt(text: str, context: str, *, resumed: bool) -> str:
    if resumed:
        head = ("Desde tu última respuesta, en el hilo se dijo:\n" + context + "\n\n") if context else ""
        return head + "Mensaje nuevo:\n" + text
    return ("Trabajas dentro de un hilo de conversación de Faustus.\n"
            + (("Resumen de lo hablado hasta ahora:\n" + context + "\n\n") if context else "\n")
            + "Mensaje nuevo:\n" + text)


def handoff_prompt(task: str, context: str) -> str:
    return ("Te pasan una tarea concreta desde un hilo de conversación de Faustus.\n"
            + (("Contexto del hilo (resumido):\n" + context + "\n\n") if context else "\n")
            + "Tarea:\n" + task + "\n\n"
            + "Cuando termines, responde con el resultado: qué hiciste, qué encontraste y qué queda.")


# ── runs ────────────────────────────────────────────────────────────────────

def _session_manager():
    from src.ai_interaction import get_session_manager
    return get_session_manager()


def _history(sid: str) -> List[Any]:
    return list(_session_manager().get_session(sid).history or [])


def _append(sid: str, role: str, content: str, metadata: Dict[str, Any]) -> None:
    from core.models import ChatMessage
    _session_manager().add_message(sid, ChatMessage(role, content, metadata=metadata))


def _workspace_for(sid: str, row: Dict[str, Any]) -> str:
    tree = row.get("worktree") or {}
    if tree.get("path") and os.path.isdir(tree["path"]):
        return tree["path"]
    if row.get("workspace") and os.path.isdir(row["workspace"]):
        return row["workspace"]
    scratch = os.path.join(_data_dir(), "thread_agents", sid[:40])
    os.makedirs(scratch, exist_ok=True)
    return scratch


def _runner_call(*args, **kwargs):
    """Indirection so tests can replace the real process launch."""
    from src import external_worker
    return external_worker.run_task(*args, **kwargs)


def _start(sid: str, *, kind: str, agent: str, text: str, model: Optional[str],
           owner: Optional[str]) -> Dict[str, Any]:
    if agent == FAUSTUS:
        raise ValueError("Faustus answers through the normal chat; choose a runner to run here")
    with _LOCK:
        row = _thread(_load(), sid)
    history = _history(sid)
    label = _label(agent)
    saved = (row.get("sessions") or {}).get(agent) if kind == "turn" else None
    resume = saved.get("id") if isinstance(saved, dict) and saved.get("id") else None
    if kind == "turn":
        since = history[int(saved.get("seen") or 0):] if resume else history
        prompt = turn_prompt(text, summarize(since), resumed=bool(resume))
    else:
        prompt = handoff_prompt(text, summarize(history))
    workspace = _workspace_for(sid, row)
    job = {"id": uuid.uuid4().hex[:12], "session_id": sid, "kind": kind, "agent": agent,
           "agent_label": label, "model": model, "task": text[:500], "status": "running",
           "started": time.time(), "finished": None, "ok": None, "error": None,
           "workspace": workspace, "resumed": bool(resume), "cancel": False}
    note = text if kind == "turn" else f"Pasar a {label}: {text}"
    _append(sid, "user", note, {"source": "thread_agent", "thread_agent": agent, "agent_label": label,
                                "job_id": job["id"], "kind": kind})
    with _LOCK:
        _JOBS[job["id"]] = job

    def remember(r):
        r["jobs"] = (list(r.get("jobs") or []) + [{k: v for k, v in job.items() if k != "cancel"}])[-MAX_JOBS_KEPT:]
    _update(sid, remember)
    threading.Thread(target=_run, args=(job, prompt, resume, owner), daemon=True,
                     name=f"thread-agent-{job['id']}").start()
    return _public(job)


def _run(job: Dict[str, Any], prompt: str, resume: Optional[str], owner: Optional[str]) -> None:
    sid, agent = job["session_id"], job["agent"]
    try:
        result = _runner_call(agent, prompt, workspace=job["workspace"], model=job["model"],
                              resume=resume, owner=owner, workspace_roots=[job["workspace"]],
                              run_id=f"thread-{job['id']}", should_cancel=lambda: bool(job.get("cancel")))
    except Exception as exc:  # noqa: BLE001 - run_task never raises; a stub might
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    ok = bool(result.get("ok"))
    answer = str(result.get("response_text") or "").strip()
    if not answer:
        answer = str(result.get("output_tail") or "").strip()
    if not ok:
        why = str(result.get("error") or result.get("why") or result.get("outcome") or "falló")
        answer = f"{job['agent_label']} no terminó: {why}" + (f"\n\n{answer}" if answer else "")
    job.update(status="cancelled" if result.get("cancelled") else ("done" if ok else "failed"),
               finished=time.time(), ok=ok, error=None if ok else str(result.get("error") or result.get("why") or ""),
               seconds=result.get("seconds"))
    meta = {"source": "thread_agent", "thread_agent": agent, "agent_label": job["agent_label"],
            "model": job["agent_label"] + (f" · {job['model']}" if job["model"] else ""),
            "job_id": job["id"], "kind": job["kind"], "ok": ok, "seconds": result.get("seconds")}
    try:
        _append(sid, "assistant", answer or f"{job['agent_label']} terminó sin texto.", meta)
    except Exception as exc:  # noqa: BLE001 - the thread may have been deleted meanwhile
        job["error"] = f"could not write the answer into the thread: {exc}"

    def remember(r):
        if job["kind"] == "turn" and result.get("session_id"):
            r.setdefault("sessions", {})[agent] = {"id": result["session_id"], "seen": len(_history(sid))}
        r["jobs"] = [({k: v for k, v in job.items() if k != "cancel"} if j.get("id") == job["id"] else j)
                     for j in r.get("jobs") or []]
    try:
        _update(sid, remember)
    except Exception:  # noqa: BLE001
        pass


def _public(job: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in job.items() if k != "cancel"}
    if out.get("status") == "running" and out.get("id") not in _JOBS:
        # Saved as running by a process that is gone: it will never finish.
        out["status"] = "interrupted"
    return out


def start_turn(sid: str, text: str, *, owner: Optional[str] = None) -> Dict[str, Any]:
    """Send ``text`` to the thread's current agent (a runner); its answer is
    appended to the thread when the run ends."""
    sid = _check_sid(sid)
    text = str(text or "").strip()
    if not text:
        raise ValueError("empty message")
    with _LOCK:
        row = _thread(_load(), sid)
    return _start(sid, kind="turn", agent=row["agent"], text=text, model=row.get("model"), owner=owner)


def start_handoff(sid: str, agent: Any, task: str, *, model: Optional[str] = None,
                  owner: Optional[str] = None) -> Dict[str, Any]:
    """Pass one task to ``agent`` with a summary of the thread; the result is
    appended to the same thread. The thread's own agent does not change."""
    sid = _check_sid(sid)
    task = str(task or "").strip()
    if not task:
        raise ValueError("empty task")
    return _start(sid, kind="handoff", agent=_resolve_agent(agent), text=task,
                  model=(str(model).strip() or None) if model else None, owner=owner)


def job(sid: str, job_id: str) -> Dict[str, Any]:
    sid = _check_sid(sid)
    with _LOCK:
        live = _JOBS.get(job_id)
        if live is not None and live["session_id"] == sid:
            return _public(live)
        for row in _thread(_load(), sid).get("jobs") or []:
            if row.get("id") == job_id:
                return _public(row)
    raise KeyError(job_id)


def cancel(sid: str, job_id: str) -> Dict[str, Any]:
    sid = _check_sid(sid)
    with _LOCK:
        live = _JOBS.get(job_id)
        if live is None or live["session_id"] != sid:
            raise KeyError(job_id)
        live["cancel"] = True
    return _public(live)


# ── worktree ────────────────────────────────────────────────────────────────

def _git(path: str, *args: str, timeout: float = 60) -> str:
    from src import git_panel
    proc = git_panel.run_git(path, *args, timeout=timeout)
    if proc.returncode != 0:
        raise ValueError((proc.stderr or proc.stdout or f"git {args[0]} failed").strip()[:500])
    return proc.stdout or ""


def _gitop(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one src.git_panel operation; its exceptions become ValueError
    carrying git's own message."""
    try:
        return fn(*args, **kwargs)
    except (KeyError, ValueError):
        raise
    except Exception as exc:  # noqa: BLE001 - git_panel has one class per failure
        detail = (getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc) or type(exc).__name__)
        raise ValueError(str(detail).strip()[:500]) from exc


def _tree(sid: str) -> Dict[str, Any]:
    with _LOCK:
        tree = _thread(_load(), sid).get("worktree")
    if not tree or not tree.get("path"):
        raise KeyError("this thread has no worktree")
    return tree


def create_worktree(sid: str, repo: str, *, base: Optional[str] = None,
                    branch: Optional[str] = None) -> Dict[str, Any]:
    """Create a git worktree for this thread on a new branch from ``base``
    (default: the repo's current HEAD)."""
    sid = _check_sid(sid)
    repo = str(repo or "").strip()
    if not repo or not os.path.isdir(repo):
        raise ValueError(f"repo is not a folder: {repo!r}")
    with _LOCK:
        if _thread(_load(), sid).get("worktree"):
            raise ValueError("this thread already has a worktree")
    top = _git(repo, "rev-parse", "--show-toplevel").strip()
    base_ref = str(base or "").strip() or "HEAD"
    base_sha = _git(top, "rev-parse", "--verify", base_ref + "^{commit}").strip()
    base_branch = str(base or "").strip() or _git(top, "rev-parse", "--abbrev-ref", "HEAD").strip()
    name = str(branch or "").strip() or f"thread/{sid[:8]}"
    path = os.path.join(_data_dir(), "thread_worktrees", sid[:40])
    from src import git_panel
    _gitop(git_panel.worktree_add, top, path, base_sha, branch=name)
    tree = {"repo": top, "path": path, "branch": name, "base_sha": base_sha,
            "base_branch": base_branch if base_branch != "HEAD" else "", "created": time.time()}
    _update(sid, lambda row: row.__setitem__("worktree", tree))
    return tree


def worktree_status(sid: str) -> Dict[str, Any]:
    """Branch, head, changed files and the thread's diff against the commit
    the worktree started from (commits and uncommitted work together)."""
    sid = _check_sid(sid)
    tree = _tree(sid)
    path = tree["path"]
    if not os.path.isdir(path):
        return {**tree, "missing": True, "files": [], "patch": "", "commits": []}
    head = _git(path, "rev-parse", "HEAD").strip()
    files = []
    for line in _git(path, "-c", "core.quotepath=off", "status", "--porcelain=v1").splitlines():
        if len(line) > 3:
            files.append({"status": line[:2].strip() or "?", "path": line[3:]})
    patch = _git(path, "diff", "--no-color", "--no-ext-diff", tree["base_sha"])
    from src import git_panel
    for item in files:
        if item["status"] == "??" and len(patch) < DIFF_LIMIT:
            try:
                patch += git_panel.working_diff(path, item["path"], staged=False).get("diff", "")
            except Exception:  # noqa: BLE001 - a binary or vanished file is skipped
                pass
    commits = [line for line in _git(path, "log", "--oneline", "--no-decorate",
                                     f"{tree['base_sha']}..HEAD").splitlines() if line.strip()]
    truncated = len(patch) > DIFF_LIMIT
    return {**tree, "head": head, "files": files, "commits": commits,
            "patch": patch[:DIFF_LIMIT], "truncated": truncated}


def worktree_commit(sid: str, message: str) -> Dict[str, Any]:
    sid = _check_sid(sid)
    message = str(message or "").strip()
    if not message:
        raise ValueError("empty commit message")
    tree = _tree(sid)
    from src import git_panel
    _gitop(git_panel.stage, tree["path"], all_=True)
    out = _gitop(git_panel.commit, tree["path"], message)
    return {"ok": True, "commit": out, "head": _git(tree["path"], "rev-parse", "HEAD").strip()}


def worktree_push(sid: str) -> Dict[str, Any]:
    sid = _check_sid(sid)
    tree = _tree(sid)
    from src import git_panel
    out = _gitop(git_panel.push, tree["path"])
    return {"ok": True, "output": out, "branch": tree["branch"]}


async def worktree_pr(sid: str, *, title: str, body: str = "", base: Optional[str] = None,
                      draft: bool = False) -> Dict[str, Any]:
    sid = _check_sid(sid)
    tree = _tree(sid)
    title = str(title or "").strip()
    if not title:
        raise ValueError("empty pull request title")
    target = str(base or "").strip() or tree.get("base_branch") or "main"
    from src import github_pr
    try:
        out = await github_pr.open_pull_request(tree["path"], base=target, head=tree["branch"],
                                                title=title, body=str(body or ""), draft=bool(draft))
    except github_pr.GithubPRError as exc:
        raise ValueError(str(exc)) from exc
    return {"ok": True, **out}


def remove_worktree(sid: str, *, force: bool = False) -> Dict[str, Any]:
    """Remove the thread's worktree folder; the branch and its commits stay."""
    sid = _check_sid(sid)
    tree = _tree(sid)
    from src import git_panel
    if os.path.isdir(tree["path"]):
        _gitop(git_panel.worktree_remove, tree["repo"], tree["path"], force=force)
    _update(sid, lambda row: row.__setitem__("worktree", None))
    return {"ok": True, "branch": tree["branch"], "repo": tree["repo"]}
