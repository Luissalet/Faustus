"""tests/test_cmp06_herdr_adapter.py — CMP-06 (INFORME_COMPARATIVO_V2.md
§3.5): `src/external_runtimes/herdr.py`, a READ-ONLY Herdr client.

Sin investigación externa: every fixture here fakes the transport — no
socket is ever opened, matching the lote contract ("sin red en tests
(fakes)"). Also covers: version negotiation refuses an unrecognised
version rather than guessing; a timeout is `delivery='unknown'`, never
treated as "not sent"; presence rows carry the right `certainty`.
"""
from __future__ import annotations

import time

import pytest
from cryptography.fernet import Fernet

from src import constants as constants_mod
from src import secret_storage
from src import settings as settings_mod
from src import external_runtimes as ext


@pytest.fixture(autouse=True)
def isolated_settings_and_key(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    # Never touch the real app key file; a fresh in-memory Fernet key per test.
    monkeypatch.setattr(secret_storage, "_fernet", Fernet(Fernet.generate_key()))
    yield


class FakeTransport:
    """Records every call; returns canned responses keyed by URL suffix, or
    raises a canned `TransportError` — no network, ever."""

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.raises = {}

    def set_response(self, suffix, status, json_body):
        self.responses[suffix] = {"status": status, "json": json_body}

    def set_raises(self, suffix, exc):
        self.raises[suffix] = exc

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, headers, timeout))
        for suffix, exc in self.raises.items():
            if url.endswith(suffix):
                raise exc
        for suffix, resp in self.responses.items():
            if url.endswith(suffix):
                return resp
        return {"status": 404, "json": None}


# ---------------------------------------------------------------------------
# config: load/save roundtrip, token encrypted at rest
# ---------------------------------------------------------------------------

def test_default_config_is_not_configured():
    cfg = ext.load_config()
    assert cfg.configured is False
    assert cfg.base_url == ""
    assert cfg.token == ""


def test_save_and_load_config_roundtrips_and_encrypts_token_at_rest():
    ext.save_config(base_url="https://herdr.example.internal", token="s3cr3t")
    cfg = ext.load_config()
    assert cfg.base_url == "https://herdr.example.internal"
    assert cfg.token == "s3cr3t"
    assert cfg.configured is True

    raw = settings_mod.get_setting("external_runtimes_herdr", {})
    assert raw["token"] != "s3cr3t"  # never plaintext at rest
    assert raw["token"].startswith("enc:")


def test_saving_base_url_without_a_token_keeps_the_stored_token():
    ext.save_config(base_url="https://herdr.example.internal", token="s3cr3t")
    ext.save_config(base_url="https://herdr2.example.internal")  # token omitted
    cfg = ext.load_config()
    assert cfg.base_url == "https://herdr2.example.internal"
    assert cfg.token == "s3cr3t"


def test_saving_an_empty_token_clears_it():
    ext.save_config(base_url="https://herdr.example.internal", token="s3cr3t")
    ext.save_config(base_url="https://herdr.example.internal", token="")
    cfg = ext.load_config()
    assert cfg.token == ""


# ---------------------------------------------------------------------------
# negotiate_version
# ---------------------------------------------------------------------------

def test_negotiate_version_requires_configuration_first():
    client = ext.HerdrClient(ext.HerdrConfig(base_url=""), transport=FakeTransport())
    with pytest.raises(ext.NotConfiguredError):
        client.negotiate_version()


def test_negotiate_version_accepts_a_known_version():
    transport = FakeTransport()
    transport.set_response("/version", 200, {"version": "1"})
    client = ext.HerdrClient(ext.HerdrConfig(base_url="https://herdr.example"), transport=transport)
    result = client.negotiate_version()
    assert result["version"] == "1"
    assert transport.calls[0][0] == "GET"


def test_negotiate_version_refuses_an_unrecognised_version_rather_than_guessing():
    transport = FakeTransport()
    transport.set_response("/version", 200, {"version": "99-beta"})
    client = ext.HerdrClient(ext.HerdrConfig(base_url="https://herdr.example"), transport=transport)
    with pytest.raises(ext.UnsupportedVersionError):
        client.negotiate_version()


def test_negotiate_version_timeout_is_unknown_never_not_delivered():
    transport = FakeTransport()
    transport.set_raises("/version", ext.TransportError("timed out", delivery="unknown"))
    client = ext.HerdrClient(ext.HerdrConfig(base_url="https://herdr.example"), transport=transport)
    with pytest.raises(ext.TransportError) as excinfo:
        client.negotiate_version()
    assert excinfo.value.delivery == "unknown"


def test_transport_error_before_any_bytes_sent_is_not_delivered():
    transport = FakeTransport()
    transport.set_raises("/version", ext.TransportError("connection refused", delivery="not_delivered"))
    client = ext.HerdrClient(ext.HerdrConfig(base_url="https://herdr.example"), transport=transport)
    with pytest.raises(ext.TransportError) as excinfo:
        client.negotiate_version()
    assert excinfo.value.delivery == "not_delivered"


# ---------------------------------------------------------------------------
# list_presence: certainty, signal_age_s, never sends
# ---------------------------------------------------------------------------

def test_list_presence_marks_structured_certainty_when_a_timestamp_is_present():
    now = time.time()
    transport = FakeTransport()
    transport.set_response("/sessions", 200, [
        {"id": "s1", "label": "Agent one", "state": "active", "last_seen_at": now - 5},
    ])
    client = ext.HerdrClient(ext.HerdrConfig(base_url="https://herdr.example"), transport=transport)
    rows = client.list_presence()
    assert len(rows) == 1
    assert rows[0].certainty == "structured"
    assert rows[0].signal_age_s is not None
    assert rows[0].signal_age_s >= 0


def test_list_presence_marks_heuristic_certainty_without_a_timestamp():
    transport = FakeTransport()
    transport.set_response("/sessions", 200, [
        {"id": "s2", "label": "Agent two", "state": "idle"},
    ])
    client = ext.HerdrClient(ext.HerdrConfig(base_url="https://herdr.example"), transport=transport)
    rows = client.list_presence()
    assert rows[0].certainty == "heuristic"
    assert rows[0].signal_age_s is None


def test_list_presence_accepts_a_wrapped_sessions_object_too():
    transport = FakeTransport()
    transport.set_response("/sessions", 200, {"sessions": [{"id": "s3", "state": "active"}]})
    client = ext.HerdrClient(ext.HerdrConfig(base_url="https://herdr.example"), transport=transport)
    rows = client.list_presence()
    assert len(rows) == 1
    assert rows[0].session_id == "s3"


def test_list_presence_only_ever_issues_get_requests():
    transport = FakeTransport()
    transport.set_response("/sessions", 200, [])
    client = ext.HerdrClient(ext.HerdrConfig(base_url="https://herdr.example"), transport=transport)
    client.list_presence()
    assert all(call[0] == "GET" for call in transport.calls)
