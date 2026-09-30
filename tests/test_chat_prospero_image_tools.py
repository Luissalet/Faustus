"""Normal chat image calls retain server provenance and Gallery ownership."""
import io
import json

import pytest
from PIL import Image
from src import tool_execution as execution, prospero_images, settings
from src.agent_tools import ToolBlock
from src.tool_policy import ToolPolicy
from src.tools import image as image_tool


@pytest.mark.asyncio
async def test_reused_provider_call_ids_are_distinct_between_server_runs(backend):
    from src.run_causality import bind_run, reset_run
    for run_id in ('run-one', 'run-one', 'run-two'):
        token = bind_run('server-session', run_id)
        try:
            await dispatch('generate_image', {'prompt': 'same intentional prompt'})
        finally:
            reset_run(token)
    ids = [call[3]['request_id'] for call in backend]
    assert ids[0] == ids[1] and ids[0] != ids[2]


@pytest.fixture
def backend(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "get_user_setting", lambda key, owner="", default=None: "prospero" if key == "image_execution_backend" else default)
    async def run(prompt, session_id, owner, **kwargs):
        calls.append((prompt, session_id, owner, kwargs))
        return {"exit_code": 0, "image_id": "generated", "image_url": "/api/generated-image/synthetic.png", "image_model": "prospero"}
    monkeypatch.setattr(prospero_images, "run_image", run)
    return calls


async def dispatch(tool, args, **options):
    return await execution.execute_tool_block(ToolBlock(tool, json.dumps(args)),
        owner="alice", session_id="server-session", call_id="server-call",
        security_context=execution.NO_TOOL_SECURITY_CONTEXT, **options)


@pytest.mark.asyncio
async def test_image_job_uses_server_owner_and_session_without_generation(monkeypatch):
    calls = []
    async def resume(request_id, session_id, owner):
        calls.append((request_id, session_id, owner))
        return {"image_url": "/api/generated-image/recovered.png", "request_id": request_id}
    async def generate(*args, **kwargs):
        pytest.fail("Checking an image must not generate another image")
    monkeypatch.setattr(prospero_images, "resume_image", resume)
    monkeypatch.setattr(prospero_images, "run_image", generate)
    _, result = await dispatch("image_job", {"request_id": "previous-request"})
    assert result["image_url"] == "/api/generated-image/recovered.png"
    assert calls == [("previous-request", "server-session", "alice")]


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{"request_id": "previous", "owner": "forged"}, {}, {"request_id": 5}])
async def test_image_job_rejects_extra_or_invalid_arguments(monkeypatch, args):
    async def resume(*args):
        pytest.fail("Invalid image lookup must not reach Prospero")
    monkeypatch.setattr(prospero_images, "resume_image", resume)
    _, result = await dispatch("image_job", args)
    assert result["exit_code"] == 1


@pytest.mark.asyncio
async def test_generate_bypasses_mcp_and_keeps_server_call_identity(backend, monkeypatch):
    async def mcp(*args, **kwargs):
        pytest.fail("Prospero must not traverse the legacy MCP image engine")
    monkeypatch.setattr(execution, "_call_mcp_tool", mcp)
    for _ in range(2):
        _, result = await dispatch("generate_image", {"prompt": "synthetic portrait", "owner": "forged", "session_id": "forged", "request_id": "forged"})
        assert result["image_id"] == "generated"
    assert backend == [("synthetic portrait", "server-session", "alice", {"request_id": "server-call"})] * 2


@pytest.mark.asyncio
async def test_generate_multiline_legacy_prompt_parser_is_reused(backend):
    _, result = await execution.execute_tool_block(ToolBlock("generate_image", "synthetic portrait\nlegacy-model\n512x512"),
        owner="alice", session_id="server-session", call_id="multiline-call", security_context=execution.NO_TOOL_SECURITY_CONTEXT)
    assert result["exit_code"] == 0
    assert backend[0][:3] == ("synthetic portrait", "server-session", "alice")
    assert backend[0][3]["request_id"] == "multiline-call"


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["disabled", "policy"])
async def test_existing_gates_prevent_any_image_submission(backend, gate):
    options = {"disabled_tools": {"generate_image"}} if gate == "disabled" else {"tool_policy": ToolPolicy(block_all_tool_calls=True)}
    _, result = await dispatch("generate_image", {"prompt": "synthetic"}, **options)
    assert result["exit_code"] == 1 and backend == []


@pytest.mark.asyncio
async def test_legacy_generation_still_uses_mcp(backend, monkeypatch):
    monkeypatch.setattr(settings, "get_user_setting", lambda key, owner="", default=None: "legacy")
    calls = []
    async def mcp(tool, content, **kwargs):
        calls.append((tool, content))
        return {"exit_code": 0, "output": "legacy result"}
    monkeypatch.setattr(execution, "_call_mcp_tool", mcp)
    _, result = await dispatch("generate_image", {"prompt": "synthetic"})
    assert result["output"] == "legacy result" and backend == [] and len(calls) == 1


@pytest.fixture
def owned_gallery(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core import database
    import src.constants
    engine = create_engine("sqlite://")
    database.GalleryImage.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(src.constants, "GENERATED_IMAGES_DIR", str(tmp_path))
    stream = io.BytesIO()
    Image.new("RGB", (4, 5), "red").save(stream, format="PNG")
    raw = stream.getvalue()
    (tmp_path / "owned.png").write_bytes(raw)
    (tmp_path / "private.png").write_bytes(raw)
    with sessions() as db:
        db.add(database.GalleryImage(id="source", filename="owned.png", owner="alice", is_active=True))
        db.add(database.GalleryImage(id="private", filename="private.png", owner="bob", is_active=True))
        db.commit()
    yield raw
    engine.dispose()


@pytest.mark.asyncio
async def test_instruction_edit_uses_owned_png_and_server_provenance(backend, owned_gallery):
    args = {"image_id": "source", "action": "instruction", "prompt": "make the background blue", "owner": "bob", "session_id": "forged", "request_id": "forged"}
    _, result = await dispatch("edit_image", args)
    assert result["image_id"] == "generated" and result["source_image_id"] == "source"
    prompt, session, owner, kwargs = backend[0]
    assert (prompt, session, owner) == (args["prompt"], "server-session", "alice")
    assert kwargs["request_id"] == "server-call"
    with Image.open(io.BytesIO(kwargs["image_bytes"])) as image:
        assert image.size == (4, 5) and image.format == "PNG"


@pytest.mark.asyncio
async def test_other_owner_image_never_reaches_adapter(backend, owned_gallery):
    _, result = await dispatch("edit_image", {"image_id": "private", "action": "instruction", "prompt": "synthetic"})
    assert result["exit_code"] == 1 and "owner" in result["error"] and backend == []


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["", "   ", 3])
async def test_instruction_prompt_validation_prevents_submission(backend, owned_gallery, prompt):
    _, result = await dispatch("edit_image", {"image_id": "source", "action": "instruction", "prompt": prompt})
    assert result["exit_code"] == 1 and backend == []


def test_native_generation_and_qualified_mcp_schema_names_do_not_collide():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_registry import _native_schema_index, _descriptor_for_mcp_tool
    names = [row["function"]["name"] for row in FUNCTION_TOOL_SCHEMAS]
    assert names.count("generate_image") == 1 and len(names) == len(set(names))
    assert _native_schema_index()["generate_image"][1]["required"] == ["prompt"]
    spec = _descriptor_for_mcp_tool({"name": "generate_image", "qualified_name": "mcp__image_gen__generate_image", "server_id": "image_gen", "input_schema": {"type": "object"}})
    assert spec.name != "generate_image"
    from src.tool_registry import snapshot
    class Mcp:
        def get_all_tools(self):
            return [{"name": "generate_image", "qualified_name": "mcp__image_gen__generate_image", "server_id": "image_gen", "input_schema": {"type": "object"}}]
    merged = [row.name for row in snapshot(mcp_manager=Mcp())]
    assert merged.count("generate_image") == 1 and merged.count("mcp__image_gen__generate_image") == 1
    assert len(merged) == len(set(merged))


@pytest.mark.asyncio
async def test_instruction_without_selected_backend_is_not_dispatched(backend, owned_gallery, monkeypatch):
    monkeypatch.setattr(settings, "get_user_setting", lambda key, owner="", default=None: "configured")
    _, result = await dispatch("edit_image", {"image_id": "source", "action": "instruction", "prompt": "synthetic"})
    assert result["exit_code"] == 1 and "Prospero" in result["error"] and backend == []


@pytest.mark.asyncio
async def test_adapter_error_does_not_fall_back_to_another_engine(backend, monkeypatch):
    async def failed(*args, **kwargs):
        return {"exit_code": 1, "error": "Synthetic Prospero failure"}
    async def mcp(*args, **kwargs):
        pytest.fail("An image failure must not switch engines")
    monkeypatch.setattr(prospero_images, "run_image", failed)
    monkeypatch.setattr(execution, "_call_mcp_tool", mcp)
    _, result = await dispatch("generate_image", {"prompt": "synthetic"})
    assert result["exit_code"] == 1 and "Prospero" in result["error"]


@pytest.mark.asyncio
async def test_generate_remains_inside_effect_intent_wrapper(backend):
    from src.run_causality import bind_run, reset_run
    events = []
    class Recorder:
        def record(self, **event):
            events.append(event)
    token = bind_run("server-session", "server-run", _effect_recorder=Recorder())
    try:
        _, result = await dispatch("generate_image", {"prompt": "synthetic"})
    finally:
        reset_run(token)
    assert result["exit_code"] == 0
    assert [event["state"] for event in events] == ["pending", "confirmed"]
    assert all(event["call_id"] == "server-call" and event["tool"] == "generate_image" for event in events)
