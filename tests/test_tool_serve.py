"""On-demand tool catalog: index, serve, defer schemas without hiding tools."""
import json

from src.tool_serve import (
    LOOKUP_TOOL,
    catalog_entry,
    compact_catalog_block,
    execute_lookup,
    one_liner,
    partition_offer,
    search_catalog,
    serve,
    suggest_close_matches,
)


def test_partition_keeps_hot_seed_and_defers_the_rest():
    hot, deferred = partition_offer(
        {"read_file", "edit_file", "send_email", "bulk_email", LOOKUP_TOOL},
        hot_seed={"read_file", "edit_file"},
        enabled=True,
    )
    assert LOOKUP_TOOL in hot
    assert "read_file" in hot and "edit_file" in hot
    assert "send_email" in deferred and "bulk_email" in deferred
    assert not (hot & deferred)
    assert hot | deferred == {"read_file", "edit_file", "send_email", "bulk_email", LOOKUP_TOOL}


def test_partition_disabled_sends_everything_hot():
    names = {"read_file", "send_email", "bulk_email"}
    hot, deferred = partition_offer(names, hot_seed={"read_file"}, enabled=False)
    assert deferred == set()
    assert names <= hot
    assert LOOKUP_TOOL in hot


def test_partition_never_drops_a_selected_tool():
    selected = {"bash", "web_search", "list_emails", "manage_calendar"}
    hot, deferred = partition_offer(selected, hot_seed={"bash"}, enabled=True)
    assert selected <= (hot | deferred)


def test_one_liner_is_short_and_named():
    text = one_liner("send_email")
    assert "email" in text.lower() or "smtp" in text.lower()
    assert len(text) <= 170


def test_catalog_block_lists_deferred_and_skips_the_helper():
    block = compact_catalog_block(
        {"send_email", "bulk_email", LOOKUP_TOOL, "read_file"},
        disabled={"read_file"},
    )
    assert "- `send_email`" in block
    assert "- `bulk_email`" in block
    assert "- `lookup_tools`" not in block
    assert "`read_file`" not in block
    assert "lookup_tools" in block  # instructions mention the helper


def test_search_by_exact_name_does_not_need_the_index():
    found = search_catalog(names=["send_email", "nope_not_a_tool"], k=8)
    assert found[0] == "send_email"


def test_search_by_query_hits_keyword_hints_without_embedder(monkeypatch):
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: None)
    found = search_catalog("send an email to Alex", k=8)
    assert "send_email" in found


def test_serve_returns_promote_list_and_schemas_for_named_tools():
    payload = serve(names=["ask_user"], detail="schema")
    assert payload["promote"] == ["ask_user"]
    assert payload["tools"][0]["name"] == "ask_user"
    assert "schema" in payload["tools"][0]
    assert payload["tools"][0]["schema"]["function"]["name"] == "ask_user"


def test_execute_lookup_empty_args_lists_categories():
    desc, result = execute_lookup("{}")
    assert result.get("exit_code") == 0
    assert result[LOOKUP_TOOL]["categories"]
    assert LOOKUP_TOOL in desc


def test_execute_lookup_plain_string_is_a_query(monkeypatch):
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: None)
    desc, result = execute_lookup("send email")
    assert result.get("exit_code") == 0
    payload = result[LOOKUP_TOOL]
    assert payload["query"] == "send email"
    assert result["promote"] == payload["promote"]
    data = json.loads(result["output"])
    assert data["promote"] == result["promote"]


def test_execute_lookup_hides_disabled_names():
    _, result = execute_lookup(
        json.dumps({"names": ["send_email", "ask_user"]}),
        ctx={"disabled_tools": {"send_email"}},
    )
    assert result.get("exit_code") == 0
    assert "send_email" not in result["promote"]
    assert "ask_user" in result["promote"]


def test_suggest_close_matches_finds_real_names():
    hits = suggest_close_matches("send_mail", ["send_email", "read_file", "bash"])
    assert "send_email" in hits


def test_catalog_entry_without_schema_is_tiny():
    entry = catalog_entry("read_file", detail="catalog")
    assert set(entry) == {"name", "summary"}
    assert "schema" not in entry


def test_keyword_hits_do_not_depend_on_the_hash_seed():
    """The hint values are sets; ranked by iteration order, `send_email` was
    inside the first eight for "send an email to Alex" on some interpreters
    and not on others. Several seeds, one fresh process each."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = ("from src.tool_serve import _keyword_hits; "
            "print(','.join(_keyword_hits('send an email to Alex')))")
    seen = set()
    for seed in ("0", "1", "7", "123"):
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parents[1]),
            env=dict(os.environ, PYTHONHASHSEED=seed),
            capture_output=True, text=True, timeout=120,
        )
        assert out.returncode == 0, out.stderr[-1500:]
        seen.add(out.stdout.strip())
    assert len(seen) == 1, seen
    assert seen.pop().split(",")[0] == "send_email"


class _LabeledMcp:
    def __init__(self, schemas):
        self.schemas = schemas

    def get_all_openai_schemas(self, _disabled):
        return self.schemas


def _mcp_schema(name, label, description):
    return {"type": "function", "function": {
        "name": name, "description": f"[MCP:{label}] {description}",
        "parameters": {"type": "object", "properties": {}},
    }}


def test_explicit_mcp_label_precedes_generic_session_keyword_hints(monkeypatch):
    target = [
        _mcp_schema("mcp__gh01__gamer_sessions", "GamerHoard", "Consultar historial de sesiones de juego, tiempo y minutos acumulados."),
        _mcp_schema("mcp__gh01__gamer_log_session", "GamerHoard", "Registrar o corregir una sesi\u00f3n de juego."),
        _mcp_schema("mcp__gh01__gamer_get", "GamerHoard", "Consultar ficha de juego y sus datos."),
    ]
    generic = [
        _mcp_schema("mcp__tasks__manage_session", "Task Manager", "Manage task sessions."),
        _mcp_schema("mcp__tasks__manage_tasks", "Task Manager", "Manage tasks."),
    ]
    other = [_mcp_schema(f"mcp__other{i:02d}__tool{i}", f"Other Connector {i}", "Unrelated operation.") for i in range(11)]
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: _LabeledMcp(target + generic + other))
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: type("Index", (), {"retrieve": lambda self, _q, k=8: ["mcp__gh01__stale_session"]})())
    monkeypatch.setattr("src.tool_serve._keyword_hits", lambda _q: ["manage_session", "manage_tasks"])

    found = search_catalog(
        "GamerHoard sesiones aisladas: ficha de juego, registrar sesi\u00f3n, tiempo acumulado", k=8,
    )

    assert found[0] == "mcp__gh01__gamer_log_session"
    assert {"mcp__gh01__gamer_sessions", "mcp__gh01__gamer_get"} <= set(found[:3])
    assert found.index("mcp__gh01__gamer_sessions") < found.index("manage_session")
    assert found[3:] == ["manage_session", "manage_tasks"]
    assert "mcp__gh01__stale_session" not in found


def test_lowercase_gamerhoard_name_still_matches_schema_label(monkeypatch):
    schemas = [
        _mcp_schema("mcp__gh01__gamer_sessions", "GamerHoard isolated", "Consultar sesiones."),
        _mcp_schema("mcp__gh01__gamer_log_session", "GamerHoard isolated", "Registrar una sesión."),
    ]
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: _LabeledMcp(schemas))
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: None)
    monkeypatch.setattr("src.tool_serve._keyword_hits", lambda _q: ["manage_session"])

    found = search_catalog("gamerhoard: sesiones", k=8)

    assert found[:2] == ["mcp__gh01__gamer_log_session", "mcp__gh01__gamer_sessions"]
    found_register = search_catalog("gamerhoard: registrar una sesi\u00f3n", k=8)
    assert found_register[:2] == ["mcp__gh01__gamer_log_session", "mcp__gh01__gamer_sessions"]


def test_generic_server_or_session_words_do_not_pin_mcp_tools(monkeypatch):
    schemas = [_mcp_schema("mcp__local01__manage_session", "Local Session Server", "Manage a local session.")]
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: _LabeledMcp(schemas))
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: type("Index", (), {"retrieve": lambda self, _q, k=8: ["manage_session"]})())
    monkeypatch.setattr("src.tool_serve._keyword_hits", lambda _q: ["manage_session"])

    found = search_catalog("local server sessions", k=8)

    assert found == ["manage_session"]
    assert "mcp__local01__manage_session" not in found


def test_explicit_mcp_search_respects_disabled_and_non_admin_filters(monkeypatch):
    schemas = [
        _mcp_schema("mcp__gh01__gamer_sessions", "GamerHoard", "Consultar sesiones."),
        _mcp_schema("mcp__gh01__gamer_log_session", "GamerHoard", "Registrar sesi\u00f3n."),
    ]
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: _LabeledMcp(schemas))
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: None)
    monkeypatch.setattr("src.tool_serve._keyword_hits", lambda _q: [])
    monkeypatch.setattr("src.tool_serve._non_admin_blocked", lambda name: name == "mcp__gh01__gamer_sessions")
    query = "GamerHoard registrar sesión"

    disabled = search_catalog(query, disabled={"mcp__gh01__gamer_log_session"})
    assert "mcp__gh01__gamer_log_session" not in disabled
    assert "mcp__gh01__gamer_sessions" in disabled

    non_admin = search_catalog(query, admin=False)
    assert "mcp__gh01__gamer_sessions" not in non_admin
    assert "mcp__gh01__gamer_log_session" in non_admin
