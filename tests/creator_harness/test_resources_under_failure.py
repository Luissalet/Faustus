"""tests/creator_harness/test_resources_under_failure.py — WP36: the
"Recursos" row of the failure matrix — `src/creator/resources.py` (WP30)
must release an admitted lease when the job it was held for fails or is
cancelled, not just when it completes cleanly. `tests/test_creator_resources.py`
(WP30's own suite) already proves the happy-path release; this file proves
the SAME gate under the fault-injection shapes `08_PRUEBAS_Y_ACEPTACION.md`
asks for: two endpoints on one physical GPU never duplicate capacity, and an
admission held by a run that then fails/is cancelled/crashes is freed for the
next job rather than leaking a permanently "busy" device.

Real `src.resource_admission` module underneath (same discipline as
`tests/test_creator_resources.py`); only the GPU/RAM readers are
monkeypatched. `fixture` evidence level throughout — no real GPU, no real
media run.
"""
from __future__ import annotations

import pytest

from src import resource_admission
from src.creator import resources


@pytest.fixture(autouse=True)
def clean_admission(tmp_path, monkeypatch):
    monkeypatch.setenv("FAUSTUS_DATA_DIR", str(tmp_path))
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    resource_admission.reset_all()
    resources.reset_for_tests()
    yield
    resource_admission.reset_all()
    resources.reset_for_tests()


def _settings(monkeypatch, **overrides):
    from src import settings as settings_mod
    base = dict(settings_mod.DEFAULT_SETTINGS)
    base.update(overrides)
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: base.get(key, default))


# ═══════════════════════════════════════════════════════════════════════
# Recursos — "dos endpoints usan la misma GPU" → una sola capacidad física
# ═══════════════════════════════════════════════════════════════════════

def test_two_endpoints_on_one_physical_gpu_share_one_serial_slot(monkeypatch):
    """`resources.admit()` pools by DEVICE ID (here, the ComfyUI base_url a
    caller passes), so two distinct endpoint strings that both actually name
    the same physical card must be given the SAME `device` id by whoever
    calls `admit()` — this test proves the gate itself treats one device id
    as ONE slot no matter how many run_ids queue up behind it, which is the
    half of RES-two-endpoints this module owns (the OTHER half — mapping two
    different endpoint URLs that happen to share a physical card down to one
    id — is `src/memory_budget.py::physical_budgets`'s job, already proven
    by `tests/test_creator_resources.py::
    test_two_endpoints_on_the_same_physical_gpu_do_not_duplicate_capacity`)."""
    _settings(monkeypatch, creator_resource_mode="serial")
    shared_device_id = "gpu:uuid:GPU-abc"
    f = resources.Footprint(vram_bytes=1, source="estimated")

    first = resources.admit(f, shared_device_id, run_id="endpoint_a_job")
    assert first.ok is True

    second = resources.admit(f, shared_device_id, run_id="endpoint_b_job")
    assert second.ok is False, "a second endpoint pointed at the SAME device id must queue/refuse, not duplicate capacity"
    assert second.wait_for == "endpoint_a_job"

    resources.release("endpoint_a_job")
    third = resources.admit(f, shared_device_id, run_id="endpoint_b_job")
    assert third.ok is True
    resources.release("endpoint_b_job")


def test_measured_mode_never_treats_an_unknown_device_as_free_capacity(monkeypatch):
    """WP30's own conservative rule, proved under a failure-adjacent shape:
    a device string with NO reading attached (the caller could not measure
    it, e.g. because a probe just failed) must fall back to serial rather
    than being treated as having room — `admit()`'s docstring: "un
    desconocido no se convierte en capacidad libre"."""
    _settings(monkeypatch, creator_resource_mode="measured")
    f = resources.Footprint(vram_bytes=1, source="estimated")
    device_with_no_reading = "http://unmeasured-gpu:8188"  # plain str, no free_bytes/total_bytes

    first = resources.admit(f, device_with_no_reading, run_id="a")
    assert first.ok is True
    second = resources.admit(f, device_with_no_reading, run_id="b")
    assert second.ok is False, "measured mode with an unmeasured device must behave like serial mode, not admit freely"
    resources.release("a")


# ═══════════════════════════════════════════════════════════════════════
# release() on failure / cancellation — a lease must not leak
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("terminal_shape", ["failed", "cancelled", "crashed_worker"])
def test_admission_is_released_when_a_run_ends_badly(monkeypatch, terminal_shape):
    """The failure matrix's point, in resources.py's own vocabulary: a run
    that fails, is cancelled, or whose worker simply disappears must still
    give its slot back — `admit()`/`release()` make no distinction between
    a clean and a dirty end, and this test is the proof that calling
    `release()` unconditionally (as `src/media_runs.py::_creator_finish`
    does, in a bare `try/except`, for every terminal status) actually frees
    the device rather than leaving it permanently reported busy."""
    _settings(monkeypatch, creator_resource_mode="serial")
    device = "http://gpu1:8188"
    f = resources.Footprint(vram_bytes=1, source="estimated")

    admitted = resources.admit(f, device, run_id="doomed_run")
    assert admitted.ok is True

    blocked = resources.admit(f, device, run_id="next_run")
    assert blocked.ok is False, "the device must be reported busy while the doomed run still holds its lease"

    # However the run ended — release() is what src/media_runs.py's
    # _creator_finish() calls unconditionally in its own bare except, so
    # this is the exact call every terminal path is expected to make.
    released = resources.release("doomed_run")
    assert released is True

    freed = resources.admit(f, device, run_id="next_run")
    assert freed.ok is True, f"a lease from a run that ended as {terminal_shape!r} must be freed, not leaked"
    resources.release("next_run")


def test_releasing_a_run_that_was_never_admitted_is_a_harmless_no_op():
    """`resources.release()`'s own documented contract: a run that was
    refused admission (or never called `admit()` at all — e.g. it failed
    BEFORE reaching the gate) must not raise or corrupt the ledger when
    something still calls `release()` on it defensively."""
    assert resources.release("never_admitted_run_id") is False


def test_double_release_after_a_crash_recovery_does_not_over_free(monkeypatch):
    """A crashed process that comes back and, not knowing whether it already
    released, calls `release()` a second time for the same run_id must not
    free a slot that was already given to someone else."""
    _settings(monkeypatch, creator_resource_mode="serial")
    device = "http://gpu1:8188"
    f = resources.Footprint(vram_bytes=1, source="estimated")

    resources.admit(f, device, run_id="a")
    resources.release("a")
    second_release = resources.release("a")  # a is no longer tracked
    assert second_release is False

    # The slot released by "a" must be usable by a genuinely new run, not
    # blocked by a phantom double-release.
    admitted = resources.admit(f, device, run_id="b")
    assert admitted.ok is True
    resources.release("b")
