"""A workspace coding request keeps the code-intelligence tools retrieval found.

23-09-2026, live on 7006 with the local 27B and Nightingale's Hoard bound:
«Si cambio cómo se interpretan los números con formato español (1.150,00) al
ingerir un CSV, ¿qué flujos de ejecución y qué tests se ven afectados? Dame
el comando de pytest exacto». The index returned code_graph_impact,
code_graph_flows and friends, the request read as a coding request, and the
Terminus replacement swapped the whole selection for the file/shell set. The
model then grepped for five minutes for what one impact call answers.
"""
import unittest.mock as mock

import pytest

import src.tool_index as tool_index
import src.agent_loop as agent_loop
from tests.test_agent_loop_workspace_tool_floor import tools_sent, workspace  # noqa: F401

REQUEST = (
    "Si cambio cómo se interpretan los números con formato español (1.150,00) "
    "al ingerir un CSV, ¿qué flujos de ejecución y qué tests se ven afectados? "
    "Dame el comando de pytest exacto para comprobarlo."
)


class _Index:
    def __init__(self, picks):
        self.picks = set(picks)

    def index_mcp_tools(self, *a, **k):
        return None

    def get_tools_for_query(self, query, k=8, **kwargs):
        return set(tool_index.ALWAYS_AVAILABLE) | self.picks


def _sent(message, ws, picks):
    with mock.patch.object(tool_index, "get_tool_index", lambda: _Index(picks)):
        return set(tools_sent(message, ws))


def test_the_request_is_a_workspace_coding_request():
    assert agent_loop._looks_like_workspace_coding_request(REQUEST)


def test_a_structure_question_gets_the_code_graph_family(workspace):  # noqa: F811
    names = _sent(REQUEST, workspace, {"web_search", "whatsapp_react"})
    assert {"code_graph_impact", "code_graph_flows", "tests_for"} <= names
    assert {"read_file", "grep", "bash"} <= names
    assert "whatsapp_react" not in names, "retriever noise is still dropped"


def test_the_family_does_not_depend_on_language(workspace):  # noqa: F811
    en = ("If I change how Spanish-formatted numbers (1.150,00) are parsed when a CSV "
          "is ingested, which execution flows and which tests are affected? Give me "
          "the exact pytest command.")
    es = _sent(REQUEST, workspace, set())
    assert es == _sent(en, workspace, set())


def test_an_ordinary_coding_request_gets_no_code_graph(workspace):  # noqa: F811
    names = _sent("arregla el bug del carrito en cart.py", workspace, {"code_graph_impact"})
    assert not {n for n in names if n.startswith("code_graph_")}


@pytest.mark.parametrize("text", [
    "¿qué se rompe si toco engine.py?",
    "what breaks if I change the parser?",
    "¿de qué partes se compone este repo?",
    "which tests are affected by editing services.py",
])
def test_intent(text):
    assert agent_loop._code_intel_family_for(text)


@pytest.mark.parametrize("text", ["fix the cart bug in cart.py", "refactoriza tests/test_cart.py",
                                  "lee cart.py y explicame que hace"])
def test_no_intent(text):
    assert not agent_loop._code_intel_family_for(text)
