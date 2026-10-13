"""Typed channel clients receive full data while existing API previews remain."""
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from src import integrations


@pytest.mark.parametrize("status", [200, 429])
async def test_optional_data_preserves_complete_response(status):
    data = {"messages": [{"ts": "001.000001", "text": "á" * 16000}], "response_metadata": {"next_cursor": "next=opaque"}}
    response = MagicMock(status_code=status, headers={"content-type": "application/json"})
    response.json.return_value = data
    client = AsyncMock();client.__aenter__.return_value = client;client.request.return_value = response
    integration = {"id": "fixture", "name": "Fixture", "base_url": "https://api.example.test", "enabled": True, "auth_type": "none"}
    with patch.object(integrations, "_find_integration", return_value=integration), patch("httpx.AsyncClient", return_value=client), \
         patch("src.url_safety._default_resolver", return_value=["93.184.216.34"]):
        result = await integrations.execute_api_call("fixture", "GET", "/messages", return_data=True)
    assert result["data"] == data
    assert len(result["data"]["messages"][0]["text"]) == 16000
    assert result["status_code"] == status
    assert result["untrusted_content"] is True
    assert result["exit_code"] == (0 if status == 200 else 1)


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]", "93.184.216.34"])
async def test_configured_literal_address_uses_validated_pin(host):
    response = MagicMock(status_code=200, headers={"content-type": "application/json"})
    response.json.return_value = {"ok": True}
    client = AsyncMock(); client.__aenter__.return_value = client; client.request.return_value = response
    integration = {"id": "fixture", "name": "Fixture", "base_url": "http://" + host, "enabled": True, "auth_type": "none"}
    with patch.object(integrations, "_find_integration", return_value=integration), patch("httpx.AsyncClient", return_value=client), \
         patch("src.url_safety._default_resolver", side_effect=AssertionError("literal must not resolve DNS")):
        result = await integrations.execute_api_call("fixture", "GET", "/messages", return_data=True)
    assert result["exit_code"] == 0
    assert result["data"] == {"ok": True}
    client.request.assert_awaited_once()
