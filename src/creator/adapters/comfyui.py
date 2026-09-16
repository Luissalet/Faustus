"""adapters/comfyui.py — WP10: ComfyUI behind the `AdapterPort` contract.

Wraps `src.media_runs` and `src.media_backends.comfyui`, both already the
render lifecycle's single authority (ADR-02) and untouched by this file —
every method below calls straight through to a public function those modules
already export, with the SAME signature they already had. Nothing here
changes what a legacy render does: `media_runs.start()`/`.poll()`/`.cancel()`/
`.reconcile_run()` are called exactly as any other caller would call them.

`describe()` reflects the outbox reconciliation `media_runs` already has
(`reconcile_run`, `find_by_client_id`) as `supports_reconcile=True` — that is
existing, demonstrated behaviour, not a new promise this adapter invents.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from src.creator.adapter_port import (
    AdapterManifest, AdapterPlan, CancelResult, CollectResult, CollectedOutput,
    StatusResult, Staging, SubmitResult,
)
from src.creator.adapters.base import BaseAdapter

NAME = "comfyui"

#: `media_runs` status -> the adapter port's status vocabulary. Same
#: strings except MediaRun's outbox-only states (`submit_pending`,
#: `submitted`, `submit_unknown`, `pending`) fold to `queued`/`unknown` —
#: they describe the SUBMIT, not the render, and a caller asking `status()`
#: about a job id is asking about the render.
_STATUS_MAP = {
    "submit_pending": "queued", "submitted": "queued", "pending": "queued",
    "queued": "queued", "running": "running", "completed": "completed",
    "failed": "failed", "cancelled": "cancelled",
    "submit_unknown": "unknown", "unknown": "unknown",
}

#: `media_runs.start()` outcome -> `SubmitResult.state`.
_SUBMIT_STATE_MAP = {
    "queued": "accepted", "submit_unknown": "accepted_uncertain",
    "failed": "rejected_before_queue",
}

#: `media_runs.cancel()` outcome -> `CancelResult.outcome`.
_CANCEL_TOO_LATE_REASONS = ("already_completed", "already_failed")


class ComfyUIAdapter(BaseAdapter):
    """One ComfyUI deployment, over `media_runs`/`media_backends.comfyui`."""

    name = NAME

    def __init__(self, base_url: str = "") -> None:
        self._base_url = base_url

    def _backend(self):
        from src.media_backends import ComfyUIBackend
        return ComfyUIBackend(self._base_url)

    # ── describe (query) ───────────────────────────────────────────────

    def describe(self) -> AdapterManifest:
        from src import media_workflows as workflows

        probe = self._backend().probe()
        tasks = tuple(sorted({w.id for w in workflows.catalogue()["workflows"]}))
        return AdapterManifest(
            name=NAME, engine="comfyui", version="",
            tasks=tasks or ("<no approved templates installed>",),
            supports_reconcile=True, supports_cancel=True,
            available=bool(probe.get("ok")), reason=str(probe.get("detail") or ""),
            limits={"max_download_bytes": 2 * 1024**3},
        )

    # ── plan (pure — media_runs.plan(check_engine=False) touches no
    # network; check_engine=True is opt-in and documented as such below) ──

    def plan(self, op: str, params: Mapping[str, Any],
             inputs: Sequence[str]) -> AdapterPlan:
        from src import media_runs

        result = media_runs.plan(op, dict(params or {}), check_engine=True)
        ok = bool(result.get("ok"))
        missing: list = []
        if not ok:
            missing_block = result.get("missing") or {}
            missing.extend(missing_block.get("nodes") or [])
            missing.extend(missing_block.get("models") or [])
            if not missing and result.get("reason"):
                missing.append(str(result["reason"]))
        return AdapterPlan(
            ok=ok, adapter=NAME, task=op,
            estimated_cost={"models": result.get("models") or []},
            missing=tuple(missing), detail=str(result.get("detail") or result.get("reason") or ""),
            engine_plan={"workflow_id": op, "inputs": dict(params or {}),
                         "engine_url": (result.get("engine") or {}).get("url", self._base_url)},
        )

    # ── submit (effect) ───────────────────────────────────────────────

    def submit(self, plan: AdapterPlan, staging: Staging) -> SubmitResult:
        from src import media_runs

        if not plan.ok:
            return SubmitResult(job_id="", state="rejected_before_queue",
                                 reason="invalid_plan", detail=plan.detail)
        engine_plan = plan.engine_plan or {}
        # Occurrence inputs are resolved through `staging.input_paths` by
        # whoever built the template values (production_runs.py substitutes
        # the staged path in place of a bare occurrence id before calling
        # here) -- this method never reaches into artifact_identity itself.
        started = media_runs.start(
            engine_plan.get("workflow_id") or plan.task,
            engine_plan.get("inputs") or {},
            engine_url=engine_plan.get("engine_url") or self._base_url,
            owner=staging.owner, project_id=staging.project_id,
        )
        run_id = str(started.get("run_id") or "")
        status = str(started.get("status") or "")
        state = _SUBMIT_STATE_MAP.get(status, "rejected_before_queue" if not run_id else "accepted_uncertain")
        return SubmitResult(job_id=run_id, state=state,
                             reason=str(started.get("reason") or ""),
                             detail=str(started.get("detail") or ""))

    # ── status (query) ────────────────────────────────────────────────

    def status(self, job_id: str) -> StatusResult:
        from src import media_runs

        record = media_runs.poll(job_id, collect=False)
        if not record.get("ok"):
            return StatusResult(job_id=job_id, state="unknown",
                                 detail=str(record.get("reason") or "not_found"))
        state = _STATUS_MAP.get(str(record.get("status") or ""), "unknown")
        ahead = record.get("ahead")
        progress = None
        return StatusResult(job_id=job_id, state=state,
                             detail=str(record.get("reason") or ""),
                             progress=progress if ahead is None else None)

    # ── cancel (effect) ───────────────────────────────────────────────

    def cancel(self, job_id: str) -> CancelResult:
        from src import media_runs

        record = media_runs.get(job_id)
        if record is None:
            return CancelResult(job_id=job_id, outcome="unknown", detail="no such run")
        result = media_runs.cancel(job_id)
        if result.get("ok") and result.get("status") == "cancelled":
            return CancelResult(job_id=job_id, outcome="confirmed",
                                 detail=str(result.get("detail") or result.get("reason") or ""))
        reason = str(result.get("reason") or "")
        if reason in _CANCEL_TOO_LATE_REASONS or reason.startswith("already_"):
            return CancelResult(job_id=job_id, outcome="too_late", detail=reason)
        if reason == "submission_not_settled":
            return CancelResult(job_id=job_id, outcome="requested", detail=reason)
        return CancelResult(job_id=job_id, outcome="unknown",
                             detail=reason or "cancel outcome could not be classified")

    # ── collect (query + bounded local effect) ────────────────────────

    def collect(self, job_id: str, tmp: str) -> CollectResult:
        """Reads back what `media_runs.poll(collect=True)` ALREADY collected
        and validated into the artifact store (hash, inspection —
        `artifact_store.collect`/`validate_artifact_bytes`, run before this
        adapter existed) rather than downloading a second copy into `tmp`.
        MediaRun stays the one place a ComfyUI output is written to disk —
        this method is a read-through, not a second collector.
        """
        from src import media_runs

        record = media_runs.poll(job_id, collect=True)
        if not record.get("ok"):
            return CollectResult(ok=False, detail=str(record.get("reason") or "not_found"))
        if record.get("status") != "completed":
            return CollectResult(ok=False, detail=f"job is {record.get('status')}, not completed")
        artifacts = record.get("artifacts") or []
        outputs = tuple(
            CollectedOutput(path=str(a.get("id") or ""), sha256=str(a.get("sha256") or ""),
                             byte_size=int(a.get("byte_size") or 0),
                             media_type=str(a.get("media_type") or ""),
                             valid=True, detail="already registered by media_runs")
            for a in artifacts
        )
        return CollectResult(ok=True, outputs=outputs,
                              detail="outputs already registered as artifact occurrences by media_runs")

    # ── reconcile (query) ─────────────────────────────────────────────

    def reconcile(self, job_id: str) -> StatusResult:
        from src import media_runs

        media_runs.reconcile_run(job_id, grace_seconds=0)
        return self.status(job_id)


ADAPTER_FACTORY = ComfyUIAdapter
