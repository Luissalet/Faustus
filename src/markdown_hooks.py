"""Portable Markdown checks: advisory hooks on the existing lifecycle engine."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from services.memory.skill_format import emit_frontmatter, parse_frontmatter, slugify

EVENTS = ("bash", "file", "stop", "prompt")
OPERATORS = ("regex_match", "contains", "equals", "not_contains", "starts_with", "ends_with")
_LIFECYCLE = {"bash": "pre_tool", "file": "pre_tool", "stop": "turn_end", "prompt": "turn_start"}


def _scalar(raw: str) -> Any:
    return parse_frontmatter(f"---\nvalue: {raw}\n---\n")[0].get("value", "")


def _conditions(text: str, fm: dict) -> list[dict]:
    """Read Hookify's list of simple condition maps with the canonical scalars."""
    raw = fm.get("conditions")
    if isinstance(raw, dict):
        return [{"field": key, "operator": "regex_match", "pattern": str(value)} for key, value in raw.items()]
    lines = text.split("\n---", 1)[0].splitlines()[1:]
    reading = False
    records: list[dict] = []
    for line in lines:
        if re.match(r"^conditions:\s*$", line):
            reading = True
            continue
        if not reading:
            continue
        if line.strip() and not line[:1].isspace():
            break
        pair = re.match(r"^\s+(-\s+)?(field|operator|pattern):\s*(.*)$", line)
        if not pair:
            continue
        if pair[1] or not records:
            records.append({})
        records[-1][pair[2]] = _scalar(pair[3])
    return records


def parse_rule(text: str, *, path: str = "") -> dict:
    fm, message = parse_frontmatter(text.lstrip("\ufeff").replace("\r\n", "\n"))
    event = str(fm.get("event") or "")
    if event not in EVENTS:
        raise ValueError(f"event must be one of {', '.join(EVENTS)}")
    name = str(fm.get("name") or Path(path).stem or "hook")
    conditions = _conditions(text, fm)
    pattern = str(fm.get("pattern") or "")
    warnings = []
    if not message.strip():
        raise ValueError("The Markdown body must contain the check's message.")
    if not pattern and not conditions:
        raise ValueError("Provide a pattern or conditions.")
    if str(fm.get("action") or "warn") != "warn":
        warnings.append("This check emits a warning; it never blocks a tool or adds an approval.")
    for condition in conditions:
        if not condition.get("field") or condition.get("operator", "regex_match") not in OPERATORS:
            raise ValueError("Conditions need a field and a supported operator.")
    for candidate in [pattern] + [str(c.get("pattern", "")) for c in conditions if c.get("operator", "regex_match") == "regex_match"]:
        if candidate:
            try:
                re.compile(candidate)
            except re.error as error:
                warnings.append(f"Invalid regex: {error}; other checks remain available.")
    return {"name": name, "event": event, "enabled": fm.get("enabled", True) is not False,
            "pattern": pattern, "conditions": conditions, "message": message.strip(),
            "path": path, "action": "warn", "warnings": warnings}


def to_markdown(rule: dict) -> str:
    fm = {"name": rule.get("name", "hook"), "enabled": rule.get("enabled", True),
          "event": rule.get("event", "prompt"), "action": "warn"}
    if rule.get("pattern"):
        fm["pattern"] = rule["pattern"]
    header = "---\n" + emit_frontmatter(fm).rstrip() + "\n---"
    conditions = rule.get("conditions") or []
    if conditions:
        lines = ["conditions:"]
        for condition in conditions:
            for index, key in enumerate(("field", "operator", "pattern")):
                lines.append(f"  {'- ' if index == 0 else '  '}{key}: {json.dumps(condition.get(key, 'regex_match' if key == 'operator' else ''), ensure_ascii=False)}")
        header = header.rsplit("\n---", 1)[0] + "\n" + "\n".join(lines) + "\n---"
    return header + "\n\n" + str(rule.get("message") or "") + "\n"


def load_rules(workspace: str) -> dict:
    rules, warnings = [], []
    if not workspace:
        return {"rules": rules, "warnings": warnings}
    directory = Path(workspace).expanduser().resolve() / ".faustus" / "hooks"
    if not directory.is_dir():
        return {"rules": rules, "warnings": warnings}
    for path in sorted(directory.glob("*.md")):
        try:
            # A rule symlink must not turn a project hook read into reading an
            # unrelated private file outside its declared hooks directory.
            if path.resolve().parent != directory.resolve():
                warnings.append({"path": str(path), "message": "Hook file points outside its hooks directory."})
                continue
            rule = parse_rule(path.read_text(encoding="utf-8"), path=str(path))
            rules.append(rule)
            warnings.extend({"path": str(path), "message": message} for message in rule["warnings"])
        except (OSError, ValueError) as error:
            warnings.append({"path": str(path), "message": str(error)})
    return {"rules": rules, "warnings": warnings}


def save_rule(workspace: str, rule: dict, *, overwrite: bool = False) -> dict:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("workspace must be an existing directory")
    content = to_markdown(rule)
    parsed = parse_rule(content)
    directory = root / ".faustus" / "hooks"
    if not directory.resolve().is_relative_to(root):
        raise ValueError("The hooks directory points outside the workspace")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (slugify(parsed["name"]) + ".md")
    if path.resolve().parent != directory.resolve():
        return {"status": "conflict", "path": str(path), "rule": parsed,
                "warnings": ["Existing hook link points outside its hooks directory and was preserved."]}
    if path.exists():
        if path.read_text(encoding="utf-8") == content:
            return {"status": "unchanged", "path": str(path), "rule": parsed}
        if not overwrite:
            return {"status": "conflict", "path": str(path), "rule": parsed}
    fd, temporary = tempfile.mkstemp(prefix=".hook-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"status": "saved", "path": str(path), "rule": parsed, "warnings": parsed["warnings"]}


def _values(ctx: dict) -> dict[str, list[str]]:
    args = ctx.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    raw_content = ctx.get("content")
    if isinstance(raw_content, dict):
        args = {**raw_content, **args}
    elif isinstance(raw_content, str):
        try:
            decoded = json.loads(raw_content)
            if isinstance(decoded, dict):
                args = {**decoded, **args}
        except ValueError:
            pass
    paths = ctx.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        path = args.get("file_path") or args.get("path") or args.get("filename")
        paths = [path] if isinstance(path, str) else []
    workspace = str(ctx.get("workspace") or "")
    relative = []
    for path in paths:
        value = str(path)
        if workspace and os.path.isabs(value):
            value = os.path.relpath(value, workspace)
        relative.append(value.replace("\\", "/"))
    body = next((args[key] for key in ("content", "new_text", "text") if key in args), raw_content)
    content = str(body or "")
    return {"command": [str(ctx.get("command") or args.get("command") or "")],
            "file_path": relative, "path": relative, "new_text": [str(args.get("new_text", content) or "")],
            "old_text": [str(args.get("old_text") or "")], "content": [content],
            "user_prompt": [str(ctx.get("user_message") or "")], "text": [content],
            "assistant_message": [str(ctx.get("assistant_message") or ctx.get("content") or "")]}


def _matches(value: str, operator: str, pattern: str) -> bool:
    if operator == "contains": return pattern in value
    if operator == "not_contains": return pattern not in value
    if operator == "equals": return value == pattern
    if operator == "starts_with": return value.startswith(pattern)
    if operator == "ends_with": return value.endswith(pattern)
    try:
        return re.search(pattern, value) is not None
    except re.error:
        return False


def evaluate_rules(rules: list[dict], event: str, ctx: dict) -> list[dict]:
    values = _values(ctx)
    hits = []
    for rule in rules:
        if not rule.get("enabled", True) or _LIFECYCLE.get(rule.get("event")) != event:
            continue
        kind = rule["event"]
        if kind == "bash" and not values["command"][0]: continue
        if kind == "file" and values["command"][0]: continue
        if kind == "file" and not (values["file_path"] or values["content"][0]): continue
        field = {"bash": "command", "file": "content", "prompt": "user_prompt", "stop": "assistant_message"}[kind]
        if rule.get("pattern") and not any(_matches(value, "regex_match", rule["pattern"]) for value in values[field]): continue
        if not all(any(_matches(value, str(condition.get("operator", "regex_match")), str(condition.get("pattern", ""))) for value in values.get(str(condition.get("field")), [])) for condition in rule.get("conditions", [])): continue
        hits.append({"id": "markdown-" + slugify(rule["name"]), "name": rule["name"], "enabled": True,
                     "event": event, "scope": "project", "match": {}, "action": "warn", "text": rule["message"],
                     "command": None, "note": "Project Markdown check; advisory only."})
    return hits


def lifecycle_matches(event: str, ctx: dict) -> list[dict]:
    if event == "turn_end" and not ctx.get("assistant_message") and ctx.get("session_id"):
        # The existing post-turn callback supplies the session id rather than
        # repeating the final answer. Read its already-live transcript only;
        # no model, disk hydration or new chat callback is needed.
        try:
            from core.models import get_session_manager_instance
            manager = get_session_manager_instance()
            session = manager.sessions.get(str(ctx["session_id"])) if manager else None
            for message in reversed(getattr(session, "history", []) or []):
                role = message.get("role") if isinstance(message, dict) else getattr(message, "role", "")
                content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
                if role == "assistant" and isinstance(content, str):
                    ctx = {**ctx, "assistant_message": content}
                    break
        except (AttributeError, ImportError):
            pass
    return evaluate_rules(load_rules(str(ctx.get("workspace") or ""))["rules"], event, ctx)


def propose_hooks(messages: list[dict]) -> dict:
    """Deterministic proposals from explicit corrections, never auto-installed."""
    proposals, notes, seen = [], [], set()
    for index, message in enumerate(messages):
        if message.get("role") != "user": continue
        text = message.get("content") or message.get("text") or ""
        if not isinstance(text, str): continue
        if not re.search(r"\b(no\s+(?:uses|utilices|vuelvas|hagas|borres)|correcci[oó]n|ya te (?:dije|he dicho)|en vez de|recuerda|don't|do not|instead of)\b", text, re.I): continue
        # A replacement's recommended command stays in the message. It is
        # evidence of the fix, never a second undesirable-pattern trigger.
        unwanted = re.findall(r"(?:en vez de|instead of|no\s+(?:uses|utilices|ejecutes|hagas|borres|vuelvas[^`\n]*?)|don't\s+(?:use|run)|do not\s+(?:use|run))\s*`([^`\n]+)`", text, re.I)
        examples = unwanted or re.findall(r"`([^`\n]+)`", text)
        if not examples:
            notes.append({"message_index": index, "message": "Correction kept as evidence; supply a concrete command/path/text to make a mechanical check."})
            continue
        for example in examples:
            digest = hashlib.sha256((text + "\0" + example).encode()).hexdigest()[:12]
            if digest in seen: continue
            seen.add(digest)
            path_example = bool(re.search(r"\.(?:py|js|ts|tsx|md|json|yaml|yml|toml|txt)$", example, re.I)) and " " not in example
            event = "file" if path_example else "bash"
            rule = {"name": f"correction-{digest}", "event": event, "enabled": True, "message": text,
                    "conditions": [{"field": "file_path" if path_example else "command", "operator": "contains", "pattern": example}]}
            proposals.append({**rule, "message_index": index, "evidence": text, "markdown": to_markdown(rule), "installed": False})
    return {"proposals": proposals, "notes": notes, "method": "deterministic-explicit-corrections", "installed": False}
