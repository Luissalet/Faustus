# -*- coding: utf-8 -*-
"""OBJ-30: the canvas pass reachable as an agent tool.

The pass already existed and nothing used it, because only code could call
it. A tool that is registered in four of the five places the rest of this
codebase requires is a tool the router will never offer, so the parity check
is half of this file.
"""

import pytest

from src import design_canvas
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
from src.agent_tools.design_canvas_tools import DesignCanvasTool
from src.tool_capabilities import ToolEffect, capabilities_for_action
from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
from src.tool_index_examples import EXAMPLES
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

NAME = "design_canvas"


def test_registered_in_all_five_places():
    assert NAME in TOOL_HANDLERS
    assert NAME in TOOL_TAGS
    assert any(t.get("function", {}).get("name") == NAME for t in FUNCTION_TOOL_SCHEMAS)
    assert BUILTIN_TOOL_DESCRIPTIONS.get(NAME)
    assert len(EXAMPLES.get(NAME, [])) >= 2


def test_it_is_a_write_tool():
    """It stores a design the next session reads as settled."""
    caps = capabilities_for_action(NAME, "{}")
    assert caps.known
    assert ToolEffect.WRITE_PRIVATE in caps.effects


def test_the_schema_requires_a_goal():
    schema = next(t for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] == NAME)
    params = schema["function"]["parameters"]
    assert params["required"] == ["goal"]
    assert set(params["properties"]) >= {"goal", "context", "name", "save"}


@pytest.mark.asyncio
async def test_a_missing_goal_is_refused_without_a_model_call():
    result = await DesignCanvasTool().execute("{}", {})
    assert result["exit_code"] == 1
    assert "goal" in result["error"]


CANVAS = {
    "requirements": ["median must be correct for even lists", "no new dependency"],
    "entities": ["stats.median"],
    "approach": {"chosen": "average the two middle values",
                 "rejected": "special-case len 2", "why": "does not generalise"},
    "structure": ["stats.py", "test_stats.py"],
    "operations": ["run pytest"],
    "norms": ["keep the existing signature"],
    "safeguards": ["an empty list still raises", "odd-length behaviour unchanged"],
}


@pytest.mark.asyncio
async def test_without_a_project_the_canvas_still_comes_back(monkeypatch):
    """Filing needs a project; drafting does not. Losing the work because
    nothing was bound to the chat would be the wrong trade."""
    async def _draft(goal, **kwargs):
        return {"canvas": CANVAS, "markdown": design_canvas.render(CANVAS),
                "paths": design_canvas.referenced_paths(CANVAS), "elapsed_ms": 1}

    import src.design_canvas_pass as dcp
    monkeypatch.setattr(dcp, "draft", _draft)

    result = await DesignCanvasTool().execute('{"goal": "fix median"}', {})
    assert result["exit_code"] == 0
    assert result["saved"] is False
    assert "no project or workspace" in result["note"]
    assert "median" in result["output"]


@pytest.mark.asyncio
async def test_save_false_skips_the_graph(monkeypatch):
    async def _draft(goal, **kwargs):
        return {"canvas": CANVAS, "markdown": "md", "paths": [], "elapsed_ms": 1}

    def _boom(*args, **kwargs):
        raise AssertionError("save=false must not touch the graph")

    import src.design_canvas_pass as dcp
    monkeypatch.setattr(dcp, "draft", _draft)
    monkeypatch.setattr(dcp, "file_as_concept", _boom)

    result = await DesignCanvasTool().execute(
        '{"goal": "fix median", "save": false}', {"workspace": "D:/somewhere"})
    assert result["saved"] is False


@pytest.mark.asyncio
async def test_a_failed_canvas_does_not_fail_open(monkeypatch):
    from src.design_canvas import DesignCanvasError

    async def _draft(goal, **kwargs):
        raise DesignCanvasError("the model returned nothing usable")

    import src.design_canvas_pass as dcp
    monkeypatch.setattr(dcp, "draft", _draft)

    result = await DesignCanvasTool().execute('{"goal": "fix median"}', {})
    assert result["exit_code"] == 1
    assert result["error_class"] == "design_canvas.no_canvas"


@pytest.mark.asyncio
async def test_bare_prose_is_taken_as_the_goal(monkeypatch):
    """Models do send a bare string where an object was asked for."""
    seen = {}

    async def _draft(goal, **kwargs):
        seen["goal"] = goal
        return {"canvas": CANVAS, "markdown": "md", "paths": [], "elapsed_ms": 1}

    import src.design_canvas_pass as dcp
    monkeypatch.setattr(dcp, "draft", _draft)

    await DesignCanvasTool().execute("design the median fix", {})
    assert seen["goal"] == "design the median fix"
