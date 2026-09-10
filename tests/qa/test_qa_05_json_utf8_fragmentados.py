"""QA-05 · JSON y UTF-8 fragmentados (docs/spec/v2/acceptance_scenarios.json).

Estimulo: dividir stream dentro de un caracter y dentro de arguments;
intercalar dos call IDs.
Resultado exigido (literal): "Reconstruye llamadas correctas, no ejecuta
fragmentos y conserva IDs."

Requisitos: CALL-01.

Estado: verde. `src/tool_call_assembler.py::ToolCallAssembler` ya cubre este
escenario literalmente (ver tests/test_tool_call_assembler.py, cuyo propio
docstring cita "QA-05, QA-06"). Este test repite el caso con un stream real:
dos IDs de llamada intercalados por indice, con un corte a mitad de un
caracter multibyte UTF-8, verificando que cada llamada se reconstruye
completa e independiente y que un fragmento incompleto nunca se reporta como
ejecutable.
"""
import pytest

from src.tool_call_assembler import STATUS_COMPLETE, STATUS_INCOMPLETE, ToolCallAssembler


pytestmark = pytest.mark.qa_state("green")


def _delta(index, id=None, name=None, arguments=None):
    d = {"index": index, "function": {}}
    if id is not None:
        d["id"] = id
    if name is not None:
        d["function"]["name"] = name
    if arguments is not None:
        d["function"]["arguments"] = arguments
    return d


def test_two_interleaved_calls_with_a_multibyte_split_reconstruct_cleanly():
    # "café" -> 'é' is the 2-byte b'\xc3\xa9'; the stream cuts right between
    # those two bytes on call #0 while call #1's fragments are interleaved.
    text0 = '{"q": "café"}'
    raw0 = text0.encode("utf-8")
    cut = raw0.index("é".encode("utf-8")) + 1

    asm = ToolCallAssembler()
    r1 = asm.feed(_delta(0, id="call_A", name="search", arguments=raw0[:cut]))
    r2 = asm.feed(_delta(1, id="call_B", name="read_file", arguments='{"path":"a'))
    # Neither call is complete yet - an assembler that "executed fragments"
    # would be the bug this scenario exists to catch.
    assert r1.status == STATUS_INCOMPLETE
    assert r2.status == STATUS_INCOMPLETE

    r1b = asm.feed(_delta(0, arguments=raw0[cut:]))
    r2b = asm.feed(_delta(1, arguments='.py"}'))

    assert r1b.status == STATUS_COMPLETE and r1b.id == "call_A"
    assert r1b.parsed_arguments == {"q": "café"}
    assert r2b.status == STATUS_COMPLETE and r2b.id == "call_B"
    assert r2b.parsed_arguments == {"path": "a.py"}
