"""pure_compute.py — python that can only compute.

The external-context gate (`tool_capabilities.ContextAwareSecurityContext`)
asks for approval before a code-running tool runs once the turn has taken
in content Faustus did not write (a file it read, a web page, an image
description), because that content could carry an instruction the code
then acts on. That is right for code that can act: write a file, open a
socket, start a process. It is noise for a snippet that shifts letters or
multiplies two numbers — and on a local model the approval pause costs the
turn its momentum (live, exam runs 13–16: the turn resumed after the card
and started the task over).

`is_pure_compute(code)` says whether a python snippet provably cannot act
outside its own process: it parses, imports only from a short list of
computation modules, calls none of the builtins that reach the file system,
the interpreter or the user (`open`, `exec`, `eval`, `compile`,
`__import__`, `input`, `breakpoint`, `globals`, `vars`...), and touches no
dunder attribute or name (no `().__class__.__subclasses__()` escapes).
Anything it cannot prove is not pure. It only ever widens the gate for code
that has no effect to gate; everything else is decided exactly as before.
"""
from __future__ import annotations

import ast
import json
from typing import Any

#: Modules a pure computation may import (no I/O, no processes, no network).
PURE_MODULES = frozenset({
    "math", "cmath", "itertools", "functools", "operator", "string", "re", "json",
    "collections", "statistics", "fractions", "decimal", "datetime", "textwrap",
    "unicodedata", "random", "heapq", "bisect", "typing", "dataclasses", "enum",
    "numbers", "difflib", "calendar", "copy", "pprint",
})

#: Builtins that reach outside the computation (files, interpreter, user,
#: introspection that leads to either).
_FORBIDDEN_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "globals",
    "locals", "vars", "memoryview", "help", "exit", "quit", "getattr", "setattr",
    "delattr", "dir", "type", "object", "super", "classmethod", "staticmethod", "property",
})

_MAX_CODE_CHARS = 40_000


def _code_of(content: Any) -> str:
    if isinstance(content, dict):
        return str(content.get("code") or content.get("content") or "")
    text = str(content or "")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except Exception:  # noqa: BLE001 - not JSON: the code itself
            return text
        if isinstance(data, dict):
            return str(data.get("code") or data.get("content") or "")
    return text


def is_pure_compute(content: Any) -> bool:
    """True only when the snippet provably cannot act outside its process."""
    code = _code_of(content)
    if not code.strip() or len(code) > _MAX_CODE_CHARS:
        return False
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] not in PURE_MODULES for alias in node.names):
                return False
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module or node.module.split(".")[0] not in PURE_MODULES:
                return False
        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_NAMES or node.id.startswith("__"):
                return False
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr.startswith("_"):
                return False
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            return False
    return True
