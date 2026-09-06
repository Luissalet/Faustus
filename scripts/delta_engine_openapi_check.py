"""List the /api/deltas routes as the REAL app publishes them.

`import app` is the point of this script, not a convenience: a router that is
written, tested and never registered is the failure mode this repository has hit
in five consecutive plans -- a whole subsystem green in its own suite and
invisible over HTTP. So this imports the actual application, reads the OpenAPI
document FastAPI built from it, and prints every delta path with its methods.
Nothing is mocked.

    venv\\Scripts\\python.exe scripts\\delta_engine_openapi_check.py

Exit code 0 when every path section 23 asks for is present, 1 otherwise, so it
can be used as a check and not only as a report.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Section 23's list, minus the four this phase deliberately did not build.
#: `/cancel` and `/wait` belong to an async comparison this build does not have
#: -- a comparison here is synchronous and returns its delta -- and `/verify`
#: is `prove`'s job, reached through `integrations/prove.py` rather than through
#: an endpoint that would look like a second authority.
EXPECTED = [
    ("GET", "/api/deltas"),
    ("POST", "/api/deltas"),
    ("GET", "/api/deltas/{delta_id}"),
    ("POST", "/api/deltas/{delta_id}/run"),
    ("POST", "/api/deltas/{delta_id}/reclassify"),
    ("GET", "/api/deltas/{delta_id}/evidence"),
    ("GET", "/api/deltas/events"),
    ("POST", "/api/deltas/intent/compile"),
    ("GET", "/api/deltas/profiles"),
    ("GET", "/api/deltas/extractors/status"),
]

#: Published by this build and not listed in section 23. Declared rather than
#: reported as a surprise: `/config` serves the closed vocabularies so the page
#: keeps no copy of its own, `/diagnostics` matches the mirror's, and
#: `/invalidate` is §3.6's other half -- retiring a delta whose revisions turned
#: out not to be immutable.
BEYOND_PLAN = [
    ("GET", "/api/deltas/config"),
    ("GET", "/api/deltas/diagnostics"),
    ("POST", "/api/deltas/{delta_id}/invalidate"),
]


def main() -> int:
    import app as application  # noqa: F401 - the import IS the test

    spec = application.app.openapi()
    found = set()
    print("delta routes in the OpenAPI document of the real app:\n")
    for path, operations in sorted(spec.get("paths", {}).items()):
        if not path.startswith("/api/deltas"):
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
        print("beyond plan section 23, declared in BEYOND_PLAN:")
        for method, path in beyond:
            print(f"  {method:7s} {path}")
    if undeclared:
        print("UNDECLARED (neither in plan section 23 nor in BEYOND_PLAN):")
        for method, path in undeclared:
            print(f"  {method:7s} {path}")
    if missing:
        print("MISSING:")
        for method, path in missing:
            print(f"  {method:7s} {path}")
        return 1
    print(f"all {len(EXPECTED)} routes of plan section 23 are published by the real app.")

    # The deep link too: a screen that 404s on reload is a screen nobody can
    # bookmark, and app.py's own `@app.get` is the only thing that serves it.
    served = {r.path for r in application.app.routes if getattr(r, "path", "")}
    print(f"/deltas deep link served by app.py: {'/deltas' in served}")

    # And the domains this machine can actually compare. A registry that
    # discovers by walking the package can still come back empty if an adapter
    # module fails to import, and an empty registry is a subsystem that answers
    # every request with `adapter_unavailable`.
    from src.delta_engine import registry

    print("\nadapters discovered:")
    for domain, row in sorted(registry.status().items()):
        state = "available" if row.get("available") else "UNAVAILABLE"
        reason = f"  ({row.get('reason')})" if row.get("reason") else ""
        print(f"  {domain:10s} {state:12s} v{row.get('version', '?')}{reason}")

    from src.agent_settings_schema import schema_keys, schema_problems
    from src.settings import DEFAULT_SETTINGS

    print()
    keys = set(schema_keys())
    for key in sorted(k for k in DEFAULT_SETTINGS if k.startswith("agent_delta_engine")):
        print(f"{key}: default={DEFAULT_SETTINGS[key]!r} in schema={key in keys}")
    print(f"schema problems: {schema_problems() or 'none'}")

    usable = [d for d, row in registry.status().items() if row.get("available")]
    return 0 if ("/deltas" in served and usable) else 1


if __name__ == "__main__":
    raise SystemExit(main())
