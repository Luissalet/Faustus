"""Real audit branch and Markdown parser, with explicit RAM-only collaborators."""
import ast
import asyncio
import copy
import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FORMAT_NAME = 'services.memory.skill_format'
spec = importlib.util.spec_from_file_location(FORMAT_NAME, ROOT / 'services/memory/skill_format.py')
format_module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {FORMAT_NAME: format_module}):
    spec.loader.exec_module(format_module)
Skill = format_module.Skill


def audit_functions():
    tree = ast.parse((ROOT / 'routes/skills_routes.py').read_bytes())
    names = {'_audit_one_skill', '_apply_skill_md', '_audit_phase'}
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {n.name for n in nodes} == names
    scope = {'logger': logging.getLogger(__name__)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'skills_routes.py [actual isolated functions]', 'exec'), scope)
    return scope


class Manager:
    def __init__(self, skill):
        self.skill = copy.deepcopy(skill)
        self.updates = []

    def read_skill_md(self, name, **kwargs):
        return self.skill.to_markdown()

    def load(self, **kwargs):
        return [self.skill.to_dict()]

    def update_skill(self, name, updates, **kwargs):
        self.updates.append(copy.deepcopy(updates))
        for key, value in updates.items():
            if key != 'owner':
                setattr(self.skill, key, copy.deepcopy(value))
        return True

    def set_audit(self, *args, **kwargs):
        pass

    def set_necessity(self, *args, **kwargs):
        pass


class MetadataEditsTests(unittest.TestCase):
    def exercise(self, verdicts, *, edited=None, teacher=None, parse_error=False, save_error=False):
        original = Skill(name='own-fixture', owner='alice', source='taught', status='draft',
                         description='Old', tags=['old'], procedure=['Exact Unicode Ω\n```json\n{"a": 1}\n```'],
                         pitfalls=['Preserve this'], verification=['Exact bytes'], body_extra='Raw 🧭\nline two')
        manager = Manager(original)
        scope = audit_functions()
        tested, fixed = [], []
        async def necessity(*args):
            return {'necessary': True}
        async def test(md, *args, **kwargs):
            tested.append(md)
            return 'CPU transcript', copy.deepcopy(verdicts[len(tested)-1])
        async def improve(md, verdict, transcript, url, model, headers):
            fixed.append(model)
            return edited
        scope.update(_eval_skill_necessity=necessity, _audit_generic_blocker=lambda *a: None,
                     _skill_duplicate_blocker=lambda *a: None, _should_check_retrieval_precision=lambda *a: False,
                     _skill_test_task=lambda *a: 'own CPU task', _run_skill_test_once=test,
                     _improve_skill_md=improve, _audit_finalize_status=lambda *a: 'published')
        if save_error:
            original_update = manager.update_skill
            def update(name, updates, **kwargs):
                if 'description' in updates:
                    raise OSError('own RAM save failure')
                return original_update(name, updates, **kwargs)
            manager.update_skill = update
        with patch.dict(sys.modules, {FORMAT_NAME: format_module}):
            parser = patch.object(Skill, 'from_markdown', side_effect=ValueError('own parser failure')) if parse_error else patch.dict({}, {})
            with parser:
                result = asyncio.run(scope['_audit_one_skill'](manager, original.to_dict(), 'unused', 'worker', {}, teacher, 'alice', lambda msg: None))
        return original, manager, tested, fixed, result

    def edited(self):
        return Skill(name='renamed', owner='mallory', source='imported', status='published', confidence=0.01,
                     teacher_model='injected', description='Narrow Ω', category='specific', version='2',
                     tags=[], platforms=['windows'], requires_toolsets=['python'], fallback_for_toolsets=[],
                     when_to_use='', procedure=['WRONG untested rule'], pitfalls=[], verification=[], body_extra='changed').to_markdown()

    def test_passing_metadata_edit_preserves_the_tested_body_and_identity(self):
        old, manager, tested, fixed, result = self.exercise([{'verdict': 'pass', 'issues': ['metadata: narrow trigger']}], edited=self.edited())
        self.assertEqual(result['result'], 'pass')
        self.assertEqual(len(tested), 1)
        for field in ('procedure', 'pitfalls', 'verification', 'body_extra', 'name', 'owner', 'source', 'teacher_model'):
            with self.subTest(field=field):
                self.assertEqual(getattr(manager.skill, field), getattr(old, field))
        self.assertEqual(manager.skill.description, 'Narrow Ω')
        self.assertEqual(manager.skill.tags, [])
        self.assertEqual(manager.skill.platforms, ['windows'])
        self.assertEqual(manager.skill.requires_toolsets, ['python'])
        self.assertEqual(manager.skill.when_to_use, '')
        self.assertEqual(manager.skill.version, '2')
        self.assertEqual(tested[0], old.to_markdown())
        self.assertEqual(manager.skill.confidence, .95)

    def test_no_metadata_issue_does_not_rewrite(self):
        old, manager, tested, fixed, result = self.exercise([{'verdict': 'pass', 'issues': []}], edited=self.edited())
        self.assertEqual(fixed, [])
        self.assertEqual(manager.skill.procedure, old.procedure)
        self.assertEqual(result['result'], 'pass')

    def test_parser_or_save_failure_keeps_original_tested_source(self):
        for failure in ('parse_error', 'save_error'):
            with self.subTest(failure=failure):
                old, manager, tested, fixed, result = self.exercise([{'verdict': 'pass', 'issues': ['metadata: fix']}], edited=self.edited(), **{failure: True})
                self.assertEqual(manager.skill.procedure, old.procedure)
                self.assertEqual(manager.skill.description, old.description)
                self.assertEqual(result['result'], 'pass')

    def test_functional_self_edit_still_retests_the_changed_source(self):
        old, manager, tested, fixed, result = self.exercise([{'verdict': 'fail'}, {'verdict': 'pass'}], edited=self.edited())
        self.assertEqual(result['result'], 'pass_after_self_edit')
        self.assertEqual(len(tested), 2)
        self.assertEqual(Skill.from_markdown(tested[1]).procedure, Skill.from_markdown(self.edited()).procedure)
        self.assertEqual(manager.skill.procedure, Skill.from_markdown(self.edited()).procedure)

    def test_teacher_rewrite_still_retests_on_the_worker(self):
        old, manager, tested, fixed, result = self.exercise([{'verdict': 'fail'}, {'verdict': 'fail'}, {'verdict': 'pass'}], edited=self.edited(), teacher=('teacher-url', 'teacher', {}))
        self.assertEqual(result['result'], 'pass_after_teacher')
        self.assertEqual(len(tested), 3)
        self.assertEqual(fixed, ['worker', 'teacher'])
        self.assertEqual(Skill.from_markdown(tested[-1]).procedure, manager.skill.procedure)


if __name__ == '__main__':
    unittest.main()
