"""Empty searches in the wrong language: manifest field, hint, loop cut, counter."""
import asyncio
import json

import pytest

from src import lifecycle_hooks, plugins, search_language as sl


def minimal(**extra):
    base = {"schema": 1, "id": "demo", "name": "Demo", "mcp": {"command": "python"}}
    base.update(extra)
    return base


# -- manifest -----------------------------------------------------------------

def test_data_language_defaults_to_spanish_and_english():
    assert plugins.parse_manifest(minimal()).data_language == ["es", "en"]


def test_data_language_accepts_text_and_list():
    assert plugins.parse_manifest(minimal(data_language="en, es-ES")).data_language == ["en", "es"]
    assert plugins.parse_manifest(minimal(data_language=["EN"])).data_language == ["en"]


@pytest.mark.parametrize("bad", [5, ["en", 3], "english-language", "e1"])
def test_data_language_rejects_nonsense(bad):
    with pytest.raises(plugins.ManifestError):
        plugins.parse_manifest(minimal(data_language=bad))


def test_shipped_silver_manifest_declares_english_first():
    plugin = plugins.load_plugins()["silver"]
    assert plugin.data_language == ["en", "es"]


# -- what counts as a search and as empty -------------------------------------

@pytest.mark.parametrize("tool,args,expected", [
    ("mcp__abc__pokemon_search", '{"query": "x"}', True),
    ("mcp__abc__find_recipe", '{"name": "x"}', True),
    ("mcp__abc__list_species", '{"name": "pikachu"}', True),
    ("mcp__abc__list_species", '{}', False),
    ("web_search", '{"query": "x"}', False),
    ("grep", '{"pattern": "x"}', False),
    ("mcp__abc__email_search", '{"query": "x"}', False),
    ("read_file", '{"path": "a"}', False),
])
def test_is_data_search(tool, args, expected):
    assert sl.is_data_search(tool, args) is expected


@pytest.mark.parametrize("result,expected", [
    ({"response": ""}, True),
    ({"response": "[]"}, True),
    ({"response": '{"results": [], "count": 0}'}, True),
    ({"response": '{"count": 0}'}, True),
    ({"response": "No results found"}, True),
    ({"response": "Sin resultados para esa consulta"}, True),
    ({"results": []}, True),
    ({"response": '{"results": [{"name": "Shaymin"}], "count": 1}'}, False),
    ({"response": "Shaymin (Sky Forme) - Grass/Flying"}, False),
    ({"error": "boom"}, False),
    ({"response": "", "exit_code": 1}, False),
])
def test_is_empty_result(result, expected):
    assert sl.is_empty_result(result) is expected


# -- the hint and the cut -----------------------------------------------------

@pytest.fixture(autouse=True)
def clean():
    sl.reset_for_tests()
    yield
    sl.reset_for_tests()


def call(tool="mcp__abc__pokemon_search", query="Shaymin Forma Cielo", result=None, session="s1"):
    return {"tool": tool, "content": json.dumps({"query": query}), "session_id": session,
            "result": {"response": "[]"} if result is None else result}


def start_turn(message="Busca Shaymin Forma Cielo en mi Pokédex", session="s1"):
    sl.on_turn_start({"session_id": session, "user_message": message})


def test_first_empty_search_gets_a_hint_with_the_data_language(monkeypatch):
    monkeypatch.setattr(sl, "data_languages", lambda tool: (["en", "es"], "Silver's Hoard"))
    monkeypatch.setattr(sl, "alias_equivalents", lambda text, langs: ["Shaymin Sky Forme"])
    start_turn()
    note = sl.on_post_tool(call())
    assert note and "Shaymin Forma Cielo" in note and "inglés" in note and "Shaymin Sky Forme" in note
    assert "Silver's Hoard" in note
    assert sl.empty_searches("s1") == 1 and sl.take_cut_tools("s1") == set()


def test_hint_follows_the_language_of_the_user(monkeypatch):
    monkeypatch.setattr(sl, "data_languages", lambda tool: (["es"], ""))
    monkeypatch.setattr(sl, "alias_equivalents", lambda text, langs: [])
    start_turn("Look up the dragon with the red wings in my bestiary please")
    note = sl.on_post_tool(call(query="dragon red wings"))
    assert note and note.startswith("[Empty search]") and "Spanish" in note


def test_no_hint_when_the_query_is_already_in_the_only_data_language(monkeypatch):
    monkeypatch.setattr(sl, "data_languages", lambda tool: (["es"], ""))
    start_turn("Busca la receta de paella con arroz bomba")
    assert sl.on_post_tool(call(query="receta de paella con arroz bomba")) is None
    assert sl.empty_searches("s1") == 1  # still counted


def test_second_empty_search_after_the_hint_cuts_the_loop(monkeypatch):
    monkeypatch.setattr(sl, "data_languages", lambda tool: (["en", "es"], "Silver"))
    monkeypatch.setattr(sl, "alias_equivalents", lambda text, langs: [])
    start_turn()
    assert sl.on_post_tool(call())
    note = sl.on_post_tool(call(query="Shaymin Sky Forme"))
    assert note and "Búsqueda vacía repetida" in note and "tras traducir" in note
    assert sl.take_cut_tools("s1") == {"mcp__abc__pokemon_search"}
    assert sl.take_cut_tools("s1") == set()  # each cut is reported once
    assert sl.empty_searches("s1") == 2


def test_same_query_twice_cuts_even_without_a_hint(monkeypatch):
    monkeypatch.setattr(sl, "data_languages", lambda tool: (["es"], ""))
    start_turn("Busca la receta de paella con arroz bomba")
    assert sl.on_post_tool(call(query="paella")) is None
    note = sl.on_post_tool(call(query="Paella!"))
    assert note and "con la misma consulta" in note
    assert sl.take_cut_tools("s1") == {"mcp__abc__pokemon_search"}


def test_a_successful_search_resets_the_streak(monkeypatch):
    monkeypatch.setattr(sl, "data_languages", lambda tool: (["en", "es"], ""))
    monkeypatch.setattr(sl, "alias_equivalents", lambda text, langs: [])
    start_turn()
    assert sl.on_post_tool(call())
    assert sl.on_post_tool(call(result={"response": '{"results": [{"name": "Shaymin"}]}'})) is None
    assert sl.on_post_tool(call(query="otra cosa")) is None  # hinted already, streak is 1 again
    assert sl.take_cut_tools("s1") == set()


def test_other_tools_and_errors_are_ignored(monkeypatch):
    start_turn()
    assert sl.on_post_tool(call(tool="web_search")) is None
    assert sl.on_post_tool(call(result={"error": "timeout"})) is None
    assert sl.empty_searches("s1") == 0


def test_a_new_turn_starts_counting_again(monkeypatch):
    monkeypatch.setattr(sl, "data_languages", lambda tool: (["en", "es"], ""))
    monkeypatch.setattr(sl, "alias_equivalents", lambda text, langs: [])
    start_turn()
    sl.on_post_tool(call())
    assert sl.take_empty_event("s1") == 1 and sl.take_empty_event("s1") == 0
    start_turn()
    assert sl.empty_searches("s1") == 0


def test_alias_hook_is_optional_and_uses_the_service_when_present(monkeypatch):
    assert sl.alias_equivalents("Forma Cielo", ["en"]) == [] or isinstance(sl.alias_equivalents("x", ["en"]), list)
    import sys, types
    module = types.ModuleType("hoard_link.aliases")
    module.equivalents = lambda text, langs: [text, "Sky Forme", "Sky Forme"]
    monkeypatch.setitem(sys.modules, "hoard_link.aliases", module)
    monkeypatch.setitem(sys.modules, "hoard_link", types.ModuleType("hoard_link"))
    assert sl.alias_equivalents("Forma Cielo", ["en"]) == ["Sky Forme"]


# -- through the real agent loop ----------------------------------------------

def test_the_agent_loop_shows_the_hint_and_hides_the_tool_after_two_empty_searches(monkeypatch, tmp_path):
    import src.agent_loop as al

    settings = {}
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: settings.get(key, default), raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(sl, "alias_equivalents", lambda text, langs: ["Shaymin Sky Forme"])
    monkeypatch.setattr(sl, "data_languages", lambda tool: (["en", "es"], "Silver's Hoard"))
    sl.reset_for_tests()

    executed = []

    async def fake_exec(block, *a, **k):
        executed.append(block.content)
        return (block.tool_type, {"response": "[]", "exit_code": 0})

    monkeypatch.setattr(al, "execute_tool_block", fake_exec, raising=False)
    queries = ["Shaymin Forma Cielo", "Shaymin Sky Forme"]
    seen_by_model = []
    state = {"n": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        seen_by_model.append(json.dumps(messages, ensure_ascii=False))
        n = state["n"]; state["n"] += 1
        if n < 2:
            call_ = [{"name": "mcp__abc__pokemon_search", "arguments": {"query": queries[n]}}]
            yield "data: " + json.dumps({"type": "tool_calls", "calls": call_}) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "tool_calls"}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": "No hay nada con ese nombre."}) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "stop"}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)

    async def drive():
        stream = al.stream_agent_loop(
            "http://127.0.0.1:11434/v1", "qwen3.5:9b",
            [{"role": "user", "content": "Busca a Shaymin Forma Cielo en mi Pokédex por favor"}],
            max_rounds=5, relevant_tools={"mcp__abc__pokemon_search"}, security_gate_bypass=True, owner="ada", session_id="s-sl",
            workspace=str(tmp_path), harness_options={"checkpoints": False, "run_tests": False, "repo_map": False})
        return [chunk async for chunk in stream]

    try:
        chunks = asyncio.run(drive())
    finally:
        sl.uninstall()
    assert len(executed) == 2, executed
    # round 2 saw the hint (language of the data + the equivalent name); round 3 saw the cut
    assert "Búsqueda vacía" in seen_by_model[1] and "Shaymin Sky Forme" in seen_by_model[1]
    assert "Búsqueda vacía repetida" in seen_by_model[2]
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {") and '"empty_search"' in c]
    assert [e["empty_searches"] for e in events] == [1, 2]
    assert events[-1]["cut_tools"] == ["mcp__abc__pokemon_search"]


# -- through the lifecycle hooks, as the agent loop calls them ----------------

def test_the_note_reaches_the_model_as_hook_notes(monkeypatch):
    monkeypatch.setattr(sl, "data_languages", lambda tool: (["en", "es"], "Silver"))
    monkeypatch.setattr(sl, "alias_equivalents", lambda text, langs: [])
    sl.install()
    try:
        asyncio.run(lifecycle_hooks.run_async("turn_start", {"session_id": "s9", "user_message": "Busca Shaymin Forma Cielo"}))
        ctx = call(session="s9")
        results = asyncio.run(lifecycle_hooks.run_async("post_tool", ctx))
        result = {"response": "[]"}
        lifecycle_hooks.attach_to_result(result, results)
        assert any("Búsqueda vacía" in n["output"] for n in result["hook_notes"])
        assert sl.take_empty_event("s9") == 1
    finally:
        sl.uninstall()
