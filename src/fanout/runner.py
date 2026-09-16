"""src/fanout/runner.py — executes one `FanoutPlan`: an isolated alternative
per candidate (`src.alternatives`), a worker running the SAME prompt inside
each (`src.agent_tools.subagent_tools._run_subagent`), bounded concurrency,
reserved budget, and a resumable JSON checkpoint per candidate.

Checkpoint layout (``DATA_DIR/fanout/<run_id>/``):
  * ``run.json``       — the plan, owner, experiment id, overall status.
  * ``<slug(label)>.json`` — one candidate's state, diff stats, tests
    result, cost and (once scored) its score breakdown.

Resumability: ``run_all`` only ever touches candidates whose checkpoint is
NOT in a terminal state (``done``/``error``/``scored``). Calling it a
second time for the same run — after this process died mid-run and left a
candidate's checkpoint at ``running``, or simply because the first call
covered only some of them — picks up exactly the unfinished ones and
leaves the finished ones untouched. That is the whole resume mechanism;
there is no separate "resume" function to fall out of sync with this one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.atomic_io import atomic_write_json
from src import alternatives, budget_account
from src.agent_tools.subagent_tools import DEFAULT_RESERVE_TOKENS_PER_ROUND, SubagentRun, _run_subagent
from src.fanout.plan import FanoutCandidate, FanoutPlan

logger = logging.getLogger(__name__)

TERMINAL_STATES = ("done", "error", "scored")
DEFAULT_TEST_TIMEOUT = 180.0


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def _root_dir() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "fanout")


def _run_dir(run_id: str) -> str:
    return os.path.join(_root_dir(), run_id)


def _manifest_path(run_id: str) -> str:
    return os.path.join(_run_dir(run_id), "run.json")


def slug(label: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", label.strip()).strip("-").lower()
    return s or "candidate"


def _candidate_path(run_id: str, label: str) -> str:
    return os.path.join(_run_dir(run_id), f"{slug(label)}.json")


def _load_json(path: str) -> Optional[Dict[str, Any]]:
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _save_manifest(manifest: Dict[str, Any]) -> None:
    atomic_write_json(_manifest_path(manifest["run_id"]), manifest)


def _save_candidate(run_id: str, cand: Dict[str, Any]) -> None:
    atomic_write_json(_candidate_path(run_id, cand["label"]), cand)


def load_manifest(run_id: str) -> Optional[Dict[str, Any]]:
    return _load_json(_manifest_path(run_id))


def load_candidate(run_id: str, label: str) -> Optional[Dict[str, Any]]:
    return _load_json(_candidate_path(run_id, label))


def load_all_candidates(run_id: str) -> List[Dict[str, Any]]:
    manifest = load_manifest(run_id)
    if not manifest:
        return []
    out = []
    for c in manifest.get("candidates", []):
        cand = load_candidate(run_id, c["label"])
        out.append(cand or c)
    return out


class FanoutNotFoundError(Exception):
    pass


class FanoutOwnerError(Exception):
    pass


def _manifest_or_error(run_id: str, owner: str) -> Dict[str, Any]:
    manifest = load_manifest(run_id)
    if manifest is None:
        raise FanoutNotFoundError(f"no fan-out run {run_id!r}")
    if str(manifest.get("owner") or "") != str(owner or ""):
        raise FanoutNotFoundError(f"no fan-out run {run_id!r}")
    return manifest


# ---------------------------------------------------------------------------
# start: set up the experiment + one isolated alternative per candidate,
# everything at state "queued". Does NOT run anything — see `run_all`.
# ---------------------------------------------------------------------------
def start(owner: str, plan: FanoutPlan) -> str:
    if not (plan.prompt or "").strip():
        raise ValueError("fanout.start: `prompt` is required")
    if not plan.candidates:
        raise ValueError("fanout.start: at least one candidate is required")
    plan._dedupe_labels()

    run_id = f"fanout-{uuid.uuid4().hex[:12]}"
    exp = alternatives.create_experiment(
        owner, plan.project_id or "", plan.goal or plan.prompt, plan.workspace,
    )
    exp_id = exp["id"]

    candidates_manifest: List[Dict[str, Any]] = []
    for candidate in plan.candidates:
        alt = alternatives.add_alternative(owner, exp_id, candidate.label)
        cand_ckpt = {
            "run_id": run_id, "label": candidate.label, "alt_id": alt["id"],
            "model": candidate.model, "endpoint_url": candidate.endpoint_url,
            "endpoint_id": candidate.endpoint_id, "profile": candidate.profile,
            "state": "queued", "error": None,
            "diff": None, "tests": None, "cost": None, "latency_s": None,
            "score": None, "started_at": None, "finished_at": None,
            "path": alt["path"],
        }
        candidates_manifest.append({"label": candidate.label, "alt_id": alt["id"]})
        _save_candidate(run_id, cand_ckpt)

    manifest = {
        "run_id": run_id, "owner": owner, "exp_id": exp_id,
        "plan": plan.to_dict(), "status": "queued",
        "created_at": time.time(), "started_at": None, "finished_at": None,
        "candidates": candidates_manifest,
    }
    _save_manifest(manifest)
    return run_id


# ---------------------------------------------------------------------------
# Concurrency: bounded by `agent_fanout_max_parallel` overall, with an extra
# lane of exactly one for candidates that look local — two big local models
# do not fit in one card's VRAM at once (same reasoning `DelegateAgentsTool`
# already applies to its own worker slots, one endpoint at a time; see
# `src.vram_fit` for the arithmetic behind that rule in the single-model
# case — this fan-out keeps to the cheap heuristic below rather than a full
# admission check across arbitrary local model pairs).
# ---------------------------------------------------------------------------
_LOCAL_HINTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def _looks_local(endpoint_url: str) -> bool:
    url = (endpoint_url or "").lower()
    return (not url) or any(h in url for h in _LOCAL_HINTS)


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


async def run_all(
    run_id: str, owner: str, *,
    coordinator_endpoint_url: str = "", coordinator_model: str = "",
    coordinator_headers: Optional[Dict[str, str]] = None,
    on_update: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    test_command_override: str = "",
) -> Dict[str, Any]:
    """Run every non-terminal candidate of `run_id` to completion (worker +
    project tests). Resumable: candidates already `done`/`error`/`scored`
    are left untouched, so calling this twice — or once after a previous
    call was killed mid-way — only does the remaining work.
    """
    manifest = _manifest_or_error(run_id, owner)
    plan = FanoutPlan.from_dict(manifest["plan"])
    exp_id = manifest["exp_id"]

    try:
        max_parallel = max(1, int(_setting("agent_fanout_max_parallel", 2) or 2))
    except (TypeError, ValueError):
        max_parallel = 2
    overall_slots = asyncio.Semaphore(max_parallel)
    local_slots = asyncio.Semaphore(1)

    manifest["status"] = "running"
    if not manifest.get("started_at"):
        manifest["started_at"] = time.time()
    _save_manifest(manifest)

    budget_run_id = run_id
    budget_account.open(budget_run_id, ceiling_tokens=int(plan.budget_tokens or 0),
                         project_id=plan.project_id or "")

    async def _emit(cand: Dict[str, Any]) -> None:
        if on_update is not None:
            try:
                await on_update(dict(cand))
            except Exception:
                logger.debug("fanout run_all: on_update callback failed", exc_info=True)

    async def _one(entry: Dict[str, Any]) -> None:
        label = entry["label"]
        cand = load_candidate(run_id, label) or dict(entry)
        if cand.get("state") in TERMINAL_STATES:
            return
        endpoint_url = cand.get("endpoint_url") or ""
        model = cand.get("model") or coordinator_model
        headers = coordinator_headers
        endpoint_id = cand.get("endpoint_id") or ""
        if not endpoint_url and endpoint_id:
            try:
                from src.endpoint_resolver import resolve_endpoint_by_id
                resolved = resolve_endpoint_by_id(endpoint_id, model or None, owner=owner)
            except Exception:
                resolved = None
            if resolved and resolved[0]:
                endpoint_url, model, headers = resolved[0], (resolved[1] or model), resolved[2]
        if not endpoint_url:
            endpoint_url = coordinator_endpoint_url

        local = _looks_local(endpoint_url)
        cand["state"] = "running"
        cand["started_at"] = time.time()
        cand["error"] = None
        _save_candidate(run_id, cand)
        await _emit(cand)

        reservation = budget_account.reserve(
            budget_run_id, slug(label), tokens=int(plan.max_rounds) * DEFAULT_RESERVE_TOKENS_PER_ROUND,
        )
        from src.budget_account import BudgetExceeded
        if isinstance(reservation, BudgetExceeded):
            cand["state"] = "error"
            cand["error"] = reservation.reason
            cand["finished_at"] = time.time()
            _save_candidate(run_id, cand)
            await _emit(cand)
            return

        async def _guarded() -> None:
            if local:
                async with local_slots:
                    await _run_one_candidate(run_id, owner, exp_id, cand, endpoint_url, model, headers, plan)
            else:
                await _run_one_candidate(run_id, owner, exp_id, cand, endpoint_url, model, headers, plan)

        try:
            async with overall_slots:
                await _guarded()
        finally:
            cand = load_candidate(run_id, label) or cand
            used_tokens = int(cand.get("_tokens_used") or 0)
            budget_account.reconcile(budget_run_id, slug(label), used_tokens, cand.get("cost"))
            await _emit(cand)

    await asyncio.gather(*(_one(e) for e in manifest["candidates"]))

    manifest = load_manifest(run_id) or manifest
    all_cands = load_all_candidates(run_id)
    manifest["status"] = "done" if all(c.get("state") in TERMINAL_STATES for c in all_cands) else "running"
    if manifest["status"] == "done":
        manifest["finished_at"] = time.time()
    _save_manifest(manifest)
    return manifest


async def _run_one_candidate(run_id: str, owner: str, exp_id: str, cand: Dict[str, Any],
                              endpoint_url: str, model: str, headers: Optional[Dict[str, str]],
                              plan: FanoutPlan) -> None:
    label = cand["label"]
    alt_path = cand["path"]
    run = SubagentRun(0, {"name": label, "instruction": plan.prompt}, role="worker")

    async def _emit(_payload: Dict[str, Any]) -> None:
        return None

    try:
        await _run_subagent(
            run, endpoint_url=endpoint_url, model=model, headers=headers, owner=owner,
            workspace=alt_path, workspace_roots=[alt_path], max_rounds=plan.max_rounds,
            shared_context="", parent_session_id=None, emit=_emit,
            timeout_s=None, save_transcript=False,
        )
    except Exception as exc:  # noqa: BLE001 - one candidate's crash must not sink the others
        run.error = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("fanout: candidate %s crashed: %s", label, exc, exc_info=True)

    report = run.report()
    cand["_tokens_used"] = int(report.get("input_tokens") or 0) + int(report.get("output_tokens") or 0)
    cand["worker_report"] = {
        k: report.get(k) for k in (
            "status", "stop_reason", "error", "tool_calls", "failed_calls",
            "mutations", "rejections", "final_text",
        )
    }
    cand["latency_s"] = report.get("duration_s")

    # Diff stats against the shared base (src.alternatives already computes
    # this for the whole experiment; recomputed per-candidate here so a
    # single worker's failure never blocks the others' diff/tests).
    try:
        exp = alternatives.get_experiment(owner, exp_id)
        alt = next((a for a in exp["alternatives"] if a["id"] == cand["alt_id"]), None)
        if alt is not None:
            changed = alternatives._alt_changed_files(exp, alt)  # noqa: SLF001 - same module family
            cand["diff"] = {
                "files_changed": len(changed),
                "additions": sum(v.get("additions") or 0 for v in changed.values()),
                "deletions": sum(v.get("deletions") or 0 for v in changed.values()),
                "files": sorted(changed.keys())[:200],
            }
    except alternatives.AlternativesError as exc:
        logger.debug("fanout: diff stat failed for %s: %s", label, exc)

    # Run the target project's own tests inside this candidate's isolated
    # copy (rule 3 of the OpenMontage ficha: auto-verify, never trust prose).
    try:
        from src import project_tests
        spec = project_tests.detect_test_command(alt_path, override=None)
        if spec and spec.get("argv"):
            command = shlex.join(spec["argv"])
            tests_result = alternatives.run_tests(owner, exp_id, cand["alt_id"], command,
                                                    timeout=DEFAULT_TEST_TIMEOUT)
            cand["tests"] = tests_result
        else:
            cand["tests"] = {"ok": None, "output": "no test runner detected", "command": None}
    except alternatives.AlternativesError as exc:
        cand["tests"] = {"ok": False, "output": str(exc), "command": None}
    except Exception as exc:  # noqa: BLE001
        cand["tests"] = {"ok": False, "output": f"{type(exc).__name__}: {exc}", "command": None}

    cand["cost"] = None  # unknown by default: a provider that gave no price is not free (see score.py)
    cand["state"] = "error" if run.error else "done"
    cand["error"] = run.error
    cand["finished_at"] = time.time()
    _save_candidate(run_id, cand)
