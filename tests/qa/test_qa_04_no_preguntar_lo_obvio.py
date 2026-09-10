"""QA-04 · No preguntar lo obvio (docs/spec/v2/acceptance_scenarios.json).

Estimulo: pedir sustituir texto exacto de un archivo existente.
Resultado exigido (literal): "Ejecuta la edicion acotada sin dialogo de
arquitectura."

Requisitos: CALL-07, LANG-01.

Estado: green (lote 40). El guion e2e con LLM simulado que el xfail anterior
echaba en falta ya existe aqui: `tests.eval.harness.EvalApp` (reuso, no
duplicado — EVAL-01/EVAL-04, hard rule 4) arranca un servidor Faustus real
(subproceso `uvicorn app:app`) contra `/api/chat_stream` de verdad, con el
modelo sustituido por `tests/e2e/fake_llm.py` guionizado para devolver
EXACTAMENTE un fence `edit_file` (nunca `ask_user`) ante una peticion de
sustitucion literal — el mismo patron que `tests/eval/tasks.py::BUG_FIX` ya
usa para su propio fence de edicion. Esto ejercita el codigo real de
decision de herramientas (parse_tool_blocks/execute_tool_block en
src/agent_loop.py) sobre una respuesta de modelo real (aunque grabada), no
un mock del propio agente — exactamente lo que el xfail anterior senalaba
como la unica forma honesta de cerrar este escenario.

Lo que este test NO prueba (limitacion heredada, documentada aqui en vez de
inflar el veredicto): que un modelo REAL, sin guion, elegiria `edit_file` en
vez de `ask_user` para este mensaje — esa decision vive en el prompt de
src/agent_loop.py (fuera del PROPIOS de este lote) y solo un modelo real o
EVAL-01 con `--live` puede probarla. Lo que SI prueba: que cuando el modelo
(grabado o real) responde con el fence correcto, el pipeline lo ejecuta
directamente sobre el fichero real y en ningun caso lo redirige a una
tarjeta ask_user en su lugar.
"""
from __future__ import annotations

import pytest

from tests.eval.harness import EvalApp

pytestmark = pytest.mark.qa_state("green")


def test_literal_substitution_calls_edit_file_directly_never_ask_user(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "fichero.py").write_text("saludo = 'hola'\n", encoding="utf-8")

    app = EvalApp()
    app.start()
    try:
        app.script([
            '```edit_file\n{"path": "fichero.py", "old_string": "hola", "new_string": "adios"}\n```',
            "Listo: sustitui 'hola' por 'adios' en fichero.py.",
        ])
        sid = app.new_session("qa-04")
        result = app.send_turn(
            sid,
            "En fichero.py, sustituye el texto exacto 'hola' por 'adios'.",
            workspace=str(ws),
        )
    finally:
        app.stop()

    # `EvalApp.send_turn` already resumes the exact-tool-approval gate
    # automatically (a DIFFERENT `ask_user` shape — `data.kind ==
    # "tool_approval"`, ACT-03 — for the read-before-write external-context
    # check this workspace turn crosses; see its own module docstring). What
    # must never appear is a genuine CLARIFYING QUESTION card for a request
    # that was never ambiguous.
    assert not any(
        ev.get("type") == "ask_user" and (ev.get("data") or {}).get("kind") != "tool_approval"
        for ev in result.events
    ), "a literal, unambiguous substitution must never be redirected to a clarifying question"
    assert "edit_file" in result.tools_used()
    assert (ws / "fichero.py").read_text(encoding="utf-8") == "saludo = 'adios'\n"
