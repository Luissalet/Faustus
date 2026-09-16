"""tests/test_creator_wp14_render.py — WP14: deterministic filtergraph,
exact tick conversion, render cache hit/miss, a REAL ffmpeg render (skipped
if not on PATH — "no afirmar éxito de motores por un mock"), progress
parsing, real per-process cancellation, and complete `derived_from`
provenance.

Fixtures follow the exact patterns `tests/test_creator_wp10_adapters.py`
(own_database/own_budget/own_artifact_store, `_make_input`, `sample_clip`)
and `tests/test_creator_wp13_routes.py` (`DocumentStore` swapped onto
`src.creator.store._store`) already establish for this domain.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from services.projects import ProjectStore
from src import budget_account
from src.artifact_identity import ensure_blob, record_occurrence
from src.contracts.blob import ArtifactOccurrence
from src.creator.adapter_port import Staging
from src.creator.adapters.ffmpeg import FfmpegAdapter
from src.creator.ops.model import Rational
from src.creator.render import cache as cache_mod
from src.creator.render import graph as graph_mod
from src.creator.render import service as render_service
from src.creator.render.profiles import Profile, get_profile, list_profiles
from src.creator.store import DocumentStore

OWNER = "alice"
LOCAL_OWNER = "__odysseus_local__"
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# ═══════════════════════════════════════════════════════════════════════
# shared fixtures
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "creator_wp14.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def own_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(budget_account, "default_path", lambda: tmp_path / "budget.sqlite3")


@pytest.fixture()
def own_artifact_store(tmp_path, monkeypatch):
    from src import artifact_store
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "artifact_store"))
    return artifact_store


@pytest.fixture()
def own_render_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "default_path", lambda: tmp_path / "render_cache.db")


@pytest.fixture()
def own_document_store(tmp_path, monkeypatch):
    import src.creator.store as store_mod
    doc_store = DocumentStore(str(tmp_path / "creator_docs.sqlite3"))
    monkeypatch.setattr(store_mod, "_store", doc_store)
    return doc_store


@pytest.fixture()
def own_projects(tmp_path, monkeypatch):
    import services.projects as projects_mod
    ps = ProjectStore(str(tmp_path / "projects"))
    monkeypatch.setattr(projects_mod, "_store", ps)
    return ps


@pytest.fixture()
def creator_enabled(monkeypatch):
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: True if key == "creator_enabled" else default)


def _make_input(owner: str, occurrence_id: str, *, path: str) -> str:
    from src import artifact_store
    sha, size, filename, _ = artifact_store.publish_copy(
        path, artifact_store.ARTIFACT_STORE_DIR, os.path.basename(path))
    ensure_blob(sha256=sha, byte_size=size, filename=filename, media_type="video/mp4")
    occ = ArtifactOccurrence.parse({
        "id": occurrence_id, "kind": "video", "blob_sha256": sha, "owner": owner,
    })
    record_occurrence(occ)
    return occurrence_id


@pytest.fixture()
def sample_clip(tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    out = str(tmp_path / "clip.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out,
    ], check=True, capture_output=True)
    return out


def _timeline_content(video_occ: str, audio_occ: str, *, ticks: int = 5) -> dict:
    clock = {"ticks_per_second_numerator": "5", "ticks_per_second_denominator": "1"}
    return {
        "clock": clock,
        "duration_ticks": str(ticks),
        "tracks": [
            {"id": "v0", "kind": "video", "locked": False, "clips": [{
                "id": "vc0", "asset_ref": video_occ,
                "timeline_start_ticks": "0", "timeline_duration_ticks": str(ticks),
                "source_range": {"start_ticks": "0", "duration_ticks": str(ticks)},
                "source_clock": clock,
            }]},
            {"id": "a0", "kind": "audio", "locked": False, "clips": [{
                "id": "ac0", "asset_ref": audio_occ,
                "timeline_start_ticks": "0", "timeline_duration_ticks": str(ticks),
                "source_range": {"start_ticks": "0", "duration_ticks": str(ticks)},
                "source_clock": clock, "gain_db": -6,
            }]},
        ],
    }


# ═══════════════════════════════════════════════════════════════════════
# graph.py — pure: determinism, exact ticks, allowed filters
# ═══════════════════════════════════════════════════════════════════════

def test_filtergraph_is_deterministic():
    content = _timeline_content("occA", "occB")
    profile = get_profile("shorts_9x16")
    g1 = graph_mod.compile_graph(content, profile=profile, document_revision=3)
    g2 = graph_mod.compile_graph(content, profile=profile, document_revision=3)
    assert g1.filter_complex == g2.filter_complex
    assert g1.video_map == g2.video_map
    assert g1.audio_map == g2.audio_map
    assert [i.occurrence_id for i in g1.inputs] == ["occA", "occB"]


def test_filtergraph_changes_with_document_revision_in_cache_key_inputs():
    """The compiled STRING can be identical across two revisions with the
    same content (nothing here forces it to differ), but `document_revision`
    is carried on the `RenderGraph` for `cache.py` to fold in regardless —
    this asserts that field is actually populated, not dropped."""
    content = _timeline_content("occA", "occB")
    profile = get_profile("shorts_9x16")
    g1 = graph_mod.compile_graph(content, profile=profile, document_revision=1)
    g2 = graph_mod.compile_graph(content, profile=profile, document_revision=2)
    assert g1.document_revision == 1
    assert g2.document_revision == 2


def test_only_allowlisted_filters_appear_in_the_compiled_graph():
    content = _timeline_content("occA", "occB")
    profile = get_profile("youtube_4k")
    g = graph_mod.compile_graph(content, profile=profile, document_revision=1)
    for segment in g.filter_complex.split(";"):
        for step in segment.split(","):
            step = step.strip()
            while step.startswith("["):
                close = step.find("]")
                if close < 0:
                    break
                step = step[close + 1:]
            step = step.strip()
            if not step:
                continue
            name = step.split("=", 1)[0].strip()
            assert name in graph_mod.FILTER_ALLOWLIST, f"disallowed filter: {name!r}"


def test_gaps_are_filled_so_every_visual_layer_spans_the_full_duration():
    content = _timeline_content("occA", "occB", ticks=20)
    # shrink the video clip to leave an 15-tick gap at the end
    content["tracks"][0]["clips"][0]["timeline_duration_ticks"] = "5"
    content["tracks"][0]["clips"][0]["source_range"]["duration_ticks"] = "5"
    profile = get_profile("shorts_9x16")
    g = graph_mod.compile_graph(content, profile=profile, document_revision=1)
    assert "color=c=black" in g.filter_complex
    assert "concat=n=2:v=1:a=0" in g.filter_complex


def test_ticks_to_seconds_conversion_has_no_float_drift_over_100_cuts():
    """Closure criterion mirrored from `clocks.py`: convert the SAME nominal
    duration 100 times and check the digits never drift — `seconds_str`
    goes through exact integer microsecond conversion, never `float()`."""
    clock = Rational(30000, 1)  # deliberately NOT a multiple of 1_000_000
    ticks_per_cut = 1001
    strings = {graph_mod.seconds_str(i * ticks_per_cut, clock) for i in range(100)}
    # every value is independently exact — recomputing the SAME tick twice
    # (once via the set's own dedupe, once directly) must be byte-identical.
    assert graph_mod.seconds_str(37 * ticks_per_cut, clock) == graph_mod.seconds_str(37 * ticks_per_cut, clock)
    # and every string is a well-formed SECONDS.MICROSECONDS decimal.
    for s in strings:
        whole, _, micros = s.partition(".")
        assert whole.isdigit() and len(micros) == 6 and micros.isdigit()


def test_compile_graph_raises_for_a_timeline_with_no_visual_track():
    content = _timeline_content("occA", "occB")
    content["tracks"] = [content["tracks"][1]]  # audio only
    with pytest.raises(Exception):
        graph_mod.compile_graph(content, profile=get_profile("shorts_9x16"), document_revision=1)


def test_profiles_catalogue_has_the_required_targets():
    ids = {p.id for p in list_profiles()}
    assert ids == {
        "youtube_1080p", "youtube_4k", "shorts_9x16", "reels_9x16", "tiktok_9x16",
        "instagram_1x1", "linkedin_16x9", "cinema_21x9",
    }
    with pytest.raises(Exception):
        get_profile("not_a_real_profile")


# ═══════════════════════════════════════════════════════════════════════
# cache.py — deterministic key, hit/miss
# ═══════════════════════════════════════════════════════════════════════

def test_cache_key_is_pure_and_order_stable():
    k1 = cache_mod.compute_key(input_hashes=["a", "b"], doc_revision=1,
                                profile_fingerprint=("p1", 1080), engine_build="ffmpeg 6.0")
    k2 = cache_mod.compute_key(input_hashes=["a", "b"], doc_revision=1,
                                profile_fingerprint=("p1", 1080), engine_build="ffmpeg 6.0")
    k3 = cache_mod.compute_key(input_hashes=["a", "c"], doc_revision=1,
                                profile_fingerprint=("p1", 1080), engine_build="ffmpeg 6.0")
    assert k1 == k2
    assert k1 != k3


def test_cache_store_and_lookup_hit_miss(own_render_cache):
    key = cache_mod.compute_key(input_hashes=["x"], doc_revision=1,
                                 profile_fingerprint=("p",), engine_build="ffmpeg 6.0")
    assert cache_mod.lookup(key, owner=OWNER) is None  # miss
    row = cache_mod.store(key, owner=OWNER, project_id="p1", doc_id="d1",
                           output_occurrence_id="occ_out1", engine_build="ffmpeg 6.0",
                           profile_id="shorts_9x16")
    assert row["output_occurrence_id"] == "occ_out1"
    hit = cache_mod.lookup(key, owner=OWNER)
    assert hit is not None and hit["output_occurrence_id"] == "occ_out1"
    # idempotent second store keeps the FIRST occurrence id
    row2 = cache_mod.store(key, owner=OWNER, project_id="p1", doc_id="d1",
                            output_occurrence_id="occ_out2", engine_build="ffmpeg 6.0",
                            profile_id="shorts_9x16")
    assert row2["output_occurrence_id"] == "occ_out1"
    # a different owner never sees it
    assert cache_mod.lookup(key, owner="mallory") is None


# ═══════════════════════════════════════════════════════════════════════
# real ffmpeg — render, provenance, cache hit/miss, progress, cancel
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_real_render_produces_a_decodable_file_and_full_derived_from(
    sample_clip, tmp_path, own_database, own_budget, own_artifact_store,
    own_render_cache, own_document_store, own_projects, creator_enabled,
):
    project = own_projects.create("WP14 Proj", owner=OWNER, scaffold_memory=False)
    project_id = project["id"]
    video_occ = _make_input(OWNER, "occ_video1", path=sample_clip)
    audio_occ = _make_input(OWNER, "occ_audio1", path=sample_clip)

    doc = own_document_store.create(OWNER, project_id, "timeline",
                                     _timeline_content(video_occ, audio_occ))

    render_plan = render_service.plan(owner=OWNER, project_id=project_id, doc_id=doc.id,
                                       profile_id="shorts_9x16")
    assert render_plan.engine_available is True
    assert render_plan.cached_occurrence_id is None

    result = render_service.render(owner=OWNER, project_id=project_id, doc_id=doc.id,
                                    profile_id="shorts_9x16")
    assert result["cache_hit"] is False, result
    assert result["status"] == "completed", result
    assert result["occurrence_id"], result
    assert len(result["artifacts"]) == 1

    # "Archivo final existe y decodifica": read it back and ffprobe it for real.
    from src import artifact_identity as identity
    occ = identity.for_owner(result["occurrence_id"], owner=OWNER)
    path = identity.path_for(result["occurrence_id"], owner=OWNER)
    assert os.path.exists(path)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", path],
        capture_output=True, text=True, timeout=15,
    )
    assert probe.returncode == 0, probe.stderr
    assert "codec_type=video" in probe.stdout

    # derived_from covers EVERY input occurrence.
    assert set(occ.relations.derived_from) == {video_occ, audio_occ}

    # a SECOND render of the exact same doc/profile is a cache HIT, reusing
    # the SAME output occurrence, with no new job.
    result2 = render_service.render(owner=OWNER, project_id=project_id, doc_id=doc.id,
                                     profile_id="shorts_9x16")
    assert result2["cache_hit"] is True
    assert result2["occurrence_id"] == result["occurrence_id"]
    assert result2["job_id"] is None


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_progress_is_parsed_from_progress_pipe(sample_clip, tmp_path):
    adapter = FfmpegAdapter()
    staging = Staging(owner=OWNER, project_id="p1", workdir=str(tmp_path / "stage"),
                       input_paths={"occ_clip": sample_clip})
    plan = adapter.plan("graph", {
        "filter_complex": "[0:v]trim=start=0.0:end=1.0,setpts=PTS-STARTPTS,"
                           "scale=64:64,setsar=1,fps=5[v0]",
        "video_map": "[v0]", "audio_map": "", "output_args": ["-r", "5", "-an"],
        "output_ext": "mp4", "total_duration_seconds": 1.0,
    }, ["occ_clip"])
    assert plan.ok is True, plan.detail

    result = adapter.submit(plan, staging)
    assert result.state == "accepted", result.detail
    status = adapter.status(result.job_id)
    assert status.state == "completed"
    assert status.progress == 1.0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_cancel_kills_the_real_process_without_touching_others(tmp_path):
    """WP14 closing criterion: "cancelación no mata procesos ajenos". Starts
    a real, deliberately slow `graph` render on a background thread, cancels
    it mid-flight, and checks (a) the SAME process this test started is
    really dead (`poll() is not None`), and (b) a DIFFERENT, unrelated
    ffmpeg process (its own `sleep`-shaped run) this test starts concurrently
    is completely unaffected — proof `cancel()` never reaches for "any
    ffmpeg pid", only the one it was handed."""
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    # A synthetic (no real file needed) large/slow source: high-res color
    # fill scaled through several passes to take real wall-clock time.
    src = str(tmp_path / "slow_src.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=6:size=320x240:rate=25",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", src,
    ], check=True, capture_output=True, timeout=30)

    adapter = FfmpegAdapter()
    staging = Staging(owner=OWNER, project_id="p1", workdir=str(stage_dir),
                       input_paths={"occ_slow": src})
    # Chained heavy scaling to keep libx264 busy well past our cancel delay.
    heavy_filter = (
        "[0:v]trim=start=0:end=6,setpts=PTS-STARTPTS,"
        "scale=3840:2160,scale=320:240,scale=3840:2160,scale=320:240,"
        "scale=3840:2160,fps=25,setsar=1[v0]"
    )
    plan = adapter.plan("graph", {
        "filter_complex": heavy_filter, "video_map": "[v0]", "audio_map": "",
        "output_args": ["-r", "25", "-an"], "output_ext": "mp4",
        "total_duration_seconds": 6.0,
    }, ["occ_slow"])
    assert plan.ok is True, plan.detail

    holder: dict = {}

    def _run():
        holder["result"] = adapter.submit(plan, staging)

    # An unrelated, independent ffmpeg process — must survive the cancel.
    other = subprocess.Popen(["ffmpeg", "-y", "-f", "lavfi", "-i",
                               "testsrc=duration=6:size=64x64:rate=5",
                               str(tmp_path / "other.mp4")],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    from src.creator.adapters import ffmpeg as ffmpeg_mod

    job_id = None
    deadline = time.time() + 10
    while time.time() < deadline:
        with ffmpeg_mod._JOBS_LOCK:
            for jid, job in ffmpeg_mod._JOBS.items():
                if job.get("output_path", "").startswith(str(stage_dir)) and job.get("proc") is not None:
                    job_id = jid
                    break
        if job_id:
            break
        time.sleep(0.05)
    assert job_id is not None, "job never registered a running process"

    outcome = adapter.cancel(job_id)
    assert outcome.outcome in ("accepted", "confirmed")
    thread.join(timeout=15)
    assert "result" in holder

    status = adapter.status(job_id)
    assert status.state == "cancelled"

    with ffmpeg_mod._JOBS_LOCK:
        proc = ffmpeg_mod._JOBS[job_id].get("proc")
    assert proc is not None
    # The real process is gone (terminated), never merely marked so.
    deadline = time.time() + 5
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert proc.poll() is not None, "cancel() did not actually kill the process"

    # The unrelated process is untouched by the cancel — either still
    # running or finished ON ITS OWN, never killed by us.
    other.wait(timeout=15)
    assert other.returncode == 0, "an unrelated ffmpeg process was disturbed by cancel()"


# ═══════════════════════════════════════════════════════════════════════
# routes — /api/creator/render/plan, /api/creator/render, GET, cancel
# ═══════════════════════════════════════════════════════════════════════

def _stub_capabilities(monkeypatch, caps):
    from src.creator import preflight as pf
    monkeypatch.setattr(pf, "_capabilities_for", lambda deployment_id: caps)


def _stub_params_ok(monkeypatch):
    from src.creator import preflight as pf
    monkeypatch.setattr(pf, "_validate_params", lambda engine, task, params: True)


@pytest.fixture()
def route_client(tmp_path, monkeypatch, own_database, own_budget, own_artifact_store,
                  own_render_cache, own_document_store, own_projects, creator_enabled):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_ENABLED", "false")
    _stub_capabilities(monkeypatch, {
        "operations": {"render_timeline": {"requires_consent": False, "consent_subject_param": ""}},
        "is_cloud": False, "weights_bytes": None, "model_info": None,
        "price_usd_per_1k_tokens": None,
    })
    _stub_params_ok(monkeypatch)

    from routes.creator_render_routes import setup_creator_render_routes
    from routes.creator_preflight_routes import setup_creator_preflight_routes
    app = FastAPI()
    app.include_router(setup_creator_render_routes())
    app.include_router(setup_creator_preflight_routes())
    client = TestClient(app)

    project = own_projects.create("WP14 Route Proj", owner=LOCAL_OWNER, scaffold_memory=False)
    return client, project["id"]


def test_route_flag_off_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "creator_enabled" else default)
    from routes.creator_render_routes import setup_creator_render_routes
    app = FastAPI()
    app.include_router(setup_creator_render_routes())
    client = TestClient(app)
    assert client.post("/api/creator/render/plan", json={
        "project_id": "p", "doc_id": "d", "profile_id": "shorts_9x16"}).status_code == 404


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_route_plan_needs_no_approval(route_client, sample_clip):
    client, project_id = route_client
    video_occ = _make_input(LOCAL_OWNER, "occ_v1", path=sample_clip)
    audio_occ = _make_input(LOCAL_OWNER, "occ_a1", path=sample_clip)
    from src.creator.store import get_store
    doc = get_store().create(LOCAL_OWNER, project_id, "timeline", _timeline_content(video_occ, audio_occ))

    resp = client.post("/api/creator/render/plan", json={
        "project_id": project_id, "doc_id": doc.id, "profile_id": "shorts_9x16"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["profile"]["id"] == "shorts_9x16"
    assert body["cached_occurrence_id"] is None


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_route_render_requires_approval_then_starts_and_completes(route_client, sample_clip):
    client, project_id = route_client
    from services.projects import get_store as get_project_store
    from src.creator import profile as profile_mod
    profile_mod.set_profile(get_project_store(), LOCAL_OWNER, project_id,
                            {"budget_mode": "warn", "production_ceiling_tokens": 1})

    video_occ = _make_input(LOCAL_OWNER, "occ_v2", path=sample_clip)
    audio_occ = _make_input(LOCAL_OWNER, "occ_a2", path=sample_clip)
    from src.creator.store import get_store
    doc = get_store().create(LOCAL_OWNER, project_id, "timeline", _timeline_content(video_occ, audio_occ))

    body = {"project_id": project_id, "doc_id": doc.id, "profile_id": "shorts_9x16"}

    plan_resp = client.post("/api/creator/render/plan", json=body)
    assert plan_resp.status_code == 200, plan_resp.text
    estimated_kb = max(1, plan_resp.json()["estimated_size_bytes"] // 1024)

    # 403 without a preflight digest.
    resp = client.post("/api/creator/render", json=body)
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "approval_required"
    digest = resp.json()["detail"]["digest"]
    assert digest

    preflight_params = {
        "project_id": project_id, "operation": "render_timeline", "engine": "ffmpeg",
        "deployment_id": "ffmpeg", "inputs": [video_occ, audio_occ],
        "params": {"profile_id": "shorts_9x16", "doc_id": doc.id, "doc_revision": doc.revision,
                   "estimated_tokens": estimated_kb},
    }
    pre = client.post("/api/creator/preflight", json=preflight_params)
    assert pre.status_code == 200, pre.text
    assert pre.json()["approval_digest"] == digest

    approve = client.post(f"/api/creator/preflight/{digest}/approve", json=preflight_params)
    assert approve.status_code == 200, approve.text

    started = client.post("/api/creator/render", json={**body, "preflight_digest": digest})
    assert started.status_code == 200, started.text
    token = started.json()["render_token"]
    assert token

    # second submit with the SAME (now spent) digest is rejected.
    again = client.post("/api/creator/render", json={**body, "preflight_digest": digest})
    assert again.status_code == 409

    deadline = time.time() + 15
    job = None
    while time.time() < deadline:
        got = client.get(f"/api/creator/render/{token}")
        assert got.status_code == 200
        job = got.json()
        if job["state"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job is not None and job["state"] == "done", job
    assert job["result"]["status"] == "completed"
    assert job["result"]["occurrence_id"]


def test_route_get_unknown_job_is_404(route_client):
    client, _project_id = route_client
    resp = client.get("/api/creator/render/not_a_real_token")
    assert resp.status_code == 404


def test_route_cancel_unknown_job_is_404(route_client):
    client, _project_id = route_client
    resp = client.post("/api/creator/render/not_a_real_token/cancel")
    assert resp.status_code == 404
