"""src/creator/adapters — WP10: one module per engine, discovered by pkgutil.

`registry()` walks this package's own directory (or, for a test, any extra
directory appended to `__path__`) and imports every module that is not
private or `base`. A module opts into being an adapter by defining a
module-level `ADAPTER_FACTORY` — a zero-argument callable returning an
`AdapterPort` — and nothing else: there is no list anywhere to remember to
update (CONTRATO.md/WP10 ficha: "nada de listas que olvidar").
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Callable, Dict

from src.creator.adapter_port import AdapterPort

__all__ = ["registry", "get"]


def registry() -> Dict[str, Callable[[], AdapterPort]]:
    """`{module_name: factory}` for every adapter module found right now.
    Re-scans `__path__` on every call rather than caching — this package's
    own modules never change at runtime, and a test that appends a temp
    directory to `__path__` expects THIS call to see it, not a cached one
    from before the append."""
    factories: Dict[str, Callable[[], AdapterPort]] = {}
    for _finder, module_name, _is_pkg in pkgutil.iter_modules(__path__, prefix=""):
        if module_name.startswith("_") or module_name == "base":
            continue
        module = importlib.import_module(f"{__name__}.{module_name}")
        factory = getattr(module, "ADAPTER_FACTORY", None)
        if factory is not None:
            factories[module_name] = factory
    return factories


def get(name: str) -> AdapterPort:
    """One adapter instance by module name, or `KeyError` naming what IS
    registered — never a bare `KeyError: 'foo'` a caller has to go look up."""
    factories = registry()
    factory = factories.get(name)
    if factory is None:
        raise KeyError(f"no adapter named {name!r}; registered: {sorted(factories)}")
    return factory()
