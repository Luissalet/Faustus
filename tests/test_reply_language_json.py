import json

import pytest

from src.reply_language import reply_language_mismatch

PAYLOAD = {'servings': 4, 'stock_sufficient': False, 'rice_to_buy_g': 100, 'milk_to_buy_ml': 100}


@pytest.mark.parametrize('text', [json.dumps(PAYLOAD), json.dumps([PAYLOAD]),
                                '```json\n'+json.dumps(PAYLOAD)+'\n```',
                                '~~~json\n'+json.dumps(PAYLOAD)+'\n~~~',
                                '```\n'+json.dumps(PAYLOAD)+'\n```'])
def test_json_schema_keys_do_not_trigger_a_narration_rewrite(text):
    assert reply_language_mismatch('es', text) is None


def test_wrong_language_prose_around_json_is_still_detected():
    text='I have checked the recipe and the amounts you need for the meal.\n\n```json\n'+json.dumps(PAYLOAD)+'\n```'
    assert reply_language_mismatch('es', text) == 'en'
    assert reply_language_mismatch('es', 'He comprobado las cantidades de la receta.\n\n```json\n'+json.dumps(PAYLOAD)+'\n```') is None


def test_a_json_string_or_invalid_structure_does_not_hide_wrong_language_narration():
    prose='I have checked the recipe and the amounts you need for the meal.'
    assert reply_language_mismatch('es', json.dumps(prose)) == 'en'
    assert reply_language_mismatch('es', '```json\n{'+prose+'}\n```') == 'en'


@pytest.mark.parametrize('constant', ['NaN', 'Infinity', '-Infinity'])
def test_non_json_numeric_constants_do_not_make_an_invalid_payload_exempt(constant):
    text='{"message":"I have checked the recipe and the amounts you need for the meal.","value":'+constant+'}'
    assert reply_language_mismatch('es', text) == 'en'


def test_agent_loop_keeps_the_requested_json_without_an_extra_language_round(tmp_path, monkeypatch):
    import src.agent_loop as al
    from tests.test_research_streak import _collect, _events, _patch_common
    _patch_common(monkeypatch)
    answer='```json\n'+json.dumps(PAYLOAD)+'\n```'
    calls=[]
    async def fake_stream(_candidates, messages, **kwargs):
        calls.append(messages)
        yield 'data: '+json.dumps({'delta': answer})+'\n\n'
        yield 'data: '+json.dumps({'type':'finish','finish_reason':'stop'})+'\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', fake_stream)
    events=_events(_collect(al.stream_agent_loop('http://127.0.0.1:11434/v1','qwen3-coder:30b',
        [{'role':'user','content':'Devuelve solo un JSON con las cantidades de arroz y leche de esta receta para cuatro personas.'}],
        max_rounds=3, relevant_tools={'lookup_tools'}, workspace=str(tmp_path))))
    assert len(calls)==1
    assert not any(e.get('status')=='language_mismatch' for e in events)
    assert ''.join(e.get('delta','') for e in events if not e.get('type') and not e.get('thinking'))==answer
