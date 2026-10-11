from pathlib import Path

import pytest

from src import markdown_hooks as hooks


def test_markdown_list_conditions_roundtrip_and_preserves_message():
    rule = {"name": "debug-note", "event": "file", "conditions": [
        {"field": "file_path", "operator": "ends_with", "pattern": ".py"},
        {"field": "new_text", "operator": "contains", "pattern": "print("}], "message": "Remove the temporary trace after verifying it."}
    markdown = hooks.to_markdown(rule)
    parsed = hooks.parse_rule(markdown)
    assert parsed["conditions"] == rule["conditions"]
    assert parsed["message"] == rule["message"]
    assert len(hooks.evaluate_rules([parsed], "pre_tool", {"paths": ["src/app.py"], "args": {"new_text": "print(42)"}})) == 1
    assert hooks.evaluate_rules([parsed], "pre_tool", {"paths": ["app.md"], "args": {"new_text": "print(42)"}}) == []


@pytest.mark.parametrize("kind,lifecycle,context", [
    ("bash", "pre_tool", {"command": "git add -A"}),
    ("file", "pre_tool", {"paths": ["app.py"], "content": "debugger"}),
    ("prompt", "turn_start", {"user_message": "use the fixture"}),
    ("stop", "turn_end", {"assistant_message": "finished fixture"}),
])
def test_all_four_events_adapt_to_existing_lifecycle(kind, lifecycle, context):
    rule = hooks.parse_rule(hooks.to_markdown({"name": "check", "event": kind, "pattern": ".*", "message": "Keep the evidence."}))
    hits = hooks.evaluate_rules([rule], lifecycle, context)
    assert len(hits) == 1 and hits[0]["action"] == "warn"
    assert hooks.evaluate_rules([rule], "post_tool", context) == []


def test_save_load_idempotent_conflict_and_explicit_edit(tmp_path):
    rule = {"name": "own-check", "event": "bash", "pattern": "pytest", "message": "Use the area tests."}
    saved = hooks.save_rule(str(tmp_path), rule)
    assert saved["status"] == "saved"
    assert Path(saved["path"]).is_relative_to(tmp_path)
    assert hooks.save_rule(str(tmp_path), rule)["status"] == "unchanged"
    changed = {**rule, "message": "Keep a meaningful reproduction."}
    assert hooks.save_rule(str(tmp_path), changed)["status"] == "conflict"
    assert hooks.load_rules(str(tmp_path))["rules"][0]["message"] == rule["message"]
    assert hooks.save_rule(str(tmp_path), changed, overwrite=True)["status"] == "saved"
    assert hooks.load_rules(str(tmp_path))["rules"][0]["message"] == changed["message"]


def test_invalid_regex_and_block_action_are_advisory_other_rules_survive(tmp_path):
    folder = tmp_path / ".faustus" / "hooks"
    folder.mkdir(parents=True)
    (folder / "invalid.md").write_text("---\nevent: bash\npattern: '['\naction: block\n---\nMechanical note\n", encoding="utf-8")
    (folder / "bad.md").write_text("---\nevent: unknown\n---\nBad event", encoding="utf-8")
    hooks.save_rule(str(tmp_path), {"name": "valid", "event": "bash", "pattern": "pytest", "message": "Retain output."})
    loaded = hooks.load_rules(str(tmp_path))
    assert len(loaded["warnings"]) == 3
    hits = hooks.lifecycle_matches("pre_tool", {"workspace": str(tmp_path), "command": "pytest tests/test_real.py"})
    assert len(hits) == 1 and hits[0]["action"] == "warn"


def test_existing_settings_hooks_and_project_markdown_both_run_without_block(tmp_path, monkeypatch):
    from src import lifecycle_hooks
    hooks.save_rule(str(tmp_path), {"name": "project-check", "event": "bash", "pattern": "pytest", "message": "Mechanical project message."})
    monkeypatch.setattr(lifecycle_hooks, "_load_hooks", lambda: [{"id": "global", "enabled": True, "event": "pre_tool", "action": "warn", "text": "Existing global message.", "match": {}, "scope": "global"}])
    hits = lifecycle_hooks.evaluate("pre_tool", {"workspace": str(tmp_path), "command": "pytest"})
    assert [row["id"] for row in hits] == ["global", "markdown-project-check"]
    assert all(row["action"] == "warn" for row in hits)


def test_corrections_propose_mechanical_checks_without_install_or_model(tmp_path):
    result = hooks.propose_hooks([
        {"role": "assistant", "content": "I used git add -A"},
        {"role": "user", "content": "No uses `git add -A`; indexa solo tus rutas."},
        {"role": "user", "content": "Recuerda revisar `src/app.py`."},
        {"role": "user", "content": "Ya te dije que conserves las capacidades."},
        {"role": "user", "content": "Please explain the plan."},
    ])
    assert len(result["proposals"]) == 2 and len(result["notes"]) == 1
    assert result["installed"] is False and not (tmp_path / ".faustus").exists()
    assert result["proposals"][0]["event"] == "bash"
    assert result["proposals"][1]["event"] == "file"
    assert hooks.parse_rule(result["proposals"][0]["markdown"])["conditions"][0]["pattern"] == "git add -A"


def test_disabled_and_and_conditions_and_relative_paths(tmp_path):
    rule = hooks.parse_rule("---\nname: check\nevent: file\nconditions:\n  file_path: 'src/.*\\.py$'\n  content: 'debug'\n---\nClean the trace.\n")
    assert hooks.evaluate_rules([rule], "pre_tool", {"workspace": str(tmp_path), "paths": [str(tmp_path / "src" / "app.py")], "content": "debug"})
    assert not hooks.evaluate_rules([rule], "pre_tool", {"paths": ["src/app.py"], "content": "normal"})
    rule["enabled"] = False
    assert not hooks.evaluate_rules([rule], "pre_tool", {"paths": ["src/app.py"], "content": "debug"})


def test_actual_native_tool_json_supplies_body_and_path_not_whole_envelope():
    import json
    rule = hooks.parse_rule(hooks.to_markdown({"name":"body-check","event":"file","conditions":[
        {"field":"file_path","operator":"ends_with","pattern":".py"},
        {"field":"new_text","operator":"starts_with","pattern":"print("}],"message":"Remove the temporary trace."}))
    assert hooks.evaluate_rules([rule],"pre_tool",{"tool":"write_file","content":json.dumps({"path":"src/app.py","content":"print(42)"})})
    assert not hooks.evaluate_rules([rule],"pre_tool",{"tool":"bash","command":"print(42)","paths":["src/app.py"],"content":"print(42)"})


def test_empty_file_body_is_not_replaced_by_the_json_envelope():
    import json
    rule=hooks.parse_rule(hooks.to_markdown({'name':'empty-body','event':'file','pattern':'^$','message':'Check the empty body.'}))
    assert hooks.evaluate_rules([rule],'pre_tool',{'tool':'write_file','content':json.dumps({'path':'empty.md','content':''})})
    assert not hooks.evaluate_rules([rule],'pre_tool',{'tool':'write_file','content':json.dumps({'path':'full.md','content':'Full body'})})


@pytest.mark.parametrize('correction',[
    'En vez de `echo viejo`, usa `echo nuevo`.',
    'No uses `echo viejo`; utiliza `echo nuevo`.',
    'Instead of `echo viejo`, use `echo nuevo`.',
])
def test_replacement_command_is_fix_evidence_not_a_warning_trigger(correction):
    proposals=hooks.propose_hooks([{'role':'user','content':correction}])['proposals']
    assert len(proposals)==1
    assert proposals[0]['conditions'][0]['pattern']=='echo viejo'
    assert 'echo nuevo' in proposals[0]['message']
    assert hooks.evaluate_rules(proposals,'pre_tool',{'command':'echo viejo'})
    assert not hooks.evaluate_rules(proposals,'pre_tool',{'command':'echo nuevo'})


def test_stop_conditions_read_only_the_live_final_answer_cache(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from core import models
    hooks.save_rule(str(tmp_path),{"name":"final-check","event":"stop","conditions":[{"field":"assistant_message","operator":"contains","pattern":"Verified"}],"message":"Keep the proof linked."})
    manager=SimpleNamespace(sessions={"owned":SimpleNamespace(history=[{"role":"user","content":"Please check"},{"role":"assistant","content":"Verified fixture output"}])})
    monkeypatch.setattr(models,'get_session_manager_instance',lambda:manager)
    ctx={"workspace":str(tmp_path),"session_id":"owned"}
    assert len(hooks.lifecycle_matches('turn_end',ctx)) == 1
    assert 'assistant_message' not in ctx
