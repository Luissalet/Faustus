"""Joint rules approval seals the exact bounded render projection."""
import hashlib
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src import workspace_trust as wt, project_rules as pr, project_instructions as pi
import routes.workspace_trust_routes as routes

@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / 'repo'; root.mkdir(); (root / '.git').mkdir()
    data = tmp_path / 'data'; data.mkdir()
    monkeypatch.setattr(wt, 'DATA_DIR', str(data))
    values = {'agent_workspace_trust': 'strict', 'project_rules_library_enabled': False}
    monkeypatch.setattr('src.settings.get_setting', lambda k, d=None: values.get(k, d))
    pi.invalidate(); pr._BLOCK_CACHE.clear()
    return root, values

def rule(root, text='CAPTURED RULE', name='sample.md'):
    p = root / '.faustus' / 'rules' / name
    p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text, encoding='utf-8')
    return p

def approve(root):
    result = wt.trust(str(root), wt.digest_for(str(root)), by='test')
    assert result['ok']


def test_v1_identity_exact_without_rules(env):
    root, _ = env; (root / 'AGENTS.md').write_bytes(b'legacy\r\n')
    parts = wt.file_parts(str(root))
    h = hashlib.sha256(b'faustus.workspace_trust.v1\x00' + str(len(parts)).encode() + b'\x00')
    for p in parts:
        rel = p['rel'].encode(); data = p['_data']
        h.update(str(len(rel)).encode()+b'\x00'+rel)
        h.update(str(p['bytes']).encode()+b'\x00')
        h.update(str(len(data)).encode()+b'\x00'+data)
    assert wt.digest_for(str(root)) == h.hexdigest()
    approve(root); assert wt.instructions_snapshot(str(root)).trusted


def test_legacy_approval_does_not_cover_added_rules(env):
    root, _ = env; (root / 'AGENTS.md').write_text('approved instruction')
    approve(root); rule(root)
    snap = wt.instructions_snapshot(str(root))
    assert snap.state == 'changed' and not snap.trusted
    assert 'CAPTURED RULE' not in pr.block_from_snapshot(snap)
    approve(root); assert wt.instructions_snapshot(str(root)).trusted


def test_rule_change_requires_reapproval_and_render_uses_capture(env, monkeypatch):
    root, _ = env; p = rule(root); approve(root)
    snap = wt.instructions_snapshot(str(root)); p.write_text('NEW UNAPPROVED CONTENT')
    monkeypatch.setattr(pr, 'discover_project_rules', lambda *a, **k: pytest.fail('second discovery'))
    rendered = pr.block_from_snapshot(snap)
    assert 'CAPTURED RULE' in rendered and 'NEW UNAPPROVED CONTENT' not in rendered
    monkeypatch.undo()
    assert not wt.instructions_snapshot(str(root)).trusted


def test_rule_only_known_folder_is_not_automatically_approved(env, monkeypatch):
    root, values = env; values['agent_workspace_trust'] = 'ask'; rule(root)
    monkeypatch.setattr(wt, 'has_checkpoint_history', lambda _: True)
    snap = wt.instructions_snapshot(str(root))
    assert snap.state == 'unapproved' and not snap.trusted and not snap.degraded
    assert wt.list_trusted() == []


def test_off_preserves_live_rules_without_approval_reads(env, monkeypatch):
    root, values = env; values['agent_workspace_trust'] = 'off'; p = rule(root)
    monkeypatch.setattr(wt, 'file_parts', lambda *a, **k: pytest.fail('off trust capture'))
    snap = wt.instructions_snapshot(str(root)); p.write_text('OFF LIVE CONTENT')
    assert snap.legacy_read and 'OFF LIVE CONTENT' in pr.block_from_snapshot(snap)


def test_capture_keeps_instruction_priority_and_empty_rules_empty(env, monkeypatch):
    root, _ = env; (root / 'AGENTS.md').write_text('priority instruction')
    approve(root); snap = wt.instructions_snapshot(str(root)); rule(root)
    assert 'priority instruction' in pi.block_from_snapshot(snap)
    assert pr.block_from_snapshot(snap) == ''


def test_rule_read_failure_changes_identity(env, monkeypatch):
    root, _ = env; rule(root); approve(root)
    monkeypatch.setattr(pr, '_read_rule_file', lambda *a, **k: ('', 0, 'unreadable fixture'))
    snap = wt.instructions_snapshot(str(root))
    assert not snap.trusted and snap.state == 'changed'
    assert 'CAPTURED RULE' not in pr.block_from_snapshot(snap)


def test_mdc_approval_is_captured_render_projection(env):
    root, _ = env; p = root / '.cursor' / 'rules' / 'one.mdc'; p.parent.mkdir(parents=True)
    p.write_text('---\ndescription: metadata\n---\nBODY RULE\n')
    approve(root); snap = wt.instructions_snapshot(str(root))
    assert snap.project_rules[0].text == 'BODY RULE'
    assert 'BODY RULE' in pr.block_from_snapshot(snap)
    assert 'description: metadata' not in pr.block_from_snapshot(snap)


def test_review_shows_all_ancestor_sources_from_capture(env, monkeypatch):
    root, _ = env; child = root / 'nested'; child.mkdir()
    for i in range(18): rule(root, f'ancestor {i}', f'{i:02}.md')
    monkeypatch.setattr(routes, 'get_current_user', lambda _: 'admin')
    monkeypatch.setattr(routes, 'owner_is_admin_or_single_user', lambda _: True)
    original = wt.state_for
    def mutate_after_capture(*a, **k):
        state = original(*a, **k)
        rule(root, 'UNREVIEWED MUTATION', '00.md')
        return state
    monkeypatch.setattr(wt, 'state_for', mutate_after_capture)
    app = FastAPI(); app.include_router(routes.setup_workspace_trust_routes())
    response = TestClient(app).get('/api/workspace-trust', params={'workspace': str(child)})
    assert response.status_code == 200
    files = response.json()['files']; assert len(files) == 18
    assert files[0]['text'] == 'ancestor 0'
    assert files[0]['rule_root'] == os.path.realpath(root)
    assert files[0]['rel'].startswith('../')


@pytest.mark.parametrize('approved', [False, True])
def test_real_prompt_uses_joint_verdict_and_captured_rule_bytes(env, monkeypatch, approved):
    from src.agent_loop import _build_system_prompt
    root, _ = env; p = rule(root)
    if approved:
        approve(root)
    capture = wt.instructions_snapshot
    def mutate_after_verdict(workspace):
        snapshot = capture(workspace)
        p.write_text('UNAPPROVED AFTER VERDICT')
        return snapshot
    monkeypatch.setattr(wt, 'instructions_snapshot', mutate_after_verdict)
    built = _build_system_prompt([{'role': 'user', 'content': 'describe repo'}],
                                 'synthetic-model', None, None,
                                 workspace=str(root), suppress_skills=True)
    messages = built[0] if isinstance(built, tuple) else built
    text = ''.join(m.get('content', '') for m in messages
                   if isinstance(m, dict) and isinstance(m.get('content'), str))
    assert ('CAPTURED RULE' in text) is approved
    assert 'UNAPPROVED AFTER VERDICT' not in text

def test_joint_review_instruction_text_is_captured_and_truncation_explicit(env, monkeypatch):
    root, _ = env; rule(root, 'R' * (routes._MAX_TEXT_CHARS + 1))
    instruction = root / 'AGENTS.md'; instruction.write_bytes(b'CAPTURED INSTRUCTION\r\n')
    monkeypatch.setattr(routes, 'get_current_user', lambda _: 'admin')
    monkeypatch.setattr(routes, 'owner_is_admin_or_single_user', lambda _: True)
    original = wt.state_for
    def mutate(*a, **k):
        state = original(*a, **k); instruction.write_text('NEW INSTRUCTION')
        return state
    monkeypatch.setattr(wt, 'state_for', mutate)
    app = FastAPI(); app.include_router(routes.setup_workspace_trust_routes())
    response = TestClient(app).get('/api/workspace-trust', params={'workspace': str(root)})
    assert response.status_code == 200
    entries = response.json()['files']
    assert entries[0]['text'] == 'CAPTURED INSTRUCTION\n'
    assert entries[1]['truncated'] and len(entries[1]['text']) == routes._MAX_TEXT_CHARS
