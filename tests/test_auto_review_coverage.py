"""Grouped reviewer transport fixtures: coverage is distinct from attempts."""
import asyncio
import json
import sys
import types

import pytest

from src import auto_review as review


OK = '{"verdict":"ok","summary":"clean","findings":[]}'
FILES = ["package/core.py", "tests/test_core.py", "examples/demo.md"]


def run_grouped(monkeypatch, answers, *, missing=(), cap=4, files=FILES, big=False, groups=None):
    diffs = {path: f"--- a/{path}\n+++ b/{path}\n@@\n+VALUE = {i}\n"
             for i, path in enumerate(files) if path not in missing}
    if big:
        diffs[files[0]] += "+" + "x" * review.MAX_DIFF_CHARS
    calls = []

    async def provider(**kwargs):
        calls.append(kwargs)
        answer = answers[len(calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    module = types.ModuleType("src.llm_core")
    module.llm_call_async = provider
    monkeypatch.setitem(sys.modules, "src.llm_core", module)
    values = {"auto_review_group_threshold_files": 1, "auto_review_max_groups": cap}
    monkeypatch.setattr(review, "_setting", lambda key, default: values.get(key, default))
    monkeypatch.setattr(review, "turn_diff", lambda *args: {
        "diff": "+initial diff\n", "source": "checkpoint", "truncated": False,
    })
    monkeypatch.setattr(review, "per_file_diffs", lambda *args, **kwargs: diffs)
    monkeypatch.setattr(review, "group_files_deterministic", lambda paths: groups or [[p] for p in paths])
    result = asyncio.run(review.review_turn(
        workspace="own-fixture", changed=files, checkpoint_sha="fixture",
        user_text="Implement the package and CLI with examples.",
        endpoint_url="https://provider.invalid/no-http", model="writer",
        reviewer_model="reviewer", workload="background", timeout_s=1,
    ))
    return result, calls


@pytest.mark.parametrize("answer", [None, "", "not JSON", "{}",
                                     '{"verdict":"ok","findings":"invalid"}',
                                     RuntimeError("own fixture unavailable"), asyncio.TimeoutError()])
def test_failed_group_is_not_successful_coverage_even_when_others_are_ok(monkeypatch, answer):
    result, calls = run_grouped(monkeypatch, [OK, answer, OK])
    assert len(calls) == 3
    assert all(call["max_retries"] == 1 for call in calls)
    assert all(call["workload"] == "background" for call in calls)
    assert result["verdict"] != "ok"
    assert result["error"]
    assert result["attempted_groups"] == 3
    assert result["groups"] == 2
    assert result["group_files"] == [[FILES[0]], [FILES[2]]]
    assert result["not_reviewed"] == [FILES[1]]
    assert result["failed_groups"][0]["files"] == [FILES[1]]
    assert result["failed_groups"][0]["error"]
    assert result["coverage_complete"] is False
    compact = review.compact({**result, "source_epoch": 37})
    for key in ("failed_groups", "not_reviewed", "attempted_groups", "coverage_complete", "source_epoch"):
        assert compact[key] == ({**result, "source_epoch": 37})[key]


def test_grounded_issues_survive_a_different_groups_error(monkeypatch):
    issues = json.dumps({"verdict": "issues", "summary": "wrong value", "findings": [{
        "severity": "error", "file": FILES[0], "line": 1,
        "evidence": "VALUE = 0", "issue": "Wrong value for this fixture.",
    }]})
    result, _ = run_grouped(monkeypatch, [issues, None, OK])
    assert result["verdict"] == "issues"
    assert result["error"] and result["coverage_complete"] is False
    assert result["findings"][0]["grounded"] is True
    assert result["findings"][0]["evidence"] == "VALUE = 0"
    assert review.compact(result)["findings"] == result["findings"]


def test_every_failed_group_stays_identifiable_after_compact(monkeypatch):
    result, calls = run_grouped(monkeypatch, [None, "unparsed", RuntimeError("fixture error")])
    assert len(calls) == 3
    assert result["verdict"] == "error" and result["groups"] == 0
    assert result["group_files"] == [] and result["not_reviewed"] == FILES
    compact = review.compact(result)
    assert len(compact["failed_groups"]) == 3
    assert all(group["error"] for group in compact["failed_groups"])


def test_missing_diffs_and_cap_are_not_silently_counted_as_reviewed(monkeypatch):
    result, calls = run_grouped(monkeypatch, [OK], missing=[FILES[1]], cap=2)
    assert len(calls) == 1
    assert result["missing_diffs"] == [FILES[1]]
    assert set(result["not_reviewed"]) == {FILES[1], FILES[2]}
    assert result["group_files"] == [[FILES[0]]]
    assert result["coverage_complete"] is False
    assert review.compact(result)["missing_diffs"] == [FILES[1]]


def test_group_context_contains_all_changed_files_beyond_cap_and_marks_partial(monkeypatch):
    files = [f"module{i}/file.py" for i in range(45)]
    result, calls = run_grouped(monkeypatch, [OK, OK], cap=2, files=files)
    assert len(calls) == 2
    for i, call in enumerate(calls):
        prompt = call["messages"][0]["content"]
        inventory = prompt.split("<all_changed_files>\n", 1)[1].split("\n</all_changed_files>", 1)[0]
        assert json.loads(inventory) == files
        assert "PARTIAL" in prompt and "not the complete workspace" in prompt
        assert "Do not infer" in prompt
        diff = prompt.split("<diff>\n", 1)[1].split("\n</diff>", 1)[0]
        assert files[i] in diff and files[-1] not in diff
    assert result["attempted_groups"] == 2
    assert len(result["not_reviewed"]) == 43


def test_successful_truncated_prefix_is_not_complete_coverage(monkeypatch):
    result, calls = run_grouped(monkeypatch, [OK, OK, OK], big=True)
    assert len(calls) == 3
    assert result["truncated_files"] == [FILES[0]]
    assert result["partially_reviewed"] == [FILES[0]]
    assert FILES[0] not in result["reviewed_files"]
    assert result["coverage_complete"] is False
    assert review.compact(result)["partially_reviewed"] == [FILES[0]]


def test_complete_successful_groups_keep_existing_ok_verdict(monkeypatch):
    result, calls = run_grouped(monkeypatch, [OK, OK, OK])
    assert len(calls) == result["groups"] == result["attempted_groups"] == 3
    assert result["verdict"] == "ok" and "error" not in result
    assert result["not_reviewed"] == result["failed_groups"] == []
    assert result["reviewed_files"] == FILES and result["coverage_complete"] is True


def test_missing_diff_within_a_successful_group_does_not_gain_coverage(monkeypatch):
    result, calls = run_grouped(monkeypatch, [OK, OK], missing=[FILES[1]],
                                groups=[FILES[:2], FILES[2:]])
    assert len(calls) == 2
    assert result["group_files"] == [[FILES[0]], [FILES[2]]]
    assert result["not_reviewed"] == result["missing_diffs"] == [FILES[1]]
    assert result["coverage_complete"] is False


def test_all_missing_diffs_do_not_call_the_provider_or_claim_success(monkeypatch):
    result, calls = run_grouped(monkeypatch, [], missing=FILES)
    assert calls == []
    assert result["verdict"] == "error" and result["error"]
    assert result["not_reviewed"] == result["missing_diffs"] == FILES
    assert result["groups"] == result["attempted_groups"] == 0
    assert result["coverage_complete"] is False
