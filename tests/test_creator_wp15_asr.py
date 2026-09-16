"""WP15 — the whisper ASR adapter (`src/creator/adapters/whisper.py`), the
`src/creator/asr.py` orchestration into a `transcript` document, and
`routes/creator_asr_routes.py`.

A deterministic FAKE engine throughout (CONTRATO.md rule 11: "no afirmar
éxito de motores por un mock; no ejecutar modelos") — no faster-whisper
weights are ever loaded. The one exception is `test_real_engine_describe_*`,
which is `skipif`-guarded on `faster_whisper` actually being importable in
this environment, and even then only checks `describe()` (an import-only
probe), never runs a real transcription.
"""
from __future__ import annotations

import os
import threading
import time
import wave
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src.artifact_identity import ensure_blob, record_occurrence
from src.contracts.blob import ArtifactOccurrence
from src.creator import adapter_port as port
from src.creator import asr as asr_mod
from src.creator import documents as documents_mod
from src.creator.adapters import whisper as whisper_mod
from src.creator.adapters.whisper import WhisperAdapter

OWNER = "alice"
HAS_FASTER_WHISPER = False
try:
    import faster_whisper  # noqa: F401
    HAS_FASTER_WHISPER = True
except ImportError:
    pass


# ── shared fixtures ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_whisper_module():
    whisper_mod.reset_for_tests()
    yield
    whisper_mod.reset_for_tests()


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "creator_wp15.db").as_posix()
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
    from src.creator import store as store_mod
    monkeypatch.setattr(store_mod, "default_path", lambda: str(tmp_path / "creator_documents.sqlite3"))
    monkeypatch.setattr(store_mod, "_store", None)
    return store_mod


def _synthetic_wav(path: str, *, seconds: float = 1.0, sample_rate: int = 8000) -> str:
    """A tiny, real, silent WAV file — enough for ffmpeg/ffprobe to treat as
    genuine audio without needing any external fixture asset."""
    n_frames = int(seconds * sample_rate)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_frames)
    return path


def _make_audio_occurrence(owner: str, occurrence_id: str, project_id: str,
                           wav_path: str) -> str:
    from src import artifact_store
    sha, size, filename, _ = artifact_store.publish_copy(
        wav_path, artifact_store.ARTIFACT_STORE_DIR, os.path.basename(wav_path))
    ensure_blob(sha256=sha, byte_size=size, filename=filename, media_type="audio/wav")
    occ = ArtifactOccurrence.parse({
        "id": occurrence_id, "kind": "audio", "blob_sha256": sha,
        "owner": owner, "project_id": project_id, "label": "sample.wav",
    })
    record_occurrence(occ)
    return occurrence_id


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ── fake faster-whisper engine ──────────────────────────────────────────

class FakeWord:
    def __init__(self, word: str, start: Optional[float], end: Optional[float],
                probability: Optional[float]):
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability


class FakeSegment:
    def __init__(self, start: float, end: float, text: str, words: List[FakeWord]):
        self.start = start
        self.end = end
        self.text = text
        self.words = words


class FakeInfo:
    def __init__(self, language: str = "en", language_probability: float = 0.98):
        self.language = language
        self.language_probability = language_probability


def _default_segments() -> List[FakeSegment]:
    return [
        FakeSegment(0.0, 1.0, "hello there", [
            FakeWord("hello", 0.0, 0.4, 0.95),
            FakeWord("there", 0.4, 1.0, 0.90),
        ]),
        FakeSegment(1.0, 2.0, "general kenobi", [
            FakeWord("general", 1.0, 1.5, 0.80),
            FakeWord("kenobi", 1.5, None, None),  # SUB01: an un-aligned word
        ]),
    ]


class FakeModel:
    """Stands in for `faster_whisper.WhisperModel`. `block_event`, when
    given, makes `transcribe()`'s generator wait before yielding its first
    segment — how the queue-of-one test controls exactly when a job
    "finishes" without a real sleep race."""

    def __init__(self, segments: Optional[List[FakeSegment]] = None,
                info: Optional[FakeInfo] = None,
                block_event: Optional[threading.Event] = None):
        self.segments = segments if segments is not None else _default_segments()
        self.info = info or FakeInfo()
        self.block_event = block_event

    def transcribe(self, path: str, **kwargs):
        def _gen():
            if self.block_event is not None:
                self.block_event.wait(timeout=10)
            for seg in self.segments:
                yield seg
        return _gen(), self.info


def _fake_adapter(*, segments=None, block_event=None, available=True) -> WhisperAdapter:
    model = FakeModel(segments=segments, block_event=block_event)
    return WhisperAdapter(
        model_loader=lambda model_size, device: model,
        engine_available=lambda: (available, "" if available else "fake: unavailable", "fake-1.0"),
    )


def _staging(occ_id: str, path: str, *, owner=OWNER, project_id="p1", workdir="") -> port.Staging:
    return port.Staging(owner=owner, project_id=project_id, workdir=workdir or "/tmp",
                        input_paths={occ_id: path})


# ═══════════════════════════════════════════════════════════════════════
# describe() / plan() — pure, no weights ever touched
# ═══════════════════════════════════════════════════════════════════════

def test_describe_without_faster_whisper_reports_unavailable_and_reason():
    """Real environment probe (this sandbox has no faster-whisper
    installed) — the ficha's "sin motor → describe lo dice" criterion,
    exercised for real rather than through the fake."""
    adapter = WhisperAdapter()
    manifest = adapter.describe()
    if HAS_FASTER_WHISPER:
        pytest.skip("faster-whisper IS installed in this environment")
    assert manifest.available is False
    assert "faster-whisper is not installed" in manifest.reason
    assert manifest.supports_reconcile is False
    assert "models" in manifest.limits and "tiny" in manifest.limits["models"]


def test_describe_never_imports_a_model_only_the_package(monkeypatch):
    """Importing this module, and calling describe(), must never construct
    a WhisperModel (which would load/download weights)."""
    calls = []

    class _ExplodingLoader:
        def __call__(self, model_size, device):
            calls.append((model_size, device))
            raise AssertionError("describe()/plan() must never load a model")

    adapter = WhisperAdapter(model_loader=_ExplodingLoader(),
                             engine_available=lambda: (True, "", "fake"))
    adapter.describe()
    adapter.plan("transcribe", {}, ["occ_1"])
    assert calls == []


def test_plan_without_engine_is_rejected_before_queue():
    adapter = _fake_adapter(available=False)
    plan = adapter.plan("transcribe", {}, ["occ_1"])
    assert plan.ok is False
    assert "asr_engine" in plan.missing

    result = adapter.submit(plan, _staging("occ_1", "/nonexistent"))
    assert result.state == "rejected_before_queue"
    assert result.reason == "invalid_plan"


def test_plan_rejects_unsupported_task():
    adapter = _fake_adapter()
    plan = adapter.plan("dub", {}, ["occ_1"])
    assert plan.ok is False
    assert "op" in plan.missing


def test_plan_rejects_wrong_input_count():
    adapter = _fake_adapter()
    plan = adapter.plan("transcribe", {}, ["occ_1", "occ_2"])
    assert plan.ok is False
    assert "inputs" in plan.missing


def test_plan_rejects_unknown_model():
    adapter = _fake_adapter()
    plan = adapter.plan("transcribe", {"model": "nope-9000"}, ["occ_1"])
    assert plan.ok is False
    assert "params" in plan.missing


def test_plan_estimates_cost_from_given_duration():
    adapter = _fake_adapter()
    plan = adapter.plan("transcribe", {"model": "base", "duration_seconds": 100.0}, ["occ_1"])
    assert plan.ok is True
    assert plan.estimated_cost["duration_seconds"] == 100.0
    assert plan.estimated_cost["estimated_seconds"] > 0


def test_plan_without_duration_reports_unknown_cost():
    adapter = _fake_adapter()
    plan = adapter.plan("transcribe", {}, ["occ_1"])
    assert plan.estimated_cost == {"seconds": "unknown"}


# ═══════════════════════════════════════════════════════════════════════
# submit()/status()/collect() lifecycle with the fake engine
# ═══════════════════════════════════════════════════════════════════════

def test_submit_without_staged_input_is_rejected_before_queue():
    adapter = _fake_adapter()
    plan = adapter.plan("transcribe", {}, ["occ_missing"])
    staging = port.Staging(owner=OWNER, project_id="p1", workdir="/tmp", input_paths={})
    result = adapter.submit(plan, staging)
    assert result.state == "rejected_before_queue"
    assert result.reason == "input_not_staged"


def test_full_lifecycle_completes_with_words_and_confidence(tmp_path):
    wav = _synthetic_wav(str(tmp_path / "in.wav"))
    adapter = _fake_adapter()
    plan = adapter.plan("transcribe", {"align_words": True}, ["occ_1"])
    assert plan.ok is True
    result = adapter.submit(plan, _staging("occ_1", wav, workdir=str(tmp_path)))
    assert result.state == "accepted"

    assert _wait_until(lambda: adapter.status(result.job_id).state == "completed")

    collected = adapter.collect(result.job_id, str(tmp_path / "collected"))
    assert collected.ok is True
    assert len(collected.outputs) == 1
    output = collected.outputs[0]
    assert output.valid is True
    assert len(output.sha256) == 64

    import json
    payload = json.loads(open(output.path, encoding="utf-8").read())
    assert payload["language"] == "en"
    assert len(payload["segments"]) == 2
    first = payload["segments"][0]
    assert first["text"] == "hello there"
    assert len(first["words"]) == 2
    assert first["words"][0]["confidence"] == 0.95
    # the un-aligned word (SUB01) keeps no fabricated timestamp/precision:
    second = payload["segments"][1]
    unaligned = second["words"][1]
    assert unaligned["start"] == 1.5 and unaligned["end"] is None
    # Wait — FakeWord stores raw None; adapter passes it through as None.
    assert unaligned["confidence"] is None


def test_collect_before_completion_refuses(tmp_path):
    wav = _synthetic_wav(str(tmp_path / "in.wav"))
    block = threading.Event()
    adapter = _fake_adapter(block_event=block)
    plan = adapter.plan("transcribe", {}, ["occ_1"])
    result = adapter.submit(plan, _staging("occ_1", wav, workdir=str(tmp_path)))
    try:
        assert _wait_until(lambda: adapter.status(result.job_id).state == "running")
        collected = adapter.collect(result.job_id, str(tmp_path / "collected"))
        assert collected.ok is False
    finally:
        block.set()


# ═══════════════════════════════════════════════════════════════════════
# cancel() — queued (future.cancel succeeds) vs. running (requested)
# ═══════════════════════════════════════════════════════════════════════

def test_cancel_unknown_job_is_unknown():
    adapter = _fake_adapter()
    assert adapter.cancel("whisper_nope").outcome == "unknown"


def test_cancel_after_completion_is_too_late(tmp_path):
    wav = _synthetic_wav(str(tmp_path / "in.wav"))
    adapter = _fake_adapter()
    plan = adapter.plan("transcribe", {}, ["occ_1"])
    result = adapter.submit(plan, _staging("occ_1", wav, workdir=str(tmp_path)))
    assert _wait_until(lambda: adapter.status(result.job_id).state == "completed")
    outcome = adapter.cancel(result.job_id)
    assert outcome.outcome == "too_late"


def test_cancel_mid_run_stops_before_completion(tmp_path):
    """Closing criterion: "cancel" — a cancel while the worker is genuinely
    running (blocked mid-engine-call) is honoured before the next segment
    is processed, and never overwritten by a late result."""
    wav = _synthetic_wav(str(tmp_path / "in.wav"))
    engine_block = threading.Event()
    adapter = _fake_adapter(block_event=engine_block)
    plan = adapter.plan("transcribe", {}, ["occ_1"])
    result = adapter.submit(plan, _staging("occ_1", wav, workdir=str(tmp_path)))

    assert _wait_until(lambda: adapter.status(result.job_id).state == "running")
    outcome = adapter.cancel(result.job_id)
    assert outcome.outcome == "requested"

    engine_block.set()  # let the fake engine's generator proceed
    assert _wait_until(lambda: adapter.status(result.job_id).state == "cancelled")
    assert adapter.status(result.job_id).state != "completed"


# ═══════════════════════════════════════════════════════════════════════
# queue of 1 — a second submit stays queued while the first is running
# ═══════════════════════════════════════════════════════════════════════

def test_queue_of_one_serializes_two_jobs(tmp_path, monkeypatch):
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: 1 if key == "creator_asr_max_concurrent" else default)

    wav = _synthetic_wav(str(tmp_path / "in.wav"))
    first_block = threading.Event()
    model1 = FakeModel(block_event=first_block)
    model2 = FakeModel()
    calls = {"n": 0}

    def _loader(model_size, device):
        calls["n"] += 1
        return model1 if calls["n"] == 1 else model2

    adapter = WhisperAdapter(model_loader=_loader,
                             engine_available=lambda: (True, "", "fake-1.0"))

    plan1 = adapter.plan("transcribe", {}, ["occ_1"])
    job1 = adapter.submit(plan1, _staging("occ_1", wav, workdir=str(tmp_path / "w1")))
    assert job1.state == "accepted"
    assert _wait_until(lambda: adapter.status(job1.job_id).state == "running")

    plan2 = adapter.plan("transcribe", {}, ["occ_2"])
    job2 = adapter.submit(plan2, _staging("occ_2", wav, workdir=str(tmp_path / "w2")))
    assert job2.state == "accepted"

    # The second job MUST NOT be allowed to run while the first occupies
    # the pool's only worker — this is the closing criterion itself.
    time.sleep(0.1)
    assert adapter.status(job2.job_id).state == "queued"

    first_block.set()
    assert _wait_until(lambda: adapter.status(job1.job_id).state == "completed")
    assert _wait_until(lambda: adapter.status(job2.job_id).state == "completed")


# ═══════════════════════════════════════════════════════════════════════
# src.creator.asr — full orchestration into a `transcript` document
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture()
def project_env(tmp_path, monkeypatch, own_database, own_artifact_store, own_document_store):
    from services.projects import ProjectStore
    import services.projects as projects_mod
    ps = ProjectStore(str(tmp_path / "projects"))
    monkeypatch.setattr(projects_mod, "_store", ps)
    project = ps.create("ASR test project", owner=OWNER, scaffold_memory=False)
    return project["id"]


def test_asr_submit_rejects_when_engine_unavailable(project_env, tmp_path):
    """No monkeypatching of the adapter here: this sandbox genuinely has no
    faster-whisper installed, so `asr.submit` must fail with a clear,
    catchable error rather than queuing a job that can never finish."""
    if HAS_FASTER_WHISPER:
        pytest.skip("faster-whisper IS installed in this environment")
    wav = _synthetic_wav(str(tmp_path / "in.wav"))
    occ_id = _make_audio_occurrence(OWNER, "occ_asr_unavail", project_env, wav)
    with pytest.raises(asr_mod.AsrError):
        asr_mod.submit(OWNER, occ_id)


def test_asr_submit_rejects_non_audio_occurrence(project_env, tmp_path, monkeypatch):
    from src import artifact_store
    sha, size, filename, _ = artifact_store.publish_copy(
        __file__, artifact_store.ARTIFACT_STORE_DIR, "not_audio.py")
    ensure_blob(sha256=sha, byte_size=size, filename=filename, media_type="text/plain")
    occ = ArtifactOccurrence.parse({"id": "occ_notaudio", "kind": "document", "blob_sha256": sha,
                                    "owner": OWNER, "project_id": project_env})
    record_occurrence(occ)
    with pytest.raises(asr_mod.AsrError):
        asr_mod.submit(OWNER, "occ_notaudio")


def test_asr_submit_unknown_occurrence_is_not_found(project_env):
    with pytest.raises(asr_mod.AsrJobNotFound):
        asr_mod.submit(OWNER, "occ_does_not_exist")


def test_asr_full_pipeline_builds_valid_transcript_document(project_env, tmp_path, monkeypatch):
    wav = _synthetic_wav(str(tmp_path / "in.wav"), seconds=2.0)
    occ_id = _make_audio_occurrence(OWNER, "occ_asr_ok", project_env, wav)

    fake_adapter = _fake_adapter()
    monkeypatch.setattr(asr_mod, "_adapter", lambda: fake_adapter)

    job = asr_mod.submit(OWNER, occ_id, align_words=True)
    assert job["state"] in ("queued", "running", "completed")

    assert _wait_until(lambda: asr_mod.get_job(OWNER, job["job_id"])["state"] == "completed")
    final = asr_mod.get_job(OWNER, job["job_id"])
    assert final["document_id"]

    from src.creator import store as store_mod
    doc = store_mod.get_store().get(OWNER, final["document_id"])
    assert doc is not None
    assert doc.kind == "transcript"
    assert doc.content["source_asset_ref"] == occ_id
    assert doc.content["sample_rate"] == asr_mod.SAMPLE_RATE
    assert doc.content["alignment_status"] == "partial"
    assert occ_id in doc.asset_refs

    # Re-validate against the SAME structural validator every other Creator
    # document kind goes through — not just "we built something".
    documents_mod.validate_content("transcript", doc.content)

    cues = doc.content["cues"]
    assert len(cues) == 2
    assert cues[0]["text"] == "hello there"
    assert cues[0]["speaker_id"] is None  # SUB02: never fabricated
    assert cues[0]["editorial_status"] == "proposed"

    # Every word lands strictly inside its own cue's sample span.
    for cue in cues:
        cue_start, cue_end = int(cue["start_sample"]), int(cue["end_sample"])
        for word in cue["words"]:
            if word["start_sample"] is None or word["end_sample"] is None:
                continue  # SUB01: an unaligned word stays unaligned, not checked here
            w_start, w_end = int(word["start_sample"]), int(word["end_sample"])
            assert cue_start <= w_start <= cue_end
            assert cue_start <= w_end <= cue_end

    # The un-aligned word from the fake engine ("kenobi", end=None) kept
    # its null precision rather than an invented end_sample/confidence.
    second_cue_words = cues[1]["words"]
    assert any(w["end_sample"] is None and w["confidence"] is None for w in second_cue_words)


def test_asr_align_words_false_produces_no_words_and_not_aligned(project_env, tmp_path, monkeypatch):
    wav = _synthetic_wav(str(tmp_path / "in.wav"))
    occ_id = _make_audio_occurrence(OWNER, "occ_asr_noalign", project_env, wav)
    fake_adapter = _fake_adapter()
    monkeypatch.setattr(asr_mod, "_adapter", lambda: fake_adapter)

    job = asr_mod.submit(OWNER, occ_id, align_words=False)
    assert _wait_until(lambda: asr_mod.get_job(OWNER, job["job_id"])["state"] == "completed")
    final = asr_mod.get_job(OWNER, job["job_id"])

    from src.creator import store as store_mod
    doc = store_mod.get_store().get(OWNER, final["document_id"])
    assert doc.content["alignment_status"] == "not_aligned"
    for cue in doc.content["cues"]:
        assert cue["words"] == []


def test_asr_diarize_requested_is_never_fabricated(project_env, tmp_path, monkeypatch):
    """SUB02's own acceptance criterion: a speaker_1 never becomes an
    identified person without an explicit datum — here, without a real
    diarization backend, it just never becomes speaker_1 at all."""
    wav = _synthetic_wav(str(tmp_path / "in.wav"))
    occ_id = _make_audio_occurrence(OWNER, "occ_asr_diarize", project_env, wav)
    fake_adapter = _fake_adapter()
    monkeypatch.setattr(asr_mod, "_adapter", lambda: fake_adapter)

    job = asr_mod.submit(OWNER, occ_id, diarize=True)
    assert job["diarize"] is True
    assert job["diarization_applied"] is False
    assert job["limitations"]

    assert _wait_until(lambda: asr_mod.get_job(OWNER, job["job_id"])["state"] == "completed")
    final = asr_mod.get_job(OWNER, job["job_id"])
    from src.creator import store as store_mod
    doc = store_mod.get_store().get(OWNER, final["document_id"])
    assert all(cue["speaker_id"] is None for cue in doc.content["cues"])


def test_asr_cancel_before_completion(project_env, tmp_path, monkeypatch):
    wav = _synthetic_wav(str(tmp_path / "in.wav"))
    occ_id = _make_audio_occurrence(OWNER, "occ_asr_cancel", project_env, wav)
    block = threading.Event()
    fake_adapter = _fake_adapter(block_event=block)
    monkeypatch.setattr(asr_mod, "_adapter", lambda: fake_adapter)

    job = asr_mod.submit(OWNER, occ_id)
    assert _wait_until(lambda: asr_mod.get_job(OWNER, job["job_id"])["state"] == "running")

    outcome = asr_mod.cancel(OWNER, job["job_id"])
    assert outcome["outcome"] in ("requested", "accepted")

    block.set()
    assert _wait_until(lambda: asr_mod.get_job(OWNER, job["job_id"])["state"] == "cancelled")
    final = asr_mod.get_job(OWNER, job["job_id"])
    assert final["document_id"] is None


def test_asr_cancel_unknown_job_not_found(project_env):
    with pytest.raises(asr_mod.AsrJobNotFound):
        asr_mod.cancel(OWNER, "asr_does_not_exist")


def test_asr_get_job_foreign_owner_is_none(project_env, tmp_path, monkeypatch):
    wav = _synthetic_wav(str(tmp_path / "in.wav"))
    occ_id = _make_audio_occurrence(OWNER, "occ_asr_owner", project_env, wav)
    fake_adapter = _fake_adapter()
    monkeypatch.setattr(asr_mod, "_adapter", lambda: fake_adapter)
    job = asr_mod.submit(OWNER, occ_id)
    assert asr_mod.get_job("mallory", job["job_id"]) is None


# ═══════════════════════════════════════════════════════════════════════
# routes — flag gate, submit, get
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture()
def route_client(tmp_path, monkeypatch, project_env):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: True if key == "creator_enabled" else default)

    fake_adapter = _fake_adapter()
    monkeypatch.setattr(asr_mod, "_adapter", lambda: fake_adapter)

    from routes.creator_asr_routes import setup_creator_asr_routes
    app = FastAPI()
    app.include_router(setup_creator_asr_routes())
    client = TestClient(app)
    return client, project_env


def test_route_flag_off_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "creator_enabled" else default)
    from routes.creator_asr_routes import setup_creator_asr_routes
    app = FastAPI()
    app.include_router(setup_creator_asr_routes())
    client = TestClient(app)
    assert client.post("/api/creator/asr", json={"occurrence_id": "occ_x"}).status_code == 404
    assert client.get("/api/creator/asr/job_x").status_code == 404


def test_route_submit_requires_occurrence_id(route_client):
    client, _project_id = route_client
    resp = client.post("/api/creator/asr", json={})
    assert resp.status_code == 400


def test_route_submit_unknown_occurrence_is_404(route_client):
    client, _project_id = route_client
    resp = client.post("/api/creator/asr", json={"occurrence_id": "occ_nope"})
    assert resp.status_code == 404


def test_route_submit_and_get_full_flow(route_client, tmp_path):
    client, project_id = route_client
    wav = _synthetic_wav(str(tmp_path / "in.wav"))
    occ_id = _make_audio_occurrence("__odysseus_local__", "occ_route_asr", project_id, wav)

    resp = client.post("/api/creator/asr", json={"occurrence_id": occ_id})
    assert resp.status_code == 200, resp.text
    job = resp.json()
    assert job["state"] in ("queued", "running", "completed")

    def _completed():
        r = client.get(f"/api/creator/asr/{job['job_id']}")
        return r.status_code == 200 and r.json()["state"] == "completed"

    assert _wait_until(_completed)
    final = client.get(f"/api/creator/asr/{job['job_id']}").json()
    assert final["document_id"]


def test_route_get_unknown_job_is_404(route_client):
    client, _project_id = route_client
    assert client.get("/api/creator/asr/asr_does_not_exist").status_code == 404


# ═══════════════════════════════════════════════════════════════════════
# real engine — import-only probe, no inference, no weights
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not HAS_FASTER_WHISPER, reason="faster-whisper is not installed")
def test_real_engine_describe_reports_available_without_loading_a_model():
    adapter = WhisperAdapter()
    manifest = adapter.describe()
    assert manifest.available is True
    assert manifest.version  # a real installed-package version string
