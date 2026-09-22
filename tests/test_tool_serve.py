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


def test_execute_lookup_rejects_empty_args():
    desc, result = execute_lookup("{}")
    assert result.get("exit_code") == 1
    assert "error" in result
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
