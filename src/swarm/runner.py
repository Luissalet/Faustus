"""src/swarm/runner.py — runs one swarm: the same instruction applied to
every item, as many at once as the backend really serves, each item retried
once and recorded on its own, then (optionally) one reduce pass.

Rules the code below keeps:

  * **One item never sinks the run.** An item that fails twice is recorded
    as failed with its error; the others carry on.
  * **The checkpoint is the truth.** ``run`` only works on items with no line
    in ``results.jsonl``; calling it again after a crash, a restart or a
    cancel does the rest and nothing twice.
  * **Background, not starving the chat.** A call to a local server runs in
    the swarm lane (:mod:`src.swarm.lane`): it waits while a foreground call
    wants the model and never holds the one-pipe lock the chat waits on.
  * **Headers are never written to disk.** The route is stored as how to
    find it (endpoint id / the chat it came from / the task model) and
    resolved again on every ``run``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from src.swarm import capacity, lane, render, store

logger = logging.getLogger(__name__)

TERMINAL = ("done", "partial", "failed", "cancelled")
MAX_ATTEMPTS = 2  # the first try plus one retry
DEFAULT_LLM_TIMEOUT = 180.0
DEFAULT_AGENT_TIMEOUT = 900.0
MAX_ITEM_TIMEOUT = 3600.0
DEFAULT_AGENT_ROUNDS = 6
MAX_AGENT_ROUNDS = 12
REDUCE_CHUNK_CHARS = 24000
REDUCE_ROW_CHARS = 2000
REDUCE_MAX_TOKENS = 4096

#: Tools a swarm worker never gets: a worker that could start another swarm
#: or another delegation would multiply the run past every limit set on it.
RECURSION_TOOLS = frozenset({"swarm_map", "swarm_cancel", "delegate_agents", "fanout_run",
                             "night_shift"})

LLM_SYSTEM = ("You handle ONE item of a larger batch. Do exactly what the instruction asks for this "
              "item only and reply with the result itself — no preamble, no questions.")

AGENT_PREAMBLE = (
    "You are one worker of a swarm: the same instruction is being applied to many items at once, "
    "and you have exactly ONE of them. Use your tools only as far as this one item needs, do not "
    "ask the user anything, and finish with the result for this item as your final answer — "
    "short, factual, nothing about the other items.")

#: run_id -> the tasks of its in-flight items (this process only).
_ITEM_TASKS: Dict[str, Set[asyncio.Task]] = {}
_CANCELLED: Set[str] = set()

ProgressCb = Optional[Callable[[Dict[str, Any]], Awaitable[None]]]


class ItemError(Exception):
    pass


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------
def resolve_route(spec: Dict[str, Any], owner: str) -> Tuple[str, str, Optional[Dict[str, str]], str]:
    """(url, model, headers, source) for a stored route spec."""
    model = str(spec.get("model") or "")
    endpoint_id = str(spec.get("endpoint_id") or "")
    if endpoint_id:
        from src.endpoint_resolver import resolve_endpoint_by_id
        resolved = resolve_endpoint_by_id(endpoint_id, model or None, owner=owner or None)
        if resolved and resolved[0]:
            return resolved[0], resolved[1] or model, resolved[2], "endpoint_id"
        raise ItemError(f"endpoint {endpoint_id!r} could not be resolved")
    session_id = str(spec.get("session_id") or "")
    if session_id:
        try:
            from src.ai_interaction import get_session_manager
            sm = get_session_manager()
            session = sm.get_session(session_id) if sm else None
        except Exception:  # noqa: BLE001
            session = None
        url = str(getattr(session, "endpoint_url", "") or "") if session is not None else ""
        if url:
            return (url, model or str(getattr(session, "model", "") or ""),
                    getattr(session, "headers", None) or None, "session")
    from src.endpoint_resolver import resolve_endpoint
    url, resolved_model, headers = resolve_endpoint("task", owner=owner or None)
    if not url:
        raise ItemError("no model endpoint is configured for background work (task/utility model)")
    return url, model or str(resolved_model or ""), headers, "task"


# ---------------------------------------------------------------------------
# One call (module-level so tests replace them)
# ---------------------------------------------------------------------------
async def _llm_call(url: str, model: str, messages: List[Dict[str, Any]], *,
                    headers: Optional[Dict[str, str]], timeout: float,
                    response_schema: Optional[Dict[str, Any]] = None,
                    max_tokens: int = 0) -> str:
    from src.llm_core import llm_call_async
    kwargs: Dict[str, Any] = {}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    return await llm_call_async(
        url, model, messages, temperature=0.2, headers=headers, timeout=int(max(10, timeout)),
        max_retries=1, workload="background", response_schema=response_schema, **kwargs,
    )


def _perms_from_dict(raw: Optional[Dict[str, Any]]):
    if not raw:
        return None
    from src.agent_defs import Rule
    from src.subagent_permissions import ChildPermissions
    rules = tuple(Rule(str(r.get("action")), str(r.get("pattern")), str(r.get("effect")))
                  for r in raw.get("rules") or () if isinstance(r, dict))
    allowed = raw.get("allowed_tools")
    return ChildPermissions(
        slug=str(raw.get("slug") or ""), label=str(raw.get("label") or ""), rules=rules,
        denied_tools=frozenset(raw.get("denied_tools") or ()),
        allowed_tools=None if allowed is None else frozenset(allowed),
        may_delegate=False, depth=int(raw.get("depth") or 1),
        workspace_roots=tuple(raw.get("workspace_roots") or ()),
        workspace=str(raw.get("workspace") or ""),
    )


async def _agent_call(manifest: Dict[str, Any], index: int, prompt: str, *, url: str, model: str,
                      headers: Optional[Dict[str, str]], owner: str) -> Dict[str, Any]:
    """One limited worker for one item (the `delegate_agents` worker, reused)."""
    from src.agent_tools.subagent_tools import SubagentRun, _run_subagent
    agent = manifest.get("agent") or {}
    run = SubagentRun(index, {"name": f"swarm {manifest['run_id'][-6:]} #{index + 1}",
                              "instruction": prompt, "system_prompt": AGENT_PREAMBLE}, role="worker")
    run.permissions = _perms_from_dict(agent.get("permissions"))
    run.lane_disabled_tools = set(RECURSION_TOOLS)
    workspace = str(agent.get("workspace") or "") or None
    roots = list(agent.get("workspace_roots") or ([workspace] if workspace else []))

    async def _emit(_payload: Dict[str, Any]) -> None:
        return None

    await _run_subagent(
        run, endpoint_url=url, model=model, headers=headers, owner=owner,
        workspace=workspace, workspace_roots=roots or None,
        max_rounds=int(agent.get("max_rounds") or DEFAULT_AGENT_ROUNDS),
        shared_context="", parent_session_id=manifest.get("session_id") or None,
        emit=_emit, timeout_s=None, save_transcript=False,
    )
    report = run.report()
    if run.error:
        raise ItemError(str(run.error))
    text = (run.text or "").strip()
    if not text:
        raise ItemError(f"the worker ended with no answer (stop reason: {report.get('stop_reason')})")
    # A worker that finished cleanly leaves no chat behind: a 200-item run
    # would otherwise add 200 chats to the sidebar. A failed one keeps its
    # chat, so what went wrong can be read.
    try:
        from src.ai_interaction import get_session_manager
        sm = get_session_manager()
        if sm and getattr(run, "session_id", None):
            sm.delete_session(run.session_id)
    except Exception:  # noqa: BLE001
        logger.debug("swarm: could not remove a worker chat", exc_info=True)
    return {"text": text, "tokens": int(report.get("input_tokens") or 0) + int(report.get("output_tokens") or 0),
            "tool_calls": report.get("tool_calls"), "rounds": report.get("rounds")}


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def request_cancel(run_id: str) -> int:
    """Stop dispatching and cancel the in-flight items (they stay pending,
    so a resume redoes them). Returns how many were cancelled."""
    _CANCELLED.add(run_id)
    n = 0
    for task in list(_ITEM_TASKS.get(run_id, ())):
        if not task.done():
            task.cancel()
            n += 1
    return n


def _counts(results: Dict[int, Dict[str, Any]], total: int) -> Dict[str, int]:
    ok = sum(1 for r in results.values() if r.get("status") == "ok")
    failed = sum(1 for r in results.values() if r.get("status") == "failed")
    return {"total": total, "ok": ok, "failed": failed, "pending": max(0, total - ok - failed)}


async def _process_item(manifest: Dict[str, Any], index: int, item: Any, *, url: str, model: str,
                        headers: Optional[Dict[str, str]], owner: str, timeout: float) -> Dict[str, Any]:
    fields = manifest.get("output_fields") or None
    prompt = render.render_prompt(manifest["instruction"], item, fields)
    started = time.time()
    if manifest.get("mode") == "agent":
        result = await asyncio.wait_for(
            _agent_call(manifest, index, prompt, url=url, model=model, headers=headers, owner=owner),
            timeout=timeout)
        text = result["text"]
        extra = {k: result.get(k) for k in ("tokens", "tool_calls", "rounds")}
    else:
        messages = [{"role": "system", "content": LLM_SYSTEM}, {"role": "user", "content": prompt}]
        text = await asyncio.wait_for(
            _llm_call(url, model, messages, headers=headers, timeout=timeout,
                      response_schema=render.response_schema(fields)),
            timeout=timeout)
        text = (text or "").strip() if isinstance(text, str) else str(text or "").strip()
        extra = {}
        if not text:
            raise ItemError("empty reply")
    row: Dict[str, Any] = {"output": text, "latency_s": round(time.time() - started, 2), **extra}
    if fields:
        parsed = render.parse_fields(text, fields)
        if parsed is None:
            raise ItemError("the reply was not the JSON object with the asked fields")
        row["fields"] = parsed
    return row


async def run(run_id: str, owner: str, *, on_progress: ProgressCb = None) -> Dict[str, Any]:
    """Run every pending item of `run_id`, then write the table and reduce.
    Safe to call again: finished items are never redone."""
    manifest = store.manifest_for(run_id, owner)
    items = store.load_items(run_id)
    _CANCELLED.discard(run_id)
    results = store.load_results(run_id)
    pending = [i for i in range(len(items)) if i not in results]

    manifest.update({"status": "running", "error": None, "cancel_requested": False})
    manifest.setdefault("started_at", None)
    manifest["started_at"] = manifest["started_at"] or time.time()
    manifest["finished_at"] = None
    try:
        url, model, headers, source = resolve_route(manifest.get("route") or {}, owner)
    except Exception as exc:  # noqa: BLE001
        manifest.update({"status": "failed", "error": f"route: {exc}", "finished_at": time.time()})
        store.save_manifest(manifest)
        return manifest
    cap = await capacity.effective_parallel(url)
    requested = int(manifest.get("max_parallel") or 0)
    parallel = max(1, min(int(cap.get("parallel") or 1), max(1, len(pending)),
                          requested if requested > 0 else 10 ** 6))
    manifest["parallel"] = {**cap, "effective": parallel}
    manifest["resolved"] = {"endpoint_url": url, "model": model, "source": source}
    manifest["counts"] = _counts(results, len(items))
    store.save_manifest(manifest)

    timeout = float(manifest.get("per_item_timeout") or
                    (DEFAULT_AGENT_TIMEOUT if manifest.get("mode") == "agent" else DEFAULT_LLM_TIMEOUT))
    use_lane = bool(cap.get("local"))
    sem = asyncio.Semaphore(parallel)
    tasks: Set[asyncio.Task] = _ITEM_TASKS.setdefault(run_id, set())

    async def _progress(last: Optional[Dict[str, Any]] = None) -> None:
        manifest["counts"] = _counts(results, len(items))
        store.save_manifest(manifest)
        if on_progress is not None:
            try:
                await on_progress({"run_id": run_id, **manifest["counts"],
                                   "last": ({"index": last["index"] + 1, "status": last["status"]}
                                            if last else None)})
            except Exception:  # noqa: BLE001
                logger.debug("swarm: progress callback failed", exc_info=True)

    async def _one(index: int) -> None:
        async with sem:
            if run_id in _CANCELLED:
                return
            attempts = 0
            error = ""
            row: Optional[Dict[str, Any]] = None
            while attempts < MAX_ATTEMPTS and run_id not in _CANCELLED:
                attempts += 1
                try:
                    ctx = lane.enter(url, run_id, parallel) if use_lane else contextlib.nullcontext()
                    with ctx:
                        row = await _process_item(manifest, index, items[index], url=url, model=model,
                                                  headers=headers, owner=owner, timeout=timeout)
                    break
                except asyncio.CancelledError:
                    if run_id in _CANCELLED:
                        return  # stays pending: a resume redoes it
                    raise
                except asyncio.TimeoutError:
                    error = f"timed out after {int(timeout)} s"
                except Exception as exc:  # noqa: BLE001 - one item never sinks the run
                    error = f"{type(exc).__name__}: {exc}"[:500] if not isinstance(exc, ItemError) else str(exc)[:500]
                logger.info("swarm %s: item %d attempt %d failed: %s", run_id, index + 1, attempts, error)
            if row is None and run_id in _CANCELLED:
                return
            record = {"index": index, "status": "ok" if row is not None else "failed",
                      "attempts": attempts, "finished_at": time.time()}
            if row is not None:
                record.update(row)
            else:
                record["error"] = error or "failed"
            store.append_result(run_id, record)
            results[index] = record
            await _progress(record)

    for index in pending:
        tasks.add(asyncio.ensure_future(_one(index)))
    try:
        await asyncio.gather(*list(tasks), return_exceptions=True)
    finally:
        _ITEM_TASKS.pop(run_id, None)

    results = store.load_results(run_id)
    counts = _counts(results, len(items))
    manifest = store.load_manifest(run_id) or manifest
    manifest["counts"] = counts
    cancelled = run_id in _CANCELLED or bool(manifest.get("cancel_requested"))
    if cancelled:
        manifest["status"] = "cancelled"
    elif counts["ok"] == counts["total"]:
        manifest["status"] = "done"
    elif counts["ok"] == 0:
        manifest["status"] = "failed"
    else:
        manifest["status"] = "partial"

    reduce_output = ""
    if manifest.get("reduce") and not cancelled and counts["ok"] > 0:
        manifest["reduce_result"] = await _reduce(manifest, items, results, url=url, model=model,
                                                  headers=headers, use_lane=use_lane, parallel=parallel)
        reduce_output = (manifest["reduce_result"] or {}).get("output") or ""
    try:
        _export(manifest, items, results, reduce_output)
    except Exception as exc:  # noqa: BLE001 - the table is extra; the results are on disk
        logger.warning("swarm %s: export failed: %s", run_id, exc, exc_info=True)
        manifest["export_error"] = str(exc)[:300]
    manifest["finished_at"] = time.time()
    store.save_manifest(manifest)
    _CANCELLED.discard(run_id)
    if on_progress is not None:
        try:
            await on_progress({"run_id": run_id, **counts, "status": manifest["status"], "last": None})
        except Exception:  # noqa: BLE001
            pass
    return manifest


# ---------------------------------------------------------------------------
# Reduce
# ---------------------------------------------------------------------------
def _reduce_lines(items: List[Any], results: Dict[int, Dict[str, Any]]) -> List[str]:
    lines = []
    for index in sorted(results):
        row = results[index]
        if row.get("status") != "ok":
            continue
        answer = row.get("fields") if row.get("fields") is not None else row.get("output")
        answer_text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        if len(answer_text) > REDUCE_ROW_CHARS:
            answer_text = answer_text[:REDUCE_ROW_CHARS] + "…"
        item_text = render.render_item(items[index]) if index < len(items) else ""
        if len(item_text) > 300:
            item_text = item_text[:300] + "…"
        lines.append(f"[{index + 1}] {item_text} => {answer_text}")
    return lines


def _chunks(lines: List[str], budget: int) -> List[List[str]]:
    out: List[List[str]] = []
    cur: List[str] = []
    size = 0
    for line in lines:
        if cur and size + len(line) + 1 > budget:
            out.append(cur)
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        out.append(cur)
    return out


async def _reduce(manifest: Dict[str, Any], items: List[Any], results: Dict[int, Dict[str, Any]], *,
                  url: str, model: str, headers: Optional[Dict[str, str]], use_lane: bool,
                  parallel: int) -> Dict[str, Any]:
    instruction = str(manifest.get("reduce") or "").strip()
    lines = _reduce_lines(items, results)
    failed = sum(1 for r in results.values() if r.get("status") == "failed")
    note = (f"\n\n({failed} item(s) failed and are not in the results above.)" if failed else "")
    timeout = float(manifest.get("reduce_timeout") or 600)

    async def _call(prompt: str) -> str:
        messages = [{"role": "system", "content": "You combine the per-item results of a batch job into "
                     "one answer. Use only the results given; say so when something is missing."},
                    {"role": "user", "content": prompt}]
        ctx = lane.enter(url, manifest["run_id"], parallel) if use_lane else contextlib.nullcontext()
        with ctx:
            return str(await asyncio.wait_for(
                _llm_call(url, model, messages, headers=headers, timeout=timeout, max_tokens=REDUCE_MAX_TOKENS),
                timeout=timeout) or "").strip()

    chunks = _chunks(lines, REDUCE_CHUNK_CHARS)
    try:
        if len(chunks) <= 1:
            output = await _call(f"{instruction}\n\nResults ({len(lines)} items):\n" + "\n".join(lines) + note)
        else:
            partials = []
            for n, chunk in enumerate(chunks, 1):
                partials.append(await _call(
                    f"{instruction}\n\nThis is part {n} of {len(chunks)} of the results; answer for this "
                    f"part only — a later step combines the parts.\n\nResults:\n" + "\n".join(chunk)))
            output = await _call(
                f"{instruction}\n\nThe results were too many for one pass; below are the answers for each "
                f"of {len(chunks)} parts. Combine them into the one final answer.\n\n"
                + "\n\n".join(f"Part {i}:\n{p}" for i, p in enumerate(partials, 1)) + note)
        if not output:
            return {"status": "failed", "error": "empty reply", "chunks": len(chunks)}
        return {"status": "ok", "output": output, "chunks": len(chunks)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300], "chunks": len(chunks)}


# ---------------------------------------------------------------------------
# Table + artifacts
# ---------------------------------------------------------------------------
def export_base(run_id: str) -> str:
    return run_id


def _export(manifest: Dict[str, Any], items: List[Any], results: Dict[int, Dict[str, Any]],
            reduce_output: str) -> None:
    run_id = manifest["run_id"]
    fields = manifest.get("output_fields") or None
    records = render.table_rows(list(results.values()), items, fields)
    cols = render.columns(fields)
    title = f"Swarm {run_id}: {_short(manifest.get('instruction'), 100)}"
    paths = render.write_exports(store.exports_dir(run_id), export_base(run_id), records, cols,
                                 title=title, reduce_output=reduce_output)
    manifest["files"] = sorted(paths)
    manifest["artifacts"] = _record_artifacts(manifest, paths)


def _short(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _record_artifacts(manifest: Dict[str, Any], paths: Dict[str, str]) -> List[Dict[str, Any]]:
    """Copy the exports into the artifact store (best effort: a run whose
    table could not be recorded still has it under its own directory)."""
    try:
        from src import artifact_store
        from src.contracts import ExecutionResult
    except Exception:  # noqa: BLE001
        return []
    out: List[Dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="faustus-swarm-") as tmp:
            for name, path in paths.items():
                shutil.copyfile(path, os.path.join(tmp, name))
            execution = ExecutionResult.parse({
                "run_id": manifest["run_id"], "backend": "swarm",
                "status": "completed",
                "partial": manifest.get("status") != "done",
                "artifact_filenames": sorted(paths),
            })
            collected = artifact_store.collect(
                execution, source_dir=tmp, owner=str(manifest.get("owner") or ""), project_id="",
                skill_id="swarm.map", skill_version="1.0.0",
                provenance={"note": f"swarm {manifest['run_id']} ({manifest.get('mode')})"},
            )
            if collected.artifacts:
                artifact_store.persist(collected.artifacts, session_id=str(manifest.get("session_id") or ""))
            for art in collected.artifacts:
                out.append({"id": art.id, "name": art.label, "filename": art.filename, "kind": art.kind})
    except Exception as exc:  # noqa: BLE001
        logger.warning("swarm %s: artifact recording failed: %s", manifest.get("run_id"), exc)
    return out
