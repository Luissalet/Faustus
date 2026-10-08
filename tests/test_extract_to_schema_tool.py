"""OBJ-24: the `extract_to_schema` agent tool and the /api/extract routes.

The tool is registered with the tool authority (family `documents`), the
handler table, the capability table (read-only, workspace-untrusted result),
plan mode and the sub-agent read rules. The REST routes are owner-scoped and
refuse schema names that could leave the owner's folder. The model is a
scripted fake throughout.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import src.agent_tools  # noqa: F401  (import order: handlers before schemas)
from src import schema_extraction as se
from src.workflows.model_calls import ModelCalls, ModelUnavailable

INVOICE = {"type": "object", "required": ["invoice_number", "total"],
           "properties": {"invoice_number": {"type": "string"}, "total": {"type": "number"},
                          "issue_date": {"type": "string", "format": "date"}}}
TEXT = "FACTURA N.º F-2026-0042\nFecha: 15/03/2026\nTotal factura: 1.234,56 €"
ANSWER = {"data": {"invoice_number": "F-2026-0042", "total": 1234.56, "issue_date": "2026-03-15"},
          "evidence": [{"path": "invoice_number", "quote": "FACTURA N.º F-2026-0042", "unit": 1},
                       {"path": "total", "quote": "Total factura: 1.234,56 €", "unit": 1},
                       {"path": "issue_date", "quote": "Fecha: 15/03/2026", "unit": 1}]}
ROUTES = {"utility": ("http://127.0.0.1:8081/v1/chat/completions", "small", {}),
          "extraction": ("http://127.0.0.1:8081/v1/chat/completions", "small", {}),
          "default": ("http://127.0.0.1:8081/v1/chat/completions", "big", {})}


class Script:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def complete(self, messages, **opts):
        self.calls.append({"messages": messages, **opts})
        if not self.answers:
            raise ModelUnavailable("the script ran out of answers")
        answer = self.answers.pop(0)
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """Production seams pointed at a scripted model and a fixed route table."""
    script = Script(ANSWER)
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.schema_extraction._default_resolver", lambda purpose, owner: ROUTES[purpose])
    monkeypatch.setattr("src.schema_extraction._json_mode_ok", lambda model: None)
    monkeypatch.setattr("src.workflows.model_calls.production",
                        lambda: ModelCalls(complete=script.complete, decide=lambda *a, **k: {}))
    return script


# ── registration ──────────────────────────────────────────────────────────

def test_the_tool_is_registered_read_only_plan_safe_and_workspace_untrusted():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.agent_tools.subagent_tools import _READ_TOOLS
    from src.tool_authority import AUTHORITY
    from src.tool_capabilities import ResultIntegrity, ToolEffect, capabilities_for_tool
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, PLAN_MODE_READONLY_TOOLS
    assert "extract_to_schema" in TOOL_TAGS and "extract_to_schema" in TOOL_HANDLERS
    assert "extract_to_schema" in PLAN_MODE_READONLY_TOOLS and "extract_to_schema" in NON_ADMIN_BLOCKED_TOOLS
    assert "extract_to_schema" in _READ_TOOLS
    caps = capabilities_for_tool("extract_to_schema")
    assert caps.effects == frozenset({ToolEffect.READ_WORKSPACE})
    assert caps.result_integrity == ResultIntegrity.WORKSPACE_UNTRUSTED
    tool = AUTHORITY.get("extract_to_schema")
    assert tool.family == "documents" and tool.origin == "authored"
    assert AUTHORITY.required_groups("extract_to_schema") == (("path", "text"),)
    params = AUTHORITY.emit("extract_to_schema")["function"]["parameters"]
    assert {"path", "text", "schema", "schema_name", "instructions", "ocr", "tier", "max_chars"} == set(params["properties"])
    assert AUTHORITY.claims_for_call("extract_to_schema", {"path": "/w/f.pdf"}) == ("fs:read:/w/f.pdf",)


def test_the_authority_stays_clean_with_the_tool_registered():
    from src.tool_authority import AUTHORITY
    from src.tool_authority_catalog import unregistered_builtins
    import src.agent_tools
    assert unregistered_builtins() == []
    assert [i for i in AUTHORITY.parity_report(src.agent_tools.TOOL_HANDLERS) if i.tool == "extract_to_schema"] == []
    assert AUTHORITY.parity_report(src.agent_tools.TOOL_HANDLERS) == []


def test_once_offered_natively_its_schema_and_discovery_entries_match_the_authority():
    """The native schema list, the tool index descriptions and its example
    phrases live in files another task holds (see task #40 in the coordination
    board); this pins the entries the moment they are added."""
    from src.tool_authority import AUTHORITY
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    native = [x for x in FUNCTION_TOOL_SCHEMAS if x["function"]["name"] == "extract_to_schema"]
    if not native:
        pytest.skip("pending: the native schema entry in src/tool_schemas.py (held by another task)")
    assert native == [AUTHORITY.emit("extract_to_schema")]
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES
    assert "extract_to_schema" in BUILTIN_TOOL_DESCRIPTIONS
    phrases = " ".join(EXAMPLES["extract_to_schema"]).lower()
    assert "factura" in phrases and "json" in phrases


# ── the handler ───────────────────────────────────────────────────────────

def _call(args, ctx=None):
    from src.agent_tools import TOOL_HANDLERS
    return asyncio.run(TOOL_HANDLERS["extract_to_schema"](json.dumps(args), ctx or {"owner": "alice"}))


def test_the_handler_extracts_text_into_the_schema_with_evidence(wired):
    out = _call({"text": TEXT, "schema": INVOICE})
    assert out["exit_code"] == 0
    result = out["extraction"]
    assert result["data"] == {"invoice_number": "F-2026-0042", "total": 1234.56, "issue_date": "2026-03-15"}
    assert result["schema_valid"] is True and result["route"]["purpose"] == "utility"
    summary = json.loads(out["output"])
    assert summary["data"]["total"] == 1234.56 and summary["evidence"][0]["quote"]
    assert "response_schema" in wired.calls[0] and "tools" not in wired.calls[0]


def test_the_handler_drops_a_rewritten_quote_and_reports_the_readers_page_and_the_limits(wired):
    forged = {"data": {"invoice_number": "F-2026-0042", "total": 9234.56, "issue_date": "2026-03-15"},
              "evidence": [{"path": "invoice_number", "quote": "FACTURA N.º F-2026-0042", "unit": 9},
                           {"path": "total", "quote": "Total factura: 9.234,56 €", "unit": 1},
                           {"path": "issue_date", "quote": "Fecha: 15/03/2026", "unit": 1}]}
    wired.answers[:] = [forged]
    out = _call({"text": TEXT, "schema": INVOICE})
    result = out["extraction"]
    assert result["data"]["total"] is None and result["missing_required"] == ["total"]
    assert result["schema_valid"] is False
    assert [d["path"] for d in result["dropped"]] == ["total"]
    page = {e["path"]: e["unit"] for e in result["evidence"]}
    assert page["invoice_number"] == 1 and "9" not in {str(v) for v in page.values()}
    summary = json.loads(out["output"])
    assert any("does not prove" in note for note in summary["limits"])


def test_the_handler_reads_a_workspace_document_through_the_confined_path(wired, monkeypatch, tmp_path):
    doc = tmp_path / "factura.txt"
    doc.write_text(TEXT, encoding="utf-8")
    seen = {}
    monkeypatch.setattr("src.tool_execution._resolve_tool_path", lambda value: seen.setdefault("p", str(doc)))
    monkeypatch.setattr("src.schema_extraction._default_reader", lambda path, ocr: {
        "ok": True, "kind": "text", "via": "local", "units": [{"kind": "page", "number": 1, "text": TEXT}]})
    out = _call({"path": "factura.txt", "schema": INVOICE})
    assert out["exit_code"] == 0 and seen["p"] == str(doc)
    assert out["extraction"]["source"]["pages"] == 1


def test_the_handler_refuses_a_path_outside_the_workspace_and_bad_arguments(wired, monkeypatch):
    def outside(value):
        raise ValueError("path is outside the allowed workspace")
    monkeypatch.setattr("src.tool_execution._resolve_tool_path", outside)
    out = _call({"path": "C:/Windows/win.ini", "schema": INVOICE})
    assert out["exit_code"] == 1 and out["error_code"] == "path_not_allowed"
    for bad in ({"schema": INVOICE}, {"text": "x", "path": "y", "schema": INVOICE},
                {"text": "x", "schema": INVOICE, "shell": "rm"}):
        assert _call(bad)["error_code"] == "invalid_arguments"
    assert _call({"text": "x", "schema": "{not json"})["error_code"] == "invalid_schema"
    assert _call({"text": "x", "schema": {"type": "array"}})["error_code"] == "invalid_schema"
    assert _call({"text": "x", "schema_name": "missing"})["error_code"] == "unknown_schema"
    assert wired.calls == []


def test_the_handler_uses_a_saved_schema_by_name(wired):
    se.save_schema("alice", "factura", INVOICE)
    out = _call({"text": TEXT, "schema_name": "factura"})
    assert out["exit_code"] == 0 and out["extraction"]["data"]["total"] == 1234.56


# ── REST ──────────────────────────────────────────────────────────────────

def _client(monkeypatch, owner="alice", admin=True):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    import routes.extraction_routes as route_module

    def gate(request):
        if not admin:
            raise HTTPException(status_code=403, detail="Admin only")

    monkeypatch.setattr(route_module, "require_admin", gate)
    monkeypatch.setattr(route_module, "_owner", lambda request: owner)
    app = FastAPI()
    app.include_router(route_module.setup_extraction_routes())
    return TestClient(app)


def test_rest_extracts_text_and_profiles_without_calling_a_model(wired, monkeypatch):
    client = _client(monkeypatch)
    res = client.post("/api/extract", json={"text": TEXT, "schema": INVOICE})
    assert res.status_code == 200, res.text
    assert res.json()["data"]["invoice_number"] == "F-2026-0042"
    calls = len(wired.calls)
    nested = {"type": "object", "properties": {"lines": {"type": "array", "items": {
        "type": "object", "properties": {"a": {"type": "number"}}}}, "parties": {"type": "array", "items": {
            "type": "object", "properties": {"n": {"type": "string"}}}}}}
    res = client.post("/api/extract/profile", json={"schema": nested})
    assert res.status_code == 200
    body = res.json()
    assert body["profile"]["tier"] == "complex" and body["route"]["purpose"] == "default"
    assert body["route"]["model"] == "big" and "_url" not in body["route"]
    assert body["envelope"]["required"] == ["data", "evidence"]
    simple = client.post("/api/extract/profile", json={"schema": INVOICE, "text_chars": 100}).json()
    assert simple["route"]["purpose"] == "utility" and simple["unchecked_keywords"] == ["format"]
    assert len(wired.calls) == calls


def test_rest_reading_a_local_path_is_admin_only_and_errors_have_codes(wired, monkeypatch):
    client = _client(monkeypatch, admin=False)
    assert client.post("/api/extract", json={"path": "C:/x.pdf", "schema": INVOICE}).status_code == 403
    client = _client(monkeypatch)
    res = client.post("/api/extract", json={"text": TEXT, "schema": {"type": "string"}})
    assert res.status_code == 400 and res.json()["detail"]["code"] == "invalid_schema"
    res = client.post("/api/extract", json={"text": TEXT, "schema_name": "nope"})
    assert res.status_code == 404
    assert client.post("/api/extract", json={"schema": INVOICE}).status_code == 400
    assert client.post("/api/extract", content=b"[1, 2]", headers={"content-type": "application/json"}).status_code == 400


def test_rest_schema_crud_is_owner_scoped_and_refuses_traversal(wired, monkeypatch, tmp_path):
    alice = _client(monkeypatch, owner="alice")
    res = alice.put("/api/extract/schemas/factura", json={"schema": INVOICE, "description": "proveedores"})
    assert res.status_code == 200 and res.json()["name"] == "factura"
    assert alice.get("/api/extract/schemas").json()["schemas"][0]["name"] == "factura"
    assert alice.get("/api/extract/schemas/factura").json()["schema"] == INVOICE
    bob = _client(monkeypatch, owner="bob")
    assert bob.get("/api/extract/schemas/factura").status_code == 404
    assert bob.get("/api/extract/schemas").json() == {"schemas": []}
    alice = _client(monkeypatch, owner="alice")      # the owner seam is module-wide
    for bad in ("..%2F..%2Fevil", "%2E%2E", "a.b", "x%5Cy", "-x"):
        res = alice.put(f"/api/extract/schemas/{bad}", json={"schema": INVOICE})
        assert res.status_code in (400, 404, 405), (bad, res.status_code)
        if res.status_code == 400:
            assert res.json()["detail"]["code"] == "invalid_name"
    assert alice.put("/api/extract/schemas/loop", json={"schema": {
        "type": "object", "properties": {"n": {"$ref": "#/$defs/N"}},
        "$defs": {"N": {"type": "object", "properties": {"next": {"$ref": "#/$defs/N"}}}}}}).status_code == 400
    written = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.json"))
    assert written == ["extraction_schemas/alice/factura.json"]
    res = alice.post("/api/extract/profile", json={"schema_name": "factura"})
    assert res.status_code == 200 and res.json()["profile"]["tier"] == "simple"
    assert alice.delete("/api/extract/schemas/factura").json() == {"deleted": "factura"}
    assert alice.delete("/api/extract/schemas/factura").status_code == 404
