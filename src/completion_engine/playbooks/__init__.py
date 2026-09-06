"""Domain playbooks: what "done" means per domain, and the hook discovery calls.

`discovery.py` declares this seam in two constants rather than leaving it to be
guessed: `PLAYBOOK_MODULE` is `src.completion_engine.playbooks` and
`PLAYBOOK_HOOK` is `candidates`. Its `_from_playbook` generator imports
`<PLAYBOOK_MODULE>.<contract.playbook>` and calls that hook on the submodule,
so a playbook is a MODULE IN THIS PACKAGE with a `candidates(data)` function.

This file holds both halves of the seam:

*   the registry -- `PLAYBOOKS`, `playbook_for` and `known` -- so that a caller
    can ask which domains have a checklist before writing a contract that names
    one, instead of finding out through an `ImportError` that discovery
    swallows;
*   a package-level `candidates` with the SAME name and the same signature, so
    a caller that reaches for the package rather than for the submodule is
    routed to the right playbook instead of silently getting nothing back. The
    name is `candidates` and not `route` on purpose: `PLAYBOOK_HOOK` is the
    contract, and a package that answered to a different name would be a second
    spelling of the one thing discovery looks for.

**Nothing here scores, and nothing here calls a model.** A playbook is a
deterministic checklist read against the record of what the turn already did;
`discovery.py`'s docstring gives the reason and it applies with full force to
this package, which is the source §7 calls `playbook`: a generator that both
proposes work and prices it has no contrast, so `expected_value` and its three
siblings are left at their defaults for the frontier to fill in.

**An unknown playbook name answers `()` rather than raising.** That mirrors
`_from_playbook`, which treats a missing module as "most domains have none yet"
and falls back to the contract's `professional_expectations`. Raising here
would turn a typo in a contract field into a failed turn, and the fallback
already covers the case where the name is wrong -- the expectations still get
checked, just by discovery instead of by a playbook.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence, Tuple

from src.completion_engine.contracts import CompletionError, ImprovementCandidate
from src.completion_engine.playbooks import code as code_playbook

if TYPE_CHECKING:  # pragma: no cover - the annotation only
    from src.completion_engine.discovery import DiscoveryInput

__all__ = ["PLAYBOOKS", "HOOK", "playbook_for", "candidates", "known"]

#: The one function name a playbook module must expose. Kept here as data so a
#: test can assert it equals `discovery.PLAYBOOK_HOOK` -- two constants that
#: agree today and are never compared are two constants that will disagree.
HOOK = "candidates"

#: Domain name -> the module that owns its checklist. Keyed by each module's
#: own `DOMAIN` rather than by a literal written twice: a module whose `DOMAIN`
#: disagreed with its key would be reachable under one name and report the
#: other, and the closeout prints the one the module reports.
PLAYBOOKS: Dict[str, Any] = {code_playbook.DOMAIN: code_playbook}


def known() -> Tuple[str, ...]:
    """Every domain that has a playbook, sorted so the answer is stable."""
    return tuple(sorted(PLAYBOOKS))


def playbook_for(domain: str) -> Optional[Any]:
    """The playbook module for `domain`, or None when the domain has none.

    None and not a raise: §13 writes six playbooks and this phase implements
    one, so "no checklist for this domain" is the ordinary answer and has to be
    cheap to ask about.
    """
    return PLAYBOOKS.get(str(domain or "").strip())


def _name_for(data: "DiscoveryInput") -> str:
    """Which playbook this run is asking for, from the contract then the scope.

    The contract's `playbook` field wins because somebody wrote it down before
    the work started, which is the whole point of `CompletionContract`. The
    envelope's `allowed_domains` is the fallback and is read in ITS OWN order
    rather than sorted: `compile_envelope` lists the domains of the resources
    the goal named, so the first one that has a playbook is the domain of the
    thing the request is actually about.
    """
    named = str(getattr(data.contract, "playbook", "") or "").strip()
    if named:
        return named if named in PLAYBOOKS else ""
    for domain in getattr(data.envelope, "allowed_domains", ()) or ():
        candidate = str(domain or "").strip()
        if candidate in PLAYBOOKS:
            return candidate
    return ""


def candidates(data: "DiscoveryInput") -> Tuple[ImprovementCandidate, ...]:
    """`PLAYBOOK_HOOK`, at the package level: route to the domain's checklist.

    The type check is not defensive noise, it is the same check
    `_from_playbook` performs and for the same stated reason: a playbook that
    contributed something the contract cannot parse would leave the closeout
    reporting a professional layer as checked while nothing checkable came out
    of it. Doing it here as well means the guarantee holds for a caller that
    used the package hook, which discovery does not go through.
    """
    module = playbook_for(_name_for(data))
    if module is None:
        return ()
    produced = getattr(module, HOOK)(data)
    return _verified(produced, getattr(module, "DOMAIN", "?"))


def _verified(produced: Sequence[Any], domain: str) -> Tuple[ImprovementCandidate, ...]:
    for item in produced or ():
        if not isinstance(item, ImprovementCandidate):
            raise CompletionError(
                f"playbook.{domain}",
                "returned something that is not an ImprovementCandidate; a "
                "playbook whose output cannot be parsed contributes nothing "
                "while the closeout reports its layer as checked",
                got=item)
    return tuple(produced or ())
