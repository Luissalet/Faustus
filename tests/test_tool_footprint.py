"""Tool catalogue footprint: per-tool cost, per-source totals, name
collisions and near-duplicate descriptions (src/tool_footprint.py)."""

import json
from types import SimpleNamespace

from src import tool_footprint as tf


def chars4(text):
    return (len(text) + 3) // 4 if text else 0


def _desc(name, description, executor="native", schema=None):
    return SimpleNamespace(name=name, description=description, executor=executor,
                           input_schema=schema or {"type": "object", "properties": {"q": {"type": "string"}}})


def test_source_and_short_name():
    assert tf.source_of("mcp:links") == "links"
    assert tf.source_of("native") == "builtin"
    assert tf.source_of("fence") == "builtin"
    assert tf.short_name("mcp__links__search") == "search"
    assert tf.short_name("web_search") == "web_search"


def test_tool_cost_is_measured_on_the_wire_shape():
    row = _desc("read_file", "Read a text file from the workspace.")
    [cost] = tf.tool_costs([row], count=chars4)
    wire = json.dumps(tf.wire_schema(row.name, row.description, row.input_schema),
                      ensure_ascii=False, separators=(",", ":"))
    assert cost["tokens"] == chars4(wire)
    assert cost["description_tokens"] == chars4(row.description)
    assert cost["schema_tokens"] > 0
    assert cost["source"] == "builtin"


def test_report_totals_sources_and_deferred_exposure():
    rows = [
        _desc("bash", "Run a shell command in the workspace and return its output."),
        _desc("rare_tool", "Rarely needed tool that is only loaded on demand by name."),
        _desc("mcp__links__search", "Search saved articles by keyword and tag.", "mcp:links"),
    ]
    exposure = {"bash": "direct", "rare_tool": "deferred"}
    report = tf.footprint_report(rows, count=chars4, exposure_of=exposure.get)
    assert report["count"] == 3
    assert report["total_tokens"] == sum(b["tokens"] for b in report["by_source"])
    rare = next(c for c in report["heaviest"] if c["name"] == "rare_tool")
    assert report["deferred_tokens"] == rare["tokens"]
    assert report["offered_tokens"] == report["total_tokens"] - rare["tokens"]
    sources = {b["source"]: b for b in report["by_source"]}
    assert sources["builtin"]["tools"] == 2 and sources["links"]["tools"] == 1
    assert abs(sum(b["share"] for b in report["by_source"]) - 1.0) < 0.01
    # MCP tools are never asked about built-in exposure.
    links = next(c for c in report["heaviest"] if c["source"] == "links")
    assert links["exposure"] is None
    assert "_words" not in report["heaviest"][0]


def test_name_collisions_across_servers():
    rows = [
        _desc("mcp__links__search", "Search saved articles.", "mcp:links"),
        _desc("mcp__borges__search", "Search indexed documents.", "mcp:borges"),
        _desc("mcp__borges__open", "Open a document.", "mcp:borges"),
    ]
    collisions = tf.footprint_report(rows, count=chars4)["name_collisions"]
    assert len(collisions) == 1
    assert collisions[0]["short_name"] == "search"
    assert collisions[0]["sources"] == ["borges", "links"]


def test_near_duplicates_found_and_ranked():
    rows = [
        _desc("mcp__a__fetch_page", "Fetch a web page by URL and convert the HTML to markdown text.", "mcp:a"),
        _desc("web_fetch", "Fetch a web page by URL and convert its HTML into markdown text."),
        _desc("calendar_add", "Create a calendar event with a title, start time and attendees."),
    ]
    pairs = tf.footprint_report(rows, count=chars4, similarity=0.6)["near_duplicates"]
    assert [(p["a"], p["b"]) for p in pairs] == [("mcp__a__fetch_page", "web_fetch")]
    assert pairs[0]["similarity"] >= 0.6
    assert pairs[0]["same_source"] is False


def test_short_descriptions_are_not_compared():
    rows = [_desc("x_one", "Read a file."), _desc("x_two", "Read a file.")]
    assert tf.footprint_report(rows, count=chars4)["near_duplicates"] == []


def test_oversized_descriptions_listed():
    long_text = "word " * 1200
    rows = [_desc("verbose_tool", long_text), _desc("tidy_tool", "Short and clear description here.")]
    report = tf.footprint_report(rows, count=chars4)
    assert [o["name"] for o in report["oversized_descriptions"]] == ["verbose_tool"]
    assert report["heaviest"][0]["name"] == "verbose_tool"


def test_rows_from_plain_mappings_mcp_and_openai_shapes():
    rows = tf.rows_from_mappings([
        {"name": "search", "description": "Search.", "inputSchema": {"type": "object"}, "server": "links"},
        {"type": "function", "function": {"name": "bash", "description": "Shell.", "parameters": {"type": "object"}}},
        {"description": "no name, skipped"},
        "not a mapping",
    ])
    assert [(r["name"], r["executor"]) for r in rows] == [("mcp__links__search", "mcp:links"), ("bash", "native")]
    report = tf.footprint_report(rows, count=chars4)
    assert {b["source"] for b in report["by_source"]} == {"links", "builtin"}


def test_report_is_deterministic_and_clamps_arguments():
    rows = [_desc(f"t{i}", f"Tool number {i} does something useful with files and folders") for i in range(5)]
    a = tf.footprint_report(rows, count=chars4, top=0, similarity=5)
    b = tf.footprint_report(list(reversed(rows)), count=chars4, top=0, similarity=5)
    assert a["heaviest"] == b["heaviest"]
    assert len(a["heaviest"]) == 1
    assert a["thresholds"]["similarity"] == 1.0


def test_live_builtin_catalogue_report():
    """The real built-in catalogue: every tool costs something and the
    default estimator from the context engine is used."""
    from src.tool_registry import snapshot
    rows = snapshot()
    report = tf.footprint_report(rows, exposure_of=tf.builtin_exposure)
    assert report["count"] == len(rows) > 50
    assert report["total_tokens"] > 0
    assert all(c["tokens"] > 0 for c in report["heaviest"])
    assert report["by_source"][0]["source"] == "builtin"


def test_native_twins_are_hidden_not_collisions():
    rows = [
        _desc("code_graph_flows", "List execution flows through the code graph for a symbol."),
        _desc("mcp__code_graph__code_graph_flows", "List execution flows through the code graph for a symbol.",
              "mcp:code_graph"),
        _desc("reply_to_email", "Reply to an email thread with a drafted message body."),
        _desc("mcp__email__reply_to_email", "Reply to an email thread with a drafted message body.", "mcp:email"),
    ]
    assert tf.native_twin("mcp__code_graph__code_graph_flows") is True
    assert tf.native_twin("mcp__email__reply_to_email") is False
    assert tf.native_twin("code_graph_flows") is False
    report = tf.footprint_report(rows, count=chars4, hidden_of=tf.native_twin)
    assert report["hidden_tools"] == 1
    assert report["total_tokens"] == report["offered_tokens"] + report["deferred_tokens"] + report["hidden_tokens"]
    assert report["by_exposure"]["hidden_twin"]["tools"] == 1
    # The hidden twin repeats its native tool on purpose; the email copy is a real duplicate.
    assert [c["short_name"] for c in report["name_collisions"]] == ["reply_to_email"]
    assert [(p["a"], p["b"]) for p in report["near_duplicates"]] == [("mcp__email__reply_to_email", "reply_to_email")]
    assert all(h["name"] != "mcp__code_graph__code_graph_flows" for h in report["heaviest"])
