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

    def patch_link_if_current(self, project_id, link_id, patch, *, expected, owner=None, before_patch=None):
        row = self.get_link(project_id, link_id, owner=owner)
        current = ProjectContextService._parse_link(row) if row else None
        if current is None or any(getattr(current, k) != v for k, v in expected.items()):
            return None, None
        payload = before_patch() if before_patch else None
        return self.patch_link(project_id, link_id, patch, owner=owner), payload


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


def test_extraction_failure_keeps_link_readable_and_refresh_retries(monkeypatch):
    store = Store()
    resolver = Resolver()
    svc = ProjectContextService(store=store, resolver_map={"document": resolver})
    actor = ActorRef(kind="system")
    extract = resolver.extract

    def fail(*args, **kwargs):
        raise RuntimeError("temporary extraction failure")

    monkeypatch.setattr(resolver, "extract", fail)
    result = svc.index(project=PROJECT, owner=OWNER, link_id="ctx_1", actor=actor)
    assert not result.ok and result.error == "index_failed"
    assert store.row["source_state"] == "ok"
    assert svc.list(project=PROJECT, owner=OWNER)[0].index_status == "failed"

    monkeypatch.setattr(resolver, "extract", extract)
    refreshed = svc.refresh(project=PROJECT, owner=OWNER, link_id="ctx_1", actor=actor)
    assert refreshed.ok and not refreshed.changed
    assert refreshed.index_status == "queued"
    assert svc.index(project=PROJECT, owner=OWNER, link_id="ctx_1", actor=actor).ok


@pytest.mark.parametrize("patch", [{"enabled": False}, {"retrieval_policy": "disabled"}])
def test_disabling_during_extraction_discards_result(monkeypatch, patch):
    store = Store()
    resolver = Resolver()
    svc = ProjectContextService(store=store, resolver_map={"document": resolver})
    extract = resolver.extract

    def disable(*args, **kwargs):
        svc.update(project=PROJECT, owner=OWNER, link_id="ctx_1", patch=patch,
                   actor=ActorRef(kind="system"))
        return extract(*args, **kwargs)

    monkeypatch.setattr(resolver, "extract", disable)
    result = svc.index(project=PROJECT, owner=OWNER, link_id="ctx_1",
                       actor=ActorRef(kind="system"))
    assert result.state == "disabled"
    assert store.row["index_status"] == "none"
    assert context_index.indexed_revision(owner=OWNER, project_id="prj_1",
                                          link_id="ctx_1") == ""


def test_missing_extracted_revision_is_not_published(monkeypatch):
    store = Store()
    resolver = Resolver()
    monkeypatch.setattr(resolver, "extract", lambda *a, **kw: ExtractedCorpus(
        revision="", chunks=(ExtractedChunk(index=0, text="unversioned"),)))
    svc = ProjectContextService(store=store, resolver_map={"document": resolver})
    result = svc.index(project=PROJECT, owner=OWNER, link_id="ctx_1",
                       actor=ActorRef(kind="system"))
    assert not result.ok and result.error == "index_failed"
    assert store.row["index_status"] == "failed"
    assert context_index.search(owner=OWNER, project_id="prj_1", link_id="ctx_1",
                                query="unversioned") == []


def test_index_write_failure_is_reported_and_preserves_previous_index(monkeypatch):
    resolver = Resolver()
    context_index.replace(owner=OWNER, project_id="prj_1", link_id="ctx_1",
                          corpus=resolver.extract(SourceRef(kind="document", id="doc_1")))
    store = Store(_link(index_revision="r1"))
    svc = ProjectContextService(store=store, resolver_map={"document": resolver})

    def fail(**kwargs):
        raise OSError("index storage unavailable")

    monkeypatch.setattr(context_index, "replace", fail)
    result = svc.index(project=PROJECT, owner=OWNER, link_id="ctx_1",
                       actor=ActorRef(kind="system"))
    assert not result.ok and result.error == "index_failed"
    assert store.row["index_status"] == "failed"
    assert store.row["index_revision"] == "r1"
    assert context_index.indexed_revision(owner=OWNER, project_id="prj_1",
                                          link_id="ctx_1") == "r1"


@pytest.mark.parametrize("disabled", [{"enabled": False}, {"retrieval_policy": "disabled"}])
def test_worker_skips_disabled_links_without_spending_work_budget(monkeypatch, disabled):
    from src.project_context.models import ProjectContextLink
    import src.project_context.indexer as indexer
    store = Store()
    svc = ProjectContextService(store=store, resolver_map={"document": Resolver()})
    monkeypatch.setattr(svc, "list", lambda **kw: [
        ProjectContextLink.parse(_link(id="ctx_disabled", **disabled)),
        ProjectContextLink.parse(_link()),
    ])
    monkeypatch.setattr(indexer, "service", lambda: svc)
    monkeypatch.setattr(indexer, "should_yield", lambda: False)
    rows = run_pending([PROJECT], max_links=1)
    assert len(rows) == 1 and rows[0]["state"] == "ready"
    assert store.row["index_status"] == "ready"


def test_worker_continues_after_project_listing_failure(monkeypatch):
    import src.project_context.indexer as indexer
    store = Store()
    svc = ProjectContextService(store=store, resolver_map={"document": Resolver()})
    original_list = svc.list

    def list_links(*, project, owner):
        if project["id"] == "broken":
            raise OSError("project storage unavailable")
        return original_list(project=project, owner=owner)

    monkeypatch.setattr(svc, "list", list_links)
    monkeypatch.setattr(indexer, "service", lambda: svc)
    monkeypatch.setattr(indexer, "should_yield", lambda: False)
    rows = run_pending([dict(PROJECT, id="broken"), PROJECT])
    assert len(rows) == 2 and not rows[0]["ok"]
    assert rows[1]["state"] == "ready"


@pytest.mark.parametrize('change,expected_state', [
    ({'enabled': False, 'index_status': 'none'}, 'disabled'),
    ({'retrieval_policy': 'disabled', 'index_status': 'none'}, 'disabled'),
    ({'content_revision': 'r2', 'index_status': 'stale'}, 'superseded'),
    (None, 'detached'),
])
def test_last_moment_change_prevents_publication(monkeypatch, change, expected_state):
    store = Store()
    real_conditional = store.patch_link_if_current
    def race(project_id, link_id, patch, **kwargs):
        if patch.get('index_status') == 'ready':
            if change is None:
                store.row = None
            else:
                store.row.update(change)
        return real_conditional(project_id, link_id, patch, **kwargs)
    monkeypatch.setattr(store, 'patch_link_if_current', race)
    svc = ProjectContextService(store=store, resolver_map={'document': Resolver()})
    result = svc.index(project=PROJECT, owner=OWNER, link_id='ctx_1', actor=ActorRef(kind='system'))
    assert result.state == expected_state
    assert context_index.indexed_revision(owner=OWNER, project_id=PROJECT['id'], link_id='ctx_1') == ''
    if change:
        assert all(store.row[k] == v for k, v in change.items())


def test_late_extraction_error_does_not_reenable_disabled_index(monkeypatch):
    store = Store()
    resolver = Resolver()
    def fail_after_disable(*args, **kwargs):
        store.row.update(enabled=False, index_status='none')
        raise OSError('late failure')
    monkeypatch.setattr(resolver, 'extract', fail_after_disable)
    svc = ProjectContextService(store=store, resolver_map={'document': resolver})
    result = svc.index(project=PROJECT, owner=OWNER, link_id='ctx_1', actor=ActorRef(kind='system'))
    assert result.state == 'disabled' and store.row['index_status'] == 'none'


def test_real_store_service_contract(tmp_path):
    from services.projects import ProjectStore
    store = ProjectStore(str(tmp_path / 'projects'))
    project = store.create('Index test', folder='Index test', owner=OWNER)
    row, _ = store.upsert_link(project['id'], _link(), owner=OWNER)
    svc = ProjectContextService(store=store, resolver_map={'document': Resolver()})
    result = svc.index(project=project, owner=OWNER, link_id=row['id'], actor=ActorRef(kind='system'))
    assert result.state == 'ready'
    assert store.get_link(project['id'], row['id'], owner=OWNER)['index_revision'] == 'r1'
