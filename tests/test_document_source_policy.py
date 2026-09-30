"""Personal document policy is enforced before opening or querying a store."""
import pytest

from src.context_engine.adapters.documents import DocumentSource
from src.context_engine.candidates import RetrievalRequest, gather
from src.context_engine.contracts import ContextExecution, ContextPolicy, ContextRequest, ContextTask
from src.context_engine.planner import plan


def request(owner="qa-owner", allowed=True):
    return ContextRequest(
        execution=ContextExecution(owner=owner),
        task=ContextTask(query="research document architecture", intent="research"),
        policy=ContextPolicy(allow_personal_memory=allowed),
    )


def retrieval(owner="qa-owner", allowed=True):
    return RetrievalRequest(request=request(owner, allowed), query="research document architecture",
                            sections=("retrieved_documents",), lanes=("semantic", "lexical"))


@pytest.mark.parametrize("owner,allowed", [("qa-owner", False), ("", True), ("", False)])
async def test_denied_requests_do_not_open_store(monkeypatch, owner, allowed):
    source = DocumentSource()
    opened = []
    def forbidden_store():
        opened.append(True)
        raise AssertionError("denied request opened the store")
    monkeypatch.setattr(source, "_store", forbidden_store)
    result = (await gather([source], retrieval(owner, allowed)))[0]
    assert opened == []
    assert result.candidates == ()
    assert not result.degraded


@pytest.mark.parametrize("owner,allowed", [("qa-owner", False), ("", True)])
def test_direct_search_also_checks_policy_before_store(monkeypatch, owner, allowed):
    source = DocumentSource()
    def forbidden_store():
        pytest.fail("direct denied search opened a store")
    monkeypatch.setattr(source, "_store", forbidden_store)
    assert source._search(retrieval(owner, allowed)) == ()


async def test_incognito_cannot_query_an_already_open_store():
    class Store:
        def search(self, *args, **kwargs):
            pytest.fail("incognito queried an existing personal store")
    result = (await gather([DocumentSource(Store())], retrieval(allowed=False)))[0]
    assert result.candidates == ()
    assert not result.degraded


async def test_allowed_request_keeps_owner_query_and_limit():
    calls = []
    class Store:
        def search(self, query, k, owner):
            calls.append((query, k, owner))
            return [{"id": "chunk-1", "document": "architecture details", "similarity": .8,
                     "metadata": {"source": "qa-document.txt", "owner": owner}}]
    req = retrieval()
    result = (await gather([DocumentSource(Store())], req))[0]
    assert calls == [(req.query, req.top(), "qa-owner")]
    assert len(result.candidates) == 1
    assert result.candidates[0].owner == "qa-owner"


def test_planner_excludes_personal_documents_when_policy_is_off():
    denied = plan(request(allowed=False), available=("documents",))
    permitted = plan(request(), available=("documents",))
    assert "documents" not in denied.source_ids
    assert "documents" in denied.skipped
    assert "documents" in permitted.source_ids
