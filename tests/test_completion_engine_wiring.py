"""Does the Greedy Completion Engine actually reach the product? Nothing else.

Six plans in a row have shipped a subsystem that was correct, tested and
UNREACHABLE. A router written and never registered. An adapter written and
never added to the one tuple that makes an adapter exist. Ten agent profiles
that `builtins()` did not serve. A ledger writing to a list nobody read. Every
one of them was green in its own suite, because each agent tested its own
module against its own module, and every one of them was invisible in the
running product.

This file asks the only question those suites cannot: do the modules reach EACH
OTHER, and does anything outside this package know they are here. It is
deliberately dumb -- it imports the REAL app, reads the REAL OpenAPI document,
and parses the REAL source with `ast` wherever importing would be a lie -- and
it is the file to extend when the next gap is found.

`ast` and not import, in most of it, for a specific reason: the agent loop is
one 3000-line async generator that cannot be called from a test, and the
questions worth asking about it are structural -- is the hook BEFORE the break,
is the call OFFLOADED, is the counter reset per turn. A refactor can undo any of
those without breaking a single unit test, and the symptom of each is invisible
until a user is waiting.

The rule for everything in here: assert the GUARANTEE, never the inventory. A
test that pins an exact set fails the day a ninth route is added, and the reflex
fix is to shrink the set -- which is how a green suite is bought by
disconnecting working code.
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path
from typing import Iterator, List

import pytest

from src.completion_engine import events as ce_events
from src.completion_engine import service as ce_service
from src.contracts.event import EVENT_NAMES

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "completion_engine"

#: The eight paths this router publishes, with the methods each answers. A
#: dict and not a set of paths: `/{decision_id}` answering POST instead of GET
#: would satisfy a path-only check and 405 every caller.
EXPECTED_ROUTES = {
    "/api/completion": {"get"},
    "/api/completion/modes": {"get"},
    "/api/completion/config": {"get"},
    "/api/completion/settings": {"get"},
    "/api/completion/diagnostics": {"get"},
    "/api/completion/events": {"get"},
    "/api/completion/{decision_id}": {"get"},
    "/api/completion/{decision_id}/reject-improvement": {"post"},
}


def _source(*parts: str) -> str:
    """`utf-8-sig`, because `src/completion_engine/scope.py` carries a BOM.

    Python imports a BOM'd file without complaint and `ast.parse` refuses it,
    so reading these as plain utf-8 makes a source-reading test fail on a file
    the product is perfectly happy with -- a false finding, which is worse than
    no finding.
    """
    return (REPO.joinpath(*parts)).read_text(encoding="utf-8-sig")


@lru_cache(maxsize=None)
def _tree(*parts: str) -> ast.Module:
    """Parsed once. `src/agent_loop.py` is 8600 lines and nine tests read it."""
    return ast.parse(_source(*parts))


def _blocks(tree: ast.AST) -> Iterator[List[ast.stmt]]:
    """Every statement list in the tree, so a SIBLING relation can be asserted.

    "The hook is before the break" is a claim about two statements in one
    block, and `ast.walk` flattens exactly that away.
    """
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block and all(
                    isinstance(stmt, ast.stmt) for stmt in block):
                yield block


def _assigns(node: ast.AST, name: str) -> bool:
    """Does this subtree assign `name` anywhere?"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assign):
            for target in sub.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return True
        if isinstance(sub, ast.AugAssign):
            if isinstance(sub.target, ast.Name) and sub.target.id == name:
                return True
    return False


@pytest.fixture(scope="module")
def spec():
    """The OpenAPI document of the REAL application.

    `import app` is the test, not a convenience: it is the step every one of
    the six unreachable subsystems skipped. It costs about forty seconds and
    connects to ChromaDB, which is why it is module-scoped and paid once.
    """
    import app as application  # noqa: F401 - the import IS the test

    return application.app.openapi()


# -- the router reaches the app ---------------------------------------------


@pytest.mark.parametrize("path,methods", sorted(EXPECTED_ROUTES.items()))
def test_every_route_is_published_by_the_real_app(spec, path, methods):
    """Asked of the running application, not of the router object.

    A router answers about itself whether or not anybody included it, which is
    precisely the question that was not asked six times. This one can only be
    answered by an app that imported the module, called the factory and
    included the result.
    """
    published = spec.get("paths", {}).get(path)
    assert published is not None, (
        f"{path} is not in the OpenAPI document of the real app; the router is "
        f"written and nothing reaches it")
    served = {m.lower() for m in published
              if m.lower() in ("get", "post", "put", "patch", "delete")}
    assert methods <= served, (
        f"{path} answers {sorted(served)} and not {sorted(methods)}; a caller "
        f"gets 405 from a route that exists")


def test_app_py_names_the_factory_and_includes_what_it_returns(spec):
    """Read out of `app.py`, because the OpenAPI doc cannot tell WHO published.

    Another router with the same prefix would satisfy the test above and leave
    `setup_completion_engine_routes` as dead code -- unlikely, and it is the
    exact class of mistake this file exists for, so it is cheap to exclude.
    """
    source = _source("app.py")
    assert "from routes.completion_engine_routes import setup_completion_engine_routes" in source, (
        "routes/completion_engine_routes.py is never imported by app.py")
    assert "app.include_router(setup_completion_engine_routes())" in source, (
        "setup_completion_engine_routes is imported and never included; the "
        "import alone publishes nothing")


def test_the_literal_paths_are_declared_before_the_id_parameter():
    """FastAPI matches in declaration order. `/{decision_id}` first eats them all.

    Read out of the source rather than off the router, because the router's own
    ordering is what is being checked and asking it would be asking the
    accused. `/config` declared after `/{decision_id}` is a 404 for a route
    that exists -- and worse here than a 404, because `/{decision_id}` would
    answer 200 with "names no decision of yours" for the word `config`.
    """
    tree = _tree("routes", "completion_engine_routes.py")
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
    literal = [(p, line) for p, line in order if p and "{" not in p]
    parameterised = [line for p, line in order if "{decision_id}" in p]
    assert literal and parameterised, "the router lost its shape"
    first_param = min(parameterised)
    late = sorted(p for p, line in literal if line > first_param)
    assert not late, (
        f"{late} are declared after /{{decision_id}} in "
        f"routes/completion_engine_routes.py; FastAPI will never reach them")


# -- the routes catch the class the shared helpers actually raise ------------


def _handlers(tree: ast.AST):
    """Every function decorated with a `@router.<method>(...)`."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                    and getattr(deco.func.value, "id", "") == "router"):
                yield node
                break


def _caught(handler: ast.AST) -> set:
    """The exception names this handler's `except` clauses mention."""
    names = set()
    for node in ast.walk(handler):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        for sub in ast.walk(node.type):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                names.add(sub.attr)
    return names


def test_no_handler_catches_the_child_without_also_catching_the_parent():
    """`CompletionError` is a SUBCLASS of `ContractError`. Catching it is not enough.

    `src/contracts/base.py` is what parses every field of every payload, and it
    raises the PARENT. A handler that catches only `CompletionError` answers
    500 to a missing field -- the commonest caller mistake there is -- and the
    delta routes shipped exactly that bug hours before this router was written.

    A `CompletionServiceError` clause is fine and expected: it is how
    `get_decision` turns `code == "not_found"` into a real 404. What is not
    fine is that clause being the ONLY one, because then every other refusal
    from the shared helpers escapes as a 500.
    """
    tree = _tree("routes", "completion_engine_routes.py")
    offenders = []
    for handler in _handlers(tree):
        caught = _caught(handler)
        narrow = caught & {"CompletionError", "CompletionServiceError"}
        if narrow and "ContractError" not in caught:
            offenders.append(f"{handler.name} catches {sorted(narrow)} only")
    assert not offenders, (
        f"{offenders} in routes/completion_engine_routes.py; ContractError is "
        f"the parent and the shared helpers raise it, so a malformed body "
        f"answers 500 instead of a 200 rejection")


def test_at_least_one_handler_catches_the_parent_at_all():
    """The other half: a router that catches nothing passes the test above.

    Vacuous truth is how a structural assertion quietly stops asserting. If
    every `try` were deleted tomorrow the check above would be satisfied by an
    empty set, so this pins that the parent is caught SOMEWHERE.
    """
    tree = _tree("routes", "completion_engine_routes.py")
    catchers = [h.name for h in _handlers(tree) if "ContractError" in _caught(h)]
    assert catchers, (
        "no handler in routes/completion_engine_routes.py catches ContractError")


def test_the_router_refuses_in_the_dialect_the_house_uses():
    """`{"ok": false, "error": {path, message, code}}`, not the older one.

    `routes/changesets_routes.py` still speaks `{"ok": false, "field",
    "reason"}`. Copying the nearer file rather than the newer convention was a
    live risk here, and a page written against one dialect renders nothing at
    all against the other.
    """
    source = _source("routes", "completion_engine_routes.py")
    assert '"path": path' in source and '"message": message' in source
    assert '"field"' not in source, (
        "the older refusal dialect leaked into the completion routes")


# -- the agent loop hook ----------------------------------------------------
#
# This is the half that has no HTTP surface, which makes it the half most
# likely to be written and never called. Everything here is read out of
# `src/agent_loop.py` with `ast`: the hook lives inside one async generator
# that a test cannot drive, so the only honest questions are structural.


def _ce_block() -> List[ast.stmt]:
    """The statement list the completion-engine hook lives in.

    Found by the `_ce_decision = None` that opens it. Returning the whole BLOCK
    rather than the node is the point: the two things worth asserting -- that
    the hook is before the break, and that it is inside the loop -- are claims
    about siblings.
    """
    for block in _blocks(_tree("src", "agent_loop.py")):
        for stmt in block:
            if isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "_ce_decision"
                    for t in stmt.targets):
                return block
    raise AssertionError(
        "no `_ce_decision` assignment anywhere in src/agent_loop.py; the "
        "completion engine is not called from the turn at all")


def test_the_loop_declares_its_own_ceiling_on_bonus_rounds():
    """`_CE_MAX_ROUNDS` exists, is a small integer, and is not the verifier's.

    A hard cap rather than a budget, because the failure is not expense: a
    cheap improvement that reveals another cheap improvement is a loop that
    converges on nothing. Separate from `_VERIFIER_MAX_ROUNDS` because the two
    ask different questions and one shared counter would let either spend the
    other's allowance.
    """
    tree = _tree("src", "agent_loop.py")
    found = [n.value for n in tree.body
             if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "_CE_MAX_ROUNDS"
                     for t in n.targets)]
    assert found, "_CE_MAX_ROUNDS is not defined at module level in src/agent_loop.py"
    assert isinstance(found[0], ast.Constant) and isinstance(found[0].value, int), (
        "_CE_MAX_ROUNDS is not an integer literal")
    assert 0 < found[0].value <= 5, (
        f"_CE_MAX_ROUNDS is {found[0].value}; a ceiling that high is not a "
        f"ceiling, it is a budget wearing one's clothes")


def test_the_round_counter_is_reset_for_every_turn():
    """`_ce_completion_rounds` is per-TURN state, so it must live in the turn.

    A module-level counter would be shared by every concurrent stream on the
    machine and would never go back to zero: the first turn of the day spends
    the allowance and every turn after it is told it has already had its bonus
    rounds. Nothing in a single-turn test could see that.
    """
    tree = _tree("src", "agent_loop.py")
    at_module = [n for n in tree.body
                 if isinstance(n, (ast.Assign, ast.AugAssign))
                 and _assigns(n, "_ce_completion_rounds")]
    assert not at_module, (
        "_ce_completion_rounds is assigned at module level in src/agent_loop.py; "
        "it is per-turn state and would be shared by every stream")
    inside = [n.name for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and any(isinstance(s, ast.Assign)
                      and any(isinstance(t, ast.Name)
                              and t.id == "_ce_completion_rounds" for t in s.targets)
                      and isinstance(s.value, ast.Constant) and s.value.value == 0
                      for s in ast.walk(n))]
    assert inside, (
        "nothing sets _ce_completion_rounds = 0 inside a function; the counter "
        "is never reset and a turn inherits the previous turn's spend")


def test_the_decision_is_taken_off_the_event_loop():
    """`decide_for_turn` must be reached through `asyncio.to_thread`. Nothing else.

    Found in a browser and by nothing else: heavy synchronous work called
    inline from an `async def` holds the event loop for the whole process, and
    the symptom is not "the completion engine is slow" -- it is that EVERY
    request in the application queues behind it, the page shows skeletons
    forever and the server log stops. This exact bug froze the Deltas page.

    A test never has a second request, so no test could have caught it, and the
    fix is one wrapper a future edit can undo without breaking anything
    visible. So it is asserted structurally: the call has to be an ARGUMENT to
    `asyncio.to_thread`, not a call of its own.
    """
    tree = _tree("src", "agent_loop.py")
    offloaded = False
    inline = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = getattr(node.func, "attr", "")
        if target == "to_thread":
            for arg in node.args:
                if getattr(arg, "attr", "") == "decide_for_turn":
                    offloaded = True
        elif target == "decide_for_turn":
            inline.append(node.lineno)
    assert offloaded, (
        "src/agent_loop.py never passes decide_for_turn to asyncio.to_thread; "
        "the discovery pass would run on the event loop and block every other "
        "request in the application")
    assert not inline, (
        f"decide_for_turn is CALLED inline at line(s) {inline} of "
        f"src/agent_loop.py; that call blocks the event loop")


def test_the_loop_consults_the_engine_only_when_a_switch_is_on():
    """`active()` gates the hook: both switches off means not even the measurement.

    The engine costs a discovery pass per turn, and an operator who turned both
    off chose to pay nothing. A hook that ran anyway would make the off
    position of the flag a lie -- and the flag is the whole reason shadow mode
    is safe to default to on.
    """
    called = {getattr(n.func, "attr", "") for n in ast.walk(_tree("src", "agent_loop.py"))
              if isinstance(n, ast.Call)}
    assert "active" in called, (
        "src/agent_loop.py never calls completion_engine.service.active(); the "
        "engine runs on every turn regardless of the two switches")


def test_a_bug_in_the_engine_can_never_be_why_an_answer_disappears():
    """The hook is wrapped. The user's reply outranks the measurement.

    This runs while somebody is waiting for their answer. An exception
    escaping it would turn "the completion engine had a bug" into "your reply
    vanished", and the engine is off by default -- so the first person to meet
    that bug would be the first person who trusted the feature.
    """
    block = _ce_block()
    guarded = any(isinstance(stmt, ast.Try) and stmt.handlers for stmt in block)
    assert guarded, (
        "the completion-engine hook in src/agent_loop.py is not inside a try/"
        "except; an engine bug would lose the user's answer")


def test_the_loop_yields_the_frame_the_page_reads():
    """A `completion_decision` frame, or the decision is taken and never shown.

    Shadow mode's entire product is this frame: the engine computes what it
    WOULD have done and the only place a person can see it is here. A decision
    recorded and not published is a measurement nobody can act on, which is the
    unreachable-subsystem failure wearing a different hat.
    """
    source = _source("src", "agent_loop.py")
    assert '"type": "completion_decision"' in source, (
        "src/agent_loop.py never yields a completion_decision frame; the "
        "engine decides and nothing downstream is told")


def test_the_hook_sits_before_the_break_that_ends_the_turn():
    """Order, asserted on siblings. After the `break` it is unreachable code.

    The moment the engine exists to catch is the one where the model produced a
    round with no tool calls and the loop is about to leave: that is the last
    instant the turn's own ledger, checks and budget are still in hand. Written
    after the `break` the hook still parses, still imports, still passes every
    test its own package has -- and never runs once. That is this repository's
    signature failure, expressed in control flow instead of in a registry.
    """
    block = _ce_block()
    hook_at = min(i for i, stmt in enumerate(block)
                  if isinstance(stmt, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "_ce_decision"
                          for t in stmt.targets))
    breaks = [i for i, stmt in enumerate(block) if isinstance(stmt, ast.Break)]
    assert breaks, (
        "the completion-engine hook in src/agent_loop.py is not in the block "
        "that breaks out of the tool loop; it is not on the path a turn ends by")
    assert hook_at < max(breaks), (
        "the completion-engine hook is written AFTER the break that ends the "
        "tool loop in src/agent_loop.py; it is unreachable code")


def test_the_loop_continues_only_when_there_is_something_to_continue_with():
    """An empty `continue_with` must break. Shadow mode depends on it.

    Shadow returns an empty list by design, and a `continue` that did not check
    would send the model back for another round with an instruction listing
    nothing -- turning the measurement into a behaviour change, which is the
    one thing shadow mode exists to avoid. The `_CE_MAX_ROUNDS` half of the
    condition is what stops a converging-on-nothing loop.
    """
    hook = _ce_block()
    guards = []
    for stmt in hook:
        for node in ast.walk(stmt):
            if not isinstance(node, ast.If):
                continue
            if not any(isinstance(s, ast.Continue) for s in node.body):
                continue
            names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
            guards.append(names)
    assert guards, (
        "nothing in the completion-engine hook continues the loop; the engine "
        "can never send a turn back for the work it says is still owed")
    assert any("_ce_next" in names for names in guards), (
        "the hook continues without testing `continue_with`; a shadow decision "
        "would restart the turn with an empty instruction")
    assert any("_ce_completion_rounds" in names and "_CE_MAX_ROUNDS" in names
               for names in guards), (
        "the hook continues without testing _ce_completion_rounds against "
        "_CE_MAX_ROUNDS; a cheap improvement that reveals another one loops "
        "forever")


# -- settings ---------------------------------------------------------------


def test_every_completion_setting_is_in_both_places_it_has_to_be():
    """`DEFAULT_SETTINGS` and `agent_settings_schema` must agree.

    `tests/test_agent_settings_schema.py` enforces the general rule; this is
    scoped so the failure names the Completion Engine instead of arriving as a
    generic parity error somebody else has to bisect. A key with a default and
    no schema entry is a switch the settings page cannot render, which is a
    feature that exists and that nobody can turn on.
    """
    from src.agent_settings_schema import schema_keys
    from src.settings import DEFAULT_SETTINGS

    keys = {k for k in DEFAULT_SETTINGS if k.startswith("agent_completion")}
    assert keys, "no agent_completion* setting exists at all"
    missing = sorted(keys - set(schema_keys()))
    assert not missing, f"{missing} have no entry in src/agent_settings_schema.py"


@pytest.mark.parametrize("key", [
    "agent_completion_engine",
    "agent_completion_engine_shadow",
    "agent_completion_verification_reserve",
    "agent_completion_max_bonus_rounds",
])
def test_the_four_keys_the_engine_reads_exist_at_all(key):
    """Named one by one, because the parity test above is vacuous without them.

    A parity check over an empty set passes. These four are read by name --
    `service.SETTING`, `service.SHADOW_SETTING`, and the two numbers
    `GET /api/completion/settings` returns -- and each missing one is a
    `get_setting` silently answering its inline default forever.
    """
    from src.agent_settings_schema import schema_keys
    from src.settings import DEFAULT_SETTINGS

    assert key in DEFAULT_SETTINGS, f"{key} has no default in src/settings.py"
    assert key in set(schema_keys()), f"{key} has no entry in src/agent_settings_schema.py"


def test_the_service_reads_the_switch_names_the_settings_declare():
    """The two constants must BE the two keys, not merely resemble them.

    A typo here is the quietest failure in the file: `get_setting` answers the
    inline default for a name nobody defined, so the engine reports itself
    disabled forever and the operator's switch does nothing at all.
    """
    from src.settings import DEFAULT_SETTINGS

    assert ce_service.SETTING in DEFAULT_SETTINGS, (
        f"service.SETTING is {ce_service.SETTING!r} and no such setting exists")
    assert ce_service.SHADOW_SETTING in DEFAULT_SETTINGS, (
        f"service.SHADOW_SETTING is {ce_service.SHADOW_SETTING!r} and no such "
        f"setting exists")
    assert DEFAULT_SETTINGS[ce_service.SETTING] is False, (
        "the engine ships ON; a switch that changes what a turn DOES has to be "
        "turned on by somebody who meant to")


# -- the event vocabulary ---------------------------------------------------


def test_every_event_this_package_declares_is_one_the_envelope_will_accept():
    """`COMPLETION_EVENTS` must stay a strict SUBSET of `EVENT_NAMES`.

    A name declared here and missing there reaches a page perfectly and is then
    refused by `Event.parse` when an audit replays it through the envelope --
    so the failure appears months later, in the one place that exists to
    reconstruct what happened.
    """
    missing = sorted(set(ce_events.COMPLETION_EVENTS) - set(EVENT_NAMES))
    assert not missing, (
        f"{missing} are published by src/completion_engine/events.py and are "
        f"not in EVENT_NAMES; add them to src/contracts/event.py")


def test_every_event_name_the_code_publishes_is_declared():
    """Read the `_emit(...)` and `publish(...)` literals out of the source.

    This catches the paths no test happens to drive: an error branch that only
    fires on a machine with a broken store still emits, and an undeclared name
    there is a silent `completion_error` in production.

    Scans ALL positional arguments and the `name=` keyword, not `args[0]`:
    getting that wrong is how a version of this test passed while reading
    nothing, because the publisher takes the name in a different position.
    """
    declared = set(ce_events.COMPLETION_EVENTS)
    emitted = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in ("_emit", "publish"):
                continue
            values = list(node.args) + [kw.value for kw in node.keywords
                                        if kw.arg == "name"]
            for arg in values:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.startswith("completion_"):
                        emitted.add(arg.value)
    undeclared = sorted(emitted - declared)
    assert not undeclared, (
        f"{undeclared} are emitted in src/completion_engine/ and are not in "
        f"COMPLETION_EVENTS; they will publish as completion_error")


def test_the_route_serves_the_same_vocabulary_the_package_declares():
    """`config()` is what stops the front end keeping its own copy of a list.

    A page with its own vocabulary drifts the day a word is added, and the
    symptom is a row that renders blank rather than an error anyone notices.
    """
    answer = ce_service.service().config()
    for key in ("modes", "layers", "relations", "stop_reasons",
                "rejection_reasons", "categories", "sources", "budget_lines",
                "units", "effects", "events", "policies"):
        assert answer.get(key), f"config() has no {key}"
    assert set(answer["events"]) == set(ce_events.COMPLETION_EVENTS), (
        "config() publishes a different event list than events.py declares")


# -- the door that must not exist -------------------------------------------


def test_the_app_publishes_no_second_door_to_a_decision(spec):
    """No `POST /run`, and no `POST /api/completion`. This is a claim, not an omission.

    The engine decides INSIDE a turn, at the moment the model stops calling
    tools, with that turn's ledger, proof and budget behind it. An endpoint that
    ran it would be a second way to reach the same answer with none of those
    behind it -- and what came out would be indistinguishable from the real
    thing, in the same store, in the same list, with the same shape. Every
    other subsystem in this repository has a `POST /run`, so its absence here
    reads as an oversight unless something asserts it on purpose.

    Asked of the real OpenAPI document rather than of the router source,
    because the failure worth catching is a `/run` reaching the app from
    anywhere -- including a second router that decided to be helpful.
    """
    paths = {p for p in spec.get("paths", {}) if p.startswith("/api/completion")}
    runs = sorted(p for p in paths if p.rstrip("/").endswith("/run"))
    assert not runs, (
        f"{runs} would run the engine over HTTP with no turn, no ledger, no "
        f"proof and no budget behind the answer")
    collection = spec.get("paths", {}).get("/api/completion", {})
    assert "post" not in {m.lower() for m in collection}, (
        "POST /api/completion exists; a decision may only be created by a turn")


def test_the_router_source_declares_no_route_that_runs_anything():
    """The same claim read off the source, and the reason it is worth twice.

    A `/run` behind a flag that is off would be absent from the OpenAPI
    document on this machine and present on the next one. The source cannot
    hide it.
    """
    tree = _tree("routes", "completion_engine_routes.py")
    declared = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute)
                and getattr(node.func.value, "id", "") == "router"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            declared.append((node.func.attr, str(node.args[0].value)))
    runs = sorted(p for _m, p in declared if p.rstrip("/").endswith("/run"))
    assert not runs, f"{runs} is declared in routes/completion_engine_routes.py"
    assert not [p for m, p in declared if m == "post" and p in ("", "/")], (
        "the router declares a POST on its collection; the only write this "
        "subsystem has is rejecting one improvement")


def test_the_one_write_is_gated_on_a_person():
    """`reject-improvement` is `require_human`. §12.

    A person can say no to an extra. A model that could reject its own
    improvements could also quietly delete the record of having been told to
    make them, and the record of what was OFFERED and turned down is the more
    interesting half of this subsystem's account.
    """
    tree = _tree("routes", "completion_engine_routes.py")
    handler = next((h for h in _handlers(tree) if h.name == "reject_improvement"), None)
    assert handler is not None, "reject_improvement is gone from the router"
    calls = {n.func.id for n in ast.walk(handler)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "require_human" in calls, (
        "reject_improvement is not require_human; a model could refuse its own "
        "improvements and erase the record of having been asked")
    assert "require_admin" in calls


# -- the store and the caller -----------------------------------------------


def test_the_package_import_registers_the_schema():
    """Importing the package must create the tables, flag or no flag.

    The schema is registered as a side effect of importing `persistence`. If
    the only importer were a route behind a switch that is off, the tables
    would never exist on a machine where nobody turned it on -- and the first
    thing that happened after they did would be a failure.
    """
    from src.completion_engine import persistence

    assert persistence.registered_schemas(), "no schema was registered on import"


def test_the_owner_comes_from_the_session_and_never_from_a_body():
    """Owner scoping is only real if the caller cannot name themselves.

    Every read here is filtered by owner and another owner's decision answers
    404, never 403. A handler that took `owner` out of the payload would hand
    that filter to the caller, and the 404 would become a directory.
    """
    tree = _tree("routes", "completion_engine_routes.py")
    for handler in _handlers(tree):
        for node in ast.walk(handler):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", "") != "get":
                continue
            base = getattr(node.func.value, "id", "")
            if base != "body":
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant):
                    assert arg.value not in ("owner", "user", "user_id", "actor"), (
                        f"{handler.name} reads {arg.value!r} out of the request "
                        f"body; the owner comes from the session or the scoping "
                        f"is a suggestion")
    assert "_require_owner" in _source("routes", "completion_engine_routes.py")
