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
    al._sticky_toolset("s", {f"t{i}" for i in range(20)}, None, set())
    fresh, _ = al._sticky_toolset("s", {f"u{i}" for i in range(20)}, None, set())
    assert fresh == {f"u{i}" for i in range(20)}


def test_disabled_tools_never_come_back():
    al._sticky_toolset("s", {"a", "b"}, None, set())
    out, _ = al._sticky_toolset("s", {"a"}, None, {"b"})
    assert out == {"a"}


def test_chats_do_not_share_sets():
    al._sticky_toolset("s1", {"a", "b"}, None, set())
    out, _ = al._sticky_toolset("s2", {"c"}, None, set())
    assert out == {"c"}
