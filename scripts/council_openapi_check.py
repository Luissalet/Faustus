"""List the /api/council routes as the REAL app publishes them.

`import app` is the point of this script, not a convenience: a router that is
written, tested and never registered is the failure mode this repo has hit
before — a whole subsystem green in its own suite and invisible over HTTP. So
this imports the actual application, reads the OpenAPI document FastAPI built
from it, and prints every council path with its methods. Nothing is mocked.

    venv\\Scripts\\python.exe scripts\\council_openapi_check.py

Exit code 0 when every path §13 asks for is present, 1 otherwise, so it can be
used as a check and not only as a report.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# §13's list, verbatim. The `{id}` of the plan is `{session_id}` in the router.
EXPECTED = [
    ("POST", "/api/council"),
    ("GET", "/api/council"),
    ("GET", "/api/council/config"),
    ("GET", "/api/council/{session_id}"),
    ("PATCH", "/api/council/{session_id}"),
    ("DELETE", "/api/council/{session_id}"),
    ("POST", "/api/council/{session_id}/messages"),
    ("GET", "/api/council/{session_id}/messages"),
    ("POST", "/api/council/{session_id}/commands"),
    ("GET", "/api/council/{session_id}/events"),
    ("GET", "/api/council/{session_id}/wait"),
    ("GET", "/api/council/{session_id}/ledger"),
    ("GET", "/api/council/{session_id}/tasks"),
    ("GET", "/api/council/{session_id}/decisions"),
    ("GET", "/api/council/{session_id}/usage"),
]

#: Routes this build publishes that §13 does not list. Declared rather than
#: reported as a surprise: `state` is the only way anything outside the process
#: can read `CouncilOrchestrator.state()`, which the plan requires the
#: coordinator to compute and gives no route for.
BEYOND_PLAN = [
    ("GET", "/api/council/{session_id}/state"),
]


def main() -> int:
    import app as application  # noqa: F401 - the import IS the test

    spec = application.app.openapi()
    found = set()
    print("council routes in the OpenAPI document of the real app:\n")
    for path, operations in sorted(spec.get("paths", {}).items()):
        if not path.startswith("/api/council"):
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
        print("beyond plan §13, declared in BEYOND_PLAN:")
        for method, path in beyond:
            print(f"  {method:7s} {path}")
    if undeclared:
        print("UNDECLARED (neither in plan §13 nor in BEYOND_PLAN):")
        for method, path in undeclared:
            print(f"  {method:7s} {path}")
    if missing:
        print("MISSING:")
        for method, path in missing:
            print(f"  {method:7s} {path}")
        return 1
    print(f"all {len(EXPECTED)} routes of plan §13 are published by the real app.")

    from src.settings import DEFAULT_SETTINGS
    from src.agent_settings_schema import schema_keys, schema_problems

    print(f"agent_council default: {DEFAULT_SETTINGS.get('agent_council')!r}")
    print(f"agent_council in the settings schema: {'agent_council' in schema_keys()}")
    print(f"schema problems: {schema_problems() or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
