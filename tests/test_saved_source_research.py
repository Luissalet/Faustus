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
