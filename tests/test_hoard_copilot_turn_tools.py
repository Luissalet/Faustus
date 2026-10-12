"""A turn from a Hoard's panel: "this" means the app's open objects, and the app's read tools are offered."""

from types import SimpleNamespace

from src.hoard_copilot_turn import (find_origin_server, origin_app_tools, origin_hint_message,
                                    pick_read_tools)


def ctx(**origin):
    return {"origin": origin, "view": {"page": "documents"}, "active_objects": [{"kind": "document", "object_id": "d_1"}]}


def manager(servers):
    return SimpleNamespace(_connections={sid: {"name": name} for sid, (name, _) in servers.items()},
                           _tools={sid: [{"name": n} for n in tools] for sid, (_, tools) in servers.items()})


SERVERS = {
    "a1b2c3d4": ("Kafka's Hoard", ["doc_get", "doc_update", "deadline_get", "deadlines_list", "kafka_status",
                                   "doc_delete", "deadline_add", "docs_search", "doc_extract"]),
    "e5f6a7b8": ("Phileas's Hoard", ["shipment_get", "shipments_list"]),
    "c9d0e1f2": ("Dürer's Hoard", ["sheet_get"]),
    "links": ("Links Hoard", ["link_get"]),
}


def test_the_origin_server_is_found_by_name_or_id_and_accents_do_not_matter():
    names = {sid: name for sid, (name, _) in SERVERS.items()}
    assert find_origin_server(names, ctx(app_id="kafka", app_name="Kafka's Hoard", tool_server_id="kafka")) == "a1b2c3d4"
    assert find_origin_server(names, ctx(app_id="durer")) == "c9d0e1f2"
    assert find_origin_server(names, ctx(tool_server_id="links")) == "links"
    assert find_origin_server(names, ctx(app_id="nobody")) is None
    assert find_origin_server(names, {}) is None
    assert find_origin_server({"x": "Kafka's Hoard", "y": "kafka"}, ctx(app_id="kafka")) is None  # ambiguous: offer nothing


def test_only_read_tools_are_offered_reads_first_and_capped():
    picked = pick_read_tools(SERVERS["a1b2c3d4"][1])
    assert set(picked) == {"doc_get", "deadline_get", "kafka_status", "deadlines_list", "docs_search"}
    assert not {"doc_update", "doc_delete", "deadline_add", "doc_extract"} & set(picked)
    assert len(pick_read_tools([f"thing{i}_get" for i in range(30)])) == 12
    assert pick_read_tools(["printcraft_comment_set_status", "mail_mark_read", "notes_clear"]) == []  # a write verb wins


def test_tools_about_the_open_objects_come_before_unrelated_status_tools():
    names = ["printcraft_status", "printcraft_doc_initial_view", "ocr_status", "scheduler_status", "kafka_overview",
             "doc_get", "docs_list", "deadline_get", "deadlines_list", "mail_status"]
    picked = pick_read_tools(names, limit=6, context=ctx(app_id="kafka"))
    assert picked[:2] == ["doc_get", "docs_list"]  # the open document first, its list included
    assert "kafka_overview" in picked and "printcraft_status" not in picked and "scheduler_status" not in picked
    with_deadline = {**ctx(app_id="kafka"), "active_objects": [{"kind": "document"}, {"kind": "deadline"}]}
    assert {"deadline_get", "deadlines_list"} <= set(pick_read_tools(names, limit=6, context=with_deadline))


def test_turn_tools_are_qualified_and_never_raise():
    mgr = manager(SERVERS)
    tools = origin_app_tools(mgr, ctx(app_id="kafka", app_name="Kafka's Hoard"))
    assert "mcp__a1b2c3d4__doc_get" in tools and all(t.startswith("mcp__a1b2c3d4__") for t in tools)
    assert origin_app_tools(mgr, ctx(app_id="unknown")) == set()
    assert origin_app_tools(object(), ctx(app_id="kafka")) == set()
    assert origin_app_tools(None, None) == set()


def test_the_hint_names_the_app_and_says_what_this_means():
    hint = origin_hint_message(ctx(app_id="kafka", app_name="Kafka's Hoard"))
    assert hint["role"] == "system"
    text = hint["content"]
    assert "Kafka's Hoard" in text and "what I have open" in text and "desktop windows" in text
    assert "no specific object" not in text
    empty = origin_hint_message({"origin": {"app_id": "phileas"}, "view": {"page": "settings"}, "active_objects": []})
    assert "no specific object is open" in empty["content"] and "settings" in empty["content"]
    assert origin_hint_message({}) is None
