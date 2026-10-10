"""Composite metadata never substitutes for authorization of every root/path."""
from dataclasses import replace
import pytest
from src.coordination.authority import authorize, paths, repositories, task_action_scope
from src.coordination.bindings import Binding, OPERATIONS, CoordinationError
from src.coordination.client import Client, compact_board
from src.coordination.store import Store
from tests.test_coordination_native import Manager

def bound(**overrides):
    values=dict(id='multi',owner='qa',project_id='project',session_id='chat',connector_id='hub-a',
        actor='faustus-multi',operations=tuple(OPERATIONS),repos=('HoardLink','Faustus'),
        resources=('path:HoardLink/hoard_link','path:Faustus/src'),thread_ids=(32,))
    return Binding(**{**values,**overrides})

def task(**overrides):
    values=dict(id=186,repo='HoardLink,Faustus',paths=['HoardLink/hoard_link/hub/a.py','Faustus/src/coordination/b.py'],
        owner=None,status='open',thread_id=72,submission_revision=1)
    return {**values,**overrides}

def test_each_qualified_root_is_checked_once_and_default_locks_match():
    b=bound();t=task()
    expected=['path:hoardlink/hoard_link/hub/a.py','path:faustus/src/coordination/b.py']
    assert paths(b,t['repo'],t['paths'])==expected
    assert authorize(b,'claim',{'task_id':186},task=t)['locks']==expected
    assert authorize(b,'add',{'title':'Both','repo':t['repo'],'paths':t['paths'],'claim':True})['locks']==expected
    assert 'claim' in task_action_scope(b,t)['allowed']

@pytest.mark.parametrize('repos',[('Faustus',),('HoardLink',),('HoardLink,Faustus','Faustus')])
def test_no_part_or_composite_alias_can_grant_missing_repository(repos):
    b=bound(repos=repos)
    with pytest.raises(CoordinationError,match='repository is outside'):
        paths(b,task()['repo'],task()['paths'])
    assert compact_board({'tasks':[task()]},b)['tasks'][0]['scope']=='global_context'

def test_all_paths_checked_including_the_one_omitted_from_preview():
    t=task(paths=['Faustus/src/'+str(i) for i in range(4)]+['HoardLink/private/secret'])
    row=compact_board({'tasks':[t]},bound())['tasks'][0]
    assert row['scope']=='project' and row['paths_omitted']==1
    assert row['action_scope']['allowed']==[]

@pytest.mark.parametrize('path',['src/a.py','Other/src/a.py','/Faustus/src/a.py','\\Faustus\\src\\a.py',
    'Faustus/src/../private/a.py','Faustus/src/./a.py','Faustus//src/a.py','C:/Faustus/src/a.py',
    'Faustus/src/a:secret','Faustus/src/*','Faustus/src/%2e','Faustus/src/\x00a'])
def test_ambiguous_outside_or_unsafe_paths_never_dispatch(path):
    with pytest.raises(CoordinationError):paths(bound(),task()['repo'],[path])

@pytest.mark.parametrize('repo',['HoardLink,Unknown','HoardLink,','HoardLink,,Faustus','HoardLink/Faustus','HoardLink:Faustus'])
def test_invalid_or_missing_constituents_are_not_silently_dropped(repo):
    with pytest.raises(CoordinationError):repositories(bound(),repo)

def test_normalized_backslashes_and_case_and_spaces_are_accepted():
    assert paths(bound(),' HOARDLINK , faustus ',['HoArDlInK\\hoard_link\\hub\\a.py','FAUSTUS\\src\\b.py'])==[
        'path:hoardlink/hoard_link/hub/a.py','path:faustus/src/b.py']

@pytest.mark.parametrize('path',['src/a.py','Faustus/src/a.py','faustus\\src\\a.py'])
def test_single_repo_accepts_relative_and_once_qualified_paths(path):
    assert paths(bound(),'Faustus',[path])==['path:Faustus/src/a.py']

def test_explicit_legacy_literal_comma_repository_keeps_its_own_root():
    b=bound(repos=('HoardLink,Faustus',),resources=('path:HoardLink,Faustus/src',))
    assert paths(b,'HoardLink,Faustus',['src/a.py'])==['path:HoardLink,Faustus/src/a.py']
    assert paths(b,'HoardLink,Faustus',['HoardLink,Faustus/src/a.py'])==['path:HoardLink,Faustus/src/a.py']
    with pytest.raises(CoordinationError):paths(b,'HoardLink,Faustus',['Faustus/src/a.py'])

def test_closed_composite_task_reports_closed_rather_than_fake_outside_scope():
    for owner in ('faustus-multi','qa-author'):
        reasons=task_action_scope(bound(),task(owner=owner,status='done'))['unavailable']
        assert all('already closed'==item['reason'].removeprefix('task is ') for item in reasons)

@pytest.mark.asyncio
async def test_native_delivery_and_preflight_keep_full_multirepo_authority(tmp_path):
    store=Store(tmp_path);b=store.save(bound());manager=Manager();t=task()
    manager.responses['task']={'ok':True,'task':t,'messages':[]}
    client=Client(store,b,manager,revalidate=lambda _:None)
    result=await client.execute('task',{'task_id':186})
    assert 'claim' in result['task']['action_scope']['allowed']
    with pytest.raises(CoordinationError):await client.execute('claim',{'task_id':186})
    store.confirm_task_delivery(b,result['_coordination_delivery']['task_receipt'])
    await client.execute('claim',{'task_id':186})
    sent=manager.calls[-1][1]
    assert sent['locks']==['path:hoardlink/hoard_link/hub/a.py','path:faustus/src/coordination/b.py']
    assert sent['agent']==b.actor
    manager.responses['task']['task']={**t,'paths':['Faustus/src/a.py','Other/private/a.py']}
    before=len([name for name,_ in manager.calls if name.endswith('_task_claim')])
    with pytest.raises(CoordinationError,match='authority changed'):await client.execute('claim',{'task_id':186})
    assert len([name for name,_ in manager.calls if name.endswith('_task_claim')])==before
