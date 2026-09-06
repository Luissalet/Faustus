"""State Mirror, asked whether it is CONNECTED rather than whether it works.

Every other state-mirror test file asks a module whether it does its job. This
one asks the product whether the modules reach each other, because four times
running in this project a subsystem has been built, unit-tested green, and
shipped disconnected:

* the Context Engine's derived stores had `as_candidates()` and were never
  registered as a `ContextSource`; the screen said "6 declared sources are not
  available" and 476 tests said nothing;
* `LINK_PATCHABLE_FIELDS` was missing `updated_at`, which is precisely what the
  service sent, so PATCH was unusable against the real store while its own
  tests passed against a permissive fake;
* ten agent profiles never reached `agent_defs`, so the Agents screen showed
  three definitions with 250 tests green;
* the council ledger appended its events to a list nobody read, so no claim, no
  objection and no decision ever reached the page -- with 457 tests green.

It happened here too, and it is the reason this file was written before the
merge rather than after the next one. Five adapters -- workspace, services,
models, hardware, connections -- were written, tested and left out of
`ADAPTER_FACTORIES`. They imported. They passed. They were invisible to every
sweep, every query and every screen, and the only reason anybody found out is
that somebody went looking.

So the questions here are all of the form "does A actually reach B":

1.  Is every adapter in the package listed in `ADAPTER_FACTORIES`, and does
    `all_adapters()` really build it?
2.  Does every schema field an adapter can emit exist in `SCHEMA_FIELDS`, and
    does every schema have a TTL policy?
3.  Do the events the subsystem publishes exist in BOTH vocabularies -- its own
    and the envelope's -- so a page can subscribe and an audit can replay?
4.  Does a sweep actually write, and does what it writes come back out of the
    public read path with its freshness recomputed?
5.  Does the route layer reach the service, and does the screen's adapter agree
    with the shapes the routes return?
6.  Is the feature flag wired to the things that cost the machine, and to
    nothing else?
"""
from __future__ import annotations

import inspect
import json
import pkgutil
from typing import Any, Dict, List

import pytest

from src.contracts.event import EVENT_NAMES
from src.state_mirror import contracts as C
from src.state_mirror import events as E
from src.state_mirror import freshness as F
from src.state_mirror import persistence as P
from src.state_mirror import adapters as A
from src.state_mirror.adapters import base as B


# -- 1. every adapter the package contains is one the package uses ---------

def _adapter_classes() -> Dict[str, Any]:
    """Every `*Adapter` class defined in `src/state_mirror/adapters/`.

    Discovered by walking the package rather than by importing a list, because
    a list is exactly the thing that was wrong: an adapter left out of one is
    invisible, and a test that read the same list would be blind in the same
    place.
    """
    found: Dict[str, Any] = {}
    for info in pkgutil.iter_modules(A.__path__):
        if info.name == "base":
            continue
        module = __import__(f"src.state_mirror.adapters.{info.name}",
                            fromlist=["*"])
        for name, obj in vars(module).items():
            if (name.endswith("Adapter") and inspect.isclass(obj)
                    and obj.__module__ == module.__name__):
                found[name] = obj
    return found


def test_every_adapter_in_the_package_is_wired_into_the_sweep():
    """The bug this file exists for, in one assertion.

    An adapter module that exists, imports and passes its own tests but is
    missing from `ADAPTER_FACTORIES` is not a source. It is a file.
    """
    defined = _adapter_classes()
    listed = {f.__name__ for f in A.ADAPTER_FACTORIES}
    missing = sorted(set(defined) - listed)

    assert not missing, (
        f"{missing} exist in src/state_mirror/adapters/ and are not in "
        "ADAPTER_FACTORIES; nothing will ever instantiate them")


def test_all_adapters_actually_builds_every_one_of_them():
    """Listing is not building. A factory that raises on construction is as
    absent as one that was never listed, and the failure looks identical from
    outside: a source that reports nothing."""
    B.reset_adapters()
    built = {type(a).__name__ for a in B.all_adapters()}
    listed = {f.__name__ for f in A.ADAPTER_FACTORIES}

    assert built == listed, (
        f"{sorted(listed - built)} are listed and did not build")


def test_every_adapter_declares_a_name_and_schemas_it_can_actually_emit():
    """A source with no name cannot be refreshed by name, and one naming a
    schema this build does not declare produces observations the contract
    silently drops -- which reads as a source that found nothing."""
    B.reset_adapters()
    seen = set()
    for adapter in B.all_adapters():
        name = getattr(adapter, "name", "")
        assert name, f"{type(adapter).__name__} has no name"
        assert name not in seen, f"two adapters both call themselves {name!r}"
        seen.add(name)
        schemas = tuple(getattr(adapter, "schemas", ()) or ())
        assert schemas, f"{name} declares no schema"
        unknown = [s for s in schemas if s not in C.STATE_SCHEMAS]
        assert not unknown, f"{name} declares {unknown}, which no contract knows"


def test_importing_the_adapters_package_pulls_in_no_heavy_dependency():
    """The rule the whole package is built on, checked rather than trusted.

    `import src.state_mirror.adapters` must not drag in sqlalchemy, the session
    database or a media backend: a sweep that is off must cost nothing, and an
    adapter whose source fails to import must cost itself and not the sweep.
    Every real import is function-local, and the only way to keep it that way
    is to notice the day one is not.
    """
    import subprocess
    import sys

    probe = (
        "import sys; import src.state_mirror.adapters; "
        "bad=[m for m in ('sqlalchemy','core.database','torch','chromadb') "
        "if m in sys.modules]; print(','.join(bad))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    leaked = out.stdout.strip()
    assert not leaked, f"importing the adapters package loaded {leaked}"


# -- 2. the schemas and their policies agree -------------------------------

def test_every_declared_schema_has_a_freshness_policy():
    """A schema with no TTL falls to the 60-second floor silently.

    That is a safe default and a bad silence: the floor is right for a probe
    and wrong for a branch name, and nothing tells you which one you got.
    """
    missing = [s for s in C.STATE_SCHEMAS if s not in F.TTL_SECONDS]
    assert not missing, (
        f"{missing} have no entry in freshness.TTL_SECONDS and will use the "
        f"{F.DEFAULT_TTL_SECONDS}s floor without saying so")


def test_the_freshness_policy_names_only_fields_that_exist():
    """A TTL for a field that was renamed stops applying and says nothing."""
    assert F.check_policy() == ()


def test_every_kind_that_has_a_schema_can_mint_an_id():
    """`SCHEMA_FOR_KIND` and `ENTITY_KINDS` must not drift.

    A kind with a schema and no id grammar cannot be observed at all; an id
    grammar with no schema produces observations `parse` refuses with "nothing
    in this build declares a schema", which is a confusing way to learn that
    somebody forgot a line in a dict.
    """
    for kind, schema in C.SCHEMA_FOR_KIND.items():
        assert kind in C.ENTITY_KINDS, f"{kind} has a schema and is not a kind"
        assert schema in C.STATE_SCHEMAS, f"{kind} names an unknown schema"
        assert C.entity_id(kind, "alice", "x")


def test_only_a_schema_whose_source_sees_everything_is_a_snapshot():
    """`SNAPSHOT_SCHEMAS` is the licence to DELETE a field.

    Adding one is the most dangerous edit in the contracts file: a partial
    sample of a snapshot schema would erase every field the probe did not
    happen to reach. The list is short on purpose and this test is here so
    lengthening it is a decision somebody argued for.
    """
    assert C.SNAPSHOT_SCHEMAS == ("device_state.v1",)
    for schema in C.SNAPSHOT_SCHEMAS:
        assert schema in C.STATE_SCHEMAS


# -- 3. the event vocabularies reach the page and the audit ----------------

def test_the_stream_vocabulary_is_a_subset_of_the_envelope_vocabulary():
    """A name a page subscribes to and an audit cannot replay is half a name.

    The council learned this the same way: `COUNCIL_EVENTS` had to be added to
    `EVENT_NAMES` or an audit replaying its own history would reject it.
    """
    missing = sorted(set(E.STATE_EVENTS) - set(EVENT_NAMES))
    assert not missing, (
        f"{missing} are published by state_mirror.events and unknown to "
        "src/contracts/event.py; an audit would refuse them")


def test_every_name_the_subsystem_publishes_is_declared():
    """Read the `publish("...")` calls out of the source and check them all.

    `publish()` turns an unknown name into `state_error` without raising, so a
    typo does not fail -- it arrives at the page as an error frame for
    something that went perfectly. Reading the source is the only way to cover
    the names no test happens to trigger.
    """
    import ast
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    package = os.path.join(root, "src", "state_mirror")
    published = set()
    for base, _dirs, files in os.walk(package):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            with open(path, "r", encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name_of = (func.attr if isinstance(func, ast.Attribute)
                           else getattr(func, "id", ""))
                if name_of not in ("publish", "_publish"):
                    continue
                # EVERY positional argument is scanned, not just the first.
                # `ingest._publish` and `reconcile._publish` take the name
                # third (`publisher, owner, name`) and `stream.publish` takes
                # it first; a scan that assumed one shape found nothing at all
                # in the other and passed by being blind, which is the exact
                # failure mode this whole file is about.
                for arg in node.args:
                    if (isinstance(arg, ast.Constant)
                            and isinstance(arg.value, str)
                            and arg.value.startswith("state_")):
                        published.add(arg.value)

    assert published, "no publish(\"...\") call was found; this test has gone blind"
    undeclared = sorted(published - set(E.STATE_EVENTS))
    assert not undeclared, (
        f"{undeclared} are published and not in STATE_EVENTS; each one would "
        "reach a page as state_error")


def test_a_published_event_is_an_unnamed_sse_frame():
    """A named frame never reaches `onmessage`.

    Paid for twice already -- `src/contracts/event.py::Event.sse` and
    `src/council/events.py::CouncilEvent.sse` both carry the scar. A page
    written against the dispatch stream goes deaf on a named one and nothing
    raises.
    """
    E.reset_streams()
    stream = E.stream_for("alice")
    stream.publish("state_changed", entity_id="run:///real/x", revision=2)
    frame = stream.since(0)[0].sse()

    assert frame.startswith("data: "), frame[:40]
    assert "\nevent:" not in frame
    body = json.loads(frame[len("data: "):].strip())
    assert body["name"] == "state_changed"


# -- 4. a sweep writes, and a read gets it back -----------------------------

@pytest.fixture()
def store(tmp_path):
    P.use_path(str(tmp_path / "state_mirror.db"))
    E.reset_streams()
    B.reset_adapters()
    yield P.store()
    E.reset_streams()
    B.reset_adapters()
    P.use_path(None)


def _observation(**over: Any) -> C.StateObservation:
    body = {
        "entity_id": C.entity_id("service", "alice", "comfyui"),
        "source": "services",
        "state": {"health": "available"},
        "observed_at": "2026-09-06T12:00:00Z",
    }
    body.update(over)
    return C.StateObservation.parse(body)


def test_an_ingested_observation_comes_back_out_of_the_public_read_path(store):
    """Write through the front door, read through the front door.

    Every layer in between -- ingest, the reducer, the store, the entity row a
    listing joins against, the service's owner check -- has to be connected for
    this to pass, and any one of them being absent looks like "the mirror knows
    nothing" from outside.
    """
    from src.state_mirror import ingest as I
    from src.state_mirror import service as S

    S.reset_service()
    report = I.ingest([_observation()], store=store)
    assert report.accepted == 1, report

    rows = S.service().entities(owner="alice")
    assert [r["id"] for r in rows] == [C.entity_id("service", "alice", "comfyui")]

    one = S.service().entity(C.entity_id("service", "alice", "comfyui"),
                             owner="alice")
    assert one, "an entity that was just ingested is not visible to the service"
    assert one["state"]["fields"]["health"]["value"] == "available"


def test_a_read_re_rates_freshness_against_the_clock_now(store):
    """The whole promise of the subsystem, checked end to end.

    A field stored `fresh` an hour ago has to read `stale` now. If a query
    returned the STORED rating, "the server was up yesterday" would read as
    "the server is up" -- which is the sentence section 2.2 exists to prevent
    and the one failure nobody would notice until it mattered.
    """
    from src.state_mirror import ingest as I
    from src.state_mirror import queries as Q

    I.ingest([_observation()], store=store, now="2026-09-06T12:00:00Z")

    soon = Q.get(C.entity_id("service", "alice", "comfyui"), owner="alice",
                 store=store, now="2026-09-06T12:00:05Z")
    later = Q.get(C.entity_id("service", "alice", "comfyui"), owner="alice",
                  store=store, now="2026-09-06T13:00:00Z")

    assert soon is not None and later is not None
    assert soon.get("health").freshness == "fresh"
    assert later.get("health").freshness == "stale", (
        "an hour-old observation is still being presented as current")
    assert soon.get("health").value == later.get("health").value == "available", (
        "re-rating changed the value; ageing says how much a fact is worth, "
        "never what it is")
    assert soon.revision == later.revision, (
        "ageing moved the revision; Delta would report the clock as a change")


def test_the_same_observation_twice_is_one_observation(store):
    """Idempotence, at the seam rather than in the reducer.

    The reducer being idempotent is not enough: the store has to dedupe too, or
    a replayed event costs a reduce and a write every time it arrives.
    """
    from src.state_mirror import ingest as I

    first = I.ingest([_observation()], store=store)
    second = I.ingest([_observation()], store=store)

    assert (first.accepted, first.duplicates) == (1, 0)
    assert (second.accepted, second.duplicates) == (0, 1)
    state = store.get_state(C.entity_id("service", "alice", "comfyui"))
    assert state.revision == 1, "a replayed observation moved the revision"


def test_a_change_reaches_the_stream_with_a_diff_and_not_the_whole_state(store):
    """`state_changed` is what a page routes on, and it has to be small.

    A full state on every change would make the stream unusable on a busy
    machine, which is the same failure as not publishing at all: the page stops
    reading it.
    """
    from src.state_mirror import ingest as I

    stream = E.stream_for("alice")
    I.ingest([_observation()], store=store, publisher=stream)
    I.ingest([_observation(state={"health": "degraded"},
                           observed_at="2026-09-06T12:01:00Z")],
             store=store, publisher=stream)

    changed = [e for e in stream.since(0) if e.name == "state_changed"]
    assert changed, "a field moved and nothing reached the stream"
    payload = changed[-1].payload
    assert "changes" in payload and payload["changes"]
    assert "fields" not in payload, "the whole state travelled on the stream"


def test_a_sweep_tells_the_adapters_where_to_look(store, monkeypatch):
    """The gap a green sweep hid, and the reason this file keeps growing.

    A sweep on the real machine ran all eleven adapters, reported zero
    failures, and produced NOTHING from `workspace` and `objectives`. Both
    worked perfectly when called directly. Both had been handed a scope with no
    folder and no project in it, which is a question they cannot answer -- and
    from outside, "I was not told where to look" and "I looked and there is
    nothing" are the same empty list.

    So the scope a sweep builds has to carry the owner's project, and this
    asserts it does. The adapters are right not to guess; the sweep is the only
    layer that knows who is asking.
    """
    from services.projects import ProjectStore
    from src.state_mirror import reconcile as RC

    # The double is built from the REAL class, so a `list()` that is renamed or
    # given a different signature fails here. The first version of this test
    # used a bare stub with an invented method name, which is how the code
    # under test came to call `list_projects()` -- a method that has never
    # existed -- and pass.
    assert callable(getattr(ProjectStore, "list", None)), (
        "ProjectStore.list is what the sweep calls to find a folder")

    class _Store(ProjectStore):
        def __init__(self) -> None:  # noqa: D107 - the real one opens a file
            pass

        def list(self, owner=None):
            # The key order a REAL project record uses: `workspace` is the
            # absolute path, `folder` is the display name. The first version of
            # this double had only `folder`, and the code it was testing read
            # `folder` first -- so both agreed on the wrong key and the sweep
            # handed the adapter the string "LocalAI" as a path.
            return [{"id": "proj_1", "folder": "repo", "workspace": "/tmp/repo",
                     "owner": owner}]

    monkeypatch.setattr("services.projects.get_store", _Store)
    scope = RC._scope_for("alice", None)

    assert scope.owner == "alice"
    assert scope.workspace == "/tmp/repo", (
        "the sweep built a scope with no folder; workspace and objectives will "
        "report nothing and look healthy doing it")
    assert scope.project_id == "proj_1"


def test_a_sweep_keeps_a_folder_the_caller_asked_for(monkeypatch):
    """An explicit scope wins. Resolution is a default, never an override.

    A caller sweeping one project must not have it replaced by whichever
    project happens to be first, or `/api/state/reconcile?project_id=X` would
    quietly report about Y.
    """
    from services.projects import ProjectStore
    from src.state_mirror import reconcile as RC

    class _Store(ProjectStore):
        def __init__(self) -> None:  # noqa: D107
            pass

        def list(self, owner=None):
            return [{"id": "proj_1", "folder": "repo", "workspace": "/tmp/repo"}]

    monkeypatch.setattr("services.projects.get_store", _Store)
    scope = RC._scope_for("alice", {"workspace": "/tmp/other", "project_id": "proj_9"})

    assert (scope.workspace, scope.project_id) == ("/tmp/other", "proj_9")


def test_a_sweep_survives_an_install_with_no_projects(monkeypatch):
    """No project store, or no projects, is not a failed sweep.

    It is the state of a fresh install, and the two adapters that need a folder
    simply have nothing to say -- which is then the truth rather than a gap.
    """
    from src.state_mirror import reconcile as RC

    def explode():
        raise RuntimeError("no project store in this build")

    monkeypatch.setattr("services.projects.get_store", explode)
    scope = RC._scope_for("alice", None)

    assert scope.owner == "alice" and scope.workspace == ""


# -- 5. the route layer reaches the service --------------------------------

def test_every_service_method_the_routes_call_exists():
    """Signatures that have to agree, checked instead of assumed.

    A rename on either side is a `TypeError` at request time, inside whatever
    `try/except` the route wraps its call in -- so the symptom is an endpoint
    that answers empty rather than one that fails.
    """
    from src.state_mirror import service as S

    facade = S.service()
    for name in ("entities", "entity", "history", "changes", "conflicts",
                 "refresh", "reconcile", "diagnostics", "events", "situation"):
        assert callable(getattr(facade, name, None)), (
            f"routes call service().{name}() and it does not exist")
        params = inspect.signature(getattr(facade, name)).parameters
        assert "owner" in params, (
            f"service().{name}() takes no owner; every read has to be scoped")


def test_the_router_publishes_the_routes_the_screen_calls():
    """A method nothing routes to is a method nobody can call."""
    import routes.state_mirror_routes as R

    paths = {getattr(route, "path", "") for route in R.setup_state_mirror_routes().routes}
    for tail in ("/entities", "/changes", "/conflicts", "/diagnostics",
                 "/events", "/refresh", "/reconcile"):
        assert f"/api/state{tail}" in paths, (
            f"/api/state{tail} is not published; the screen calls it")


def test_the_screen_is_registered_in_all_four_places():
    """A screen registered in three of four places 404s on reload.

    The four are the component, the lazy route in `AppShell`, the entry in
    `routes.ts`, and the deep-link handler in `app.py`. Missing the last one is
    the classic: it works when you navigate to it and fails when you refresh.
    """
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(*parts: str) -> str:
        with open(os.path.join(root, *parts), "r", encoding="utf-8") as handle:
            return handle.read()

    assert os.path.exists(os.path.join(root, "studio", "src", "screens",
                                       "StateMirror.tsx"))
    shell = _read("studio", "src", "shell", "AppShell.tsx")
    assert "StateMirror" in shell and '"/state"' in shell.replace("'", '"')
    routes_ts = _read("studio", "src", "shell", "routes.ts")
    assert "'/state'" in routes_ts or '"/state"' in routes_ts
    app_py = _read("app.py")
    assert 'get("/state")' in app_py, (
        "/state is not in app.py's deep-link whitelist; the screen will 404 on "
        "a reload, which is exactly the bug that whitelist exists to prevent")


# -- 6. the flag gates the machine and nothing else ------------------------

def test_the_flag_exists_in_both_settings_files():
    """`agent_*` keys must appear in DEFAULT_SETTINGS and in the schema.

    `tests/test_agent_settings_schema.py` enforces the parity in general; this
    asserts the particular keys, so a rename shows up here as "state mirror
    lost its flag" rather than as a generic parity failure somewhere else.
    """
    from src.agent_settings_schema import schema_keys
    from src.settings import DEFAULT_SETTINGS

    for key in ("agent_state_mirror", "agent_state_mirror_sweep_seconds"):
        assert key in DEFAULT_SETTINGS, f"{key} is not a setting"
        assert key in schema_keys(), f"{key} has no schema entry"
    assert DEFAULT_SETTINGS["agent_state_mirror"] is False, (
        "the sweep probes the machine on a timer; it is opt-in")


def test_the_flag_stops_the_sweep_and_stops_nothing_else(store, monkeypatch):
    """Off means "do not cost the machine", never "hide what was observed".

    The council flag draws the same line and for the same reason: switching a
    feature off is a decision about what may RUN, and a room already recorded
    stays readable. A flag that also hid the reads would make turning it off
    feel like data loss, and nobody would turn it off.
    """
    from src.state_mirror import ingest as I
    from src.state_mirror import service as S

    S.reset_service()
    I.ingest([_observation()], store=store)

    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: False
                        if key == "agent_state_mirror" else default)

    assert S.service().entities(owner="alice"), (
        "the flag hid a read; off must mean 'do not probe', not 'forget'")
    refused = S.service().reconcile(owner="alice")
    assert refused.get("ok") is False, "the flag did not stop a sweep"
