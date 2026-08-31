"""project_instructions.py — standing instructions the project keeps in its own files.

Coding agents have converged on a convention: a Markdown file at the root of
the repository (AGENTS.md, CLAUDE.md, …) that tells the agent how the project
works — conventions, how to run the tests, what not to touch. Faustus injects
that file into the system prompt of every turn that has a workspace, so a
local model does not have to rediscover (or invent) the rules each time.

Lookup order (first existing file wins, unless the setting lists otherwise):
    AGENTS.md, CLAUDE.md, .odysseus/INSTRUCTIONS.md, ODYSSEUS.md,
    .cursorrules, CONVENTIONS.md, .github/copilot-instructions.md

The block is byte-identical across turns until the file changes (KV-cache
friendly), capped at `agent_project_instructions_max_chars`, and cached by
mtime. Stdlib only, never raises.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_FILES = (
    "AGENTS.md", "CLAUDE.md", os.path.join(".odysseus", "INSTRUCTIONS.md"), "ODYSSEUS.md",
    ".cursorrules", "CONVENTIONS.md", os.path.join(".github", "copilot-instructions.md"),
)
DEFAULT_MAX_CHARS = 6000
_CACHE: Dict[str, Tuple[float, Optional[str], float, str]] = {}   # root → (checked_at, path, mtime, block)
_LOCK = threading.Lock()
_TTL_S = 5.0


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def candidate_files() -> List[str]:
    raw = _setting("agent_project_instructions_files", None)
    if isinstance(raw, str) and raw.strip():
        return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    if isinstance(raw, (list, tuple)) and raw:
        return [str(p).strip() for p in raw if str(p).strip()]
    return list(DEFAULT_FILES)


def find_file(workspace: str) -> Optional[str]:
    """Absolute path of the first instructions file that exists, or None."""
    if not workspace:
        return None
    root = os.path.realpath(os.path.expanduser(workspace))
    for rel in candidate_files():
        p = os.path.join(root, rel)
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
        except OSError:
            continue
    return None


def read(workspace: str) -> Dict[str, Any]:
    """{"path", "rel", "text", "truncated", "chars"} or an empty dict."""
    p = find_file(workspace)
    if not p:
        return {}
    try:
        limit = int(_setting("agent_project_instructions_max_chars", DEFAULT_MAX_CHARS) or DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_CHARS
    limit = max(500, min(limit, 60_000))
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(limit + 1)
    except OSError:
        return {}
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]
    root = os.path.realpath(os.path.expanduser(workspace))
    return {
        "path": p,
        "rel": os.path.relpath(p, root).replace(os.sep, "/"),
        "text": text.replace("\r\n", "\n").strip(),
        "truncated": truncated,
        "chars": len(text),
    }


def block(workspace: str) -> str:
    """The system-prompt section, '' when the feature is off or no file exists."""
    if not workspace or not bool(_setting("agent_project_instructions", True)):
        return ""
    root = os.path.realpath(os.path.expanduser(workspace))
    now = time.time()
    with _LOCK:
        cached = _CACHE.get(root)
    if cached and now - cached[0] < _TTL_S:
        return cached[3]
    p = find_file(root)
    mtime = 0.0
    if p:
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = 0.0
    if cached and cached[1] == p and cached[2] == mtime:
        with _LOCK:
            _CACHE[root] = (now, p, mtime, cached[3])
        return cached[3]
    info = read(root) if p else {}
    text = ""
    if info.get("text"):
        note = " (truncated — read the file for the rest)" if info.get("truncated") else ""
        text = (
            f"\n\n## Project instructions from {info['rel']}{note}\n"
            "These are the project's standing rules, written by its maintainers. Follow them "
            "(conventions, how to run tests, what not to touch) unless the user says otherwise.\n"
            f"{info['text']}"
        )
    with _LOCK:
        _CACHE[root] = (now, p, mtime, text)
    if text:
        logger.debug("[instructions] injecting %s (%d chars)", info.get("rel"), len(info.get("text") or ""))
    return text


# ---------------------------------------------------------------------------
# Draft an AGENTS.md from what the runtime can already see
# ---------------------------------------------------------------------------

_LANG_BY_EXT = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript/React",
    ".jsx": "JavaScript/React", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".cs": "C#",
    ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".c": "C", ".cpp": "C++", ".h": "C/C++", ".vue": "Vue",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".sql": "SQL", ".sh": "shell", ".ps1": "PowerShell",
}


def draft(workspace: str, language: str = "en") -> Dict[str, Any]:
    """A first AGENTS.md for the workspace, built from facts the runtime already
    detects (test runner, languages, top-level layout, package manifests) plus
    the conventions a local model needs spelled out. The user edits the rest.
    Returns {"text", "path", "exists", "facts"}."""
    root = os.path.realpath(os.path.expanduser(workspace or ""))
    if not root or not os.path.isdir(root):
        return {"text": "", "path": "", "exists": False, "facts": {}, "error": "workspace is not a folder"}
    facts: Dict[str, Any] = {}
    try:
        from src.agent_harness import workspace_file_index
        files = workspace_file_index(root)
    except Exception:
        files = []
    counts: Dict[str, int] = {}
    for rel in files:
        ext = os.path.splitext(rel)[1].lower()
        lang = _LANG_BY_EXT.get(ext)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    langs = [k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:4]]
    facts["languages"] = langs
    top_dirs = sorted({rel.split("/", 1)[0] for rel in files if "/" in rel})[:14]
    facts["top_dirs"] = top_dirs
    manifests = [m for m in ("pyproject.toml", "setup.py", "requirements.txt", "package.json", "Cargo.toml", "go.mod",
                             "Makefile", "docker-compose.yml", "Dockerfile") if os.path.isfile(os.path.join(root, m))]
    facts["manifests"] = manifests
    test_cmd = ""
    try:
        from src.project_tests import detect_test_command
        spec = detect_test_command(root)
        if spec:
            test_cmd = spec.get("shell") or " ".join(
                os.path.basename(a) if i == 0 else a for i, a in enumerate(spec.get("argv") or []))
    except Exception:
        spec = None
    facts["test_command"] = test_cmd
    existing = find_file(root)
    es = language == "es"
    name = os.path.basename(root.rstrip(os.sep)) or root
    lines = [f"# {name} — instructions for the coding agent", ""]
    lines.append("<!-- Draft generated by Faustus from what it detected in this folder. Edit freely: this file is "
                 "injected into every agent turn that works in this workspace. -->" if not es else
                 "<!-- Borrador generado por Faustus a partir de lo que detecta en esta carpeta. Edítalo: este fichero "
                 "se inyecta en cada turno del agente que trabaje en este workspace. -->")
    lines += ["", "## Project" if not es else "## Proyecto"]
    if langs:
        lines.append(("- Languages: " if not es else "- Lenguajes: ") + ", ".join(langs))
    if top_dirs:
        lines.append(("- Layout: " if not es else "- Estructura: ") + ", ".join(f"`{d}/`" for d in top_dirs))
    if manifests:
        lines.append(("- Manifests: " if not es else "- Manifiestos: ") + ", ".join(f"`{m}`" for m in manifests))
    lines += ["", "## How to run the tests" if not es else "## Cómo se ejecutan los tests"]
    if test_cmd:
        lines.append(f"- `{test_cmd}`" + (" (detected; the runtime runs the related tests after every change)" if not es
                                           else " (detectado; el runtime ejecuta los tests relacionados tras cada cambio)"))
    else:
        lines.append("- <no test runner detected — write the command here, e.g. `pytest -q` or `npm test`>" if not es
                     else "- <no se ha detectado un runner — escribe aquí el comando, p. ej. `pytest -q` o `npm test`>")
    lines += ["", "## Conventions" if not es else "## Convenciones",
              "- Prefer small, focused edits (`edit_file`) over rewriting whole files." if not es
              else "- Prefiere cambios pequeños y concretos (`edit_file`) a reescribir ficheros enteros.",
              "- Keep the existing style (naming, formatting, line endings)." if not es
              else "- Mantén el estilo existente (nombres, formato, finales de línea).",
              "- Add or update a test next to any behaviour change." if not es
              else "- Añade o actualiza un test junto a cualquier cambio de comportamiento.",
              "", "## Do not touch" if not es else "## No tocar",
              "- <generated files, vendored code, migrations, secrets… list them here>" if not es
              else "- <ficheros generados, código vendored, migraciones, secretos… lístalos aquí>",
              "", "## When unsure" if not es else "## Ante la duda",
              "- Ask before changing public interfaces, data formats or dependencies." if not es
              else "- Pregunta antes de cambiar interfaces públicas, formatos de datos o dependencias.", ""]
    return {"text": "\n".join(lines), "path": os.path.join(root, "AGENTS.md"), "exists": bool(existing),
            "existing": existing, "facts": facts}


REMEMBER_HEADING = "## Notes added from chat"
_REMEMBER_MAX_CHARS = 500


def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def normalise_rule(text: str) -> str:
    """One clean bullet body from whatever the user typed.

    Leading "#" (the composer shortcut), list markers and internal newlines all
    go, because the value of this file is that it stays a short readable list —
    a pasted three-paragraph note is a worse instruction than one sentence.
    """
    t = (text or "").strip()
    while t[:1] in ("#", "-", "*", ">"):
        t = t[1:].lstrip()
    t = " ".join(t.split())
    if len(t) > _REMEMBER_MAX_CHARS:
        t = t[: _REMEMBER_MAX_CHARS - 1].rstrip() + "…"
    return t


def remember(workspace: str, text: str, *, heading: str = REMEMBER_HEADING) -> Dict[str, Any]:
    """Append one standing rule to the workspace's instructions file.

    Claude Code's "#" shortcut: the rule you just had to repeat becomes part of
    every future turn's system prompt instead of being retyped. Uses the file
    the project already has (AGENTS.md, CLAUDE.md, …) and creates AGENTS.md
    when there is none. Never rewrites what is already in the file.

    Returns {"path", "rel", "rule", "created", "duplicate", "chars"} or
    {"error": ...}.
    """
    root = os.path.realpath(os.path.expanduser(workspace or ""))
    if not root or not os.path.isdir(root):
        return {"error": "workspace is not a folder"}
    rule = normalise_rule(text)
    if not rule:
        return {"error": "nothing to remember"}
    path = find_file(root)
    created = False
    if path:
        try:
            # newline="" so CRLF survives the read — universal newlines would
            # hide it and the rewrite would silently convert the whole file.
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
                body = f.read()
        except OSError as e:
            return {"error": f"could not read {os.path.basename(path)}: {e}"}
    else:
        rel = candidate_files()[0] if candidate_files() else "AGENTS.md"
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path) or root, exist_ok=True)
        body = f"# {os.path.basename(root) or 'Project'}\n\nInstructions for the coding agent.\n"
        created = True
    nl = _detect_newline(body)
    flat = body.replace("\r\n", "\n")
    bullet = f"- {rule}"
    if bullet in flat.split("\n"):
        return {"path": path, "rel": os.path.relpath(path, root).replace(os.sep, "/"),
                "rule": rule, "created": False, "duplicate": True, "chars": len(body)}
    lines = flat.rstrip("\n").split("\n") if flat.strip() else []
    if heading in lines:
        # Insert at the end of that section, before the next "## " heading.
        idx = lines.index(heading) + 1
        while idx < len(lines) and not lines[idx].startswith("## "):
            idx += 1
        while idx > 0 and not lines[idx - 1].strip():
            idx -= 1
        lines.insert(idx, bullet)
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [heading, "", bullet]
    out = nl.join(lines) + nl
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(out)
    except OSError as e:
        return {"error": f"could not write {os.path.basename(path)}: {e}"}
    invalidate(root)
    return {"path": path, "rel": os.path.relpath(path, root).replace(os.sep, "/"),
            "rule": rule, "created": created, "duplicate": False, "chars": len(out)}


def invalidate(workspace: Optional[str] = None) -> None:
    with _LOCK:
        if workspace:
            _CACHE.pop(os.path.realpath(os.path.expanduser(workspace)), None)
        else:
            _CACHE.clear()
