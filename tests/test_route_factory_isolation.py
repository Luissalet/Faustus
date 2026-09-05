"""B-006: a route factory must not build on a module-level router.

Five modules declared `router = APIRouter(...)` at import time and every
`setup_*_routes(manager)` call appended its routes to that same object, each
set closed over a different manager. Two apps in one process (or two tests)
then shared one router, and looking a route up by path could hand back a
closure over somebody else's manager — which is how a browser-restart test
ended up calling another test's McpManager and hanging on a real `npx`.

These tests build two of everything and check the two never meet.
"""

import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest


def _fake(name):
    """A manager stand-in that is identifiable and does nothing."""
    return SimpleNamespace(_probe=name)


def _build_mcp(manager):
    from routes.mcp.mcp_routes import setup_mcp_routes
    return setup_mcp_routes(manager)


def _build_session(manager):
    from routes.session_routes import setup_session_routes
    return setup_session_routes(manager, {})


def _build_upload(handler):
    from routes.upload_routes import setup_upload_routes
    router, _cleanup = setup_upload_routes(handler)
    return router


def _build_compare(manager):
    from routes.compare.compare_routes import setup_compare_routes
    return setup_compare_routes(manager)


def _build_webhook(manager):
    from routes.webhook.webhook_routes import setup_webhook_routes
    return setup_webhook_routes(manager, _fake("auth"))


FACTORIES = {
    "mcp": _build_mcp,
    "session": _build_session,
    "upload": _build_upload,
    "compare": _build_compare,
    "webhook": _build_webhook,
}


def _closed_over(router):
    """Every object the router's endpoints hold in a closure cell."""
    seen = set()
    for route in router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        try:
            nonlocals = inspect.getclosurevars(endpoint).nonlocals
        except (TypeError, ValueError):
            continue
        for value in nonlocals.values():
            seen.add(id(value))
    return seen


@pytest.mark.parametrize("name", sorted(FACTORIES))
def test_two_setups_produce_two_independent_routers(name, monkeypatch):
    import fastapi.dependencies.utils as dependency_utils
    monkeypatch.setattr(dependency_utils, "ensure_multipart_is_installed", lambda: None)

    build = FACTORIES[name]
    first_manager, second_manager = _fake("first"), _fake("second")
    first = build(first_manager)
    second = build(second_manager)

    assert first is not second
    assert first.routes, f"{name}: the factory produced no routes"
    # The bug's signature: the second call doubling the first router's routes.
    assert len(first.routes) == len(second.routes)
    paths_first = [getattr(r, "path", "") for r in first.routes]
    assert len(paths_first) == len(set(zip(paths_first, [
        tuple(sorted(getattr(r, "methods", []) or [])) for r in first.routes
    ]))), f"{name}: the same path is registered twice in one router"


@pytest.mark.parametrize("name", sorted(FACTORIES))
def test_each_router_closes_over_its_own_manager(name, monkeypatch):
    import fastapi.dependencies.utils as dependency_utils
    monkeypatch.setattr(dependency_utils, "ensure_multipart_is_installed", lambda: None)

    build = FACTORIES[name]
    first_manager, second_manager = _fake("first"), _fake("second")
    first = build(first_manager)
    second = build(second_manager)

    first_cells, second_cells = _closed_over(first), _closed_over(second)
    assert id(first_manager) in first_cells, f"{name}: router does not hold its manager"
    assert id(second_manager) not in first_cells, (
        f"{name}: the first router reaches the second manager — routes are shared"
    )
    assert id(second_manager) in second_cells
    assert id(first_manager) not in second_cells


def test_no_route_module_builds_its_router_at_import_time():
    """The guard: a new module-level router reintroduces the whole bug."""
    offenders = []
    for path in Path("routes").rglob("*.py"):
        source = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(source.splitlines(), 1):
            if re.match(r"^\w[\w_]*\s*=\s*APIRouter\(", line):
                offenders.append(f"{path}:{number}: {line.strip()}")
    assert not offenders, (
        "module-level APIRouter(s) found — build the router inside the factory:\n"
        + "\n".join(offenders)
    )
