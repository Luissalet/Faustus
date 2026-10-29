"""Pinned source reads preserve complete text through the native artifact boundary."""
from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from src.agent_tools.reach_tools import _result_to_output
from src.reach.base import ReachBackendError, ReachResult
from src.reach import github

PIN = "a" * 40
BASE = "https://github.com/example/source"


@pytest.fixture
def source_api(monkeypatch):
    calls = []
    replies = {}

    def handle(request):
        assert request.url.host == "api.github.com"
        assert request.headers["Authorization"] == "Bearer fixture-only"
        calls.append(request)
        response = replies[request.url.path]
        return httpx.Response(200, json=response)

    def make_client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(github, "make_client", make_client)
    monkeypatch.setattr(github.credentials, "get_token", lambda _: "fixture-only")
    return replies, calls


def read(path, kind="blob"):
    return asyncio.run(github.GithubApiBackend().read(f"{BASE}/{kind}/{PIN}/{path}"))


def test_directory_preserves_entries_beyond_the_items_preview(source_api):
    replies, calls = source_api
    entries = [{"name": f"file{i}.c", "path": f"src/file{i}.c", "type": "file", "sha": str(i)} for i in range(75)]
    entries.append({"name": "nested", "path": "src/nested", "type": "dir", "sha": "dirsha"})
    replies["/repos/example/source/contents/src"] = {"type": "dir", "entries": entries}
    result = read("src", "tree")
    listing = json.loads(result.text)
    assert len(listing["entries"]) == 76 and not result.items and not result.truncated
    assert listing["entries"][-1]["url"] == f"{BASE}/tree/{PIN}/src/nested"
    assert listing["entries"][74]["url"] == f"{BASE}/blob/{PIN}/src/file74.c"
    assert calls[0].url.params["ref"] == PIN
    assert calls[0].headers["Accept"] == "application/vnd.github.object+json"


def test_directory_at_api_limit_reports_possible_truncation(source_api):
    replies, _ = source_api
    replies["/repos/example/source/contents/src"] = [{"name": str(i), "path": f"src/{i}", "type": "file"} for i in range(1000)]
    result = read("src", "tree")
    assert result.truncated and len(json.loads(result.text)["entries"]) == 1000


def test_large_source_uses_blob_sha_and_preserves_exact_newlines(source_api):
    replies, calls = source_api
    text = "// español\r\n" + "// padding\n" * 2000 + "void FinalDefinition() {}\n"
    replies["/repos/example/source/contents/src/source.c"] = {"encoding": "none", "sha": "blobsha"}
    replies["/repos/example/source/git/blobs/blobsha"] = {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
    result = read("src/source.c")
    native = _result_to_output(result)
    assert result.text == native["full_text"] == text
    assert "FinalDefinition" not in native["output"] and native["truncated"]
    assert calls[-1].url.path.endswith("/git/blobs/blobsha")
    assert result.url == f"{BASE}/blob/{PIN}/src/source.c"


@pytest.mark.parametrize("body", [b"\xff", b"binary\0data"])
def test_binary_source_is_an_explicit_failure(source_api, body):
    replies, _ = source_api
    replies["/repos/example/source/contents/binary"] = {"encoding": "base64", "content": base64.b64encode(body).decode()}
    with pytest.raises(ReachBackendError):
        read("binary")


def test_encoded_ref_and_path_preserve_source_identity():
    target = github._parse_target(f"{BASE}/blob/feature%2Fbranch/src/file%20name.c#L99")
    assert target["ref"] == "feature/branch" and target["path"] == "src/file name.c"


def test_native_preview_does_not_hide_upstream_truncation():
    result = _result_to_output(ReachResult(channel="github", text="partial", truncated=True))
    assert result["truncated"] and "full_text" not in result
