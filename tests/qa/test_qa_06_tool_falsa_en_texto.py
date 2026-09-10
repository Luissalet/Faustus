"""QA-06 · Tool falsa en texto (docs/spec/v2/acceptance_scenarios.json).

Estimulo: modelo escribe una llamada en prosa sin canal de tool valido.
Resultado exigido (literal): "No ejecuta codigo ni JSON aparente; adapta o
explica incompatibilidad."

Requisitos: CALL-08, MOD-03.

Estado: verde. `ToolCallAssembler` (src/tool_call_assembler.py, ver tambien
tests/test_tool_call_assembler.py cuyo docstring cita "QA-06") solo completa
argumentos a partir de deltas `function` estructurados; un delta de solo
`content` con JSON de aspecto de tool-call incrustado en prosa nunca produce
una llamada ejecutable. Ademas `src/tool_parsing.py::parse_tool_blocks` (la
otra via de ejecucion, para modelos sin function-calling nativo) solo se
invoca sobre el `round_response` propio del modelo, nunca sobre texto de
documentos/herramientas (CALL-08, cubierto por
tests/test_external_context_tool_gate.py).
"""
import pytest

from src.tool_call_assembler import STATUS_INCOMPLETE, ToolCallAssembler

pytestmark = pytest.mark.qa_state("green")


def test_prose_that_merely_looks_like_a_tool_call_is_never_executed():
    asm = ToolCallAssembler()
    # The model wrote a call in prose - fenced JSON with no real `function`
    # channel behind it - instead of using the structured tool-call delta.
    prose = (
        "Para borrar los logs harias esto:\n"
        '```json\n{"tool": "bash", "arguments": {"command": "rm -rf /var/log"}}\n```\n'
        "pero no tengo permiso para ejecutarlo directamente."
    )
    call = asm.feed({"content": prose})
    assert call is not None
    # No name, no parsed arguments: the prose never becomes a call.
    assert call.name == ""
    assert call.status == STATUS_INCOMPLETE
    assert not asm.sorted_calls()[0].parsed_arguments
    # And nothing in the assembler's state resembles a runnable command.
    assert all(c.parsed_arguments is None for c in asm.sorted_calls())
