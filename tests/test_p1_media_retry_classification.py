"""MEDIA-04 — retry by class, without duplicating a render that already
reached the engine.

The core acceptance criterion ("a timeout after accepting the job checks its
status before creating another that burns VRAM") is already true of
`media_runs.poll()`/`reconcile_run()` — see `tests/test_media_runs_outbox.py`
for that behaviour proven directly. What this file adds is
`classify_failure()` (a pure label for one run) and `retry()` (which uses
that label, but ALWAYS re-checks the run through `poll()` first — so a
caller invoking `retry()` after a timeout gets the same "ask before
creating another" protection `poll()` already provides, not a second,
looser code path around it).

Revert-and-fail check (COMUN.md rule 5): with the `if verdict != "transient": return ...`
early-out removed from `retry()` (temporary edit, tested, reverted — same
procedure as `test_p1_media_recipe_review.py`'s docstring), every
`test_retry_refuses_*` test below fails: `retry()` resubmits a permanently
failed, a completed, and a still-queued run alike, and the still-queued one
even reaches the fake engine a second time.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src import media_runs
from tests.test_comfyui_backend import FakeComfy

ASK = {"prompt": "a ceramic mug", "aspect_ratio": "1:1"}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "media.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    from src import artifact_store
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(artifact_store, "ARTIFACT_RUNS_DIR", str(tmp_path / "runs"))
    fake = FakeComfy()
    base_url = fake.start()
    monkeypatch.setenv("COMFYUI_URL", base_url)
    try:
        yield fake
    finally:
        fake.stop()
        engine.dispose()


# ── classify_failure(): pure, no engine or database involved ──────────────

@pytest.mark.parametrize("status,reason,expected", [
    ("completed", "", "not_retryable"),
    ("cancelled", "cancelled while queued", "not_retryable"),
    ("queued", "", "in_progress"),
    ("running", "", "in_progress"),
    ("submit_pending", "", "in_progress"),
    ("submit_unknown", "socket closed", "in_progress"),
    ("unknown", "", "ambiguous"),
    ("failed", "missing_requirements: no checkpoint", "permanent"),
    ("failed", "rejected_by_engine: bad node", "permanent"),
    ("failed", "Engine unavailable; retrying: connection refused", "transient"),
    ("failed", "the engine is reachable and holds no job carrying this run's "
              "client id, so the prompt never reached the queue", "transient"),
    ("failed", "this run carries no client id, so a job of its on the engine "
              "cannot be told from anyone else's", "transient"),
    ("failed", "the sampler produced NaN output", "unclassified"),
    ("failed", "", "unclassified"),
])
def test_classify_failure_labels(status, reason, expected):
    assert media_runs.classify_failure({"status": status, "reason": reason}) == expected


# ── retry(): always re-checks through poll() first ─────────────────────────

def test_retry_resubmits_a_run_that_never_reached_the_queue(world):
    started = media_runs.start("image.product", ASK)
    assert started["ok"] is True
    run_id = started["run_id"]
    # Simulate "this process lost the outbox entry, and the engine never
    # actually got it" — reconcile_run() writes exactly this reason.
    media_runs._update(run_id, status="failed",
                       reason="the engine is reachable and holds no job carrying "
                              "this run's client id, so the prompt never reached "
                              "the queue")
    out = media_runs.retry(run_id)
    assert out["ok"] is True
    assert out["retry_of"] == run_id
    assert out["run_id"] != run_id
    # The retry carries the SAME seed — a retry reproduces the picture,
    # it does not roll a new one.
    assert out["values"]["seed"] == started["values"]["seed"]


def test_retry_refuses_a_permanently_failed_run(world):
    started = media_runs.start("image.product", ASK)
    run_id = started["run_id"]
    media_runs._update(run_id, status="failed",
                       reason="missing_requirements: no checkpoint on this engine")
    out = media_runs.retry(run_id)
    assert out["ok"] is False and out["reason"] == "not_retryable_permanent"
    # No second run was created.
    assert len(media_runs.recent(limit=50)) == 1


def test_retry_refuses_a_completed_run(world):
    started = media_runs.start("image.product", ASK)
    run_id = started["run_id"]
    media_runs._update(run_id, status="completed", ended_at="2026-01-01T00:00:00Z")
    out = media_runs.retry(run_id)
    assert out["ok"] is False and out["reason"] == "not_retryable_not_retryable"
    assert len(media_runs.recent(limit=50)) == 1


def test_retry_refuses_a_run_that_is_still_genuinely_queued(world):
    """`retry()` never resubmits anything whose status is not `failed` —
    `queued`/`running`/an unsettled submit are all `"in_progress"`, and a
    run stays exactly that until code that actually asked the engine
    (`_poll()`/`reconcile_run()`) writes something else. This is the same
    discipline the rest of the module already follows (`_poll()` treats
    `failed`/`completed`/`cancelled` as settled and everything else as "ask
    the engine"); `retry()` inherits it via `poll()` rather than reading the
    row directly, so it can never be the ONE caller that skips the check."""
    started = media_runs.start("image.product", ASK)
    run_id = started["run_id"]
    assert media_runs.get(run_id)["status"] == "queued"
    before = len(world.submitted)
    out = media_runs.retry(run_id)
    assert out["ok"] is False and out["reason"] == "not_retryable_in_progress"
    assert len(world.submitted) == before, "retry must not have queued a second job"


def test_retry_of_an_unknown_run_is_not_found(world):
    out = media_runs.retry("mrun_doesnotexist")
    assert out == {"ok": False, "reason": "not_found", "run_id": "mrun_doesnotexist"}


def test_retry_is_owner_scoped(world):
    started = media_runs.start("image.product", ASK, owner="alice")
    run_id = started["run_id"]
    media_runs._update(run_id, status="failed", reason="Engine unavailable; retrying: refused")
    out = media_runs.retry(run_id, owner="bob")
    assert out == {"ok": False, "reason": "not_found", "run_id": run_id}
