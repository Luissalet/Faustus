import json
import os
import pytest


@pytest.fixture
def shared(monkeypatch, tmp_path):
    if os.environ.get('FAUSTUS_TEST_COORDINATION_SOURCE'):
        monkeypatch.syspath_prepend(os.environ['FAUSTUS_TEST_COORDINATION_SOURCE'])
    native = pytest.importorskip('hoard_link.hub.agora')
    if not any('ADD COLUMN context' in step for step in native.MIGRATIONS if isinstance(step,str)):
        pytest.skip('Requires the shared task ledger package with project compatibility')
    from src import constants, project_tasks, project_task_compat
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(project_task_compat, 'marker', lambda pid: tmp_path/'adopted'/(pid+'.json'))
    ledger = native.Agora(tmp_path/'shared.db')
    def rpc(tool, args):
        try: return getattr(ledger,tool.removeprefix('hub_agora_'))(args)
        except native.AgoraError as exc: return {'ok':False,'error':str(exc),'status':exc.status}
    monkeypatch.setattr(project_tasks, 'call', rpc)
    project = {'id':'compat-test','name':'Compat test','workspace':str(tmp_path/'workspace')}
    (tmp_path/'workspace').mkdir()
    yield project, ledger
    ledger.close()


@pytest.mark.asyncio
async def test_real_board_tools_and_editor_use_same_task_without_changing_sources(shared):
    from src import project_tasks, project_board
    from src.agent_tools.board_tools import BoardCreateTool, BoardUpdateTool, BoardCommentTool, BoardGetTool
    project, ledger = shared
    project_tasks.prepare(project)
    ctx={'project_id':project['id'],'owner':''}
    created = await BoardCreateTool().execute({'title':'Agent task','type':'bug','body':'First','labels':['shared']},ctx)
    assert created['exit_code']==0,created
    key=created['issue']['id'];tid=created['issue']['canonical_id']
    assert project_tasks.listing(project)['tasks'][0]['id']==tid
    updated=await BoardUpdateTool().execute({'id':key,'status':'done','body':'Done by tool','type':'idea','labels':['urgent']},ctx)
    assert updated['exit_code']==0,updated
    comment=await BoardCommentTool().execute({'id':key,'body':'Completion proof'},ctx)
    assert comment['exit_code']==0,comment
    detail=project_tasks.command(project,'task',{'task_id':tid})
    assert (detail['task']['status'],detail['task']['body'],detail['task']['labels'])==('done','Done by tool',['urgent'])
    assert any(msg['body']=='Completion proof' for msg in detail['messages'])
    project_tasks.command(project,'task_edit',{'task_id':tid,'expected_etag':detail['task']['etag'],'status':'open','body':'Luis reopened','kind':'task','labels':['clarification']})
    read=await BoardGetTool().execute({'id':key},ctx)
    assert (read['issue']['status'],read['issue']['body_md'],read['issue']['labels'])==('open','Luis reopened',['clarification'])
    assert project_tasks.prepare(project)['imported']==0
    assert project_board.Store().list_issues(project['id'])[0]==[]


def test_legacy_ids_objectives_and_requirements_are_canonical_after_adoption(shared):
    from src import project_tasks, project_board
    from src.requirements import store as req
    from services import objectives
    project,ledger=shared;pid=project['id']
    old=project_board.Store().create_issue(pid,'OLD',type='task',title='Legacy issue',body_md='Original')
    requirement=req.Store().create(pid,title='Legacy requirement',text='Initial',acceptance=['Original criterion'])
    oid=objectives.apply_deltas(project,[{'op':'ADD','title':'Legacy objective','priority':1}],'user')['applied'][0]['id']
    project_tasks.prepare(project)
    project_board.update_issue(old['id'],{'status':'done','body_md':'Same shared task'})
    assert project_board.Store().get(old['id'])['body_md']=='Original'
    imported=next(t for t in project_tasks.listing(project)['tasks'] if t['origin_ref'].endswith(':issue:'+old['id']))
    assert imported['body']=='Same shared task' and imported['status']=='done'
    objectives.apply_deltas(project,[{'op':'EDIT','id':oid,'notes':'Updated from normal objective tool','priority':4}],'agent')
    row=next(v for v in project_tasks.listing(project)['tasks'] if v['origin_ref'].endswith(':objective:'+oid))
    assert row['body']=='Updated from normal objective tool' and row['priority']==3
    assert objectives.add_evidence(project,oid,'test','CANONICAL-PROOF',1.0)
    assert 'CANONICAL-PROOF' in json.dumps(project_tasks.command(project,'task',{'task_id':row['id']}))
    req.update(pid,requirement['key'],{'text':'Updated requirement','acceptance':['New criterion']},by='human')
    value=req.get(pid,requirement['key'])
    native=project_tasks.command(project,'task',{'task_id':value['canonical_id']})['task']
    assert native['body']=='Updated requirement' and native['acceptance']=='New criterion'
    assert req.Store().get(pid,requirement['key'])['text']=='Initial'
    assert req.revisions(pid,requirement['key'])


def test_normal_proposal_writer_and_dependency_share_canonical_records(shared):
    from src import project_tasks, project_board
    from src.requirements import store as req
    project,ledger=shared;pid=project['id'];project_tasks.prepare(project)
    proposed=req.create(pid,title='Proposal',text='New from req_propose',proposed_by='model',status='accepted')
    assert proposed['status']=='proposed'
    with pytest.raises(req.RequirementsError): req.update(pid,proposed['key'],{'status':'accepted'},by='model')
    a=project_board.create_issue(pid,'XX',type='task',title='First')
    b=project_board.create_issue(pid,'XX',type='task',title='Second')
    project_board.add_link(b['id'],'blocked_by',a['id'])
    assert project_board.get(b['id'])['blocked_by']==[a['id']]
    project_board.add_ref(a['id'],'file','proof.txt',label='proof')
    assert project_board.find_issue_by_ref(pid,'file','proof.txt','proof')['id']==a['id']
    assert len(project_tasks.listing(project)['tasks'])==3


@pytest.mark.asyncio
async def test_proposal_evidence_and_meeting_writers_keep_their_contracts(shared, monkeypatch, tmp_path):
    from src import project_tasks, project_board, meetings
    from src.agent_tools.requirement_tools import ReqProposeTool
    from src.requirements import store as req, evidence
    from src.agent_tools.board_tools import MeetingActionsToBoardTool
    project,ledger=shared;pid=project['id'];project_tasks.prepare(project)
    proposed=await ReqProposeTool().execute({'title':'Actual proposal tool','text':'Shared text'}, {'project_id':pid,'owner':''})
    assert proposed['exit_code']==0,proposed
    key=proposed['requirement']['key']
    link=req.add_link(pid,key,kind='evidences',target='run:proof',revision='',content_hash=None,created_by='tester')
    requirement=req.get(pid,key)
    assert requirement['current_revision']==link['req_revision_at_link']
    assert evidence.matrix(pid,key)['verified']
    req.update(pid,key,{'text':'Changed criterion'},by='human')
    assert not evidence.matrix(pid,key)['verified']
    assert req.list_links(pid,key)[0]['state']=='stale'
    folder=tmp_path/'meetings';folder.mkdir();monkeypatch.setattr(meetings,'MEETINGS_DIR',str(folder))
    (folder/'test.json').write_text(json.dumps({'id':'test','title':'Meeting','owner':'','project_id':pid,'model_ok':True}),encoding='utf-8')
    (folder/'test.md').write_text('## Action items\n- Prepare migration (responsable: Ana García)\n',encoding='utf-8')
    result=await MeetingActionsToBoardTool().execute({'meeting_id':'test','commit':True},{'project_id':pid,'owner':''})
    assert result['exit_code']==0 and result['created']==1,result
    issue=project_board.get(result['actions'][0]['issue_id'])
    assert issue['assignee']=='Ana García' and issue['refs']
    repeat=await MeetingActionsToBoardTool().execute({'meeting_id':'test','commit':True},{'project_id':pid,'owner':''})
    assert repeat['created']==0


def test_existing_markdown_import_and_export_use_shared_records(shared):
    from pathlib import Path
    from src import project_tasks, project_board
    project,ledger=shared;project_tasks.prepare(project)
    (Path(project['workspace'])/'OBJETIVOS.md').write_text('## OBJ-1: Imported goal\n\nOriginal context.\n',encoding='utf-8')
    preview=project_board.import_sources(project,'SH',sources=['objetivos'],dry_run=True)
    assert len(preview['preview'])==1 and not project_tasks.listing(project)['tasks']
    assert project_board.import_sources(project,'SH',sources=['objetivos'],dry_run=False)['created']==1
    assert project_board.import_sources(project,'SH',sources=['objetivos'],dry_run=False)['created']==0
    assert 'Original context.' in project_board.export_markdown(project['id'],'SH',project['name'])
    assert project_board.Store().list_issues(project['id'])[0]==[]


def test_adoption_keeps_operational_dependencies_aliases_and_personal_assignment(shared):
    from src import project_tasks, project_board
    from services import objectives
    project,ledger=shared;pid=project['id'];raw=project_board.Store()
    first=raw.create_issue(pid,'OLD',type='task',title='Prerequisite')
    second=raw.create_issue(pid,'OLD',type='task',title='Dependent',assignee='Ana García')
    raw.add_link(second['id'],'blocked_by',first['id'])
    a=objectives.apply_deltas(project,[{'op':'ADD','title':'Goal prerequisite'}],'user')['applied'][0]['id']
    b=objectives.apply_deltas(project,[{'op':'ADD','title':'Goal dependent','deps':[a]}],'user')['applied'][0]['id']
    project_tasks.prepare(project)
    dependent=project_board.get(second['id'])
    assert dependent['blocked_by']==[first['id']] and dependent['assignee']=='Ana García'
    assert second['id'] not in [row['id'] for row in project_board.ready_issues(pid)]
    assert {'from':b,'to':a} in objectives.load_state(project)['edges']
    relation=next(link for link in dependent['links'] if link['kind']=='blocked_by')
    assert project_board.remove_link(second['id'],relation['id'])
    assert not project_board.get(second['id'])['blocked_by']
    project_tasks.prepare(project)
    assert not project_board.get(second['id'])['blocked_by'],'reopening cannot restore an intentionally removed edge'
    assert raw.get(second['id'])['blocked_by']==[first['id']]


def test_creation_links_and_imported_requirement_link_state_stay_canonical(shared):
    from src import project_tasks, project_board
    from src.requirements import store as req, evidence
    project,ledger=shared;pid=project['id'];raw=req.Store()
    original=raw.create(pid,title='Legacy requirement',text='Original')
    link=raw.add_link(pid,original['key'],kind='evidences',target='proof:old',revision='',content_hash=None,created_by='tester')
    project_tasks.prepare(project)
    first=project_board.create_issue(pid,'NEW',type='task',title='Prerequisite')
    second=project_board.create_issue(pid,'NEW',type='task',title='New dependent',links=[{'kind':'blocked_by','target':first['id']},{'kind':'relates_to','target':first['id']}])
    assert second['blocked_by']==[first['id']]
    assert {row['kind'] for row in second['links']}=={'blocked_by','relates_to'}
    assert any(row['kind']=='blocks' and row['target_issue_id']==second['id'] for row in project_board.get(first['id'])['links'])
    req.update(pid,original['key'],{'text':'Changed after adoption'},by='human')
    assert not evidence.matrix(pid,original['key'])['verified']
    assert req.list_links(pid,original['key'])[0]['state']=='stale'
    assert raw.list_links(pid,original['key'])[0]['state']=='linked','import sources must stay read-only'
