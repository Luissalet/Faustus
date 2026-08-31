"""Per-section accounting of the context window (FAUSTUS).

The roadmap item is "agent prompt/context bloat"; the first half of fixing it
is being able to say *which* half of the window went where. These pin the
classification (the part that silently rots when a new context source appears),
the arithmetic, and the advice thresholds.
"""

from src.context_ledger import (build_ledger, classify, should_emit,
                                summary_line)


def untrusted(source, text="x" * 100):
    return {"role": "user", "content": text,
            "metadata": {"trusted": False, "source": source}}


def big(role, n=1000):
    return {"role": role, "content": "y" * n}


class TestClassify:
    def test_roles_that_speak_for_themselves(self):
        assert classify({"role": "system", "content": "hi"}) == "system"
        assert classify({"role": "tool", "content": "out"}) == "tool_results"

    def test_untrusted_sources_map_to_their_section(self):
        cases = {
            "skill under test": "skills",
            "Memory": "memory",
            "repository map": "instructions",
            "web search results": "web",
            "youtube transcript": "web",
            "personal documents": "documents",
            "files the user pointed at with @": "documents",
            "attachment: photo.png": "attachments",
        }
        for source, expected in cases.items():
            assert classify(untrusted(source)) == expected, source

    def test_unknown_source_lands_in_retrieved_not_conversation(self):
        """A new context source must show up as retrieved bulk, not hide inside
        the chat history where nobody would look for it."""
        assert classify(untrusted("background job output")) == "retrieved"

    def test_only_the_real_last_user_message_is_the_question(self):
        assert classify({"role": "user", "content": "q"}, is_last_user=True) == "user"
        assert classify({"role": "user", "content": "q"}) == "conversation"
        # Retrieved context wears the user role too — it is not the question.
        assert classify(untrusted("webpage"), is_last_user=True) == "web"

    def test_garbage_never_raises(self):
        assert classify(None) == "conversation"
        assert classify({"role": "user", "metadata": "not-a-dict"}) == "conversation"


class TestBuild:
    def test_sections_are_sorted_and_empty_ones_dropped(self):
        led = build_ledger([
            {"role": "system", "content": "s" * 100},
            untrusted("Memory", "m" * 4000),
            {"role": "user", "content": "hello"},
        ])
        keys = [s["key"] for s in led["sections"]]
        assert keys[0] == "memory"
        assert "tool_results" not in keys
        assert led["total"] == sum(s["tokens"] for s in led["sections"])

    def test_tool_schemas_are_counted_even_though_they_are_not_messages(self):
        tools = [{"type": "function", "function": {"name": f"t{i}",
                  "description": "d" * 200, "parameters": {}}} for i in range(20)]
        with_tools = build_ledger([{"role": "user", "content": "hi"}], tools)
        without = build_ledger([{"role": "user", "content": "hi"}])
        assert with_tools["tool_count"] == 20
        assert with_tools["total"] > without["total"] * 5
        assert any(s["key"] == "tools" for s in with_tools["sections"])

    def test_percentages_are_of_the_window_when_known(self):
        led = build_ledger([big("user", 10000)], context_length=10000)
        assert led["context_pct"] == round(led["total"] * 100.0 / 10000, 1)

    def test_unknown_window_disables_only_the_percentage(self):
        led = build_ledger([big("user", 500)])
        assert led["context_pct"] is None
        assert led["total"] > 0

    def test_survives_empty_and_malformed_input(self):
        assert build_ledger(None)["total"] == 0
        assert build_ledger([None, "nope", 7])["total"] == 0
        led = build_ledger([{"role": "user", "content": "hi"}], [object()])
        assert led["tool_count"] == 1  # unserializable schemas still get sized


class TestAdvice:
    def _keys(self, led):
        return {a["key"] for a in led["advice"]}

    def test_tool_schemas_eating_the_window_is_called_out(self):
        tools = [{"name": f"t{i}", "description": "d" * 400} for i in range(30)]
        led = build_ledger([{"role": "user", "content": "hi"}], tools,
                           context_length=8192)
        assert "tools" in self._keys(led)

    def test_a_lean_prompt_gets_no_advice_at_all(self):
        led = build_ledger([{"role": "system", "content": "be nice"},
                            {"role": "user", "content": "hi"}],
                           context_length=131072)
        assert led["advice"] == []

    def test_retrieved_bulk_is_called_out_separately(self):
        led = build_ledger([untrusted("personal documents", "d" * 30000),
                            {"role": "user", "content": "hi"}],
                           context_length=16384)
        assert "retrieved" in self._keys(led)

    def test_about_to_overflow_beats_the_small_window_note(self):
        led = build_ledger([big("user", 40000)], context_length=12000)
        keys = self._keys(led)
        assert "total" in keys and "small_window" not in keys

    def test_small_window_note_only_when_the_window_is_small(self):
        msgs = [big("assistant", 30000), {"role": "user", "content": "hi"}]
        assert "small_window" in self._keys(build_ledger(msgs, context_length=16384))
        assert "small_window" not in self._keys(build_ledger(msgs, context_length=131072))


class TestEmissionThrottle:
    def test_first_round_always_reports(self):
        assert should_emit(None, {"total": 10}) is True

    def test_a_flat_second_round_stays_quiet(self):
        led = {"total": 1000, "context_pct": 10}
        assert should_emit(led, dict(led)) is False

    def test_real_growth_reports_again(self):
        assert should_emit({"total": 1000}, {"total": 1300, "context_pct": 20}) is True

    def test_pressure_always_reports_even_without_growth(self):
        assert should_emit({"total": 1000}, {"total": 1000, "context_pct": 88}) is True

    def test_no_ledger_no_event(self):
        assert should_emit(None, {}) is False


def test_summary_line_is_log_sized():
    led = build_ledger([big("user", 4000)], context_length=8192)
    line = summary_line(led)
    assert "/8192" in line and "%" in line
    assert len(line) < 120
