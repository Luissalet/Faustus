"""In-place recovery of the ChromaDB-backed stores (FAUSTUS).

The scenario: Docker gets closed, the container dies, and the store objects
held by the chat processor and the memory provider go unhealthy. Recovery has
to happen *on those objects* — a new instance would leave every holder pointing
at the dead one — and it must never raise, because it runs inside an endpoint.
"""

import src.service_recovery as recovery


class FakeStore:
    def __init__(self, healthy_after=True, boom=False):
        self._healthy_after = healthy_after
        self.boom = boom
        self.calls = 0
        self.healthy = False

    def reconnect(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("chroma still down")
        self.healthy = self._healthy_after
        return self.healthy


class LegacyStore:
    """No public reconnect() — recovery falls back to the initializer."""

    def __init__(self):
        self.healthy = False
        self.calls = 0

    def _initialize(self):
        self.calls += 1
        self.healthy = True


class TestReconnectOne:
    def test_missing_store_is_absent_not_an_error(self):
        assert recovery._reconnect_one(None) == "absent"

    def test_healthy_after_reconnect(self):
        store = FakeStore()
        assert recovery._reconnect_one(store) == "healthy"
        assert store.calls == 1

    def test_still_down_reports_unhealthy(self):
        assert recovery._reconnect_one(FakeStore(healthy_after=False)) == "unhealthy"

    def test_raising_store_is_contained(self):
        assert recovery._reconnect_one(FakeStore(boom=True)) == "error"

    def test_legacy_store_uses_its_initializer(self):
        store = LegacyStore()
        assert recovery._reconnect_one(store) == "healthy"
        assert store.calls == 1


class TestReconnectVectorStores:
    def test_resets_the_client_singleton_first(self, monkeypatch):
        """Without dropping the cached client, re-init reuses the dead socket."""
        seen = []
        import src.chroma_client as chroma_client
        monkeypatch.setattr(chroma_client, "reset_client",
                            lambda: seen.append("reset"))
        rag, mem = FakeStore(), FakeStore()
        out = recovery.reconnect_vector_stores(rag, mem)
        assert seen == ["reset"]
        assert out == {"chroma_client": "reset", "rag": "healthy", "memory": "healthy"}

    def test_clears_the_lazy_singleton_retry_throttle(self, monkeypatch):
        """A failed init blocks retries for 30s; recovery must not wait it out."""
        import src.rag_singleton as rag_singleton
        monkeypatch.setattr(rag_singleton, "_last_attempt", 12345.0, raising=False)
        monkeypatch.setattr(rag_singleton, "rag_instance", None, raising=False)
        recovery.reconnect_vector_stores(FakeStore(), FakeStore())
        assert rag_singleton._last_attempt == 0.0

    def test_uses_the_live_singleton_when_no_manager_was_passed(self, monkeypatch):
        import src.rag_singleton as rag_singleton
        live = FakeStore()
        monkeypatch.setattr(rag_singleton, "rag_instance", live, raising=False)
        out = recovery.reconnect_vector_stores(None, None)
        assert out["rag"] == "healthy"
        assert out["memory"] == "absent"
        assert live.calls == 1

    def test_a_broken_client_reset_does_not_abort_recovery(self, monkeypatch):
        import src.chroma_client as chroma_client

        def boom():
            raise RuntimeError("no chromadb package")

        monkeypatch.setattr(chroma_client, "reset_client", boom)
        out = recovery.reconnect_vector_stores(FakeStore(), FakeStore())
        assert out["chroma_client"] == "error"
        assert out["rag"] == "healthy"
