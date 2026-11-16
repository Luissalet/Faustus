"""agent_importer.py — bring another coding agent's setup into Faustus.

Reads what Claude Code (``~/.claude``, ``~/.claude.json``), Codex CLI
(``~/.codex``) and Cursor (``~/.cursor``) keep for their user, shows it item by
item, and imports what is chosen:

    Claude Code   CLAUDE.md                        -> pinned memories (one per section)
                  projects/*/memory/*.md           -> memories
                  ~/.claude.json, settings.json    -> MCP servers   (mcpServers)
                  skills/<name>/SKILL.md (+ files) -> skills
                  agents/*.md                      -> agent definitions (DATA_DIR/agents)
    Codex CLI     AGENTS.md                        -> pinned memories
                  memories/*.md                    -> memories
                  config.toml [mcp_servers.*]      -> MCP servers
                  skills/<name>/SKILL.md           -> skills
    Cursor        AGENTS.md, rules/**/*.md(c)      -> pinned memories
                  mcp.json                         -> MCP servers
                  skills/<name>/SKILL.md           -> skills

Project files (a repository's AGENTS.md / CLAUDE.md / .cursorrules) are not
imported: Faustus already reads them from the workspace on every turn
(src/project_instructions.py, src/project_rules.py).

Nothing is written by :func:`preview`. :func:`apply` writes through a
:class:`Target` (the app's memory store, MCP servers, skill library and agent
definitions — see routes/agent_import_routes.py) and records each imported item
in a manifest (``DATA_DIR/agent_import.json``) with the hash of its source.
:func:`resync` re-reads every source imported before and re-imports what changed
there since (and what is new), replacing what the earlier import created.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol

AGENTS = ("claude-code", "codex", "cursor")
LABEL = {"claude-code": "Claude Code", "codex": "Codex CLI", "cursor": "Cursor"}
KINDS = ("instructions", "memory", "mcp", "skill", "agent")
_MAX_FILE = 256_000
_MAX_SKILL_FILES = 40
_TEXT_EXT = {".md", ".mdc", ".txt", ".py", ".js", ".mjs", ".ts", ".sh", ".ps1", ".json", ".yaml", ".yml", ".toml",
             ".html", ".css", ".csv", ".xml"}


def default_root(agent: str, home: Optional[str] = None) -> str:
    home = home or os.path.expanduser("~")
    return os.path.join(home, {"claude-code": ".claude", "codex": ".codex", "cursor": ".cursor"}[agent])


@dataclass
class Item:
    agent: str
    kind: str
    name: str
    source: str
    payload: Dict[str, Any]
    summary: str = ""
    sha: str = ""
    status: str = "new"         # new | changed | unchanged | exists
    warnings: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.agent}:{self.kind}:{self.name}"

    def public(self) -> Dict[str, Any]:
        return {"key": self.key, "agent": self.agent, "kind": self.kind, "name": self.name,
                "source": self.source, "summary": self.summary, "status": self.status,
                "warnings": list(self.warnings)}


def _sha(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _read(path: Path) -> Optional[str]:
    try:
        if path.stat().st_size > _MAX_FILE:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _strip_frontmatter(text: str) -> tuple:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            head = text[3:end]
            meta = {}
            for line in head.splitlines():
                k, sep, v = line.partition(":")
                if sep and k.strip() and not k.startswith((" ", "\t")):
                    meta[k.strip()] = v.strip().strip('"\'')
            return meta, text[end + 4:].lstrip("\n")
    return {}, text


def sections(text: str, label: str) -> List[str]:
    """Split instructions at their ``## `` headings; each part becomes one memory
    that starts with where it came from. Empty parts are dropped."""
    parts, cur = [], []
    for line in (text or "").splitlines():
        if line.startswith("## ") and cur:
            parts.append("\n".join(cur).strip())
            cur = []
        cur.append(line)
    if cur:
        parts.append("\n".join(cur).strip())
    return [f"[{label}] {p}" for p in parts if p and p.strip("# \n")]


# --------------------------------------------------------------------------- scanning

def _instructions(agent: str, path: Path, name: str, label: str) -> Optional[Item]:
    text = _read(path) if path.is_file() else None
    if not text or not text.strip():
        return None
    meta, body = _strip_frontmatter(text)
    parts = sections(body, label)
    if not parts:
        return None
    return Item(agent, "instructions", name, str(path), {"texts": parts},
                summary=f"{len(parts)} sección(es), {len(body)} caracteres")


def _memory_files(agent: str, folder: Path, prefix: str, label: str) -> List[Item]:
    items = []
    if not folder.is_dir():
        return items
    for path in sorted(folder.glob("*.md")):
        if path.name.upper() == "MEMORY.MD":       # an index of the others, not a memory
            continue
        text = _read(path)
        if not text or not text.strip():
            continue
        meta, body = _strip_frontmatter(text)
        title = meta.get("name") or path.stem
        desc = meta.get("description") or ""
        mem = f"[{label}] {title}" + (f" — {desc}" if desc else "") + "\n" + body.strip()
        items.append(Item(agent, "memory", f"{prefix}{path.stem}", str(path), {"text": mem.strip()},
                          summary=(desc or body.strip().splitlines()[0] if body.strip() else title)[:160]))
    return items


def _mcp_items(agent: str, servers: Any, source: str) -> List[Item]:
    items = []
    if not isinstance(servers, dict):
        return items
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        out: Dict[str, Any] = {"name": str(name)}
        url = spec.get("url") or spec.get("httpUrl") or spec.get("serverUrl")
        if url:
            kind = str(spec.get("type") or spec.get("transport") or "").lower()
            out.update(transport="sse" if kind == "sse" else "http", url=str(url))
        elif spec.get("command"):
            out.update(transport="stdio", command=str(spec["command"]),
                       args=[str(a) for a in (spec.get("args") or [])])
        else:
            continue
        env = spec.get("env") or {}
        out["env"] = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
        headers = spec.get("headers") or spec.get("http_headers") or {}
        warn = []
        if headers:
            warn.append("cabeceras HTTP no importadas: configúralas en el servidor MCP de Faustus")
        if spec.get("disabled") is True or spec.get("enabled") is False:
            out["disabled"] = True
        what = out.get("url") or " ".join([out.get("command", "")] + out.get("args", []))
        items.append(Item(agent, "mcp", str(name), source, out,
                          summary=f"{out['transport']}: {what[:140]}" + (f" · {len(out['env'])} variable(s)" if out["env"] else ""),
                          warnings=warn))
    return items


def _skills(agent: str, folder: Path) -> List[Item]:
    items = []
    if not folder.is_dir():
        return items
    for skill_md in sorted(folder.glob("*/SKILL.md")):
        d = skill_md.parent
        if d.name.startswith("."):
            continue
        files: Dict[str, str] = {}
        for p in sorted(d.rglob("*")):
            if len(files) >= _MAX_SKILL_FILES:
                break
            if p.is_file() and p.suffix.lower() in _TEXT_EXT and not any(part.startswith(".") for part in p.relative_to(d).parts):
                text = _read(p)
                if text is not None:
                    files[p.relative_to(d).as_posix()] = text
        if "SKILL.md" not in files:
            continue
        meta, _ = _strip_frontmatter(files["SKILL.md"])
        items.append(Item(agent, "skill", d.name, str(d), {"files": files},
                          summary=(meta.get("description") or "")[:160] + f" ({len(files)} fichero(s))"))
    return items


def _agents(agent: str, folder: Path) -> List[Item]:
    items = []
    if not folder.is_dir():
        return items
    for path in sorted(folder.glob("*.md")):
        text = _read(path)
        if not text:
            continue
        meta, body = _strip_frontmatter(text)
        if not body.strip():
            continue
        warn = []
        if meta.get("tools"):
            warn.append("su lista de herramientas no se traslada (otros nombres): el agente usará las de Faustus")
        if meta.get("model"):
            warn.append(f"modelo `{meta['model']}` no trasladado: el agente usará el del coordinador")
        name = meta.get("name") or path.stem
        items.append(Item(agent, "agent", name, str(path),
                          {"name": name, "description": meta.get("description") or "", "body": body.strip()},
                          summary=(meta.get("description") or "")[:160], warnings=warn))
    return items


def _toml(path: Path) -> Dict[str, Any]:
    try:
        import tomllib
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable config imports nothing from it
        return {}


def _json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def scan(agent: str, root: Optional[str] = None, *, home: Optional[str] = None) -> Dict[str, Any]:
    """Everything importable from one agent, plus what was seen and left out."""
    if agent not in AGENTS:
        raise ValueError(f"unknown agent {agent!r}: one of {', '.join(AGENTS)}")
    home = home or os.path.expanduser("~")
    base = Path(root or default_root(agent, home))
    label = LABEL[agent]
    items: List[Item] = []
    skipped: List[str] = []
    if not base.is_dir():
        return {"agent": agent, "root": str(base), "found": False, "items": [], "skipped": []}
    if agent == "claude-code":
        it = _instructions(agent, base / "CLAUDE.md", "CLAUDE.md", label)
        items += [it] if it else []
        projects = base / "projects"
        if projects.is_dir():
            for proj in sorted(p for p in projects.iterdir() if p.is_dir()):
                items += _memory_files(agent, proj / "memory", f"{proj.name}/", f"{label} · {proj.name}")
        seen = set()
        for src in (Path(home) / ".claude.json", base / "settings.json"):
            if src.is_file():
                for m in _mcp_items(agent, _json(src).get("mcpServers"), str(src)):
                    if m.name not in seen:
                        seen.add(m.name)
                        items.append(m)
        items += _skills(agent, base / "skills")
        items += _agents(agent, base / "agents")
        cmds = list((base / "commands").glob("*.md")) if (base / "commands").is_dir() else []
        if cmds:
            skipped.append(f"{len(cmds)} comando(s) de barra en commands/: sin equivalente directo en Faustus")
    elif agent == "codex":
        it = _instructions(agent, base / "AGENTS.md", "AGENTS.md", label)
        items += [it] if it else []
        items += _memory_files(agent, base / "memories", "", label)
        if (base / "config.toml").is_file():
            items += _mcp_items(agent, _toml(base / "config.toml").get("mcp_servers"), str(base / "config.toml"))
        items += _skills(agent, base / "skills")
    else:
        it = _instructions(agent, base / "AGENTS.md", "AGENTS.md", label)
        items += [it] if it else []
        rules = base / "rules"
        if rules.is_dir():
            for path in sorted([*rules.rglob("*.md"), *rules.rglob("*.mdc")]):
                rel = path.relative_to(rules).as_posix()
                rit = _instructions(agent, path, f"rules/{rel}", f"{label} · regla {path.stem}")
                items += [rit] if rit else []
        if (base / "mcp.json").is_file():
            items += _mcp_items(agent, _json(base / "mcp.json").get("mcpServers"), str(base / "mcp.json"))
        items += _skills(agent, base / "skills")
    for it in items:
        it.sha = _sha(it.payload)
    return {"agent": agent, "root": str(base), "found": True, "items": items, "skipped": skipped}


# --------------------------------------------------------------------------- manifest

def manifest_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "agent_import.json")


def load_manifest(path: Optional[str] = None) -> Dict[str, Any]:
    try:
        with open(path or manifest_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("sources", {})
            data.setdefault("items", {})
            return data
    except (OSError, ValueError):
        pass
    return {"sources": {}, "items": {}}


def save_manifest(data: Dict[str, Any], path: Optional[str] = None) -> None:
    from core.atomic_io import atomic_write_text
    target = path or manifest_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    atomic_write_text(target, json.dumps(data, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- preview / apply

def preview(agents: Iterable[str], roots: Optional[Dict[str, str]] = None, *, home: Optional[str] = None,
            existing: Optional[Dict[str, set]] = None, manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Scan, then mark each item new / changed / unchanged against the manifest, or
    ``exists`` when Faustus already has an MCP server or skill of that name that an
    import did not create. Writes nothing."""
    manifest = manifest if manifest is not None else load_manifest()
    existing = existing or {}
    out = []
    for agent in agents:
        res = scan(agent, (roots or {}).get(agent) or manifest["sources"].get(agent), home=home)
        for it in res["items"]:
            prev = manifest["items"].get(it.key)
            if prev:
                it.status = "unchanged" if prev.get("sha") == it.sha else "changed"
            elif it.kind in ("mcp", "skill") and it.name.lower() in {n.lower() for n in existing.get(it.kind, set())}:
                it.status = "exists"
        out.append(res)
    return {"sources": [{**r, "items": [i.public() for i in r["items"]]} for r in out], "_items": [i for r in out for i in r["items"]]}


class Target(Protocol):
    def add_memory(self, text: str, pinned: bool, source: str) -> str: ...
    def remove_memory(self, memory_id: str) -> None: ...
    async def add_mcp(self, spec: Dict[str, Any]) -> str: ...
    async def remove_mcp(self, server_id: str) -> None: ...
    def add_skill(self, files: Dict[str, str], source: str) -> str: ...
    def remove_skill(self, name: str) -> None: ...
    def write_agent(self, slug: str, text: str) -> str: ...
    def remove_agent(self, slug: str) -> None: ...


def agent_markdown(payload: Dict[str, Any], source_label: str) -> str:
    from services.memory.skill_format import emit_frontmatter
    fm = {"name": payload["name"][:80], "description": (payload.get("description") or f"Importado de {source_label}")[:400]}
    return "---\n" + emit_frontmatter(fm) + "\n---\n\n" + payload["body"].rstrip() + "\n"


async def _remove(target: Target, kind: str, ids: List[str]) -> None:
    for tid in ids:
        try:
            if kind in ("instructions", "memory"):
                target.remove_memory(tid)
            elif kind == "mcp":
                await target.remove_mcp(tid)
            elif kind == "skill":
                target.remove_skill(tid)
            elif kind == "agent":
                target.remove_agent(tid)
        except Exception:  # noqa: BLE001 - already gone is fine
            pass


async def _create(target: Target, it: Item) -> List[str]:
    src = f"import:{it.agent}"
    if it.kind == "instructions":
        return [target.add_memory(t, True, src) for t in it.payload["texts"]]
    if it.kind == "memory":
        return [target.add_memory(it.payload["text"], False, src)]
    if it.kind == "mcp":
        return [await target.add_mcp(it.payload)]
    if it.kind == "skill":
        return [target.add_skill(it.payload["files"], it.source)]
    if it.kind == "agent":
        from src.agent_defs import clean_slug
        slug = clean_slug(it.payload["name"]) or clean_slug(Path(it.source).stem) or "imported-agent"
        return [target.write_agent(slug, agent_markdown(it.payload, LABEL[it.agent]))]
    raise ValueError(f"unknown kind {it.kind}")


async def apply(items: List[Item], target: Target, *, keys: Optional[Iterable[str]] = None,
                manifest_file: Optional[str] = None, roots: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Import ``items`` (only ``keys`` when given, else every new or changed one).
    A changed item replaces what its earlier import created. One failure is reported
    and the rest continue."""
    manifest = load_manifest(manifest_file)
    wanted = set(keys) if keys is not None else None
    done, failed, skipped = [], [], []
    for it in items:
        if wanted is not None:
            if it.key not in wanted:
                continue
        elif it.status not in ("new", "changed"):
            skipped.append(it.key)
            continue
        prev = manifest["items"].get(it.key) or {}
        try:
            if prev.get("targets"):
                await _remove(target, it.kind, list(prev["targets"]))
            ids = await _create(target, it)
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": it.key, "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        manifest["items"][it.key] = {"sha": it.sha, "kind": it.kind, "agent": it.agent, "name": it.name,
                                     "source": it.source, "targets": ids, "at": int(time.time())}
        root = (roots or {}).get(it.agent)
        manifest["sources"].setdefault(it.agent, root or "")
        done.append({"key": it.key, "targets": ids, "replaced": bool(prev.get("targets"))})
        save_manifest(manifest, manifest_file)
    return {"imported": done, "failed": failed, "skipped": skipped}


async def resync(target: Target, *, home: Optional[str] = None, existing: Optional[Dict[str, set]] = None,
                 manifest_file: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Re-read every agent imported before; import what changed or is new there."""
    manifest = load_manifest(manifest_file)
    agents = [a for a in AGENTS if a in manifest["sources"]]
    pv = preview(agents, {a: manifest["sources"][a] for a in agents if manifest["sources"][a]},
                 home=home, existing=existing, manifest=manifest)
    todo = [i for i in pv["_items"] if i.status in ("new", "changed")]
    if dry_run:
        return {"agents": agents, "would_import": [i.public() for i in todo]}
    result = await apply(todo, target, keys=[i.key for i in todo], manifest_file=manifest_file,
                         roots={a: manifest["sources"][a] for a in agents})
    return {"agents": agents, **result}
