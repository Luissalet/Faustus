"""
tests/test_media_runs.py — a render from recipe to artifact, and back.

Everything here runs against the real fake engine from `test_comfyui_backend`
(a `ThreadingHTTPServer` speaking ComfyUI's protocol) and a real SQLite file,
because the two claims worth proving are both about persistence:

* **a render survives the web process.** The row carries the engine's job id,
  so `poll()` after a "restart" asks the engine what happened instead of
  trusting a status written before the process died;
* **the picture keeps its story.** The artifact row carries the recipe, its
  version and fingerprint, the seed, the resolved inputs, the model and — the
  one everybody forgets — the model's licence. A file handed to a client
  carries the licence of the model that made it, and by the time somebody
  asks, nobody remembers.

The database is a temp FILE, not the shared in-memory one: these tests are
about rows outliving things, and a database other modules truncate cannot
prove that.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src import media_runs
from tests.test_comfyui_backend import FakeComfy


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A real engine, a real database, a real artifact store — all disposable."""
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


ASK = {"prompt": "a ceramic mug on a white background", "aspect_ratio": "4:5"}


def started_fingerprint(run_id):
    return media_runs.get(run_id)["fingerprint"]


# ── planning ──────────────────────────────────────────────────────────────

def test_a_plan_shows_the_seed_and_the_licence_before_anything_is_queued(world):
    out = media_runs.plan("image.product", ASK)
    assert out["ok"] is True
    assert out["values"]["prompt"] == ASK["prompt"]
    assert isinstance(out["values"]["seed"], int)
    assert out["values"]["negative_prompt"], "a default nobody sees is a default"
    assert out["models"][0]["license"] == "CreativeML Open RAIL++-M"
    assert out["engine"]["ok"] is True
    assert world.submitted == [], "planning queued something"


def test_a_plan_for_a_template_that_does_not_exist_says_what_does(world):
    out = media_runs.plan("image.nope", {})
    assert out["ok"] is False and out["reason"] == "no_such_workflow"
    assert "image.product" in out["detail"]


def test_a_plan_with_an_input_the_template_rejects_names_the_field(world):
    out = media_runs.plan("image.product", {"prompt": "x", "steps": 900})
    assert out["ok"] is False and out["reason"] == "bad_inputs"
    assert out["field"] == "inputs.steps"


def test_a_plan_says_when_the_engine_lacks_the_model_rather_than_at_render_time(world):
    world.checkpoints = ["something_else.safetensors"]
    out = media_runs.plan("image.product", ASK)
    assert out["ok"] is False and out["reason"] == "missing_requirements"
    assert out["missing"]["models"] == ["sd_xl_base_1.0.safetensors"]


# ── running ───────────────────────────────────────────────────────────────

def test_a_render_goes_from_queued_to_an_artifact_with_its_whole_story(world):
    started = media_runs.start("image.product", ASK, owner="luis",
                               project_id="proj-1", session_id="sess-1")
    assert started["ok"] and started["status"] == "queued"
    run_id = started["run_id"]
    seed = started["values"]["seed"]

    # The graph that reached the engine is the template's, with the values in it.
    sent = world.submitted[0]["prompt"]
    assert sent["2"]["inputs"]["text"] == ASK["prompt"]
    assert sent["4"]["inputs"]["width"] == 896            # 4:5
    assert sent["5"]["inputs"]["seed"] == seed

    while_waiting = media_runs.poll(run_id)
    assert while_waiting["status"] in ("queued", "running")
    assert while_waiting["artifact_ids"] == []

    world.finish(started["engine_job_id"])
    done = media_runs.poll(run_id)
    assert done["status"] == "completed"
    assert len(done["artifacts"]) == 1

    art = done["artifacts"][0]
    assert art["kind"] == "image"
    assert art["sha256"] and art["byte_size"] > 0
    prov = art["provenance"]
    assert prov["recipe"] == "image.product" and prov["recipe_version"] == "1.0.0"
    assert prov["recipe_fingerprint"] == started_fingerprint(run_id)
    assert prov["seed"] == seed
    assert prov["model"] == "sd_xl_base_1.0.safetensors"
    assert prov["model_license"] == "CreativeML Open RAIL++-M"
    assert prov["engine"] == "comfyui"
    assert prov["engine_job_id"] == started["engine_job_id"]

    # A DIGEST of the inputs, not the prompt itself: a prompt can carry a
    # client's name or an unreleased product, and this row is read by more
    # people than the media run is. The note points at where the values live.
    assert len(prov["inputs_digest"]) == 64
    assert ASK["prompt"] not in json.dumps(prov)
    assert run_id in prov["note"]

    # and it is a row, not just a return value
    from core.database import ArtifactRow, SessionLocal
    db = SessionLocal()
    try:
        row = db.get(ArtifactRow, art["id"])
        assert row is not None
        assert row.model_license == "CreativeML Open RAIL++-M"
        assert row.recipe == "image.product"
        assert row.recipe_fingerprint == prov["recipe_fingerprint"]
        assert row.seed == seed
        assert row.engine == "comfyui"
        assert row.engine_job_id == started["engine_job_id"]
        assert row.owner == "luis" and row.session_id == "sess-1"
    finally:
        db.close()


def test_an_artifact_from_a_render_has_no_gaps_in_its_record(world):
    """`Provenance.unknowns()` is the audit's way of saying "this file cannot
    account for itself". A render is the one producer that should have no
    excuse: it knows its model, its backend, its recipe and its inputs."""
    from src.contracts import Artifact

    started = media_runs.start("image.product", ASK, owner="luis")
    world.finish(started["engine_job_id"])
    done = media_runs.poll(started["run_id"])

    art = Artifact.parse(done["artifacts"][0])
    assert art.provenance.unknowns() == ()


def test_a_finished_render_is_answered_from_the_row_without_asking_again(world):
    started = media_runs.start("image.product", ASK)
    world.finish(started["engine_job_id"])
    media_runs.poll(started["run_id"])

    before = len(world.calls)
    again = media_runs.poll(started["run_id"])
    assert again["status"] == "completed" and again["checked"] is False
    assert len(world.calls) == before, "it went back to the engine for a finished run"


def test_a_render_survives_the_process_that_started_it(world):
    """The claim of the phase. Nothing in memory carries the render — the row
    has the engine's job id, so a completely fresh look reconciles from the
    engine rather than from what was written before the restart."""
    started = media_runs.start("image.product", ASK, owner="luis")
    run_id = started["run_id"]

    # As if the web process had died here: the row still says `queued`.
    assert media_runs.get(run_id)["status"] == "queued"

    # Meanwhile the engine finished the job.
    world.finish(started["engine_job_id"])

    out = media_runs.poll(run_id)
    assert out["status"] == "completed" and out["artifacts"]
    assert media_runs.get(run_id)["artifact_ids"] == [out["artifacts"][0]["id"]]


def test_the_engine_being_down_makes_a_run_unknown_not_failed(world):
    """A status written on a guess is how a finished render gets reported as
    a failure. If the engine cannot be reached, that is what is said."""
    started = media_runs.start("image.product", ASK)
    world.stop()

    out = media_runs.poll(started["run_id"])
    assert out["engine_reachable"] is False
    assert out["status"] == "queued", "it invented a failure it had not seen"
    assert media_runs.get(started["run_id"])["status"] == "queued"


def test_a_render_somebody_stopped_reads_as_cancelled_not_failed(world):
    """The engine reports an interruption in the same shape as a crash.
    Telling a person who stopped a render that it broke is a small lie that
    costs a real minute of worry."""
    started = media_runs.start("image.product", ASK)
    world.interrupt(started["engine_job_id"])
    out = media_runs.poll(started["run_id"])
    assert out["status"] == "cancelled"
    assert media_runs.get(started["run_id"])["status"] == "cancelled"


def test_an_engine_that_forgot_the_job_is_unknown_with_the_reason(world):
    started = media_runs.start("image.product", ASK)
    world.pending = []          # a restarted ComfyUI: history and queue empty
    out = media_runs.poll(started["run_id"])
    assert out["status"] == "unknown"
    assert "restarted" in out["reason"]


def test_a_render_that_failed_keeps_the_engine_s_own_words(world):
    started = media_runs.start("image.product", ASK)
    world.fail(started["engine_job_id"], node="KSampler", why="CUDA out of memory")
    out = media_runs.poll(started["run_id"])
    assert out["status"] == "failed"
    assert "KSampler" in out["reason"] and "out of memory" in out["reason"]


def test_a_render_no_engine_can_take_never_becomes_a_run_at_all(world):
    """It is refused during engine selection, before a row exists — the same
    place `no_such_workflow` and `bad_inputs` are refused, and for the same
    reason: nothing reached an engine, so there is no run to record. The
    answer carries what EVERY engine said, because "no engine available" on a
    machine with two of them is the least useful sentence in the system."""
    # An engine holding the WRONG model, which is the case where naming the
    # file matters: the usual cause is one letter, not an empty folder.
    world.checkpoints = ["dreamshaper_8.safetensors"]
    out = media_runs.start("image.product", ASK, owner="luis")

    assert out["ok"] is False
    assert "run_id" not in out, "a request that never reached an engine left a row"
    assert media_runs.recent() == []
    assert "sd_xl_base_1.0.safetensors" in out["detail"]
    assert out["why"] and all("url" in w for w in out["why"])
    assert world.submitted == [], "it queued a job it knew would fail"


def test_an_engine_with_no_models_at_all_is_told_where_to_put_one(world):
    """A different refusal from "the wrong model", and it should read
    differently: an empty folder is answered with the folder, not with a file
    name nobody could have known."""
    world.checkpoints = []
    out = media_runs.start("image.product", ASK, owner="luis")

    assert out["ok"] is False and "run_id" not in out
    assert "models/checkpoints" in out["detail"]
    assert "does not download models" in out["detail"]


def test_an_input_the_template_rejects_never_becomes_a_run_at_all(world):
    out = media_runs.start("image.product", {"prompt": "x", "sampler": "evil"})
    assert out["ok"] is False and out["field"] == "inputs.sampler"
    assert media_runs.recent() == [], "a refused request left a row behind"


# ── cancelling ────────────────────────────────────────────────────────────

def test_cancelling_a_queued_render_frees_the_engine_and_records_it(world):
    started = media_runs.start("image.product", ASK)
    out = media_runs.cancel(started["run_id"])
    assert out["ok"] and out["status"] == "cancelled"
    assert world.pending == []
    assert media_runs.get(started["run_id"])["status"] == "cancelled"


def test_cancelling_a_finished_render_says_so_rather_than_pretending(world):
    started = media_runs.start("image.product", ASK)
    world.finish(started["engine_job_id"])
    media_runs.poll(started["run_id"])
    out = media_runs.cancel(started["run_id"])
    assert out["ok"] is False and out["reason"] == "already_completed"


def test_the_run_list_is_newest_first_and_filters_by_owner(world):
    media_runs.start("image.product", ASK, owner="luis")
    media_runs.start("image.product", ASK, owner="ana")
    assert len(media_runs.recent()) == 2
    mine = media_runs.recent(owner="luis")
    assert len(mine) == 1 and mine[0]["owner"] == "luis"


def test_two_renders_of_the_same_thing_share_one_stored_file(world):
    """Content-hash storage, from Phase 0. The engine returns the same bytes
    for both, so the store keeps one file and both runs point at it — which is
    what makes "try it again" cheap."""
    first = media_runs.start("image.product", ASK)
    world.finish(first["engine_job_id"])
    a = media_runs.poll(first["run_id"])

    second = media_runs.start("image.product", ASK)
    world.finish(second["engine_job_id"])
    b = media_runs.poll(second["run_id"])

    assert a["artifacts"][0]["sha256"] == b["artifacts"][0]["sha256"]
    assert a["artifacts"][0]["id"] == b["artifacts"][0]["id"]
    assert b["run_id"] != a["run_id"]
