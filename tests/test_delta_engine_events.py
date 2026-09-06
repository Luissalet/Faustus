"""The delta engine's in-process wiring: the event stream and the adapter registry.

Two modules, one file, because they are the same kind of thing -- the parts that
decide what reaches a consumer and which code is allowed to answer a question.
The durable half lives in `test_delta_engine_persistence.py`.

The guarantees, one test each:

* `DELTA_EVENTS` is exactly the `delta_*` block of `EVENT_NAMES`, checked by
  READING `src/contracts/event.py` rather than by repeating the list here --
  a copy of a list in a test is a second place to forget a name;
* a name this module never declared is published as `delta_error` with the
  requested name inside the payload: never dropped, never raised;
* the cursor resumes exactly where it stopped, and says so when the capacity
  dropped what the caller asked for;
* the frame is an UNNAMED SSE frame with the name inside the JSON;
* a publisher cannot put an event in somebody else's stream;
* `discover()` finds every module of the package that declares
  `ADAPTER_FACTORY` -- checked by WALKING the package, never against a list of
  domains written down here, because the adapters are being written in
  parallel and a test that pins the set would be wrong by tomorrow;
* a module that explodes on import costs its own domain and nothing else;
* `adapter_for` on a domain with no working adapter raises and says why, and
  never hands back the binary adapter.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import pkgutil
import sys
import threading
from textwrap import dedent

import pytest

from src.contracts.event import EVENT_NAMES
from src.delta_engine import events as E
from src.delta_engine import registry as R
from src.delta_engine.contracts import DOMAINS, DeltaError

OWNER = "alice"


@pytest.fixture(autouse=True)
def clean_registry():
    """No test inherits another's adapter table or another's streams."""
    R.reset()
    E.reset_streams()
    try:
        yield
    finally:
        R.reset()
        E.reset_streams()


def stream(owner: str = OWNER, **kwargs) -> E.DeltaEventStream:
    return E.DeltaEventStream(owner, **kwargs)


# -- the vocabulary is closed, and it is closed in one place ----------------


def test_delta_events_is_exactly_the_delta_block_of_the_envelope():
    """Read from `EVENT_NAMES`, not copied into this file.

    Both directions matter and they fail differently. A name here and not there
    reaches a page and is then refused by the envelope an audit replays it
    through. A name there and not here is a name this module can never publish,
    which is the `state_mirror/adapters` tuple failure wearing a different hat.
    """
    declared = set(E.DELTA_EVENTS)
    in_envelope = {name for name in EVENT_NAMES if name.startswith("delta_")}

    assert declared <= set(EVENT_NAMES), (
        f"{sorted(declared - set(EVENT_NAMES))} would reach a page and then be "
        f"refused by the envelope")
    assert declared == in_envelope, (
        f"the two lists have drifted: {sorted(declared ^ in_envelope)}")
    assert declared < set(EVENT_NAMES), "a strict subset, not the whole vocabulary"
    assert len(E.DELTA_EVENTS) == len(declared), "no name is declared twice"


def test_an_unknown_name_is_published_as_delta_error_and_is_never_lost():
    """A comparison must not die of a typo in a progress line, and the typo
    must still be findable afterwards."""
    events = stream()
    published = events.publish("delta_finished", delta_id="delta_1")

    assert published.name == "delta_error"
    assert published.payload["unknown_event"] == "delta_finished"
    assert published.payload["delta_id"] == "delta_1"
    assert published.seq == 1, "it consumed a sequence number like any other"
    assert events.since(0)[0][0].id == published.id


def test_publishing_never_raises_whatever_the_name_is():
    events = stream()
    for name in ("", "   ", None, "delta.completed", "state_changed"):
        assert events.publish(name, delta_id="d").name == "delta_error"
    assert events.stats()["last_seq"] == 5, "nothing was dropped on the way"


# -- resuming, and admitting a hole -----------------------------------------


def test_the_cursor_resumes_exactly_where_it_stopped():
    events = stream()
    for index in range(3):
        events.publish("delta_assertion_created", delta_id="d", path=f"p{index}")

    first, cursor, gap = events.since(0, limit=2)
    assert [e.seq for e in first] == [1, 2]
    assert (cursor, gap) == (2, False), (
        "the cursor is the last event of THIS page, not the head of the stream")

    second, cursor, gap = events.since(cursor)
    assert [e.seq for e in second] == [3]
    assert (cursor, gap) == (3, False)

    tail, cursor, gap = events.since(cursor)
    assert (tail, cursor, gap) == ([], 3, False), (
        "an empty page returns the cursor unchanged; resetting it is how a "
        "poller silently starts replaying history")


def test_a_consumer_ahead_of_the_stream_gets_nothing_rather_than_a_rewind():
    events = stream()
    events.publish("delta_completed", delta_id="d")
    assert events.since(99) == ([], 99, False)


def test_the_gap_is_announced_when_the_capacity_dropped_what_was_asked_for():
    """A silent hole costs a regression nobody knows was missed; the warning
    costs a refresh."""
    events = stream(capacity=4)
    for index in range(10):
        events.publish("delta_assertion_created", delta_id="d", path=f"p{index}")

    page, cursor, gap = events.since(0)
    assert gap is True
    assert [e.seq for e in page] == [7, 8, 9, 10]
    assert cursor == 10
    assert events.stats()["dropped"] == 6

    inside, _, gap_again = events.since(8)
    assert [e.seq for e in inside] == [9, 10]
    assert gap_again is False, "there is no hole between 8 and what survives"


# -- the envelope -----------------------------------------------------------


def test_every_event_carries_the_common_payload_even_when_nobody_supplied_it():
    """Absent and empty are two different facts, and a consumer should only ever
    have to handle one of them."""
    published = stream().publish("delta_requested", delta_id="delta_1")
    for key in E.COMMON_PAYLOAD_KEYS:
        assert key in published.payload, f"{key} is missing from the payload"
    assert published.payload["domain"] == ""
    assert published.payload["at"], "the timestamp is filled in, never blank"
    assert published.payload["event_id"] == published.id


def test_a_publisher_cannot_put_an_event_in_someone_elses_stream():
    """The one mistake in this file with a blast radius outside it."""
    published = stream("alice").publish("delta_completed", owner="mallory",
                                        delta_id="delta_1")
    assert published.owner == "alice"
    assert published.payload["owner"] == "alice"


def test_the_frame_is_an_unnamed_sse_frame_with_the_name_inside_the_json():
    """A named frame never reaches `onmessage`, and a page written against the
    unnamed dispatch stream goes deaf on a named one without erroring."""
    frame = stream().publish("delta_completed", delta_id="delta_1").sse()

    assert frame.startswith("data: ") and frame.endswith("\n\n")
    assert "event:" not in frame
    body = json.loads(frame[len("data: "):].strip())
    assert body["name"] == "delta_completed"
    assert body["delta_id"] == "delta_1"


def test_a_payload_that_will_not_serialise_costs_its_own_frame_only():
    class Opaque:
        def __repr__(self):
            return "<opaque>"

    events = stream()
    published = events.publish("delta_extraction_completed", delta_id="d",
                               extractor=Opaque())
    frame = published.sse()

    assert "opaque" in frame, "`default=str` keeps the frame instead of losing it"
    assert events.publish("delta_completed", delta_id="d").seq == 2


def test_a_secret_in_a_payload_does_not_leave_and_the_count_says_so():
    """The delta payload quotes values out of somebody's file, so this is not
    theoretical here."""
    published = stream().publish("delta_source_resolved", delta_id="d",
                                 api_key="sk-live-01234567890abcdef")

    assert "sk-live-01234567890abcdef" not in json.dumps(
        published.to_dict(), default=str)
    assert published.payload["redactions"] >= 1


# -- waiting ----------------------------------------------------------------


def test_wait_wakes_on_a_publish_from_another_thread():
    """Extraction runs on a worker while a route holds the long poll, so the
    cross-thread wake is the normal path here and not the exception. A lost wake
    would look exactly like a comparison that has hung."""
    events = stream()

    async def scenario() -> bool:
        threading.Timer(0.02, lambda: events.publish("delta_completed",
                                                     delta_id="d")).start()
        return await events.wait(0, timeout=5.0)

    assert asyncio.run(scenario()) is True
    assert events.since(0)[0][0].name == "delta_completed"


def test_wait_answers_false_on_the_deadline_and_that_is_not_an_error():
    events = stream()

    async def scenario() -> bool:
        return await events.wait(0, timeout=0.05)

    assert asyncio.run(scenario()) is False


def test_a_closed_stream_answers_immediately_instead_of_holding_the_poll():
    events = stream()
    events.close()

    async def scenario() -> bool:
        return await events.wait(0, timeout=30.0)

    assert asyncio.run(scenario()) is True


def test_an_event_after_close_is_kept_numbered_and_marked_late():
    """What arrived after a shutdown is part of the record of what happened,
    and is acted on by nothing."""
    events = stream()
    events.publish("delta_requested", delta_id="d")
    events.close()
    late = events.publish("delta_completed", delta_id="d")

    assert late.seq == 2
    assert late.payload["late"] is True
    assert events.stats()["late"] == 1


# -- one stream per owner ---------------------------------------------------


def test_a_stream_is_made_once_and_kept_so_its_numbers_keep_meaning_something():
    first = E.stream_for(OWNER)
    first.publish("delta_requested", delta_id="d")
    assert E.stream_for(OWNER) is first
    assert E.stream_for("bob") is not first
    assert set(E.stream_names()) == {OWNER, "bob"}

    E.close_stream(OWNER)
    assert E.stream_for(OWNER) is first, (
        "a closed stream stays in the registry; recreating it would restart the "
        "sequence at 1 and hand a reconnecting client numbers it had already used")
    assert E.stream_for(OWNER).last_seq() == 1

    E.reset_streams()
    assert E.stream_names() == ()
    assert E.stream_for(OWNER) is not first


# ===========================================================================
# the adapter registry
# ===========================================================================

#: One adapter module, parameterised. Written to a temporary package rather
#: than added to `src/delta_engine/adapters/`, for two reasons: the real
#: adapters are being written in parallel by other people, and "a module that
#: explodes on import" is not something to ship in order to test.
ADAPTER_SOURCE = """
class Adapter:
    domain = {domain!r}
    version = {version!r}

    def available(self):
        return {available!r}

    def snapshot(self, revision, *, scope):
        raise NotImplementedError

    def compare(self, source, target, *, scope):
        raise NotImplementedError

    def check_invariants(self, intent, source, target, *, scope):
        raise NotImplementedError


ADAPTER_FACTORY = Adapter
"""


@pytest.fixture
def adapter_package(tmp_path, monkeypatch):
    """A temporary package the registry walks instead of the real one.

    `ADAPTER_PACKAGE` is a module-level name precisely so this is possible: it
    is the only way to test the failure paths without shipping a broken module.
    """
    root = tmp_path / "pkgroot"
    package = root / "fake_delta_adapters"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    # `base` must be skipped by name. If it were not, this file would be
    # reported as a module declaring an uncallable factory.
    (package / "base.py").write_text("ADAPTER_FACTORY = None\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(R, "ADAPTER_PACKAGE", "fake_delta_adapters")

    def write(name: str, source: str) -> None:
        (package / f"{name}.py").write_text(dedent(source), encoding="utf-8")
        importlib.invalidate_caches()
        R.reset()

    try:
        yield write
    finally:
        R.reset()
        for module in [m for m in sys.modules if m.startswith("fake_delta_adapters")]:
            sys.modules.pop(module, None)


def adapter_module(domain: str, *, version: str = "1.0",
                   available: bool = True) -> str:
    return ADAPTER_SOURCE.format(domain=domain, version=version,
                                 available=available)


# -- discovery, checked by walking ------------------------------------------


def test_discover_walks_the_real_package_rather_than_reading_a_list():
    """The rule, not the roster.

    `state_mirror/adapters/__init__.py` records what a list costs: five
    adapters written, imported and tested green while being invisible to every
    sweep because nobody added them to a tuple. So this test walks the package
    itself and asserts that every module declaring `ADAPTER_FACTORY` is in
    `discover()` and nothing else is -- which holds with zero adapters, with
    one, and with twenty.
    """
    package = importlib.import_module(R.ADAPTER_PACKAGE)
    expected = set()
    for _finder, name, _ispkg in pkgutil.iter_modules(list(package.__path__)):
        if name.startswith("_") or name in R.SKIPPED_MODULES:
            continue
        try:
            module = importlib.import_module(f"{R.ADAPTER_PACKAGE}.{name}")
        except Exception:  # a broken module is the other test's subject
            continue
        if getattr(module, R.ADAPTER_ATTR, None) is not None:
            expected.add(f"{R.ADAPTER_PACKAGE}.{name}")

    assert {getattr(f, "__module__", "") for f in R.discover()} == expected


def test_discover_finds_a_module_written_after_the_process_started(
        adapter_package):
    """Declaring the symbol IS the registration; there is no second place."""
    assert R.discover() == ()

    adapter_package("code", adapter_module("code"))

    assert [f.__module__ for f in R.discover()] == ["fake_delta_adapters.code"]
    assert [a.domain for a in R.adapters()] == ["code"]
    assert R.adapter_for("code").version == "1.0"


# -- a broken adapter costs its domain, never the subsystem -----------------


def test_a_module_that_explodes_on_import_costs_its_domain_and_nothing_else(
        adapter_package):
    adapter_package("broken", "raise RuntimeError('no python parser here')")
    adapter_package("code", adapter_module("code"))
    adapter_package("image", adapter_module("image"))

    assert {a.domain for a in R.adapters()} == {"code", "image"}
    assert R.adapter_for("code").domain == "code"

    report = R.status()
    assert report["code"]["available"] is True
    assert "no python parser here" in report["module:broken"]["reason"]
    assert "module:base" not in report, "`base` is the shape, not an adapter"


def test_a_factory_that_raises_costs_its_domain_and_nothing_else(adapter_package):
    adapter_package("angry", "def ADAPTER_FACTORY():\n"
                             "    raise RuntimeError('ffmpeg is missing')\n")
    adapter_package("code", adapter_module("code"))

    assert {a.domain for a in R.adapters()} == {"code"}
    assert "ffmpeg is missing" in R.status()["module:angry"]["reason"]


def test_a_module_that_declares_nothing_is_reported_and_is_not_an_error(
        adapter_package):
    adapter_package("helpers", "SHARED = ('a', 'b')\n")
    adapter_package("code", adapter_module("code"))

    assert {a.domain for a in R.adapters()} == {"code"}
    assert R.ADAPTER_ATTR in R.status()["module:helpers"]["reason"]


# -- the refusals -----------------------------------------------------------


def test_adapter_for_a_domain_with_no_adapter_never_falls_back_to_binary(
        adapter_package):
    """The tempting mistake and the expensive one.

    `binary` can compare any two byte streams, so a fallback would answer
    `reencoded` about a rewritten Python file: confident, specific and entirely
    wrong, and indistinguishable from a real answer. The binary adapter is
    PRESENT in this test, which is the only version of it that proves anything.
    """
    adapter_package("binary", adapter_module("binary"))

    assert R.adapter_for("binary").domain == "binary"
    with pytest.raises(DeltaError) as caught:
        R.adapter_for("image")

    message = str(caught.value)
    assert "image" in message
    assert "binary" in message, "the refusal explains what it is not doing"
    assert R.has_adapter("image") is False


def test_a_domain_this_system_does_not_have_is_refused_by_name(adapter_package):
    with pytest.raises(DeltaError) as caught:
        R.adapter_for("spreadsheet")
    assert "spreadsheet" in str(caught.value)
    assert all(domain in str(caught.value) for domain in DOMAINS)


def test_two_modules_declaring_one_domain_are_a_named_error_and_neither_wins(
        adapter_package):
    """"Last import wins" would make which adapter runs depend on filename
    order, which is a fact nobody discovers until the answers change and no code
    did."""
    adapter_package("code_ast", adapter_module("code", version="ast"))
    adapter_package("code_tokens", adapter_module("code", version="tokens"))
    adapter_package("image", adapter_module("image"))

    with pytest.raises(DeltaError) as caught:
        R.adapter_for("code")
    message = str(caught.value)
    assert "code_ast" in message and "code_tokens" in message

    assert R.status()["code"]["available"] is False
    assert R.adapter_for("image").domain == "image", (
        "the collision costs its own domain and not the registry")


def test_an_adapter_that_reports_itself_unavailable_is_refused_and_says_why(
        adapter_package):
    adapter_package("code", adapter_module("code", available=False))

    with pytest.raises(DeltaError) as caught:
        R.adapter_for("code")
    assert "unavailable" in str(caught.value)
    assert R.has_adapter("code") is False
    assert R.status()["code"]["available"] is False
    assert [a.domain for a in R.adapters()] == ["code"], (
        "it is still built and still reported; availability is not a reason to "
        "forget that the adapter exists")


def test_an_adapter_whose_availability_check_raises_is_unavailable_not_fatal(
        adapter_package):
    adapter_package("code", """
        class Adapter:
            domain = "code"
            version = "1.0"

            def available(self):
                raise OSError("the parser binary is gone")

        ADAPTER_FACTORY = Adapter
    """)

    with pytest.raises(DeltaError) as caught:
        R.adapter_for("code")
    assert "parser binary is gone" in str(caught.value)
    assert R.status()["code"]["available"] is False


def test_availability_is_asked_every_time_and_never_cached(adapter_package):
    """A model server that was down when the first delta ran may be up for the
    second. A cached `unavailable` would answer for the rest of the process."""
    adapter_package("code", """
        STATE = {"up": False}


        class Adapter:
            domain = "code"
            version = "1.0"

            def available(self):
                return STATE["up"]

        ADAPTER_FACTORY = Adapter
    """)

    assert R.has_adapter("code") is False
    importlib.import_module("fake_delta_adapters.code").STATE["up"] = True
    assert R.has_adapter("code") is True, (
        "nothing was refreshed; the registry caches which adapter serves a "
        "domain, not whether it can work right now")


# -- the one deliberate override --------------------------------------------


def test_register_installs_an_adapter_and_overrides_the_one_in_the_package(
        adapter_package):
    """An explicit registration is a decision; a second module in the package
    is an accident. Only the accident is refused."""
    adapter_package("code", adapter_module("code", version="from-the-package"))

    class Fake:
        domain = "code"
        version = "registered"

        def available(self):
            return True

    R.register(Fake)

    assert R.adapter_for("code").version == "registered"
    assert [a.domain for a in R.adapters()] == ["code"], (
        "an override replaces the adapter for its domain; it does not add a "
        "second one")

    R.reset()
    assert R.adapter_for("code").version == "from-the-package"


def test_register_refuses_what_can_never_serve_a_request():
    class Nowhere:
        domain = "spreadsheet"
        version = "1.0"

        def available(self):
            return True

    with pytest.raises(DeltaError):
        R.register(Nowhere)
    with pytest.raises(DeltaError):
        R.register("not a factory")


def test_status_answers_for_every_domain_it_knows_about(adapter_package):
    adapter_package("code", adapter_module("code", version="2.1"))

    entry = R.status()["code"]
    assert set(entry) >= {"available", "version", "module", "reason"}
    assert entry["available"] is True
    assert entry["version"] == "2.1"
    assert entry["module"].startswith("fake_delta_adapters.code")
    assert entry["reason"] == ""
