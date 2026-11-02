"""Read-only paging contract without a WhatsApp account or Node dependencies."""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_real_node_pages_same_second_restarts_edits_and_late_messages():
    module = (ROOT / 'bridges/whatsapp/message_pages.mjs').as_uri()
    script = '''
import {messagePage} from MODULE;
import assert from 'node:assert/strict';
const chat='gittrend@g.us';
const rows=Array.from({length:7},(_,i)=>({id:`m${i}`,chat,ts:10,text:'a'.repeat(9000),from_me:true}));
rows.push({id:'other',chat:'other@g.us',ts:10,text:'private'});
let cursor='',seen=[];
do { const page=messagePage(rows,{chat,cursor,limit:2}); seen.push(...page.messages); cursor=page.next_cursor; if(!page.has_more)break; } while(true);
assert.deepEqual(seen.map(x=>x.id),['m0','m1','m2','m3','m4','m5','m6']);
assert(seen.every(x=>x.from_me && x.text.length===9000));
const first=messagePage(rows,{chat,limit:2});
assert.deepEqual(messagePage(rows,{chat,cursor:first.next_cursor,limit:2}).messages.map(x=>x.id),['m2','m3']);
rows[0].text='edited'; rows.push({id:'late',chat,ts:1,text:'arrived late'});
const replay=messagePage(rows,{chat,limit:20});
assert.equal(replay.messages[0].id,'late'); assert.equal(replay.messages[1].text,'edited');
assert.equal(replay.coverage.phone_history_complete,false);
assert.equal(replay.coverage.repeat_from_start_for_updates,true);
assert.throws(()=>messagePage(rows,{chat:'other@g.us',cursor:first.next_cursor}));
assert.throws(()=>messagePage(rows,{chat,cursor:'garbage'}));
assert.throws(()=>messagePage(rows,{chat:'Gittrend'}));
assert.throws(()=>messagePage(rows,{chat,limit:0}));
assert.equal(messagePage([],{chat}).has_more,false);
console.log('paging-contract-pass');
'''.replace('MODULE', json.dumps(module))
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_python_wrapper_uses_exact_chat_and_no_unread(monkeypatch):
    from src import whatsapp_bridge as wa
    calls = []
    monkeypatch.setattr(wa, '_get', lambda path, args: calls.append((path, args)) or {'messages': []})
    assert wa.message_page('gittrend@g.us', cursor='opaque', limit=3) == {'messages': []}
    assert calls == [('/message-page', {'chat': 'gittrend@g.us', 'cursor': 'opaque', 'limit': 3})]
    for chat, limit in [('Gittrend', 1), ('x@g.us', 0), ('x@g.us', True)]:
        with pytest.raises(wa.BridgeError):
            wa.message_page(chat, limit=limit)


def test_api_and_tool_preserve_cursor_coverage_and_own_messages(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import whatsapp_routes as routes
    from src import whatsapp_bridge as wa, whatsapp_tools as wt
    page = {'messages': [{'id': 'x', 'from_me': True, 'text': 'a' * 9000}], 'next_cursor': 'opaque', 'has_more': False, 'coverage': {'phone_history_complete': False}}
    calls = []
    def fake(chat, **kwargs):
        calls.append((chat, kwargs))
        return page
    monkeypatch.setattr(wa, 'message_page', fake)
    monkeypatch.setattr(routes, 'require_admin', lambda request: None)
    app = FastAPI()
    app.include_router(routes.setup_whatsapp_routes())
    with TestClient(app) as client:
        response = client.get('/api/whatsapp/message-page', params={'chat': 'x@g.us', 'cursor': 'before', 'limit': 2})
    assert response.status_code == 200
    assert response.json() == page
    assert calls == [('x@g.us', {'cursor': 'before', 'limit': 2})]
    result = wt.read({'action': 'message_page', 'chat': 'x@g.us', 'cursor': 'before', 'limit': 2})
    assert result['messages'] == page['messages'] and result['next_cursor'] == 'opaque'
    assert result['exit_code'] == 0
    monkeypatch.setattr(wa, 'message_page', lambda *a, **kw: (_ for _ in ()).throw(wa.BridgeError('invalid cursor for this chat')))
    with TestClient(app) as client:
        assert client.get('/api/whatsapp/message-page', params={'chat': 'x@g.us'}).status_code == 400


def test_real_node_sync_status_phases():
    module = (ROOT / 'bridges/whatsapp/sync_status.mjs').as_uri()
    script = '''
import {syncView} from MODULE;
import assert from 'node:assert/strict';
const fresh = () => ({startedAt: null, pending: false, received: 0, history: {batches: 0, added: 0, progress: null, lastAt: null}});
let s = fresh();
assert.equal(syncView(s).active, false);
s.startedAt = 1000; s.pending = true; s.received = 4;
let v = syncView(s, 2000);
assert.equal(v.phase, 'catching_up'); assert.equal(v.active, true); assert.equal(v.progress, null); assert.equal(v.received, 4);
assert.equal(syncView(s, 1000 + 121000).phase, 'idle');
s.pending = false; s.history.lastAt = 2000; s.history.progress = 40;
v = syncView(s, 5000); assert.equal(v.phase, 'history'); assert.equal(v.progress, 40);
v = syncView(s, 2000 + 16000); assert.equal(v.phase, 'idle'); assert.equal(v.active, false);
s.history.lastAt = 30000; s.history.progress = 100;
assert.equal(syncView(s, 30001).phase, 'idle');
console.log('sync-status-pass');
'''.replace('MODULE', json.dumps(module))
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
