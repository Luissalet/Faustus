"""Pure metadata witnesses for declared startup; no app imports or servers."""
import ast
import io
import json
import ntpath
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from urllib.parse import quote,unquote,urljoin,urlsplit

SOURCE=Path(__file__).resolve().parents[1]/'src/ui_smoke.py'
ROOT=r'X:\declared-backend-metadata'

def scope(manifest=None):
    files={ntpath.join(ROOT,'index.html'):'<p>Index</p>'}
    if manifest is not None:files[ntpath.join(ROOT,'.faustus','ui_smoke.json')]=manifest
    dirs={ROOT,ntpath.join(ROOT,'.faustus'),ntpath.join(ROOT,'server')}
    def read(path,*args,**kwargs):
        path=ntpath.normpath(str(path))
        if path not in files:raise FileNotFoundError(path)
        return io.StringIO(files[path])
    path=SimpleNamespace(**{name:getattr(ntpath,name) for name in ('join','abspath','dirname','basename','splitext','commonpath','relpath','isabs','normcase','normpath')},
        isfile=lambda p:ntpath.normpath(str(p)) in files,isdir=lambda p:ntpath.normpath(str(p)) in dirs)
    def walk(root):
        yield ROOT,['.faustus','server'],['index.html']
    ns=dict(os=SimpleNamespace(path=path,walk=walk,sep='\\'),re=re,json=json,open=read,
        quote=quote,unquote=unquote,urljoin=urljoin,urlsplit=urlsplit,IS_WINDOWS=True,
        shutil=SimpleNamespace(which=lambda _:None),time=SimpleNamespace(time=lambda:0))
    tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) or isinstance(n,ast.Assign) and not any(isinstance(t,ast.Name) and t.id=='logger' for t in n.targets)]
    future=ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future,*nodes],type_ignores=[])),'[real smoke functions only]','exec'),ns)
    return ns

class DeclaredBackendTests(unittest.TestCase):
    def test_without_manifest_keeps_static_detection(self):
        ns=scope();self.assertEqual(ns['detect_server'](ROOT)['kind'],'static')

    def test_manifest_precedes_static_index(self):
        ns=scope(json.dumps(dict(argv=['{python}','server.py','--port','{port}'])))
        spec=ns['detect_server'](ROOT)
        self.assertEqual(spec['kind'],'declared')
        self.assertEqual(spec['entry_path'],'/')

    def test_explicit_argv_and_empty_argument_are_preserved(self):
        ns=scope(json.dumps(dict(argv=['{python}','server.py','','--port','{port}','a b','ñ','{"a":1}','&& echo x'])))
        spec=ns['detect_server'](ROOT)
        try:actual=ns['_build_launch'](spec,ROOT,3210,'PYTHON-MARKER')
        except ValueError as e:actual=dict(error=str(e))
        expected=(['PYTHON-MARKER','server.py','','--port','3210','a b','ñ','{"a":1}','&& echo x'],{'PORT':'3210','FLASK_RUN_PORT':'3210'},ROOT)
        self.assertEqual(actual,expected)

    def test_valid_relative_cwd_and_local_entry(self):
        ns=scope(json.dumps(dict(argv=['program'],cwd='server',entry_path='/ui/index.html?q=1#x')))
        spec=ns['detect_server'](ROOT)
        try:actual=ns['_build_launch'](spec,ROOT,3210,'PYTHON-MARKER')
        except ValueError as e:actual=dict(error=str(e))
        self.assertEqual(actual,(['program'],{'PORT':'3210','FLASK_RUN_PORT':'3210'},ntpath.join(ROOT,'server')))
        self.assertEqual(spec.get('entry_path'),'/ui/index.html?q=1#x')

    def test_invalid_configuration_does_not_fall_back(self):
        cases=['{',json.dumps([]),json.dumps(dict(argv=[])),json.dumps(dict(argv=[''])),
            json.dumps(dict(argv=['x',False])),json.dumps(dict(argv=['x'],cwd='..')),
            json.dumps(dict(argv=['x'],entry_path='https://example.invalid/'))]
        for document in cases:
            with self.subTest(document=document):
                ns=scope(document);spec=ns['detect_server'](ROOT)
                self.assertEqual(spec['kind'],'declared')
                with self.assertRaises(ValueError):ns['_build_launch'](spec,ROOT,3210,'PYTHON-MARKER')

    def test_manifest_only_mutation_is_ui_facing_in_both_filters(self):
        ns=scope();self.assertTrue(ns['_is_ui_mutation']('.faustus/ui_smoke.json'))
        source=SOURCE.parents[0]/'agent_harness.py'
        tree=ast.parse(source.read_text(encoding='utf-8-sig'))
        node=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='UI_PATH_RE' for t in n.targets))
        pattern=eval(compile(ast.Expression(node.value),'[actual ledger UI regex only]','eval'),dict(re=re))
        self.assertTrue(pattern.search('.faustus/ui_smoke.json'))
        self.assertFalse(pattern.search('.faustus/unrelated.json'))

    def test_substitution_is_one_pass_and_literal(self):
        ns=scope();workspace=ROOT+r'\{port}'
        spec=dict(kind='declared',dir=ROOT,argv=['{workspace}','{data_dir}','{python}','{unknown}','{"a":1}'])
        result=ns['_build_launch'](spec,workspace,3210,'PYTHON-MARKER',data_dir='OWN-DATA')
        self.assertEqual(result[0],[workspace,'OWN-DATA','PYTHON-MARKER','{unknown}','{"a":1}'])

    def lifecycle(self,*,data=True,ready=True,stop=True,unknown_tree=False,spawn_error=False,cleanup_error=False,audit_error=False):
        ns=scope();trace=[];state=dict(running=True);counter=[0]
        proc=SimpleNamespace(pid=123,stdout=None,poll=lambda:None if state['running'] else 0)
        def temporary(**kwargs):
            trace.append('temporary');counter[0]+=1;return 'OWN-DATA-'+str(counter[0])
        def spawn(argv,**kwargs):
            trace.append(('spawn',argv,kwargs['cwd']))
            if spawn_error:raise FileNotFoundError('missing executable')
            state['running']=True
            return proc
        def kill(owned):
            if owned is not None:
                trace.append('stop');state['running']=not stop
        def snapshot(owned):
            trace.append('observe');return None if unknown_tree else []
        def remove(path):
            trace.append(('remove',path))
            if cleanup_error:raise OSError('directory busy')
        ns.update(tempfile=SimpleNamespace(mkdtemp=temporary),
            subprocess=SimpleNamespace(Popen=spawn,PIPE=-1,STDOUT=-2),
            native_host_environment=lambda **kwargs:{},_free_port=lambda:3210,_project_python=lambda _: 'PYTHON-MARKER',
            _popen_kwargs=lambda:{},_wait_ready=lambda *args:(ready,'not ready'),
            _crawl=lambda *args:([dict(url='http://127.0.0.1:3210/',status=200,ok=True)],[],[]),
            playwright_available=lambda:True,_playwright_audit=lambda *args:dict(console_errors=[]),
            _kill_process_tree=kill,_launch_tree_snapshot=snapshot,_setting=lambda key,default:default,
            logger=SimpleNamespace(debug=lambda *args:None))
        if audit_error:
            def fail(*args):raise RuntimeError('crawl failed')
            ns['_crawl']=fail
        ns['shutil'].rmtree=remove
        spec=dict(kind='declared',argv=['{python}','program.py','--port','{port}'],dir=ROOT,entry_path='/')
        if data:spec['argv']+=['--db','{data_dir}/state.sqlite3']
        return ns,spec,trace

    def test_temporary_is_lazy_and_cleanup_follows_stop(self):
        ns,spec,trace=self.lifecycle()
        self.assertEqual(trace,[])
        report=ns['run_smoke'](ROOT,spec)
        self.assertTrue(report['ok']);self.assertEqual(report['data_cleanup'],dict(state='removed'))
        self.assertEqual([item if isinstance(item,str) else item[0] for item in trace],['temporary','spawn','observe','stop','remove'])
        self.assertEqual(report['server_argv'][-1],'OWN-DATA-1/state.sqlite3')

    def test_no_marker_needs_no_temporary(self):
        ns,spec,trace=self.lifecycle(data=False);report=ns['run_smoke'](ROOT,spec)
        self.assertTrue(report['ok']);self.assertNotIn('temporary',trace)
        self.assertFalse(any(isinstance(item,tuple) and item[0]=='remove' for item in trace))

    def test_start_readiness_and_audit_failures_keep_their_result(self):
        for kwargs in (dict(spawn_error=True),dict(ready=False),dict(audit_error=True)):
            with self.subTest(kwargs=kwargs):
                ns,spec,trace=self.lifecycle(**kwargs);report=ns['run_smoke'](ROOT,spec)
                self.assertFalse(report['ok']);self.assertEqual(report['data_cleanup']['state'],'removed')

    def test_uncertain_stop_or_tree_retains_data(self):
        for kwargs in (dict(stop=False),dict(unknown_tree=True)):
            with self.subTest(kwargs=kwargs):
                ns,spec,trace=self.lifecycle(**kwargs);report=ns['run_smoke'](ROOT,spec)
                self.assertEqual(report['data_cleanup']['state'],'retained')
                self.assertFalse(any(isinstance(item,tuple) and item[0]=='remove' for item in trace))
                self.assertTrue(report['quality_warnings'])

    def test_cleanup_error_does_not_hide_a_failed_check(self):
        ns,spec,trace=self.lifecycle(ready=False,cleanup_error=True);report=ns['run_smoke'](ROOT,spec)
        self.assertFalse(report['ok']);self.assertIn('never answered',report['summary'])
        self.assertEqual(report['data_cleanup'],dict(state='retained',reason='directory busy'))

    def test_creation_failure_starts_no_process(self):
        ns,spec,trace=self.lifecycle()
        def fail(**kwargs):raise OSError('no temporary directory')
        ns['tempfile'].mkdtemp=fail
        report=ns['run_smoke'](ROOT,spec)
        self.assertFalse(report['ok']);self.assertEqual(trace,[])

    def test_environment_failure_cleans_created_data_without_start(self):
        ns,spec,trace=self.lifecycle()
        def fail(**kwargs):raise OSError('environment unavailable')
        ns['native_host_environment']=fail
        report=ns['run_smoke'](ROOT,spec)
        self.assertFalse(report['ok'])
        self.assertEqual(trace,['temporary',('remove','OWN-DATA-1')])
        self.assertEqual(report['data_cleanup']['state'],'removed')

    def test_preview_build_does_not_allocate_data(self):
        ns=scope();spec=dict(kind='declared',argv=['program','{data_dir}'],dir=ROOT)
        with self.assertRaises(ValueError):ns['_build_launch'](spec,ROOT,3210,'PYTHON-MARKER')

    def test_invalid_manifest_only_change_reaches_failed_report(self):
        ns=scope('{');ns['_setting']=lambda key,default:default
        ns['_free_port']=lambda:3210;ns['_project_python']=lambda _: 'PYTHON-MARKER'
        ns['native_host_environment']=lambda **kwargs:{}
        report=ns['run_for_turn'](ROOT,['.faustus/ui_smoke.json'])
        self.assertTrue(report['ran']);self.assertFalse(report['ok'])
        self.assertIn('.faustus/ui_smoke.json',report['summary'])

    def test_read_error_is_a_configuration_failure(self):
        ns=scope()
        def denied(*args,**kwargs):raise PermissionError('unreadable declaration')
        ns['open']=denied
        spec=ns['detect_server'](ROOT)
        self.assertEqual(spec['kind'],'declared');self.assertIn('unreadable',spec['configuration_error'])

    def test_external_authority_and_backslash_entry_are_rejected(self):
        for entry in ('//example.invalid/','/\\example.invalid/','https://example.invalid/'):
            with self.subTest(entry=entry):
                ns=scope(json.dumps(dict(argv=['program'],entry_path=entry)))
                self.assertIn('configuration_error',ns['detect_server'](ROOT))

    def test_a_real_console_error_still_fails(self):
        ns,spec,trace=self.lifecycle()
        ns['_playwright_audit']=lambda *args:dict(console_errors=['404 from actual backend'])
        report=ns['run_smoke'](ROOT,spec)
        self.assertFalse(report['ok']);self.assertEqual(report['console_errors'],['404 from actual backend'])
        self.assertEqual(report['data_cleanup']['state'],'removed')

    def test_entry_is_used_by_crawl_and_browser(self):
        ns,spec,trace=self.lifecycle();spec['entry_path']='/ui/index.html?q=1#x'
        observed=[]
        ns['_playwright_audit']=lambda url,timeout:(observed.append(url) or dict(console_errors=[]))
        report=ns['run_smoke'](ROOT,spec)
        self.assertEqual(observed,['http://127.0.0.1:3210/ui/index.html?q=1#x'])
        # The real crawler's first-request expression preserves the same entry.
        source_scope=scope();calls=[]
        source_scope['_fetch']=lambda url:(calls.append(url) or (200,'text/html',b''))
        source_scope['_crawl'](spec,'http://127.0.0.1:3210',10)
        self.assertEqual(calls,['http://127.0.0.1:3210/ui/index.html?q=1#x'])

    def test_separate_runs_get_separate_data_paths(self):
        ns,spec,trace=self.lifecycle()
        one=ns['run_smoke'](ROOT,spec);two=ns['run_smoke'](ROOT,spec)
        self.assertNotEqual(one['launch_data_dir'],two['launch_data_dir'])

    def test_cleanup_requires_known_gone_processes(self):
        ns=scope();removed=[];ns['shutil'].rmtree=lambda path:removed.append(path)
        proc=SimpleNamespace(poll=lambda:0)
        alive=SimpleNamespace(is_running=lambda:True)
        result=ns['_cleanup_launch_data']('OWN-DATA',proc,[alive])
        self.assertEqual(result['state'],'retained');self.assertEqual(removed,[])
        gone=SimpleNamespace(is_running=lambda:False)
        result=ns['_cleanup_launch_data']('OWN-DATA',proc,[gone])
        self.assertEqual(result['state'],'removed');self.assertEqual(removed,['OWN-DATA'])

if __name__=='__main__':unittest.main(verbosity=2)
