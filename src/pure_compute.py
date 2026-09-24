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
    "math", "cmath", "itertools", "functools", "string", "re", "json",
    "collections", "statistics", "fractions", "decimal", "datetime", "textwrap",
    "unicodedata", "random", "heapq", "bisect", "numbers", "difflib", "calendar",
})
# Left out on purpose: `operator` (attrgetter/methodcaller take attribute
# names as strings the AST cannot see), `typing` (get_type_hints evaluates
# strings), `dataclasses`/`enum`/`copy`/`pprint` (reach into object
# internals or streams). A snippet that needs them simply asks as before.

#: Builtins that reach outside the computation (files, interpreter, user,
#: introspection that leads to either).
_FORBIDDEN_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "globals",
    "locals", "vars", "memoryview", "help", "exit", "quit", "getattr", "setattr",
    "delattr", "dir", "type", "object", "super", "classmethod", "staticmethod", "property",
})

#: Attribute names that walk from an ordinary object to frames, globals,
#: the builtins or a file/process, even without a leading underscore.
_FORBIDDEN_ATTRS = frozenset({
    "gi_frame", "gi_code", "cr_frame", "cr_code", "ag_frame", "ag_code", "f_globals",
    "f_locals", "f_builtins", "f_back", "f_code", "tb_frame", "tb_next", "mro",
    "Formatter", "get_field", "vformat", "format_field", "open", "system", "popen",
    # Modules an allowed module re-exports (`calendar.sys.modules["os"]`,
    # `json.codecs.open`, `fractions.operator.attrgetter`): the walk below
    # adds whatever this Python exposes; these stay even if it finds none.
    "sys", "modules", "codecs", "operator", "copyreg", "enum", "os", "io", "builtins",
    "subprocess", "importlib", "shutil", "socket", "pathlib", "ctypes",
})


def _reexported_unsafe_modules() -> frozenset:
    """Public attribute names, anywhere under the allowed modules, whose value
    is a module outside them."""
    import importlib
    import types
    out: set = set()
    seen: set = set()
    queue = []
    for name in PURE_MODULES:
        try:
            queue.append(importlib.import_module(name))
        except Exception:  # noqa: BLE001 - a missing module adds nothing
            pass
    while queue:
        mod = queue.pop()
        if mod.__name__ in seen:
            continue
        seen.add(mod.__name__)
        for attr in dir(mod):
            if attr.startswith("_"):
                continue
            val = getattr(mod, attr, None)
            if isinstance(val, types.ModuleType):
                if val.__name__.split(".")[0] in PURE_MODULES:
                    queue.append(val)
                else:
                    out.add(attr)
    return frozenset(out)


try:
    _FORBIDDEN_ATTRS = _FORBIDDEN_ATTRS | _reexported_unsafe_modules()
except Exception:  # noqa: BLE001 - the static list above still applies
    pass

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
            # `from calendar import sys` is `calendar.sys` by another name.
            if any(alias.name == "*" or alias.name.startswith("_") or alias.name in _FORBIDDEN_ATTRS
                   for alias in node.names):
                return False
        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_NAMES or node.id.startswith("__"):
                return False
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in _FORBIDDEN_ATTRS:
                return False
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A dunder-shaped string is how a name the AST cannot see gets
            # looked up (a dict key, a format field): never in a computation.
            if "__" in node.value:
                return False
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            return False
    return True
