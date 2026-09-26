"""Follow-up rounds of an agent turn may think with a smaller budget
(src/round_reasoning.py); the first round and any round after trouble keep
the full one, and the feature is off unless the setting is set."""

from src import round_reasoning as rr


def _settings(cap):
    return lambda key, default=None: cap if key == rr.SETTING_KEY else default


BASE = {"think": True, "reasoning_budget": 4096}


def test_off_by_default_changes_nothing():
    out, cap = rr.followup_overrides(BASE, round_num=5, trouble=False, pinned=False,
                                     get_setting=_settings(0))
    assert out == BASE and cap is None


def test_clean_followup_round_is_capped_without_mutating_input():
    src = dict(BASE)
    out, cap = rr.followup_overrides(src, round_num=3, trouble=False, pinned=False,
                                     get_setting=_settings(1536))
    assert cap == 1536 and out["reasoning_budget"] == 1536 and out["think"] is True
    assert src == BASE


def test_first_round_trouble_and_pinned_keep_full_budget():
    for kw in ({"round_num": 1, "trouble": False, "pinned": False},
               {"round_num": 4, "trouble": True, "pinned": False},
               {"round_num": 4, "trouble": False, "pinned": True}):
        out, cap = rr.followup_overrides(BASE, get_setting=_settings(1024), **kw)
        assert cap is None and out["reasoning_budget"] == 4096


def test_never_raises_a_budget_and_ignores_thinking_off():
    low = {"think": True, "reasoning_budget": 512}
    assert rr.followup_overrides(low, round_num=3, trouble=False, pinned=False,
                                 get_setting=_settings(2048))[1] is None
    off = {"think": False}
    assert rr.followup_overrides(off, round_num=3, trouble=False, pinned=False,
                                 get_setting=_settings(2048)) == (off, None)
    assert rr.followup_overrides(None, round_num=3, trouble=False, pinned=False,
                                 get_setting=_settings(2048)) == (None, None)


def test_budgetless_thinking_gets_the_cap():
    out, cap = rr.followup_overrides({"think": True}, round_num=2, trouble=False,
                                     pinned=False, get_setting=_settings(2048))
    assert cap == 2048 and out["reasoning_budget"] == 2048


def test_tool_result_trouble_classifier():
    assert rr.tool_result_is_trouble({"error": "boom"})
    assert rr.tool_result_is_trouble({"exit_code": 2, "output": ""})
    assert rr.tool_result_is_trouble({"blocked": True})
    assert rr.tool_result_is_trouble({"success": False})
    assert rr.tool_result_is_trouble({"argument_errors": [{"field": "x"}]})
    assert not rr.tool_result_is_trouble({"exit_code": 0, "output": "ok"})
    assert not rr.tool_result_is_trouble({"content": "text", "error": ""})
    assert not rr.tool_result_is_trouble({"exit_code": False})
    assert not rr.tool_result_is_trouble("not a dict")


def test_harness_note_since_last_assistant():
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "tool", "content": "ok"}]
    assert not rr.harness_note_pending(msgs)
    msgs.append({"role": "user", "_harness_note": True, "content": "verify"})
    assert rr.harness_note_pending(msgs)
    msgs.append({"role": "assistant", "content": "done"})
    assert not rr.harness_note_pending(msgs)
    assert not rr.harness_note_pending(None)
