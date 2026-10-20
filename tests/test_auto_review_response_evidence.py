"""Evidence follows the review that produced it, including partial failures."""
import asyncio
import json
import sys
import types
from unittest.mock import patch

import pytest
from src import auto_review as review


EVIDENCE = {"source": "http_response", "attempt": 1, "http_status": 200,
    "request": {"controls": {"max_tokens": 1200}}, "response": {"usage": {"input_tokens": 9, "output_tokens": 1200},
        "choices": [{"finish_reason": "length", "channels": {"content": {"present": True, "type": "NoneType"},
            "reasoning": {"present": True, "type": "str", "chars": 4500}}}]}}


@pytest.mark.parametrize("reply", ["", "unparsed fixture", '{"verdict":"ok","summary":"Valid","findings":[]}'])
def test_reviewer_preserves_response_evidence_on_all_parse_outcomes(reply):
    async def completion(**kwargs):
        observer = kwargs.get("_response_observer")
        if observer:
            observer(EVIDENCE)
        return reply
    module = types.ModuleType("src.llm_core"); module.llm_call_async = completion
    with patch.dict(sys.modules, {"src.llm_core": module}):
        parsed, error = asyncio.run(review._call_reviewer(diff="+return a - b", files=["a.py"],
            user_text="Return sum", endpoint_url="https://evidence.invalid/v1", reviewer="fixture",
            headers=None, tests=None, timeout=1, workload="foreground"))
    assert parsed["response_evidence"] == [EVIDENCE]
    assert bool(error) is (reply in ("", "unparsed fixture"))


def test_single_and_compact_keep_error_evidence():
    async def failed(**kwargs):
        return {"verdict": "error", "findings": [], "response_evidence": [EVIDENCE]}, "own empty reply"
    with patch.object(review, "_call_reviewer", failed), patch.object(review, "_setting", side_effect=lambda k,d:d), \
         patch.object(review, "turn_diff", return_value={"diff": "+return a - b", "source": "checkpoint"}):
        result = asyncio.run(review.review_turn(workspace="own-fixture", changed=["a.py"],
            checkpoint_sha="own", user_text="sum", endpoint_url="https://evidence.invalid/v1", model="fixture"))
    assert result["verdict"] == "error"
    assert result["response_evidence"] == review.compact(result)["response_evidence"] == [EVIDENCE]


def test_group_evidence_keeps_each_files_scope_including_failed_group():
    async def reviewer(**kwargs):
        evidence = dict(EVIDENCE, scope_id=kwargs["files"][0])
        return {"verdict": "error", "findings": [], "response_evidence": [evidence]}, "own empty reply"
    async def grouped(*args, **kwargs):
        return [["a.py"], ["b.py"]]
    def setting(key, default):
        return 1 if key == "auto_review_group_threshold_files" else default
    with patch.object(review, "_call_reviewer", reviewer), patch.object(review, "group_files", grouped), \
         patch.object(review, "_setting", side_effect=setting), \
         patch.object(review, "turn_diff", return_value={"diff": "+a\n+b", "source": "checkpoint"}), \
         patch.object(review, "per_file_diffs", return_value={"a.py": "+a", "b.py": "+b"}):
        result = asyncio.run(review.review_turn(workspace="own-fixture", changed=["a.py", "b.py"],
            checkpoint_sha="own", user_text="sum", endpoint_url="https://evidence.invalid/v1", model="fixture"))
    assert result["verdict"] == "error" and result["attempted_groups"] == 2 and result["groups"] == 0
    assert [r["files"] for r in result["response_evidence"]] == [["a.py"], ["b.py"]]
    assert [r["scope_id"] for r in result["response_evidence"]] == ["a.py", "b.py"]
    assert review.compact(result)["response_evidence"] == result["response_evidence"]


def test_staged_review_error_and_markdown_retain_metadata_without_false_clean():
    from src import staged_review as staged
    async def failed(**kwargs):
        return {"verdict": "error", "findings": [], "response_evidence": [EVIDENCE]}, "own empty reply"
    with patch.object(review, "_call_reviewer", failed), patch.object(staged, "collect_diff", return_value={
        "diff": "+return a - b", "files": ["a.py"], "base": "HEAD"}):
        result = asyncio.run(staged.review_worktree("own-fixture", stages=("diff", "model"),
            endpoint_url="https://evidence.invalid/v1", model="fixture"))
    assert result["verdict"] == "error" and result["stages"]["model"]["error"] == "own empty reply"
    assert result["stages"]["model"]["response_evidence"] == [EVIDENCE]
    assert "Model HTTP 200" in result["markdown"] and "Finish: length" in result["markdown"]
    assert "9/1200" in result["markdown"] and "reasoning: 4500" in result["markdown"]
