"""WP18 — casting and TTS: `src/creator/adapters/tts.py`, `src/creator/
consent.py`, `src/creator/voices.py`, `routes/creator_voice_routes.py`.

A deterministic FAKE synthesis engine throughout (CONTRATO.md rule 8/11: "no
afirmar éxito de motores por un mock; no ejecutar modelos") — no kokoro/
piper/edge-tts/chatterbox package is ever imported by these tests. The one
exception is `test_real_engine_describe_reports_absent` at the bottom, which
only checks `describe()`'s honest probe in an environment with none of
these installed, never a real synthesis.
"""
from __future__ import annotations

import io
import time
import types
import wave
from typing import Any, Dict, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src.creator import adapter_port as port
from src.creator import consent as consent_mod
from src.creator import voices as voices_mod
from src.creator.adapters import tts as tts_mod
from src.creator.adapters.tts import TtsAdapter

OWNER = "alice"


# ── fake synthesis ──────────────────────────────────────────────────────

def _synthetic_wav_bytes(*, seconds: float = 0.2, sample_rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    n_frames = int(seconds * sample_rate)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_frames)
    return buf.getvalue()


def _fake_synth(engine: str = "kokoro"):
    calls = []

    def _fn(text: str, raw_voice: str, language: str, speed: float) -> Tuple[bytes, str]:
        calls.append((text, raw_voice, language, speed))
        return _synthetic_wav_bytes(), "audio/wav"

    return _fn, calls


def _unavailable_synth(reason: str = "fake: engine unavailable"):
    def _fn(text, raw_voice, language, speed):
        raise tts_mod.EngineUnavailable(reason)
    return _fn


def _fake_adapter(*, engines=("kokoro", "system", "piper", "edge_tts", "chatterbox")) -> TtsAdapter:
    fn, _calls = _fake_synth()
    synth = {name: fn for name in engines}
    probe = lambda engine: {"installed": engine in engines, "detail": "" if engine in engines else "fake: not installed"}
    return TtsAdapter(synth=synth, probe=probe)


def _staging(*, owner=OWNER, project_id="p1", workdir="/tmp", input_paths=None) -> port.Staging:
    return port.Staging(owner=owner, project_id=project_id, workdir=workdir, input_paths=input_paths or {})


@pytest.fixture(autouse=True)
def _reset_tts_module():
    tts_mod.reset_for_tests()
    yield
    tts_mod.reset_for_tests()


# ═══════════════════════════════════════════════════════════════════════
# adapter: describe() / plan() — pure, no engine ever touched
# ═══════════════════════════════════════════════════════════════════════

def test_describe_without_any_engine_reports_unavailable():
    adapter = TtsAdapter(synth={}, probe=lambda engine: {"installed": False, "detail": "fake: none"})
    manifest = adapter.describe()
    assert manifest.available is False
    assert "engines" in manifest.limits
    assert "voices" in manifest.limits


def test_describe_reports_per_engine_probe_without_loading_anything():
    adapter = _fake_adapter(engines=("kokoro",))
    manifest = adapter.describe()
    assert manifest.available is True
    assert manifest.limits["engines"]["kokoro"]["installed"] is True
    assert manifest.limits["engines"]["piper"]["installed"] is False


def test_chatterbox_turbo_en_is_never_multilingual():
    """AUD10: 'Turbo inglés' is never presented as multilingual."""
    turbo = tts_mod.VOICE_BY_ID["chatterbox:turbo-en"]
    assert turbo.languages == ("en",)
    assert turbo.multilingual is False
    multi = tts_mod.VOICE_BY_ID["chatterbox:multilingual"]
    assert multi.multilingual is True
    assert len(multi.languages) > 1


def test_plan_rejects_unknown_voice():
    adapter = _fake_adapter()
    plan = adapter.plan("tts", {"voice_id": "nope:nope", "text": "hi"}, [])
    assert plan.ok is False
    assert "voice_id" in plan.missing


def test_plan_rejects_language_the_voice_does_not_support():
    adapter = _fake_adapter()
    plan = adapter.plan("tts", {"voice_id": "kokoro:af_heart", "text": "hola", "language": "es"}, [])
    assert plan.ok is False
    assert "language" in plan.missing


def test_plan_rejects_engine_not_installed():
    adapter = _fake_adapter(engines=("kokoro",))
    plan = adapter.plan("tts", {"voice_id": "piper:en_US-amy-medium", "text": "hi"}, [])
    assert plan.ok is False
    assert "engine" in plan.missing


def test_plan_drops_unsupported_tags_and_reports_them():
    """AUD02: an unsupported tag is escaped, never spoken literally."""
    adapter = _fake_adapter()
    plan = adapter.plan("tts", {"voice_id": "kokoro:af_heart",
                                "text": "hello [pause:500ms] world [laughs]"}, [])
    assert plan.ok is True
    assert plan.engine_plan["text"] == "hello world"
    assert "[pause:500ms]" in plan.engine_plan["dropped_tags"]
    assert "[laughs]" in plan.engine_plan["dropped_tags"]
    assert "dropped unsupported tag" in plan.detail


def test_plan_rejects_text_that_is_only_unsupported_tags():
    adapter = _fake_adapter()
    plan = adapter.plan("tts", {"voice_id": "kokoro:af_heart", "text": "[pause:1s]"}, [])
    assert plan.ok is False
    assert "text" in plan.missing


def test_plan_rejects_cloning_op_on_non_cloning_voice():
    adapter = _fake_adapter()
    plan = adapter.plan("voice_clone", {"voice_id": "kokoro:af_heart", "text": "hi",
                                        "consent_subject": "Jane"}, ["occ_ref"])
    assert plan.ok is False
    assert "voice_id" in plan.missing


def test_plan_rejects_cloning_op_without_consent_subject():
    adapter = _fake_adapter()
    plan = adapter.plan("voice_clone", {"voice_id": "chatterbox:turbo-en", "text": "hi"}, ["occ_ref"])
    assert plan.ok is False
    assert "consent_subject" in plan.missing


def test_plan_rejects_cloning_op_without_reference_input():
    adapter = _fake_adapter()
    plan = adapter.plan("voice_clone", {"voice_id": "chatterbox:turbo-en", "text": "hi",
                                        "consent_subject": "Jane"}, [])
    assert plan.ok is False
    assert "inputs" in plan.missing


# ═══════════════════════════════════════════════════════════════════════
# adapter: submit() / status() / collect() — synchronous, fake engine
# ═══════════════════════════════════════════════════════════════════════

def test_submit_synthesizes_and_completes_synchronously():
    fn, calls = _fake_synth()
    adapter = TtsAdapter(synth={"kokoro": fn}, probe=lambda e: {"installed": e == "kokoro", "detail": ""})
    plan = adapter.plan("tts", {"voice_id": "kokoro:af_heart", "text": "hello there"}, [])
    assert plan.ok is True
    result = adapter.submit(plan, _staging())
    assert result.state == "accepted"
    assert adapter.status(result.job_id).state == "completed"
    assert calls and calls[0][0] == "hello there"


def test_collect_returns_valid_hashed_wav(tmp_path):
    adapter = _fake_adapter()
    plan = adapter.plan("tts", {"voice_id": "kokoro:af_heart", "text": "hello"}, [])
    result = adapter.submit(plan, _staging())
    collected = adapter.collect(result.job_id, str(tmp_path / "out"))
    assert collected.ok is True
    output = collected.outputs[0]
    assert output.valid is True
    assert len(output.sha256) == 64
    assert output.media_type == "audio/wav"


def test_submit_with_unavailable_synth_fn_is_rejected_before_queue():
    adapter = TtsAdapter(synth={"kokoro": _unavailable_synth("fake: no CUDA")},
                         probe=lambda e: {"installed": True, "detail": ""})
    plan = adapter.plan("tts", {"voice_id": "kokoro:af_heart", "text": "hello"}, [])
    result = adapter.submit(plan, _staging())
    assert result.state == "rejected_before_queue"
    assert "no CUDA" in result.detail


def test_cancel_after_completion_is_too_late():
    adapter = _fake_adapter()
    plan = adapter.plan("tts", {"voice_id": "kokoro:af_heart", "text": "hi"}, [])
    result = adapter.submit(plan, _staging())
    outcome = adapter.cancel(result.job_id)
    assert outcome.outcome == "too_late"


def test_cancel_unknown_job_is_unknown():
    adapter = _fake_adapter()
    assert adapter.cancel("tts_nope").outcome == "unknown"


# ═══════════════════════════════════════════════════════════════════════
# consent.py — owner-scoped, scoped, expiring, revocable
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture()
def own_consent_db(tmp_path, monkeypatch):
    monkeypatch.setattr(consent_mod, "default_path", lambda: str(tmp_path / "consent.db"))
    return consent_mod


def test_register_requires_owner_subject_grantor(own_consent_db):
    assert consent_mod.register("", "Jane", granted_by="bob")["ok"] is False
    assert consent_mod.register(OWNER, "", granted_by="bob")["ok"] is False
    assert consent_mod.register(OWNER, "Jane", granted_by="")["ok"] is False


def test_register_and_is_valid(own_consent_db):
    result = consent_mod.register(OWNER, "Jane Doe", granted_by="bob", scope="clone")
    assert result["ok"] is True
    assert consent_mod.is_valid(OWNER, "Jane Doe", scope="clone") is True
    # a different owner never sees it — owner scoping (CONTRATO.md rule 3)
    assert consent_mod.is_valid("mallory", "Jane Doe", scope="clone") is False
    # a different scope is a different question
    assert consent_mod.is_valid(OWNER, "Jane Doe", scope="dub") is False


def test_revoke_blocks_future_validity(own_consent_db):
    result = consent_mod.register(OWNER, "Jane Doe", granted_by="bob")
    consent_id = result["consent"]["id"]
    assert consent_mod.is_valid(OWNER, "Jane Doe") is True
    revoked = consent_mod.revoke(OWNER, consent_id)
    assert revoked["ok"] is True
    assert consent_mod.is_valid(OWNER, "Jane Doe") is False


def test_revoke_is_idempotent(own_consent_db):
    result = consent_mod.register(OWNER, "Jane Doe", granted_by="bob")
    consent_id = result["consent"]["id"]
    consent_mod.revoke(OWNER, consent_id)
    second = consent_mod.revoke(OWNER, consent_id)
    assert second["ok"] is True
    assert second["idempotent"] is True


def test_revoke_foreign_or_unknown_id_not_found(own_consent_db):
    result = consent_mod.register(OWNER, "Jane Doe", granted_by="bob")
    consent_id = result["consent"]["id"]
    assert consent_mod.revoke("mallory", consent_id)["reason"] == "not_found"
    assert consent_mod.revoke(OWNER, "consent_does_not_exist")["reason"] == "not_found"


def test_expiry_in_the_past_is_rejected_at_register(own_consent_db):
    result = consent_mod.register(OWNER, "Jane Doe", granted_by="bob", expires_at=time.time() - 10)
    assert result["ok"] is False
    assert result["reason"] == "expiry_in_past"


def test_expired_consent_is_not_valid_even_though_never_revoked(own_consent_db, monkeypatch):
    """AUD10: 'un permiso vencido se comprueba antes de ejecutar, aunque el
    preflight anterior fuera válido' — is_valid() checks expiry live."""
    # The permit is registered with a real, generous window and then the
    # clock is moved past it, which is what "expired" means for a permit
    # nobody revoked. Sleeping through a 50ms window instead made the first
    # assertion a race -- registering writes to sqlite, and on a loaded
    # machine the window was already gone when it ran. (Registering one
    # that is ALREADY expired is not an option: `register` refuses those,
    # which the test above covers.)
    expires_at = time.time() + 3600
    consent_mod.register(OWNER, "Jane Doe", granted_by="bob", expires_at=expires_at)
    assert consent_mod.is_valid(OWNER, "Jane Doe") is True

    monkeypatch.setattr(consent_mod, "time",
                        types.SimpleNamespace(time=lambda: expires_at + 1))
    assert consent_mod.is_valid(OWNER, "Jane Doe") is False, (
        "expiry is checked live, not trusted from an earlier preflight")


def test_register_bridges_into_media_consent(own_consent_db, tmp_path, monkeypatch):
    import src.media_consent as media_consent
    monkeypatch.setattr(media_consent, "CONSENT_FILE", str(tmp_path / "media_consent.json"))
    result = consent_mod.register(OWNER, "Jane Doe", granted_by="bob")
    assert result["bridged_to_media_consent"] is True
    # preflight's own gate (WP09) reads exactly this function:
    assert media_consent.has_consent("Jane Doe") is True


def test_revoke_bridges_the_revoke_into_media_consent(own_consent_db, tmp_path, monkeypatch):
    import src.media_consent as media_consent
    monkeypatch.setattr(media_consent, "CONSENT_FILE", str(tmp_path / "media_consent.json"))
    result = consent_mod.register(OWNER, "Jane Doe", granted_by="bob")
    consent_mod.revoke(OWNER, result["consent"]["id"])
    assert media_consent.has_consent("Jane Doe") is False


# ═══════════════════════════════════════════════════════════════════════
# adapter: voice_clone/dub gated on live consent, checked at submit()
# ═══════════════════════════════════════════════════════════════════════

def test_submit_voice_clone_without_consent_is_rejected(own_consent_db):
    fn, _calls = _fake_synth()
    adapter = TtsAdapter(synth={"chatterbox": fn}, probe=lambda e: {"installed": True, "detail": ""})
    plan = adapter.plan("voice_clone", {"voice_id": "chatterbox:turbo-en", "text": "hi",
                                        "consent_subject": "Jane Doe"}, ["occ_ref"])
    assert plan.ok is True  # plan alone does not require an owner hint to pass structurally
    staging = _staging(input_paths={"occ_ref": "/tmp/ref.wav"})
    result = adapter.submit(plan, staging)
    assert result.state == "rejected_before_queue"
    assert result.reason == "consent_missing_or_revoked_or_expired"


def test_submit_voice_clone_with_live_consent_succeeds(own_consent_db):
    consent_mod.register(OWNER, "Jane Doe", granted_by="bob", scope="clone")
    fn, calls = _fake_synth()
    adapter = TtsAdapter(synth={"chatterbox": fn}, probe=lambda e: {"installed": True, "detail": ""})
    plan = adapter.plan("voice_clone", {"voice_id": "chatterbox:turbo-en", "text": "hi",
                                        "consent_subject": "Jane Doe"}, ["occ_ref"])
    staging = _staging(input_paths={"occ_ref": "/tmp/ref.wav"})
    result = adapter.submit(plan, staging)
    assert result.state == "accepted"
    assert calls


def test_submit_voice_clone_after_revoke_is_rejected(own_consent_db):
    """AUD10: revoking blocks a job not yet authorized to run."""
    granted = consent_mod.register(OWNER, "Jane Doe", granted_by="bob", scope="clone")
    fn, _calls = _fake_synth()
    adapter = TtsAdapter(synth={"chatterbox": fn}, probe=lambda e: {"installed": True, "detail": ""})
    plan = adapter.plan("voice_clone", {"voice_id": "chatterbox:turbo-en", "text": "hi",
                                        "consent_subject": "Jane Doe"}, ["occ_ref"])
    consent_mod.revoke(OWNER, granted["consent"]["id"])
    staging = _staging(input_paths={"occ_ref": "/tmp/ref.wav"})
    result = adapter.submit(plan, staging)
    assert result.state == "rejected_before_queue"
    assert result.reason == "consent_missing_or_revoked_or_expired"


# ═══════════════════════════════════════════════════════════════════════
# voices.py — catalog / casting / audition / synthesize_transcript
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "creator_wp18.db").as_posix()
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


@pytest.fixture()
def project_env(tmp_path, monkeypatch, own_database, own_artifact_store, own_document_store, own_consent_db):
    from services.projects import ProjectStore
    import services.projects as projects_mod
    ps = ProjectStore(str(tmp_path / "projects"))
    monkeypatch.setattr(projects_mod, "_store", ps)
    project = ps.create("Voices test project", owner=OWNER, scaffold_memory=False)
    return project["id"]


@pytest.fixture()
def fake_tts_adapter(monkeypatch):
    adapter = _fake_adapter()
    monkeypatch.setattr(voices_mod, "_adapter", lambda: adapter)
    return adapter


def _make_audio_occurrence(owner: str, occurrence_id: str, project_id: str) -> str:
    from src import artifact_store
    from src.artifact_identity import ensure_blob, record_occurrence
    from src.contracts.blob import ArtifactOccurrence
    import wave as wave_mod

    path = str(artifact_store.ARTIFACT_STORE_DIR)
    import os
    os.makedirs(path, exist_ok=True)
    src_path = os.path.join(path, "_src.wav")
    with wave_mod.open(src_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x00\x00" * 8000)
    sha, size, filename, _ = artifact_store.publish_copy(src_path, path, "src.wav")
    ensure_blob(sha256=sha, byte_size=size, filename=filename, media_type="audio/wav")
    occ = ArtifactOccurrence.parse({"id": occurrence_id, "kind": "audio", "blob_sha256": sha,
                                    "owner": owner, "project_id": project_id, "label": "src.wav"})
    record_occurrence(occ)
    return occurrence_id


def _make_transcript_doc(owner: str, project_id: str, source_ref: str):
    from src.creator import store as store_mod
    content = {
        "source_asset_ref": source_ref, "sample_rate": 16000, "alignment_status": "partial",
        "cues": [
            {"id": "cue_0", "text": "hello there", "language": "en", "speaker_id": "spk_a",
             "start_sample": "0", "end_sample": "1600", "words": [], "editorial_status": "proposed"},
            {"id": "cue_1", "text": "general kenobi", "language": "en", "speaker_id": "spk_b",
             "start_sample": "1600", "end_sample": "3200", "words": [], "editorial_status": "proposed"},
        ],
    }
    return store_mod.get_store().create(owner, project_id, "transcript", content, asset_refs=[source_ref])


def test_list_voices_filters_by_language():
    catalog = voices_mod.list_voices(language="es")
    ids = {v["id"] for v in catalog["voices"]}
    assert "piper:es_ES-mls_10246-low" in ids
    assert "kokoro:af_heart" not in ids  # english-only, correctly excluded


def test_set_casting_unknown_voice_rejected(project_env):
    doc = _make_transcript_doc(OWNER, project_env, "occ_src")
    with pytest.raises(voices_mod.VoiceError):
        voices_mod.set_casting(OWNER, project_env, doc.id, {"spk_a": "nope:nope"})


def test_set_casting_persists_and_reports_invalidated_cues(project_env):
    doc = _make_transcript_doc(OWNER, project_env, "occ_src")
    first = voices_mod.set_casting(OWNER, project_env, doc.id, {"spk_a": "kokoro:af_heart"})
    assert first["casting"] == {"spk_a": "kokoro:af_heart"}
    assert first["invalidated_cue_ids"] == ["cue_0"]

    # Changing casting for spk_a only invalidates spk_a's cue, per AUD01.
    second = voices_mod.set_casting(OWNER, project_env, doc.id, {"spk_a": "kokoro:am_adam"})
    assert second["invalidated_cue_ids"] == ["cue_0"]
    assert "cue_1" not in second["invalidated_cue_ids"]

    stored = voices_mod.get_casting(OWNER, project_env, doc.id)
    assert stored["spk_a"] == "kokoro:am_adam"


def test_audition_registers_an_occurrence(project_env, fake_tts_adapter):
    result = voices_mod.audition(OWNER, project_env, "kokoro:af_heart", "hello there")
    assert result["ok"] is True
    assert result["run"]["artifacts"]
    assert result["run"]["artifacts"][0]["kind"] == "audio"


def test_synthesize_transcript_produces_derived_occurrences_with_manifest(project_env, fake_tts_adapter):
    occ_id = _make_audio_occurrence(OWNER, "occ_src_1", project_env)
    doc = _make_transcript_doc(OWNER, project_env, occ_id)
    voices_mod.set_casting(OWNER, project_env, doc.id, {"spk_a": "kokoro:af_heart", "spk_b": "kokoro:am_adam"})

    result = voices_mod.synthesize_transcript(OWNER, project_env, doc.id)
    assert len(result["results"]) == 2
    assert all(r["ok"] for r in result["results"])
    assert len(result["manifest"]) == 2
    for entry in result["manifest"]:
        assert entry["occurrence_id"]

    from src.artifact_identity import for_owner
    occ = for_owner(result["manifest"][0]["occurrence_id"], owner=OWNER)
    assert occ_id in occ.relations.derived_from


def test_synthesize_transcript_replace_original_never_mutates_silently(project_env, fake_tts_adapter):
    occ_id = _make_audio_occurrence(OWNER, "occ_src_2", project_env)
    doc = _make_transcript_doc(OWNER, project_env, occ_id)
    voices_mod.set_casting(OWNER, project_env, doc.id, {"spk_a": "kokoro:af_heart", "spk_b": "kokoro:am_adam"})

    result = voices_mod.synthesize_transcript(OWNER, project_env, doc.id, replace_original=True)
    assert result["replaced_original"] is False
    assert result["limitations"]


def test_synthesize_transcript_only_regenerates_requested_cue_ids(project_env, fake_tts_adapter):
    occ_id = _make_audio_occurrence(OWNER, "occ_src_3", project_env)
    doc = _make_transcript_doc(OWNER, project_env, occ_id)
    voices_mod.set_casting(OWNER, project_env, doc.id, {"spk_a": "kokoro:af_heart", "spk_b": "kokoro:am_adam"})

    result = voices_mod.synthesize_transcript(OWNER, project_env, doc.id, cue_ids=["cue_0"])
    assert len(result["results"]) == 1
    assert result["results"][0]["cue_id"] == "cue_0"


def test_synthesize_transcript_uncast_speaker_without_default_is_reported(project_env, fake_tts_adapter):
    occ_id = _make_audio_occurrence(OWNER, "occ_src_4", project_env)
    doc = _make_transcript_doc(OWNER, project_env, occ_id)
    # only spk_a cast; spk_b left uncast, no default given
    voices_mod.set_casting(OWNER, project_env, doc.id, {"spk_a": "kokoro:af_heart"})

    result = voices_mod.synthesize_transcript(OWNER, project_env, doc.id)
    by_cue = {r["cue_id"]: r for r in result["results"]}
    assert by_cue["cue_0"]["ok"] is True
    assert by_cue["cue_1"]["ok"] is False
    assert by_cue["cue_1"]["reason"] == "no_voice_assigned"


# ═══════════════════════════════════════════════════════════════════════
# routes — flag gate, casting, consent, synthesize
# ═══════════════════════════════════════════════════════════════════════

ROUTE_OWNER = "__odysseus_local__"  # src.owner_identity.DEFAULT_LOCAL_OWNER, with AUTH_ENABLED=false


@pytest.fixture()
def route_client(tmp_path, monkeypatch, own_database, own_artifact_store, own_document_store,
                 own_consent_db, fake_tts_adapter):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: True if key == "creator_enabled" else default)

    from services.projects import ProjectStore
    import services.projects as projects_mod
    ps = ProjectStore(str(tmp_path / "projects"))
    monkeypatch.setattr(projects_mod, "_store", ps)
    project = ps.create("Voices route test project", owner=ROUTE_OWNER, scaffold_memory=False)

    from routes.creator_voice_routes import setup_creator_voice_routes, setup_creator_consent_routes
    app = FastAPI()
    app.include_router(setup_creator_voice_routes())
    app.include_router(setup_creator_consent_routes())
    client = TestClient(app)
    return client, project["id"]


def test_route_flag_off_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "creator_enabled" else default)
    from routes.creator_voice_routes import setup_creator_voice_routes, setup_creator_consent_routes
    app = FastAPI()
    app.include_router(setup_creator_voice_routes())
    app.include_router(setup_creator_consent_routes())
    client = TestClient(app)
    assert client.get("/api/creator/voices").status_code == 404
    assert client.post("/api/creator/voices/audition", json={}).status_code == 404
    assert client.post("/api/creator/consent", json={}).status_code == 404


def test_route_list_voices(route_client):
    client, _project_id = route_client
    resp = client.get("/api/creator/voices")
    assert resp.status_code == 200
    assert resp.json()["voices"]


def test_route_casting_roundtrip(route_client):
    client, project_id = route_client
    doc = _make_transcript_doc(ROUTE_OWNER, project_id, "occ_x")
    resp = client.post("/api/creator/voices/casting", json={
        "project_id": project_id, "doc_id": doc.id, "assignments": {"spk_a": "kokoro:af_heart"},
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["casting"]["spk_a"] == "kokoro:af_heart"

    resp2 = client.get("/api/creator/voices/casting", params={"project_id": project_id, "doc_id": doc.id})
    assert resp2.status_code == 200
    assert resp2.json()["casting"]["spk_a"] == "kokoro:af_heart"


def test_route_consent_register_and_revoke(route_client):
    client, _project_id = route_client
    resp = client.post("/api/creator/consent", json={"subject": "Jane Doe", "scope": "clone"})
    assert resp.status_code == 200, resp.text
    consent_id = resp.json()["consent"]["id"]

    listed = client.get("/api/creator/consent")
    assert any(c["id"] == consent_id for c in listed.json()["consent"])

    revoked = client.delete(f"/api/creator/consent/{consent_id}")
    assert revoked.status_code == 200
    assert revoked.json()["reason"] == "revoked"

    again = client.delete(f"/api/creator/consent/{consent_id}")
    assert again.status_code == 200
    assert again.json()["idempotent"] is True


def test_route_consent_revoke_unknown_is_404(route_client):
    client, _project_id = route_client
    resp = client.delete("/api/creator/consent/consent_does_not_exist")
    assert resp.status_code == 404


def test_route_synthesize_full_flow(route_client):
    client, project_id = route_client
    occ_id = _make_audio_occurrence(ROUTE_OWNER, "occ_route_src", project_id)
    doc = _make_transcript_doc(ROUTE_OWNER, project_id, occ_id)
    client.post("/api/creator/voices/casting", json={
        "project_id": project_id, "doc_id": doc.id,
        "assignments": {"spk_a": "kokoro:af_heart", "spk_b": "kokoro:am_adam"},
    })

    resp = client.post("/api/creator/voices/synthesize", json={"project_id": project_id, "doc_id": doc.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) == 2
    assert all(r["ok"] for r in body["results"])


def test_route_synthesize_unknown_doc_is_400(route_client):
    client, project_id = route_client
    resp = client.post("/api/creator/voices/synthesize",
                       json={"project_id": project_id, "doc_id": "doc_does_not_exist"})
    assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════
# real engines — import-only probe, no inference, no weights
# ═══════════════════════════════════════════════════════════════════════

def test_real_engine_describe_reports_absent_in_this_environment():
    """This sandbox has no kokoro/piper/edge_tts/chatterbox installed —
    exercised for real (no fake) so a false 'available' never slips by."""
    for engine in ("kokoro", "piper", "edge_tts", "chatterbox"):
        probed = tts_mod.probe_engine(engine)
        if probed["installed"]:
            pytest.skip(f"{engine} IS installed in this environment")
        assert probed["installed"] is False
        assert probed["detail"]

    adapter = TtsAdapter()
    manifest = adapter.describe()
    # 'system' can be True only on Windows, which this sandbox is not.
    assert manifest.limits["engines"]["system"]["installed"] is False
