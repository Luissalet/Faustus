"""QA-03 · Diseno ambiguo (docs/spec/v2/acceptance_scenarios.json).

Estimulo: pedir sistema nuevo con varias arquitecturas razonables.
Resultado exigido (literal): "Pregunta con opciones/descripcion/libre antes
de escribir; la respuesta queda vinculada a la decision."

Requisitos: CALL-07, PLAN-01.

Estado: verde. `AskUserTool` (src/agent_tools/interaction_tools.py) devuelve
un payload `ask_user` con `question_id` estable, opciones {label,
description, id} y `allow_free_text` (la UI ofrece una linea de texto libre
junto a los botones) - eso es "opciones/descripcion/libre" en un unico
objeto direccionable. `question_store.resolve_question` (ver
tests/qa/test_qa_14_respuesta_vieja.py) exige ese mismo `question_id` para
resolver, que es como la respuesta "queda vinculada a la decision" en vez de
a "la ultima pregunta". Y `tests/test_agent_asks_before_a_new_system.py`
prueba, contra el prompt real, que el agente efectivamente pregunta antes de
levantar un sistema nuevo cuando hay varias arquitecturas razonables (el
incidente del que nace ese test es justamente ese).
"""
import json

import pytest

from src.agent_tools.interaction_tools import AskUserTool

pytestmark = pytest.mark.qa_state("green")


@pytest.mark.asyncio
async def test_ask_user_offers_options_description_and_free_text_bound_to_an_id():
    desc, result = await AskUserTool().execute(
        json.dumps({
            "question": "Como estructuramos el nuevo sistema de colas?",
            "options": [
                {"label": "Redis Streams", "description": "Ligero, ya usado en el repo"},
                {"label": "Postgres LISTEN/NOTIFY", "description": "Sin nueva dependencia"},
            ],
        }),
        {},
    )
    payload = result["ask_user"]
    # Options carry label + description, each with a stable id - the
    # "opciones/descripcion" half of the requirement.
    assert len(payload["options"]) == 2
    for opt in payload["options"]:
        assert opt["label"] and "description" in opt and opt["id"]
    # A free-text line is offered alongside the buttons - the "libre" half.
    assert payload["allow_free_text"] is True
    # The whole question carries one stable id: this is what a later answer
    # binds to, instead of "whichever question is most recent".
    assert payload["question_id"].startswith("qst_")
    assert result["exit_code"] == 0
