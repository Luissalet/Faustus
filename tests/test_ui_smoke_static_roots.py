"""CPU-only real ui_smoke functions over path/HTML metadata in RAM.

No product import, fixture app, server, socket, browser, process or HTTP.
Translate paths with the actual stdlib handler method without a handler.
"""
import ast
import io
import json
import ntpath
import os
from pathlib import Path
import posixpath
import re
import sysconfig
import time
from types import SimpleNamespace
import unittest
import urllib.parse


SOURCE=Path(__file__).resolve().parents[1]/'src/ui_smoke.py'
ROOT=r'X:\ui-smoke-path-metadata'


def functions(files):
    tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
    names={'_index_html_is_served_source','_find_index_html_dir','_static_server_spec',
        '_local_imports_source','detect_server','_build_launch','_extract_asset_refs',
        '_check_asset','_crawl','_report','_quality_warnings','_summarize','failure_message','compact'}
    selected=[n for n in tree.body if isinstance(n,ast.Assign) and not any(isinstance(t,ast.Name) and t.id=='logger' for t in n.targets)
        or isinstance(n,ast.FunctionDef) and n.name in names]
    files={ntpath.normpath(name):value for name,value in files.items()}
    directories={ROOT}
    for name in files:
        directory=ntpath.dirname(name)
        while directory and directory not in directories:
            directories.add(directory);directory=ntpath.dirname(directory)
    reads=[]
    def read(path,*args,**kwargs):
        path=ntpath.normpath(str(path));reads.append(path)
        if path not in files:raise FileNotFoundError(path)
        return io.StringIO(files[path])
    def walk(root):
        def visit(directory):
            subs=sorted(ntpath.basename(d) for d in directories if d!=directory and ntpath.dirname(d)==directory)
            names=sorted(ntpath.basename(p) for p in files if ntpath.dirname(p)==directory)
            yield directory,subs,names
            for sub in subs:yield from visit(ntpath.join(directory,sub))
        yield from visit(ntpath.normpath(root))
    path=SimpleNamespace(**{name:getattr(ntpath,name) for name in
        ('join','dirname','basename','splitext','normpath','normcase','commonpath','relpath')},
        abspath=ntpath.normpath,realpath=ntpath.normpath,
        isfile=lambda p:ntpath.normpath(p) in files,isdir=lambda p:ntpath.normpath(p) in directories)
    ns=dict(os=SimpleNamespace(path=path,walk=walk,sep='\\'),re=re,json=json,time=time,
        urljoin=urllib.parse.urljoin,urlsplit=urllib.parse.urlsplit,unquote=urllib.parse.unquote,quote=urllib.parse.quote,
        IS_WINDOWS=True,shutil=SimpleNamespace(which=lambda _:None),open=read)
    future=ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future,*selected],type_ignores=[])),str(SOURCE),'exec'),ns)
    def read_text(path,cap=400_000):
        try:return read(path).read(cap)
        except OSError:return ''
    ns['_read_text']=read_text
    return ns,files,reads


def translate(directory,path):
    source=Path(sysconfig.get_path('stdlib'))/'http/server.py'
    tree=ast.parse(source.read_text(encoding='utf-8'))
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='SimpleHTTPRequestHandler')
    method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='translate_path')
    scope=dict(os=os,posixpath=posixpath,urllib=urllib)
    exec(compile(ast.Module(body=[method],type_ignores=[]),'[stdlib translate_path only]','exec'),scope)
    return scope['translate_path'](SimpleNamespace(directory=directory),path)


class StaticRootTests(unittest.TestCase):
    def detect(self,html,assets,folder='static',extras=None):
        directory=ntpath.join(ROOT,folder) if folder else ROOT
        metadata={ntpath.join(directory,'index.html'):html,**{ntpath.join(ROOT,name):'' for name in assets}}
        metadata.update(extras or {})
        ns,files,reads=functions(metadata);spec=ns['detect_server'](ROOT)
        return ns,spec,files,reads

    def launch(self,ns,spec):return ns['_build_launch'](spec,ROOT,12345,'PYTHON-MARKER')

    def test_nested_absolute_namespace_uses_workspace_and_real_html_entry(self):
        ns,spec,files,reads=self.detect('<script src="/static/widget.js"></script><link href="/static/widget.css">',
            ['static/widget.js','static/widget.css'],extras={ntpath.join(ROOT,'app.py'):'from http.server import ThreadingHTTPServer\n'})
        argv,env,cwd=self.launch(ns,spec)
        self.assertEqual(cwd,ROOT)
        self.assertEqual(argv,['PYTHON-MARKER','-m','http.server','12345']);self.assertEqual(env,{})
        entry=spec.get('entry_path') or '/'
        self.assertEqual(entry,'/static/index.html')
        self.assertEqual(translate(cwd,entry),ntpath.join(ROOT,'static','index.html'))
        for ref in ns['_extract_asset_refs'](files[ntpath.join(ROOT,'static','index.html')]):
            self.assertIn(translate(cwd,urllib.parse.urlsplit(urllib.parse.urljoin('http://metadata.invalid'+entry,ref)).path),files)
        self.assertTrue(all(ntpath.commonpath([ROOT,p])==ROOT for p in reads))

    def test_flat_and_nested_relative_entries_keep_existing_resolution(self):
        for folder,assets in (('', ['widget.js']),('static',['static/widget.js'])):
            with self.subTest(folder=folder):
                ns,spec,files,_=self.detect('<script src="widget.js"></script>',assets,folder)
                _,_,cwd=self.launch(ns,spec)
                self.assertEqual(cwd,ntpath.join(ROOT,folder) if folder else ROOT)
                self.assertEqual(spec.get('entry_path') or '/','/')
                self.assertIn(translate(cwd,'/widget.js'),files)

    def test_nested_own_absolute_assets_prefer_nearest_root(self):
        ns,spec,files,_=self.detect('<script src="/assets/widget.js"></script>',
            ['site/assets/widget.js','assets/widget.js'],folder='site')
        _,_,cwd=self.launch(ns,spec)
        self.assertEqual(cwd,ntpath.join(ROOT,'site'))
        self.assertEqual(spec.get('entry_path') or '/','/')
        self.assertIn(translate(cwd,'/assets/widget.js'),files)

    def test_parent_relative_reference_and_unicode_entry_are_not_lost(self):
        ns,spec,files,_=self.detect('<script src="../shared/widget.js"></script>',['shared/widget.js'],folder='página #1')
        _,_,cwd=self.launch(ns,spec);entry=spec.get('entry_path') or '/'
        self.assertEqual(cwd,ROOT)
        self.assertEqual(entry,'/p%C3%A1gina%20%231/index.html')
        url=urllib.parse.urljoin('http://metadata.invalid'+entry,'../shared/widget.js')
        self.assertIn(translate(cwd,urllib.parse.urlsplit(url).path),files)

    def test_crawl_fetches_entry_not_directory_listing_and_keeps_missing_asset(self):
        html='<script src="/static/widget.js"></script><link href="/static/missing.css">'
        ns,spec,files,_=self.detect(html,['static/widget.js'])
        urls=[]
        def fetch(url):
            urls.append(url)
            path=urllib.parse.urlsplit(url).path
            if path=='/static/index.html':return 200,'text/html',html.encode()
            if path=='/static/widget.js':return 200,'text/javascript',b''
            return 404,'text/html',b''
        ns['_fetch']=fetch
        pages,assets,problems=ns['_crawl'](spec,'http://metadata.invalid',time.time()+10)
        self.assertEqual(pages[0]['url'],'http://metadata.invalid/static/index.html')
        self.assertTrue(pages[0]['ok']);self.assertEqual(len(assets),2)
        self.assertTrue(problems);self.assertFalse(next(a for a in assets if a['url'].endswith('missing.css'))['ok'])

    def test_real_mime_and_status_checks_still_fail(self):
        ns,_,_,_=self.detect('',[],folder='')
        for reply,problem in (((200,'text/plain',b''),'content_type'),((404,'text/javascript',b''),'status')):
            with self.subTest(reply=reply):
                ns['_fetch']=lambda url:reply
                result=ns['_check_asset']('http://metadata.invalid/widget.js')
                self.assertFalse(result['ok']);self.assertEqual(result['problem'],problem)

    def test_recognized_backend_priority_commands_and_local_imports_unchanged(self):
        variants=[('from flask import Flask\n','flask'),('app = FastAPI()\n','fastapi'),
            ('from pkg.api import create_app\n','flask')]
        for header,kind in variants:
            with self.subTest(header=header):
                extras={ntpath.join(ROOT,'app.py'):header,ntpath.join(ROOT,'pkg','api.py'):'from flask import Flask\n',
                    ntpath.join(ROOT,'package.json'):'{"scripts":{"start":"marker-only"}}'}
                ns,spec,_,_=self.detect('',[],extras=extras)
                self.assertEqual(spec['kind'],kind)
                argv,env,cwd=self.launch(ns,spec);self.assertEqual(cwd,ROOT)
                if kind=='flask':self.assertEqual(argv[1:],['-m','flask','run','--port','12345']);self.assertEqual(env['FLASK_APP'],'app.py')
                else:self.assertEqual(argv[1:4],['-m','uvicorn','app:app'])
        ns,spec,_,_=self.detect('',[],extras={ntpath.join(ROOT,'package.json'):'{"scripts":{"dev":"marker-only"}}'})
        self.assertEqual(spec['kind'],'npm');self.assertEqual(self.launch(ns,spec)[0],['npm.cmd','run','dev'])

    def test_fallback_failure_and_compact_preserve_launch_metadata(self):
        ns,_,_,_=self.detect('',[],folder='')
        report=dict(ran=True,ok=False,summary='metadata-only failure',server_kind='static',server_cwd=ROOT,
            server_cmd='PYTHON-MARKER -m http.server 12345',entry_path='/static/index.html')
        note=ns['failure_message'](report)
        self.assertIn('static preview',note)
        self.assertNotIn("started your app's own server",note)
        self.assertIn('/static/index.html',note)
        compact=ns['compact'](report)
        for key in ('server_kind','server_cwd','server_cmd','entry_path'):self.assertEqual(compact.get(key),report[key])

    def test_playwright_and_report_wiring_use_html_entry(self):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        run=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run_smoke')
        audit=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_playwright_audit')
        call=next(n for n in ast.walk(run) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='_playwright_audit')
        scope=dict(port=12345,spec={'entry_path':'/static/index.html'},urljoin=urllib.parse.urljoin)
        # Evaluate only URL-building assignments, never orchestration/lifecycle.
        assignments={n.targets[0].id:n for n in ast.walk(run) if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name)}
        def value(node):
            for name in {n.id for n in ast.walk(node) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load)}-scope.keys():
                assignment=assignments[name]
                scope[name]=value(assignment.value)
            return eval(compile(ast.Expression(body=node),'[actual URL expression only]','eval'),scope)
        self.assertEqual(value(call.args[0]),'http://127.0.0.1:12345/static/index.html')
        goto=next(n for n in ast.walk(audit) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='goto')
        target=eval(compile(ast.Expression(body=goto.args[0]),'[actual goto URL expression only]','eval'),
            {'base_url':'http://127.0.0.1:12345/static/index.html'})
        self.assertEqual(target,'http://127.0.0.1:12345/static/index.html')


if __name__=='__main__':unittest.main(verbosity=2)
