"""Large input still follows the normal math/workspace routing predicates."""
from pathlib import Path
import subprocess
import sys

import pytest

from src import agent_loop
from src import agent_harness


@pytest.mark.parametrize('name', [
    'cart.py', '-cart.py', '..cart.PY', '3.py', '_cart.tsx',
    'área.py', 'Книга.py', '脚本.js', 'a-b.c.py', '...a-b.py!',
])
def test_source_filenames_keep_their_workspace_signal(name):
    assert agent_loop._WORKSPACE_CODE_TARGET_RE.search(name)
    assert agent_loop._SOURCE_ARTIFACT_SIGNAL_RE.search(name)


@pytest.mark.parametrize('name', [
    '.py', '...py', '-py', 'ordinary.words', 'not_a_source.pyextra',
])
def test_nonfilenames_do_not_become_workspace_targets(name):
    assert not agent_loop._WORKSPACE_CODE_TARGET_RE.search(name)


@pytest.mark.parametrize('text', [
    'Calcula 2 + 2', 'Suma estos importes: 12, 5 y 8',
    'Calculate 3 * 7',
])
def test_math_requests_keep_the_answer_only_route(text):
    assert agent_loop._answer_only_math_request(text)


def test_extracted_paths_keep_captures_order_and_existing_filters():
    text = 'Edit @src/cart.py, tests/área_test.py and src/cart.py again. https://example.com/a.py node.js 1.0.json'
    assert agent_harness.extract_path_tokens(text) == [
        'src/cart.py', 'tests/área_test.py',
    ]


def test_long_no_space_inputs_finish_in_bounded_child_process():
    # Compile the actual two expressions without bootstrapping application
    # services in the child. A stuck historical matcher is confined to this
    # owned child; it must not stall the complete pytest process.
    source = Path(agent_loop.__file__).resolve()
    script = r'''
import ast, json, pathlib, re, sys, time
tree = ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
names = ('_WORKSPACE_CODE_TARGET_RE', '_SOURCE_ARTIFACT_SIGNAL_RE')
nodes = [n for n in tree.body if isinstance(n, ast.Assign)
         and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)]
assert len(nodes) == 2
scope = {'re': re}
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(sys.argv[1]), 'exec'), scope)
tree = ast.parse(pathlib.Path(sys.argv[2]).read_text(encoding='utf-8'))
nodes = [n for n in tree.body if isinstance(n, ast.Assign)
         and any(isinstance(t, ast.Name) and t.id in ('_PATH_EXTS', 'PATH_TOKEN_RE') for t in n.targets)]
assert len(nodes) == 2
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(sys.argv[2]), 'exec'), scope)
names += ('PATH_TOKEN_RE',)
body = 'current-data-' * 300000
started = time.monotonic()
for prefix, suffix, expected in [('', '', False), ('cart.py ', '', True),
                                 ('', ' cart.py', True), ('-', '-cart.py!', True)]:
    for name in names:
        assert bool(scope[name].search(prefix + body + suffix)) == expected
print(json.dumps({'characters': len(body), 'checks': 12, 'elapsed_s': time.monotonic() - started}))
'''
    result = subprocess.run([sys.executable, '-c', script, str(source), str(Path(agent_harness.__file__).resolve())],
                            capture_output=True, text=True, timeout=12)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"checks": 12' in result.stdout
