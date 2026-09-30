"""By-id expansion must preserve learned-memory retrieval boundaries."""
from datetime import datetime, timedelta, timezone
import pytest
from src import memory_engine as engine
from src.context_engine.adapters.memory import MemoryEngineSource
from src.context_engine.candidates import RetrievalRequest, fetch_ref
from src.context_engine.contracts import ContextRequest, ContextExecution, ContextPolicy

@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "memory_conflict_detection_enabled", lambda: False)
    engine.set_vector_store(None)
    yield
    engine.reset_vector_store()

def req(owner="qa", workspace="workspace", project="", allowed=True):
    return RetrievalRequest(request=ContextRequest(execution=ContextExecution(
        owner=owner, workspace=workspace, project_id=project),
        policy=ContextPolicy(allow_personal_memory=allowed)),
        query="", sections=("retrieved_memory",), lanes=("mandatory",))

@pytest.mark.parametrize("case", ["blank_owner", "other_owner", "other_workspace", "blank_workspace", "secret", "expired", "future"])
async def test_by_id_refuses_rows_outside_retrieval_scope(database, case):
    now = datetime.now(timezone.utc)
    options = {"owner": "qa", "project": "workspace"}
    request = req()
    if case == "blank_owner": request = req(owner="")
    elif case == "other_owner": options["owner"] = "other"
    elif case == "other_workspace": options["project"] = "elsewhere"
    elif case == "blank_workspace": request = req(workspace="")
    elif case == "secret": options["sensitivity"] = "secret"
    elif case == "expired": options["valid_until"] = now-timedelta(days=1)
    elif case == "future": options["valid_from"] = now+timedelta(days=1)
    item = engine.add_item("QA excluded "+case, level="procedural", trust_class="human_explicit", **options)
    assert await fetch_ref("mem:"+item["id"], request, sources=[MemoryEngineSource()]) is None

@pytest.mark.parametrize("owner,workspace,project,stored_owner,stored_project", [
    ("qa", "workspace", "", "qa", "workspace"),
    ("qa", "workspace", "", "", ""),
    ("", "", "", "", ""),
    ("qa", "", "project-fallback", "qa", "project-fallback"),
])
async def test_by_id_preserves_authorized_and_global_rows(database, owner, workspace, project, stored_owner, stored_project):
    item = engine.add_item("QA authorized", owner=stored_owner, project=stored_project,
        level="procedural", trust_class="human_explicit")
    candidate = await fetch_ref("mem:"+item["id"], req(owner, workspace, project), sources=[MemoryEngineSource()])
    assert candidate is not None and candidate.body == "QA authorized"
    assert engine.get_item(item["id"])["access_count"] == item["access_count"] == 0
    assert engine.get_item(item["id"])["last_accessed"] == item["last_accessed"]

async def test_by_id_policy_gate_precedes_store_read(database, monkeypatch):
    monkeypatch.setattr(engine, "get_item", lambda *args: pytest.fail("policy opened memory"))
    assert await fetch_ref("mem:any", req(allowed=False), sources=[MemoryEngineSource()]) is None
