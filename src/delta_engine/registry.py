"""Which adapter serves which domain -- found by walking, never by a list.

**A list is exactly what was wrong the last time.** `state_mirror/adapters/
__init__.py` carries a tuple called `ADAPTER_FACTORIES`, and its own comment
records what that cost: five adapters were written, imported cleanly and passed
their own tests while being invisible to every sweep, every query and every
screen, because nobody added them to the tuple. Green tests, no error, no
warning -- the modules simply did not exist as far as the running system was
concerned, and the only reason it was caught is that somebody went looking.

So there is no tuple here to forget. This module walks
`src.delta_engine.adapters` with `pkgutil.iter_modules` and collects, from every
module except `base`, a module-level symbol called `ADAPTER_FACTORY`. Writing
the module and declaring the symbol IS the registration; there is no second
place to update, and an adapter cannot be simultaneously finished and missing.

The cost of discovery is that a broken module is found at runtime rather than
at review time, and the three rules below are what make that cost bounded:

* **A module that fails to import costs its own domain, never the registry.**
  The failure is recorded with its reason and `status()` reports it. One
  adapter that cannot find its parser must not take the other eight down.

* **`adapter_for` on a domain with no working adapter RAISES, and says why.**
  It never falls back to the binary adapter. The fallback is the tempting
  mistake and it is the expensive one: `binary` can compare any two byte
  streams, so it would answer `reencoded` about a Python file whose function
  bodies were rewritten -- a confident, specific, entirely wrong answer that
  looks exactly like a real one. `DOMAINS` in `contracts.py` says the same
  thing in its own comment. An honest refusal is cheap; a plausible wrong
  answer is not.

* **Two modules declaring the same domain is a NAMED error, not a race.**
  Neither wins: the domain is reported unavailable with both module names in
  the reason, and `adapter_for` refuses it. "Last import wins" would make which
  adapter runs depend on filename order, which is the kind of fact nobody
  discovers until the answers change and no code did.

`register()` is the one deliberate override, and it takes precedence over a
discovered module BECAUSE it is deliberate: a test installing a fake adapter and
a plugin shipping one outside the package are decisions somebody made, while a
second module in the package declaring a domain that already has one is an
accident. That asymmetry is the whole difference between the two paths.

Nothing here caches `available()`. The registry caches which adapter serves a
domain -- a fact about the code -- and asks the adapter whether it can work
every single time, because that is a fact about the machine right now: a model
server that was down when the first delta ran may be up for the second, and a
cached "unavailable" would keep answering for the rest of the process.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import threading
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from src.delta_engine.contracts import DOMAINS, DeltaError

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from src.delta_engine.adapters.base import DeltaAdapter

logger = logging.getLogger(__name__)

__all__ = [
    "ADAPTER_ATTR",
    "ADAPTER_PACKAGE",
    "SKIPPED_MODULES",
    "discover",
    "adapters",
    "adapter_for",
    "has_adapter",
    "status",
    "register",
    "reset",
]

#: The module-level symbol a module must declare to be an adapter:
#: `ADAPTER_FACTORY: Callable[[], DeltaAdapter]`. A factory rather than an
#: instance so that importing the package does not build eleven adapters, each
#: of which may want to probe for a parser it needs.
ADAPTER_ATTR = "ADAPTER_FACTORY"

#: The package that is walked. A module-level name rather than a literal inside
#: `discover()` so a test can point the walk at a package of its own -- which is
#: the only way to test "a module that explodes on import does not stop the
#: others" without shipping a module that explodes on import.
ADAPTER_PACKAGE = "src.delta_engine.adapters"

#: `base` holds the Protocol, the dataclasses and `unreadable()`; it is the
#: shape adapters have, not an adapter. Anything starting with `_` is private by
#: the same convention the rest of the repository uses.
SKIPPED_MODULES: Tuple[str, ...] = ("base",)

_LOCK = threading.RLock()
_ADAPTERS: Optional[Tuple[Any, ...]] = None
_BY_DOMAIN: Dict[str, Any] = {}
_CONFLICTS: Dict[str, str] = {}
_NOTES: Dict[str, Dict[str, Any]] = {}
_REGISTERED: Dict[str, Callable[[], Any]] = {}


def _note(*, module: str, reason: str, domain: str = "",
          version: str = "") -> Dict[str, Any]:
    """One `status()` entry for something that did not become an adapter.

    Keyed `module:<name>` rather than by domain, because a module that failed to
    import never got far enough to say which domain it serves, and putting a
    module name where a caller reads domains is how `adapter_for("code_v2")`
    gets written.
    """
    return {"available": False, "domain": domain, "version": version,
            "module": module, "reason": reason}


def _walk() -> Tuple[List[Callable[[], Any]], Dict[str, Dict[str, Any]]]:
    """Import every module in the adapter package; collect factories and failures.

    Never raises. A package that cannot be imported at all is itself reported as
    a note, because "there are no adapters" and "the adapters package is broken"
    lead to completely different actions and an empty tuple says the first one.
    """
    notes: Dict[str, Dict[str, Any]] = {}
    try:
        package = importlib.import_module(ADAPTER_PACKAGE)
    except Exception as exc:  # noqa: BLE001 - a broken package is a report
        logger.error("delta registry: %s could not be imported (%s); no domain "
                     "has an adapter until that is fixed", ADAPTER_PACKAGE, exc)
        notes[f"module:{ADAPTER_PACKAGE}"] = _note(
            module=ADAPTER_PACKAGE,
            reason=f"the adapter package could not be imported: {exc}")
        return [], notes

    factories: List[Callable[[], Any]] = []
    search_path = list(getattr(package, "__path__", ()) or ())
    for _finder, name, _ispkg in pkgutil.iter_modules(search_path):
        if name.startswith("_") or name in SKIPPED_MODULES:
            continue
        full = f"{ADAPTER_PACKAGE}.{name}"
        try:
            module = importlib.import_module(full)
        except Exception as exc:  # noqa: BLE001 - one module, not the registry
            logger.error("delta registry: %s failed to import (%s); its domain "
                         "has no adapter and the rest are unaffected", full, exc)
            notes[f"module:{name}"] = _note(
                module=full, reason=f"failed to import: {exc}")
            continue
        factory = getattr(module, ADAPTER_ATTR, None)
        if factory is None:
            # Not an error. A module here may be a helper, and saying so in
            # `status()` is more use than a warning nobody reads.
            notes[f"module:{name}"] = _note(
                module=full,
                reason=f"declares no module-level {ADAPTER_ATTR}")
            continue
        if not callable(factory):
            logger.error("delta registry: %s.%s is not callable (%r)", full,
                         ADAPTER_ATTR, type(factory).__name__)
            notes[f"module:{name}"] = _note(
                module=full,
                reason=f"{ADAPTER_ATTR} is not callable; it must be a "
                       f"zero-argument factory returning a DeltaAdapter")
            continue
        factories.append(factory)
    return factories, notes


def discover() -> Tuple[Callable[[], "DeltaAdapter"], ...]:
    """Every `ADAPTER_FACTORY` in the adapter package, in walk order.

    The package only -- what `register()` added is not here, because this
    function answers "what does the tree contain" and a test's fake adapter is
    not in the tree. `adapters()` is the one that answers "what will actually
    serve a request".
    """
    factories, _ = _walk()
    return tuple(factories)


def _describe(factory: Callable[[], Any]) -> str:
    """Where a factory came from, for an error somebody has to act on."""
    module = getattr(factory, "__module__", "") or "<unknown module>"
    name = getattr(factory, "__qualname__", None) or getattr(
        factory, "__name__", None) or type(factory).__name__
    return f"{module}.{name}"


def _install(factory: Callable[[], Any], *, discovered: bool,
             by_domain: Dict[str, Any], conflicts: Dict[str, str],
             notes: Dict[str, Dict[str, Any]]) -> Optional[Any]:
    """Build one adapter and file it under its domain, or record why not."""
    origin = _describe(factory)
    # The short module name, so that `status()` keys the failure the same way
    # `_walk` keys an import failure: one shape of key, or a reader has to know
    # which half of this file produced the entry they are looking at.
    key = f"module:{(getattr(factory, '__module__', '') or origin).rsplit('.', 1)[-1]}"
    try:
        adapter = factory()
    except Exception as exc:  # noqa: BLE001 - a factory that throws is a report
        logger.error("delta registry: %s could not be built (%s)", origin, exc)
        notes[key] = _note(module=origin, reason=f"the factory raised: {exc}")
        return None
    domain = str(getattr(adapter, "domain", "") or "")
    if domain not in DOMAINS:
        # An unknown domain is refused rather than added, because `DOMAINS` is
        # closed for the same reason this registry exists: a domain nothing
        # routes on is a string in a database.
        logger.error("delta registry: %s serves %r, which is not one of %s",
                     origin, domain, list(DOMAINS))
        notes[key] = _note(module=origin, domain=domain,
                           reason=f"declares domain {domain!r}, which is not a "
                                  f"known domain; known: {list(DOMAINS)}")
        return None
    incumbent = by_domain.get(domain)
    if incumbent is not None and discovered:
        # Both lose. Picking one would mean the answer depends on the order
        # `pkgutil` happened to walk the directory in.
        reason = (f"is declared by two modules -- {_describe(type(incumbent))} "
                  f"and {origin}; neither is used, because which one won would "
                  f"depend on filename order")
        logger.error("delta registry: domain %s %s", domain, reason)
        conflicts[domain] = reason
        by_domain.pop(domain, None)
        return None
    by_domain[domain] = adapter
    conflicts.pop(domain, None)
    return adapter


def _build() -> None:
    """Rebuild the domain table. Caller holds `_LOCK`."""
    global _ADAPTERS
    factories, notes = _walk()
    by_domain: Dict[str, Any] = {}
    conflicts: Dict[str, str] = {}
    for factory in factories:
        _install(factory, discovered=True, by_domain=by_domain,
                 conflicts=conflicts, notes=notes)
    # Explicit registrations last, and they overwrite: see the module docstring.
    # A deliberate override is a decision; a second module in the package is an
    # accident, and only the accident is refused.
    for factory in list(_REGISTERED.values()):
        _install(factory, discovered=False, by_domain=by_domain,
                 conflicts=conflicts, notes=notes)
    _BY_DOMAIN.clear()
    _BY_DOMAIN.update(by_domain)
    _CONFLICTS.clear()
    _CONFLICTS.update(conflicts)
    _NOTES.clear()
    _NOTES.update(notes)
    _ADAPTERS = tuple(by_domain[domain] for domain in sorted(by_domain))


def adapters(*, refresh: bool = False) -> Tuple["DeltaAdapter", ...]:
    """Every adapter that was built and owns a domain, ordered by domain.

    Availability is NOT filtered here. Whether an adapter can work is a fact
    about the machine at this instant -- a parser that is not installed yet, a
    model server still starting -- and answering it from a table built once per
    process would freeze a temporary no into a permanent one. `adapter_for` asks
    the adapter itself, every time.
    """
    with _LOCK:
        if refresh or _ADAPTERS is None:
            _build()
        return tuple(_ADAPTERS or ())


def adapter_for(domain: str) -> "DeltaAdapter":
    """The adapter that serves this domain, or a `DeltaError` that says why not.

    Four different refusals, because they call for four different actions:
    the domain is not one this system has (fix the caller), no module declares
    it (write the adapter), two modules declare it (delete one), or its adapter
    says it cannot work right now (install the parser, start the server).

    There is NO fallback. Handing back the binary adapter would answer
    `reencoded` about a rewritten Python file with `exact` confidence, and a
    caller cannot tell that from a real answer -- see the module docstring.
    """
    wanted = str(domain or "")
    if wanted not in DOMAINS:
        raise DeltaError(
            "domain",
            f"is not a domain this system compares; a domain nothing routes on "
            f"is a string in a database. Known: {list(DOMAINS)}", got=domain)
    with _LOCK:
        if _ADAPTERS is None:
            _build()
        adapter = _BY_DOMAIN.get(wanted)
        conflict = _CONFLICTS.get(wanted)
        note = _NOTES.get(f"module:{wanted}")
    if conflict:
        raise DeltaError("domain", f"{conflict}; nothing is served for it until "
                                   f"one of the two is removed", got=wanted)
    if adapter is None:
        detail = (f" -- the module named after it was found but {note['reason']}"
                  if note else "")
        raise DeltaError(
            "domain",
            f"has no adapter{detail}. It is NOT served by the binary adapter: "
            f"comparing bytes would answer `reencoded` about a {wanted} change "
            f"and the answer would look exactly like a real one",
            got=wanted)
    try:
        ready = bool(adapter.available())
    except Exception as exc:  # noqa: BLE001 - a probe that throws is unavailable
        raise DeltaError(
            "domain",
            f"has an adapter ({_describe(type(adapter))}) whose availability "
            f"check raised: {exc}", got=wanted) from exc
    if not ready:
        raise DeltaError(
            "domain",
            f"has an adapter ({_describe(type(adapter))}) that reports itself "
            f"unavailable on this machine; the comparison is refused rather "
            f"than handed to a byte comparer that would answer anyway",
            got=wanted)
    return adapter


def has_adapter(domain: str) -> bool:
    """Whether `adapter_for(domain)` would answer. Never raises.

    A convenience for a caller deciding whether to offer a comparison at all.
    Anything that is about to RUN one should call `adapter_for` and let the
    refusal carry its reason, because "no" without a reason is what makes a
    missing parser look like an empty result.
    """
    try:
        adapter_for(domain)
    except DeltaError:
        return False
    return True


def status() -> Dict[str, Dict[str, Any]]:
    """What every domain and every module in the package is doing, right now.

    Keys are domains for adapters that were built, and `module:<name>` for
    anything that never got far enough to claim one -- a module that failed to
    import, a factory that raised, a helper with no `ADAPTER_FACTORY`. The two
    key shapes cannot collide, so nothing here can be mistaken for a domain that
    exists.

    `available` is asked of the adapter on every call and is never read from a
    cache; see the module docstring.
    """
    with _LOCK:
        if _ADAPTERS is None:
            _build()
        by_domain = dict(_BY_DOMAIN)
        conflicts = dict(_CONFLICTS)
        notes = {key: dict(value) for key, value in _NOTES.items()}
    out: Dict[str, Dict[str, Any]] = {}
    for domain, adapter in sorted(by_domain.items()):
        reason = ""
        try:
            ready = bool(adapter.available())
            if not ready:
                reason = "the adapter reports itself unavailable on this machine"
        except Exception as exc:  # noqa: BLE001 - diagnostics never raise
            ready, reason = False, f"the availability check raised: {exc}"
        out[domain] = {
            "available": ready,
            "domain": domain,
            "version": str(getattr(adapter, "version", "") or ""),
            "module": _describe(type(adapter)),
            "reason": reason,
        }
    for domain, reason in sorted(conflicts.items()):
        out[domain] = {"available": False, "domain": domain, "version": "",
                       "module": "", "reason": reason}
    out.update(notes)
    return out


def register(factory: Callable[[], "DeltaAdapter"]) -> None:
    """Install an adapter that does not live in the package. Tests and plugins.

    The factory is called once here, immediately, so a caller registering a
    broken one learns about it at the `register()` call in its own code rather
    than three layers down inside somebody else's comparison. A discovered
    module gets the opposite treatment on purpose -- it may not be ours to fix,
    so it is recorded and skipped.

    Registering the domain a package module already serves REPLACES it. That is
    what makes this usable from a test, and it is safe precisely because it is
    explicit: nobody registers an adapter by accident.
    """
    if not callable(factory):
        raise DeltaError("factory", "is not callable; register a zero-argument "
                                    "factory, not an adapter instance",
                         got=type(factory).__name__)
    adapter = factory()
    domain = str(getattr(adapter, "domain", "") or "")
    if domain not in DOMAINS:
        raise DeltaError(
            "factory.domain",
            f"is not a domain this system compares. Known: {list(DOMAINS)}",
            got=domain)
    global _ADAPTERS
    with _LOCK:
        previous = _REGISTERED.get(domain)
        if previous is not None and previous is not factory:
            logger.info("delta registry: %s replaces %s for domain %s",
                        _describe(factory), _describe(previous), domain)
        _REGISTERED[domain] = factory
        _ADAPTERS = None  # rebuilt on the next call, with this one in it


def reset() -> None:
    """Forget everything, registrations included. Tests and shutdown.

    Also drops the memoised walk, so a test that wrote a module into a temporary
    package sees it on the next call instead of the table built before the file
    existed.
    """
    global _ADAPTERS
    with _LOCK:
        _ADAPTERS = None
        _BY_DOMAIN.clear()
        _CONFLICTS.clear()
        _NOTES.clear()
        _REGISTERED.clear()
