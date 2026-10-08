import json
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.agent_loop as agent_loop
import src.tool_index as tool_index
from src.tool_execution import format_tool_result
from src.tool_index import ToolIndex


@pytest.fixture()
def artifact_store_world(tmp_path, monkeypatch):
    from core import database as db_mod
    from src import artifact_store

    engine = create_engine(
        "sqlite:///" + (tmp_path / "task24-offload.db").as_posix(),
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    db_mod.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    yield
    engine.dispose()


def test_cold_keyword_fallback_selects_deliverable_inspection():
    query = "Comprueba si defensa.pptx conserva la plantilla plantilla.pptx"
    selected = ToolIndex.keyword_tools_for_query(query)
    assert "inspect_deliverable" in selected
    assert "inspect_deliverable" not in ToolIndex.keyword_tools_for_query(
        "Prepara una plantilla.pptx para la presentación"
    )


def test_cold_keyword_fallback_preserves_legacy_substring_hints():
    assert "manage_calendar" in ToolIndex.keyword_tools_for_query(
        "show calendars", match_substrings=True,
    )
    # Warm retrieval retains its stricter word-boundary behavior by default.
    assert "manage_calendar" not in ToolIndex.keyword_tools_for_query("show calendars")
    assert "manage_calendar" in ToolIndex.keyword_tools_for_query("show calendar")


@pytest.mark.parametrize("verb", ["inspecciona", "inspeccionad"])
def test_spanish_deliverable_inspection_variants_select_the_inspector(verb):
    assert "inspect_deliverable" in ToolIndex.keyword_tools_for_query(
        f"{verb} defensa.pptx"
    )


def test_api_fenced_tool_diagnostics_only_include_offered_tool_names():
    response = (
        '```inspect_deliverable JSON\n{"path":"defensa.pptx",'
        '"template_path":"plantilla.pptx"}\n```\n'
        '```read_file JSON\n{"path":"notes.txt"}\n```'
    )
    assert agent_loop._scoped_fenced_tool_names(
        response, {"inspect_deliverable"}
    ) == ["inspect_deliverable"]
    assert agent_loop._scoped_fenced_tool_names(response, {"read_file"}) == ["read_file"]
    assert agent_loop._scoped_fenced_tool_names(response, {"glob"}) == []


def test_scoped_fence_distinguishes_example_request_from_mixed_action_request():
    response = (
        "La herramienta lee la estructura del archivo. Ejemplo de sintaxis:\n"
        "```inspect_deliverable\n{\"path\":\"defensa.pptx\"}\n```"
    )
    offered = {"inspect_deliverable"}

    assert agent_loop._scoped_fenced_tool_names(
        response, offered,
        user_request="Explica cómo se inspecciona un PPTX e incluye un ejemplo",
    ) == []
    assert agent_loop._scoped_fenced_tool_names(
        response, offered,
        user_request=(
            "Comprueba si defensa.pptx conserva plantilla.pptx y explica "
            "con un ejemplo de sintaxis"
        ),
    ) == ["inspect_deliverable"]


@pytest.mark.parametrize(
    "user_request",
    [
        "Write example.py to back up this directory",
        "Crea examples.json con estos datos",
        "Ejecuta el script de ejemplo",
        "Show example.py",
        "Muestra examples.json",
        "Show docs/example",
        "Show my-example",
        "How do I call inspect_deliverable? Then inspect defense.pptx",
        "¿Qué formato tiene la llamada a inspect_deliverable? Comprueba defensa.pptx",
    ],
)
def test_action_and_filename_example_words_are_not_documentation(user_request):
    assert not agent_loop._is_tool_documentation_request(user_request)


def test_nested_how_to_verb_is_documentation_but_action_first_wins():
    assert agent_loop._is_tool_documentation_request(
        "Explícame la sintaxis para llamar a inspect_deliverable, con un ejemplo, sin ejecutarlo"
    )
    assert agent_loop._is_tool_documentation_request("Explain example.py with an example")
    assert agent_loop._is_tool_documentation_request("Show an example.")
    assert agent_loop._is_tool_documentation_request(
        "Explica cómo ejecutar X con un ejemplo"
    )
    assert agent_loop._is_tool_documentation_request(
        "Explain how to run X with an example"
    )
    assert not agent_loop._is_tool_documentation_request(
        "Ejecuta X y explica con un ejemplo"
    )
    assert not agent_loop._is_tool_documentation_request(
        "Run X and explain with an example"
    )


@pytest.mark.parametrize("user_request,tool_name", [
    ("Write example.py to back up this directory", "write_file"),
    ("Show example.py", "read_file"),
])
def test_api_loop_retries_file_fence_when_action_names_example_file(monkeypatch, tmp_path, user_request, tool_name):
    offered_per_round = []
    prompts_per_round = []
    executed = []
    provider_round = {"n": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        provider_round["n"] += 1
        prompts_per_round.append("\n".join(
            str(message.get("content") or "")
            for message in messages if isinstance(message, dict)
        ))
        offered_per_round.append([
            schema["function"]["name"]
            for schema in kwargs.get("tools", [])
            if schema.get("function")
        ])
        yield "data: " + json.dumps({
            "delta": '```' + tool_name + ' JSON\n{"path":"example.py","content":"backup"}\n```'
        }) + "\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *_args, **_kwargs):
        executed.append(block.tool_type)
        return block.tool_type, {"output": "unexpected execution", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)
    monkeypatch.setattr(agent_loop, "_agent_route_tool_mode", lambda *a, **k: (True, False, False))
    monkeypatch.setattr(agent_loop, "get_setting", lambda _key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *_a, **_k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda _owner: set())
    monkeypatch.setattr(tool_index, "get_tool_index", lambda: None)

    async def drive():
        return [chunk async for chunk in agent_loop.stream_agent_loop(
            "https://provider.example/v1", "test-model",
            [{"role": "user", "content": user_request}],
            workspace=str(tmp_path), owner="admin", session_id="task24-example-action",
            max_rounds=3, context_length=32768,
            relevant_tools={tool_name},
            harness_options={"checkpoints": False, "run_tests": False, "repo_map": False},
        )]

    chunks = asyncio.run(drive())
    events = [
        json.loads(chunk[6:])
        for chunk in chunks
        if chunk.startswith("data: {")
    ]
    replacements = [event.get("text", "") for event in events if event.get("type") == "response_replace"]

    assert provider_round["n"] == 2
    assert len(offered_per_round) == 2
    assert offered_per_round[0] == offered_per_round[1]
    assert tool_name in offered_per_round[0]
    assert "fenced request for a tool" in prompts_per_round[1]
    assert executed == []
    assert any("This tool call could not be completed" in text for text in replacements)


def test_inspection_formatter_omits_duplicate_payload_and_keeps_other_metadata():
    payload = {"template": {"matches": True}, "slides": 8}
    formatted = format_tool_result(
        "inspect_deliverable",
        {
            "output": json.dumps(payload),
            "deliverable": payload,
            "source": "workspace",
            "exit_code": 0,
        },
        tool="inspect_deliverable",
    )
    assert formatted.count('"matches"') == 1
    assert '"source": "workspace"' in formatted
    assert '"deliverable"' not in formatted


def test_offloaded_inspection_preview_is_deduplicated_and_artifact_survives(artifact_store_world):
    from src import tool_result_offload as offload

    payload = {
        "template_comparison": {"matches": True, "matched_layouts": 7},
        "facts": {"slide_count": 12},
        "padding": "x" * 32000,
    }
    result = {
        "output": json.dumps(payload, ensure_ascii=False, allow_nan=False),
        "deliverable": payload,
        "exit_code": 0,
    }
    assert offload._string_chars(result) > offload.offload_threshold_chars()

    offloaded = offload.offload_if_oversized(
        result, owner="alice", session_id="s-task24", run_id="r-task24",
        call_id="c-large-inspection", tool="inspect_deliverable",
    )
    formatted = format_tool_result("inspect_deliverable", offloaded, tool="inspect_deliverable")

    assert offloaded[offload.OFFLOAD_MARKER] is True
    assert offloaded["artifact_id"] in formatted
    assert formatted.count('"template_comparison"') == 1
    restored = json.loads(offload.read_artifact_range(
        offloaded["artifact_id"], owner="alice", end=offloaded["offload_original_chars"],
    )["text"])
    assert restored["deliverable"] == payload

    altered = dict(offloaded)
    altered["output"] = offloaded["output"] + " altered"
    preserved = format_tool_result("inspect_deliverable", altered, tool="inspect_deliverable")
    assert '"deliverable"' in preserved
    assert preserved.count('"template_comparison"') >= 2


def test_formatter_still_preserves_structured_extras_for_other_tools():
    formatted = format_tool_result(
        "other_tool",
        {"output": "ok", "deliverable": {"slides": 8}, "exit_code": 0},
        tool="other_tool",
    )
    assert '"deliverable"' in formatted


@pytest.mark.parametrize(
    "message,public_error",
    [
        (
            "Responde en español: Comprueba si defensa.pptx conserva la plantilla plantilla.pptx",
            "No se pudo completar esta llamada a la herramienta",
        ),
        (
            "Answer in English: Check if defense.pptx preserves template.pptx",
            "This tool call could not be completed",
        ),
        (
            "Cómo se llama a inspect_deliverable: ejecútalo sobre defensa.pptx",
            "No se pudo completar esta llamada a la herramienta",
        ),
    ],
)
def test_api_provider_loop_retries_fenced_call_once_then_shows_error(
    monkeypatch, tmp_path, message, public_error
):
    offered_per_round = []
    prompts_per_round = []
    executions = []
    provider_round = {"n": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        provider_round["n"] += 1
        prompts_per_round.append("\n".join(
            str(message.get("content") or "")
            for message in messages if isinstance(message, dict)
        ))
        offered_per_round.append([
            schema["function"]["name"]
            for schema in kwargs.get("tools", [])
            if schema.get("function")
        ])
        yield "data: " + json.dumps({
            "delta": '```inspect_deliverable JSON\n{"path":"defensa.pptx"}\n```'
        }) + "\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *_args, **_kwargs):
        executions.append(block.tool_type)
        return block.tool_type, {"output": "should not execute", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)
    monkeypatch.setattr(agent_loop, "_agent_route_tool_mode", lambda *a, **k: (True, False, False))
    monkeypatch.setattr(agent_loop, "get_setting", lambda _key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *_a, **_k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda _owner: set())
    monkeypatch.setattr(tool_index, "get_tool_index", lambda: None)

    async def drive():
        return [chunk async for chunk in agent_loop.stream_agent_loop(
            "https://provider.example/v1", "test-model",
            [{"role": "user", "content": message}],
            workspace=str(tmp_path), owner="admin", session_id="task24",
            max_rounds=3, context_length=32768,
            harness_options={"checkpoints": False, "run_tests": False, "repo_map": False},
        )]

    chunks = asyncio.run(drive())
    events = [
        json.loads(chunk[6:])
        for chunk in chunks
        if chunk.startswith("data: {")
    ]
    replacements = [event.get("text", "") for event in events if event.get("type") == "response_replace"]

    assert provider_round["n"] == 2
    assert len(offered_per_round) == 2
    assert offered_per_round[0] == offered_per_round[1]
    assert "fenced request for a tool" in prompts_per_round[1]
    assert "inspect_deliverable" in offered_per_round[0]
    assert executions == []
    assert any(public_error in text for text in replacements)
    assert all("Cualquier narración antes de tu respuesta final" not in text for text in replacements)
    assert all("[Runtime requirement — reply language]" not in text for text in replacements)


@pytest.mark.parametrize(
    "message,response,expected_prose",
    [
        (
            "Explícame la sintaxis para llamar a inspect_deliverable, con un ejemplo, sin ejecutarlo",
            "La herramienta extrae estructura y texto. Ejemplo de sintaxis:\n"
            "```inspect_deliverable JSON\n{\"path\":\"defensa.pptx\"}\n```",
            "La herramienta extrae estructura y texto.",
        ),
        (
            "What is the syntax of an inspect_deliverable call?",
            "The tool extracts structure and text. Example syntax:\n"
            "```inspect_deliverable JSON\n{\"path\":\"defense.pptx\"}\n```",
            "The tool extracts structure and text.",
        ),
        (
            "¿Cuál es la sintaxis para llamar a inspect_deliverable?",
            "La herramienta extrae estructura y texto. Ejemplo de sintaxis:\n"
            "```inspect_deliverable JSON\n{\"path\":\"defensa.pptx\"}\n```",
            "La herramienta extrae estructura y texto.",
        ),
        (
            "Explica cómo se inspecciona defensa.pptx e incluye un ejemplo de sintaxis.",
            "La herramienta extrae estructura y texto. Ejemplo de sintaxis:\n"
            "```inspect_deliverable\n{\"path\":\"defensa.pptx\"}\n```",
            "La herramienta extrae estructura y texto.",
        ),
        (
            "Explain how to inspect defense.pptx and include a syntax example.",
            "The tool extracts structure and text. Example syntax:\n"
            "```inspect_deliverable\n{\"path\":\"defense.pptx\"}\n```",
            "The tool extracts structure and text.",
        ),
        *[
            (question, ("La herramienta extrae estructura y texto.\n" if question.startswith("¿")
                        else "The tool extracts structure and text.\n") +
             "```inspect_deliverable JSON\n{\"path\":\"defense.pptx\"}\n```",
             "La herramienta extrae estructura y texto." if question.startswith("¿")
             else "The tool extracts structure and text.")
            for question in (
                "¿Cómo se llama a inspect_deliverable? Pon un ejemplo",
                "How do I call inspect_deliverable?",
                "Show me how to call inspect_deliverable",
                "¿Qué formato tiene la llamada a inspect_deliverable?",
            )
        ],
    ],
)
def test_api_loop_preserves_requested_tool_examples_as_documentation(
    monkeypatch, message, response, expected_prose
):
    provider_round = {"n": 0}
    offered_tools = []
    executed = []

    async def fake_stream(_candidates, _messages, **kwargs):
        provider_round["n"] += 1
        offered_tools.append([
            schema["function"]["name"]
            for schema in kwargs.get("tools", [])
            if schema.get("function")
        ])
        yield "data: " + json.dumps({"delta": response}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *_args, **_kwargs):
        executed.append(block.tool_type)
        return block.tool_type, {"output": "unexpected execution", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)
    monkeypatch.setattr(agent_loop, "_agent_route_tool_mode", lambda *a, **k: (True, False, False))
    monkeypatch.setattr(agent_loop, "get_setting", lambda _key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *_a, **_k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda _owner: set())
    monkeypatch.setattr(tool_index, "get_tool_index", lambda: None)

    async def drive():
        return [chunk async for chunk in agent_loop.stream_agent_loop(
            "https://provider.example/v1", "test-model",
            [{"role": "user", "content": message}],
            owner="admin", session_id="task24-docs",
            max_rounds=3, context_length=32768,
            relevant_tools={"inspect_deliverable"},
            harness_options={"checkpoints": False, "run_tests": False, "repo_map": False},
        )]

    chunks = asyncio.run(drive())
    events = [
        json.loads(chunk[6:])
        for chunk in chunks
        if chunk.startswith("data: {")
    ]
    streamed_text = "\n".join(
        str(event.get("delta") or event.get("text") or "") for event in events
    )

    assert provider_round["n"] == 1
    assert offered_tools and "inspect_deliverable" in offered_tools[0]
    assert executed == []
    assert expected_prose in streamed_text
    assert not any(event.get("type") == "provider_tool_format_error" for event in events)
    assert "could not be completed" not in streamed_text
    assert "No se pudo completar esta llamada" not in streamed_text


def test_native_tool_call_with_an_example_completes_without_format_error(monkeypatch):
    provider_round = {"n": 0}
    executed = []
    example = (
        "Voy a comparar los archivos. Ejemplo de sintaxis:\n"
        "```inspect_deliverable\n{\"path\":\"defensa.pptx\"}\n```"
    )

    async def fake_stream(_candidates, _messages, **_kwargs):
        provider_round["n"] += 1
        if provider_round["n"] == 1:
            yield "data: " + json.dumps({"delta": example}) + "\n\n"
            yield "data: " + json.dumps({
                "type": "tool_calls",
                "calls": [{
                    "name": "inspect_deliverable",
                    "arguments": json.dumps({"path": "defensa.pptx"}),
                }],
            }) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": "La plantilla conserva la estructura esperada."}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *_args, **_kwargs):
        executed.append(block.tool_type)
        payload = {"template_comparison": {"matches": True}}
        return block.tool_type, {
            "output": json.dumps(payload, ensure_ascii=False, allow_nan=False),
            "deliverable": payload,
            "exit_code": 0,
        }

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)
    monkeypatch.setattr(agent_loop, "_agent_route_tool_mode", lambda *a, **k: (True, False, False))
    monkeypatch.setattr(agent_loop, "get_setting", lambda _key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *_a, **_k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda _owner: set())

    async def drive():
        return [chunk async for chunk in agent_loop.stream_agent_loop(
            "https://provider.example/v1", "test-model",
            [{
                "role": "user",
                "content": (
                    "Comprueba si defensa.pptx conserva la plantilla plantilla.pptx "
                    "y explica con un ejemplo de sintaxis."
                ),
            }],
            owner="admin", session_id="task24-native-example",
            max_rounds=3, context_length=32768,
            relevant_tools={"inspect_deliverable"},
            harness_options={"checkpoints": False, "run_tests": False, "repo_map": False},
        )]

    chunks = asyncio.run(drive())
    events = [
        json.loads(chunk[6:])
        for chunk in chunks
        if chunk.startswith("data: {")
    ]
    public_text = "\n".join(
        str(event.get("delta") or event.get("text") or "") for event in events
    )

    assert provider_round["n"] == 2
    assert executed == ["inspect_deliverable"]
    assert "provider_tool_format_error" not in public_text
    assert "No se pudo completar esta llamada" not in public_text
    assert "La plantilla conserva la estructura esperada." in public_text
