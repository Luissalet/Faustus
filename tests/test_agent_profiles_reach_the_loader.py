"""
The test that was missing, and the reason it was missing.

`tests/test_agent_profiles_builtin.py` is thorough about the ten specialised
profiles — and every one of its assertions calls `builtin.profile_defs()`
directly. So the module was comparing itself against itself, and nobody asked
the *loader* whether the profiles had arrived. They had not: `agent_defs`
served three definitions, the Agents screen showed three cards, and 250 green
tests said nothing was wrong. It took opening the page in a browser to see it.

This file asks the only question those tests could not: does the catalogue the
rest of the product reads actually contain them?

The second test is the other half of the same lesson. `builtins()` must keep
returning what `agent_defs.py` itself ships and nothing more, because that is
what lets the profile module compare an augmented definition against the
original — if the augmented set fed back into `builtins()`, the comparison
would be circular and would pass no matter what it did.
"""

from __future__ import annotations

import pytest

from src import agent_defs as defs
from src.agent_profiles import builtin as profiles


def test_every_specialised_profile_reaches_the_catalogue_the_product_reads():
    """Not `profile_defs()` — `load_all()`. A profile that stops at the module
    that defines it is written, tested and invisible."""
    catalogue = defs.load_all().by_slug()
    missing = [d.slug for d in profiles.profile_defs() if d.slug not in catalogue]
    assert missing == [], f"profiles never reach load_all(): {missing}"


def test_the_loader_serves_the_profile_version_not_the_bare_one():
    """`implementer` and `reviewer` are re-emitted by the profile catalogue
    with their completion mode and profile references. The loader has to hand
    back the augmented one, or the augmentation is decoration."""
    catalogue = defs.load_all().by_slug()
    for definition in profiles.profile_defs():
        served = catalogue.get(definition.slug)
        assert served is not None
        assert served.default_completion_mode == definition.default_completion_mode
        assert served.context_profile == definition.context_profile
        assert served.verification_profile == definition.verification_profile


def test_a_slug_appears_exactly_once_in_the_catalogue():
    """Augmenting replaces; it never ships both copies. Two definitions of one
    slug is the duplicate catalogue this whole layer exists to avoid."""
    slugs = [d.slug for d in defs.load_all().agents]
    duplicated = sorted({s for s in slugs if slugs.count(s) > 1})
    assert duplicated == []


def test_builtins_still_answers_only_for_what_this_file_ships():
    """The circularity guard. If the specialised profiles fed back into
    `builtins()`, `test_agent_profiles_builtin.py` would be comparing each
    augmented definition against itself and would pass on anything."""
    assert {d.slug for d in defs.builtins()} == {"reviewer", "planner", "implementer"}
    assert len(defs.load_all().agents) > len(defs.builtins())


def test_every_profile_the_loader_serves_can_still_be_found_by_its_slug():
    """`clean_slug` runs every slug through `slugify`, so a definition named
    with an underscore loads and can then never be looked up again. This is
    the check that catches it at the catalogue level rather than at the point
    where a dispatched job silently finds no agent."""
    for definition in defs.load_all().agents:
        assert defs.clean_slug(definition.slug) == definition.slug
        assert defs.get(definition.slug) is not None


@pytest.mark.parametrize("kind, attr", [
    ("verification", "verification_profile"),
    ("context", "context_profile"),
    ("budget", "budget_profile"),
    ("collaboration", "collaboration_profile"),
])
def test_every_profile_reference_in_the_catalogue_resolves(kind, attr):
    """A definition that names a profile nobody registered is a run that will
    fail at resolution time, on the machine, at night."""
    from src.agent_profiles import catalog

    for definition in defs.load_all().agents:
        value = getattr(definition, attr, "") or ""
        if not value:
            continue
        assert catalog.exists(kind, value), (
            f"{definition.slug} names {kind} profile {value!r}, which is not registered")
