"""Stored-source research uses its observed results without workspace bookkeeping."""
import json
import pytest
from src import agent_loop as al, agent_harness as h, deliverables
from test_unattended_conversation import _collect, _events, _patch_common, _scripted, _text

ARTIFACT = "occ_" + "a" * 32
REQUEST = "Read the source text stored in artifact "+ARTIFACT+" with read_artifact and return the original line numbers in your answer."

def read_ledger(tmp_path, prompt=REQUEST):
    ledger = h.TurnLedger(str(tmp_path), prompt)
    ledger.record("read_artifact", json.dumps({"artifact_id":"occ_fixture","query":"FirstBattle"}), {"output":"line 3: FirstBattle();","exit_code":0})
    return ledger

def test_four_saved_reads_finish_without_forced_progress_or_completion_audit(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    calls=[]
    async def read(block,*args,**kwargs):
        calls.append(block.tool_type)
        return block.tool_type,{"output":f"Stored as artifact {ARTIFACT}; source line {len(calls)}.","artifact_id":ARTIFACT,"exit_code":0}
    monkeypatch.setattr(al,"execute_tool_block",read)
    reads = [_text('\x60\x60\x60read_artifact\n'+json.dumps({"artifact_id":ARTIFACT,"query":"FirstBattle"+str(i)})+'\n\x60\x60\x60', "tool_calls") for i in range(4)]
    seen = _scripted(monkeypatch, [*reads, _text("The saved source has FirstBattle on line 3.")])
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "fixture-model", [{"role":"user","content":REQUEST}],
        workspace=str(tmp_path), unattended=True, max_rounds=8,
        relevant_tools={"read_artifact","todowrite"},
    )))
    if len(seen) != 5:
        print("FIXTURE_EVENTS="+json.dumps([{k:e.get(k) for k in ("type","status","reason","tool","error","stop_reason")} for e in events]))
    assert len(seen) == 5
    assert not any(e.get("reason")=="completion_audit" for e in events)
    notes=[m.get("content","") for messages in seen for m in messages if m.get("_harness_note")]
    assert not any("todowrite ONCE" in str(note) for note in notes)

def test_a_failed_read_does_not_exempt_requested_work(tmp_path):
    ledger=h.TurnLedger(str(tmp_path), REQUEST)
    ledger.record("read_artifact", "{}", {"error":"artifact not found","exit_code":1})
    assert al._workspace_completion_work({"workspace_work":True}, [], ledger)

def test_a_source_filename_in_a_read_request_is_not_a_local_deliverable(tmp_path):
    prompt=REQUEST+" The saved source is generated/trainers.txt; quote its symbols."
    files=deliverables.requested_files(prompt)
    assert files==[]
    assert not al._workspace_completion_work({"workspace_work":True}, files, read_ledger(tmp_path,prompt))

@pytest.mark.parametrize("tool,args",[("read_file",{"path":"cart.py"}),("write_file",{"path":"cart.py","content":"x=1"}),("powershell",{"command":"Get-Content cart.py"})])
def test_local_work_still_requires_the_existing_audit(tmp_path,tool,args):
    ledger=read_ledger(tmp_path)
    ledger.record(tool,json.dumps(args),{"output":"ok","exit_code":0})
    assert al._workspace_completion_work({"workspace_work":True},[],ledger)

@pytest.mark.parametrize("prompt",["Read artifact ABC and build an app.","Consulta el artefacto ABC y corrige cart.py.","Read artifact ABC and save a report."])
def test_an_explicit_action_keeps_the_existing_workspace_path(tmp_path,prompt):
    assert al._workspace_completion_work({"workspace_work":True},[],read_ledger(tmp_path,prompt))

def test_published_content_references_and_real_edits_are_distinct():
    assert h.find_claimed_paths("Incluido en el post: el índice de generated/trainers.txt; he modificado cart.py.")==["cart.py"]
    assert h.find_claimed_paths("Added in the message: results from source.txt. I updated result.py.")==["result.py"]
    assert h.find_claimed_paths("He incluido la función en post.md.")==["post.md"]

@pytest.mark.parametrize("article",["la","una"])
def test_published_response_reference_does_not_claim_file_edits(article):
    assert h.find_claimed_paths(f"Incluido en {article} respuesta: el índice pertenece a generated/trainers.txt.")==[]
    assert h.find_claimed_paths(f"Incluido en {article} respuesta: el índice pertenece a generated/trainers.txt; he modificado module.py.")==["module.py"]
    assert h.find_claimed_paths("He incluido la función en post.md.")==["post.md"]

@pytest.mark.parametrize("tools",[("mcp__85276bea__hub_agora_post",),("mcp__85276bea__hub_agora_thread","mcp__85276bea__hub_agora_post")])
def test_generic_mcp_coordination_is_not_observed_workspace_execution(tmp_path,tools):
    ledger=read_ledger(tmp_path)
    for tool in tools:
        ledger.record(tool,json.dumps({"thread_id":1890,"body":"original source ordinals"}),{"output":"ok","exit_code":0})
    caps=al.capabilities_for_tool(tools[-1])
    assert not caps.known
    assert al.ToolEffect.EXECUTE_CODE in caps.effects
    assert not al._workspace_completion_work({"workspace_work":True},[],ledger)


PUBLIC_REQUEST = 'En el Ágora Silver, amplía la investigación de HoardDex1747 sobre el rival del acceso entre Ciudad Corazón y Ruta 209 de Platino US. Tu tabla anterior de equipos está contrastada. La referencia fija sigue siendo pret/pokeplatinum@be36a9db3d2390a5009adc087c7aef550ce335e7. Lee las tres fuentes públicas que resuelven el guion que faltaba: res/field/scripts/scripts_route_209_gate_to_hearthome_city.s, res/field/events/events_route_209_gate_to_hearthome_city.json y res/field/scripts/scripts_hearthome_city_gym_leader_room.s. Determina dónde ocurre el combate, qué activa el evento, qué inicial del jugador selecciona cada variante y qué sucede al ganar o perder. Usa las herramientas normales de lectura y coordinación del proyecto y publica una sola ampliación factual en el hilo1890 con las fuentes y límites. Se pide lectura y respuesta; Root ya prepara el banco de producto1780. Termina con un resumen breve coherente con la ampliación.'
PUBLIC_EN = 'Read the public sources at https://github.com/pret/pokeplatinum/blob/be36a9db/file.s and report the trigger in your answer.'
PUBLIC_ES = 'Lee las fuentes públicas de GitHub y publica las condiciones del combate en el Ágora.'

def public_source_ledger(tmp_path, prompt=PUBLIC_REQUEST, tool='web_fetch', ok=True):
    l = h.TurnLedger(str(tmp_path), prompt)
    for i in range(3):
        l.record(tool, json.dumps({'url': 'https://github.com/pret/pokeplatinum/blob/be36a9db/file' + str(i) + '.s'}), {'output': 'source lines', 'exit_code': 0 if ok else 1})
    l.record('coordination_post', json.dumps({'thread_id': 1890, 'body': 'source facts'}), {'output': '21402', 'exit_code': 0})
    return l

@pytest.mark.parametrize('prompt', [PUBLIC_REQUEST, PUBLIC_EN, PUBLIC_ES])
def test_public_source_answers_need_no_workspace_audit(tmp_path, prompt):
    assert not al._workspace_completion_work({'workspace_work': True}, [], public_source_ledger(tmp_path, prompt))

def test_public_reach_read_answer_needs_no_workspace_audit(tmp_path):
    assert not al._workspace_completion_work({'workspace_work': True}, [], public_source_ledger(tmp_path, PUBLIC_EN, 'reach_read'))

@pytest.mark.parametrize('suffix', [' Implement the change.', ' Save a report.', ' Download the repository.', ' Export the result.', ' Corrige el generador.', ' Guarda el informe.', ' Descarga las tablas.', ' Exporta la respuesta.'])
def test_explicit_work_keeps_completion(tmp_path, suffix):
    assert al._workspace_completion_work({'workspace_work': True}, [], public_source_ledger(tmp_path, PUBLIC_EN + suffix))

@pytest.mark.parametrize('tool,args', [('read_file', {'path': 'cart.py'}), ('write_file', {'path': 'cart.py', 'content': 'x=1'}), ('bash', {'command': 'ls'})])
def test_local_action_keeps_completion(tmp_path, tool, args):
    l = public_source_ledger(tmp_path)
    l.record(tool, json.dumps(args), {'output': 'ok', 'exit_code': 0})
    assert al._workspace_completion_work({'workspace_work': True}, [], l)

def test_local_deliverable_keeps_completion(tmp_path):
    assert al._workspace_completion_work({'workspace_work': True}, ['answer.md'], public_source_ledger(tmp_path))

def test_failed_sources_do_not_exempt(tmp_path):
    assert al._workspace_completion_work({'workspace_work': True}, [], public_source_ledger(tmp_path, ok=False))

def test_search_alone_does_not_exempt(tmp_path):
    assert al._workspace_completion_work({'workspace_work': True}, [], public_source_ledger(tmp_path, tool='web_search'))

def test_unrelated_request_does_not_exempt(tmp_path):
    assert al._workspace_completion_work({'workspace_work': True}, [], public_source_ledger(tmp_path, 'Describe the current workspace.'))

def test_just_publication_does_not_exempt(tmp_path):
    l = h.TurnLedger(str(tmp_path), PUBLIC_EN)
    l.record('coordination_post', '{}', {'output': 'ok', 'exit_code': 0})
    assert al._workspace_completion_work({'workspace_work': True}, [], l)

def test_four_public_reads_finish_without_forced_progress_or_completion_audit(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    calls = []

    async def read(block, *args, **kwargs):
        calls.append(block.tool_type)
        return (block.tool_type, {'output': f'Stored as artifact {ARTIFACT}; source {len(calls)} line 3: FirstBattle.', 'artifact_id': ARTIFACT, 'exit_code': 0})
    monkeypatch.setattr(al, 'execute_tool_block', read)
    reads = [_text('```web_fetch\n' + json.dumps({'url': 'https://example.com/source' + str(i)}) + '\n```', 'tool_calls') for i in range(4)]
    seen = _scripted(monkeypatch, [*reads, _text('The four public sources contain FirstBattle on line 3.'), _text('The four public sources contain FirstBattle on line 3.')])
    events = _events(_collect(al.stream_agent_loop('http://127.0.0.1:11434/v1', 'fixture-model', [{'role': 'user', 'content': 'Read the four public sources ' + ', '.join('https://example.com/source' + str(i) for i in range(4)) + ' and report their contents in your answer.'}], workspace=str(tmp_path), unattended=True, max_rounds=8, relevant_tools={'web_fetch', 'todowrite'})))
    if len(seen) != 6:
        print('FIXTURE_EVENTS=' + json.dumps([{k: e.get(k) for k in ('type', 'reason', 'tool')} for e in events]))
    assert len(seen) == 6
    assert not any((e.get('reason') == 'completion_audit' for e in events))
    notes = [m.get('content', '') for messages in seen for m in messages if m.get('_harness_note')]
    assert not any(('todowrite ONCE' in str(note) for note in notes))


MCP_REQUEST = 'Read these public sources https://example.com/source.json and publish the table in your answer.'
MCP_SOURCE_TOOL = 'mcp__connector_fixture__hub_web_fetch'

def mcp_source_ledger(tmp_path, **changes):
    l = h.TurnLedger(str(tmp_path), MCP_REQUEST)
    payload = {'ok': True, 'status': 200, 'url': 'https://example.com/source.json', 'content': 'source text'}
    payload.update(changes)
    l.record(MCP_SOURCE_TOOL, json.dumps({'url': 'https://example.com/source.json'}), {'output': json.dumps(payload), 'exit_code': 0})
    l.record('coordination_post', '{}', {'output': 'posted', 'exit_code': 0})
    return l

def test_observed_mcp_source_read_is_not_workspace_work(tmp_path):
    assert not al._workspace_completion_work({'workspace_work': True}, [], mcp_source_ledger(tmp_path))

@pytest.mark.parametrize('change', [{'ok': False}, {'status': 403}, {'content': ''}, {'content': None}, {'url': 'https://example.com/unrequested.json'}])
def test_failed_or_unmatched_receipts_do_not_exempt(tmp_path, change):
    assert al._workspace_completion_work({'workspace_work': True}, [], mcp_source_ledger(tmp_path, **change))

@pytest.mark.parametrize('tool,args', [('read_file', {'path': 'x.py'}), ('write_file', {'path': 'x.py', 'content': 'x=1'}), ('bash', {'command': 'pwd'})])
def test_real_local_work_still_requires_completion(tmp_path, tool, args):
    l = mcp_source_ledger(tmp_path)
    l.record(tool, json.dumps(args), {'output': 'ok', 'exit_code': 0})
    assert al._workspace_completion_work({'workspace_work': True}, [], l)

def test_source_receipt_does_not_change_authorization(tmp_path):
    caps = al.capabilities_for_tool(MCP_SOURCE_TOOL)
    assert not caps.known
    assert al.ToolEffect.WRITE_WORKSPACE in caps.effects
    assert al._workspace_completion_work({'workspace_work': True}, ['result.md'], mcp_source_ledger(tmp_path))

def test_remote_read_does_not_hide_a_recorded_mutation(tmp_path):
    l = h.TurnLedger(str(tmp_path), 'Read the public source https://example.com/a and answer.')
    l.record('mcp__fixture__reader', json.dumps({'url': 'https://example.com/a'}), {'output': json.dumps({'ok': True, 'status': 200, 'url': 'https://example.com/a', 'content': 'source'}), 'exit_code': 0})
    l.events.append({'tool': 'fixture-worker', 'ok': True, 'kind': 'mutation', 'paths': ['changed.py']})
    assert al._workspace_completion_work({'workspace_work': True}, [], l)
