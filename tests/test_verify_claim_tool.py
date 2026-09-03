"""`verify_claim` — the deterministic ladder, wired as a tool.

`src/claim_verify.py` shipped with its own suite and no wiring at all, so the
one question it answers — *the model just asserted X from this document; is X
actually in the document?* — could not be asked from a turn.

What this pins is the wiring and the two honesty rules the module refuses to
bend, now that a model can reach them:

* **layer 5 is not available here.** The model-judgement rung needs a judge
  model, and the point of the ladder is that it is deterministic; the tool says
  so rather than letting a model wait for a verdict that is not coming. An
  unsettled claim comes back `layer: null` — "not shown", never "false".
* **nothing is fetched.** The source is the text the caller already has; `url`
  is recorded so the verdict can be cited and is never dereferenced.
"""
from __future__ import annotations

import json

import pytest

from src.agent_tools import ToolBlock

SOURCE = (
    "El informe de 2024 registra 3.4 millones de usuarios activos en Europa. "
    "Naomi Brenner firma el capítulo sobre retención, y la caída del cuarto "
    "trimestre se atribuye al cambio de precios."
)


async def _call(args):
    import src.tool_execution as te
    return await te._execute_tool_block_impl(
        ToolBlock(tool_type="verify_claim", content=json.dumps(args)),
        session_id="s1", owner="luis")


# ── the wiring ──────────────────────────────────────────────────────────────

def test_the_tool_is_wired_everywhere_a_tool_must_be():
    from src.agent_tools import TOOL_TAGS
    from src.agent_loop import TOOL_SECTIONS
    from src.tool_capabilities import KNOWN_CAPABILITY_TOOLS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block

    assert "verify_claim" in TOOL_TAGS
    assert "verify_claim" in KNOWN_CAPABILITY_TOOLS
    assert "verify_claim" in TOOL_SECTIONS
    assert len(BUILTIN_TOOL_DESCRIPTIONS["verify_claim"]) > 40
    schema = next(s["function"] for s in FUNCTION_TOOL_SCHEMAS
                  if s["function"]["name"] == "verify_claim")
    assert schema["parameters"]["required"] == ["claim", "source"]
    assert set(schema["parameters"]["properties"]) == {"claim", "source", "url"}
    block = function_call_to_tool_block("verify_claim", {"claim": "x", "source": "y"})
    assert block.tool_type == "verify_claim"
    assert json.loads(block.content) == {"claim": "x", "source": "y"}


def test_the_description_says_layer_five_is_not_on_offer():
    """A model that expects a judge to settle what the ladder could not will
    read `layer: null` as a failure of the tool. Both model-facing texts say
    the rung is absent and why."""
    from src.agent_loop import TOOL_SECTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    schema = next(s["function"] for s in FUNCTION_TOOL_SCHEMAS
                  if s["function"]["name"] == "verify_claim")
    assert "NO LAYER 5" in schema["description"]
    assert "'not shown', not 'false'" in schema["description"]
    assert "no model-judgement layer here" in TOOL_SECTIONS["verify_claim"]
    assert "not the same as false" in TOOL_SECTIONS["verify_claim"]


def test_the_tool_declares_no_effect_beyond_reading_its_own_arguments():
    """It opens no socket and writes nothing — but its result quotes a page or
    a document, so what comes back is external and untrusted."""
    from src.tool_capabilities import ResultIntegrity, ToolEffect, TOOL_CAPABILITIES
    caps = TOOL_CAPABILITIES["verify_claim"]
    assert caps.effects == frozenset({ToolEffect.READ_PUBLIC})
    assert caps.result_integrity is ResultIntegrity.EXTERNAL_UNTRUSTED


# ── the ladder, through the tool ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_verbatim_claim_is_settled_by_layer_one():
    desc, result = await _call({"claim": "3.4 millones de usuarios activos en Europa",
                                "source": SOURCE})
    assert desc == "verify_claim: layer 1"
    assert result["supported"] is True and result["layer"] == 1
    assert result["confidence"] == 1.0 and result["label"] == "deterministic"
    assert result["unsupported_terms"] == []
    assert result["source_chars"] == len(SOURCE)
    assert "note" not in result, "the ladder settled it; nothing to explain"


@pytest.mark.asyncio
async def test_an_invented_figure_is_refuted_by_layer_four_and_named():
    """The layer the whole module exists for: the claim borrows the source's
    wording and changes the number."""
    desc, result = await _call({"claim": "El informe de 2024 registra 8.9 millones "
                                         "de usuarios activos en Europa",
                                "source": SOURCE})
    assert desc == "verify_claim: layer 4"
    assert result["supported"] is False and result["layer"] == 4
    assert "8.9" in result["unsupported_terms"]
    assert "no contiene" in result["why"] or "does not contain" in result["why"]


@pytest.mark.asyncio
async def test_an_invented_name_is_refuted_too():
    _, result = await _call({"claim": "Judith Farrow firma el capítulo sobre retención",
                             "source": SOURCE})
    assert result["supported"] is False and result["layer"] == 4
    assert "judith" in result["unsupported_terms"] or "farrow" in result["unsupported_terms"]


@pytest.mark.asyncio
async def test_an_unsettled_claim_is_not_shown_not_false_and_says_where_layer_five_went():
    """Every figure and name checks out and no layer can settle the sentence
    built from them. That is `layer: null` — and the answer explains that the
    rung which would have judged it is deliberately absent."""
    desc, result = await _call({"claim": "Naomi Brenner recomienda congelar precios "
                                         "durante todo el ejercicio siguiente",
                                "source": SOURCE})
    assert result["layer"] is None and result["supported"] is False
    assert result["confidence"] == 0.0
    assert "layer 5 (model judgement) is not available from this tool" in result["note"]
    assert "not the same as false" in result["note"]
    assert desc == "verify_claim: layer None"


@pytest.mark.asyncio
async def test_the_url_is_recorded_and_never_fetched(monkeypatch):
    import urllib.request

    def explode(*a, **k):  # pragma: no cover - it must never be called
        raise AssertionError("verify_claim opened the network")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    _, result = await _call({"claim": "3.4 millones de usuarios activos en Europa",
                             "source": SOURCE, "url": "https://example.com/informe-2024"})
    assert result["source_url"] == "https://example.com/informe-2024"
    assert result["supported"] is True


# ── the calls that are not calls ────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("args,expected", [
    ({}, "'claim' is required"),
    ({"source": SOURCE}, "'claim' is required"),
    ({"claim": "   "}, "'claim' is required"),
    ({"claim": "3.4 millones"}, "'source' is required"),
    ({"claim": "3.4 millones", "source": "  "}, "'source' is required"),
])
async def test_a_call_with_nothing_to_check_says_which_half_is_missing(args, expected):
    desc, result = await _call(args)
    assert desc == "verify_claim" and result["exit_code"] == 1
    assert expected in result["error"]
    assert "supported" not in result, "a malformed call is not a verdict"


@pytest.mark.asyncio
async def test_junk_arguments_are_a_readable_error_not_a_crash():
    import src.tool_execution as te
    for content in ("", "not json at all", "[1, 2, 3]", '"a string"', "null", "{"):
        desc, result = await te._execute_tool_block_impl(
            ToolBlock(tool_type="verify_claim", content=content),
            session_id="s1", owner="luis")
        assert desc == "verify_claim" and result["exit_code"] == 1
        assert isinstance(result["error"], str) and result["error"]


@pytest.mark.asyncio
async def test_a_claim_and_a_source_that_are_not_strings_still_answer():
    """The module is total; the tool must not become the place that raises."""
    _, result = await _call({"claim": 2024, "source": SOURCE})
    assert result["supported"] is True and result["layer"] == 1
    _, result = await _call({"claim": "2024", "source": ["not", "a", "string"]})
    assert result["supported"] in (True, False) and "why" in result
