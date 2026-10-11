"""Portable instruction bundles; preview first, write only the supplied files."""
from __future__ import annotations

import base64
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

from services.memory.skill_format import emit_body, emit_frontmatter, parse_frontmatter, slugify

TARGETS = ("claude", "codex", "cursor")
_SKILL_KEYS = ("name", "description", "metadata", "license", "allowed-tools")
_CLAUDE_VAR = re.compile(r"\$\{(CLAUDE_[A-Z0-9_]+)\}")


def _portable(text, target, warnings, label):
    text = str(text or "")
    if target == "claude":
        return text
    names = sorted(set(_CLAUDE_VAR.findall(text)))
    if names:
        warnings.append(f"{label}: host-specific variables {', '.join(names)} replaced with portable path hints; check their meaning.")
        text = _CLAUDE_VAR.sub(lambda m: "." if m[1] in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT") else f"<{m[1].lower().replace('claude_', '')}>", text)
    return text


def _markdown(frontmatter, body):
    body = str(body or "")
    return "---\n" + emit_frontmatter(frontmatter) + "\n---\n\n" + body + ("" if body.endswith("\n") else "\n")


def _portable_metadata(value, target, warnings, label):
    if isinstance(value, str):
        return _portable(value, target, warnings, label)
    if isinstance(value, dict):
        return {k: _portable_metadata(v, target, warnings, label) for k, v in value.items()}
    if isinstance(value, list):
        return [_portable_metadata(v, target, warnings, label) for v in value]
    return value


def _skill_parts(item):
    if item.get("markdown"):
        fm, body = parse_frontmatter(item["markdown"])
    else:
        fm = {**(item.get("extra") or {}), **{k: item[k] for k in _SKILL_KEYS if k in item}}
        body = emit_body({k: item.get(k) for k in ("when_to_use", "procedure", "pitfalls", "verification", "body_extra")})
    fm.setdefault("name", item.get("name", "skill"))
    fm.setdefault("description", item.get("description", ""))
    return fm, body


def preview_bundle(skills, rules, agents, target):
    """Convert canonical records without modifying the source or destination.

    Text files have UTF-8 content; binary supporting assets use encoding=base64.
    Host runtime options that cannot be represented are reported as warnings.
    """
    if target not in TARGETS:
        raise ValueError("Unknown export target")
    files, warnings, used = [], [], set()

    def unique(name):
        base = slugify(name, fallback="instruction")[:64]
        result, number = base, 2
        while result in used:
            result = f"{base[:60]}-{number}"; number += 1
        used.add(result)
        if result != base:
            warnings.append(f"{base}: duplicate export name renamed to {result}.")
        return result

    def add(path, content, **extra):
        files.append({"path": path, "content": content, **extra})

    def skill(name, description, body, fm=None, manual=False):
        name = unique(name)
        fm = fm or {}
        metadata = {k: fm[k] for k in _SKILL_KEYS if k in fm}
        metadata.update(name=name, description=str(description or name))
        body = _portable(body, target, warnings, name)
        if target == "cursor":
            cursor = {"description": "" if manual else metadata["description"], "alwaysApply": False, "globs": ""}
            add(f".cursor/rules/{name}.mdc", _markdown(cursor, body))
            return f".cursor/skills/{name}"
        parent = f"{'.claude' if target == 'claude' else '.agents'}/skills/{name}"
        if target == "claude" and manual:
            metadata["disable-model-invocation"] = True
        # Metadata is separate from the Codex appearance/invocation-policy file.
        if target == "codex":
            metadata = _portable_metadata(metadata, target, warnings, name)
        add(f"{parent}/SKILL.md", _markdown(metadata, body))
        if target == "codex":
            interface = {"display_name": metadata["description"], "short_description": metadata["description"], "default_prompt": f"Use ${name} to complete the requested workflow."}
            appearance = {"interface": interface}
            if manual:
                appearance["policy"] = {"allow_implicit_invocation": False}
            add(f"{parent}/agents/openai.yaml", emit_frontmatter(appearance) + "\n")
        return parent

    for item in skills or []:
        fm, body = _skill_parts(item)
        manual = bool(fm.get("manual_only") or fm.get("disable-model-invocation") or (item.get("extra") or {}).get("manual_only") or item.get("manual_only"))
        parent = skill(fm["name"], fm["description"], body, fm, manual)
        source = Path(item["path"]).parent if item.get("path") else None
        if source and source.is_dir():
            asset_parent = parent or f".cursor/skills/{slugify(fm['name'], fallback='skill')}"
            if target == "cursor":
                # Rule files and supporting resources live in different host
                # folders. Resolve Markdown links from the exported rule file.
                file = files[-1]
                file["content"] = re.sub(r"(\]\()(?:(?:\./)?)(scripts|references|assets|templates)/", lambda m: m[1] + "../skills/" + asset_parent.rsplit('/', 1)[-1] + "/" + m[2] + "/", file["content"])
                file["content"] = re.sub(r"(^[ \t]{0,3}\[[^\]]+\]:[ \t]*)(?:\./)?(scripts|references|assets|templates)/", lambda m: m[1] + "../skills/" + asset_parent.rsplit('/', 1)[-1] + "/" + m[2] + "/", file["content"], flags=re.MULTILINE)
                file["content"] += f"\nSupporting scripts and assets are in `{asset_parent}` relative to the workspace.\n"
            for dirname in ("scripts", "references", "assets", "templates"):
                folder = source / dirname
                if not folder.is_dir():
                    continue
                for path in sorted(folder.rglob("*")):
                    if not path.is_file() or "__pycache__" in path.parts:
                        continue
                    if not path.resolve().is_relative_to(source.resolve()):
                        warnings.append(f"{fm['name']}: external linked resource {path.name} was not copied.")
                        continue
                    data = path.read_bytes()
                    destination = f"{asset_parent}/{path.relative_to(source).as_posix()}"
                    try:
                        add(destination, _portable(data.decode("utf8"), target, warnings, destination))
                    except UnicodeDecodeError:
                        add(destination, base64.b64encode(data).decode("ascii"), encoding="base64")

    for rule in rules or []:
        name = rule.get("name") or rule.get("id") or "rule"
        body = rule.get("text") or rule.get("body") or ""
        paths = rule.get("paths") or []
        manual = bool(rule.get("manual"))
        description = rule.get("description") or str(name)
        if target == "cursor":
            fm = {"description": "" if manual else description, "globs": "" if manual else ",".join(paths), "alwaysApply": not paths and not manual}
            add(f".cursor/rules/{unique(name)}.mdc", _markdown(fm, _portable(body, target, warnings, str(name))))
        elif target == "claude" and not manual:
            fm = {"paths": list(paths)} if paths else {}
            add(f".claude/rules/{unique(name)}.md", _markdown(fm, body))
        else:
            scope = "\n\nApply to files matching: " + ", ".join(paths) if paths else ""
            skill("rule-" + str(name), description, str(body) + scope, manual=manual)
            if target == "codex":
                warnings.append(f"{name}: exported as a selectable rule workflow; automatic per-path injection is not represented.")

    for agent in agents or []:
        name = agent.get("slug") or agent.get("name") or "agent"
        description = agent.get("description") or agent.get("name") or str(name)
        body = agent.get("prompt") or ""
        if target == "claude":
            fm = {"name": unique(name), "description": description}
            if agent.get("tools"):
                aliases = {"read_file": "Read", "write_file": "Write", "edit_file": "Edit", "bash": "Bash", "grep": "Grep", "ls": "Glob", "web_fetch": "WebFetch", "web_search": "WebSearch"}
                known = {"Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebFetch", "WebSearch"}
                tools = [aliases.get(t, t) for t in agent["tools"]]
                if all(t in known or t.startswith("mcp__") for t in tools):
                    fm["tools"] = ", ".join(tools)
                else:
                    warnings.append(f"{name}: source tool names have no native host mapping; tool restriction was not exported.")
            add(f".claude/agents/{fm['name']}.md", _markdown(fm, body))
        else:
            skill("agent-" + str(name), description, body, manual=True)
            warnings.append(f"{name}: specialist instructions exported as a workflow; host agent runtime settings are not transferred.")
    return {"target": target, "files": files, "warnings": warnings}


def write_bundle(workspace, bundle, *, overwrite=False):
    """Apply the preview's files, preserving existing different files by default."""
    root = Path(workspace).resolve()
    prepared = []
    seen = set()
    for entry in bundle.get("files", []):
        relative = PurePosixPath(str(entry["path"]).replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or ":" in str(relative):
            raise ValueError("Export file must be relative to the workspace")
        path = (root / str(relative)).resolve()
        if not path.is_relative_to(root) or path in seen:
            raise ValueError("Export path leaves the workspace or is duplicated")
        seen.add(path)
        data = base64.b64decode(entry["content"], validate=True) if entry.get("encoding") == "base64" else entry["content"].encode("utf8")
        prepared.append((entry["path"], path, data))
    results = []
    for relative, path, data in prepared:
        exists = path.exists()
        if exists and path.read_bytes() == data:
            status = "unchanged"
        elif exists and not overwrite:
            status = "conflict"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".export-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            status = "updated" if exists else "created"
        results.append({"path": relative, "status": status})
    return {"files": results, "created": sum(r["status"] == "created" for r in results), "updated": sum(r["status"] == "updated" for r in results), "unchanged": sum(r["status"] == "unchanged" for r in results), "conflicts": [r["path"] for r in results if r["status"] == "conflict"], "warnings": list(bundle.get("warnings", []))}
