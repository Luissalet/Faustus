"""Lote 70a, punto A.9 — `routes/session_routes.py::export_sessions_batch`
must run each member through `output_oracle.verify_artifact` exactly as
`export_session` (the single-conversation route) already does — a batch
member whose bytes do not reopen as their declared format must degrade to
an `<name>.error.txt` note like any other per-chat renderer failure, not get
zipped as if it succeeded.

Same harness as `tests/test_session_export_routes.py`
(`_make_export_double`/`_install_export_double`/`harness`), reused rather
than duplicated.
"""
from __future__ import annotations

from datetime import datetime

from tests.test_session_export_routes import (
    MEDIA,
    _Result,
    _add_session,
    _zip_of,
    harness,  # noqa: F401 - reused fixture
)


def test_a_batch_member_that_fails_verification_becomes_an_error_txt(monkeypatch, harness):
    ok = _add_session(harness, name="Good", folder="Work",
                      when=datetime(2026, 3, 3, 8, 0))
    corrupt = _add_session(harness, name="Corrupt", folder="Work",
                           when=datetime(2026, 3, 2, 8, 0))

    def render(transcript, fmt, filename=""):
        if transcript.session_id == corrupt:
            # Not a %PDF- signature — the exact shape output_oracle rejects.
            return _Result(b"not actually a pdf", MEDIA[fmt], f"{transcript.name}.{fmt}")
        return _Result(b"%PDF-1.4\nreal content", MEDIA[fmt], f"{transcript.name}.{fmt}")

    monkeypatch.setattr(harness.export, "render", render)
    zf = _zip_of(harness.client.get("/api/sessions/export",
                                    params={"fmt": "pdf", "folder": "Work"}))
    names = set(zf.namelist())
    assert {"Good.pdf", "Corrupt.error.txt", "index.md"} == names

    body = zf.read("Corrupt.error.txt").decode("utf-8")
    assert "verification failed" in body
    assert "%PDF-" in body

    index = zf.read("index.md").decode("utf-8")
    assert "Could not be exported" in index
    assert "Corrupt.error.txt" in index
    # The verification-failed chat is not listed as a successful export.
    table = index.split("## Could not be exported")[0]
    assert "Corrupt.error.txt" not in table


def test_an_empty_batch_member_also_fails_verification(monkeypatch, harness):
    """The generic (non-DOCX/PDF) path — empty content is rejected for every
    format, matching `export_session`'s own "export vacío" coverage."""
    _add_session(harness, name="Empty", folder="Work")

    def render(transcript, fmt, filename=""):
        return _Result(b"", MEDIA[fmt], f"{transcript.name}.{fmt}")

    monkeypatch.setattr(harness.export, "render", render)
    zf = _zip_of(harness.client.get("/api/sessions/export",
                                    params={"fmt": "md", "folder": "Work"}))
    names = set(zf.namelist())
    assert names == {"Empty.error.txt", "index.md"}
    body = zf.read("Empty.error.txt").decode("utf-8")
    assert "verification failed" in body
