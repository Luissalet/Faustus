"""PDF schemas and all three calling surfaces share one argument contract."""
import json

import pytest

from src import pdf_tree
from src.agent_tools import TOOL_HANDLERS
from src.code_mode.bridge import dispatch_call
from src.pdf_tool_contracts import function_schemas, parse_content
from src.tool_execution import execute_tool_block
from src.tool_capabilities import ToolRunSecurityContext
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block, validate_tool_arguments


async def call(route, name, args, tmp_path):
    if route == "direct":
        return await TOOL_HANDLERS[name](args, {})
    if route == "native":
        block = function_call_to_tool_block(name, json.dumps(args))
        _, result = await execute_tool_block(block, workspace=str(tmp_path), workspace_roots=[str(tmp_path)],
                                             security_context=ToolRunSecurityContext())
        return result
    return await dispatch_call(name, args, session_id=None, owner=None, workspace=str(tmp_path),
                               workspace_roots=[str(tmp_path)], disabled_tools=set(), call_id="pdf-contract")


INVALID = [
    ("pdf_outline", {"path": "unused.pdf", "max_depth": True}),
    ("pdf_outline", {"path": "unused.pdf", "max_depth": 0}),
    ("pdf_outline", {"path": 123}),
    ("pdf_outline", {"path": "   "}),
    ("pdf_outline", {"path": "unused.pdf", "typo": 1}),
    ("pdf_read_section", {"path": "unused.pdf", "node_id": "1", "max_chars": 0}),
    ("pdf_read_section", {"path": "unused.pdf", "node_id": "1", "max_chars": "20"}),
    ("pdf_find_section", {"path": "unused.pdf", "query": "x", "limit": True}),
    ("pdf_find_section", {"path": "unused.pdf", "query": "x", "limit": -2}),
    ("pdf_find_section", {"path": "unused.pdf", "query": "x", "limit": None}),
    ("pdf_find_section", {"path": "unused.pdf", "query": "x", "limit": 1.5}),
]


@pytest.mark.parametrize("route", ["native", "direct", "code_mode"])
@pytest.mark.parametrize("name,args", INVALID)
async def test_invalid_arguments_reject_before_pdf_io(route, name, args, monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid arguments reached PDF I/O")
    for function in ("build_tree", "read_section", "find_in_tree"):
        monkeypatch.setattr(pdf_tree, function, forbidden)
    assert validate_tool_arguments(name, args)
    result = await call(route, name, args, tmp_path)
    assert result["exit_code"] == 1 and result["error_class"] == "pdf_tree.error"


@pytest.fixture
def sample_pdf(tmp_path):
    from reportlab.pdfgen import canvas
    import pypdf
    source = tmp_path / "source.pdf"
    canvas_ = canvas.Canvas(str(source))
    canvas_.drawString(72, 700, "A traceable fixture fact.")
    canvas_.save()
    writer = pypdf.PdfWriter()
    writer.append(str(source))
    writer.add_outline_item("Fixture chapter", 0)
    path = tmp_path / "fixture.pdf"
    with path.open("wb") as stream:
        writer.write(stream)
    return str(path)


@pytest.mark.parametrize("route", ["native", "direct", "code_mode"])
async def test_real_fixture_success_through_every_surface(route, sample_pdf, tmp_path):
    outline = await call(route, "pdf_outline", {"path": sample_pdf}, tmp_path)
    assert outline["exit_code"] == 0 and outline["pages"] == 1
    matches = await call(route, "pdf_find_section", {"path": sample_pdf, "query": "Fixture"}, tmp_path)
    node = matches["matches"][0]["id"]
    section = await call(route, "pdf_read_section", {"path": sample_pdf, "node_id": node}, tmp_path)
    assert section["exit_code"] == 0
    assert "[page 1]" in section["output"] and "traceable fixture fact" in section["output"]


async def test_outline_bare_path_remains_supported(sample_pdf):
    result = await TOOL_HANDLERS["pdf_outline"](sample_pdf, {})
    assert result["exit_code"] == 0 and result["pages"] == 1


async def test_code_mode_still_obeys_disabled_tool_gate(sample_pdf, tmp_path):
    result = await dispatch_call("pdf_outline", {"path": sample_pdf}, session_id=None, owner=None,
                                workspace=str(tmp_path), workspace_roots=[str(tmp_path)],
                                disabled_tools={"pdf_outline"}, call_id="denied-pdf")
    assert result.get("error") and result["exit_code"] != 0


def test_schema_exports_match_contract_and_do_not_mutate_it():
    generated = function_schemas()
    runtime = {row["function"]["name"]: row for row in FUNCTION_TOOL_SCHEMAS}
    for row in generated:
        assert runtime[row["function"]["name"]] == row
    generated[0]["function"]["parameters"]["properties"]["max_depth"]["minimum"] = -100
    assert function_schemas()[0]["function"]["parameters"]["properties"]["max_depth"]["minimum"] == 1
    assert parse_content("pdf_find_section", {"path": "a.pdf", "query": "q"})["limit"] == 8
    assert parse_content("pdf_read_section", {"path": "a.pdf", "node_id": "1"})["max_chars"] == 20000


def test_integral_json_number_matches_integer_schema():
    args = {"path": "a.pdf", "query": "q", "limit": 2.0}
    assert validate_tool_arguments("pdf_find_section", args) == []
    parsed = parse_content("pdf_find_section", args)
    assert parsed["limit"] == 2 and type(parsed["limit"]) is int
