"""Tests for chromadb_health — ok/degraded/down/disabled classification."""
import pytest

from src import service_health as sh


class _Store:
    def __init__(self, healthy):
        self.healthy = healthy


def test_chromadb_both_healthy_ok():
    s = sh.chromadb_health(_Store(True), _Store(True))
    assert s["status"] == sh.OK
    assert s["meta"] == {"rag": True, "memory": True}


def test_chromadb_one_down_degraded():
    s = sh.chromadb_health(_Store(True), _Store(False))
    assert s["status"] == sh.DEGRADED


def test_chromadb_both_unhealthy_down():
    s = sh.chromadb_health(_Store(False), _Store(False))
    assert s["status"] == sh.DOWN


def test_chromadb_both_absent_disabled():
    s = sh.chromadb_health(None, None)
    assert s["status"] == sh.DISABLED


def test_chromadb_one_absent_one_healthy_ok():
    # An absent store is not a failure; the present one being healthy is ok.
    s = sh.chromadb_health(_Store(True), None)
    assert s["status"] == sh.OK
    assert s["meta"]["memory"] is None


# ── The server behind the flags (Agora #225) ──
# The stores flip `healthy` only when an operation fails, so a ChromaDB that
# stops after startup read as "Vector stores healthy" in the services panel.

import socket

from src import chroma_client


@pytest.fixture
def connected(monkeypatch):
    """Pretend a client was connected to `addr`, as get_chroma_client does."""
    def at(addr):
        monkeypatch.setattr(chroma_client, "_client", object())
        monkeypatch.setattr(chroma_client, "_client_addr", addr)
    return at


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_healthy_flags_but_server_gone_is_down(connected):
    connected(("127.0.0.1", _closed_port()))
    s = sh.chromadb_health(_Store(True), _Store(True))
    assert s["status"] == sh.DOWN
    assert "stopped answering" in s["detail"]
    assert s["meta"]["server_reachable"] is False


def test_healthy_flags_and_server_answering_is_ok(connected):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        connected(srv.getsockname())
        s = sh.chromadb_health(_Store(True), _Store(True))
        assert s["status"] == sh.OK
    finally:
        srv.close()


def test_no_connected_client_trusts_the_flags(monkeypatch):
    monkeypatch.setattr(chroma_client, "_client", None)
    monkeypatch.setattr(chroma_client, "_client_addr", None)
    assert chroma_client.server_reachable() is None
    assert sh.chromadb_health(_Store(True), _Store(True))["status"] == sh.OK


def test_reset_client_forgets_the_address(connected):
    connected(("127.0.0.1", 1))
    chroma_client.reset_client()
    assert chroma_client._client is None
    assert chroma_client._client_addr is None
