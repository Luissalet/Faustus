"""Meeting-notes pipeline tests (FEATURE B) — chunking, cleaned transcript,
Markdown sections, file + sidecar persistence, and the model-failure
fallback, all against a fake STT service and a fake notes model (no real
faster-whisper or LLM endpoint needed)."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import meetings


@pytest.fixture(autouse=True)
def _redirect_meetings_dir(tmp_path, monkeypatch):
    meetings_dir = tmp_path / "meetings"
    monkeypatch.setattr(meetings, "MEETINGS_DIR", str(meetings_dir))
    monkeypatch.setattr(meetings, "JOBS_FILE", str(meetings_dir / "jobs.json"))
    monkeypatch.setattr(meetings, "_TMP_DIR", str(meetings_dir / "_jobs"))
    return meetings_dir


class FakeSTT:
    """Returns two clean, pre-timestamped segments per chunk — as if
    stt_cleanup had already run inside transcribe_segments (it has, for the
    real service; this fake skips straight to the cleaned result since
    src/stt_cleanup.py is unit-tested on its own)."""

    available = True

    def __init__(self, per_chunk):
        self._per_chunk = per_chunk
        self.calls = []

    def _load_settings(self):
        return {"stt_provider": "local"}

    def transcribe_segments(self, audio_bytes, *, language="", metadata=None):
        self.calls.append(audio_bytes)
        if metadata is not None:
            metadata["cleanup_stats"] = {"removed_hallucination": 1, "removed_duplicate": 1}
        idx = len(self.calls) - 1
        return self._per_chunk[idx]


def _fake_chunks(job_dir: str, count: int, chunk_seconds: int = 300):
    paths = []
    for i in range(count):
        p = Path(job_dir) / f"fake_chunk_{i}.wav"
        p.write_bytes(b"RIFF....WAVEfmt ")
        paths.append({"path": str(p), "offset": i * chunk_seconds})
    return paths


def test_pipeline_chunks_transcribes_cleans_and_saves_markdown(tmp_path, monkeypatch):
    job_dir = tmp_path / "job1"
    job_dir.mkdir()

    per_chunk = [
        [{"start": 0.0, "end": 3.0, "text": "Buenos días, empecemos la reunión."}],
        [{"start": 2.0, "end": 5.0, "text": "Decidimos lanzar el producto el lunes."}],
    ]
    fake_stt = FakeSTT(per_chunk)
    monkeypatch.setattr(meetings, "_split_chunks", lambda src, wd, secs: _fake_chunks(str(job_dir), 2, chunk_seconds=300))
    monkeypatch.setattr("services.stt.stt_service.get_stt_service", lambda: fake_stt)
    monkeypatch.setattr(
        meetings,
        "_generate_notes_sync",
        lambda transcript_text, *, owner, **_kw: (
            "## Summary\n\nThe team agreed to ship the product on Monday.\n\n"
            "## Decisions\n\n- Ship on Monday\n\n"
            "## Action items\n\n- Prepare release notes (owner: Ana, due: Monday)\n\n"
            "## Open questions\n\n- None recorded."
        ),
    )

    src_path = job_dir / "source.wav"
    src_path.write_bytes(b"RIFF....WAVEfmt ")

    meetings._update_job("job-1", owner="alice", status="queued")
    meetings._run_job(
        "job-1", str(src_path), str(job_dir), "team-sync.wav", "Team Sync",
        "es", None, "alice",
    )

    job = meetings.get_job("job-1", owner="alice")
    assert job["status"] == "done"
    meeting_id = job["meeting_id"]
    assert meeting_id

    meeting = meetings.get_meeting(meeting_id, owner="alice")
    assert meeting is not None
    md = meeting["markdown"]

    # Sections present.
    for heading in ("## Summary", "## Decisions", "## Action items", "## Open questions", "## Transcript"):
        assert heading in md

    # Cleaned transcript content, with a second chunk's timestamp offset by
    # its chunk start (2.0 + 300 = 302s -> 05:02).
    assert "Buenos días, empecemos la reunión." in md
    assert "Decidimos lanzar el producto el lunes." in md
    assert "[00:00]" in md
    assert "[05:02]" in md

    # Sidecar persisted with expected metadata.
    sidecar = meeting
    assert sidecar["owner"] == "alice"
    assert sidecar["model_ok"] is True
    assert sidecar["chunk_count"] == 2
    assert sidecar["cleanup_stats"]["removed_hallucination"] == 2

    # Files actually on disk.
    md_files = list(Path(meetings.MEETINGS_DIR).glob("*.md"))
    json_files = list(Path(meetings.MEETINGS_DIR).glob("*.json"))
    assert len(md_files) == 1
    assert any(p.name != "jobs.json" for p in json_files)

    # Job temp dir cleaned up.
    assert not job_dir.exists()

    # Both chunks were actually sent to STT.
    assert len(fake_stt.calls) == 2


def test_pipeline_model_failure_saves_transcript_only_with_warning(tmp_path, monkeypatch):
    job_dir = tmp_path / "job2"
    job_dir.mkdir()

    per_chunk = [[{"start": 0.0, "end": 2.0, "text": "Repasamos el presupuesto anual."}]]
    fake_stt = FakeSTT(per_chunk)
    monkeypatch.setattr(meetings, "_split_chunks", lambda src, wd, secs: _fake_chunks(str(job_dir), 1))
    monkeypatch.setattr("services.stt.stt_service.get_stt_service", lambda: fake_stt)

    def _boom(transcript_text, *, owner, **_kw):
        raise RuntimeError("no endpoint reachable")

    monkeypatch.setattr(meetings, "_generate_notes_sync", _boom)

    src_path = job_dir / "source.wav"
    src_path.write_bytes(b"RIFF....WAVEfmt ")

    meetings._update_job("job-2", owner="bob", status="queued")
    meetings._run_job("job-2", str(src_path), str(job_dir), "budget.wav", "", "", None, "bob")

    job = meetings.get_job("job-2", owner="bob")
    assert job["status"] == "done"
    meeting = meetings.get_meeting(job["meeting_id"], owner="bob")

    assert meeting["model_ok"] is False
    assert any("unavailable" in w.lower() or "failed" in w.lower() for w in meeting["warnings"])
    assert "Repasamos el presupuesto anual." in meeting["markdown"]
    assert "could not be generated automatically" in meeting["markdown"].lower()


def test_pipeline_stt_unavailable_marks_job_failed(tmp_path, monkeypatch):
    job_dir = tmp_path / "job3"
    job_dir.mkdir()

    class Unavailable:
        available = False

    monkeypatch.setattr("services.stt.stt_service.get_stt_service", lambda: Unavailable())

    src_path = job_dir / "source.wav"
    src_path.write_bytes(b"RIFF....WAVEfmt ")

    meetings._update_job("job-3", owner="carol", status="queued")
    meetings._run_job("job-3", str(src_path), str(job_dir), "x.wav", "", "", None, "carol")
    job = meetings.get_job("job-3", owner="carol")
    assert job["status"] == "failed"
    assert "STT" in job["error"]


def test_slugify_and_meeting_id_uniqueness(tmp_path, monkeypatch):
    assert meetings.slugify("Reunión de Ventas!") == "reunion-de-ventas"
    assert meetings.slugify("") == "meeting"


def test_list_meetings_is_owner_scoped(tmp_path, monkeypatch):
    job_dir = tmp_path / "job4"
    job_dir.mkdir()
    fake_stt = FakeSTT([[{"start": 0.0, "end": 1.0, "text": "Hola equipo."}]])
    monkeypatch.setattr(meetings, "_split_chunks", lambda src, wd, secs: _fake_chunks(str(job_dir), 1))
    monkeypatch.setattr("services.stt.stt_service.get_stt_service", lambda: fake_stt)
    monkeypatch.setattr(meetings, "_generate_notes_sync", lambda t, *, owner, **_kw: "## Summary\n\nHi.\n\n## Decisions\n\nNone recorded.\n\n## Action items\n\nNone recorded.\n\n## Open questions\n\nNone recorded.")

    src_path = job_dir / "source.wav"
    src_path.write_bytes(b"RIFF....WAVEfmt ")
    meetings._update_job("job-4", owner="alice", status="queued")
    meetings._run_job("job-4", str(src_path), str(job_dir), "standup.wav", "Standup", "", None, "alice")

    assert len(meetings.list_meetings(owner="alice")) == 1
    assert len(meetings.list_meetings(owner="bob")) == 0


def test_notes_prompt_follows_the_meeting_language():
    from src import meetings as m
    es = "Buenos días, empezamos la reunión de la versión dos y decidimos que el lanzamiento es el martes para todos los equipos de la empresa con las notas de la versión"
    assert m._guess_language(es) == "es"
    assert "Spanish" in m._notes_prompt("es")
    assert m._notes_prompt("") == m._NOTES_SYSTEM_PROMPT
