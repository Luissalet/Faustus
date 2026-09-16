"""src/rewrite_policy.py — H4, deterministic policy against repeated
whole-file write_file rewrites of the same large existing file in one turn."""
from __future__ import annotations

import pytest

from src.rewrite_policy import (
    RewritePolicy,
    deny_message,
    deny_result,
)

BIG = 151  # one line over the default 150-line floor


def test_first_rewrite_of_a_large_existing_file_is_ok():
    p = RewritePolicy()
    assert p.observe("silhouettes/vector.py", "write_file", BIG) == "ok"


def test_second_rewrite_of_a_large_existing_file_requires_edit():
    """Exactly the CONTRATO.md acceptance case: 1st ok, 2nd require_edit."""
    p = RewritePolicy()
    assert p.observe("silhouettes/vector.py", "write_file", BIG) == "ok"
    assert p.observe("silhouettes/vector.py", "write_file", BIG) == "require_edit"


def test_third_rewrite_still_require_edit_fourth_blocks():
    """Exactly the CONTRATO.md acceptance case: 4th -> block."""
    p = RewritePolicy()
    verdicts = [p.observe("silhouettes/vector.py", "write_file", BIG) for _ in range(4)]
    assert verdicts == ["ok", "require_edit", "require_edit", "block"]


def test_fifth_and_later_rewrites_stay_blocked():
    p = RewritePolicy()
    for _ in range(4):
        p.observe("x.py", "write_file", BIG)
    assert p.observe("x.py", "write_file", BIG) == "block"
    assert p.observe("x.py", "write_file", BIG) == "block"


def test_brand_new_file_never_restricted_no_matter_how_many_times():
    """CONTRATO.md: 'ficheros nuevos ... nunca'. lines_before=0 means the
    file did not exist before this write."""
    p = RewritePolicy()
    for _ in range(10):
        assert p.observe("brand_new_module.py", "write_file", 0) == "ok"


def test_small_existing_file_never_restricted():
    """CONTRATO.md: 'ficheros ... pequeños nunca' — at or under the floor."""
    p = RewritePolicy(min_lines=150)
    for _ in range(10):
        assert p.observe("tiny.py", "write_file", 150) == "ok"  # exactly at the floor


def test_edit_file_never_counts_toward_the_streak():
    """CONTRATO.md: 'apply_patch/edit_file no cuentan'."""
    p = RewritePolicy()
    for _ in range(10):
        assert p.observe("big.py", "edit_file", BIG) == "ok"
    assert p.rewrite_count("big.py") == 0
    # And a surgical edit in between two rewrites does not reset or excuse them.
    assert p.observe("big.py", "write_file", BIG) == "ok"
    assert p.observe("big.py", "edit_file", BIG) == "ok"
    assert p.observe("big.py", "write_file", BIG) == "require_edit"


def test_apply_patch_never_counts_toward_the_streak():
    p = RewritePolicy()
    for _ in range(10):
        assert p.observe("big.py", "apply_patch", BIG) == "ok"
    assert p.rewrite_count("big.py") == 0


def test_counts_are_per_path_independent():
    p = RewritePolicy()
    p.observe("a.py", "write_file", BIG)
    p.observe("a.py", "write_file", BIG)
    assert p.observe("b.py", "write_file", BIG) == "ok"
    assert p.rewrite_count("a.py") == 2
    assert p.rewrite_count("b.py") == 1


def test_path_matching_is_case_and_separator_insensitive():
    p = RewritePolicy()
    p.observe("Silhouettes/Vector.py", "write_file", BIG)
    assert p.observe("silhouettes\\vector.py", "write_file", BIG) == "require_edit"


def test_reset_clears_all_counters_for_a_new_turn():
    p = RewritePolicy()
    p.observe("a.py", "write_file", BIG)
    p.observe("a.py", "write_file", BIG)
    p.reset()
    assert p.rewrite_count("a.py") == 0
    assert p.observe("a.py", "write_file", BIG) == "ok"


def test_a_fresh_instance_per_turn_never_carries_over_state():
    """One instance per turn, mirroring LoopPolicy's contract."""
    turn1 = RewritePolicy()
    turn1.observe("a.py", "write_file", BIG)
    turn1.observe("a.py", "write_file", BIG)
    turn2 = RewritePolicy()
    assert turn2.observe("a.py", "write_file", BIG) == "ok"


def test_off_mode_disables_the_policy_entirely():
    p = RewritePolicy(mode="off")
    for _ in range(10):
        assert p.observe("big.py", "write_file", BIG) == "ok"
    assert p.enabled is False


def test_from_settings_reads_agent_rewrite_policy_and_defaults():
    calls = {}

    def get_setting(key, default=None):
        calls[key] = default
        return {
            "agent_rewrite_policy": "require_edit",
            "agent_rewrite_policy_require_edit_after": 2,
            "agent_rewrite_policy_block_after": 4,
            "agent_rewrite_policy_min_lines": 150,
        }.get(key, default)

    p = RewritePolicy.from_settings(get_setting)
    assert p.mode == "require_edit"
    assert p.require_edit_after == 2
    assert p.block_after == 4
    assert p.min_lines == 150
    assert p.enabled is True


def test_from_settings_off_short_circuits():
    p = RewritePolicy.from_settings(lambda key, default=None: "off" if key == "agent_rewrite_policy" else default)
    assert p.enabled is False
    assert p.observe("big.py", "write_file", BIG) == "ok"


def test_misconfigured_thresholds_are_clamped_monotonic():
    """block_after <= require_edit_after must not skip a rung or divide by
    zero — same defensive posture as LoopPolicy.__post_init__."""
    p = RewritePolicy(require_edit_after=5, block_after=1)
    assert p.block_after > p.require_edit_after
    assert p.require_edit_after >= 1


@pytest.mark.parametrize("verdict", ["require_edit", "block"])
def test_deny_message_names_the_path_and_mentions_edit_tools(verdict):
    msg = deny_message("silhouettes/vector.py", verdict, 3)
    assert "silhouettes/vector.py" in msg
    assert "edit_file" in msg
    assert "apply_patch" in msg


def test_deny_result_shape_matches_the_wiring_contract():
    """H45_wiring.md's write_file refusal shape."""
    result = deny_result("big.py", "require_edit", 2)
    assert result["exit_code"] == 1
    assert result["policy"] == "rewrite_policy"
    assert result["policy_verdict"] == "require_edit"
    assert "error" in result and "big.py" in result["error"]


def test_observe_lines_counts_from_raw_text():
    p = RewritePolicy()
    before = "\n".join(f"line {i}" for i in range(200))
    assert p.observe_lines("big.py", "write_file", before) == "ok"
    assert p.observe_lines("big.py", "write_file", before) == "require_edit"


def test_observe_lines_empty_before_text_is_a_new_file():
    p = RewritePolicy()
    for _ in range(5):
        assert p.observe_lines("new.py", "write_file", None) == "ok"
        assert p.observe_lines("new.py", "write_file", "") == "ok"


def test_snapshot_reports_mode_thresholds_and_counts():
    p = RewritePolicy()
    p.observe("a.py", "write_file", BIG)
    snap = p.snapshot()
    assert snap["mode"] == "require_edit"
    assert snap["enabled"] is True
    assert snap["counts"] == {"a.py": 1}
    assert snap["require_edit_after"] == 2
    assert snap["block_after"] == 4
    assert snap["min_lines"] == 150
