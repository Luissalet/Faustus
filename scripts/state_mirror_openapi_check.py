"""List the /api/state routes as the REAL app publishes them.

`import app` is the point of this script, not a convenience: a router that is
written, tested and never registered is the failure mode this repo has hit
before -- a whole subsystem green in its own suite and invisible over HTTP. So
this imports the actual application, reads the OpenAPI document FastAPI built
from it, and prints every state path with its methods. Nothing is mocked.

    venv\\Scripts\\python.exe scripts\\state_mirror_openapi_check.py

Exit code 0 when every path section 17 asks for is present, 1 otherwise, so it
can be used as a check and not only as a report.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Section 17's list, verbatim. `{entity_id}` is declared in the router as
# `{entity_id:path}` -- an entity id is `<kind>://<owner>/<namespace>/<ident>`
# and carries slashes -- and FastAPI publishes the converter-free spelling, so
# this is what the document says and what a client sees.
EXPECTED = [
    ("GET", "/api/state/entities"),
    ("GET", "/api/state/entities/{entity_id}"),
    ("GET", "/api/state/entities/{entity_id}/history"),
    ("GET", "/api/state/changes"),
    ("GET", "/api/state/conflicts"),
    ("POST", "/api/state/refresh"),
    ("POST", "/api/state/reconcile"),
    ("GET", "/api/state/diagnostics"),
    ("GET", "/api/state/events"),
    ("GET", "/api/state/situation/{name}"),
]

#: Routes this build publishes that section 17 does not list. Declared rather
#: than reported as a surprise, and empty today: everything the router answers
#: is in the plan's own list.
BEYOND_PLAN: list = []


def main() -> int:
    import app as application  # noqa: F401 - the import IS the test

    spec = application.app.openapi()
    found = set()
    print("state routes in the OpenAPI document of the real app:\n")
    for path, operations in sorted(spec.get("paths", {}).items()):
        if not path.startswith("/api/state"):
            continue
        for method in sorted(operations):
            if method.upper() not in ("GET", "POST", "PATCH", "DELETE", "PUT"):
                continue
            found.add((method.upper(), path))
            print(f"  {method.upper():7s} {path}")

    missing = [row for row in EXPECTED if row not in found]
    beyond = [row for row in BEYOND_PLAN if row in found]
    undeclared = sorted(found - set(EXPECTED) - set(BEYOND_PLAN))
    print()
    if beyond:
        print("beyond plan section 17, declared in BEYOND_PLAN:")
        for method, path in beyond:
            print(f"  {method:7s} {path}")
    if undeclared:
        print("UNDECLARED (neither in plan section 17 nor in BEYOND_PLAN):")
        for method, path in undeclared:
            print(f"  {method:7s} {path}")
    if missing:
        print("MISSING:")
        for method, path in missing:
            print(f"  {method:7s} {path}")
        return 1
    print(f"all {len(EXPECTED)} routes of plan section 17 are published by the real app.")

    # The deep link too: a screen that 404s on reload is a screen nobody can
    # bookmark, and the whitelist in app.py is the only thing that serves it.
    served = {r.path for r in application.app.routes if getattr(r, "path", "")}
    print(f"/state deep link served by app.py: {'/state' in served}")

    from src.settings import DEFAULT_SETTINGS
    from src.agent_settings_schema import schema_keys, schema_problems

    print(f"agent_state_mirror default: {DEFAULT_SETTINGS.get('agent_state_mirror')!r}")
    print(f"agent_state_mirror_sweep_seconds default: "
          f"{DEFAULT_SETTINGS.get('agent_state_mirror_sweep_seconds')!r}")
    keys = schema_keys()
    print(f"agent_state_mirror in the settings schema: {'agent_state_mirror' in keys}")
    print(f"agent_state_mirror_sweep_seconds in the settings schema: "
          f"{'agent_state_mirror_sweep_seconds' in keys}")
    print(f"schema problems: {schema_problems() or 'none'}")
    return 0 if "/state" in served else 1


if __name__ == "__main__":
    raise SystemExit(main())
