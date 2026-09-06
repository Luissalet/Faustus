"""List the /api/completion routes as the REAL app publishes them.

`import app` is the point of this script, not a convenience: a router that is
written, tested and never registered is the failure mode this repository has hit
in six consecutive plans -- a whole subsystem green in its own suite and
invisible over HTTP. So this imports the actual application, reads the OpenAPI
document FastAPI built from it, and prints every completion path with its
methods. Nothing is mocked.

    venv\\Scripts\\python.exe scripts\\completion_engine_openapi_check.py

Exit code 0 when every path the plan asks for is present, 1 otherwise, so it can
be used as a check and not only as a report.

It also prints the absence this subsystem is defined by. There is no endpoint
that RUNS the engine: it decides inside a turn, at the moment the model stops
calling tools, with that turn's ledger, proof and budget behind it, and a
`POST /run` would be a second door to the same answer with none of those behind
it. Every other engine here has one, so the absence is reported out loud rather
than left to be read as an oversight.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The plan's list. Eight reads and one write, and no way to run anything.
EXPECTED = [
    ("GET", "/api/completion"),
    ("GET", "/api/completion/modes"),
    ("GET", "/api/completion/config"),
    ("GET", "/api/completion/settings"),
    ("GET", "/api/completion/diagnostics"),
    ("GET", "/api/completion/events"),
    ("GET", "/api/completion/{decision_id}"),
    ("POST", "/api/completion/{decision_id}/reject-improvement"),
]

#: Published by this build and not named in the plan. Declared rather than
#: reported as a surprise -- nothing here yet, and the list stays so the next
#: route that appears has somewhere honest to be written down instead of
#: quietly widening EXPECTED.
BEYOND_PLAN: list = []


def main() -> int:
    import app as application  # noqa: F401 - the import IS the test

    spec = application.app.openapi()
    found = set()
    print("completion routes in the OpenAPI document of the real app:\n")
    for path, operations in sorted(spec.get("paths", {}).items()):
        if not path.startswith("/api/completion"):
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
        print("beyond the plan, declared in BEYOND_PLAN:")
        for method, path in beyond:
            print(f"  {method:7s} {path}")
    if undeclared:
        print("UNDECLARED (neither in the plan nor in BEYOND_PLAN):")
        for method, path in undeclared:
            print(f"  {method:7s} {path}")
    if missing:
        print("MISSING:")
        for method, path in missing:
            print(f"  {method:7s} {path}")
        return 1
    print(f"all {len(EXPECTED)} routes of the plan are published by the real app.")

    # The absence, asserted out loud. A `/run` here would produce an answer
    # indistinguishable from one a real turn took -- same store, same list,
    # same shape -- with no ledger, no proof and no budget behind it.
    runs = sorted(p for _m, p in found if p.rstrip("/").endswith("/run"))
    posts_to_collection = [row for row in found if row == ("POST", "/api/completion")]
    print(f"no endpoint runs the engine: {not runs and not posts_to_collection}")
    if runs or posts_to_collection:
        print(f"  SECOND DOOR: {runs or posts_to_collection}")
        return 1

    # The hook, which has no HTTP surface at all and is therefore the half most
    # likely to have been written and never called. Read out of the source: the
    # loop is one async generator nothing outside a live turn can drive.
    loop = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "agent_loop.py")
    with open(loop, "r", encoding="utf-8") as handle:
        loop_source = handle.read()
    print("\nagent_loop hook:")
    for label, needle in (
        ("_CE_MAX_ROUNDS declared", "_CE_MAX_ROUNDS"),
        ("per-turn counter reset", "_ce_completion_rounds = 0"),
        ("consulted behind active()", "_ce_service.active()"),
        ("offloaded to a thread", "await asyncio.to_thread("),
        ("decide_for_turn called", "decide_for_turn"),
        ("frame yielded", '"type": "completion_decision"'),
    ):
        print(f"  {label:28s} {needle in loop_source}")

    from src.completion_engine import events as ce_events
    from src.completion_engine import persistence, service as ce_service
    from src.contracts.event import EVENT_NAMES

    undeclared_events = sorted(set(ce_events.COMPLETION_EVENTS) - set(EVENT_NAMES))
    print(f"\nevent names outside EVENT_NAMES: {undeclared_events or 'none'}")
    print(f"schemas registered on import: {list(persistence.registered_schemas())}")

    from src.agent_settings_schema import schema_keys, schema_problems
    from src.settings import DEFAULT_SETTINGS

    print()
    keys = set(schema_keys())
    completion_keys = sorted(k for k in DEFAULT_SETTINGS if k.startswith("agent_completion"))
    for key in completion_keys:
        print(f"{key}: default={DEFAULT_SETTINGS[key]!r} in schema={key in keys}")
    print(f"schema problems: {schema_problems() or 'none'}")
    print(f"engine enabled={ce_service.enabled()} "
          f"shadow={ce_service.shadow_enabled()} active={ce_service.active()}")

    ok = (not undeclared_events
          and completion_keys
          and all(k in keys for k in completion_keys))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
