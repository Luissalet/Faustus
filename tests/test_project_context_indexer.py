import copy

import pytest

from src.context_engine.adapters.project_links import ProjectLinksSource
from src.project_context import index as context_index
from src.project_context.indexer import run_pending
from src.project_context.models import (
    ActorRef, ExtractedChunk, ExtractedCorpus, SourceMetadata, SourceRef,
)
from src.project_context.service import ProjectContextService


OWNER = "luis"
PROJECT = {"id": "prj_1", "owner": OWNER, "name": "Faustus", "enabled": True}


def _link(**patch):
    row = {
        "id": "ctx_1", "kind": "document", "ref_id": "doc_1",
        "label": "Requirements", "retrieval_policy": "auto",
        "version_policy": "latest", "content_revision": "r1",
        "index_status": "queued", "index_revision": "", "enabled": True,
    }
    row.update(patch)
    return row


class Store:
    def __init__(self, row=None):
        self.row = _link() if row is None else copy.deepcopy(row)

    def list_links(self, project_id, *, owner=None, kind="", enabled_only=False):
        if owner != OWNER or project_id != PROJECT["id"] or self.row is None:
            return []
        return [copy.deepcopy(self.row)]

    def get_link(self, project_id, link_id, *, owner=None):
        rows = self.list_links(project_id, owner=owner)
        return rows[0] if rows and rows[0]["id"] == link_id else None

    def patch_link(self, project_id, link_id, patch, *, owner=None):
        if not self.get_link(project_id, link_id, owner=owner):
            return None
        self.row.update(dict(patch or {}))
        return copy.deepcopy(self.row)

    def remove_link(self, project_id, link_id, *, owner=None):
        if not self.get_link(project_id, link_id, owner=owner):
            return False
        self.row = None
        return True


class Resolver:
    kind = "document"

    def __init__(self, revisions=("r1",)):
        self.revisions = list(revisions)
        self.search_called = False

    def metadata(self, ref, *, owner, project):
        if owner != OWNER:
            return SourceMetadata.denied("document", "forbidden")
        return SourceMetadata(state="ok", kind="document", owner=OWNER,
                              canonical_ref=ref.locator, revision=self.revisions[0])

    validate = metadata

    def revision(self, ref, *, version_policy="latest", pinned_version=None):
        if len(self.revisions) > 1:
            return self.revisions.pop(0)
        return self.revisions[0]

    def extract(self, ref, *, version_policy="latest", pinned_version=None):
        return ExtractedCorpus(
            revision="r1",
            chunks=(ExtractedChunk(index=0, text="The launch code is aurora.",
                                   title="Requirements", location={"line": 4}),),
        )

    def search(self, ref, query, *, limit=20):
        self.search_called = True
        raise AssertionError("a ready durable index must be used")


@pytest.fixture(autouse=True)
def isolated_index(tmp_path):
    context_index.use_path(str(tmp_path / "project-context.db"))
    yield
    context_index.use_path(None)


def test_service_builds_owner_scoped_index_and_marks_link_ready():
    store = Store()
    svc = ProjectContextService(store=store,
                                resolver_map={"document": Resolver()}, clock=lambda: 42)

    result = svc.index(project=PROJECT, owner=OWNER, link_id="ctx_1",
                       actor=ActorRef(kind="system"))

    assert result.ok and result.state == "ready" and result.chunks == 1
    assert store.row["index_status"] == "ready"
    assert store.row["index_revision"] == "r1"
    assert context_index.indexed_revision(owner=OWNER, project_id="prj_1",
                                          link_id="ctx_1") == "r1"
    assert context_index.search(owner=OWNER, project_id="prj_1", link_id="ctx_1",
                                query="aurora")[0].location == {"line": 4}
    assert context_index.search(owner="mallory", project_id="prj_1", link_id="ctx_1",
                                query="aurora") == []


def test_changed_source_during_extract_is_discarded_and_requeued():
    store = Store()
    svc = ProjectContextService(store=store,
                                resolver_map={"document": Resolver(("r1", "r2"))})

    result = svc.index(project=PROJECT, owner=OWNER, link_id="ctx_1",
                       actor=ActorRef(kind="system"))

    assert result.ok and result.state == "stale"
    assert store.row["content_revision"] == "r2"
    assert store.row["index_status"] == "stale"
    assert context_index.indexed_revision(owner=OWNER, project_id="prj_1",
                                          link_id="ctx_1") == ""


def test_project_link_search_uses_matching_durable_index(monkeypatch):
    resolver = Resolver()
    context_index.replace(
        owner=OWNER, project_id="prj_1", link_id="ctx_1",
        corpus=resolver.extract(SourceRef(kind="document", id="doc_1")),
    )
    from src.project_context.resolvers import base
    monkeypatch.setattr(base, "get_resolver", lambda kind: resolver)
    link = _link(index_status="ready", index_revision="r1")

    hits = ProjectLinksSource._matches(link, "aurora", OWNER, PROJECT, 3)

    assert len(hits) == 1
    assert hits[0].mode == "index" and not hits[0].degraded
    assert not resolver.search_called


def test_idle_worker_consumes_queued_links(monkeypatch):
    store = Store()
    svc = ProjectContextService(store=store, resolver_map={"document": Resolver()})
    import src.project_context.indexer as indexer
    monkeypatch.setattr(indexer, "service", lambda: svc)
    monkeypatch.setattr(indexer, "should_yield", lambda: False)

    rows = run_pending([PROJECT])

    assert len(rows) == 1 and rows[0]["state"] == "ready"
    assert store.row["index_status"] == "ready"


def test_idle_worker_yields_to_an_active_turn(monkeypatch):
    import src.project_context.indexer as indexer
    monkeypatch.setattr(indexer, "should_yield", lambda: True)

    assert run_pending([PROJECT]) == [
        {"ok": True, "status": "yielded", "reason": "interactive_run"}
    ]
