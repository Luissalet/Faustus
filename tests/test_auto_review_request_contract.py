"""The reviewer receives the complete current request, without live providers."""
import asyncio
import json
import sys
import types
import unittest
from unittest.mock import patch

from src import auto_review as review


# A short contract with its CLI requirements deliberately after the old cut.
# The CLI wording comes from the Windows KV acceptance task; no external file
# or project data is needed to run these regressions.
CLI = (
    "## CLI estable\n"
    "Key/value son base64 estándar de bytes, con validación estricta.\n"
    "stdout contiene exactamente un objeto JSON ASCII (ensure_ascii=True).\n"
    'count devuelve {"count":N}.\n'
)
REQUEST = "Construye un almacén persistente.\n" + "Detalles de la API.\n" * 190 + CLI + "\nFin: acción, 日本語, 🧪.\n"
DIFF = '+print("debug")\n'


def request_in(prompt):
    return prompt.split("<user_request>\n", 1)[1].split("\n</user_request>", 1)[0]


class RequestContractTests(unittest.TestCase):
    def test_prompt_preserves_cli_after_3000_and_unicode_final_line(self):
        self.assertGreater(REQUEST.index(CLI), 3000)
        prompt = review._prompt(REQUEST, DIFF, ["kvstore.py"], None)
        self.assertEqual(request_in(prompt), REQUEST)
        self.assertIn(CLI, prompt)
        self.assertTrue(request_in(prompt).endswith("Fin: acción, 日本語, 🧪.\n"))
        self.assertIn("<diff>\n" + DIFF, prompt)

    def test_single_group_provider_receives_whole_request_and_grounded_tail(self):
        calls = []
        async def provider(**kwargs):
            calls.append(kwargs)
            self.assertEqual(request_in(kwargs["messages"][0]["content"]), REQUEST)
            return json.dumps({"verdict": "issues", "summary": "CLI contract violated", "findings": [{
                "severity": "error", "file": "kvstore.py", "line": 1,
                "evidence": "stdout contiene exactamente un objeto JSON ASCII",
                "issue": "Debug output violates the CLI response contract.",
            }]})
        module = types.ModuleType("src.llm_core")
        module.llm_call_async = provider
        with patch.dict(sys.modules, {"src.llm_core": module}), patch.object(
            review, "_setting", side_effect=lambda key, default: default
        ), patch.object(review, "turn_diff", return_value={
            "diff": DIFF, "source": "checkpoint", "truncated": False
        }):
            result = asyncio.run(review.review_turn(
                workspace="own-fixture", changed=["kvstore.py"], checkpoint_sha="fixture",
                user_text=REQUEST, endpoint_url="https://provider.invalid/no-http",
                model="writer", reviewer_model="reviewer", tests={"ran": True, "ok": True},
            ))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "reviewer")
        self.assertEqual([m["role"] for m in calls[0]["messages"]], ["user"])
        self.assertEqual(calls[0]["max_retries"], 1)
        self.assertEqual(calls[0]["max_tokens"], 0)
        self.assertEqual(result["verdict"], "issues")
        self.assertTrue(result["findings"][0]["grounded"])
        self.assertEqual(result["findings"][0]["severity"], "error")
        self.assertFalse(result["truncated"])

    def test_empty_request_keeps_the_existing_empty_section(self):
        for request in (None, ""):
            with self.subTest(request=request):
                self.assertEqual(request_in(review._prompt(request, DIFF, [], None)), "")

    def test_partial_group_scope_preserves_request_tests_and_local_diff(self):
        changed = ["package/core.py", "tests/test_core.py", "examples/demo.md"]
        prompt = review._prompt(REQUEST, DIFF, [changed[-1]],
                                {"ran": True, "ok": True, "summary": "own fixture"}, changed)
        self.assertEqual(request_in(prompt), REQUEST)
        self.assertIn("PARTIAL", prompt)
        self.assertIn("not the complete workspace", prompt)
        self.assertIn("Project tests after the change: PASSED (own fixture)", prompt)
        self.assertIn("<changed_files>\nexamples/demo.md\n</changed_files>", prompt)
        inventory = prompt.split("<all_changed_files>\n", 1)[1].split("\n</all_changed_files>", 1)[0]
        self.assertEqual(json.loads(inventory), changed)
        self.assertIn("<diff>\n" + DIFF, prompt)

    def test_single_group_prompt_keeps_existing_scope(self):
        prompt = review._prompt(REQUEST, DIFF, ["kvstore.py"], None)
        self.assertNotIn("<all_changed_files>", prompt)
        self.assertNotIn("group-specific", prompt)

    def test_legacy_verdict_inference_retains_valid_findings(self):
        parsed = review._parse(json.dumps({"findings": [{
            "severity": "error", "file": "kvstore.py", "evidence": "debug",
            "issue": "Own fixture defect",
        }]}))
        self.assertEqual(parsed["verdict"], "issues")
        self.assertEqual(parsed["findings"][0]["issue"], "Own fixture defect")

    def test_reviewer_selection_off_same_and_explicit_override_are_unchanged(self):
        with patch.object(review, "_setting", return_value="off"):
            self.assertEqual(review.resolve_reviewer_with_reason("writer"), (None, None))
            model, reason = review.resolve_reviewer_with_reason("writer", "same", available_models=["writer"])
            self.assertIsNone(model)
            self.assertTrue(reason)
            self.assertEqual(review.resolve_reviewer_with_reason("writer", "same", available_models=[]), ("writer", None))
            self.assertEqual(review.resolve_reviewer_with_reason("writer", "writer", available_models=["writer"]), ("writer", None))

    def test_provider_errors_are_errors_and_unparsed_is_not_ok(self):
        for answer in (None, "not a JSON review", RuntimeError("own provider failure"), asyncio.TimeoutError()):
            with self.subTest(answer=repr(answer)):
                async def provider(**kwargs):
                    if isinstance(answer, Exception):
                        raise answer
                    return answer
                module = types.ModuleType("src.llm_core")
                module.llm_call_async = provider
                with patch.dict(sys.modules, {"src.llm_core": module}):
                    result, error = asyncio.run(review._call_reviewer(
                        diff=DIFF, files=["kvstore.py"], user_text=REQUEST,
                        endpoint_url="https://provider.invalid/no-http", reviewer="writer",
                        headers=None, tests=None, timeout=1, workload="foreground",
                    ))
                if answer == "not a JSON review":
                    self.assertEqual(result["verdict"], "unparsed")
                else:
                    self.assertEqual(result["verdict"], "error")
                    self.assertTrue(error)

    def test_unseen_evidence_still_cannot_become_a_grounded_error(self):
        result = review.ground_findings([{
            "severity": "error", "evidence": "invented contract that was never requested",
            "issue": "Invented defect", "file": "kvstore.py", "line": 1,
        }], DIFF, REQUEST)
        self.assertEqual(result["ungrounded"], 1)
        self.assertFalse(result["findings"][0]["grounded"])
        self.assertEqual(result["findings"][0]["severity"], "warning")


if __name__ == "__main__":
    unittest.main()
