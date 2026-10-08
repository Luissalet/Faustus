"""Malformed model arguments must fail without reading or mutating reports."""
import json

import pytest

from src.tools import research


@pytest.mark.parametrize("field", ["action", "id", "session_id", "research_id", "search"])
@pytest.mark.parametrize("value", [[], ["delete"], {}, {"id": "saved"}, True, False, 0, 2.5])
async def test_non_text_arguments_fail_before_filesystem_access(monkeypatch, field, value):
    class UnavailableReports:
        def __fspath__(self):
            raise AssertionError("Invalid arguments accessed the report directory")

    monkeypatch.setattr(research, "DEEP_RESEARCH_DIR", UnavailableReports())
    result = await research.do_manage_research(json.dumps({field: value}))
    assert result["error_code"] == "invalid_arguments"
    assert result["exit_code"] == 1
    assert field in result["error"]


async def test_bad_delete_then_corrected_read_preserves_report(monkeypatch, tmp_path):
    monkeypatch.setattr(research, "DEEP_RESEARCH_DIR", tmp_path)
    report = tmp_path / "saved.json"
    original = b'{"query":"Isolated report","summary":"Keep this content"}'
    report.write_bytes(original)
    bad = await research.do_manage_research(json.dumps({"action": ["delete"], "id": "saved"}))
    assert bad["error_code"] == "invalid_arguments"
    assert report.read_bytes() == original
    corrected = await research.do_manage_research(json.dumps({"action": "read", "id": "saved"}))
    assert corrected["exit_code"] == 0
    assert "Keep this content" in corrected["output"]
    assert report.read_bytes() == original
    assert list(tmp_path.iterdir()) == [report]


@pytest.mark.parametrize("args", [{}, {"action": ""}, {"action": None, "search": None}])
async def test_existing_default_list_behavior(monkeypatch, tmp_path, args):
    monkeypatch.setattr(research, "DEEP_RESEARCH_DIR", tmp_path)
    (tmp_path / "saved.json").write_text('{"query":"Existing default","summary":"Content"}')
    result = await research.do_manage_research(json.dumps(args))
    assert result["exit_code"] == 0
    assert "Existing default" in result["output"]


async def test_valid_delete_remains_available(monkeypatch, tmp_path):
    monkeypatch.setattr(research, "DEEP_RESEARCH_DIR", tmp_path)
    report = tmp_path / "saved.json"
    report.write_text('{"query":"Disposable fixture"}')
    result = await research.do_manage_research('{"action":"delete","id":"saved"}')
    assert result["exit_code"] == 0
    assert not report.exists()
