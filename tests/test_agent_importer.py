"""src/agent_importer.py: Claude Code / Codex CLI / Cursor setups into Faustus."""
import asyncio
import json
from pathlib import Path

import pytest

from src import agent_importer as ai


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    c = h / ".claude"
    _write(c / "CLAUDE.md", "# Reglas\nUsa español.\n\n## Git\nSin push.\n\n## Tests\nPrueba antes.\n")
    _write(c / "projects" / "E--repo" / "memory" / "MEMORY.md", "- [x](x.md)\n")
    _write(c / "projects" / "E--repo" / "memory" / "user_role.md",
           "---\nname: user-role\ndescription: ExampleUser es desarrollador\n---\nPrefiere respuestas cortas.\n")
    _write(c / "skills" / "tdd" / "SKILL.md", "---\nname: tdd\ndescription: Test first\n---\nWrite the test.\n")
    _write(c / "skills" / "tdd" / "references" / "x.md", "ref")
    _write(c / "agents" / "reviewer.md", "---\nname: reviewer\ndescription: Reviews code\ntools: Read, Grep\nmodel: sonnet\n---\nYou review diffs.\n")
    _write(c / "commands" / "deploy.md", "deploy")
    _write(c / "settings.json", json.dumps({"mcpServers": {"dup": {"command": "y"}}}))
    _write(h / ".claude.json", json.dumps({"mcpServers": {
        "fs": {"command": "npx", "args": ["-y", "server-fs"], "env": {"TOKEN": "abc"}},
        "remote": {"type": "sse", "url": "https://x/sse", "headers": {"A": "b"}},
        "dup": {"command": "x"}}}))
    x = h / ".codex"
    _write(x / "AGENTS.md", "Siempre tests.\n")
    _write(x / "config.toml", '[mcp_servers.docs]\ncommand = "uvx"\nargs = ["docs-mcp"]\n\n[mcp_servers.web]\nurl = "https://w/mcp"\n')
    _write(x / "memories" / "note.md", "Recuerda el puerto 7000.\n")
    _write(x / "skills" / ".system" / "builtin" / "SKILL.md", "---\nname: builtin\n---\nx")
    u = h / ".cursor"
    _write(u / "rules" / "style.mdc", "---\nalwaysApply: true\n---\nCódigo en inglés.\n")
    _write(u / "mcp.json", json.dumps({"mcpServers": {"cur": {"url": "https://c/mcp"}}}))
    return h


def _items(res):
    return {f"{i.kind}:{i.name}": i for i in res["items"]}


def test_scan_claude_code(home):
    res = ai.scan("claude-code", home=str(home))
    items = _items(res)
    assert set(items) == {"instructions:CLAUDE.md", "memory:E--repo/user_role", "mcp:fs", "mcp:remote", "mcp:dup",
                          "skill:tdd", "agent:reviewer"}
    assert len(items["instructions:CLAUDE.md"].payload["texts"]) == 3
    assert items["instructions:CLAUDE.md"].payload["texts"][1].startswith("[Claude Code] ## Git")
    assert "ExampleUser es desarrollador" in items["memory:E--repo/user_role"].payload["text"]
    assert items["mcp:fs"].payload == {"name": "fs", "transport": "stdio", "command": "npx", "args": ["-y", "server-fs"], "env": {"TOKEN": "abc"}}
    assert items["mcp:remote"].payload["transport"] == "sse" and items["mcp:remote"].warnings
    assert items["mcp:dup"].payload["command"] == "x"                 # ~/.claude.json wins over settings.json
    assert set(items["skill:tdd"].payload["files"]) == {"SKILL.md", "references/x.md"}
    assert len(items["agent:reviewer"].warnings) == 2
    assert any("commands" in s for s in res["skipped"])


def test_scan_codex_and_cursor(home):
    codex = _items(ai.scan("codex", home=str(home)))
    assert set(codex) == {"instructions:AGENTS.md", "memory:note", "mcp:docs", "mcp:web"}
    assert codex["mcp:web"].payload == {"name": "web", "transport": "http", "url": "https://w/mcp", "env": {}}
    cursor = _items(ai.scan("cursor", home=str(home)))
    assert set(cursor) == {"instructions:rules/style.mdc", "mcp:cur"}
    assert cursor["instructions:rules/style.mdc"].payload["texts"] == ["[Cursor · regla style] Código en inglés."]


def test_missing_agent_folder_is_not_an_error(tmp_path):
    assert ai.scan("cursor", home=str(tmp_path))["found"] is False


class FakeTarget:
    def __init__(self):
        self.memories, self.mcp, self.skills, self.agents = {}, {}, {}, {}
        self.n = 0

    def _id(self):
        self.n += 1
        return f"id{self.n}"

    def add_memory(self, text, pinned, source):
        i = self._id()
        self.memories[i] = (text, pinned, source)
        return i

    def remove_memory(self, i):
        self.memories.pop(i, None)

    async def add_mcp(self, spec):
        i = self._id()
        self.mcp[i] = spec
        return i

    async def remove_mcp(self, i):
        self.mcp.pop(i, None)

    def add_skill(self, files, source):
        self.skills[self._id()] = files
        return list(self.skills)[-1]

    def remove_skill(self, name):
        self.skills.pop(name, None)

    def write_agent(self, slug, text):
        self.agents[slug] = text
        return slug

    def remove_agent(self, slug):
        self.agents.pop(slug, None)


def test_preview_apply_and_resync(home, tmp_path):
    mf = str(tmp_path / "manifest.json")
    target = FakeTarget()
    pv = ai.preview(["claude-code"], home=str(home), existing={"mcp": {"fs"}}, manifest=ai.load_manifest(mf))
    status = {i.key: i.status for i in pv["_items"]}
    assert status["claude-code:mcp:fs"] == "exists" and status["claude-code:skill:tdd"] == "new"

    res = asyncio.run(ai.apply(pv["_items"], target, manifest_file=mf))
    assert "claude-code:mcp:fs" not in {d["key"] for d in res["imported"]}        # an existing name is left alone
    assert len(res["imported"]) == 6 and not res["failed"]
    pinned = [m for m in target.memories.values() if m[1]]
    assert len(pinned) == 3 and all(m[2] == "import:claude-code" for m in pinned)
    assert "reviewer" in target.agents and target.agents["reviewer"].startswith("---\n")

    pv2 = ai.preview(["claude-code"], home=str(home), manifest=ai.load_manifest(mf))
    assert {i.status for i in pv2["_items"] if i.key != "claude-code:mcp:fs"} == {"unchanged"}

    _write(home / ".claude" / "CLAUDE.md", "# Reglas\nUsa español siempre.\n")
    before = set(target.memories)
    out = asyncio.run(ai.resync(target, home=str(home), existing={"mcp": {"fs"}}, manifest_file=mf))
    assert [d["key"] for d in out["imported"]] == ["claude-code:instructions:CLAUDE.md"]
    assert out["imported"][0]["replaced"] is True
    texts = [m[0] for m in target.memories.values() if m[1]]
    assert texts == ["[Claude Code] # Reglas\nUsa español siempre."]
    assert len(set(target.memories) - before) == 1


def test_apply_reports_a_failure_and_continues(home, tmp_path):
    class Broken(FakeTarget):
        async def add_mcp(self, spec):
            raise RuntimeError("refused")
    pv = ai.preview(["codex"], home=str(home), manifest=ai.load_manifest(str(tmp_path / "m.json")))
    res = asyncio.run(ai.apply(pv["_items"], Broken(), manifest_file=str(tmp_path / "m.json")))
    assert {f["key"] for f in res["failed"]} == {"codex:mcp:docs", "codex:mcp:web"}
    assert {d["key"] for d in res["imported"]} == {"codex:instructions:AGENTS.md", "codex:memory:note"}


def test_agent_markdown_loads_as_a_faustus_agent_definition(home):
    from src import agent_defs
    item = next(i for i in ai.scan("claude-code", home=str(home))["items"] if i.kind == "agent")
    text = ai.agent_markdown(item.payload, "Claude Code")
    d = agent_defs.parse(text, slug="reviewer")
    assert d.description == "Reviews code" and "You review diffs." in text
