"""Permanent file-transfer contracts; local files and declared simulated peers, no browser/model."""
import base64
import hashlib
from pathlib import Path

import pytest

from src.browser_relay.protocol import validate_arguments
from src.browser_relay.server import download_target, mcp_result, save_download, upload_inputs


def packet(data, parts=None):
    # Padding belongs to each chunk; concatenating these strings is incorrect.
    chunks = parts if parts is not None else [data[:1], data[1:7], data[7:]]
    return {"ok": True, "phase": "readback_complete", "result": {
        "tab": {"id": 2}, "action": "download", "source": {"url": "blob:https://writer.test/own"},
        "download": {"name": "cambios-Élodie.json", "mime": "application/json", "bytes": len(data),
                     "parts": [base64.b64encode(chunk).decode() for chunk in chunks]}}}


@pytest.mark.parametrize("data", [b"", "á你🙂é\n".encode(), bytes(range(256)) * 513 + b"X"], ids=["empty", "unicode", "binary-multipart"])
def test_complete_bytes_and_receipt_without_inline_transfer(tmp_path, data):
    path = tmp_path / "cambios-你.json"
    raw = packet(data)
    result = save_download(raw, str(path))
    assert result["ok"] and path.read_bytes() == data
    receipt = result["result"]["file"]
    assert receipt == {"path": str(path), "name": "cambios-Élodie.json", "mime": "application/json",
                       "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "complete": True}
    assert "download" not in result["result"]
    public = mcp_result(result)
    assert public.structuredContent == result and not public.isError
    assert '"parts"' not in public.content[0].text
    assert raw["result"]["download"]["parts"], "input fixture remains unchanged"


@pytest.mark.parametrize("transfer", [
    {"name": "x", "mime": "", "bytes": 2, "parts": ["YQ=="]},
    {"name": "x", "mime": "", "bytes": 1, "parts": ["%bad"]},
    {"name": "x", "mime": "", "bytes": 1, "parts": [None]},
    {"name": "x", "mime": "", "bytes": True, "parts": []},
    {"name": "x", "mime": "", "bytes": -1, "parts": []},
    {"name": "x", "mime": "", "bytes": 0},
])
def test_invalid_or_incomplete_transfer_never_creates_file(tmp_path, transfer):
    raw = packet(b"");raw["result"]["download"] = transfer
    path = tmp_path / "never.json"
    result = save_download(raw, str(path))
    assert not result["ok"] and result["error"]["code"] == "invalid_download"
    assert result["phase"] == "action_confirmed" and not path.exists()
    assert '"parts"' not in mcp_result(result).content[0].text


def test_existing_readonly_file_and_source_input_preserved(tmp_path):
    path = tmp_path / "existing.bin";path.write_bytes(b"ORIGINAL");path.chmod(0o444)
    try:
        with pytest.raises(FileExistsError):download_target(str(path))
        result = save_download(packet(b"NEW"), str(path))
        assert not result["ok"] and result["error"]["code"] == "download_write_failed"
        assert path.read_bytes() == b"ORIGINAL"
    finally:path.chmod(0o666)


def test_upload_preflight_observes_real_unicode_file_and_refuses_missing_directory_relative(tmp_path):
    path = tmp_path / "paquete-你.json";data = b'{}\n';path.write_bytes(data)
    assert upload_inputs([str(path)]) == [{"path": str(path), "name": path.name, "bytes": len(data),
                                         "sha256": hashlib.sha256(data).hexdigest()}]
    assert path.read_bytes() == data
    for bad in [str(tmp_path / "absent"), str(tmp_path), "relative.json"]:
        with pytest.raises(ValueError):upload_inputs([bad])


@pytest.mark.parametrize("arguments", [
    {"action": "upload", "tab_id": 2, "ref": "r", "files": []},
    {"action": "upload", "tab_id": True, "ref": "r", "files": ["x"]},
    {"action": "download", "tab_id": 2, "ref": "r"},
    {"action": "click", "tab_id": 2, "ref": "r", "files": ["x"]},
    {"action": "upload", "tab_id": 2, "ref": "r", "files": ["x"], "output_path": "x"},
    {"action": "download", "tab_id": 2, "ref": "r", "output_path": "x", "script": "caller code"},
])
def test_file_schema_is_explicit_without_a_caller_script(arguments):
    with pytest.raises(ValueError):validate_arguments(arguments)


def test_new_and_old_schema_and_detached_files_list():
    args = {"action": "upload", "tab_id": 2, "ref": "r", "files": ["C:/qa/one.json"], "view": {"query": "Imported"}}
    detached = validate_arguments(args);args["files"].append("C:/qa/two.json")
    assert detached["files"] == ["C:/qa/one.json"]
    assert validate_arguments({"action": "download", "tab_id": 2, "ref": "r", "output_path": "C:/qa/new.json"})["action"] == "download"
    assert validate_arguments({"action": "tabs"}) == {"action": "tabs"}



def test_disk_write_failure_is_not_complete_and_retains_only_new_partial_file(tmp_path, monkeypatch):
    path = tmp_path / 'partial.bin';real_open = Path.open
    class Partial:
        def __enter__(self):self.handle=real_open(path,'xb');return self
        def write(self,data):self.handle.write(data[:1]);raise OSError('simulated disk write failure')
        def __exit__(self,*args):self.handle.close()
    def opened(self,mode='r',*args,**kwargs):
        return Partial() if self==path and mode=='xb' else real_open(self,mode,*args,**kwargs)
    monkeypatch.setattr(Path,'open',opened)
    result=save_download(packet(b'XYZ'),str(path))
    assert not result['ok'] and result['error']['code']=='download_write_failed'
    assert str(path) in result['error']['message'] and path.read_bytes()==b'X'
    assert 'file' not in result and 'complete' not in result



@pytest.mark.parametrize("data,uri", [
    ("payload privado Élodie你🙂\n".encode(), "data:text/plain;charset=utf-8,payload%20privado%20%C3%89lodie%E4%BD%A0%F0%9F%99%82%0A"),
    (bytes(range(256)), "data:application/octet-stream;base64," + base64.b64encode(bytes(range(256))).decode()),
])
def test_data_url_payload_never_appears_in_public_mcp_receipt(tmp_path, data, uri):
    raw = packet(data);raw["result"]["source"] = {"url": uri, "document": {"url": "https://writer.test/own"}}
    result = save_download(raw, str(tmp_path / "data.bin"))
    assert result["ok"] and (tmp_path / "data.bin").read_bytes() == data
    assert result["result"]["source"] == {"scheme": "data:", "url_omitted": True, "document": {"url": "https://writer.test/own"}}
    assert raw["result"]["source"]["url"] == uri
    public = mcp_result(result)
    assert public.structuredContent == result and not public.isError
    serialized = public.model_dump_json()
    assert uri not in serialized and uri.split(",", 1)[1] not in serialized
    assert '"parts"' not in public.content[0].text and "download" not in public.structuredContent["result"]
    assert result["result"]["file"]["sha256"] == hashlib.sha256(data).hexdigest()
