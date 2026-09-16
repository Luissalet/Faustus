"""Typed, declarative document operations (WP12).

Sub-package of ``src/creator`` (WP02's ``DocumentStore``/``CreatorDocument``).
Each sibling module (``canvas_ops``, ``timeline_ops``, ``transcript_ops``,
``undo``) exposes a module-level ``OPS: Dict[str, Callable]`` mapping an
``op["type"]`` string to a pure handler ``(doc, op) -> new_content``.
``registry.py`` discovers them via :mod:`pkgutil` and is the single entry
point ``src/creator/store.py`` dispatches through (see that module's
``_apply_op`` docstring for the wiring).

``OPERATIONS_SEMANTICS_VERSION`` versions THIS package's op vocabulary and
ordering rules. It is unrelated to ``src/media_edit_projects.py``'s
``OPERATION_SEMANTICS_VERSION = 1`` (WP01's legacy layered-compositor
history), which this package never imports, reinterprets or changes.
"""
from __future__ import annotations

OPERATIONS_SEMANTICS_VERSION = 1
