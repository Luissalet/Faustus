"""Final answers link a just-produced gallery image by the URL its tool returned."""
import asyncio
import json

import pytest

from src import agent_loop as al
from src.answer_checks import canonical_generated_image_links
from tests.test_autonomy_budget import _events

ID = 'fb667e0e-f586-4d59-a380-577b4e4698bf'
URL = f'/api/generated-image/{ID}.png'
EVENTS = [{'tool': 'edit_image', 'exit_code': 0, 'image_id': ID, 'image_url': URL}]


@pytest.mark.parametrize('written, expected', [
    (f'![](https://api.gallery.example.com/generated-image/{ID}.png)', f'![]({URL})'),
    (f'[la imagen](http://localhost:9999/files/{ID}.png "hat")', f'[la imagen]({URL} "hat")'),
    (f'![x](<sandbox:/mnt/data/{ID}.png>)', f'![x]({URL})'),
    (f'Aquí: https://cdn.invented.test/a/{ID}.png, listo.', f'Aquí: {URL}, listo.'),
    (f'![ok]({URL})', f'![ok]({URL})'),
])
def test_rewrites_only_links_to_the_produced_image(written, expected):
    assert canonical_generated_image_links(written, EVENTS) == expected


@pytest.mark.parametrize('written', [
    '![other](https://example.com/cat.png)',
    '[docs](https://prospero.local/api/assets/abc.png)',
    f'The gallery ID is `{ID}`.',
    'https://example.com/other-image.png',
])
def test_unrelated_links_and_plain_ids_are_untouched(written):
    assert canonical_generated_image_links(written, EVENTS) == written


@pytest.mark.parametrize('events', [
    [],
    [{'tool': 'edit_image', 'exit_code': 1, 'image_id': ID, 'image_url': URL}],
    [{'tool': 'edit_image', 'image_id': ID, 'image_url': 'https://remote.test/x.png'}],
    [{'tool': 'edit_image', 'image_id': 'other', 'image_url': URL}],
])
def test_no_trusted_production_means_no_rewrite(events):
    written = f'![](https://api.gallery.example.com/generated-image/{ID}.png)'
    assert canonical_generated_image_links(written, events) == written


def test_real_loop_replaces_invented_host_after_image_tool(monkeypatch):
    from tests.test_main_retry_pending_admission import configure
    configure(monkeypatch, ceiling=100000)
    calls, effects = [], []
    async def provider(*a, **k):
        calls.append(True)
        if len(calls) == 1:
            body = '```edit_image\n' + json.dumps({'action': 'instruction', 'image_id': 'src',
                                                   'prompt': 'add a hat'}) + '\n```'
        else:
            body = f'Done. ![](https://api.gallery.example.com/generated-image/{ID}.png)'
        yield 'data: ' + json.dumps({'delta': body}) + '\n\n'
        yield 'data: [DONE]\n\n'
    async def execute(block, *a, **k):
        effects.append(block.tool_type)
        return block.tool_type, {'output': f'Edited image {ID}', 'exit_code': 0,
                                 'image_id': ID, 'image_url': URL}
    monkeypatch.setattr(al, 'stream_llm_with_fallback', provider)
    monkeypatch.setattr(al, 'execute_tool_block', execute)
    async def run():
        return [c async for c in al.stream_agent_loop('https://fixture.invalid/v1', 'm',
            [{'role': 'user', 'content': 'Put a hat on my image'}], max_rounds=3,
            relevant_tools={'edit_image'}, owner='admin')]
    events = _events(asyncio.run(run()))
    assert effects == ['edit_image']
    assert any(e.get('type') == 'generated_image' and e.get('url') == URL for e in events)
    replaced = [e for e in events if e.get('type') == 'response_replace']
    assert replaced and replaced[-1]['text'] == f'Done. ![]({URL})'


def test_approved_event_without_image_id_still_identifies_its_image():
    # Seen live: the saved event of an approved call had the URL but no ID,
    # and «![](https://api.generated-image/<id>.png)» stayed broken.
    events = [{'tool': 'edit_image', 'approved': True, 'exit_code': None, 'image_url': URL}]
    written = f'Aquí está el resultado:\n\n![](https://api.generated-image/{ID}.png)'
    assert canonical_generated_image_links(written, events) == f'Aquí está el resultado:\n\n![]({URL})'
