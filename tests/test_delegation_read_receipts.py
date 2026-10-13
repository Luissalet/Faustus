from types import SimpleNamespace

import pytest

from src import delegation_receipts as dr


@pytest.mark.parametrize('task', [
    'Lee evidence.txt y confirma su valor con read_file.',
    'Read settings.py and explain the defaults.',
    'Review src/example.py for correctness and report findings.',
    'Compara a.json con b.json y responde con las diferencias.',
])
def test_read_task_is_not_changed_into_a_write(task):
    assert not dr.looks_like_write_task(task)
    run = SimpleNamespace(name='reader', instruction=task, mutations=[], tool_calls=1,
                          failed_calls=0, text='The checked value is 42.', stop_reason='complete', error=None)
    assert dr.receipt_for(run).verdict == 'reported'


def test_write_task_still_needs_actual_mutation():
    run = SimpleNamespace(name='writer', instruction='Read src/a.py and implement the fix.',
                          mutations=[], tool_calls=1, text='The checked value is 42.', stop_reason='complete', error=None)
    assert dr.looks_like_write_task(run.instruction)
    assert dr.receipt_for(run).verdict == dr.VERDICT_EMPTY


@pytest.mark.parametrize('text', ['', 'Done.', 'Reference context received.'])
def test_read_task_ack_is_not_a_report(text):
    run = SimpleNamespace(name='reader', instruction='Read a.txt and explain it.', mutations=[],
                          tool_calls=1, text=text, stop_reason='complete', error=None)
    assert dr.receipt_for(run).verdict == dr.VERDICT_ACK_ONLY


def test_all_failed_reads_are_not_reported_as_verified_work():
    run = SimpleNamespace(name='reader', instruction='Read a.txt and explain it.', mutations=[],
                          tool_calls=2, failed_calls=2, text='I checked it.', stop_reason='complete', error=None)
    assert dr.receipt_for(run).verdict == dr.VERDICT_EMPTY


@pytest.mark.parametrize('task', ['Edit the title of slide 2 in Cicero.', 'Create the meeting in Calendar.'])
def test_successful_non_file_tool_work_does_not_need_write_file(task):
    run = SimpleNamespace(name='editor', instruction=task, mutations=[], tool_calls=1,
                          failed_calls=0, text='The requested tool completed.', stop_reason='complete', error=None)
    assert not dr.looks_like_write_task(task)
    assert dr.receipt_for(run).verdict == 'reported'
    run.tool_calls = 0
    assert dr.receipt_for(run).verdict == dr.VERDICT_EMPTY


@pytest.mark.parametrize('task', ['Implement pagination in the backend.', 'Fix the validation bug in the API.',
                                 'Implement authentication correctly.'])
def test_code_changes_without_named_path_still_need_mutations(task):
    run = SimpleNamespace(name='editor', instruction=task, files=['src/api.py'], mutations=[],
                          tool_calls=1, failed_calls=0, text='I recommend using this fix.', stop_reason='complete', error=None)
    assert dr.looks_like_write_task(task, run.files)
    assert dr.receipt_for(run).verdict == dr.VERDICT_EMPTY


def test_calendar_input_csv_is_not_an_output_file():
    task = 'Create a calendar event using dates.csv.'
    assert not dr.looks_like_write_task(task)
    assert dr.looks_like_write_task('Create events.ics for the calendar.')


def test_explicit_read_only_task_can_report_review_findings_without_edits():
    run = SimpleNamespace(name='reviewer', instruction='Review how to fix the API bug.', files=['src/api.py'],
                          read_only=True, mutations=[], tool_calls=1, failed_calls=0,
                          text='The validation should reject null.', stop_reason='complete', error=None)
    assert dr.receipt_for(run).verdict == 'reported'
