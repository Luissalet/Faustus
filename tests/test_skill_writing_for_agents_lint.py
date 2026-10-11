"""#325 r2: lint_skill (writing-for-agents) + manual_only + learn-time gate."""
import sys, unittest
sys.path.insert(0, ".")

from src.skill_governance import lint_skill


GOOD = """---
name: fix-flask-mime
description: "Use when Flask serves .mjs as text/html and the browser refuses the module: fix the MIME map."
version: 1.0.0
---

## Procedure

1. Add the .mjs entry to the static MIME map in app startup.
2. Restart the dev server.

## Verification

- curl -I the .mjs asset and expect Content-Type: text/javascript.
"""

NOOP = """---
name: context-only
description: "Use when you need background about the deploy pipeline."
---

## Notes

The deploy pipeline has three stages and they run in order.
"""


class LintSkillTests(unittest.TestCase):
    def test_clean_skill_has_no_findings(self):
        self.assertEqual(lint_skill(GOOD), [])

    def test_noop_body_is_blocking(self):
        findings = lint_skill(NOOP)
        self.assertTrue(any(f["check"] == "no-op" and f["severity"] == "blocking"
                            for f in findings), findings)

    def test_description_without_trigger_is_major(self):
        md = GOOD.replace(
            'description: "Use when Flask serves .mjs as text/html and the browser refuses the module: fix the MIME map."',
            'description: "A compressed guide to Flask MIME handling, well written."')
        findings = lint_skill(md)
        self.assertTrue(any(f["check"] == "description-trigger" and f["severity"] == "major"
                            for f in findings), findings)

    def test_missing_verification_is_blocking(self):
        md = GOOD.replace(
            "- curl -I the .mjs asset and expect Content-Type: text/javascript.\n", "")
        findings = lint_skill(md)
        self.assertTrue(any(f["check"] == "done-criterion" and f["severity"] == "blocking"
                            for f in findings), findings)

    def test_generic_name_is_major(self):
        md = GOOD.replace("name: fix-flask-mime", "name: skill")
        findings = lint_skill(md)
        self.assertTrue(any(f["check"] == "generic-name" for f in findings), findings)

    def test_mojibake_is_major(self):
        md = GOOD.replace("browser", "browÃ©ser")
        findings = lint_skill(md)
        self.assertTrue(any(f["check"] == "mojibake" for f in findings), findings)

    def test_prose_skill_mention_is_major(self):
        md = GOOD.replace("2. Restart the dev server.",
                          "2. Restart the dev server, or see the skill fix-flask-esm for the ESM case.")
        findings = lint_skill(md)
        self.assertTrue(any(f["check"] == "literal-call" for f in findings), findings)

    def test_empty_input_yields_blocking(self):
        findings = lint_skill("")
        self.assertTrue(any(f["severity"] == "blocking" for f in findings), findings)


class ManualOnlySelectorTests(unittest.TestCase):
    def _sm(self, skills):
        class _SM:
            def _filter_candidates(self, skills, min_confidence):
                return list(skills)
            def _load_usage(self):
                return {}
            def _usage_entry(self, usage, name, owner):
                return {}
            def get_relevant_skills(self, query, skills=None, threshold=0.3,
                                    max_items=5, min_confidence=0.0):
                return []
        return _SM()

    def test_manual_only_skill_never_surfaced(self):
        from src.skills_runtime import selector
        selector.reset_selector_state()
        sk = {"id": "secret-recipe", "name": "secret-recipe",
              "description": "Use when baking.", "when_to_use": "",
              "status": "published", "confidence": 0.9,
              "extra": {"manual_only": True}}
        out = selector.select(self._sm(None), None, "baking recipe", skills=[sk])
        self.assertEqual(out, [])

    def test_normal_skill_still_surfaced(self):
        from src.skills_runtime import selector
        selector.reset_selector_state()
        sk = {"id": "fix-flask-mime", "name": "fix-flask-mime",
              "description": "Use when Flask serves .mjs as text/html.",
              "when_to_use": "Use when Flask serves .mjs as text/html.",
              "status": "published", "confidence": 0.9, "extra": {}}
        out = selector.select(self._sm(None), None, "flask mjs mime", skills=[sk])
        self.assertEqual([s["name"] for s in out], ["fix-flask-mime"])


class LearnTimeGateTests(unittest.TestCase):
    def test_blocking_skill_refused_at_learn(self):
        import tempfile, os
        from services.memory.skills import SkillsManager
        with tempfile.TemporaryDirectory() as tmp:
            sm = SkillsManager(data_dir=tmp)
            out = sm.add_skill(
                title="Context only", description="Background about deploys.",
                source="learned", dedupe=False)
            self.assertTrue(out.get("_lint_refused"), out)
            self.assertTrue(any(f["check"] == "no-op" for f in out["_lint_findings"]))

    def test_user_source_never_refused(self):
        import tempfile
        from services.memory.skills import SkillsManager
        with tempfile.TemporaryDirectory() as tmp:
            sm = SkillsManager(data_dir=tmp)
            out = sm.add_skill(
                title="Context only", description="Background about deploys.",
                source="user", dedupe=False)
            self.assertFalse(out.get("_lint_refused"), out)
            self.assertIn("name", out)


if __name__ == "__main__":
    unittest.main()
