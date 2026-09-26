"""A chat keeps last turn's tool set while it covers the new turn
(src/agent_loop.py `_sticky_toolset`): the tool list sits at the top of the
prompt, so a different list costs a full reprocess of the history."""
import src.agent_loop as al


def test_a_covered_turn_reuses_the_previous_set():
    first, _ = al._sticky_toolset("s", {"a", "b", "c"}, {"a"}, set())
    second, hot = al._sticky_toolset("s", {"a", "b"}, {"a"}, set())
    assert first == second == {"a", "b", "c"} and hot == {"a"}


def test_a_turn_that_needs_more_widens_it_while_small():
    al._sticky_toolset("s", {"a", "b"}, {"a"}, set())
    widened, hot = al._sticky_toolset("s", {"a", "x"}, {"x"}, set())
    assert widened == {"a", "b", "x"} and hot == {"a", "x"}
    again, _ = al._sticky_toolset("s", {"b"}, set(), set())
    assert again == {"a", "b", "x"}


def test_past_the_cap_the_turn_starts_over():
    al._sticky_toolset("s", {f"t{i}" for i in range(30)}, None, set())
    fresh, _ = al._sticky_toolset("s", {f"u{i}" for i in range(30)}, None, set())
    assert fresh == {f"u{i}" for i in range(30)}


def test_disabled_tools_never_come_back():
    al._sticky_toolset("s", {"a", "b"}, None, set())
    out, _ = al._sticky_toolset("s", {"a"}, None, {"b"})
    assert out == {"a"}


def test_chats_do_not_share_sets():
    al._sticky_toolset("s1", {"a", "b"}, None, set())
    out, _ = al._sticky_toolset("s2", {"c"}, None, set())
    assert out == {"c"}


def test_sets_survive_a_restart(tmp_path, monkeypatch):
    path = tmp_path / "session_toolsets.json"
    monkeypatch.setattr(al, "_session_toolsets_path", lambda: str(path))
    al._SESSION_TOOLSETS.clear()
    monkeypatch.setattr(al, "_SESSION_TOOLSETS_LOADED", True)
    al._sticky_toolset("keep-me", {"a", "b", "c"}, {"a"}, set())
    assert path.exists()
    # A fresh process: nothing in memory, the file still there.
    al._SESSION_TOOLSETS.clear()
    monkeypatch.setattr(al, "_SESSION_TOOLSETS_LOADED", False)
    out, hot = al._sticky_toolset("keep-me", {"a"}, {"a"}, set())
    assert out == {"a", "b", "c"} and hot == {"a"}


def test_the_cap_is_a_setting(monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda k, d=None: 45 if k == "agent_sticky_toolset_max" else d, raising=False)
    al._sticky_toolset("s", {f"t{i}" for i in range(20)}, None, set())
    widened, _ = al._sticky_toolset("s", {f"u{i}" for i in range(20)}, None, set())
    assert len(widened) == 40


def test_plugin_tools_only_retrieval_picked_do_not_widen_a_follow_up():
    al._sticky_toolset("s", {"read_file", "inspect_image", "mcp__a__x"}, {"inspect_image"}, set())
    out, hot = al._sticky_toolset("s", {"read_file", "inspect_image", "mcp__b__y", "mcp__b__z"},
                                  {"inspect_image", "mcp__b__y"}, set(),
                                  optional={"mcp__b__y", "mcp__b__z", "mcp__a__x"})
    assert out == {"read_file", "inspect_image", "mcp__a__x"} and hot == {"inspect_image"}
    # A built-in tool the turn needs still widens it.
    out2, _ = al._sticky_toolset("s", {"read_file", "write_file", "mcp__b__y"}, None, set(),
                                 optional={"mcp__b__y"})
    assert "write_file" in out2 and "mcp__b__y" not in out2


def test_the_first_turn_keeps_what_retrieval_found():
    out, _ = al._sticky_toolset("fresh", {"read_file", "mcp__b__y"}, None, set(), optional={"mcp__b__y"})
    assert out == {"read_file", "mcp__b__y"}


def test_weak_plugin_picks_already_in_the_set_are_not_promoted_to_schemas():
    # An earlier turn left mcp__b__y in the set but deferred (not hot).
    al._sticky_toolset("s", {"read_file", "inspect_image", "mcp__b__y"}, {"inspect_image"}, set())
    out, hot = al._sticky_toolset("s", {"read_file", "inspect_image", "mcp__b__y"},
                                  {"inspect_image", "mcp__b__y"}, set(), optional={"mcp__b__y"})
    assert out == {"read_file", "inspect_image", "mcp__b__y"} and hot == {"inspect_image"}


def test_a_turn_that_starts_over_leaves_weak_picks_out(monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda k, d=None: 4 if k == "agent_sticky_toolset_max" else d, raising=False)
    al._sticky_toolset("s", {"a", "b", "c"}, None, set())
    out, _ = al._sticky_toolset("s", {"x", "y", "mcp__w__z"}, None, set(), optional={"mcp__w__z"})
    assert out == {"x", "y"}


def test_a_covered_turn_keeps_the_same_full_schemas():
    al._sticky_toolset("s", {"a", "b", "c"}, {"a"}, set())
    out, hot = al._sticky_toolset("s", {"a", "b"}, {"a", "b"}, set())
    assert out == {"a", "b", "c"} and hot == {"a"}
