"""Package imports stay cheap; accessing each public service preserves its real API."""
import json
import os
import subprocess
import sys


def run_python(code):
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                            env=dict(os.environ), timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_services_does_not_initialize_unrequested_backends():
    result = run_python("import services,sys,json; print(json.dumps({'rag': 'src.rag_vector' in sys.modules, 'memory_vector': 'src.memory_vector' in sys.modules, 'names': services.__all__}))")
    assert not result['rag'] and not result['memory_vector']
    assert 'DocsService' in result['names'] and 'MemoryService' in result['names']


def test_requested_exports_keep_class_identity_and_module_initialization():
    code = '''import importlib,services,json,sys
mapping={'search':['SearchService','SearchResult','SearchResponse'], 'docs':['DocsService','DocChunk','IndexResult'], 'research':['ResearchService','ResearchResult','ResearchSource'], 'memory':['MemoryService','Memory','MemorySearchResult'], 'shell':['ShellService','ShellResult']}
for module,names in mapping.items():
    for name in names:
        value=getattr(services,name)
        assert value is getattr(importlib.import_module('services.'+module),name)
        assert getattr(services,name) is value
assert 'src.rag_vector' in sys.modules
try: getattr(services,'unknown_service')
except AttributeError: pass
else: raise AssertionError('unknown export was fabricated')
print(json.dumps({'exports':len(services.__all__),'backend_initialized_on_request':True}))'''
    assert run_python(code)['backend_initialized_on_request']
