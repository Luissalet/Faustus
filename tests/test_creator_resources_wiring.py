"""tests/test_creator_resources_wiring.py — WP30: the real cabling in
`src/media_runs.py`.

Same fake ComfyUI + real sqlite database as `tests/test_media_runs.py`; what
is new here is proving three things about the admission gate ITSELF being
called from `start()`/`poll()`/`cancel()`:

* with `creator_enabled` off, `resource_admission.acquire` is never called
  and every observable outcome of `media_runs` is unchanged (a spy, not an
  assumption);
* with it on and the device already busy (serial mode), a second `start()`
  leaves its row `queued` with a reason instead of ever calling
  `engine.submit()`;
* a run that is admitted and then fails frees the device for the next one.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src import media_runs, resource_admission, settings as settings_mod
from src.creator import resources as creator_resources
from tests.test_comfyui_backend import FakeComfy

ASK = {"prompt": "a ceramic mug on a white background", "aspect_ratio": "4:5"}


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

    monkeypatch.setenv("FAUSTUS_DATA_DIR", str(tmp_path))
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))

    resource_admission.reset_all()
    creator_resources.reset_for_tests()

    fake = FakeComfy()
    base_url = fake.start()
    monkeypatch.setenv("COMFYUI_URL", base_url)
    try:
        yield fake
    finally:
        fake.stop()
        engine.dispose()
        resource_admission.reset_all()
        creator_resources.reset_for_tests()


def _settings(monkeypatch, **overrides):
    base = dict(settings_mod.DEFAULT_SETTINGS)
    base.update(overrides)
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: base.get(key, default))


# ── flag off: zero behaviour change ─────────────────────────────────────

def test_flag_off_admission_is_never_called_and_behaviour_is_unchanged(world, monkeypatch):
    _settings(monkeypatch, creator_enabled=False)
    calls = []
    real_acquire = resource_admission.acquire

    async def spy_acquire(*a, **kw):
        calls.append((a, kw))
        return await real_acquire(*a, **kw)

    monkeypatch.setattr(resource_admission, "acquire", spy_acquire)

    started = media_runs.start("image.product", ASK, owner="alice")
    assert started["ok"] is True and started["status"] == "queued"
    assert started["engine_job_id"], "flag off: the job must reach the engine exactly as before"
    assert calls == [], "resource_admission.acquire must never be called with creator_enabled off"

    world.finish(started["engine_job_id"])
    done = media_runs.poll(started["run_id"])
    assert done["status"] == "completed"
    assert calls == [], "acquire must stay uncalled through the whole lifecycle"


# ── flag on, serial mode: the real gate ─────────────────────────────────

def test_flag_on_serial_mode_queues_the_second_job_without_submitting_it(world, monkeypatch):
    _settings(monkeypatch, creator_enabled=True, creator_resource_mode="serial")

    first = media_runs.start("image.product", ASK, owner="alice")
    assert first["ok"] is True
    assert first.get("admitted", True) is not False
    assert first["engine_job_id"]
    assert len(world.submitted) == 1

    second = media_runs.start("image.product", ASK, owner="bob")
    assert second["ok"] is True
    assert second["status"] == "queued"
    assert second.get("admitted") is False
    assert "busy" in second["reason"]
    assert len(world.submitted) == 1, "the second job must never reach the engine while the first holds the device"

    row = media_runs.get(second["run_id"])
    assert row["status"] == "queued"
    assert not row["engine_job_id"]
    assert "resource_wait" in row["reason"]

    # Finishing the first frees the device; nothing here re-submits the
    # second automatically (that is out of this lot's scope — see the
    # final report's pendientes), but the gate itself is provably released.
    world.finish(first["engine_job_id"])
    media_runs.poll(first["run_id"])
    third = media_runs.start("image.product", ASK, owner="carol")
    assert third["ok"] is True and third["engine_job_id"]
    assert len(world.submitted) == 2


def test_a_run_that_fails_before_reaching_the_engine_frees_the_device(world, monkeypatch):
    _settings(monkeypatch, creator_enabled=True, creator_resource_mode="serial")
    # The job passes engine selection (the model IS there) but the engine
    # rejects the rendered graph itself once submitted -- REFUSED_BEFORE_QUEUE.
    world.node_errors = {"5": {"class_type": "KSampler",
                               "errors": [{"message": "value not in list"}]}}
    failed = media_runs.start("image.product", ASK, owner="alice")
    assert failed["ok"] is False
    assert failed["status"] == "failed"
    assert failed["reason"] == "rejected_by_engine"

    world.node_errors = {}
    ok = media_runs.start("image.product", ASK, owner="bob")
    assert ok["ok"] is True and ok["engine_job_id"], \
        "a lease taken for a run that failed before reaching the engine must be released"


def test_cancelling_an_admitted_run_frees_the_device(world, monkeypatch):
    _settings(monkeypatch, creator_enabled=True, creator_resource_mode="serial")
    first = media_runs.start("image.product", ASK, owner="alice")
    assert first["ok"] is True
    stopped = media_runs.cancel(first["run_id"])
    assert stopped["ok"] is True and stopped["status"] == "cancelled"

    second = media_runs.start("image.product", ASK, owner="bob")
    assert second["ok"] is True and second["engine_job_id"], \
        "cancelling the first run must free the device for the next one"
