"""CPU source-isolated contract tests; no application, DB, network or model."""
import ast
import asyncio
import copy
import json
import logging
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def functions(path, names, **values):
    tree = ast.parse((ROOT / path).read_bytes())
    selected = []
    for name in names:
        node = next((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), None)
        if node is None:
            raise AssertionError('Missing native contract function: ' + name)
        node = copy.deepcopy(node)
        node.decorator_list = []
        selected.append(node)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *selected], type_ignores=[])
    scope = dict(logger=logging.getLogger(__name__), asyncio=asyncio, time=time, **values)
    exec(compile(ast.fix_missing_locations(module), path + ' [isolated CPU]', 'exec'), scope)
    return scope


def job(status='running', started=1.0):
    return dict(status=status, scope='scheduled', model='worker', teacher='teacher',
                total=2, done=0, current=None, results=[], log=[], started=started, cancel=False)


def projection(jobs):
    return functions('routes/skills_routes.py', ['_skill_audit_activity', '_skill_audit_status'], _skill_audit_jobs=jobs)


class AuditActivityTests(unittest.TestCase):
    def test_all_creators_preserve_cancelled_job_until_real_cleanup_finishes(self):
        jobs = {('alice',): job()}
        guards = []
        for path, name in (('routes/skills_routes.py', 'audit_all_skills'),
                           ('routes/skills_routes.py', 'run_scheduled_skill_audit'),
                           ('src/builtin_actions.py', 'action_audit_skills')):
            tree = ast.parse((ROOT / path).read_bytes())
            creator = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
            guard = next(n.test for n in ast.walk(creator) if isinstance(n, ast.If)
                         and any(isinstance(x, ast.Name) and x.id == 'existing' for x in ast.walk(n.test)))
            guards.append((name, compile(ast.Expression(guard), path + ' [actual admission condition]', 'eval')))

        async def exercise():
            ready, cancelling, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def stage(*args):
                ready.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelling.set()
                    await finish.wait()
                    raise
            manager = types.SimpleNamespace(load=lambda **kwargs: [{'name': 'a'}])
            scope = functions('routes/skills_routes.py', ['_run_audit_all_job', 'audit_cancel'],
                              _skill_audit_jobs=jobs, _audit_one_skill=stage,
                              effective_user=lambda request: 'alice')
            task = asyncio.create_task(scope['_run_audit_all_job'](('alice',), manager, ['a'], 'unused', 'worker', {}, None, 'alice'))
            jobs[('alice',)]['task'] = task
            try:
                await asyncio.wait_for(ready.wait(), 1)
                await scope['audit_cancel'](object())
                await asyncio.wait_for(cancelling.wait(), 1)
                self.assertEqual(jobs[('alice',)]['status'], 'cancelled')
                self.assertTrue(jobs[('alice',)]['active'])
                self.assertFalse(task.done())
                for name, guard in guards:
                    with self.subTest(creator=name, phase='cancelled but still active'):
                        self.assertTrue(eval(guard, {'existing': jobs[('alice',)]}), 'A new request must retain this still-active slot')
            finally:
                finish.set()
                await asyncio.wait_for(task, 1)
            self.assertFalse(jobs[('alice',)]['active'])
            for name, guard in guards:
                with self.subTest(creator=name, phase='cleanup finished'):
                    self.assertFalse(eval(guard, {'existing': jobs[('alice',)]}), 'A new request may proceed after finally')
                    self.assertTrue(eval(guard, {'existing': {'status': 'running'}}), 'Legacy running job remains protected')
        asyncio.run(exercise())

    def test_active_and_terminal_are_visible_without_chat_identity(self):
        jobs = {('',): job(), ('alice',): job('done', 2.0)}
        jobs[('alice',)].update(active=False, done=2, finished=3.0)
        scope = projection(jobs)
        rows = scope['_skill_audit_activity']('admin', True)
        self.assertEqual([x['active'] for x in rows], [True, False])
        self.assertEqual({x['kind'] for x in rows}, {'skill_audit'})
        self.assertEqual(set(rows[0]), set('id kind active status scope owner model teacher phase phase_model attempt total done current started finished error'.split()))
        self.assertEqual(jobs[('',)], job(), 'Projection must not mutate the registry')
        self.assertFalse({'results', 'log', 'task', 'session_id', 'run_id'} & set(rows[0]))

    def test_owner_admin_and_exact_result_selection(self):
        jobs = {('alice',): job(), ('bob',): job('done', 2), ('',): job('done', 3)}
        jobs[('bob',)]['results'] = [{'skill': 'private', 'result': 'error', 'error': 'retained'}]
        scope = projection(jobs)
        rows = scope['_skill_audit_activity']('alice', False)
        self.assertEqual([r['owner'] for r in rows], ['alice'])
        bob = next(r for r in scope['_skill_audit_activity']('admin', True) if r['owner'] == 'bob')
        self.assertEqual(scope['_skill_audit_status']('admin', True, bob['id'])['results'][0]['error'], 'retained')
        self.assertIsNone(scope['_skill_audit_status']('alice', False, bob['id']))
        old_id = rows[0]['id']; jobs[('alice',)]['started'] = 4
        self.assertIsNone(scope['_skill_audit_status']('alice', False, old_id))
        self.assertEqual(scope['_skill_audit_status']('nobody', False), {'status': 'none', 'jobs': []})

    def test_cancelled_but_unfinished_remains_busy_and_sleep_is_absent(self):
        scope = projection({('alice',): dict(job('cancelled'), active=True)})
        self.assertTrue(scope['_skill_audit_activity']('alice', False)[0]['active'])
        self.assertEqual(projection({})['_skill_audit_activity']('alice', False), [])

    def test_runner_stays_active_between_awaits_and_retains_errors(self):
        jobs = {('alice',): job()}; observed = []
        async def stage(manager, skill, url, model, headers, teacher, owner, log):
            log.phase('necessity', model, 0)
            observed.append(copy.deepcopy(jobs[('alice',)]))
            await asyncio.sleep(0)
            self.assertTrue(jobs[('alice',)]['active'])
            if skill['name'] == 'a': raise RuntimeError('actual stage failure')
            return {'skill': skill['name'], 'result': 'pass'}
        manager = types.SimpleNamespace(load=lambda **kwargs: [{'name': 'a'}, {'name': 'b'}])
        scope = functions('routes/skills_routes.py', ['_run_audit_all_job'], _skill_audit_jobs=jobs, _audit_one_skill=stage)
        asyncio.run(scope['_run_audit_all_job'](('alice',), manager, ['a', 'b'], 'unused', 'worker', {}, None, 'alice'))
        current = jobs[('alice',)]
        self.assertEqual(current['done'], 2)
        self.assertFalse(current['active'])
        self.assertEqual(current['results'][0]['error'], 'actual stage failure')
        self.assertEqual(current['error'], 'actual stage failure')
        self.assertTrue(all(x['active'] and x['phase'] == 'necessity' for x in observed))

    def test_outer_failure_is_not_overwritten_as_done(self):
        jobs = {('alice',): job()}
        def broken(**kwargs): raise RuntimeError('load failed outside skill try')
        scope = functions('routes/skills_routes.py', ['_run_audit_all_job'], _skill_audit_jobs=jobs)
        with self.assertRaisesRegex(RuntimeError, 'load failed'):
            asyncio.run(scope['_run_audit_all_job'](('alice',), types.SimpleNamespace(load=broken), ['a'], 'unused', 'worker', {}, None, 'alice'))
        self.assertEqual(jobs[('alice',)]['status'], 'error')
        self.assertEqual(jobs[('alice',)]['phase'], 'error')
        self.assertFalse(jobs[('alice',)]['active'])

    def test_real_skill_pipeline_reports_actual_stage_models_and_retries(self):
        calls = []; verdicts = iter(['fail', 'fail', 'pass'])
        class Manager:
            def read_skill_md(self, *args, **kwargs): return 'initial md'
            def load(self, **kwargs): return [{'name': 'alpha'}]
            def __getattr__(self, name): return lambda *args, **kwargs: None
        async def stream(*args, **kwargs): yield 'data: {"delta":"actual synthetic run"}'
        async def review(*args): return dict(verdict=next(verdicts), issues=[], summary='private fixture')
        async def necessity(*args): return {'necessary': True}
        async def improve(md, *args): return md + ' changed'
        scope = functions('routes/skills_routes.py', ['_audit_phase', '_audit_one_skill', '_run_skill_test_once'],
            _skill_test_messages=lambda *args: [], _eval_skill_run=review, _eval_skill_necessity=necessity,
            _audit_generic_blocker=lambda *args: None, _skill_duplicate_blocker=lambda *args: None,
            _should_check_retrieval_precision=lambda *args: False, _skill_test_task=lambda *args: 'task',
            _improve_skill_md=improve, _apply_skill_md=lambda *args: True,
            _audit_finalize_status=lambda *args: 'published')
        with patch.dict('sys.modules', {'src.agent_loop': types.SimpleNamespace(stream_agent_loop=stream)}):
            result = asyncio.run(scope['_audit_one_skill'](Manager(), {'name': 'alpha'}, 'unused', 'worker', {}, ('unused-teacher', 'teacher', {}), 'alice', lambda msg: None, phase=lambda *x: calls.append(x)))
        self.assertEqual(result['result'], 'pass_after_teacher')
        self.assertEqual([(p, m, a) for p, m, a in calls if p in ('testing', 'review', 'retry')],
                         [('testing','worker',1), ('review','worker',1), ('retry','worker',2), ('review','worker',2), ('retry','worker',3), ('review','worker',3)])
        self.assertIn(('teacher', 'teacher', 2), calls)
        self.assertIn(('necessity', 'worker', 0), calls)

    def test_phase_observer_failures_do_not_control_work(self):
        scope = functions('routes/skills_routes.py', ['_audit_phase'])
        for error in (RuntimeError, asyncio.CancelledError):
            def broken(*args): raise error('observer only')
            scope['_audit_phase'](broken, 'review', 'worker', 1)

    def test_chat_projection_failure_is_unverified_and_success_keeps_terminal(self):
        registry = types.SimpleNamespace(activity_details=lambda: {}, active_session_ids=lambda: [], queued_positions=lambda: {}, interrupted_runs=lambda: [])
        routes = types.SimpleNamespace(_skill_audit_activity=lambda *args: [{'id':'terminal', 'active':False}])
        security = types.SimpleNamespace(owner_is_admin_or_single_user=lambda user: False)
        extras = dict(effective_user=lambda request:'alice', agent_runs=registry,
            tool_approval_store=types.SimpleNamespace(pending_session_ids=lambda **kwargs:[]))
        mods = {'routes.skills_routes':routes, 'src.tool_security':security,
                'src.agent_tools.subagent_tools':types.SimpleNamespace(worker_board=lambda:{})}
        scope = functions('routes/chat_routes.py', ['chat_activity'], **extras)
        with patch.dict('sys.modules', mods):
            value = asyncio.run(scope['chat_activity'](object()))
            self.assertEqual(value['background_jobs'], [{'id':'terminal', 'active':False}])
            self.assertEqual(value['running'], [])
            def broken(*args): raise RuntimeError('registry read failed')
            routes._skill_audit_activity = broken
            failed = asyncio.run(scope['chat_activity'](object()))
            self.assertEqual(failed['unverified'], 1)

    def test_audit_routes_share_cookie_and_authenticated_bearer_identity(self):
        auth = functions('src/auth_helpers.py', ['get_current_user', 'effective_user'])
        for state, expected in ((types.SimpleNamespace(current_user='alice'),'alice'),
                                (types.SimpleNamespace(current_user='api',api_token=True,api_token_owner='alice'),'alice'),
                                (types.SimpleNamespace(current_user='api',api_token=True),'api')):
            request = types.SimpleNamespace(state=state,query_params={})
            security = types.SimpleNamespace(owner_is_admin_or_single_user=lambda user:False)
            scope = functions('routes/skills_routes.py', ['audit_status'], effective_user=auth['effective_user'],
                _owner=auth['get_current_user'], _skill_audit_jobs={('alice',):job(),('api',):job()},
                _skill_audit_status=lambda owner,admin,job_id:dict(owner=owner), HTTPException=RuntimeError)
            with patch.dict('sys.modules', {'src.tool_security':security}):
                self.assertEqual(asyncio.run(scope['audit_status'](request))['owner'],expected)
        tree=ast.parse((ROOT/'routes/skills_routes.py').read_bytes())
        for name in ('audit_all_skills','audit_cancel'):
            node=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
            self.assertTrue(any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='effective_user' for n in ast.walk(node)))

    def test_mcp_exposes_only_native_read_routes(self):
        tree=ast.parse((ROOT/'mcp_servers/hoard_copilot_server.py').read_bytes())
        assignment=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='OPERATIONS' for t in n.targets))
        value=next(ast.literal_eval(v) for k,v in zip(assignment.value.keys,assignment.value.values) if isinstance(k,ast.Constant) and k.value=='hoard_activity')
        self.assertEqual(value[:2],('GET','/api/chat/activity'))
        value=next(ast.literal_eval(v) for k,v in zip(assignment.value.keys,assignment.value.values) if isinstance(k,ast.Constant) and k.value=='hoard_skill_audit_status')
        self.assertEqual(value[:2],('GET','/api/skills/audit-all/status'))
        self.assertIn('job_id',value[3])


if __name__ == '__main__': unittest.main()
