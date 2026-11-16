"""tests/test_skill_library_web_guidance.py - the vendored modern web guidance.

`skills/guidance/modern-web-guidance/` holds the Chrome team's guides (Apache-2.0) with a local index;
`src/web_guidance.py` searches and reads them, and the same three operations are the `web_guidance` chat
tool, the `web_guidance` built-in MCP server and `/api/web-guidance/*`. The `modern-web-guidance` library
skill tells the model to use them before it writes web UI. No network anywhere in these tests.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from src import skill_library, web_guidance

ROOT = Path(web_guidance.GUIDANCE_DIR)
SKILL_DIR = Path(skill_library.LIBRARY_DIR) / "modern-web-guidance"


# --- the vendored tree ----------------------------------------------------------------------------------------------

def test_the_tree_carries_its_licence_and_provenance():
    source = json.loads((ROOT / "SOURCE.json").read_text(encoding="utf-8"))
    assert source["package"] == "modern-web-guidance" and source["license"] == "Apache-2.0"
    assert source["version"] and source["skill_version"] and source["tarball_sha256"]
    licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in licence and "Version 2.0" in licence


def test_the_index_and_the_guide_files_describe_the_same_guides():
    index = json.loads((ROOT / "index.json").read_text(encoding="utf-8"))
    on_disk = {p.relative_to(ROOT / "guides").as_posix() for p in (ROOT / "guides").rglob("*.md")}
    assert len(index) >= 200
    assert {row["path"] for row in index} == on_disk
    assert len({row["id"] for row in index}) == len(index), "a guide id must be unique across categories"
    for row in index:
        assert row["description"].strip() and row["category"] and row["title"]
        assert row["path"] == f"{row['category']}/{row['id']}.md"


def test_the_skill_reference_index_is_the_one_the_update_script_would_write():
    index = json.loads((ROOT / "index.json").read_text(encoding="utf-8"))
    written = (SKILL_DIR / "references" / "guide-index.md").read_text(encoding="utf-8")
    assert written == web_guidance.guide_index_markdown(index)


def test_status_reports_what_is_vendored():
    status = web_guidance.status()
    assert status["available"] and status["guides"] >= 200 and status["license"] == "Apache-2.0"
    assert status["categories"]["ui-components"] >= 20


# --- search ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("query, wanted", [
    ("light dismiss dialog", "light-dismiss-a-dialog"),
    ("responsive data table", "responsive-table"),
    ("animated modal dialog", "dialog"),
    ("dark mode toggle", "dark-mode-toggle"),
    ("tooltip anchored to a button", "position-aware-tooltips"),
    ("sanitize untrusted html", "sanitize-untrusted-html"),
    ("passkey sign in", "passkeys"),
    ("fix slow interaction INP", "identify-inp-causes"),
    ("copy to clipboard button", "copy-button"),
    ("persistent toast notification", "persistent-toast-notifications"),
    ("smooth page transition between pages", "cross-document-transitions"),
])
def test_search_finds_the_guide_for_a_capability(query, wanted):
    ids = [r["id"] for r in web_guidance.search(query, limit=5)["results"]]
    assert wanted in ids, (query, ids)


def test_search_knows_the_words_people_use_for_a_ui_primitive():
    assert "dialog" in [r["id"] for r in web_guidance.search("modal", limit=6)["results"]]
    assert "declarative-dialog-popover-control" in [r["id"] for r in web_guidance.search("popup", limit=8)["results"]]
    assert "viewport-aware-dropdowns" in [r["id"] for r in web_guidance.search("dropdown", limit=6)["results"]]


def test_search_exact_id_wins_and_category_filters_apply():
    assert web_guidance.search("dialog")["results"][0]["id"] == "dialog"
    forms = web_guidance.search("validation", limit=30, category="forms")["results"]
    assert forms and {r["category"] for r in forms} == {"forms"}


def test_search_without_a_query_or_a_match_says_so_instead_of_guessing():
    assert web_guidance.search("   ")["results"] == []
    nothing = web_guidance.search("zzzzqqqq xxyyzz")
    assert nothing["results"] == [] and "rephrase" in nothing["note"]
    assert web_guidance.search("dialog", limit="many")["results"]  # a bad limit falls back to the default


# --- retrieve -------------------------------------------------------------------------------------------------------

def test_retrieve_returns_the_text_and_points_cross_references_at_this_tool():
    got = web_guidance.retrieve("dialog,css")
    assert [g["id"] for g in got["guides"]] == ["dialog", "css"]
    for g in got["guides"]:
        assert g["text"].strip() and "npx" not in g["text"] and "modern-web-guidance@latest" not in g["text"]
    raw = (ROOT / "guides" / "ui-components" / "app-menus.md").read_text(encoding="utf-8")
    assert "npx -y modern-web-guidance@latest retrieve" in raw
    assert 'web_guidance action=retrieve ids="menu"' in web_guidance.retrieve(["app-menus"])["guides"][0]["text"]


def test_a_long_guide_comes_back_in_slices_that_add_up_to_the_whole():
    whole = web_guidance.retrieve(["accessibility"], max_chars=web_guidance.MAX_CHARS_CAP)["guides"][0]
    assert whole["chars"] > 40_000
    text, offset, rounds = "", 0, 0
    while True:
        piece = web_guidance.retrieve(["accessibility"], offset=offset, max_chars=20_000)["guides"][0]
        text += piece["text"]
        rounds += 1
        if not piece["truncated"]:
            break
        offset = piece["next_offset"]
    assert rounds >= 3 and len(text) == whole["chars"]
    assert text.startswith(web_guidance.retrieve(["accessibility"], max_chars=2_000)["guides"][0]["text"])


def test_an_unknown_or_hostile_id_is_reported_as_missing_with_the_closest_guides():
    got = web_guidance.retrieve(["dialoog", "../../etc/passwd", "dialog"])
    assert [g["id"] for g in got["guides"]] == ["dialog"]
    missing = {m["id"]: m["closest"] for m in got["missing"]}
    assert set(missing) == {"dialoog", "../../etc/passwd"}      # only ids in the index ever reach the filesystem
    assert "dialog" in missing["dialoog"]
    assert web_guidance.retrieve([])["guides"] == []


def test_render_gives_the_model_ids_with_descriptions_and_a_continuation_hint():
    found = web_guidance.render("search", web_guidance.search("light dismiss dialog", limit=3))
    assert "light-dismiss-a-dialog [ui-behaviors] - " in found and "action=retrieve" in found
    cut = web_guidance.render("retrieve", web_guidance.retrieve(["accessibility"], max_chars=2_000))
    assert "continue with offset=2000" in cut
    assert "no guide 'nope'" in web_guidance.render("retrieve", web_guidance.retrieve(["nope"]))
    assert web_guidance.render("list", web_guidance.list_guides("wasm")).startswith("cpp-on-the-web [wasm]")


# --- the three ways in ----------------------------------------------------------------------------------------------

def test_the_chat_tool_searches_reads_and_lists():
    from src.agent_tools import TOOL_HANDLERS
    run = lambda args: asyncio.run(TOOL_HANDLERS["web_guidance"](args, {}))  # noqa: E731
    hit = run({"action": "search", "query": "light dismiss dialog"})
    assert hit["exit_code"] == 0 and "light-dismiss-a-dialog" in hit["output"]
    read = run({"ids": "dialog"})                       # ids alone mean retrieve
    assert read["exit_code"] == 0 and "=== dialog (ui-components) ===" in read["output"]
    assert run('{"action": "list", "category": "wasm"}')["result"]["vendored"]["available"]
    assert run("responsive data table")["exit_code"] == 0   # a bare string is a query
    assert run({"action": "search"})["exit_code"] == 1 and run({"action": "retrieve"})["exit_code"] == 1
    assert run({"action": "frobnicate"})["error_class"] == "web_guidance.bad_args"


def test_the_tool_is_declared_indexed_and_has_no_twin_noise():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.builtin_mcp import _BUILTIN_SERVERS, NATIVE_TWIN_SERVERS, is_native_twin
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS as TOOL_SCHEMAS
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert "web_guidance" in names and "web_guidance" in TOOL_HANDLERS and "web_guidance" in TOOL_TAGS
    assert BUILTIN_TOOL_DESCRIPTIONS["web_guidance"] and len(EXAMPLES["web_guidance"]) >= 3
    assert "web_guidance" in _BUILTIN_SERVERS and "web_guidance" in NATIVE_TWIN_SERVERS
    assert is_native_twin("mcp__web_guidance__web_guidance_search")


def test_the_mcp_server_serves_the_same_operations_without_the_app_running():
    from mcp_servers import web_guidance_server as srv
    assert [t.name for t in asyncio.run(srv.list_tools())] == ["web_guidance_search", "web_guidance_retrieve", "web_guidance_list"]

    def call(name, args):
        result = asyncio.run(srv.call_tool(name, args))
        assert not result.isError
        return json.loads(result.content[0].text)

    assert call("web_guidance_search", {"query": "light dismiss dialog"})["results"][0]["id"] == "light-dismiss-a-dialog"
    assert call("web_guidance_retrieve", {"ids": "dialog"})["guides"][0]["id"] == "dialog"
    listed = call("web_guidance_list", {"category": "wasm"})
    assert listed["count"] == 1 and listed["vendored"]["version"]
    assert asyncio.run(srv.call_tool("nope", {})).isError


def test_the_routes_serve_the_same_operations():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.web_guidance_routes import setup_web_guidance_routes
    app = FastAPI()
    app.include_router(setup_web_guidance_routes())
    client = TestClient(app)
    assert client.get("/api/web-guidance/status").json()["guides"] >= 200
    assert client.get("/api/web-guidance/list", params={"category": "wasm"}).json()["count"] == 1
    hit = client.get("/api/web-guidance/search", params={"q": "responsive data table"}).json()
    assert hit["results"][0]["id"] == "responsive-table"
    one = client.get("/api/web-guidance/guide/dialog").json()
    assert one["id"] == "dialog" and one["text"]
    cut = client.get("/api/web-guidance/guide/accessibility", params={"max_chars": 2000}).json()
    assert cut["truncated"] and cut["next_offset"] == 2000
    missing = client.get("/api/web-guidance/guide/nope-nope")
    assert missing.status_code == 404 and missing.json()["detail"]["id"] == "nope-nope"


# --- the update script ----------------------------------------------------------------------------------------------

def _tarball(version="9.9.9", extra_guide=True) -> bytes:
    prefix = "package/skills/modern-web-guidance/"
    files = {
        "package/LICENSE": b"Apache License\nVersion 2.0\n",
        "package/package.json": json.dumps({"name": "modern-web-guidance", "version": version}).encode(),
        prefix + "SKILL.md": b"npx -y modern-web-guidance@latest search \"q\" --skill-version 2030_01_01-abc123\n",
        prefix + "gen/guides.json": json.dumps([
            {"id": "alpha", "description": "Alpha guide.", "category": "css", "featuresUsed": ["f"], "tokenCount": 7}]).encode(),
        prefix + "guides/css/alpha.md": b"# Alpha\r\n\r\nFirst paragraph of alpha.\r\n\r\n## Part\r\n",
        prefix + "gen/query-words.i8": b"\x00" * 10,           # the vector search is not kept
        "package/skills/chrome-extensions/SKILL.md": b"not ours",
        "../escape.md": b"never extracted",
    }
    if extra_guide:
        files[prefix + "guides/html/beta.md"] = b"# Beta guide\n\nBeta does things.\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def test_the_update_script_rebuilds_the_tree_from_a_package_tarball(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("update_web_guidance", Path(__file__).resolve().parents[1] / "scripts" / "update_web_guidance.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    dest = tmp_path / "skills" / "guidance" / "modern-web-guidance"
    reference = tmp_path / "skills" / "library" / "modern-web-guidance" / "references" / "guide-index.md"
    monkeypatch.setattr(web_guidance, "GUIDANCE_DIR", str(dest))
    monkeypatch.setattr(script, "SKILL_INDEX", reference)
    tarball = tmp_path / "pkg.tgz"
    tarball.write_bytes(_tarball())

    assert script.main(["--tarball", str(tarball)]) == 0
    source = json.loads((dest / "SOURCE.json").read_text(encoding="utf-8"))
    assert source["version"] == "9.9.9" and source["skill_version"] == "2030_01_01-abc123" and source["guides"] == 2
    assert sorted(p.name for p in (dest / "guides").rglob("*.md")) == ["alpha.md", "beta.md"]
    assert not (dest.parent / "escape.md").exists() and not list(dest.rglob("*.i8"))
    assert b"\r" not in (dest / "guides" / "css" / "alpha.md").read_bytes()
    index = {r["id"]: r for r in json.loads((dest / "index.json").read_text(encoding="utf-8"))}
    assert index["alpha"]["description"] == "Alpha guide." and index["alpha"]["headings"] == ["Part"]
    assert index["beta"]["description"] == "Beta does things." and index["beta"]["title"] == "Beta guide"
    assert "`alpha`: Alpha guide." in reference.read_text(encoding="utf-8")
    assert web_guidance.search("alpha")["results"][0]["id"] == "alpha"

    tarball.write_bytes(_tarball(version="10.0.0", extra_guide=False))
    assert script.main(["--tarball", str(tarball)]) == 0
    assert [p.name for p in (dest / "guides").rglob("*.md")] == ["alpha.md"]      # a refresh replaces, never accumulates
    assert not [p for p in dest.parent.iterdir() if p.name.startswith(".web-guidance-")]


def test_the_update_script_refuses_a_tarball_without_guides_and_leaves_the_tree_alone(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("update_web_guidance", Path(__file__).resolve().parents[1] / "scripts" / "update_web_guidance.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    dest = tmp_path / "tree"
    dest.mkdir()
    (dest / "keep.txt").write_text("kept", encoding="utf-8")
    monkeypatch.setattr(web_guidance, "GUIDANCE_DIR", str(dest))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("package/LICENSE")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    bad = tmp_path / "bad.tgz"
    bad.write_bytes(buf.getvalue())
    with pytest.raises(SystemExit):
        script.main(["--tarball", str(bad)])
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "kept"


# --- the library skill ----------------------------------------------------------------------------------------------

def test_the_library_skill_is_installable_exportable_and_tells_the_model_to_use_the_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_library, "DATA_DIR", str(tmp_path), raising=False)
    result = skill_library.install("alice", ["modern-web-guidance"])
    assert result["results"][0]["status"] == "installed"
    installed = next(s for s in skill_library._skills_manager().load(owner="alice") if "library:modern-web-guidance" in s["tags"])
    folder = Path(installed["path"]).parent
    assert (folder / "references" / "guide-index.md").is_file()
    body = Path(installed["path"]).read_text(encoding="utf-8")
    assert "web_guidance" in body and "npx" not in body

    from src import instruction_portability
    bundle = instruction_portability.preview_bundle([installed], [], [], "claude")
    paths = {f["path"] for f in bundle["files"]}
    assert ".claude/skills/modern-web-guidance/SKILL.md" in paths
    assert ".claude/skills/modern-web-guidance/references/guide-index.md" in paths


def test_the_selector_offers_the_skill_for_web_ui_work_and_not_for_backend_work(tmp_path, monkeypatch):
    from src.skills_runtime import selector
    monkeypatch.setattr(skill_library, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(selector, "_embedder_failed", True)     # lexical and trigger lanes only: no model download
    skill_library.install("alice", ["modern-web-guidance"])
    mgr = skill_library._skills_manager()
    skills = mgr.load(owner="alice")

    def picked(prompt):
        return [s["name"] for s in selector.select(mgr, "alice", prompt, skills=skills, threshold=None, max_items=5, min_confidence=0.0)]

    for prompt in ("add an animated modal dialog to the settings page",
                   "hazme un diálogo modal animado para confirmar el borrado",
                   "build a tooltip anchored to a button with CSS",
                   "add dark mode toggle to the site",
                   "create a signup form with validation and autofill",
                   "añade una animación de entrada a la tarjeta con CSS"):
        assert "modern-web-guidance" in picked(prompt), prompt
    for prompt in ("write a SQL migration to add a column", "refactor the python backend module",
                   "summarise this meeting transcript"):
        assert "modern-web-guidance" not in picked(prompt), prompt
