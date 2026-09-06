"""Resolver behaviour for project context links.

The tests that matter most here are the negative ones. A resolver is the only
place in this subsystem holding somebody else's row in a local variable, so the
question is never just "did it say no" but "did the no say anything". Several
of these assert on the *absence* of a title, a path and a size in the answer.
"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from src.project_context.models import SourceRef
from src.project_context.resolvers.artifact import ArtifactResolver
from src.project_context.resolvers.base import (
    get_resolver, register_resolver, reset_resolvers, resolvers,
)
from src.project_context.resolvers.document import DocumentResolver, chunk_markdown
from src.project_context.resolvers.filesystem import FilesystemResolver, has_traversal
from src.project_context.resolvers.gallery import GalleryImageResolver

OWNER = "luis"
OTHER = "mallory"
PROJECT = {"id": "prj_1", "name": "Faustus", "owner": OWNER, "enabled": True}

SECRET_TITLE = "Q3 restructuring memo"


@pytest.fixture
def db_factory(tmp_path):
    """A file-backed SQLite database with the real schema."""
    url = "sqlite:///" + (tmp_path / "resolvers.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False},
                           poolclass=NullPool)
    cdb.Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _document(db_factory, *, doc_id="doc_1", owner=OWNER, title="Voice architecture",
              content="# Voice\n\nDecisions.\n", versions=1):
    db = db_factory()
    try:
        db.add(cdb.Document(id=doc_id, title=title, current_content=content,
                            version_count=versions, owner=owner, language="markdown"))
        db.commit()
    finally:
        db.close()
    return doc_id


def _version(db_factory, doc_id, number, content):
    db = db_factory()
    try:
        db.add(cdb.DocumentVersion(id=f"{doc_id}-v{number}", document_id=doc_id,
                                   version_number=number, content=content))
        doc = db.query(cdb.Document).filter(cdb.Document.id == doc_id).first()
        doc.version_count = max(int(doc.version_count or 0), number)
        db.commit()
    finally:
        db.close()


# ── the registry ───────────────────────────────────────────────────────────

def test_registry_registers_replaces_and_resets():
    class Fake:
        kind = "document"

    original = dict(resolvers())
    try:
        fake = Fake()
        register_resolver(fake)
        assert get_resolver("document") is fake
        reset_resolvers({})
        assert get_resolver("document") is None
    finally:
        reset_resolvers(original)
    assert isinstance(get_resolver("document"), DocumentResolver)


def test_a_resolver_without_a_kind_is_refused():
    class Nameless:
        kind = ""

    with pytest.raises(ValueError):
        register_resolver(Nameless())


# ── document resolver ──────────────────────────────────────────────────────

def test_document_metadata_for_its_owner(db_factory):
    doc_id = _document(db_factory)
    resolver = DocumentResolver(db_factory)
    meta = resolver.metadata(SourceRef(kind="document", id=doc_id),
                             owner=OWNER, project=PROJECT)
    assert meta.state == "ok"
    assert meta.label == "Voice architecture"
    assert meta.owner == OWNER
    assert meta.canonical_ref == f"document:{doc_id}"
    assert meta.revision.startswith(f"document:{doc_id}:v1:")


def test_a_document_of_another_owner_is_forbidden_and_leaks_nothing(db_factory):
    """MANDATORY. A refusal that names the document has already leaked it."""
    doc_id = _document(db_factory, owner=OTHER, title=SECRET_TITLE,
                       content="Layoffs are planned for November.")
    resolver = DocumentResolver(db_factory)
    meta = resolver.metadata(SourceRef(kind="document", id=doc_id),
                             owner=OWNER, project=PROJECT)

    assert meta.state == "forbidden"
    rendered = repr(meta.to_dict())
    for leaked in (SECRET_TITLE, "Layoffs", doc_id):
        assert leaked not in rendered
    assert meta.label == ""
    assert meta.byte_size == 0
    assert meta.owner == ""
    assert meta.canonical_ref == ""
    assert meta.revision == ""


def test_a_missing_document_is_missing_not_forbidden(db_factory):
    resolver = DocumentResolver(db_factory)
    meta = resolver.metadata(SourceRef(kind="document", id="nope"),
                             owner=OWNER, project=PROJECT)
    assert meta.state == "missing"
    assert meta.label == ""


def test_ownership_is_the_owner_column_not_the_session(db_factory):
    """A deleted chat sets documents.session_id NULL; the document survives and
    still belongs to its owner."""
    doc_id = _document(db_factory)
    db = db_factory()
    try:
        doc = db.query(cdb.Document).filter(cdb.Document.id == doc_id).first()
        doc.session_id = None
        db.commit()
    finally:
        db.close()
    meta = DocumentResolver(db_factory).metadata(
        SourceRef(kind="document", id=doc_id), owner=OWNER, project=PROJECT)
    assert meta.state == "ok"


def test_latest_follows_a_new_version_and_pinned_keeps_the_old_one(db_factory):
    """MANDATORY. The two policies must be observably different."""
    doc_id = _document(db_factory, content="version one body", versions=1)
    _version(db_factory, doc_id, 1, "version one body")
    resolver = DocumentResolver(db_factory)
    ref = SourceRef(kind="document", id=doc_id)

    first_latest = resolver.revision(ref, version_policy="latest")
    pinned_before = resolver.revision(ref, version_policy="pinned", pinned_version=1)

    db = db_factory()
    try:
        doc = db.query(cdb.Document).filter(cdb.Document.id == doc_id).first()
        doc.current_content = "version two body"
        doc.version_count = 2
        db.add(cdb.DocumentVersion(id=f"{doc_id}-v2", document_id=doc_id,
                                   version_number=2, content="version two body"))
        db.commit()
    finally:
        db.close()

    assert resolver.revision(ref, version_policy="latest") != first_latest
    assert resolver.revision(ref, version_policy="pinned", pinned_version=1) == pinned_before
    assert resolver.read(ref, version_policy="latest").text == "version two body"
    assert resolver.read(ref, version_policy="pinned",
                         pinned_version=1).text == "version one body"


def test_a_pinned_version_that_does_not_exist_is_missing_not_the_nearest(db_factory):
    doc_id = _document(db_factory, content="only version", versions=1)
    _version(db_factory, doc_id, 1, "only version")
    resolver = DocumentResolver(db_factory)
    ref = SourceRef(kind="document", id=doc_id)
    assert resolver.revision(ref, version_policy="pinned", pinned_version=7) == ""
    assert resolver.read(ref, version_policy="pinned", pinned_version=7).text == ""


def test_snapshot_is_unsupported_and_says_why(db_factory):
    doc_id = _document(db_factory)
    resolver = DocumentResolver(db_factory)
    note = resolver.policy_note("snapshot")
    assert "Artifact" in note
    corpus = resolver.extract(SourceRef(kind="document", id=doc_id),
                              version_policy="snapshot")
    assert corpus.degraded is True
    assert "Artifact" in corpus.note


def test_document_read_is_bounded_and_says_so(db_factory):
    doc_id = _document(db_factory, content="x" * 5000)
    content = DocumentResolver(db_factory).read(
        SourceRef(kind="document", id=doc_id), start=0, limit=100)
    assert len(content.text) == 100
    assert content.total == 5000
    assert content.truncated is True


def test_document_search_and_extract(db_factory):
    body = "# Alpha\n\nthe needle is here\n\n# Beta\n\nsomething else\n"
    doc_id = _document(db_factory, content=body)
    resolver = DocumentResolver(db_factory)
    ref = SourceRef(kind="document", id=doc_id)

    hits = resolver.search(ref, "NEEDLE")
    assert len(hits) == 1
    assert hits[0].location["line"] == 3
    assert "needle" in hits[0].snippet

    corpus = resolver.extract(ref)
    assert [c.title for c in corpus.chunks] == ["Alpha", "Beta"]
    assert corpus.revision.startswith(f"document:{doc_id}:")


def test_chunk_markdown_splits_on_headings_and_size():
    chunks = chunk_markdown("# One\n\nbody\n\n# Two\n\nbody\n")
    assert [t for t, _ in chunks] == ["One", "Two"]
    long_body = "\n\n".join("para " + str(i) * 200 for i in range(10))
    assert len(chunk_markdown("# Big\n\n" + long_body, max_chars=500)) > 1


# ── filesystem resolver ────────────────────────────────────────────────────

def test_has_traversal_sees_both_separators():
    assert has_traversal("a/../b")
    assert has_traversal(r"a\..\b")
    assert not has_traversal("a/b..c/d")
    assert not has_traversal(r"D:\docs\reference")


def test_a_path_with_a_dotdot_segment_is_rejected(tmp_path):
    """MANDATORY. realpath would collapse the '..' into a legal path; the
    syntax is refused before it gets the chance."""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "notes.md").write_text("hello", encoding="utf-8")
    escaping = os.path.join(str(root), "..", "docs", "notes.md")

    resolver = FilesystemResolver("file")
    meta = resolver.metadata(SourceRef(kind="file", path=escaping),
                             owner=OWNER, project=PROJECT)
    assert meta.state == "forbidden"
    assert meta.label == ""
    assert meta.canonical_ref == ""
    assert resolver.revision(SourceRef(kind="file", path=escaping)) == ""
    assert resolver.read(SourceRef(kind="file", path=escaping)).text == ""


def test_a_file_needs_an_effective_owner(tmp_path):
    target = tmp_path / "a.md"
    target.write_text("x", encoding="utf-8")
    meta = FilesystemResolver("file").metadata(
        SourceRef(kind="file", path=str(target)), owner="", project=PROJECT)
    assert meta.state == "forbidden"


def test_a_missing_file_is_missing(tmp_path):
    meta = FilesystemResolver("file").metadata(
        SourceRef(kind="file", path=str(tmp_path / "gone.md")),
        owner=OWNER, project=PROJECT)
    assert meta.state == "missing"


def test_kind_and_target_must_agree(tmp_path):
    folder = tmp_path / "tree"
    folder.mkdir()
    meta = FilesystemResolver("file").metadata(
        SourceRef(kind="file", path=str(folder)), owner=OWNER, project=PROJECT)
    assert meta.state == "unsupported"


def test_a_small_file_is_content_addressed_and_moves_when_edited(tmp_path):
    target = tmp_path / "spec.md"
    target.write_text("first", encoding="utf-8")
    resolver = FilesystemResolver("file")
    ref = SourceRef(kind="file", path=str(target))

    first = resolver.revision(ref)
    assert ":sha256:" in first
    target.write_text("second", encoding="utf-8")
    assert resolver.revision(ref) != first


def test_a_folder_read_lists_and_does_not_concatenate(tmp_path):
    root = tmp_path / "kb"
    (root / "node_modules").mkdir(parents=True)
    (root / "node_modules" / "junk.md").write_text("SHOULD NOT APPEAR", encoding="utf-8")
    (root / "a.md").write_text("alpha body", encoding="utf-8")
    (root / "b.md").write_text("beta body", encoding="utf-8")

    resolver = FilesystemResolver("folder")
    ref = SourceRef(kind="folder", path=str(root))
    meta = resolver.metadata(ref, owner=OWNER, project=PROJECT)
    assert meta.state == "ok"
    assert meta.media_type == "inode/directory"
    assert meta.extra["entries"] == 2

    listing = resolver.read(ref).text
    assert "a.md" in listing and "b.md" in listing
    assert "alpha body" not in listing
    assert "junk.md" not in listing


def test_a_folder_extract_prunes_and_chunks(tmp_path):
    root = tmp_path / "kb"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config.md").write_text("GIT INTERNALS", encoding="utf-8")
    (root / "guide.md").write_text("# Guide\n\nthe needle is here\n", encoding="utf-8")
    (root / "photo.png").write_bytes(b"\x89PNG binary")

    resolver = FilesystemResolver("folder")
    ref = SourceRef(kind="folder", path=str(root))
    corpus = resolver.extract(ref)
    text = "\n".join(c.text for c in corpus.chunks)
    assert "needle" in text
    assert "GIT INTERNALS" not in text
    assert "PNG" not in text

    hits = resolver.search(ref, "needle")
    assert len(hits) == 1
    assert hits[0].location["path"] == "guide.md"


def test_a_binary_file_is_not_decoded(tmp_path):
    target = tmp_path / "image.png"
    target.write_bytes(b"\x89PNG\x00\x01binary bytes")
    resolver = FilesystemResolver("file")
    ref = SourceRef(kind="file", path=str(target))
    content = resolver.read(ref)
    assert content.text == ""
    assert content.media_type == "application/octet-stream"
    assert resolver.extract(ref).degraded is True


# ── artifact resolver ──────────────────────────────────────────────────────

def _artifact(db_factory, store_dir, *, artifact_id="art_1", owner=OWNER,
              project_id="prj_1", kind="text", filename="art_1.md",
              body="# Board\n\nvisual identity notes\n", label="Visual identity"):
    path = os.path.join(str(store_dir), filename)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    db = db_factory()
    try:
        db.add(cdb.ArtifactRow(id=artifact_id, kind=kind, filename=filename,
                               media_type="text/markdown", byte_size=len(body),
                               label=label, owner=owner, project_id=project_id,
                               sha256="a" * 64))
        db.commit()
    finally:
        db.close()
    return artifact_id


def test_artifact_metadata_and_text(db_factory, tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    art_id = _artifact(db_factory, store)
    resolver = ArtifactResolver(db_factory, store_dir=str(store))
    ref = SourceRef(kind="artifact", id=art_id)

    meta = resolver.metadata(ref, owner=OWNER, project=PROJECT)
    assert meta.state == "ok"
    assert meta.label == "Visual identity"
    assert meta.revision == f"artifact:{art_id}:sha256:{'a' * 64}"
    assert "visual identity notes" in resolver.read(ref).text
    assert resolver.extract(ref).chunks


def test_an_artifact_of_another_owner_leaks_nothing(db_factory, tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    art_id = _artifact(db_factory, store, owner=OTHER, label=SECRET_TITLE)
    meta = ArtifactResolver(db_factory, store_dir=str(store)).metadata(
        SourceRef(kind="artifact", id=art_id), owner=OWNER, project=PROJECT)
    assert meta.state == "forbidden"
    assert SECRET_TITLE not in repr(meta.to_dict())


def test_an_artifact_of_another_project_is_forbidden(db_factory, tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    art_id = _artifact(db_factory, store, project_id="prj_other", label=SECRET_TITLE)
    meta = ArtifactResolver(db_factory, store_dir=str(store)).metadata(
        SourceRef(kind="artifact", id=art_id), owner=OWNER, project=PROJECT)
    assert meta.state == "forbidden"
    assert SECRET_TITLE not in repr(meta.to_dict())


def test_an_artifact_without_a_project_may_be_linked(db_factory, tmp_path):
    """NULL project_id means unknown, never 'belongs to everyone' — but it is
    the pre-attribution case and linking it is allowed."""
    store = tmp_path / "store"
    store.mkdir()
    art_id = _artifact(db_factory, store, project_id=None)
    meta = ArtifactResolver(db_factory, store_dir=str(store)).metadata(
        SourceRef(kind="artifact", id=art_id), owner=OWNER, project=PROJECT)
    assert meta.state == "ok"


def test_a_binary_artifact_is_described_never_decoded(db_factory, tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    art_id = _artifact(db_factory, store, kind="image", filename="art_1.png",
                       body="not really png", label="Cover art")
    resolver = ArtifactResolver(db_factory, store_dir=str(store))
    text = resolver.read(SourceRef(kind="artifact", id=art_id)).text
    assert "Cover art" in text
    assert "not really png" not in text


# ── gallery resolver ───────────────────────────────────────────────────────

def _image(db_factory, *, image_id="img_1", owner=OWNER, caption="Mood board",
           prompt="a quiet room"):
    db = db_factory()
    try:
        db.add(cdb.GalleryImage(id=image_id, filename=f"{image_id}.png", prompt=prompt,
                                caption=caption, owner=owner, file_hash="b" * 64))
        db.commit()
    finally:
        db.close()
    return image_id


def test_gallery_image_metadata_and_description(db_factory):
    image_id = _image(db_factory)
    resolver = GalleryImageResolver(db_factory)
    ref = SourceRef(kind="gallery_image", id=image_id)
    meta = resolver.metadata(ref, owner=OWNER, project=PROJECT)
    assert meta.state == "ok"
    assert meta.label == "Mood board"
    assert meta.extra["transitional"] is True
    assert "a quiet room" in resolver.read(ref).text


def test_a_gallery_image_of_another_owner_leaks_nothing(db_factory):
    image_id = _image(db_factory, owner=OTHER, caption=SECRET_TITLE)
    meta = GalleryImageResolver(db_factory).metadata(
        SourceRef(kind="gallery_image", id=image_id), owner=OWNER, project=PROJECT)
    assert meta.state == "forbidden"
    assert SECRET_TITLE not in repr(meta.to_dict())


def test_a_gallery_image_ignores_the_project_because_it_has_no_column(db_factory):
    """Documented weakness, asserted so it cannot change silently: membership
    comes from the link, not from the row."""
    image_id = _image(db_factory)
    meta = GalleryImageResolver(db_factory).metadata(
        SourceRef(kind="gallery_image", id=image_id), owner=OWNER,
        project={"id": "some_other_project", "owner": OWNER})
    assert meta.state == "ok"
