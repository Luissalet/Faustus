"""A turn resumed after an approval card gets back the tool work it had done.

Seen live: the paused turn's eleven tool calls lived only in the saved
message's metadata, the resumed prompt replayed just its text, and the model
redid the work."""
from src.agent_loop import _paused_turn_work_note


def _paused(events):
    return [
        {"role": "user", "content": "¿Qué partes ya existen?"},
        {"role": "assistant", "content": "Voy a verificar los candidatos.",
         "metadata": {"tool_events": events}},
    ]


def test_note_lists_done_calls_and_skips_the_pending_approved_one():
    events = [
        {"tool": "prior_art", "command": '{"action":"rubric"}', "output": "Decompose this idea…"},
        {"tool": "prior_art", "command": '{"action":"verify"}', "output": "Kozea/WeasyPrint found, BSD-3-Clause"},
        {"tool": "reach_read", "command": "https://example.org/LICENSE", "output": "Waiting for an exact user approval"},
    ]
    note = _paused_turn_work_note(_paused(events), "reach_read")
    assert note and "Kozea/WeasyPrint found" in note and "rubric" in note
    assert "example.org/LICENSE" not in note
    assert "do not repeat them" in note


def test_completed_queries_of_the_same_tool_survive_the_next_approval():
    events = [
        {"tool": "mcp__homeqa__home_find_item", "command": '{"query":"Linterna Philips"}',
         "output": '{"id":"torch","quantity":1}', "exit_code": 0},
        {"tool": "mcp__homeqa__home_find_item", "command": '{"query":"Cable USB-C"}',
         "output": '{"id":"cable","quantity":2}', "exit_code": 0},
        {"tool": "mcp__homeqa__home_find_item", "command": '{"query":"Pilas AA"}',
         "output": "Waiting for an exact user approval", "exit_code": None},
    ]
    note = _paused_turn_work_note(_paused(events), "mcp__homeqa__home_find_item")
    assert '"id":"torch"' in note and '"id":"cable"' in note
    assert "Pilas AA" not in note


def test_explicit_pending_card_is_skipped_but_empty_success_is_preserved():
    events = [
        {"tool": "python", "command": "pass", "output": "", "exit_code": 0},
        {"tool": "python", "command": "print(2)", "output": "Approval required",
         "ask_user": {"kind": "tool_approval"}},
    ]
    note = _paused_turn_work_note(_paused(events), "python")
    assert note is not None and "pass" in note and "print(2)" not in note


def test_no_events_no_note():
    assert _paused_turn_work_note(_paused([]), "reach_read") is None
    assert _paused_turn_work_note([{"role": "user", "content": "hola"}], "x") is None


def test_only_the_latest_turns_are_searched():
    old = {"role": "assistant", "content": "antiguo",
           "metadata": {"tool_events": [{"tool": "grep", "command": "x", "output": "y"}]}}
    msgs = [old, {"role": "assistant", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert _paused_turn_work_note(msgs, "reach_read") is None


def test_the_note_is_bounded():
    events = [{"tool": "read_file", "command": f"f{i}.py", "output": "x" * 3000} for i in range(20)]
    note = _paused_turn_work_note(_paused(events), "reach_read", limit=4000)
    assert note is not None and len(note) < 4300


def test_identical_completed_calls_remain_countable_across_the_next_approval():
    command='{"recipe_id":"synthetic","servings":4}'
    events=[{'tool':'mcp__cook__recipe_check','command':command,'output':'{"stock_sufficient":true}','exit_code':0},
            {'tool':'mcp__cook__recipe_check','command':command,'output':'{"stock_sufficient":true}','exit_code':0},
            {'tool':'mcp__cook__what_to_cook','command':'{"servings":4}',
             'output':'Waiting for an exact user approval.','exit_code':None}]
    note=_paused_turn_work_note(_paused(events),'mcp__cook__what_to_cook')
    assert 'Recorded call 1:' in note and 'Recorded call 2:' in note
    assert note.count(command)==2
    assert 'additional' in note and 'not a replacement' in note
    assert 'mcp__cook__what_to_cook' not in note


def test_resume_counts_include_the_current_execution_with_canonical_arguments():
    import json
    tool = 'mcp__cook__recipe_check'
    command = '{"recipe_id":"synthetic","servings":4}'
    prior = {'tool': tool, 'command': command, 'output': '{}', 'exit_code': 0}
    current = {**prior, 'command': '{"servings": 4, "recipe_id": "synthetic"}'}
    note = _paused_turn_work_note(_paused([prior]), tool, current_execution=current)
    counts = [json.loads(line) for line in note.splitlines() if line.startswith('{"tool":')]
    assert counts == [{'tool': tool, 'arguments': command, 'succeeded': 2, 'failed': 0, 'unknown': 0}]
    assert note.index('"succeeded": 2') < note.index('Recorded call 1:')
    assert 'INCLUDING the current approved execution' in note
    assert 'Recorded call 2:' in note and 'Current approved execution, already INCLUDED' in note
    assert note.count('-> {}') == 1  # current result is injected separately


def test_count_index_separates_failures_unknown_results_and_changed_arguments():
    import json
    from src.agent_loop import _completed_execution_counts
    tool = 'mcp__cook__recipe_check'
    events = [{'tool': tool, 'command': '{"servings":4}', 'output': '{}', 'exit_code': rc}
              for rc in (0, 1, None)]
    events += [{'tool': tool, 'command': '{"servings":2}', 'output': '{}', 'exit_code': 0},
               {'tool': tool, 'command': '{"servings":4}',
                'output': 'Waiting for an exact user approval.', 'exit_code': None}]
    counts = [json.loads(line) for line in _completed_execution_counts(events).splitlines()]
    assert counts[0] == {'tool': tool, 'arguments': '{"servings":4}', 'succeeded': 1, 'failed': 1, 'unknown': 1}
    assert counts[1]['arguments'] == '{"servings":2}' and counts[1]['succeeded'] == 1


def test_count_index_reports_omitted_groups_without_truncating_arguments():
    from src.agent_loop import _completed_execution_counts
    events = [{'tool': 'read_file', 'command': 'x' * 2000, 'exit_code': 0},
              {'tool': 'read_file', 'command': 'small', 'exit_code': 0}]
    summary = _completed_execution_counts(events, limit=150)
    assert '(1 argument group(s) omitted)' in summary and 'small' in summary
    assert 'x' * 10 not in summary
