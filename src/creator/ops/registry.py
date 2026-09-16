"""Op registry — discovers ``OPS`` dicts from sibling modules via
:mod:`pkgutil`, and dispatches ``op["type"]`` to the matching handler.

``src/creator/store.py``'s ``_apply_op`` is the single caller in this repo
(WP12's aditive dispatcher line — see that module). Nothing here knows
about sqlite, revisions or command dedup: ``apply()`` is pure, ``(doc, op)
-> new_content``.
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Callable, Dict, Optional

from ..errors import InvalidOperation

_REGISTRY: Optional[Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]]] = None

# Modules in this package that are NOT op providers (no OPS dict expected).
_NON_OP_MODULES = {"__init__", "registry", "model"}


def _discover() -> Dict[str, Callable]:
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY
    registry: Dict[str, Callable] = {}
    package = importlib.import_module(__package__)  # src.creator.ops
    for modinfo in pkgutil.iter_modules(package.__path__):
        if modinfo.name in _NON_OP_MODULES:
            continue
        module = importlib.import_module(f"{__package__}.{modinfo.name}")
        ops = getattr(module, "OPS", None)
        if not isinstance(ops, dict):
            continue
        for op_type, handler in ops.items():
            if not isinstance(op_type, str) or not op_type:
                raise RuntimeError(f"{modinfo.name}.OPS has a non-string key: {op_type!r}")
            if op_type in registry:
                raise RuntimeError(
                    f"duplicate op type {op_type!r} registered by both "
                    f"{registry[op_type].__module__!r} and {modinfo.name!r}"
                )
            if not callable(handler):
                raise RuntimeError(f"{modinfo.name}.OPS[{op_type!r}] is not callable")
            registry[op_type] = handler
    _REGISTRY = registry
    return registry


def known_op_types() -> Dict[str, str]:
    """``{op_type: defining module name}`` — introspection helper for
    callers (routes, tests) that want to list what is registered."""
    return {op_type: handler.__module__ for op_type, handler in _discover().items()}


def get(op_type: str) -> Optional[Callable]:
    return _discover().get(op_type)


def apply(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """``doc`` is a plain dict snapshot with at least ``kind``/``content``
    (``src/creator/store.py`` passes ``revision``/``state``/``asset_refs``
    too, for ops that need them, e.g. undo's bounds check). Raises
    :class:`InvalidOperation` for an unknown ``op['type']`` or a malformed
    op shape — never silently no-ops."""
    if not isinstance(op, dict) or not isinstance(op.get("type"), str) or not op["type"]:
        raise InvalidOperation("op must be an object with a non-empty string 'type'")
    handler = get(op["type"])
    if handler is None:
        raise InvalidOperation(f"unknown op type: {op['type']!r}")
    return handler(doc, op)
