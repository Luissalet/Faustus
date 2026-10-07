"""MCP lexical recovery must complement a live but semantically stale index."""

from src import tool_serve
from src.tool_policy import ToolPolicy


def _install_live_printcraft(monkeypatch):
    names = {
        "workflow": "mcp__kafka_printcraft_fixture__printcraft_workflow",
        "artifacts": "mcp__kafka_printcraft_fixture__printcraft_artifacts",
        "status": "mcp__kafka_printcraft_fixture__printcraft_status",
        "other": "mcp__other_connector__unrelated_action",
    }
    schemas = [
        {"type": "function", "function": {"name": names["workflow"],
         "description": "[MCP:Kafka PrintCraft] PrintCraft workflow: edit PDF paragraphs and pages, exportar PDF, save revisions and artifact receipts."}},
        {"type": "function", "function": {"name": names["artifacts"],
         "description": "[MCP:Kafka PrintCraft] List PrintCraft PDF revisions, receipts, artifact hashes and source documents."}},
        {"type": "function", "function": {"name": names["status"],
         "description": "[MCP:Kafka PrintCraft] Check local PrintCraft availability and connected PDF engine."}},
        {"type": "function", "function": {"name": names["other"],
         "description": "[MCP:Other Connector] Perform an unrelated remote action."}},
    ]
    for index in range(8):
        schemas.append({"type": "function", "function": {
            "name": f"mcp__kafka_printcraft_fixture__printcraft_aux_{index}",
            "description": "[MCP:Kafka PrintCraft] PrintCraft workflow receipt helper.",
        }})

    class Manager:
        def get_all_openai_schemas(self, _ctx):
            return schemas

    class PartialIndex:
        # Reproduce a live semantic index which returns valid but unrelated
        # built-ins and has no MCP result for the connected fresh tools.
        def retrieve(self, _query, k=8):
            return [
                "edit_image", "pdf_ops", "create_document", "edit_document",
                "git_status", "todowrite", "ui_control", "inspect_deliverable",
                "image_job", "generate_image", "read_file", "grep",
            ][:k]

    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: Manager())
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: PartialIndex())
    return names


def test_partial_semantic_results_still_recover_printcraft_mcp(monkeypatch):
    names = _install_live_printcraft(monkeypatch)
    result = tool_serve.serve(query="printcraft workflow receipts", k=12)
    assert names["workflow"] in result["promote"]
    assert names["artifacts"] in result["promote"]
    # Native tools complement, rather than erase, the existing built-ins.
    assert "pdf_ops" in result["promote"]
    assert sum(name.startswith("mcp__") for name in result["promote"]) <= 4


def test_export_pdf_query_recovers_connected_exporter_and_pdf_builtin(monkeypatch):
    names = _install_live_printcraft(monkeypatch)
    result = tool_serve.serve(query="exportar PDF", k=12)
    assert names["workflow"] in result["promote"]
    assert "pdf_ops" in result["promote"]


def test_unrelated_generic_query_does_not_add_all_connected_mcp(monkeypatch):
    names = _install_live_printcraft(monkeypatch)
    result = tool_serve.serve(query="calendar week schedule", k=12)
    assert "manage_calendar" in result["promote"]
    assert not any(name.startswith("mcp__") for name in result["promote"])
    assert names["workflow"] not in result["promote"]


def test_generic_mcp_category_lists_connected_servers_and_respects_disabled(monkeypatch):
    names = _install_live_printcraft(monkeypatch)
    payload = tool_serve.serve_categories(category="mcp")
    assert payload["category"] == "mcp"
    assert payload["servers"][0]["name"] == "mcp:kafka_printcraft_fixture"
    assert names["workflow"] in payload["promote"]
    assert names["other"] in payload["promote"]

    disabled = tool_serve.serve_categories(category="mcp", disabled=(names["workflow"],))
    assert names["workflow"] not in disabled["promote"]
    assert names["artifacts"] in disabled["promote"]

    policy_blocked = tool_serve.serve_categories(
        category="mcp", tool_policy=ToolPolicy(disabled_tools=frozenset({names["workflow"]})),
    )
    assert names["workflow"] not in policy_blocked["promote"]
    assert names["artifacts"] in policy_blocked["promote"]


def test_generic_mcp_category_query_filters_to_mcp_without_widening(monkeypatch):
    names = _install_live_printcraft(monkeypatch)
    payload = tool_serve.serve(query="printcraft workflow", category="mcp", k=12)
    assert names["workflow"] in payload["promote"]
    assert "pdf_ops" not in payload["promote"]


def test_generic_mcp_category_keeps_result_cap_and_full_server_count(monkeypatch):
    schemas = [
        {"type": "function", "function": {
            "name": f"mcp__large_plugin__tool_{index:02d}",
            "description": "[MCP:Large Plugin] A connected plugin tool.",
        }}
        for index in range(45)
    ]

    class Manager:
        def get_all_openai_schemas(self, _ctx):
            return schemas

    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: Manager())
    payload = tool_serve.serve_categories(category="mcp")
    assert len(payload["promote"]) == 40
    assert payload["more"] == 5
    assert payload["servers"] == [{
        "name": "mcp:large_plugin", "count": 45,
        "examples": [f"mcp__large_plugin__tool_{index:02d}" for index in range(4)],
    }]


def test_category_budget_counts_only_candidates_that_pass_filter(monkeypatch):
    target = "mcp__cicero__export_pdf"
    schemas = [
        {"type": "function", "function": {
            "name": f"mcp__aaa_exporter__export_pdf_{index:02d}",
            "description": "Export PDF document to a file.",
        }}
        for index in range(8)
    ]
    schemas.append({"type": "function", "function": {
        "name": target,
        "description": "Export PDF document to a file.",
    }})

    class Manager:
        def get_all_openai_schemas(self, _ctx):
            return schemas

    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: Manager())

    class PartialIndex:
        def retrieve(self, _query, k=8):
            return ["edit_image", "pdf_ops", "create_document", "edit_document"][:k]

    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: PartialIndex())
    result = tool_serve.serve(query="export PDF", category="mcp:cicero", k=4)
    assert result["promote"] == [target]
