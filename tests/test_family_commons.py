from src import family_services
from src.reach.rss import _parse_minimal
from src.url_safety import check_outbound_url


def test_family_adapter_uses_configured_hub_and_does_not_copy_tokens(monkeypatch):
    monkeypatch.setattr(family_services.hoard_hub, "hub_location", lambda: {
        "url": "http://127.0.0.1:8810", "token_file": "fixture-token"})
    seen = []
    monkeypatch.setattr(family_services.family, "configure", lambda *a, **k: seen.append((a, k)))
    monkeypatch.setattr(family_services.fam_media, "download", lambda url, **kw: {"ok": True, "path": "audio.wav"})
    assert family_services.download("https://example.com/audio")["ok"]
    assert seen == [(("faustus",), {"token_file": "fixture-token", "hub": "http://127.0.0.1:8810"})]


def test_atom_keeps_updated_and_refuses_entities():
    xml = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Feed</title><entry><title>Item</title><updated>2026-10-02T12:00:00Z</updated><published>2026-10-01T12:00:00Z</published></entry></feed>'
    assert _parse_minimal(xml, "https://example.com/feed").items[0]["at"] == "2026-10-02T12:00:00Z"


def test_family_uses_installed_token_only_for_default_local_hub(monkeypatch):
    seen = []
    monkeypatch.setattr(family_services.family, "configure", lambda *a, **k: seen.append(k))
    monkeypatch.setattr(family_services, "_installed_hub_token", lambda: "installed-path")
    for url in ("http://127.0.0.1:8810", "https://example.com", "http://127.0.0.1:9999"):
        monkeypatch.setattr(family_services.hoard_hub, "hub_location", lambda: {"url": url, "token_file": ""})
        family_services.configure()
    assert [entry["token_file"] for entry in seen] == ["installed-path", "", ""]


def test_shared_url_guard_preserves_local_models_and_blocks_obfuscated_loopback():
    assert check_outbound_url("http://127.0.0.1:8081/v1")[0]
    assert not check_outbound_url("http://2130706433", block_private=True)[0]
    assert not check_outbound_url("http://user:pw@example.com", resolver=lambda h: ["8.8.8.8"])[0]


def test_page_transport_prefers_hub_and_falls_back_only_when_unavailable(monkeypatch):
    from services.search import content
    monkeypatch.setattr(family_services, "fetch", lambda *a, **k: {
        "ok": True, "status": 200, "body": b"hello", "headers": {"Content-Type": "text/plain"}})
    assert content._get_public_url("https://example.com", {}, 1).content == b"hello"
    seen = []
    monkeypatch.setattr(content, "_get_local_public_url", lambda *a: seen.append(a) or "fallback")
    monkeypatch.setattr(family_services, "fetch", lambda *a, **k: {"ok": False, "error": "hub unreachable"})
    assert content._get_public_url("https://example.com", {}, 1) == "fallback" and len(seen) == 1


def test_audio_owner_reads_temporary_file_before_it_is_removed(monkeypatch):
    from pathlib import Path
    paths = []
    def transcribe(path, **kwargs):
        paths.append(path)
        assert Path(path).read_bytes() == b"original-container"
        assert kwargs["local_fallback"] is False
        return {"ok": True, "text": "Speech."}
    monkeypatch.setattr(family_services, "transcribe", transcribe)
    assert family_services.transcribe_bytes(b"original-container")["text"] == "Speech."
    assert not Path(paths[0]).exists()


def test_scanned_pdf_prefers_family_ocr_with_page_evidence(tmp_path, monkeypatch):
    from pypdf import PdfWriter
    from src import document_processor
    path = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)
    seen = []
    monkeypatch.setattr(family_services, "extract", lambda *a, **kw: seen.append((a, kw)) or {
        "ok": True, "units": [{"kind": "page", "number": 1, "text": "Recovered from the scan."}]})
    evidence = []
    text = document_processor._process_pdf(str(path), owner="fixture", evidence_out=evidence)
    assert "[Page 1 OCR via Kafka]" in text and "Recovered from the scan." in text
    assert len(evidence) == 1 and evidence[0].locator.value == "1"
    assert seen[0][1]["local_fallback"] is False
    seen.clear()
    document_processor._process_pdf(str(path), allow_vision=False)
    assert not seen
