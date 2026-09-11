"""VER-04 · a document export must reopen/validate before a link is served.

`src/output_oracle.py::verify_artifact` extends the oracle from "exit 0 with
the declared string missing" to "a renderer returned bytes without raising,
but the file itself is corrupt or empty" — a DOCX that is not a valid zip, a
PDF missing its signature, or any format that came back with zero bytes must
never reach the caller looking like a successful download.

`routes/session_routes.py::export_session` calls it after rendering and
before building the Response: a failed verdict becomes a 500 (no link), a
passing one adds an `X-Export-Verification` header naming the
generated/saved/opens/reviewed/verified stages the file actually cleared.

These pin both halves: the oracle function in isolation, and the route
wiring against a `chat_export` double (same harness as
`tests/test_session_export_routes.py` / `tests/test_l36_session_export_
artifact_manifest.py`) that can be made to return corrupt or empty bytes —
something the real renderers in `src/chat_export_docx.py`/`chat_export_pdf.py`
never do, so the failure path needs a double to exercise at all.
"""
from __future__ import annotations

import io
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import output_oracle as oracle

from tests.test_session_export_routes import (
    MEDIA,
    _Harness,
    _add_session,
    _install_export_double,
    _temp_db,
)


# ---------------------------------------------------------------------------
# output_oracle.verify_artifact — pure function
# ---------------------------------------------------------------------------

def _real_docx_bytes() -> bytes:
    docx = pytest.importorskip("docx")
    buf = io.BytesIO()
    document = docx.Document()
    document.add_paragraph("Hello from a real export")
    document.save(buf)
    return buf.getvalue()


def test_a_real_docx_passes_every_stage():
    verdict = oracle.verify_artifact("docx", _real_docx_bytes())
    assert verdict.ok is True
    assert verdict.stage == "verified"
    assert verdict.stages == oracle.ARTIFACT_STAGES


def test_a_corrupt_docx_fails_before_verified():
    """The literal stimulus: bytes that look plausible but are not a valid
    zip at all — the classic 'download cut off mid-transfer' shape."""
    verdict = oracle.verify_artifact("docx", b"PK\x03\x04not a real zip stream")
    assert verdict.ok is False
    assert verdict.stage == "saved"
    assert "zip" in verdict.reason.lower()


def test_a_zip_without_document_xml_is_not_a_docx():
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "not a word document")
    verdict = oracle.verify_artifact("docx", buf.getvalue())
    assert verdict.ok is False
    assert "document.xml" in verdict.reason


def test_a_docx_with_an_empty_document_xml_fails_reviewed():
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "")
    verdict = oracle.verify_artifact("docx", buf.getvalue())
    assert verdict.ok is False
    assert "empty" in verdict.reason


def test_a_valid_pdf_header_passes():
    verdict = oracle.verify_artifact("pdf", b"%PDF-1.7\n%mock content\n%%EOF")
    assert verdict.ok is True
    assert verdict.stage == "verified"


def test_a_pdf_without_the_signature_fails():
    verdict = oracle.verify_artifact("pdf", b"not actually a pdf at all")
    assert verdict.ok is False
    assert "%PDF-" in verdict.reason


@pytest.mark.parametrize("fmt", ["md", "txt", "html", "json", ""])
def test_an_empty_export_fails_at_the_generated_stage_for_every_format(fmt):
    verdict = oracle.verify_artifact(fmt, b"")
    assert verdict.ok is False
    assert verdict.stage == "generated"
    assert verdict.stages == ("generated",)


def test_a_whitespace_only_text_export_is_treated_as_empty():
    verdict = oracle.verify_artifact("md", b"   \n\t  \n")
    assert verdict.ok is False
    assert verdict.stage == "saved"


def test_a_real_markdown_export_passes():
    verdict = oracle.verify_artifact("md", "# Roadmap\n\nSome content.".encode("utf-8"))
    assert verdict.ok is True
    assert verdict.stage == "verified"


def test_none_content_is_handled_like_empty_not_a_crash():
    verdict = oracle.verify_artifact("docx", None)  # type: ignore[arg-type]
    assert verdict.ok is False
    assert verdict.stage == "generated"


# ---------------------------------------------------------------------------
# route wiring: export_session must not serve a link for a failed verdict
# ---------------------------------------------------------------------------

@dataclass
class _Result:
    content: bytes
    media_type: str
    filename: str


class _Unavailable(RuntimeError):
    pass


def _double_returning(fmt_to_bytes: dict) -> "SimpleNamespace":
    import types

    mod = types.ModuleType("src.chat_export")
    mod.SUPPORTED_FORMATS = tuple(fmt_to_bytes.keys())
    mod.ExportUnavailable = _Unavailable
    mod.calls = []

    def build_transcript(session):
        mod.calls.append(("build_transcript", getattr(session, "id", None)))
        return SimpleNamespace(name=getattr(session, "name", ""),
                               session_id=getattr(session, "id", ""))

    def render(transcript, fmt, filename=""):
        mod.calls.append(("render", transcript.session_id, fmt, filename))
        return _Result(content=fmt_to_bytes[fmt], media_type=MEDIA.get(fmt, "application/octet-stream"),
                      filename=filename or f"{transcript.name}.{fmt}")

    mod.build_transcript = build_transcript
    mod.render = render
    return mod


@pytest.fixture
def harness(monkeypatch, tmp_path):
    import routes.session_routes as sr

    factory = _temp_db(tmp_path)
    monkeypatch.setattr(sr, "SessionLocal", factory)
    monkeypatch.setattr(sr, "effective_user", lambda request: "alice")

    store = {}

    def get_session(sid):
        if sid not in store:
            raise KeyError(sid)
        return store[sid]

    sm = MagicMock()
    sm.sessions = store
    sm.get_session.side_effect = get_session

    router = sr.setup_session_routes(sm, {})
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield _Harness(client, sm, factory, None)


def test_a_corrupt_docx_export_is_a_500_not_a_download(monkeypatch, harness):
    double = _double_returning({"docx": b"this is not a zip file at all"})
    _install_export_double(monkeypatch, double)
    sid = _add_session(harness, name="Roadmap")

    r = harness.client.get(f"/api/session/{sid}/export", params={"fmt": "docx"})

    assert r.status_code == 500, r.text
    body = r.json()["detail"]
    assert body["stage"] in ("generated", "saved")
    assert "stages" in body
    assert "content-disposition" not in {k.lower() for k in r.headers}


def test_an_empty_export_is_a_500_not_a_download(monkeypatch, harness):
    double = _double_returning({"md": b""})
    _install_export_double(monkeypatch, double)
    sid = _add_session(harness, name="Roadmap")

    r = harness.client.get(f"/api/session/{sid}/export", params={"fmt": "md"})

    assert r.status_code == 500, r.text
    assert r.json()["detail"]["stage"] == "generated"


def test_a_valid_export_carries_the_verification_stages_header(monkeypatch, harness):
    double = _double_returning({"md": b"# Roadmap\n\nreal content"})
    _install_export_double(monkeypatch, double)
    sid = _add_session(harness, name="Roadmap")

    r = harness.client.get(f"/api/session/{sid}/export", params={"fmt": "md"})

    assert r.status_code == 200, r.text
    assert r.headers["X-Export-Verification"] == ",".join(oracle.ARTIFACT_STAGES)
    assert r.content == b"# Roadmap\n\nreal content"
