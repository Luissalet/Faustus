"""With rag_pii_redaction on, a chunk that had something redacted must still
index: Chroma metadata only takes scalars, and a dict of counts made the
whole chunk fail (seen live, 26-09)."""

from src.rag_vector import _redaction_meta


def test_counts_become_scalars():
    meta = _redaction_meta({"PHONE": 1, "EMAIL": 2})
    assert meta == {"redactions": "EMAIL:2,PHONE:1", "redaction_count": 3}
    assert all(isinstance(v, (str, int)) for v in meta.values())


def test_nothing_redacted_adds_nothing():
    assert _redaction_meta({}) == {}


def test_index_keeps_a_redacted_pdf_chunk(tmp_path, monkeypatch):
    import src.rag_vector as rv
    added = []

    class FakeRAG(rv.VectorRAG):
        def __init__(self):  # no Chroma
            self._lanes = []

        def add_document(self, text, metadata):
            # What Chroma enforces: scalar metadata values only.
            assert all(isinstance(v, (str, int, float, bool)) or v is None for v in metadata.values()), metadata
            added.append((text, metadata))
            return True

    monkeypatch.setattr(rv, "get_setting", lambda k, d=None: True if k == "rag_pii_redaction" else d)
    import src.personal_docs as pd
    monkeypatch.setattr(pd, "extract_pdf_pages", lambda path: ["Contacto: ana@ejemplo.es, telefono 612 345 678."])
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4 fake")
    res = FakeRAG().index_personal_documents(str(tmp_path))
    assert res["indexed_count"] == 1 and res["failed_count"] == 0
    text, meta = added[0]
    assert "ana@ejemplo.es" not in text and meta["redaction_count"] >= 1 and meta["locator"].startswith("p1")
