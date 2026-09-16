"""tests/creator_harness/test_adapter_contract.py — WP36: the AdapterPort
contract, proved against EVERY adapter `src.creator.adapters.registry()`
discovers via pkgutil, not a hardcoded pair.

`src/creator/adapter_port.py`'s module docstring states the contract; this
file is the runtime proof of it, generic across adapters rather than one
adapter-specific assertion each (WP10's `tests/test_creator_wp10_adapters.py`
already covers ComfyUI-mapping-specifics/real-ffmpeg-composition in depth —
this file complements it with the SAME checks applied uniformly, so a third
adapter dropped into `src/creator/adapters/` inherits contract coverage
without anyone remembering to write it by hand).

Evidence level (WP36 closing criterion: "reporte separa niveles de
evidencia" — see `docs/spec/creator/WP36.md`): every test below is
`fixture` or `fake_engine` level for ComfyUI (a fake `src.media_runs`, no
network), and `real_engine` for ffmpeg WHEN the real binary is on PATH
(`fixture` otherwise — `describe()`/`plan()` still get proven honest about
being unavailable, never faked as available).
"""
from __future__ import annotations

from typing import Callable, Tuple

import pytest

from src.creator.adapter_port import (
    CANCEL_OUTCOMES, STATUS_STATES, SUBMIT_STATES,
    AdapterManifest, AdapterPlan, AdapterPort, Staging,
)
from src.creator import adapters as adapters_pkg
from tests.creator_harness import fixtures as fx
from tests.creator_harness.matrix import registered_adapters


def _staging(owner: str = "alice", project_id: str = "p1", workdir: str = "/tmp") -> Staging:
    return Staging(owner=owner, project_id=project_id, workdir=workdir, input_paths={})


# ── per-adapter setup: how to build one WITHOUT hitting a real network,
# and how to count "jobs this adapter thinks it queued" for the purity
# checks below. Every registered adapter needs an entry here — a name the
# registry finds with none is a genuine gap this harness reports, not a
# silent skip (see `test_every_registered_adapter_has_a_contract_setup`). ─

class _FakeMediaRunsForContract:
    """Same minimal shape as `tests/test_creator_wp10_adapters.py`'s
    `_FakeMediaRuns` — this file does not import that one (it is
    module-private there) and keeps its own, smaller copy: only what the
    generic contract checks below need."""

    def __init__(self) -> None:
        self.rows = {}
        self.plan_calls = 0
        self.start_calls = 0

    def plan(self, workflow_id, inputs, check_engine=True):
        self.plan_calls += 1
        if workflow_id == "__contract_missing__":
            return {"ok": False, "reason": "missing_requirements",
                     "detail": "no checkpoint", "missing": {"nodes": [], "models": ["m.safetensors"]}}
        if not workflow_id or workflow_id.startswith("__"):
            # Stands in for "this workflow id does not exist" — a real
            # engine's own catalogue lookup fails the same way real
            # media_workflows.render()/plan() does for an unknown id.
            return {"ok": False, "reason": "unknown_workflow",
                     "detail": f"no such workflow {workflow_id!r}", "missing": {"nodes": [], "models": []}}
        return {"ok": True, "workflow": workflow_id, "models": [],
                 "engine": {"url": "http://fake-contract:8188"}}

    def start(self, workflow_id, inputs, *, engine_url="", owner="", project_id=""):
        self.start_calls += 1
        run_id = f"mrun_{workflow_id}_{self.start_calls}"
        self.rows[run_id] = {"status": "queued", "id": run_id}
        return {"ok": True, "run_id": run_id, "status": "queued", "reason": "", "detail": ""}

    def get(self, run_id):
        return self.rows.get(run_id)

    def poll(self, run_id, collect=True):
        row = self.rows.get(run_id)
        if row is None:
            return {"ok": False, "reason": "not_found"}
        return {"ok": True, "status": row["status"], "reason": "", "artifacts": row.get("artifacts", [])}

    def cancel(self, run_id):
        row = self.rows.get(run_id)
        if row is None:
            return {"ok": False, "reason": "not_found"}
        row["status"] = "cancelled"
        return {"ok": True, "status": "cancelled"}

    def reconcile_run(self, run_id, grace_seconds=0):
        return {"ok": True, "run_id": run_id, "changed": False}


def _setup_comfyui(monkeypatch) -> Tuple[AdapterPort, str, Callable[[], int]]:
    fake = _FakeMediaRunsForContract()
    monkeypatch.setattr("src.media_runs.plan", fake.plan)
    monkeypatch.setattr("src.media_runs.start", fake.start)
    monkeypatch.setattr("src.media_runs.get", fake.get)
    monkeypatch.setattr("src.media_runs.poll", fake.poll)
    monkeypatch.setattr("src.media_runs.cancel", fake.cancel)
    monkeypatch.setattr("src.media_runs.reconcile_run", fake.reconcile_run)
    from src.creator.adapters.comfyui import ComfyUIAdapter
    return ComfyUIAdapter(), "fake_engine", (lambda: fake.start_calls)


def _setup_ffmpeg(monkeypatch) -> Tuple[AdapterPort, str, Callable[[], int]]:
    from src.creator.adapters import ffmpeg as ffmpeg_mod
    evidence = "real_engine" if fx.ffmpeg_available() and fx.ffprobe_available() else "fixture"
    return ffmpeg_mod.FfmpegAdapter(), evidence, (lambda: len(ffmpeg_mod._JOBS))


_CONTRACT_SETUPS = {"comfyui": _setup_comfyui, "ffmpeg": _setup_ffmpeg}


@pytest.fixture(params=sorted(registered_adapters().keys()))
def adapter_name(request) -> str:
    return request.param


@pytest.fixture()
def prepared(adapter_name, monkeypatch, request):
    setup = _CONTRACT_SETUPS.get(adapter_name)
    if setup is None:
        pytest.skip(
            f"WP36 harness has no generic contract setup for adapter "
            f"{adapter_name!r} yet — add one to _CONTRACT_SETUPS instead of "
            f"letting it go unchecked.")
    adapter, evidence, job_count = setup(monkeypatch)
    request.node.user_properties.append(("evidence_level", evidence))
    return adapter, evidence, job_count


# ═══════════════════════════════════════════════════════════════════════
# describe() — QUERY: never queues a job to answer, always an AdapterManifest
# ═══════════════════════════════════════════════════════════════════════

def test_describe_returns_a_well_formed_manifest(prepared):
    adapter, _evidence, _job_count = prepared
    manifest = adapter.describe()
    assert isinstance(manifest, AdapterManifest)
    assert manifest.name  # never blank
    assert isinstance(manifest.tasks, tuple)
    assert manifest.available is True or manifest.reason, (
        "unavailable without a reason is exactly the case "
        "CONTRATO.md/WP10 forbid: available=False MUST carry `reason`")


def test_describe_never_queues_a_job(prepared):
    adapter, _evidence, job_count = prepared
    before = job_count()
    adapter.describe()
    adapter.describe()
    assert job_count() == before, "describe() created a job — it must be a pure health read, never a submit"


# ═══════════════════════════════════════════════════════════════════════
# plan() — PURE: no filesystem write, no job created, for a good OR a bad op
# ═══════════════════════════════════════════════════════════════════════

def test_plan_creates_no_job_for_a_valid_looking_task(prepared, adapter_name):
    adapter, _evidence, job_count = prepared
    before = job_count()
    op = "sdxl_txt2img" if adapter_name == "comfyui" else "trim"
    params = {} if adapter_name == "comfyui" else {"start_seconds": 0, "duration_seconds": 1}
    inputs = [] if adapter_name == "comfyui" else ["occ_1"]
    adapter.plan(op, params, inputs)
    assert job_count() == before, "plan() must never create a job — that is submit()'s job alone"


def test_plan_rejects_garbage_without_raising(prepared):
    adapter, _evidence, job_count = prepared
    before = job_count()
    result = adapter.plan("__definitely_not_a_real_task__", {"nonsense": True}, ["x"])
    assert isinstance(result, AdapterPlan)
    assert result.ok is False
    assert job_count() == before


# ═══════════════════════════════════════════════════════════════════════
# submit() — EFFECT: a rejected plan classifies as rejected_before_queue,
# never silently promoted and never left without a state at all.
# ═══════════════════════════════════════════════════════════════════════

def test_submit_of_an_invalid_plan_is_rejected_before_queue(prepared, adapter_name):
    adapter, _evidence, job_count = prepared
    before = job_count()
    bad_plan = AdapterPlan(ok=False, adapter=adapter_name, task="nope", detail="deliberately invalid")
    result = adapter.submit(bad_plan, _staging())
    assert result.state == "rejected_before_queue"
    assert result.state in SUBMIT_STATES
    assert job_count() == before, "a rejected plan must never reach the engine's queue"


# ═══════════════════════════════════════════════════════════════════════
# collect() — never registers an unknown/nonexistent job as valid output
# ═══════════════════════════════════════════════════════════════════════

def test_collect_of_an_unknown_job_id_is_not_ok(prepared, tmp_path):
    adapter, _evidence, _job_count = prepared
    result = adapter.collect("__job_that_was_never_submitted__", str(tmp_path / "collect"))
    assert result.ok is False
    assert not any(o.valid for o in result.outputs), (
        "collect() on an unknown job must never report a valid output")


# ═══════════════════════════════════════════════════════════════════════
# cancel() — always the documented enum, never a raise, never a bare string
# ═══════════════════════════════════════════════════════════════════════

def test_cancel_of_an_unknown_job_id_uses_the_documented_enum(prepared):
    adapter, _evidence, _job_count = prepared
    result = adapter.cancel("__job_that_was_never_submitted__")
    assert result.outcome in CANCEL_OUTCOMES
    assert result.outcome == "unknown"


# ═══════════════════════════════════════════════════════════════════════
# reconcile() — only meaningful per the manifest's own declaration
# ═══════════════════════════════════════════════════════════════════════

def test_reconcile_matches_its_own_manifest_declaration(prepared):
    adapter, _evidence, _job_count = prepared
    manifest = adapter.describe()
    result = adapter.reconcile("__job_that_was_never_submitted__")
    assert result.state in STATUS_STATES
    if not manifest.supports_reconcile:
        # BaseAdapter's contract: an adapter honest about not supporting
        # reconciliation still answers — by falling back to status() —
        # rather than raising or inventing a stronger guarantee.
        assert result.state == adapter.status("__job_that_was_never_submitted__").state


# ═══════════════════════════════════════════════════════════════════════
# the registry itself — every adapter pkgutil finds has SOME setup above
# ═══════════════════════════════════════════════════════════════════════

def test_every_registered_adapter_has_a_contract_setup():
    """A new adapter dropped into `src/creator/adapters/` with no matching
    entry in `_CONTRACT_SETUPS` fails THIS test loudly instead of the
    contract suite quietly skipping it — WP36 closing criterion: "un
    adapter nuevo no se habilita sólo por responder health 200" starts with
    "this harness actually looked at it"."""
    found = set(adapters_pkg.registry().keys())
    covered = set(_CONTRACT_SETUPS.keys())
    missing = found - covered
    assert not missing, (
        f"adapters registered but not covered by tests/creator_harness contract "
        f"setup: {sorted(missing)} — add a _CONTRACT_SETUPS entry, do not "
        f"silently skip a real adapter")
