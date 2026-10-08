"""OpenAPI must publish JSON requestBody for extract routes (REST→MCP bridge)."""
from app import app


def _body_schema(path: str, method: str) -> dict:
    op = app.openapi()["paths"][path][method]
    assert "requestBody" in op, (path, method, sorted(op))
    return op["requestBody"]["content"]["application/json"]["schema"]


def test_extract_profile_and_put_schema_declare_json_bodies():
    extract = _body_schema("/api/extract", "post")
    assert "path" in extract["properties"] and "text" in extract["properties"]
    assert "schema" in extract["properties"] or "schema_name" in extract["properties"]

    profile = _body_schema("/api/extract/profile", "post")
    assert "schema" in profile["properties"] or "schema_name" in profile["properties"]

    save = _body_schema("/api/extract/schemas/{name}", "put")
    assert "schema" in save["properties"]
    assert "schema" in (save.get("required") or [])
