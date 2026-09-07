"""Service diagnostics stay bounded, read-only and explicit about uncertainty."""
import json
from types import SimpleNamespace

import httpx
import pytest

from src import doctor


@pytest.mark.parametrize("models,state", [([], "absent"), ([{"name": "local:small"}], "ok")])
def test_model_catalogue_without_inference(monkeypatch, models, state):
    urls = []
    monkeypatch.setattr(doctor, "_probe_json", lambda url: urls.append(url) or {"models": models})
    result = doctor._models()
    assert result.state == state
    assert urls[0].endswith("/api/tags")
    assert result.facts["count"] == len(models)


@pytest.mark.parametrize("payload", [{}, {"models": [None]}, {"models": [{"name": ""}]}])
def test_malformed_model_catalogue_is_not_empty(monkeypatch, payload):
    monkeypatch.setattr(doctor, "_probe_json", lambda _: payload)
    assert doctor._models().state == "unknown"


@pytest.mark.parametrize("payload,state", [(b"[]", "ok"), (b"[{}]", "ok"),
                                          (b"{", "fail"), (b"[null]", "fail"),
                                          (b"\xff", "fail")])
def test_memory_diagnostic_never_rewrites_store(monkeypatch, tmp_path, payload, state):
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    target = tmp_path / "memory.json"
    target.write_bytes(payload)
    assert doctor._memory_store().state == state
    assert target.read_bytes() == payload


def test_absent_memory_does_not_create_files(monkeypatch, tmp_path):
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    assert doctor._memory_store().state == "absent"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("heartbeat,state", [(123, "ok"), (True, "unknown"), (None, "unknown")])
def test_chroma_heartbeat_does_not_create_collection(monkeypatch, heartbeat, state):
    urls = []
    monkeypatch.setattr(doctor, "_probe_json", lambda url: urls.append(url) or
                        {"nanosecond heartbeat": heartbeat})
    assert doctor._memory_vectors().state == state
    assert urls[0].endswith("/api/v2/heartbeat")


@pytest.mark.parametrize("status,state", [("connected", "ok"), ("error", "warn"),
                                         ("disconnected", "warn")])
def test_browser_reads_connection_without_opening_page(monkeypatch, status, state):
    from src import tool_utils
    manager = SimpleNamespace(get_server_status=lambda sid: {"status": status})
    monkeypatch.setattr(tool_utils, "get_mcp_manager", lambda: manager)
    assert doctor._browser().state == state


def test_area_filter_does_not_probe_unrequested_backends(monkeypatch):
    monkeypatch.setattr(doctor, "_backends", lambda: pytest.fail("unexpected backend probe"))
    monkeypatch.setattr(doctor, "_browser", lambda: doctor.Finding("browser", "browser", "ok"))
    assert doctor.run(areas=["browser"])["worst"] == "ok"


def test_probe_rejects_redirect_and_large_body(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "https://secret.invalid"}))
    with httpx.Client(transport=transport) as client:
        monkeypatch.setattr(httpx, "stream", client.stream)
        with pytest.raises(httpx.HTTPStatusError):
            doctor._probe_json("http://local/health")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (1024 * 1024 + 1)))
    with httpx.Client(transport=transport) as client:
        monkeypatch.setattr(httpx, "stream", client.stream)
        with pytest.raises(ValueError, match="budget"):
            doctor._probe_json("http://local/health")


def test_failed_probe_never_echoes_endpoint_credentials(monkeypatch):
    def fail(_):
        raise RuntimeError("https://user:secret@private.invalid/?api_key=secret")
    monkeypatch.setattr(doctor, "_probe_json", fail)
    assert "secret" not in json.dumps(doctor._models().to_dict())
    assert "secret" not in json.dumps(doctor._memory_vectors().to_dict())
