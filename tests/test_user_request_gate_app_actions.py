"""A read action of a multi-action app tool, asked for by name, needs no card.

Seen live on the 27B: «En Nightingale's Hoard … entrena un clasificador …
Luego dame su diagnóstico … y genera el informe PDF» stopped at «Allow this
task to continue?» on `data_model {"action": "evaluate", "model_id": 6}`:
the tool also trains, so it is not read-only as a whole, and the request
named no value of that call.
"""
import pytest

DATA_MODEL = {
    "name": "data_model",
    "description": "Train, evaluate, explain and report models (write for train).",
    "annotations": {"readOnlyHint": False, "destructiveHint": False},
}


class _Mcp:
    _tools = {"af27db0c": [DATA_MODEL]}


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(
        "src.connector_sidecar.get_connector_for_server",
        lambda server_id, redact=True: (
            {"id": "c-" + server_id, "preset_id": "nightingale", "name": "Nightingale's Hoard"}
            if server_id == "af27db0c" else None
        ),
    )
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: _Mcp())


TOOL = "mcp__af27db0c__data_model"
ASKED = ("En Nightingale's Hoard, con el dataset ventas_coach, entrena un clasificador. "
         "Luego dame su diagnóstico (exactitud y AUC por clase) y genera el informe PDF.")


def test_the_read_action_asked_for_passes():
    from src.user_request_gate import allows
    assert allows(TOOL, '{"action": "evaluate", "model_id": 6}', ASKED) is True
    assert allows(TOOL, '{"action": "report", "model_id": 6}', ASKED) is True


def test_without_the_app_named_or_the_act_asked_it_still_asks():
    from src.user_request_gate import allows
    assert allows(TOOL, '{"action": "evaluate", "model_id": 6}', "dame su diagnóstico") is False
    assert allows(TOOL, '{"action": "explain", "model_id": 6}', ASKED) is False


def test_a_write_action_is_not_let_through_by_its_word():
    from src.user_request_gate import allows
    # «entrena» is said, but training writes: it needs one of its own values.
    assert allows(TOOL, '{"action": "train", "dataset": "otras_ventas", "target": "x"}', ASKED) is False
