"""The typed context-link store: one list, idempotent writes, no lost updates.

Links live in the same `context_items` a project has always had — a second
parallel list would give "what belongs to this project" two answers, and the
first time they disagreed nobody would notice. So the pressure points are:
old items must keep behaving exactly as before, new ones must not inherit
write access they were never granted, repeating an attach must not duplicate
it, and two agents attaching at the same time must both survive.
"""

import os
import sys
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import database as db_mod  # noqa: E402
from core.database import Base, Session as DbSession  # noqa: E402
from services import projects as projects_mod  # noqa: E402
from services.projects import ProjectError, ProjectStore  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    st = ProjectStore(str(tmp_path / "data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    return st


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return str(ws)


@pytest.fixture()
def project(store, workspace):
    return store.create("Faustus", folder="Faustus", workspace=workspace)


# ── legacy compatibility ──────────────────────────────────────────────


def test_a_legacy_item_normalizes_without_losing_write_access(store, project, tmp_path):
    """The defaults of §7, and the one that would change behaviour if wrong.

    Today's attached folders ARE editable roots for the file tools. Normalising
    them to `read_only` would silently revoke write access the user already
    has, so a raw {id, path, kind, name} item must come back as a work root.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    item = store.add_context_item(project["id"], str(docs))
    assert set(item) == {"id", "path", "kind", "name"}      # still written legacy-shaped

    link = store.normalize_link(item)
    assert link["id"] == item["id"]
    assert link["kind"] == "folder"
    assert link["path"] == str(docs)
    assert link["label"] == "docs"                          # from `name`
    assert link["access_mode"] == "work_root"
    assert link["retrieval_policy"] == "on_demand"
    assert link["version_policy"] == "latest"
    assert link["role"] == "reference"
    assert link["enabled"] is True
    assert link["ref_id"] == ""
    assert link["index_status"] == "none"


def test_reading_a_project_does_not_rewrite_it(store, project, tmp_path):
    """Normalisation is in memory. projects.json is only rewritten when that
    project is next mutated anyway — an upgrade must not touch the user's file
    just because something read it."""
    docs = tmp_path / "docs"
    docs.mkdir()
    store.add_context_item(project["id"], str(docs))
    before = open(store.path, encoding="utf-8").read()

    store.list_links(project["id"])
    store.system_block(store.get(project["id"]))
    ProjectStore(os.path.dirname(store.path)).list_links(project["id"])

    assert open(store.path, encoding="utf-8").read() == before


def test_a_bogus_stored_value_falls_back_instead_of_raising(store):
    """projects.json is hand-editable. A typo there costs a default, not a chat."""
    link = store.normalize_link({"id": "ctx_x", "kind": "document", "ref_id": "d1",
                                 "retrieval_policy": "on-demand", "role": "boss"})
    assert link["retrieval_policy"] == "on_demand"
    assert link["role"] == "reference"


# ── idempotency ───────────────────────────────────────────────────────


def test_attaching_the_same_source_twice_leaves_one_link(store, project):
    first, dedup_a = store.upsert_link(project["id"], {
        "kind": "document", "ref_id": "doc-1", "label": "Requisitos", "role": "requirements",
    })
    second, dedup_b = store.upsert_link(project["id"], {
        "kind": "document", "ref_id": "doc-1", "label": "Otro nombre",
    })

    assert dedup_a is False and dedup_b is True
    assert second["id"] == first["id"]
    assert second["label"] == "Requisitos"      # re-attaching is not a reset
    assert len(store.list_links(project["id"])) == 1


def test_the_same_path_spelled_differently_is_the_same_source(store, project, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    store.upsert_link(project["id"], {"kind": "folder", "path": str(docs)})
    _, dedup = store.upsert_link(project["id"], {"kind": "folder", "path": str(docs) + os.sep + "."})
    assert dedup is True
    assert len(store.list_links(project["id"])) == 1


def test_the_same_document_under_two_version_policies_is_two_links(store, project):
    """"The requirements as they evolve" and "the requirements as approved at
    v3" are different sources of knowledge that happen to share a ref_id.
    Collapsing them would make pinning impossible to express."""
    latest, _ = store.upsert_link(project["id"], {
        "kind": "document", "ref_id": "doc-1", "version_policy": "latest",
    })
    pinned, dedup = store.upsert_link(project["id"], {
        "kind": "document", "ref_id": "doc-1", "version_policy": "pinned", "pinned_version": 3,
    })

    assert dedup is False
    assert pinned["id"] != latest["id"]
    assert len(store.list_links(project["id"])) == 2


def test_a_new_link_is_read_only_unless_asked_otherwise(store, project, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    link, _ = store.upsert_link(project["id"], {"kind": "folder", "path": str(docs)})
    assert link["access_mode"] == "read_only"

    other = tmp_path / "code"
    other.mkdir()
    root, _ = store.upsert_link(project["id"], {
        "kind": "folder", "path": str(other), "access_mode": "work_root",
    })
    assert root["access_mode"] == "work_root"


def test_a_link_needs_a_locator(store, project):
    with pytest.raises(ProjectError):
        store.upsert_link(project["id"], {"kind": "document"})       # no ref_id
    with pytest.raises(ProjectError):
        store.upsert_link(project["id"], {"kind": "folder"})         # no path
    with pytest.raises(ProjectError):
        store.upsert_link(project["id"], {"kind": "website", "ref_id": "x"})


# ── patching ──────────────────────────────────────────────────────────


def test_patch_changes_policy_but_never_identity(store, project):
    link, _ = store.upsert_link(project["id"], {"kind": "document", "ref_id": "doc-1"})

    patched = store.patch_link(project["id"], link["id"], {
        "retrieval_policy": "auto", "role": "requirements", "tags": ["voz"], "enabled": False,
    })
    assert patched["retrieval_policy"] == "auto"
    assert patched["role"] == "requirements"
    assert patched["tags"] == ["voz"]
    assert patched["enabled"] is False
    assert patched["ref_id"] == "doc-1"

    for forbidden in ({"kind": "artifact"}, {"ref_id": "doc-2"}, {"path": "D:/x"}):
        with pytest.raises(ProjectError):
            store.patch_link(project["id"], link["id"], forbidden)

    still = store.get_link(project["id"], link["id"])
    assert still["kind"] == "document" and still["ref_id"] == "doc-1" and still["path"] == ""


def test_patching_an_unknown_field_is_refused(store, project):
    link, _ = store.upsert_link(project["id"], {"kind": "document", "ref_id": "doc-1"})
    with pytest.raises(ProjectError):
        store.patch_link(project["id"], link["id"], {"owner": "otro"})


# ── detach ────────────────────────────────────────────────────────────


def test_removing_a_link_never_removes_the_source(store, project, tmp_path):
    """Detach withdraws a statement about belonging. It is not a delete."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "brief.md").write_text("requisitos", encoding="utf-8")

    link, _ = store.upsert_link(project["id"], {"kind": "folder", "path": str(docs)})
    assert store.remove_link(project["id"], link["id"]) is True

    assert store.list_links(project["id"]) == []
    assert (docs / "brief.md").read_text(encoding="utf-8") == "requisitos"
    assert store.remove_link(project["id"], link["id"]) is False


# ── ownership ─────────────────────────────────────────────────────────


def test_another_owner_gets_nothing_and_learns_nothing(store, workspace, tmp_path):
    mine = store.create("Faustus", folder="Faustus", workspace=workspace, owner="luis")
    link, _ = store.upsert_link(mine["id"], {"kind": "document", "ref_id": "doc-1",
                                             "label": "Secreto"}, owner="luis")

    assert store.list_links(mine["id"], owner="otro") == []
    assert store.get_link(mine["id"], link["id"], owner="otro") is None
    assert store.upsert_link(mine["id"], {"kind": "document", "ref_id": "d2"},
                             owner="otro") == (None, False)
    assert store.patch_link(mine["id"], link["id"], {"enabled": False}, owner="otro") is None
    assert store.remove_link(mine["id"], link["id"], owner="otro") is False

    # Nothing changed, and no exception carried the project's name out.
    assert store.get_link(mine["id"], link["id"], owner="luis")["enabled"] is True


# ── revision ──────────────────────────────────────────────────────────


def test_context_revision_rises_on_every_mutation(store, project, tmp_path):
    """Indexing runs in the background and can outlive the link it was started
    for. The job carries the revision it read and drops its result when this
    number has moved."""
    pid = project["id"]
    assert store.context_revision(pid) == 0

    link, _ = store.upsert_link(pid, {"kind": "document", "ref_id": "doc-1"})
    after_attach = store.context_revision(pid)
    assert after_attach > 0

    store.patch_link(pid, link["id"], {"retrieval_policy": "auto"})
    after_patch = store.context_revision(pid)
    assert after_patch > after_attach

    store.remove_link(pid, link["id"])
    assert store.context_revision(pid) > after_patch

    # The legacy entry points move it too — they mutate the same list.
    docs = tmp_path / "docs"
    docs.mkdir()
    before = store.context_revision(pid)
    store.add_context_item(pid, str(docs))
    assert store.context_revision(pid) > before


def test_an_unknown_project_has_no_revision(store):
    assert store.context_revision("nope") == 0


# ── belonging is not permission ───────────────────────────────────────


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.session_manager as sm_mod

    url = "sqlite:///" + (tmp_path / "links.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    monkeypatch.setattr(sm_mod, "SessionLocal", Session)
    yield Session
    engine.dispose()


def test_a_read_only_link_does_not_widen_the_work_roots(db, store, project, tmp_path):
    """The separation of plan §8. A source can belong to the project's
    knowledge without the file tools being allowed to write to it — otherwise
    "add this reference to the project" would quietly hand out write access to
    wherever it lives."""
    session = db()
    try:
        session.add(DbSession(id="s1", name="s1", endpoint_url="http://ep", model="m",
                              folder="Faustus", project_id=project["id"]))
        session.commit()
    finally:
        session.close()

    reference = tmp_path / "reference"
    reference.mkdir()
    editable = tmp_path / "editable"
    editable.mkdir()

    store.upsert_link(project["id"], {"kind": "folder", "path": str(reference)})
    store.upsert_link(project["id"], {"kind": "folder", "path": str(editable),
                                      "access_mode": "work_root"})
    # A non-path source can never be a work root whatever it claims.
    store.upsert_link(project["id"], {"kind": "document", "ref_id": "doc-1",
                                      "access_mode": "work_root"})

    roots = [os.path.normcase(r) for r in projects_mod.work_roots_for_session("s1")]
    assert os.path.normcase(str(editable)) in roots
    assert os.path.normcase(str(reference)) not in roots
    assert os.path.normcase(project["workspace"]) in roots


def test_legacy_items_keep_their_work_root_semantics(db, store, project, tmp_path):
    """The compatibility half of the same rule: nothing the user already
    attached loses write access."""
    session = db()
    try:
        session.add(DbSession(id="s1", name="s1", endpoint_url="http://ep", model="m",
                              folder="Faustus", project_id=project["id"]))
        session.commit()
    finally:
        session.close()

    docs = tmp_path / "docs"
    docs.mkdir()
    store.add_context_item(project["id"], str(docs))

    roots = [os.path.normcase(r) for r in projects_mod.work_roots_for_session("s1")]
    assert os.path.normcase(str(docs)) in roots


# ── the prompt manifest ───────────────────────────────────────────────


def test_system_block_lists_links_without_their_content(store, project, tmp_path):
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "brief.md").write_text("EL CONTENIDO SECRETO DEL BRIEF", encoding="utf-8")

    doc, _ = store.upsert_link(project["id"], {
        "kind": "document", "ref_id": "doc-1", "label": "Product requirements v4",
        "role": "requirements", "retrieval_policy": "auto",
    })
    store.upsert_link(project["id"], {
        "kind": "folder", "path": str(reference), "label": "Brief folder",
        "role": "style_reference",
    })

    block = store.system_block(store.get(project["id"]))
    assert "## Project knowledge sources" in block
    assert f"- {doc['id']} [requirements, document, auto] Product requirements v4" in block
    assert "[style_reference, folder, on_demand] Brief folder" in block
    assert "EL CONTENIDO SECRETO DEL BRIEF" not in block
    # A read_only link is knowledge, not a work root: it must not be advertised
    # under the section that says the file tools may modify it.
    work_roots_section = block.split("## Project knowledge sources")[0]
    assert str(reference) not in work_roots_section


def test_no_typed_links_means_no_empty_heading(store, project, tmp_path):
    """The section sits in the stable prefix of every prompt in the project.
    An empty heading there is pure cost."""
    assert "Project knowledge sources" not in store.system_block(store.get(project["id"]))

    docs = tmp_path / "docs"
    docs.mkdir()
    store.add_context_item(project["id"], str(docs))
    block = store.system_block(store.get(project["id"]))
    assert "Project knowledge sources" not in block   # legacy items are work roots
    assert "Project work roots" in block


def test_a_disabled_link_leaves_the_manifest(store, project):
    link, _ = store.upsert_link(project["id"], {"kind": "document", "ref_id": "doc-1",
                                                "label": "Borrador"})
    assert "Borrador" in store.system_block(store.get(project["id"]))
    store.patch_link(project["id"], link["id"], {"enabled": False})
    assert "Borrador" not in store.system_block(store.get(project["id"]))


def test_the_manifest_is_byte_stable_across_calls(store, project):
    store.upsert_link(project["id"], {"kind": "document", "ref_id": "doc-1", "label": "A"})
    store.upsert_link(project["id"], {"kind": "artifact", "ref_id": "art-1", "label": "B"})
    project_row = store.get(project["id"])
    assert store.system_block(project_row) == store.system_block(project_row)


# ── concurrency ───────────────────────────────────────────────────────


def test_twenty_concurrent_attaches_all_survive(store, project):
    """The lost update of plan §18.

    Two agents attaching a source at the same time used to interleave
    load-modify-save and drop one of the two links. `os.replace` does not fix
    that: it guarantees no half-written file, not that both writers' changes
    survive. Remove the RLock from ProjectStore and this test fails.
    """
    pid = project["id"]
    barrier = threading.Barrier(20)
    errors = []

    def attach(n):
        try:
            barrier.wait()
            store.upsert_link(pid, {"kind": "document", "ref_id": f"doc-{n}"})
        except Exception as exc:            # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=attach, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    links = store.list_links(pid)
    assert len(links) == 20
    assert sorted(ln["ref_id"] for ln in links) == sorted(f"doc-{n}" for n in range(20))
    # And what is on disk agrees with what is cached.
    reloaded = ProjectStore(os.path.dirname(store.path))
    assert len(reloaded.list_links(pid)) == 20
