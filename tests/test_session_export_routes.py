"""Contract tests for the rewritten chat-export routes.

``routes/session_routes.py`` no longer builds md/txt/json/html by hand; it
delegates to ``src/chat_export.py``. These tests therefore drive the routes
against a *double* of that module (installed into ``sys.modules``), so they
pin the route's own responsibilities — format validation, ownership, the
Content-Disposition header, the batch zip and its ceilings — without waiting
on the renderers.

Fixture note: ``setup_session_routes()`` appends to the MODULE-level
``sr.router``. Duplicate paths pile up across test modules that call it, and
the first match wins — so a fixture that merely appends can end up serving a
*previous* module's session manager (``tests/test_session_list_owner_scope.py``
leaves exactly that behind). Every fixture here empties ``sr.router.routes``
before setup and restores the snapshot on teardown, so these tests neither
inherit nor cause that contamination.
"""
import io
import sys
import types
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import Session as DbSession


# ---------------------------------------------------------------------------
# The chat_export double
# ---------------------------------------------------------------------------

SUPPORTED = ("md", "txt", "json", "html", "pdf", "docx")

MEDIA = {
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "json": "application/json",
    "html": "text/html; charset=utf-8",
    "pdf": "application/pdf",
    "docx": ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document"),
}


@dataclass
class _Result:
    content: bytes
    media_type: str
    filename: str


class _Unavailable(RuntimeError):
    pass


def _make_export_double(**overrides):
    """A stand-in for ``src.chat_export`` with the published interface."""
    mod = types.ModuleType("src.chat_export")
    mod.SUPPORTED_FORMATS = SUPPORTED
    mod.ExportUnavailable = _Unavailable
    mod.calls = []

    def build_transcript(session):
        mod.calls.append(("build_transcript", getattr(session, "id", None)))
        return SimpleNamespace(
            name=getattr(session, "name", ""),
            model=getattr(session, "model", ""),
            session_id=getattr(session, "id", ""),
            messages=list(getattr(session, "history", []) or []),
        )

    def render(transcript, fmt, filename=""):
        mod.calls.append(("render", transcript.session_id, fmt, filename))
        name = filename or f"{transcript.name or 'conversation'}.{fmt}"
        return _Result(
            content=f"<{fmt}:{transcript.name}>".encode("utf-8"),
            media_type=MEDIA[fmt],
            filename=name,
        )

    mod.build_transcript = overrides.get("build_transcript", build_transcript)
    mod.render = overrides.get("render", render)
    return mod


def _install_export_double(monkeypatch, mod):
    import src as src_pkg
    monkeypatch.setitem(sys.modules, "src.chat_export", mod)
    monkeypatch.setattr(src_pkg, "chat_export", mod, raising=False)
    return mod


# ---------------------------------------------------------------------------
# App / DB harness
# ---------------------------------------------------------------------------

def _temp_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'export.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class _Harness:
    def __init__(self, client, sm, db_factory, export):
        self.client = client
        self.sm = sm
        self.db_factory = db_factory
        self.export = export


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Session routes over a temp DB, a stub session manager and an export
    double, with the module router restored afterwards."""
    import routes.session_routes as sr

    # setup_session_routes() APPENDS to the module-level router, and sibling
    # test modules call it without cleaning up (see the module docstring).
    # Start from an empty route list so this app gets exactly one copy of each
    # route — bound to the stubs below, not to a previous module's session
    # manager — and hand the module back exactly as it was found.
    saved_routes = list(sr.router.routes)
    sr.router.routes[:] = []
    factory = _temp_db(tmp_path)
    monkeypatch.setattr(sr, "SessionLocal", factory)
    monkeypatch.setattr(sr, "effective_user", lambda request: "alice")

    export = _install_export_double(monkeypatch, _make_export_double())

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
    try:
        with TestClient(app) as client:
            yield _Harness(client, sm, factory, export)
    finally:
        sr.router.routes[:] = saved_routes


def _add_session(harness, *, name, owner="alice", model="gpt-4o", folder=None,
                 messages=3, sid=None, when=None):
    sid = sid or str(uuid.uuid4())
    when = when or datetime(2026, 5, 4, 9, 30)
    db = harness.db_factory()
    try:
        db.add(DbSession(
            id=sid, owner=owner, name=name, endpoint_url="http://localhost",
            model=model, archived=False, folder=folder, message_count=messages,
            created_at=when, updated_at=when, last_message_at=when,
        ))
        db.commit()
    finally:
        db.close()
    harness.sm.sessions[sid] = SimpleNamespace(
        id=sid, name=name, model=model, owner=owner, folder=folder,
        history=[SimpleNamespace(role="user", content="hi")] * messages,
    )
    return sid


# ---------------------------------------------------------------------------
# Single export
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", SUPPORTED)
def test_every_supported_format_is_served_with_its_media_type(harness, fmt):
    sid = _add_session(harness, name="Roadmap")
    r = harness.client.get(f"/api/session/{sid}/export", params={"fmt": fmt})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(MEDIA[fmt].split(";")[0])
    assert r.content == f"<{fmt}:Roadmap>".encode("utf-8")
    assert r.headers["content-disposition"].startswith("attachment;")
    assert f"Roadmap.{fmt}" in r.headers["content-disposition"]


def test_pdf_and_docx_are_reachable_through_the_route(harness):
    """The two new formats are the point of the rewrite — pin them explicitly."""
    sid = _add_session(harness, name="Deck")
    for fmt in ("pdf", "docx"):
        r = harness.client.get(f"/api/session/{sid}/export", params={"fmt": fmt})
        assert r.status_code == 200
        assert ("render", sid, fmt, "") in harness.export.calls


def test_default_format_is_still_markdown(harness):
    sid = _add_session(harness, name="Roadmap")
    r = harness.client.get(f"/api/session/{sid}/export")
    assert r.status_code == 200
    assert ("render", sid, "md", "") in harness.export.calls


def test_unknown_format_is_a_400_listing_the_valid_ones(harness):
    """The old route fell through to markdown for anything it didn't know, so
    `?fmt=pdf` returned a .md file claiming to be the export you asked for."""
    sid = _add_session(harness, name="Roadmap")
    r = harness.client.get(f"/api/session/{sid}/export", params={"fmt": "epub"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "epub" in detail
    for fmt in SUPPORTED:
        assert fmt in detail
    # No renderer was ever asked to produce it.
    assert not any(c[0] == "render" for c in harness.export.calls)


def test_export_unavailable_is_a_503_with_the_message_verbatim(monkeypatch, harness):
    sid = _add_session(harness, name="Roadmap")
    msg = "PDF export needs the 'reportlab' package: pip install reportlab"

    def boom(transcript, fmt, filename=""):
        raise _Unavailable(msg)

    monkeypatch.setattr(harness.export, "render", boom)
    r = harness.client.get(f"/api/session/{sid}/export", params={"fmt": "pdf"})
    assert r.status_code == 503
    assert r.json()["detail"] == msg


def test_missing_session_is_404(harness):
    r = harness.client.get(f"/api/session/{uuid.uuid4()}/export")
    assert r.status_code == 404


def test_another_users_session_is_404_like_its_sibling_routes(harness):
    """_verify_session_owner answers 404 (not 403) so the route can't be used
    to probe which session ids exist."""
    sid = _add_session(harness, name="Bob private", owner="bob")
    r = harness.client.get(f"/api/session/{sid}/export", params={"fmt": "md"})
    assert r.status_code == 404
    assert not any(c[0] == "render" for c in harness.export.calls)


def test_filename_parameter_is_sanitised_before_reaching_the_renderer(harness):
    sid = _add_session(harness, name="Roadmap")
    r = harness.client.get(
        f"/api/session/{sid}/export",
        params={"fmt": "md", "filename": "../../etc/passwd"},
    )
    assert r.status_code == 200
    passed = [c for c in harness.export.calls if c[0] == "render"][0][3]
    # No path separator survives, so the name can never escape a directory —
    # neither the renderer's nor the browser's.
    assert "/" not in passed and "\\" not in passed
    assert passed == ".._.._etc_passwd"
    assert "/" not in r.headers["content-disposition"]


# ---------------------------------------------------------------------------
# Content-Disposition encoding
# ---------------------------------------------------------------------------

def test_accented_and_spaced_filename_is_rfc5987_encoded(monkeypatch, harness):
    """A chat called "Informe 2026 — año" used to produce
    `attachment; filename=Informe 2026 — año.md`: unquoted (so browsers cut it
    at the first space) and non-latin-1 (so Starlette raised). Both halves must
    be present now: a quoted ASCII fallback and filename*=UTF-8''<pct>."""
    sid = _add_session(harness, name="Informe 2026 — año")
    r = harness.client.get(f"/api/session/{sid}/export", params={"fmt": "md"})
    assert r.status_code == 200

    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment; ")
    assert 'filename="Informe_2026___a_o.md"' in cd
    assert "filename*=UTF-8''" in cd

    encoded = cd.split("filename*=UTF-8''", 1)[1]
    from urllib.parse import unquote
    assert unquote(encoded) == "Informe 2026 — año.md"
    # The header must stay latin-1 encodable, which is what the ASGI layer
    # requires; a raw "ñ" here is what used to blow up.
    cd.encode("latin-1")


def test_header_cannot_be_injected_through_the_derived_name(monkeypatch, harness):
    def sneaky(transcript, fmt, filename=""):
        return _Result(b"x", "text/plain", 'a"\r\nX-Evil: 1.md')

    monkeypatch.setattr(harness.export, "render", sneaky)
    sid = _add_session(harness, name="Roadmap")
    r = harness.client.get(f"/api/session/{sid}/export", params={"fmt": "txt"})
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert "\r" not in cd and "\n" not in cd
    assert "x-evil" not in {k.lower() for k in r.headers}


# ---------------------------------------------------------------------------
# Batch export
# ---------------------------------------------------------------------------

def _zip_of(response):
    assert response.status_code == 200, response.text
    return zipfile.ZipFile(io.BytesIO(response.content))


def test_batch_by_folder_returns_a_zip_with_an_index(harness):
    _add_session(harness, name="Alpha", folder="Work", messages=4,
                 when=datetime(2026, 3, 2, 8, 0))
    _add_session(harness, name="Beta", folder="Work", messages=7,
                 when=datetime(2026, 3, 1, 8, 0))
    _add_session(harness, name="Elsewhere", folder="Other")

    r = harness.client.get("/api/sessions/export",
                           params={"fmt": "md", "folder": "Work"})
    zf = _zip_of(r)
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"].startswith("attachment;")

    names = set(zf.namelist())
    assert "index.md" in names
    assert {"Alpha.md", "Beta.md"} <= names
    assert not any(n.startswith("Elsewhere") for n in names)
    assert zf.read("Alpha.md") == b"<md:Alpha>"

    index = zf.read("index.md").decode("utf-8")
    for token in ("Alpha", "Beta", "gpt-4o", "2026-03-02", "| 4 |", "| 7 |",
                  "Alpha.md", "Beta.md"):
        assert token in index, token
    # zipfile itself must accept the archive.
    assert zf.testzip() is None


def test_batch_gives_duplicate_chat_names_unique_zip_entries(harness):
    for _ in range(3):
        _add_session(harness, name="Notes", folder="Work")
    zf = _zip_of(harness.client.get("/api/sessions/export",
                                    params={"fmt": "md", "folder": "Work"}))
    members = [n for n in zf.namelist() if n != "index.md"]
    assert len(members) == 3
    assert len(set(members)) == 3
    assert set(members) == {"Notes.md", "Notes-2.md", "Notes-3.md"}
    index = zf.read("index.md").decode("utf-8")
    for m in members:
        assert m in index


def test_batch_by_ids_only_takes_the_listed_chats(harness):
    a = _add_session(harness, name="A")
    b = _add_session(harness, name="B")
    _add_session(harness, name="C")
    zf = _zip_of(harness.client.get("/api/sessions/export",
                                    params={"fmt": "txt", "ids": f"{a},{b}"}))
    assert {n for n in zf.namelist() if n != "index.md"} == {"A.txt", "B.txt"}


def test_batch_never_leaves_the_requesting_users_sessions(harness):
    _add_session(harness, name="Mine", folder="Shared")
    bob = _add_session(harness, name="Bobs", owner="bob", folder="Shared")
    zf = _zip_of(harness.client.get("/api/sessions/export",
                                    params={"fmt": "md", "folder": "Shared"}))
    members = {n for n in zf.namelist() if n != "index.md"}
    assert members == {"Mine.md"}
    # And an explicit id request for someone else's chat gets nothing back.
    r = harness.client.get("/api/sessions/export",
                           params={"fmt": "md", "ids": bob})
    assert r.status_code == 404


def test_batch_by_project_uses_the_projects_folder(monkeypatch, harness):
    import services.projects as projects

    _add_session(harness, name="In project", folder="LocalAI")
    _add_session(harness, name="Not in project", folder="Misc")

    store = MagicMock()
    store.get.return_value = {"id": "p1", "name": "Local AI", "folder": "LocalAI"}
    monkeypatch.setattr(projects, "get_store", lambda: store)

    zf = _zip_of(harness.client.get("/api/sessions/export",
                                    params={"fmt": "md", "project": "p1"}))
    assert {n for n in zf.namelist() if n != "index.md"} == {"In project.md"}
    store.get.assert_called_once_with("p1", "alice")


def test_unknown_project_is_404(monkeypatch, harness):
    import services.projects as projects
    store = MagicMock()
    store.get.return_value = None
    monkeypatch.setattr(projects, "get_store", lambda: store)
    r = harness.client.get("/api/sessions/export",
                           params={"fmt": "md", "project": "nope"})
    assert r.status_code == 404


def test_a_project_with_no_folder_does_not_export_every_chat(monkeypatch, harness):
    """A blank folder means "no membership rule". Falling through to an
    unfiltered query would hand back the user's entire history."""
    import services.projects as projects
    _add_session(harness, name="Unrelated", folder="Somewhere")
    _add_session(harness, name="Also unrelated", folder=None)

    store = MagicMock()
    store.get.return_value = {"id": "p1", "name": "Broken", "folder": ""}
    monkeypatch.setattr(projects, "get_store", lambda: store)

    r = harness.client.get("/api/sessions/export",
                           params={"fmt": "md", "project": "p1"})
    assert r.status_code == 400
    assert "folder" in r.json()["detail"]


def test_an_oversized_id_list_is_refused_before_the_query(monkeypatch, harness):
    import routes.session_routes as sr
    monkeypatch.setattr(sr, "EXPORT_BATCH_MAX_SESSIONS", 3)
    r = harness.client.get("/api/sessions/export",
                           params={"fmt": "md", "ids": "a,b,c,d,e"})
    assert r.status_code == 400
    assert "3" in r.json()["detail"]


def test_batch_without_a_selector_is_400(harness):
    r = harness.client.get("/api/sessions/export", params={"fmt": "md"})
    assert r.status_code == 400
    assert "project" in r.json()["detail"]


def test_batch_rejects_an_unknown_format_before_touching_the_db(harness):
    _add_session(harness, name="A", folder="Work")
    r = harness.client.get("/api/sessions/export",
                           params={"fmt": "epub", "folder": "Work"})
    assert r.status_code == 400
    assert "epub" in r.json()["detail"]


def test_batch_over_the_session_cap_is_a_clear_400(monkeypatch, harness):
    import routes.session_routes as sr
    monkeypatch.setattr(sr, "EXPORT_BATCH_MAX_SESSIONS", 2)
    for i in range(3):
        _add_session(harness, name=f"S{i}", folder="Work")
    r = harness.client.get("/api/sessions/export",
                           params={"fmt": "pdf", "folder": "Work"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "3" in detail and "2" in detail
    # Refused before a single renderer ran — that is the point of the cap.
    assert not any(c[0] == "render" for c in harness.export.calls)


def test_batch_over_the_byte_cap_is_a_clear_400(monkeypatch, harness):
    import routes.session_routes as sr
    monkeypatch.setattr(sr, "EXPORT_BATCH_MAX_BYTES", 1024)

    def fat(transcript, fmt, filename=""):
        return _Result(b"x" * 700, MEDIA[fmt], f"{transcript.name}.{fmt}")

    monkeypatch.setattr(harness.export, "render", fat)
    for i in range(4):
        _add_session(harness, name=f"S{i}", folder="Work")
    r = harness.client.get("/api/sessions/export",
                           params={"fmt": "pdf", "folder": "Work"})
    assert r.status_code == 400
    assert "limit" in r.json()["detail"].lower()


def test_one_broken_chat_becomes_an_error_txt_and_the_batch_continues(monkeypatch, harness):
    ok_a = _add_session(harness, name="Good A", folder="Work",
                        when=datetime(2026, 3, 3, 8, 0))
    bad = _add_session(harness, name="Broken", folder="Work",
                       when=datetime(2026, 3, 2, 8, 0))
    _add_session(harness, name="Good B", folder="Work",
                 when=datetime(2026, 3, 1, 8, 0))

    def flaky(transcript, fmt, filename=""):
        if transcript.session_id == bad:
            raise ValueError("table cell exploded")
        return _Result(b"ok", MEDIA[fmt], f"{transcript.name}.{fmt}")

    monkeypatch.setattr(harness.export, "render", flaky)
    zf = _zip_of(harness.client.get("/api/sessions/export",
                                    params={"fmt": "pdf", "folder": "Work"}))
    names = set(zf.namelist())
    assert {"Good A.pdf", "Good B.pdf", "Broken.error.txt", "index.md"} == names
    body = zf.read("Broken.error.txt").decode("utf-8")
    assert "table cell exploded" in body

    index = zf.read("index.md").decode("utf-8")
    assert "Could not be exported" in index
    assert "Broken.error.txt" in index
    assert "table cell exploded" in index
    # The failed chat is not listed as a successful export.
    table = index.split("## Could not be exported")[0]
    assert "Broken.error.txt" not in table


def test_missing_dependency_fails_the_whole_batch_with_503(monkeypatch, harness):
    """ExportUnavailable is global (no reportlab installed), so zipping N
    identical error notes would be worse than one actionable 503."""
    msg = "DOCX export needs the 'python-docx' package: pip install python-docx"

    def boom(transcript, fmt, filename=""):
        raise _Unavailable(msg)

    monkeypatch.setattr(harness.export, "render", boom)
    _add_session(harness, name="A", folder="Work")
    _add_session(harness, name="B", folder="Work")
    r = harness.client.get("/api/sessions/export",
                           params={"fmt": "docx", "folder": "Work"})
    assert r.status_code == 503
    assert r.json()["detail"] == msg


def test_batch_matching_nothing_is_404(harness):
    r = harness.client.get("/api/sessions/export",
                           params={"fmt": "md", "folder": "Empty"})
    assert r.status_code == 404


def test_batch_zip_name_is_encoded_too(harness):
    _add_session(harness, name="A", folder="Año récord")
    r = harness.client.get("/api/sessions/export",
                           params={"fmt": "md", "folder": "Año récord"})
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert 'filename="' in cd and "filename*=UTF-8''" in cd
    cd.encode("latin-1")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_unique_zip_name_suffixes_before_the_extension():
    from routes.session_routes import _unique_zip_name
    taken = set()
    assert _unique_zip_name("Chat.pdf", taken) == "Chat.pdf"
    assert _unique_zip_name("Chat.pdf", taken) == "Chat-2.pdf"
    assert _unique_zip_name("Chat.pdf", taken) == "Chat-3.pdf"
    assert _unique_zip_name("noext", taken) == "noext"
    assert _unique_zip_name("noext", taken) == "noext-2"


def test_export_download_name_keeps_unicode_but_drops_separators():
    from routes.session_routes import _export_download_name
    assert _export_download_name("año récord.md") == "año récord.md"
    assert _export_download_name("../../etc/passwd") == "_.._etc_passwd"
    assert "/" not in _export_download_name("a/b/c.md")
    assert "\\" not in _export_download_name("a\\b.md")
    assert _export_download_name("") == "export"
    assert _export_download_name(None) == "export"


# ---------------------------------------------------------------------------
# One pass over the real renderer, so the double above cannot drift away from
# the interface it stands in for (a wrong keyword or a renamed attribute would
# never show up in the mocked tests).
# ---------------------------------------------------------------------------

@pytest.fixture
def real_harness(monkeypatch, tmp_path):
    import routes.session_routes as sr
    pytest.importorskip("src.chat_export")

    # setup_session_routes() APPENDS to the module-level router, and sibling
    # test modules call it without cleaning up (see the module docstring).
    # Start from an empty route list so this app gets exactly one copy of each
    # route — bound to the stubs below, not to a previous module's session
    # manager — and hand the module back exactly as it was found.
    saved_routes = list(sr.router.routes)
    sr.router.routes[:] = []
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
    try:
        with TestClient(app) as client:
            yield _Harness(client, sm, factory, None)
    finally:
        sr.router.routes[:] = saved_routes


def _add_real_session(harness, name, folder=None):
    sid = _add_session(harness, name=name, folder=folder, messages=2)
    harness.sm.sessions[sid].history = [
        SimpleNamespace(role="user", content="Hola **mundo**", metadata={}),
        SimpleNamespace(role="assistant", content="| a | b |\n| --- | --- |\n| 1 | 2 |",
                        metadata={}),
    ]
    return sid


def test_real_renderer_round_trips_through_the_route(real_harness):
    """md/txt/json/html have no optional dependency, so these must always work."""
    sid = _add_real_session(real_harness, "Informe 2026 — año")
    for fmt in ("md", "txt", "json", "html"):
        r = real_harness.client.get(f"/api/session/{sid}/export", params={"fmt": fmt})
        assert r.status_code == 200, (fmt, r.text)
        assert r.content
        cd = r.headers["content-disposition"]
        assert cd.startswith('attachment; filename="') and "filename*=UTF-8''" in cd
        cd.encode("latin-1")


def test_real_renderer_batch_produces_a_readable_zip(real_harness):
    _add_real_session(real_harness, "Uno", folder="Trabajo")
    _add_real_session(real_harness, "Dos", folder="Trabajo")
    r = real_harness.client.get("/api/sessions/export",
                                params={"fmt": "md", "folder": "Trabajo"})
    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.testzip() is None
    assert "index.md" in zf.namelist()
    assert len(zf.namelist()) == 3
    index = zf.read("index.md").decode("utf-8")
    assert "Uno" in index and "Dos" in index


def test_real_binary_format_is_either_served_or_a_503(real_harness):
    """PDF/DOCX depend on optional packages. Whichever way this box is set up,
    the route must answer 200 or an actionable 503 — never a 500."""
    sid = _add_real_session(real_harness, "Deck")
    for fmt in ("pdf", "docx"):
        r = real_harness.client.get(f"/api/session/{sid}/export", params={"fmt": fmt})
        assert r.status_code in (200, 503), (fmt, r.status_code, r.text[:400])
        if r.status_code == 503:
            # The message must name what to install, not leak a traceback.
            assert r.json()["detail"].strip()
            assert "Traceback" not in r.json()["detail"]


def test_index_escapes_pipes_so_the_table_survives():
    from routes.session_routes import _build_export_index
    index = _build_export_index(
        [{"name": "a|b", "model": "m", "date": "d", "message_count": 1,
          "filename": "a_b.md"}], [], "md")
    assert "a\\|b" in index
