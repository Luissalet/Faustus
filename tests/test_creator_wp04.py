"""WP04 — bounded ingestion and proxies (src/creator/ingest.py).

Real sqlite (own engine, per test_creator_wp03.py's pattern), a real PNG via
Pillow, a real synthetic WAV via the stdlib ``wave`` module (no ffmpeg
needed to produce test fixtures for the audio-signature/size-limit cases),
ffmpeg/ffprobe monkeypatched for the deterministic validation/proxy-pipeline
tests, and one real-ffmpeg end-to-end test (skipped when ffmpeg is not on
this machine) exercising the actual 480p transcode and numpy peaks decode.
"""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
import wave
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from src import artifact_identity as identity
from src import artifact_store
from src.creator import ingest

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "wp04.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    db_mod.Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "creator" / "ingest.db")


@pytest.fixture()
def store_dir(tmp_path):
    return str(tmp_path / "store")


def _png_file(tmp_path, name="pic.png", size=(40, 30), color=(200, 40, 40)):
    from PIL import Image
    path = tmp_path / name
    Image.new("RGB", size, color).save(str(path), format="PNG")
    return str(path)


def _wav_file(tmp_path, name="clip.wav", seconds=1.0, rate=8000):
    path = tmp_path / name
    n = int(rate * seconds)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        samples = [int(500 * ((i % 40) - 20)) for i in range(n)]
        fh.writeframes(struct.pack(f"<{n}h", *samples))
    return str(path)


def _text_file(tmp_path, name="fake.png", body=b"this is not an image"):
    path = tmp_path / name
    path.write_bytes(body)
    return str(path)


def _tiny_video(tmp_path, name="clip.mp4"):
    path = tmp_path / name
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", "color=c=blue:s=32x32:d=1",
                    "-frames:v", "5", str(path)], capture_output=True, timeout=30, check=True)
    return str(path)


def _tiny_audio_mp3_container(tmp_path, name="clip.wav"):
    return _wav_file(tmp_path, name=name)


# ── fake ffprobe/ffmpeg: no real binaries, deterministic outputs ────────

def _fake_which(name):
    return f"/fake/{name}"


def _fake_run_factory(*, has_video=True, has_audio=False, duration=2.5):
    """Serves both `ffprobe` (container validation) and every `ffmpeg`
    invocation this module makes (frame/waveform via library.py, 480p
    preview, peaks pcm decode) from one callable, branching on argv shape —
    the same trick test_creator_wp03.py uses for a single-purpose fake."""
    def fake_run(cmd, **kw):
        exe = cmd[0]
        if "ffprobe" in exe:
            streams = []
            if has_video:
                streams.append({"codec_type": "video", "width": 32, "height": 32,
                               "codec_name": "h264"})
            if has_audio:
                streams.append({"codec_type": "audio", "sample_rate": "8000",
                               "channels": 1, "codec_name": "pcm_s16le"})
            payload = json.dumps({"format": {"duration": str(duration)}, "streams": streams})
            return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")
        # every ffmpeg call names its output as the LAST argv entry
        out_path = cmd[-1]
        if out_path.endswith(".pcm"):
            n = 400
            samples = [int(2000 * ((i % 20) - 10)) for i in range(n)]
            with open(out_path, "wb") as fh:
                fh.write(struct.pack(f"<{n}h", *samples))
        else:
            with open(out_path, "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n" if out_path.endswith(".png") else b"fake-mp4-bytes")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
    return fake_run


# ── size / mime validation, before anything is written ─────────────────

def test_oversized_file_is_rejected_before_any_write(own_database, tmp_path, db_path, store_dir):
    src = _png_file(tmp_path)
    with pytest.raises(ingest.IngestValidationError):
        ingest.ingest_file("alice", "p1", src, "pic.png", max_bytes=5,
                           db_path=db_path, store_dir=store_dir)
    assert ingest.get_job("alice", "does-not-exist", db_path=db_path) is None
    store = tmp_path / "store"
    assert not store.exists() or list(store.glob("**/*")) == []


def test_falsified_mime_is_rejected(own_database, tmp_path, db_path, store_dir):
    src = _text_file(tmp_path)  # named .png, actually plain text
    with pytest.raises(ingest.IngestValidationError):
        ingest.ingest_file("alice", "p1", src, "fake.png", which=lambda n: None,
                           db_path=db_path, store_dir=store_dir)


def test_empty_file_is_rejected(own_database, tmp_path, db_path, store_dir):
    path = tmp_path / "empty.png"
    path.write_bytes(b"")
    with pytest.raises(ingest.IngestValidationError):
        ingest.ingest_file("alice", "p1", str(path), "empty.png", db_path=db_path, store_dir=store_dir)


def test_missing_project_id_is_rejected(own_database, tmp_path, db_path, store_dir):
    src = _png_file(tmp_path)
    with pytest.raises(ValueError):
        ingest.ingest_file("alice", "", src, "pic.png", db_path=db_path, store_dir=store_dir)


# ── image ingestion (PIL only, no ffmpeg needed) ────────────────────────

def test_ingest_image_creates_occurrence_and_proxy(own_database, tmp_path, db_path, store_dir):
    src = _png_file(tmp_path, size=(800, 600))
    job = ingest.ingest_file("alice", "p1", src, "pic.png", db_path=db_path, store_dir=store_dir)
    assert job["state"] == "done"
    assert job["occurrence_id"]
    occ = identity.for_owner(job["occurrence_id"], owner="alice")
    assert occ.kind == "image"
    assert occ.owner == "alice"
    assert len(job["proxies"]) == 1
    assert job["proxies"][0]["recipe"] == "creator.proxy.image_thumbnail"

    proxy = identity.for_owner(job["proxies"][0]["occurrence_id"], owner="alice")
    assert proxy.relations.derived_from == (occ.id,)


def test_ingest_image_twice_is_idempotent_by_sha(own_database, tmp_path, db_path, store_dir):
    src = _png_file(tmp_path)
    first = ingest.ingest_file("alice", "p1", src, "pic.png", db_path=db_path, store_dir=store_dir)
    second = ingest.ingest_file("alice", "p1", src, "pic.png", db_path=db_path, store_dir=store_dir)
    assert first["job_id"] == second["job_id"]
    assert first["occurrence_id"] == second["occurrence_id"]


def test_ingest_image_owner_mismatch_on_same_sha_is_not_found(own_database, tmp_path, db_path, store_dir):
    src = _png_file(tmp_path)
    ingest.ingest_file("alice", "p1", src, "pic.png", db_path=db_path, store_dir=store_dir)
    with pytest.raises(ingest.IngestJobNotFound):
        # Different owner, but the deterministic job id is derived from
        # (owner, project_id, sha256) so this cannot collide in practice —
        # this only exercises the defensive branch directly.
        job_id = ingest._job_id("alice", "p1", ingest._sha256_of(src))
        ingest.resume_job("mallory", job_id, db_path=db_path, store_dir=store_dir)


# ── video/audio: ffprobe + ffmpeg monkeypatched ─────────────────────────

def test_ingest_video_builds_frame_thumbnail_and_480p_proxy(own_database, tmp_path, db_path, store_dir):
    src = _text_file(tmp_path, name="clip.mp4",
                     body=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)  # ftyp-boxed
    fake_run = _fake_run_factory(has_video=True, has_audio=False, duration=2.0)
    job = ingest.ingest_file("alice", "p1", src, "clip.mp4", which=_fake_which,
                             run=fake_run, db_path=db_path, store_dir=store_dir)
    assert job["state"] == "done"
    occ = identity.for_owner(job["occurrence_id"], owner="alice")
    assert occ.kind == "video"
    recipes = {p["recipe"] for p in job["proxies"]}
    assert "creator.proxy.video_frame" in recipes
    assert "creator.ingest.video_proxy_480p" in recipes


def test_ingest_audio_builds_waveform_and_peaks(own_database, tmp_path, db_path, store_dir):
    src = _wav_file(tmp_path)
    fake_run = _fake_run_factory(has_video=False, has_audio=True, duration=1.0)
    job = ingest.ingest_file("alice", "p1", src, "clip.wav", which=_fake_which,
                             run=fake_run, db_path=db_path, store_dir=store_dir)
    assert job["state"] == "done"
    occ = identity.for_owner(job["occurrence_id"], owner="alice")
    assert occ.kind == "audio"
    recipes = {p["recipe"] for p in job["proxies"]}
    assert "creator.proxy.audio_waveform" in recipes
    assert "creator.ingest.audio_peaks" in recipes

    peaks_entry = next(p for p in job["proxies"] if p["recipe"] == "creator.ingest.audio_peaks")
    peaks_occ = identity.for_owner(peaks_entry["occurrence_id"], owner="alice")
    peaks_path = identity.path_for(peaks_occ.id, owner="alice", store_dir=store_dir)
    with open(peaks_path) as fh:
        data = json.load(fh)
    assert data["bucket_count"] > 0
    assert all(len(p) == 2 for p in data["peaks"])


def test_video_ftyp_only_audio_stream_is_reclassified_as_audio(own_database, tmp_path, db_path, store_dir):
    src = _text_file(tmp_path, name="clip.m4a",
                     body=b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 32)
    fake_run = _fake_run_factory(has_video=False, has_audio=True, duration=1.0)
    job = ingest.ingest_file("alice", "p1", src, "clip.m4a", which=_fake_which,
                             run=fake_run, db_path=db_path, store_dir=store_dir)
    occ = identity.for_owner(job["occurrence_id"], owner="alice")
    assert occ.kind == "audio"


def test_duration_exceeding_limit_is_rejected(own_database, tmp_path, db_path, store_dir):
    src = _wav_file(tmp_path)
    fake_run = _fake_run_factory(has_video=False, has_audio=True, duration=100.0)
    with pytest.raises(ingest.IngestValidationError):
        ingest.ingest_file("alice", "p1", src, "clip.wav", which=_fake_which, run=fake_run,
                           max_duration_s=1.0, db_path=db_path, store_dir=store_dir)


def test_ffprobe_failing_the_container_is_rejected(own_database, tmp_path, db_path, store_dir):
    src = _wav_file(tmp_path)

    def failing_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="{}", stderr="malformed")

    with pytest.raises(ingest.IngestValidationError):
        ingest.ingest_file("alice", "p1", src, "clip.wav", which=_fake_which, run=failing_run,
                           db_path=db_path, store_dir=store_dir)


def test_no_ffprobe_no_ffmpeg_degrades_to_ingest_without_proxies(own_database, tmp_path, db_path, store_dir):
    src = _wav_file(tmp_path)
    job = ingest.ingest_file("alice", "p1", src, "clip.wav", which=lambda n: None,
                             db_path=db_path, store_dir=store_dir)
    assert job["state"] == "done"
    assert job["proxies"] == []  # documented degrade, not a failure


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")
def test_real_ffmpeg_end_to_end_audio(own_database, tmp_path, db_path, store_dir):
    src = _wav_file(tmp_path, seconds=1.0)
    job = ingest.ingest_file("alice", "p1", src, "clip.wav", db_path=db_path, store_dir=store_dir)
    assert job["state"] == "done"
    occ = identity.for_owner(job["occurrence_id"], owner="alice")
    assert occ.kind == "audio"
    recipes = {p["recipe"] for p in job["proxies"]}
    assert "creator.proxy.audio_waveform" in recipes
    assert "creator.ingest.audio_peaks" in recipes


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")
def test_real_ffmpeg_end_to_end_video(own_database, tmp_path, db_path, store_dir):
    src = _tiny_video(tmp_path)
    job = ingest.ingest_file("alice", "p1", src, "clip.mp4", db_path=db_path, store_dir=store_dir)
    assert job["state"] == "done"
    occ = identity.for_owner(job["occurrence_id"], owner="alice")
    assert occ.kind == "video"
    recipes = {p["recipe"] for p in job["proxies"]}
    assert "creator.proxy.video_frame" in recipes
    assert "creator.ingest.video_proxy_480p" in recipes


# ── resumability across a simulated restart ─────────────────────────────

def test_resume_job_finishes_a_job_stuck_at_proxying(own_database, tmp_path, db_path, store_dir):
    src = _wav_file(tmp_path)
    fake_run = _fake_run_factory(has_video=False, has_audio=True, duration=1.0)
    job = ingest.ingest_file("alice", "p1", src, "clip.wav", which=_fake_which, run=fake_run,
                             db_path=db_path, store_dir=store_dir)
    assert job["state"] == "done"

    # Simulate a crash right after the source occurrence was durably
    # recorded but before proxies were built: rewind the job row, WITHOUT
    # touching the already-durable occurrence.
    ingest._update_job(db_path, job["job_id"], state="proxying",
                       occurrence_id=job["occurrence_id"], proxies=[])
    rewound = ingest.get_job("alice", job["job_id"], db_path=db_path)
    assert rewound["state"] == "proxying"
    assert rewound["proxies"] == []

    resumed = ingest.resume_job("alice", job["job_id"], which=_fake_which, run=fake_run,
                                db_path=db_path, store_dir=store_dir)
    assert resumed["state"] == "done"
    assert resumed["occurrence_id"] == job["occurrence_id"]
    assert {p["recipe"] for p in resumed["proxies"]} == {
        "creator.proxy.audio_waveform", "creator.ingest.audio_peaks"}


def test_resume_job_on_done_or_pending_is_a_no_op(own_database, tmp_path, db_path, store_dir):
    src = _png_file(tmp_path)
    job = ingest.ingest_file("alice", "p1", src, "pic.png", db_path=db_path, store_dir=store_dir)
    same = ingest.resume_job("alice", job["job_id"], db_path=db_path, store_dir=store_dir)
    assert same["state"] == "done"
    assert same["proxies"] == job["proxies"]


def test_resume_job_unknown_id_is_not_found(own_database, db_path):
    with pytest.raises(ingest.IngestJobNotFound):
        ingest.resume_job("alice", "ing_doesnotexist", db_path=db_path)


def test_resume_pending_sweeps_every_stuck_job(own_database, tmp_path, db_path, store_dir):
    a = ingest.ingest_file("alice", "p1", _png_file(tmp_path, name="a.png"), "a.png",
                           db_path=db_path, store_dir=store_dir)
    b = ingest.ingest_file("alice", "p1", _png_file(tmp_path, name="b.png"), "b.png",
                           db_path=db_path, store_dir=store_dir)
    ingest._update_job(db_path, a["job_id"], state="proxying", occurrence_id=a["occurrence_id"], proxies=[])
    ingest._update_job(db_path, b["job_id"], state="proxying", occurrence_id=b["occurrence_id"], proxies=[])

    resumed = ingest.resume_pending(owner="alice", db_path=db_path, store_dir=store_dir)
    assert {r["job_id"] for r in resumed} == {a["job_id"], b["job_id"]}
    assert all(r["state"] == "done" for r in resumed)


# ── URL ingestion ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_url_youtube_uses_the_existing_handler(own_database, monkeypatch, db_path, store_dir):
    from services.youtube import youtube_handler as yt

    monkeypatch.setattr(yt, "is_youtube_url", lambda url: True)
    monkeypatch.setattr(yt, "extract_youtube_id", lambda url: "abc123")
    monkeypatch.setattr(yt, "YOUTUBE_AVAILABLE", True)

    async def fake_transcript(url, video_id, max_retries=3):
        return {"success": True, "transcript": "hello world transcript", "video_id": video_id}

    monkeypatch.setattr(yt, "extract_transcript_async", fake_transcript)

    job = await ingest.ingest_url("alice", "p1", "https://youtu.be/abc123",
                                  db_path=db_path, store_dir=store_dir)
    assert job["state"] == "done"
    occ = identity.for_owner(job["occurrence_id"], owner="alice")
    assert occ.kind == "text"
    path = identity.path_for(occ.id, owner="alice", store_dir=store_dir)
    assert "hello world transcript" in open(path, encoding="utf-8").read()


@pytest.mark.asyncio
async def test_ingest_url_youtube_transcript_failure_is_rejected(own_database, monkeypatch, db_path, store_dir):
    from services.youtube import youtube_handler as yt

    monkeypatch.setattr(yt, "is_youtube_url", lambda url: True)
    monkeypatch.setattr(yt, "extract_youtube_id", lambda url: "abc123")
    monkeypatch.setattr(yt, "YOUTUBE_AVAILABLE", True)

    async def fake_transcript(url, video_id, max_retries=3):
        return {"success": False, "error": "no captions", "transcript": None}

    monkeypatch.setattr(yt, "extract_transcript_async", fake_transcript)

    with pytest.raises(ingest.IngestValidationError):
        await ingest.ingest_url("alice", "p1", "https://youtu.be/abc123",
                                db_path=db_path, store_dir=store_dir)


@pytest.mark.asyncio
async def test_ingest_url_generic_web_uses_reach_router(own_database, monkeypatch, db_path, store_dir):
    from src.reach import router as reach_router
    from src.reach.base import ReachResult

    async def fake_read(url, **kw):
        return ReachResult(channel="web", backend="fake", url=url, title="A page",
                           text="some readable page text", source_trust="scrape")

    monkeypatch.setattr(reach_router, "read", fake_read)

    job = await ingest.ingest_url("alice", "p1", "https://example.com/article",
                                  db_path=db_path, store_dir=store_dir)
    assert job["state"] == "done"
    occ = identity.for_owner(job["occurrence_id"], owner="alice")
    assert occ.kind == "text"
    assert occ.label == "A page"


@pytest.mark.asyncio
async def test_ingest_url_rejects_non_http_scheme(own_database, db_path, store_dir):
    with pytest.raises(ingest.IngestValidationError):
        await ingest.ingest_url("alice", "p1", "file:///etc/passwd", db_path=db_path, store_dir=store_dir)


@pytest.mark.asyncio
async def test_ingest_url_missing_project_id(own_database, db_path, store_dir):
    with pytest.raises(ValueError):
        await ingest.ingest_url("alice", "", "https://example.com", db_path=db_path, store_dir=store_dir)


# ── HTTP layer ────────────────────────────────────────────────────────

@pytest.fixture()
def route_client(own_database, tmp_path, monkeypatch, db_path, store_dir):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: True if key == "creator_enabled" else default)
    import src.creator.ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "default_db_path", lambda: db_path)

    import src.artifact_store as artifact_store_mod
    monkeypatch.setattr(artifact_store_mod, "ARTIFACT_STORE_DIR", store_dir)
    import src.constants as constants_mod
    monkeypatch.setattr(constants_mod, "ARTIFACT_STORE_DIR", store_dir)

    from routes.creator_ingest_routes import setup_creator_ingest_routes
    app = FastAPI()
    app.include_router(setup_creator_ingest_routes())
    return TestClient(app)


def test_route_flag_off_is_404(own_database, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "creator_enabled" else default)
    from routes.creator_ingest_routes import setup_creator_ingest_routes
    app = FastAPI()
    app.include_router(setup_creator_ingest_routes())
    client = TestClient(app)
    assert client.post("/api/creator/ingest", json={"project_id": "p1", "url": "https://x.com"}).status_code == 404
    assert client.get("/api/creator/ingest/ing_x").status_code == 404


def test_route_multipart_upload_ingests_a_file(route_client, tmp_path):
    body = BytesIO()
    from PIL import Image
    Image.new("RGB", (20, 20), (1, 2, 3)).save(body, format="PNG")
    body.seek(0)

    resp = route_client.post("/api/creator/ingest",
                             data={"project_id": "p1"},
                             files={"file": ("pic.png", body, "image/png")})
    assert resp.status_code == 200, resp.text
    job = resp.json()
    assert job["state"] == "done"

    fetched = route_client.get(f"/api/creator/ingest/{job['job_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["job_id"] == job["job_id"]


def test_route_multipart_oversized_is_rejected(route_client, monkeypatch):
    import src.creator.ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "max_ingest_bytes", lambda: 5)

    body = BytesIO(b"x" * 1000)
    resp = route_client.post("/api/creator/ingest",
                             data={"project_id": "p1"},
                             files={"file": ("big.png", body, "image/png")})
    assert resp.status_code == 400


def test_route_requires_project_id_and_file_or_url(route_client):
    resp = route_client.post("/api/creator/ingest", json={"url": "https://x.com"})
    assert resp.status_code == 400


def test_route_get_unknown_job_is_404(route_client):
    resp = route_client.get("/api/creator/ingest/ing_doesnotexist")
    assert resp.status_code == 404
