"""A page snapshot the browser saved to a file is put back inline.

Seen live (22-10): recent @playwright/mcp builds answer navigate / snapshot
with "- [Snapshot](.playwright-mcp\\page-....yml)"; the model cannot open the
file, so every browser read came back blind.
"""
import os

from src import browser_snapshot_file as bsf
from src.mcp_manager import McpManager


def _snap(tmp_path, name="page-1.yml", body='- heading "Minecraft Dungeons II" [level=1] [ref=e5]\n- text: All-time peak 32,556\n'):
    folder = tmp_path / ".playwright-mcp"
    folder.mkdir(exist_ok=True)
    f = folder / name
    f.write_text(body, encoding="utf-8")
    return f


def test_a_linked_snapshot_is_inlined(tmp_path, monkeypatch):
    _snap(tmp_path)
    monkeypatch.chdir(tmp_path)
    text = "### Page\n- Page URL: https://x.test/\n### Snapshot\n- [Snapshot](.playwright-mcp\\page-1.yml)\n### Events\n- none"
    out = bsf.inline_snapshot_links(text)
    assert "32,556" in out and "[ref=e5]" in out
    assert "### Page snapshot\n```yaml" in out and "[Snapshot](" not in out
    assert out.endswith("### Events\n- none")


def test_forward_slashes_and_absolute_paths_resolve(tmp_path, monkeypatch):
    f = _snap(tmp_path, "page-2.yml")
    monkeypatch.chdir(tmp_path)
    assert "32,556" in bsf.inline_snapshot_links("- [Snapshot](.playwright-mcp/page-2.yml)")
    assert "32,556" in bsf.inline_snapshot_links(f"- [Snapshot]({f})")


def test_a_link_outside_the_snapshot_folder_is_never_read(tmp_path, monkeypatch):
    secret = tmp_path / "secret.yml"
    secret.write_text("password: hunter2", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for link in ("secret.yml", str(secret), ".playwright-mcp/../secret.yml"):
        out = bsf.inline_snapshot_links(f"- [Snapshot]({link})")
        assert "hunter2" not in out and "[Snapshot](" in out


def test_a_missing_file_leaves_the_link(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    text = "- [Snapshot](.playwright-mcp/nope.yml)"
    assert bsf.inline_snapshot_links(text) == text


def test_an_empty_snapshot_says_so(tmp_path, monkeypatch):
    _snap(tmp_path, "page-3.yml", body="")
    monkeypatch.chdir(tmp_path)
    assert "no accessible content" in bsf.inline_snapshot_links("- [Snapshot](.playwright-mcp/page-3.yml)")


def test_the_manager_inlines_browser_results(tmp_path, monkeypatch):
    _snap(tmp_path, "page-4.yml")
    monkeypatch.chdir(tmp_path)
    for tool in ("browser_navigate", "browser_wait_for"):
        res = McpManager._postprocess_browser_result(
            tool, {"stdout": "### Snapshot\n- [Snapshot](.playwright-mcp/page-4.yml)", "exit_code": 0},
            server_id="builtin_browser")
        assert "32,556" in res["stdout"], tool
