"""Typed failures go to the recipe owner; the client never restarts a LAN process."""
import asyncio
import json

import pytest

from src import llm_core, model_server_heal, sparks_recovery

URL = "http://10.33.0.9:8012/v1"


@pytest.mark.parametrize('url',[
    'https://api.example.com/v1','http://8.8.8.8/v1','http://127.0.0.1:8012/v1',
    'http://localhost/v1','http://[::1]/v1','file:///tmp/model','http://0.0.0.0/v1',
    'http://224.0.0.1/v1','http://user:secret@10.33.0.9/v1',
    'http://10.33.0.9/v1?token=secret','http://10.33.0.9:wrong/v1',
])
def test_unsupported_endpoint_never_contacts_owner(monkeypatch,url):
    monkeypatch.setattr(sparks_recovery,'_send',lambda *a:pytest.fail('must not send'))
    assert asyncio.run(sparks_recovery.report(url,'model'))['action']=='skipped'


def test_report_uses_family_owner_and_does_not_claim_recovery(monkeypatch):
    from src import family_services
    from src.hoard_link import _famsvc
    calls=[]
    monkeypatch.setattr(family_services,'configure',lambda:None)
    def call(owner,tool,args,**kwargs):
        calls.append((owner,tool,args,kwargs))
        return {'ok':True,'data':{'id':'recovery-one','status':'pending','deduplicated':False}}
    monkeypatch.setattr(_famsvc,'call_tool',call)
    result=asyncio.run(sparks_recovery.report(URL+'/','model','repeated prose loop'))
    assert result['action']=='checking' and result['status']=='pending'
    assert calls==[('prometheus','deploy_report_issue',{
        'base_url':URL,'model':'model','error_class':'degenerate_output','reason':'repeated prose loop',
    },{'timeout_s':15.0})]


def test_unavailable_owner_never_becomes_healthy_or_restarted(monkeypatch):
    monkeypatch.setattr(sparks_recovery,'_send',lambda *a:{'ok':False,'kind':'tool_missing'})
    assert asyncio.run(sparks_recovery.report(URL,'model'))['action']=='unknown'
    monkeypatch.setattr(sparks_recovery,'_send',lambda *a:{'ok':True,'data':{'status':'inconclusive'}})
    assert asyncio.run(sparks_recovery.report(URL,'model'))['action']=='unknown'


def test_concurrent_lan_recovery_reports_share_attempt_without_local_kill(monkeypatch):
    monkeypatch.setattr(model_server_heal,'_settings',lambda:{'enabled':True})
    monkeypatch.setattr(model_server_heal,'listener_pid',lambda *a:pytest.fail('LAN is owned remotely'))
    calls=[]
    async def report(*args):
        calls.append(args)
        await asyncio.sleep(.01)
        return {'action':'checking','status':'pending'}
    monkeypatch.setattr(sparks_recovery,'report',report)
    async def run():
        model_server_heal._INFLIGHT.clear()
        results=await asyncio.gather(model_server_heal.heal(URL,'model'),model_server_heal.heal(URL,'model'))
        assert len(calls)==1 and all(r['action']=='checking' for r in results)
        assert not model_server_heal._INFLIGHT
    asyncio.run(run())


@pytest.mark.parametrize('reason',['repeated prose unit','repeated unit \'/\''])
def test_typed_stream_failure_reports_once_and_keeps_original_error(monkeypatch,reason):
    calls=[]
    monkeypatch.setattr(model_server_heal,'schedule',lambda *args:calls.append(args) or True)
    chunk=llm_core._degenerate_output_error_chunk(llm_core.DegenerateOutput(reason,'model',URL))
    assert calls==[(URL,'model',reason)]
    data=json.loads(chunk.split('data: ',1)[1])
    assert data['error_class']=='degenerate_output'
    assert 'asked the model service owner' in data['text']
    assert 'recovered' not in data['text']


def test_existing_auto_heal_setting_disables_lan_reports(monkeypatch):
    monkeypatch.setattr(model_server_heal,'_settings',lambda:{'enabled':False})
    assert model_server_heal.schedule(URL,'model') is False
    assert asyncio.run(model_server_heal.heal(URL,'model'))['action']=='off'


@pytest.mark.parametrize("status,action", [
    ("waiting_coordination", "checking"), ("checking", "checking"),
    ("recovered", "restarted"), ("inconclusive", "unknown"),
])
def test_owner_scope_survives_reporting_and_heal_history(monkeypatch, status, action):
    scope = {"verified_scope": "structural_generation_and_literal_manifest",
             "algorithm_semantics_verified": False, "canaries_discriminatively_validated": False}
    monkeypatch.setattr(sparks_recovery, "_send", lambda *args: {
        "ok": True, "data": {"id": "recovery-scope", "status": status, **scope}})
    monkeypatch.setattr(model_server_heal, "_settings", lambda: {"enabled": True})
    model_server_heal._INFLIGHT.clear()
    result = asyncio.run(model_server_heal.heal(URL, "model"))
    assert result["action"] == action and result["status"] == status
    assert {key: result[key] for key in scope} == scope
    assert {key: model_server_heal._EVENTS[-1][key] for key in scope} == scope
    if status == "inconclusive":
        assert "structural generation checks" in result["detail"]
        assert "healthy model" not in result["detail"]


def test_absent_owner_scope_is_not_invented(monkeypatch):
    monkeypatch.setattr(sparks_recovery, "_send", lambda *args: {
        "ok": True, "data": {"id": "legacy-owner", "status": "recovered"}})
    result = asyncio.run(sparks_recovery.report(URL, "model"))
    assert result["action"] == "restarted"
    assert not {"verified_scope", "algorithm_semantics_verified", "canaries_discriminatively_validated"} & result.keys()
