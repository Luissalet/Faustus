"""Does the Delta Engine actually reach the product? Nothing else.

Five plans in a row have shipped a subsystem that was correct, tested and
UNREACHABLE. Plan 1 registered six context sources nowhere. Plan 2 left
`updated_at` out of the patchable fields the service was sending. Plan 3 wrote
ten agent profiles that `builtins()` did not serve. Plan 4 had a ledger writing
to a list nobody read, and five more. Plan 5 had five adapters missing from the
one tuple that makes an adapter exist.

Every one of those had green tests, because each agent tested its own module
against its own module. This file asks the only question those cannot: do the
modules reach EACH OTHER, and does anything outside this package know they are
here. It is deliberately dumb -- it reads source, walks packages and compares
registrations -- and it is the file to extend when the next gap is found.

The rule for everything in here: assert the GUARANTEE, never the inventory. A
test that pins the exact set of adapters fails the day a seventh is added, and
the reflex fix is to shrink the set -- which is how a green suite is bought by
disconnecting working code.
"""
from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

from src.contracts.event import EVENT_NAMES
from src.delta_engine import contracts as dc
from src.delta_engine import events as de_events
from src.delta_engine import registry, service as de_service

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "delta_engine"


def _source(*parts: str) -> str:
    return (REPO.joinpath(*parts)).read_text(encoding="utf-8")


def _tree(*parts: str) -> ast.AST:
    return ast.parse(_source(*parts))


# -- the event vocabulary ---------------------------------------------------


def test_every_event_this_package_declares_is_one_the_envelope_will_accept():
    """`DELTA_EVENTS` must stay a SUBSET of `EVENT_NAMES`.

    A name declared here and missing there reaches a page perfectly and is then
    refused by `Event.parse` when an audit replays it through the envelope --
    so the failure appears months later, in the one place that exists to
    reconstruct what happened.
    """
    missing = sorted(set(de_events.DELTA_EVENTS) - set(EVENT_NAMES))
    assert not missing, (
        f"{missing} are published by src/delta_engine/events.py and are not in "
        f"EVENT_NAMES; add them to src/contracts/event.py")


def test_every_event_name_the_code_publishes_is_declared():
    """Read the `_emit(...)` and `publish(...)` literals out of the source.

    This catches the paths no test happens to drive: a comparison that only
    goes wrong on a machine with a broken parser still emits, and an
    undeclared name there is a silent `delta_error` in production.

    Scans ALL positional arguments and the `name=` keyword, not `args[0]`:
    getting that wrong is how the council's version of this test passed while
    reading nothing, because the real publisher takes the name third.
    """
    declared = set(de_events.DELTA_EVENTS)
    emitted: set = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name not in ("_emit", "publish"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.startswith("delta_"):
                        emitted.add(arg.value)
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str) and kw.value.value.startswith("delta_"):
                        emitted.add(kw.value.value)
    undeclared = sorted(emitted - declared)
    assert not undeclared, (
        f"{undeclared} are emitted in src/delta_engine/ and are not in "
        f"DELTA_EVENTS; they will publish as delta_error")


# -- adapters ---------------------------------------------------------------


def test_every_adapter_module_declares_the_symbol_the_registry_looks_for():
    """Walk the package. A module that forgets `ADAPTER_FACTORY` does not exist.

    This is the plan-5 failure with the list removed: there is no tuple to be
    left out of, so the only way to write an invisible adapter is to omit the
    module-level symbol -- and this walks the package rather than reading a
    list, because a list is exactly what was wrong last time.
    """
    missing = []
    for info in pkgutil.iter_modules([str(PACKAGE / "adapters")]):
        if info.name in registry.SKIPPED_MODULES or info.name.startswith("_"):
            continue
        module = importlib.import_module(f"src.delta_engine.adapters.{info.name}")
        if not hasattr(module, registry.ADAPTER_ATTR):
            missing.append(info.name)
    assert not missing, (
        f"{missing} live in src/delta_engine/adapters/ and declare no "
        f"{registry.ADAPTER_ATTR}; the registry cannot see them and they are "
        f"code nobody can reach")


def test_every_discovered_adapter_answers_the_protocol_the_service_calls():
    """A domain in the registry has to survive the calls `service._run` makes.

    Not `isinstance(..., DeltaAdapter)`: a runtime-checkable Protocol only
    checks that the attributes exist, and the failure this guards against is a
    renamed method -- exactly the kind a `try/except` swallows.
    """
    required = ("domain", "version", "available", "snapshot", "compare",
                "check_invariants")
    for adapter in registry.adapters(refresh=True):
        for name in required:
            assert hasattr(adapter, name), (
                f"{type(adapter).__name__} has no {name}; service._run calls it")
        assert adapter.domain in dc.DOMAINS, (
            f"{type(adapter).__name__} claims domain {adapter.domain!r}, which "
            f"is not in contracts.DOMAINS and can never be requested")
        assert str(adapter.version), (
            f"{type(adapter).__name__} has an empty version; the cache cannot "
            f"tell an old conclusion from a new one without it")


def test_at_least_the_domains_the_product_advertises_can_be_compared():
    """A subset check, and the docstring says why it is not an equality.

    Written as `>=` on purpose. An equality here fails the day a seventh
    adapter is added, and the reflex fix -- deleting the new name from the
    expectation, or worse from the registry -- would disconnect working code to
    make a test green. What must never regress is that these four, which the
    Studio screen and the settings copy both promise, still answer.
    """
    available = {a.domain for a in registry.adapters(refresh=True) if a.available()}
    promised = {"code", "document", "workflow", "state"}
    assert promised <= available, f"missing adapters for {sorted(promised - available)}"


# -- settings ---------------------------------------------------------------


def test_every_delta_setting_is_in_both_places_it_has_to_be():
    """`DEFAULT_SETTINGS` and `agent_settings_schema` must agree.

    The repository already enforces this globally, and it is repeated here
    scoped to this subsystem so the failure names the Delta Engine instead of
    arriving as a generic parity error somebody else has to bisect.
    """
    from src.agent_settings_schema import schema_keys
    from src.settings import DEFAULT_SETTINGS

    keys = {k for k in DEFAULT_SETTINGS if k.startswith("agent_delta_engine")}
    assert keys, "no agent_delta_engine* setting exists at all"
    missing = sorted(keys - set(schema_keys()))
    assert not missing, f"{missing} have no entry in src/agent_settings_schema.py"


def test_the_switch_gates_running_and_never_reading():
    """The two halves of the flag, asserted on the service, not on prose.

    `create`, `run` and `compile_intent` cost this machine a re-read of both
    revisions; `get`, `list` and `evidence` cost nothing and answer a question
    about a conclusion already recorded. A flag that hid the second would be a
    delete wearing the clothes of a setting.
    """
    source = _source("src", "delta_engine", "service.py")
    tree = ast.parse(source)
    gated = {"create", "run", "compile_intent"}
    ungated = {"get", "list", "evidence", "diagnostics", "config", "extractors"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in (gated | ungated):
            continue
        calls = {n.func.id for n in ast.walk(node)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if node.name in gated:
            assert "enabled" in calls, (
                f"service.{node.name} does not read the switch; it would run a "
                f"comparison the operator switched off")
        else:
            body = ast.get_source_segment(source, node) or ""
            assert "code=\"disabled\"" not in body, (
                f"service.{node.name} refuses when the switch is off; reading a "
                f"delta already stored must never be gated")


# -- routes -----------------------------------------------------------------


def test_the_router_is_registered_in_app():
    """A router nobody includes is a subsystem with no product in front of it."""
    source = _source("app.py")
    assert "setup_delta_engine_routes" in source, (
        "routes/delta_engine_routes.py is never imported by app.py")
    assert "app.include_router(setup_delta_engine_routes())" in source, (
        "setup_delta_engine_routes is imported and never included")


def test_the_literal_paths_are_declared_before_the_id_parameter():
    """FastAPI matches in declaration order. `/{delta_id}` first eats them all.

    Read out of the source rather than off the router, because the router's
    own ordering is what is being checked and asking it would be asking the
    accused. `/config` declared after `/{delta_id}` is a 404 for a route that
    exists, which is the single most confusing failure this file can prevent.
    """
    tree = _tree("routes", "delta_engine_routes.py")
    order = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            if not (isinstance(deco.func, ast.Attribute)
                    and getattr(deco.func.value, "id", "") == "router"):
                continue
            if deco.args and isinstance(deco.args[0], ast.Constant):
                order.append((deco.args[0].value, node.lineno))
    literal = [(p, line) for p, line in order if p and "{" not in p and p != ""]
    parameterised = [line for p, line in order if "{delta_id}" in p]
    assert literal and parameterised, "the router lost its shape"
    first_param = min(parameterised)
    late = sorted(p for p, line in literal if line > first_param)
    assert not late, (
        f"{late} are declared after /{{delta_id}}; FastAPI will answer 404 for "
        f"routes that exist")


def test_the_two_expensive_endpoints_do_not_run_on_the_event_loop():
    """A comparison must not be executed inside an `async def` handler.

    Found in the browser and by nothing else: pointed at two checkpoints of
    this repository, one comparison held the event loop for minutes, and the
    symptom was not "deltas are slow" -- it was that EVERY request in the
    application queued behind it, the page showed skeletons forever and the
    server log stopped. A test never has a second request, so no test could
    have seen it.

    Asserted structurally, on the source, because the fix is one `await
    _offloaded(...)` that a future edit can undo without breaking anything
    visible in a unit test.
    """
    tree = _tree("routes", "delta_engine_routes.py")
    for name in ("create_delta", "run_delta"):
        node = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.AsyncFunctionDef) and n.name == name), None)
        assert node is not None, f"{name} is gone from the router"
        offloaded = any(
            isinstance(call.func, ast.Name) and call.func.id == "_offloaded"
            for call in ast.walk(node) if isinstance(call, ast.Call))
        assert offloaded, (
            f"{name} calls the service inline; a comparison there blocks the "
            f"event loop for the whole application")


def test_the_route_refuses_with_the_shape_the_house_uses():
    """`{"ok": false, "error": {path, message, code}}`, not the older dialect.

    `routes/changesets_routes.py` still speaks `{"ok": false, "field",
    "reason"}`, and this subsystem sits right next to ChangeSets -- copying the
    nearer file rather than the newer convention was a live risk.
    """
    source = _source("routes", "delta_engine_routes.py")
    assert '"path": path' in source and '"message": message' in source
    assert '"field"' not in source and '"reason": ' not in source, (
        "the older refusal dialect leaked into the delta routes")


def test_the_routes_catch_the_parent_error_class():
    """`except DeltaError` misses every rejection from the shared helpers.

    `DeltaError` subclasses `ContractError`, and `contracts/base.py` -- which
    is what parses every field of every payload -- raises the parent. Catching
    only the child answered 500 to `POST /api/deltas` with a missing `source`,
    which is the commonest caller mistake there is.
    """
    source = _source("routes", "delta_engine_routes.py")
    assert "except DeltaError as exc" not in source, (
        "a handler catches DeltaError instead of ContractError; a malformed "
        "body will answer 500 instead of a 200 rejection")
    assert "except ContractError as exc" in source


# -- the Studio screen ------------------------------------------------------


@pytest.mark.parametrize("where,needle", [
    ("studio/src/shell/AppShell.tsx", "screens/Deltas"),
    ("studio/src/shell/AppShell.tsx", 'path="/deltas"'),
    ("studio/src/shell/routes.ts", "'/deltas'"),
    ("app.py", '@app.get("/deltas")'),
])
def test_the_screen_is_registered_in_every_place_it_has_to_be(where, needle):
    """Four registrations, and the two that are always forgotten.

    `SERVER_ROUTES` in `routes.ts`: a route the server does not serve is a 404
    on reload. The deep link in `app.py`: the same symptom from the other side.
    A screen that is lazy-imported and never routed renders nothing at all and
    breaks no build.
    """
    assert needle in _source(*where.split("/")), f"{needle} missing from {where}"


def test_the_screen_has_its_own_pure_logic_check():
    """The adapter is testable without a browser, and something runs it."""
    assert (REPO / "studio" / "checks" / "deltas.check.mjs").exists()
    assert (REPO / "tests" / "test_studio_deltas_js.py").exists()
    assert (REPO / "studio" / "src" / "adapters" / "deltas.ts").exists()


# -- the Context Engine -----------------------------------------------------


def test_a_delta_is_reachable_as_context():
    """`SOURCE_TYPES` reserved `"delta"` long before anything filled it.

    A reserved word is the most convincing kind of unwired: `/context` reports
    the type as declared, and nothing anywhere serves it. This asserts the four
    registrations that make a `ContextSource` real, and that the two lists which
    have to agree do agree -- the exact disagreement that produced "6 declared
    sources are not available".
    """
    from src.context_engine import candidates, planner
    from src.context_engine.contracts import SECTION_KINDS, SOURCE_TYPES

    assert "delta" in SOURCE_TYPES
    # `default_sources()` is what builds them; `registered_sources()` alone is
    # empty until something has. Asking the builder is the honest question --
    # "is this source among the ones the compiler constructs" -- and it is what
    # was false in plan 1, where the classes existed and the factory list did
    # not mention them.
    built = {getattr(s, "source_id", "") for s in candidates.default_sources()}
    assert "deltas" in built, (
        "the delta source is not in SOURCE_FACTORIES; the compiler cannot see it")
    assert "deltas" in set(candidates.registered_sources()), (
        "default_sources() built the delta source and did not register it")
    assert "deltas" in planner.SOURCE_SECTIONS, (
        "planner.SOURCE_SECTIONS does not know the delta source; it will never "
        "be planned into a section")
    for section in planner.SOURCE_SECTIONS["deltas"]:
        assert section in SECTION_KINDS, (
            f"the delta source claims section {section!r}, which is not a "
            f"SECTION_KIND and will be dropped silently")
    source = candidates.get_source("deltas")
    assert source is not None
    assert any(h.startswith("delta:") for h in getattr(source, "handles", ())), (
        "the delta source handles no `delta:` refs, so UniversalDeltaRef."
        "source_ref() addresses nobody")


# -- the integrations -------------------------------------------------------


def test_the_two_verdict_vocabularies_never_become_each_other():
    """`assessment` and `prove.VERDICTS` share `partial` and nothing else.

    The council needed an explicit map for the same reason and this one is
    checked, not assumed: every ceiling this module can impose has to be a real
    assessment, and no proof verdict may raise one.
    """
    from src.delta_engine.integrations import prove as prove_int

    for verdict, ceiling in prove_int.ASSESSMENT_CEILING.items():
        assert verdict in prove_int.PROOF_VERDICTS, f"{verdict} is not a prove verdict"
        assert ceiling == "" or ceiling in dc.ASSESSMENTS, (
            f"the ceiling for {verdict} is {ceiling!r}, which is not an assessment")
    # A proof can only ever lower. `proved` imposes nothing; nothing promotes.
    assert prove_int.ASSESSMENT_CEILING["proved"] == ""
    for name in dc.ASSESSMENTS:
        capped, _reason = prove_int.cap(name, {"verdict": "proved"})
        assert capped == name, "a proof raised an assessment"


def test_the_changeset_bridge_uses_the_strictest_intent():
    """`implement`, always -- a read-only intent refuses any changed file.

    `contracts/changeset.py` rejects a changed path under `explore`, `plan` or
    `review`, so a bridge built with one of those would raise on exactly the
    deltas that matter. The council took the same decision and called it the
    strictest of them.
    """
    from src.contracts.changeset import READ_ONLY_INTENTS
    from src.delta_engine.integrations import changesets as cs_int

    assert cs_int.CHANGESET_INTENT not in READ_ONLY_INTENTS


def test_the_service_freezes_the_intent_before_it_reads_anything():
    """The order in `_run`, read out of the source. §1.9.1.

    A contract compiled after the target was seen can be compiled to match it,
    and then every evaluation scores full marks. `service.create` calls
    `_intent_for` before `_run`, and `_run` calls `_snapshot` after; this reads
    the line numbers rather than trusting the docstring, because a refactor
    that reorders them breaks nothing else and nothing else would notice.
    """
    source = _source("src", "delta_engine", "service.py")
    tree = ast.parse(source)
    create = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "create")
    calls = [(getattr(n.func, "attr", ""), n.lineno) for n in ast.walk(create)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    intent_line = min((line for name, line in calls if name == "_intent_for"),
                      default=None)
    run_line = min((line for name, line in calls if name == "_run"), default=None)
    assert intent_line is not None and run_line is not None
    assert intent_line < run_line, (
        "service.create runs the comparison before freezing the intent")


def test_the_package_import_registers_the_schema():
    """Importing the package must create the tables, flag or no flag.

    The schema is registered as a side effect of importing `persistence`. If
    the only importer were a route behind a switch that is off, the tables
    would never exist on a machine where nobody turned it on -- and the first
    thing that happened after they did would be a failure.
    """
    import src.delta_engine as pkg
    from src.delta_engine import persistence

    assert "persistence" in dir(pkg)
    assert persistence.registered_schemas(), "no schema was registered on import"


def test_the_service_answers_every_vocabulary_the_screen_reads():
    """`config()` is what stops the front end keeping its own copy of a list.

    A page with its own vocabulary drifts the day a word is added, and the
    symptom is a row that renders blank rather than an error anyone notices.
    """
    answer = de_service.service().config()
    for key in ("domains", "assessments", "classifications", "operations",
                "severities", "confidence", "tiers", "invariant_classes",
                "invariant_statuses", "coverage_dimensions", "condition_kinds"):
        assert answer.get(key), f"config() has no {key}"
    assert set(answer["assessments"]) == set(dc.ASSESSMENTS)
    assert set(answer["classifications"]) == set(dc.CLASSIFICATIONS)
