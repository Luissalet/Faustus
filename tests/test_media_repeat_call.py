"""A later round repeating a finished image request gets that image back."""
import asyncio
import json

import pytest

from src import agent_loop as al
from tests.test_autonomy_budget import _events

ID = 'f14e5735-da83-4d6d-b541-abeff9f0dacc'
URL = f'/api/generated-image/{ID}.png'
ARGS = {'image_id': 'src', 'action': 'instruction', 'prompt': 'ponle un sombrero', 'scale': 1}


def _event(**over):
    body = json.dumps(ARGS)
    event = {'tool': 'edit_image', 'round': 1, 'command': body, 'exit_code': None,
             'image_url': URL, 'image_id': ID,
             'media_job_key': al._media_job_key('edit_image', body)}
    event.update(over)
    return event


def test_repeat_from_earlier_round_reuses_result():
    result = al._repeated_media_result('edit_image', json.dumps(ARGS), [_event()], 2)
    assert result['image_url'] == URL and result['image_id'] == ID and result['exit_code'] == 0
    assert result['reused_result'] and 'not run again' in result['output']


@pytest.mark.parametrize('tool, content, events, round_num', [
    ('edit_image', json.dumps(ARGS), [_event()], 1),                       # same round: a batch
    ('edit_image', json.dumps({**ARGS, 'prompt': 'otro'}), [_event()], 2),  # different request
    ('edit_image', json.dumps(ARGS), [_event(exit_code=1)], 2),             # failed earlier
    ('edit_image', json.dumps(ARGS), [_event(image_url='')], 2),            # produced nothing
    ('generate_image', json.dumps(ARGS), [_event()], 2),                    # another tool
    ('read_file', json.dumps(ARGS), [_event(tool='read_file')], 2),         # not a media job
    ('edit_image', 'not json', [_event()], 2),
])
def test_everything_else_runs_normally(tool, content, events, round_num):
    assert al._repeated_media_result(tool, content, events, round_num) is None


def test_truncated_display_without_signature_is_never_matched():
    long_args = {**ARGS, 'prompt': 'x' * 400}
    body = json.dumps(long_args)
    event = _event(command=body[:240], media_job_key=None)
    assert al._repeated_media_result('edit_image', body, [event], 2) is None
    full = _event(command=body, media_job_key=al._media_job_key('edit_image', body))
    assert al._repeated_media_result('edit_image', body, [full], 2)['image_url'] == URL


def test_real_loop_runs_one_render_for_a_repeated_request(monkeypatch):
    from tests.test_main_retry_pending_admission import configure
    configure(monkeypatch, ceiling=100000)
    calls, effects = [], []
    call = '```edit_image\n' + json.dumps(ARGS) + '\n```'
    async def provider(*a, **k):
        calls.append(True)
        body = call if len(calls) <= 2 else 'Listo, aquí tienes el sombrero.'
        yield 'data: ' + json.dumps({'delta': body}) + '\n\n'
        yield 'data: [DONE]\n\n'
    async def execute(block, *a, **k):
        effects.append(block.tool_type)
        return block.tool_type, {'output': f'Generated image\nGallery image ID: {ID}',
                                 'image_id': ID, 'image_url': URL}
    monkeypatch.setattr(al, 'stream_llm_with_fallback', provider)
    monkeypatch.setattr(al, 'execute_tool_block', execute)
    async def run():
        return [c async for c in al.stream_agent_loop('https://fixture.invalid/v1', 'm',
            [{'role': 'user', 'content': 'Ponle un sombrero'}], max_rounds=4,
            relevant_tools={'edit_image'}, owner='admin')]
    events = _events(asyncio.run(run()))
    assert len(calls) == 3 and effects == ['edit_image']
    outputs = [e for e in events if e.get('type') == 'tool_output' and e.get('tool') == 'edit_image']
    assert len(outputs) == 2 and 'not run again' in json.dumps(outputs[1])
    assert {e.get('url') for e in events if e.get('type') == 'generated_image'} == {URL}


def test_live_repeat_with_stray_fields_is_the_same_instruction_job():
    # Seen live: the repeat added the previous request ID as mask_id and
    # dropped scale; an instruction edit reads neither.
    repeat = {'image_id': 'src', 'action': 'instruction', 'prompt': ' ponle  un sombrero ',
              'mask_id': 'tool-2ceb004f'}
    assert al._repeated_media_result('edit_image', json.dumps(repeat), [_event()], 2)['image_url'] == URL


def test_inpaint_mask_and_strength_define_the_job():
    base = {'image_id': 'src', 'action': 'inpaint', 'prompt': 'hat', 'mask_id': 'm1', 'strength': 0.8}
    event = _event(command=json.dumps(base), media_job_key=al._media_job_key('edit_image', json.dumps(base)))
    assert al._repeated_media_result('edit_image', json.dumps(base), [event], 2)
    for change in ({'mask_id': 'm2'}, {'strength': 0.5}):
        assert al._repeated_media_result('edit_image', json.dumps({**base, **change}), [event], 2) is None
