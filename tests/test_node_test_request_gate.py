"""Native request gate: a requested Node test is narrower than general shell."""
import asyncio
import shutil
from types import SimpleNamespace

import pytest

from src.tool_capabilities import ToolRunSecurityContext
from src.tool_policy import ToolPolicy
from src.user_request_gate import allows

REQUEST = 'Corrige el problema del proyecto y ejecuta las pruebas.'


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests' / 'genealogy.test.mjs').write_text('')
    (tmp_path / 'tests' / 'typed.test.ts').write_text('')
    return str(tmp_path)


@pytest.mark.parametrize('command', [
    'node --test tests/genealogy.test.mjs',
    'node --test --experimental-strip-types tests/typed.test.ts',
    'node --experimental-strip-types --test tests/typed.test.ts',
    'node --test --test-reporter=spec tests/genealogy.test.mjs',
    'node --test tests/*.test.mjs | tail -20',
    'node --test',
])
def test_requested_node_tests_pass_without_a_workspace_shell_grant(workspace, command):
    assert allows('bash', command, REQUEST, workspace)


@pytest.mark.parametrize('command', [
    'node tests/genealogy.test.mjs',
    'node --test -e code', 'node --test --eval=code', 'node --test -p code',
    'node --test --print=code', 'node --test -r load.js', 'node --test --require=load.js',
    'node --test --import=load.js', 'node --test --loader=load.js',
    'node --test --test-reporter=./reporter.js',
    'node --test --test-reporter-destination=../outside.txt',
    'node --test --unknown-option tests/genealogy.test.mjs',
    'node --test ../outside.test.mjs', 'node --test tests/../../outside.test.mjs',
    'node --test /tmp/outside.test.mjs', 'node --test C:/outside.test.mjs',
    'node --test tests/genealogy.test.mjs && node -e code',
    'node --test tests/genealogy.test.mjs > outside.txt',
    'node --test tests/genealogy.test.mjs | node -e code',
    'NODE_OPTIONS=--import=outside.js node --test tests/genealogy.test.mjs',
])
def test_extra_code_loading_writing_options_and_outside_paths_stay_gated(workspace, command):
    assert not allows('bash', command, REQUEST, workspace)


def test_tests_need_user_request_and_a_bound_workspace(workspace):
    command = 'node --test tests/genealogy.test.mjs'
    assert not allows('bash', command, 'Inspecciona la carpeta.', workspace)
    assert not allows('bash', command, REQUEST, '')
    assert not allows('bash', command, 'No ejecutar las pruebas; inspecciona el código.', workspace)
    assert not allows('bash', command, "Don't run tests; inspect the files.", workspace)


def test_native_armed_gate_and_readonly_dispatch_keep_their_boundaries(workspace, monkeypatch):
    from src import tool_execution as te
    monkeypatch.setattr('src.tool_capabilities.tool_approval_mode', lambda: 'ask')
    gate = ToolRunSecurityContext(workspace=workspace, user_request=REQUEST)
    gate.external_untrusted_context_seen = True
    gate.external_sources = ['read_file']
    command = 'node --test --experimental-strip-types tests/typed.test.ts'
    assert gate.decision_for('bash', command).allowed
    assert not gate.approval_gate_bypassed
    assert not gate.decision_for('bash', 'node --test --import=load.js').allowed
    monkeypatch.setattr(te, 'owner_is_admin_or_single_user', lambda owner: True)
    _formatted, result = asyncio.run(te.execute_tool_block(
        SimpleNamespace(tool_type='bash', content=command), workspace=workspace,
        security_context=gate, owner='qa', tool_policy=ToolPolicy(read_only=True),
    ))
    assert result['blocked']
    assert result['policy'] == 'tool_policy'
    _formatted, result = asyncio.run(te.execute_tool_block(
        SimpleNamespace(tool_type='bash', content=command), workspace=workspace,
        security_context=gate, owner='qa', disabled_tools={'bash'},
    ))
    assert result['exit_code'] != 0
    assert 'disabled' in result['error']


@pytest.mark.skipif(not shutil.which('node'), reason='Node unavailable')
def test_real_native_inspection_and_node_typescript_check_need_no_general_shell_grant(workspace, monkeypatch):
    from pathlib import Path
    from src import tool_execution as te
    monkeypatch.setattr(te, 'owner_is_admin_or_single_user', lambda owner: True)
    monkeypatch.setattr('src.tool_capabilities.tool_approval_mode', lambda: 'ask')
    Path(workspace, 'tests', 'typed.test.ts').write_text(
        "import { test } from 'node:test';\nimport assert from 'node:assert/strict';\n"
        "const twice = (x: number) => x * 2;\ntest('synthetic check', () => assert.equal(twice(3), 6));\n"
    )
    gate = ToolRunSecurityContext(workspace=workspace, user_request=REQUEST)
    async def run():
        _formatted, inspection = await te.execute_tool_block(
            SimpleNamespace(tool_type='read_file', content='{"path":"tests/typed.test.ts"}'),
            workspace=workspace, security_context=gate, owner='qa',
        )
        assert inspection['exit_code'] == 0
        assert gate.external_untrusted_context_seen
        _formatted, result = await te.execute_tool_block(
            SimpleNamespace(tool_type='bash', content='node --test --experimental-strip-types tests/typed.test.ts'),
            workspace=workspace, security_context=gate, owner='qa',
        )
        assert result['exit_code'] == 0, result
        assert 'synthetic check' in result.get('output', '')
        assert not gate.approval_gate_bypassed
    asyncio.run(run())


def test_an_explicit_symlink_outside_does_not_gain_a_test_allowance(workspace, tmp_path):
    outside = tmp_path.parent / f'{tmp_path.name}-outside.test.mjs'
    outside.write_text('')
    link = tmp_path / 'tests' / 'linked.test.mjs'
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip('Symlinks unavailable on this host')
    assert not allows('bash', 'node --test tests/linked.test.mjs', REQUEST, workspace)
    assert not allows('bash', 'node --test tests/*.test.mjs', REQUEST, workspace)
