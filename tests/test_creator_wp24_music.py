"""WP24 — Music Studio: the `src/creator/adapters/music.py` adapter, the
`src/creator/music.py` orchestration into `song.record_take` commands, and
`routes/creator_music_routes.py`.

A deterministic FAKE engine throughout (CONTRATO.md rule 11: "no afirmar
éxito de motores por un mock; no ejecutar modelos") — neither `acestep` nor
`audiocraft` is ever imported for real, and this sandbox genuinely has
neither installed, which `test_generate_rejects_when_no_engine_installed`
relies on directly rather than mocking anything.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src.creator import adapter_port as port
from src.creator import documents as documents_mod
from src.creator import music as music_mod
from src.creator import store as store_mod
from src.creator.adapters import music as music_adapter_mod
from src.creator.adapters.music import MusicAdapter, _EngineResult, build_structured_prompt, synth_wav
from src.creator.errors import InvalidOperation

OWNER = "alice"


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture(autouse=True)
def _reset_music_module():
    music_adapter_mod.reset_for_tests()
    music_mod.reset_for_tests()
    yield
    music_adapter_mod.reset_for_tests()
    music_mod.reset_for_tests()


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "creator_wp24.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def own_artifact_store(tmp_path, monkeypatch):
    from src import artifact_store
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "artifact_store"))
    return artifact_store


@pytest.fixture()
def own_document_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "default_path", lambda: str(tmp_path / "creator_documents.sqlite3"))
    monkeypatch.setattr(store_mod, "_store", None)
    return store_mod


@pytest.fixture()
def project_env(tmp_path, monkeypatch, own_database, own_artifact_store, own_document_store):
    from services.projects import ProjectStore
    import services.projects as projects_mod
    ps = ProjectStore(str(tmp_path / "projects"))
    monkeypatch.setattr(projects_mod, "_store", ps)
    project = ps.create("Music test project", owner=OWNER, scaffold_memory=False)
    return project["id"]


def _song_content(*, sections=None, style_tags=None, bpm=None, key=None, seed=None) -> Dict[str, Any]:
    return {
        "language": "en",
        "sections": sections or [
            {"id": "s1", "kind": "verse", "lyrics": "walking down the empty street"},
            {"id": "s2", "kind": "chorus", "lyrics": "we are the light tonight"},
        ],
        "takes": [],
        "selected_take": None,
        "style_tags": style_tags or ["synthpop", "upbeat"],
        "bpm_target": bpm,
        "key_target": key,
        "seed": seed,
    }


def _make_song(owner: str, project_id: str, **kwargs) -> "documents_mod.CreatorDocument":
    return store_mod.get_store().create(owner, project_id, "song", _song_content(**kwargs))


# ═══════════════════════════════════════════════════════════════════════
# build_structured_prompt — deterministic
# ═══════════════════════════════════════════════════════════════════════

def test_structured_prompt_is_deterministic_regardless_of_tag_order():
    a = build_structured_prompt(
        style_tags=["pop", "synth"], bpm=120, key="C major",
        sections=[{"kind": "verse", "lyrics": "hello"}, {"kind": "chorus", "lyrics": "world"}],
    )
    b = build_structured_prompt(
        style_tags=["synth", "pop"], bpm=120, key="C major",
        sections=[{"kind": "verse", "lyrics": "hello"}, {"kind": "chorus", "lyrics": "world"}],
    )
    assert a == b
    assert a["structured_lyrics"] == "[verse]\nhello\n[chorus]\nworld"
    assert a["style_tags"] == ["pop", "synth"]  # sorted


def test_structured_prompt_instrumental_has_no_lyrics_block():
    p = build_structured_prompt(style_tags=["ambient"], sections=[{"kind": "intro", "lyrics": ""}])
    assert p["structured_lyrics"] == "[intro]"


# ═══════════════════════════════════════════════════════════════════════
# MusicAdapter — describe / plan
# ═══════════════════════════════════════════════════════════════════════

def test_describe_reports_unavailable_when_neither_engine_is_installed():
    adapter = MusicAdapter(engine_probe=lambda: {
        "ace_step": (False, "not installed", ""), "musicgen": (False, "not installed", ""),
    })
    manifest = adapter.describe()
    assert manifest.available is False
    assert "not installed" in manifest.reason


def _available_probe():
    return {"ace_step": (True, "", "0.1.0"), "musicgen": (False, "not installed", "")}


def test_plan_rejects_unknown_engine():
    adapter = MusicAdapter(engine_probe=_available_probe)
    plan = adapter.plan("music.generate", {"engine": "not_a_real_engine", "duration_s": 30}, [])
    assert plan.ok is False
    assert "engine" in plan.missing


def test_plan_rejects_duration_out_of_bounds():
    adapter = MusicAdapter(engine_probe=_available_probe)
    plan = adapter.plan("music.generate", {"engine": "ace_step", "duration_s": 999, "style_tags": ["pop"]}, [])
    assert plan.ok is False
    assert "duration_s" in plan.missing


def test_plan_rejects_empty_request_with_no_prompt_lyrics_or_tags():
    adapter = MusicAdapter(engine_probe=_available_probe)
    plan = adapter.plan("music.generate", {"engine": "ace_step", "duration_s": 30}, [])
    assert plan.ok is False
    assert "prompt" in plan.missing


def test_plan_accepts_style_tags_only_instrumental():
    adapter = MusicAdapter(engine_probe=_available_probe)
    plan = adapter.plan("music.generate", {"engine": "ace_step", "duration_s": 30,
                                           "style_tags": ["lofi", "instrumental"]}, [])
    assert plan.ok is True
    assert plan.engine_plan["structured_prompt"]["structured_lyrics"] == ""


def test_plan_musicgen_accepts_lyrics_as_a_caveated_hint():
    probe = lambda: {"ace_step": (False, "no", ""), "musicgen": (True, "", "1.0")}
    adapter = MusicAdapter(engine_probe=probe)
    plan = adapter.plan("music.generate", {"engine": "musicgen", "duration_s": 10,
                                           "lyrics": "la la la"}, [])
    assert plan.ok is True
    assert "no structured lyrics channel" in plan.detail


def test_plan_rejects_when_engine_not_installed():
    adapter = MusicAdapter(engine_probe=lambda: {"ace_step": (False, "nope", ""), "musicgen": (False, "nope", "")})
    plan = adapter.plan("music.generate", {"engine": "ace_step", "duration_s": 30, "style_tags": ["pop"]}, [])
    assert plan.ok is False
    assert "music_engine" in plan.missing


# ═══════════════════════════════════════════════════════════════════════
# MusicAdapter — submit / status / collect / cancel with a fake engine
# ═══════════════════════════════════════════════════════════════════════

def _fake_runner(seconds: float = 0.5, *, sample_rate: int = 8000, delay: float = 0.0,
                 raise_error: Optional[Exception] = None):
    def runner(engine: str, params: Dict[str, Any], out_path: str) -> _EngineResult:
        if delay:
            time.sleep(delay)
        if raise_error is not None:
            raise raise_error
        synth_wav(out_path, seconds=seconds, sample_rate=sample_rate)
        return _EngineResult(path=out_path, bpm=params.get("bpm") or 120, key=params.get("key") or "C major",
                             timing=None, engine_version="fake-1.0", warnings=())
    return runner


def _staging(tmp_path, workdir_name="w") -> port.Staging:
    return port.Staging(owner=OWNER, project_id="proj", workdir=str(tmp_path / workdir_name), input_paths={})


def test_submit_plan_status_collect_full_lifecycle(tmp_path):
    adapter = MusicAdapter(engine_probe=_available_probe, engine_runner=_fake_runner())
    plan = adapter.plan("music.generate", {"engine": "ace_step", "duration_s": 20,
                                           "style_tags": ["pop"], "seed": 42}, [])
    assert plan.ok
    result = adapter.submit(plan, _staging(tmp_path))
    assert result.state == "accepted"
    assert _wait_until(lambda: adapter.status(result.job_id).state == "completed")

    collected = adapter.collect(result.job_id, str(tmp_path / "collect"))
    assert collected.ok
    kinds = {o.media_type for o in collected.outputs}
    assert kinds == {"audio/wav", "application/json"}
    audio = next(o for o in collected.outputs if o.media_type == "audio/wav")
    assert audio.byte_size > 0
    assert audio.valid


def test_submit_rejects_invalid_plan_before_queue(tmp_path):
    adapter = MusicAdapter(engine_probe=_available_probe)
    bad_plan = adapter.plan("music.generate", {"engine": "ace_step", "duration_s": 999}, [])
    result = adapter.submit(bad_plan, _staging(tmp_path))
    assert result.state == "rejected_before_queue"


def test_cancel_before_worker_starts(tmp_path):
    adapter = MusicAdapter(engine_probe=_available_probe, engine_runner=_fake_runner(delay=5.0))
    # A pool of size 1 already busy with a slow first job — the second job
    # sits `queued` and can be cancelled before it ever runs.
    plan1 = adapter.plan("music.generate", {"engine": "ace_step", "duration_s": 20, "style_tags": ["pop"]}, [])
    job1 = adapter.submit(plan1, _staging(tmp_path, "w1"))
    plan2 = adapter.plan("music.generate", {"engine": "ace_step", "duration_s": 20, "style_tags": ["rock"]}, [])
    job2 = adapter.submit(plan2, _staging(tmp_path, "w2"))
    assert _wait_until(lambda: adapter.status(job1.job_id).state == "running")
    assert adapter.status(job2.job_id).state == "queued"
    outcome = adapter.cancel(job2.job_id)
    assert outcome.outcome == "accepted"
    assert adapter.status(job2.job_id).state == "cancelled"


def test_collect_before_completion_fails_honestly(tmp_path):
    adapter = MusicAdapter(engine_probe=_available_probe, engine_runner=_fake_runner(delay=2.0))
    plan = adapter.plan("music.generate", {"engine": "ace_step", "duration_s": 20, "style_tags": ["pop"]}, [])
    result = adapter.submit(plan, _staging(tmp_path))
    collected = adapter.collect(result.job_id, str(tmp_path / "c"))
    assert collected.ok is False
    adapter.cancel(result.job_id)


# ═══════════════════════════════════════════════════════════════════════
# song_ops — typed operations
# ═══════════════════════════════════════════════════════════════════════

def test_song_ops_edit_lyrics_and_add_section_and_reorder():
    from src.creator.ops import song_ops

    doc = {"kind": "song", "content": _song_content()}
    content = song_ops.edit_lyrics(doc, {"object_id": "s1", "lyrics": "new lyrics here"})
    assert content["sections"][0]["lyrics"] == "new lyrics here"

    doc2 = {"kind": "song", "content": content}
    content2 = song_ops.add_section(doc2, {"section_id": "s3", "kind": "bridge", "lyrics": "bridge text"})
    assert [s["id"] for s in content2["sections"]] == ["s1", "s2", "s3"]

    doc3 = {"kind": "song", "content": content2}
    content3 = song_ops.reorder_sections(doc3, {"order": ["s3", "s1", "s2"]})
    assert [s["id"] for s in content3["sections"]] == ["s3", "s1", "s2"]

    with pytest.raises(InvalidOperation):
        song_ops.reorder_sections(doc3, {"order": ["s1", "s2"]})  # not a full permutation


def test_song_ops_set_style_and_set_seed():
    from src.creator.ops import song_ops

    doc = {"kind": "song", "content": _song_content()}
    content = song_ops.set_style(doc, {"style_tags": ["trap"], "bpm_target": 140, "key_target": "A minor"})
    assert content["style_tags"] == ["trap"]
    assert content["bpm_target"] == 140
    assert content["key_target"] == "A minor"

    doc2 = {"kind": "song", "content": content}
    content2 = song_ops.set_seed(doc2, {"seed": 7})
    assert content2["seed"] == 7


def test_song_ops_remove_section_refuses_to_empty_the_list():
    from src.creator.ops import song_ops

    doc = {"kind": "song", "content": _song_content(sections=[{"id": "only", "kind": "verse", "lyrics": "x"}])}
    with pytest.raises(InvalidOperation):
        song_ops.remove_section(doc, {"object_id": "only"})


def test_song_ops_record_and_select_take():
    from src.creator.ops import song_ops

    doc = {"kind": "song", "content": _song_content()}
    take = {"id": "t1", "occurrence_id": "occ_1", "engine": "ace_step", "seed": 1,
           "bpm": 120, "key": "C", "duration_s": 20, "created_at": time.time(), "lyrics_timing": None}
    content = song_ops.record_take(doc, {"take": take, "select": True})
    assert content["takes"][0]["id"] == "t1"
    assert content["selected_take"] == "t1"

    with pytest.raises(InvalidOperation):
        song_ops.record_take({"kind": "song", "content": content}, {"take": take})  # duplicate id

    doc2 = {"kind": "song", "content": content}
    content2 = song_ops.select_take(doc2, {"take_id": None})
    assert content2["selected_take"] is None


# ═══════════════════════════════════════════════════════════════════════
# src.creator.music — full orchestration into a song.record_take command
# ═══════════════════════════════════════════════════════════════════════

def test_generate_rejects_when_no_engine_installed(project_env):
    """No monkeypatching of the adapter here — this sandbox genuinely has
    neither acestep nor audiocraft installed."""
    doc = _make_song(OWNER, project_env)
    with pytest.raises(music_mod.MusicError):
        music_mod.generate(OWNER, doc.id, duration_s=20)


def test_generate_rejects_non_song_document(project_env):
    other = store_mod.get_store().create(
        OWNER, project_env, "canvas",
        {"width": 4, "height": 4, "layers": [], "base_asset_ref": "none"},
    )
    with pytest.raises(music_mod.MusicError):
        music_mod.generate(OWNER, other.id, duration_s=20)


def _install_fake_adapter(monkeypatch, **runner_kwargs):
    adapter = MusicAdapter(engine_probe=_available_probe, engine_runner=_fake_runner(**runner_kwargs))
    monkeypatch.setattr(music_mod, "_adapter", lambda: adapter)
    return adapter


def test_generate_completes_and_records_a_take(project_env, monkeypatch):
    _install_fake_adapter(monkeypatch)
    doc = _make_song(OWNER, project_env)

    job = music_mod.generate(OWNER, doc.id, duration_s=20, engine="ace_step", seed=99)
    assert job["state"] in ("queued", "running", "completed")
    assert _wait_until(lambda: music_mod.get_job(OWNER, job["job_id"])["state"] == "completed")

    final = music_mod.get_job(OWNER, job["job_id"])
    assert final["occurrence_id"]
    assert final["take_id"]

    updated = store_mod.get_store().get(OWNER, doc.id)
    assert len(updated.content["takes"]) == 1
    take = updated.content["takes"][0]
    assert take["occurrence_id"] == final["occurrence_id"]
    assert take["seed"] == 99
    assert take["engine"] == "ace_step"
    assert take["lyrics_timing"] is None  # honestly absent — the fake engine reports no timing

    # MUS01: the song is saved as a linked asset, not just a message.
    from src import artifact_identity as identity
    occ = identity.for_owner(final["occurrence_id"], owner=OWNER)
    assert occ.kind == "audio"
    assert doc.id in occ.relations.derived_from


def test_generate_foreign_owner_document_is_not_found(project_env, monkeypatch):
    _install_fake_adapter(monkeypatch)
    doc = _make_song(OWNER, project_env)
    with pytest.raises(music_mod.MusicJobNotFound):
        music_mod.generate("mallory", doc.id, duration_s=20)


def test_variants_use_distinct_seeds_and_reject_bad_ones_independently(project_env, monkeypatch):
    _install_fake_adapter(monkeypatch)
    doc = _make_song(OWNER, project_env)

    jobs = music_mod.variants(OWNER, doc.id, 3, duration_s=20, engine="ace_step", base_seed=100)
    assert len(jobs) == 3
    for job in jobs:
        assert job["state"] in ("queued", "running", "completed", "failed")

    ok_jobs = [j for j in jobs if j.get("job_id")]
    assert len(ok_jobs) == 3
    for job in ok_jobs:
        assert _wait_until(lambda j=job: music_mod.get_job(OWNER, j["job_id"])["state"] == "completed")

    updated = store_mod.get_store().get(OWNER, doc.id)
    seeds = sorted(t["seed"] for t in updated.content["takes"])
    assert seeds == [100, 101, 102]
    # Choosing a variant never deletes history or the others' takes.
    assert len(updated.content["takes"]) == 3


def test_variants_rejects_more_than_eight_per_call(project_env, monkeypatch):
    _install_fake_adapter(monkeypatch)
    doc = _make_song(OWNER, project_env)
    with pytest.raises(music_mod.MusicError):
        music_mod.variants(OWNER, doc.id, 9, duration_s=20)


def test_generate_dedupes_the_recorded_take_on_repeated_completion_poll(project_env, monkeypatch):
    """A crashed-and-retried poll (or two concurrent pollers) must never
    record the same job's take twice — `command_id` dedupe at the store
    layer is what prevents it, exercised here by calling the internal
    finish path twice directly."""
    _install_fake_adapter(monkeypatch)
    doc = _make_song(OWNER, project_env)
    job = music_mod.generate(OWNER, doc.id, duration_s=20, engine="ace_step", seed=5)
    assert _wait_until(lambda: music_mod.get_job(OWNER, job["job_id"])["state"] == "completed")

    music_mod._finish_from_collect(OWNER, job["job_id"])  # second call: must no-op
    updated = store_mod.get_store().get(OWNER, doc.id)
    assert len(updated.content["takes"]) == 1


# ═══════════════════════════════════════════════════════════════════════
# routes — flag gate, generate, get, preflight gate
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture()
def route_client(tmp_path, monkeypatch, project_env):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: True if key == "creator_enabled" else default)
    _install_fake_adapter(monkeypatch)

    from routes.creator_music_routes import setup_creator_music_routes
    app = FastAPI()
    app.include_router(setup_creator_music_routes())
    client = TestClient(app)
    return client, project_env


def test_route_flag_off_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "creator_enabled" else default)
    from routes.creator_music_routes import setup_creator_music_routes
    app = FastAPI()
    app.include_router(setup_creator_music_routes())
    client = TestClient(app)
    assert client.post("/api/creator/music/generate", json={"doc_id": "doc_x"}).status_code == 404
    assert client.get("/api/creator/music/job_x").status_code == 404
    assert client.post("/api/creator/music/doc_x/variants", json={"n": 2}).status_code == 404


def test_route_generate_requires_doc_id(route_client):
    client, _project_id = route_client
    resp = client.post("/api/creator/music/generate", json={})
    assert resp.status_code == 400


def test_route_generate_unknown_document_is_404(route_client):
    client, _project_id = route_client
    resp = client.post("/api/creator/music/generate", json={"doc_id": "doc_nope", "duration_s": 20})
    assert resp.status_code == 404


def test_route_generate_and_get_full_flow(route_client):
    client, project_id = route_client
    doc = _make_song("__odysseus_local__", project_id)

    resp = client.post("/api/creator/music/generate",
                       json={"doc_id": doc.id, "duration_s": 20, "engine": "ace_step", "seed": 11})
    assert resp.status_code == 200, resp.text
    job = resp.json()
    assert job["state"] in ("queued", "running", "completed")

    def _completed():
        r = client.get(f"/api/creator/music/{job['job_id']}")
        return r.status_code == 200 and r.json()["state"] == "completed"

    assert _wait_until(_completed)
    final = client.get(f"/api/creator/music/{job['job_id']}").json()
    assert final["occurrence_id"]


def test_route_get_unknown_job_is_404(route_client):
    client, _project_id = route_client
    assert client.get("/api/creator/music/music_does_not_exist").status_code == 404


def test_route_variants_full_flow(route_client):
    client, project_id = route_client
    doc = _make_song("__odysseus_local__", project_id)
    resp = client.post(f"/api/creator/music/{doc.id}/variants",
                       json={"n": 2, "duration_s": 20, "engine": "ace_step", "seed": 200})
    assert resp.status_code == 200, resp.text
    jobs = resp.json()["jobs"]
    assert len(jobs) == 2


def test_route_generate_blocked_without_approved_preflight_when_required(route_client, monkeypatch):
    """Forces `run_preflight` to say this plan needs approval, and checks
    the route refuses to submit without a granted card — the ficha's
    "exige preflight aprobado" requirement, exercised end to end without
    depending on this sandbox's actual budget thresholds."""
    client, project_id = route_client
    doc = _make_song("__odysseus_local__", project_id)

    from src.creator import preflight as preflight_mod

    class _FakeReport:
        requires_approval = True
        approval_digest = "deadbeef" * 4
        approval_plan = {"action": "creator.generate_music", "fields": {}}

        def to_dict(self):
            return {}

    monkeypatch.setattr(preflight_mod, "run_preflight", lambda **kwargs: _FakeReport())

    resp = client.post("/api/creator/music/generate", json={"doc_id": doc.id, "duration_s": 20})
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "preflight_required"

    # A digest that does not match the recomputed plan's digest: 409.
    resp2 = client.post("/api/creator/music/generate",
                        json={"doc_id": doc.id, "duration_s": 20, "preflight_digest": "wrong"})
    assert resp2.status_code == 409
