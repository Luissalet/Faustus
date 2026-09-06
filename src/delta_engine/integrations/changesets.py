"""Turning a code delta into the ChangeSet `prove` already knows how to judge.

`src/changesets.py` is the canonical wrapper for the changes observed in one
execution, and this module does not replace it: it builds one from what a delta
OBSERVED on disk, hands it to `changesets.judge()`, and keeps the packet that
comes back. §2 of the plan is explicit that a delta "no sustituye paths, hashes,
checkpoints ni evidencia de filesystem".

Two decisions inherited from the council, both deliberate and both worth
keeping visible:

1.  **`intent` is always `implement`.** `src/contracts/changeset.py` refuses any
    changed file under a `READ_ONLY_INTENT`, so a comparison built with
    `explore` would raise on exactly the deltas that matter. The council took
    the same decision in `src/council/adapters.py` and called `implement` "the
    strictest of them", which is the right reason: the strictest intent is the
    one that cannot flatter the result.

2.  **Claims come from what the delta observed, never from what anyone
    reported.** A ChangeSet's whole value is the gap between claim and
    evidence; filling both sides from the same source closes the gap and makes
    the proof meaningless. Here both sides come from the extraction, which is
    itself a reading of the two revisions -- so the packet answers "does the
    evidence support the paths we say changed", not "did the executor tell the
    truth". That is a narrower question than the council's and the docstring
    says so rather than letting a reader assume the wider one.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.delta_engine.contracts import UniversalDelta

logger = logging.getLogger(__name__)

__all__ = [
    "FILE_PREFIX",
    "CHANGESET_INTENT",
    "file_changes",
    "changeset_for",
    "judge",
]

#: The element-key prefix the code adapter uses for a file. Kept here as a
#: constant rather than a literal at three call sites, because this string is a
#: contract between two packages and a typo in it produces an empty ChangeSet
#: that judges perfectly and describes nothing.
FILE_PREFIX = "file:"

#: See decision 1 in the module docstring.
CHANGESET_INTENT = "implement"

#: How a delta operation maps onto the `CHANGE_SOURCES` view a ChangeSet keeps.
#: `moved` appears on both sides: a rename is a deletion and an addition to a
#: filesystem, and pretending otherwise would leave the old path unaccounted.
_ADDED = ("added",)
_DELETED = ("missing",)
_MODIFIED = ("modified", "reencoded")


def file_changes(delta: UniversalDelta) -> Dict[str, Any]:
    """The `changes` block of a ChangeSet, read off the delta's assertions.

    `source` is `"checkpoint"` when both revisions are checkpoints, because
    that is the only case where the paths came from git rather than from a
    walk, and `prove` reads `source == "checkpoint"` as the difference between
    `claims_unaccounted` and `claim_not_on_disk`. Claiming `checkpoint`
    anywhere else would buy a stronger proof with a sentence that is not true.
    """
    added: List[str] = []
    deleted: List[str] = []
    modified: List[str] = []
    for assertion in delta.assertions:
        if not assertion.path.startswith(FILE_PREFIX):
            continue
        path = assertion.path[len(FILE_PREFIX):]
        if not path:
            continue
        if assertion.operation in _ADDED:
            added.append(path)
        elif assertion.operation in _DELETED:
            deleted.append(path)
        elif assertion.operation in _MODIFIED:
            modified.append(path)
        elif assertion.operation == "moved":
            # Both ends, and the `before` is where it used to be. A rename with
            # only the new path recorded leaves the old one in `deleted` on
            # disk and unaccounted in the claims, which reads as an unexplained
            # deletion in every proof that follows.
            after = (assertion.after or path).removeprefix(FILE_PREFIX)
            before = (assertion.before or "").removeprefix(FILE_PREFIX)
            added.append(after)
            if before and before != after:
                deleted.append(before)
    exact = (delta.source.kind == "checkpoint" and delta.target.kind == "checkpoint")
    return {
        "source": "checkpoint" if exact else "mtime",
        "added": sorted(set(added)),
        "modified": sorted(set(modified)),
        "deleted": sorted(set(deleted)),
        "checkpoint": delta.target.hash if exact else "",
        "truncated": bool(delta.coverage.excluded),
    }


def changeset_for(delta: UniversalDelta, *, workspace: str = "",
                  title: str = "") -> Optional[Any]:
    """A `ChangeSet` describing this delta's file-level observations.

    Returns `None` for a delta with no file assertions -- a state or an image
    comparison has nothing a ChangeSet can hold, and building an empty one
    would produce a `no_changes` proof about a comparison that observed plenty.
    """
    changes = file_changes(delta)
    if not (changes["added"] or changes["modified"] or changes["deleted"]):
        return None
    try:
        from src import changesets as changesets_mod
    except Exception as exc:  # noqa: BLE001 - a missing module is not a delta failure
        logger.warning("delta/changesets: module unavailable: %s", exc)
        return None
    claims = [
        {"path": path, "kind": kind}
        for kind, paths in (("created", changes["added"]),
                            ("modified", changes["modified"]),
                            ("deleted", changes["deleted"]))
        for path in paths
    ]
    return changesets_mod.build(
        intent=CHANGESET_INTENT,
        workspace=workspace,
        checkpoint=changes["checkpoint"],
        changes=changes,
        claims=claims,
        title=title or f"delta {delta.id}",
        run_id=delta.run_id,
        owner=delta.owner,
        project_id=delta.project_id,
    )


def judge(delta: UniversalDelta, *, workspace: str = "") -> Dict[str, Any]:
    """`changesets.judge()` on the ChangeSet this delta implies. `{}` if none.

    Deliberately not wrapped in a delta-shaped result: what comes back is
    `prove`'s own packet, in `prove`'s own vocabulary, and translating it here
    would create the third dialect this subsystem spent a docstring avoiding.
    `integrations/prove.py` is where the two vocabularies meet, once.
    """
    changeset = changeset_for(delta, workspace=workspace)
    if changeset is None:
        return {}
    try:
        from src import changesets as changesets_mod

        return dict(changesets_mod.judge(changeset))
    except Exception as exc:  # noqa: BLE001 - a proof that fails is not a delta that fails
        logger.warning("delta/changesets: judge failed for %s: %s", delta.id, exc)
        return {}
