"""An image edit claimed without an image tool is rejected like a coding claim."""
import asyncio
import json

from src import agent_loop as al
from tests.test_autonomy_budget import _events
from tests.test_image_attachment_routing import CHROME

ID = 'b3201cd9-1904-4394-964d-f9aefbeb0867'
URL = f'/api/generated-image/{ID}.png'


def _run(monkeypatch, replies, user):
    from tests.test_main_retry_pending_admission import configure
    configure(monkeypatch, ceiling=100000)
    calls, effects = [], []
    async def provider(*a, **k):
        calls.append(True)
        body = replies[min(len(calls), len(replies)) - 1]
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
            [{'role': 'user', 'content': user}], max_rounds=4,
            relevant_tools={'edit_image', 'image_job'}, owner='admin')]
    return calls, effects, _events(asyncio.run(run()))


def test_claimed_edit_without_tool_gets_another_round_and_real_edit(monkeypatch):
    claim = 'Se ha añadido un bigote grande al hombre en la imagen original.'
    call = '```edit_image\n' + json.dumps({'action': 'instruction', 'image_id': 'src',
                                           'prompt': 'ponle un bigote'}) + '\n```'
    user = 'Edita la imagen adjunta: ponle un bigote grande al hombre.' + CHROME
    calls, effects, events = _run(monkeypatch, [claim, call, 'Listo.'], user)
    assert effects == ['edit_image'] and len(calls) == 3
    assert any(e.get('type') == 'harness_check' and 'image_claim_without_tool' in (e.get('reasons') or [])
               for e in events)
    assert any(e.get('type') == 'generated_image' and e.get('url') == URL for e in events)


def test_plain_question_about_an_image_is_not_held_to_a_claim(monkeypatch):
    # No gallery image, no edit request: an ordinary answer is left alone.
    calls, effects, events = _run(monkeypatch, ['Se ve un retrato con fondo gris.'],
                                  '¿Qué ves en esta imagen de ejemplo?')
    assert len(calls) == 1 and effects == []
    assert not any(e.get('type') == 'harness_check' and e.get('reasons') for e in events)


def test_claim_that_survives_the_retry_is_replaced_by_the_truth(monkeypatch):
    claim = 'Se ha añadido un bigote grande al hombre en la imagen original.'
    user = 'Edita la imagen adjunta: ponle un bigote grande al hombre.' + CHROME
    calls, effects, events = _run(monkeypatch, [claim], user)
    assert effects == []
    replaced = [e for e in events if e.get('type') == 'response_replace']
    assert replaced and 'No he editado ni generado ninguna imagen' in replaced[-1]['text']


import pytest
from src.agent_harness import find_image_result_claims


@pytest.mark.parametrize('text', [
    'Se ha añadido un bigote grande al hombre.',
    'He aplicado un estilo anime a tu foto.',
    'Aquí tienes la nueva imagen con el sombrero.',
    "I've added a hat to the man.",
    'Here is the edited image.',
])
def test_image_result_claims_found(text):
    assert find_image_result_claims(text)


@pytest.mark.parametrize('text', [
    'No he podido editar la imagen porque el estudio no responde.',
    'Si quieres, he preparado una lista de estilos posibles.',
    'I could not edit the image.',
    '¿Qué estilo quieres que aplique?',
    'La imagen muestra a un hombre con camiseta gris.',
])
def test_non_claims_ignored(text):
    assert not find_image_result_claims(text)
