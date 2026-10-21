"""Audit judges receive the same complete skill contract as the executor."""
import ast
import asyncio
import json
import logging
import re
import sys
import types
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
JUDGES = ('_eval_skill_run', '_eval_skill_necessity', '_eval_skill_retrieval_precision')


def judge_functions():
    tree = ast.parse((ROOT / 'routes/skills_routes.py').read_bytes())
    nodes = [node for node in tree.body
             if isinstance(node, ast.AsyncFunctionDef) and node.name in JUDGES]
    assert len(nodes) == len(JUDGES)
    scope = {'Optional': Optional, 'logger': logging.getLogger(__name__),
             '_VERDICT_PROSE_RE': re.compile(r'never-match-this-fixture')}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'skills_routes.py [actual judges]', 'exec'), scope)
    return scope


class AuditContextTests(unittest.TestCase):
    def call(self, name, skill, others=None, *, retry=False, transcript='actual test transcript'):
        calls = []
        replies = (['not valid JSON'] if retry else []) + [json.dumps({
            'verdict': 'pass', 'confidence': .8, 'summary': 'CPU fixture only',
            'issues': [], 'necessary': True, 'redundant_with': [], 'reason': 'Specific', 'ok': True,
        })]

        async def capture(url, model, messages, **kwargs):
            calls.append((url, model, messages, kwargs))
            return replies.pop(0)

        transport = types.ModuleType('src.llm_core')
        transport.llm_call_async = capture
        functions = judge_functions()
        arguments = (skill, 'Do the supplied task', transcript) if name == JUDGES[0] else (skill, others or [])
        with patch.dict(sys.modules, {'src.llm_core': transport}):
            result = asyncio.run(functions[name](*arguments, 'unused-url', 'global-model', {'X-Fixture': 'cpu'}))
        self.assertIsNotNone(result)
        for url, model, _, kwargs in calls:
            self.assertEqual((url, model, kwargs['headers']),
                             ('unused-url', 'global-model', {'X-Fixture': 'cpu'}))
        return calls

    def test_complete_unicode_skill_and_final_contract_reach_every_judge(self):
        skill = '---\nname: fixture\n---\n' + ('Exact rule Ω🧭\n' * 500)
        skill += '\n## Verification\n' + json.dumps({'final_vector': [0, 255, 'é']}, ensure_ascii=False)
        for name in JUDGES:
            with self.subTest(judge=name):
                content = self.call(name, skill)[0][2][1]['content']
                self.assertIn(skill + '\n\n===', content)

    def test_complete_catalog_descriptions_and_records_reach_both_catalog_judges(self):
        catalogs = [
            [{'name': f'wide-{i}', 'description': ('exact boundary Ω ' * 400) + f'END-{i}'} for i in range(2)],
            [{'name': f'short-{i}', 'description': f'Contract {i}'} for i in range(85)],
        ]
        for name in JUDGES[1:]:
            for catalog in catalogs:
                with self.subTest(judge=name, catalog_size=len(catalog)):
                    content = self.call(name, 'Short specific skill', catalog)[0][2][1]['content']
                    expected = '\n'.join(f"- {row['name']}: {row['description']}" for row in catalog)
                    self.assertIn(expected, content)

    def test_short_empty_and_missing_skills_remain_accepted(self):
        for name in JUDGES:
            for skill in (None, '', 'A short precise contract'):
                with self.subTest(judge=name, skill=skill):
                    self.assertIn(skill or '', self.call(name, skill)[0][2][1]['content'])

    def test_run_retry_keeps_context_and_generation_policy(self):
        skill = 'Complete rule Ω\n' * 500
        calls = self.call(JUDGES[0], skill, retry=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2][1], calls[1][2][1])
        for _, _, messages, kwargs in calls:
            self.assertIn(skill, messages[1]['content'])
            self.assertEqual((kwargs['temperature'], kwargs['max_tokens'], kwargs['timeout']), (.1, 32768, 180))

    def test_transcript_clip_remains_explicit_and_keeps_both_ends(self):
        transcript = 'BEGIN-TRANSCRIPT\n' + ('m' * 25000) + '\nEND-TRANSCRIPT'
        content = self.call(JUDGES[0], 'Short skill', transcript=transcript)[0][2][1]['content']
        self.assertIn('BEGIN-TRANSCRIPT', content)
        self.assertIn('END-TRANSCRIPT', content)
        self.assertIn('[transcript trimmed for length]', content)


if __name__ == '__main__':
    unittest.main()
