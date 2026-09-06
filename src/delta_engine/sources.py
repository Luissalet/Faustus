"""Turning an immutable `RevisionRef` into bytes -- or saying why it cannot.

Rule 2 of `contracts.py` says a comparison needs two immutable ends. This
module is where that rule stops being a dataclass field and becomes an I/O
decision: it materialises one end, declares how much of it it actually read,
and answers `readable=False` with a reason whenever it cannot.

Four rules, each with the failure it prevents:

* **A refusal is a result, not an exception.** `Resolved(readable=False,
  reason=...)` is the honest answer for a checkpoint that was garbage-collected
  or a domain nobody has written a reader for yet. Raising instead would lose
  the work already done on the other end and would tell the caller nothing
  about WHICH end failed -- and `Snapshot.readable` in `adapters/base.py`
  exists to carry exactly that distinction into the delta.

* **A guessed reader is worse than no reader.** `blob`, `artifact`,
  `document`, `state`, `workflow` and `skill` have no resolver here, because
  this module does not know where those live. Each one answers with a concrete
  reason naming what is missing. A reader invented from the shape of a ref
  would return plausible bytes from the wrong place, and every conclusion drawn
  from them would look exactly like a real one.

* **No reference escapes its scope.** §25: "no permitir referencias arbitrarias
  fuera de scopes". The check is `os.path.realpath` against the workspace root
  and not a string comparison, because `..`, a symlink and a different drive
  letter all defeat the string version and all three are how a delta ends up
  reading a file nobody scoped it to.

* **A partial read says so, in the size AND in the reason.** Past
  `scope.budget.max_bytes` the payload is cut, `truncated` is set and `size`
  keeps the REAL size, so whoever compares can say it only saw part. A
  truncated read that reports its own length as the size is a comparison that
  claims completeness, which is the failure the whole subsystem exists to
  prevent.

`verify` is rule 2 turned into a check: the sha256 of what was read against the
sha256 the caller asked for. §3.6 -- "si cambian durante la comparación,
invalidar o reiniciar" -- so a mismatch invalidates the comparison rather than
producing a delta about two revisions that were never both current. It returns
`(False, reason)` and never raises, because it is called between two reads that
already succeeded and an exception there would discard both.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from src.delta_engine.adapters.base import Scope
from src.delta_engine.contracts import DeltaError, RevisionRef

logger = logging.getLogger(__name__)

__all__ = [
    "SourceError",
    "Resolved",
    "RESOLVERS",
    "CHECKPOINT_REF",
    "resolve",
    "verify",
    "stash",
    "forget",
    "stash_size",
]


class SourceError(DeltaError):
    """A read that could not even be attempted. Names the field and the value.

    Deliberately rare. Almost everything that can go wrong while resolving a
    revision is an ANSWER (`Resolved(readable=False, ...)`); this is reserved
    for a caller asking a `Resolved` for bytes it already said it does not
    have, which is a bug in the caller and not a fact about the revision.
    """


#: `checkpoint:<sha>#<path relative to the workspace>`. A short sha is accepted
#: because that is what `workspace_checkpoints.checkpoint()` hands back to the
#: rest of the repo, and refusing it here would make this module disagree with
#: the only writer of these refs.
CHECKPOINT_REF = re.compile(r"^checkpoint:(?P<sha>[0-9a-fA-F]{4,64})#(?P<path>.+)$")

#: Media type for a payload nobody described. Spelled out rather than left
#: empty so that an adapter comparing media types has a value on both sides:
#: `"" != "application/octet-stream"` would read as a format change caused by
#: this module's own silence.
DEFAULT_MEDIA_TYPE = "application/octet-stream"


@dataclass(frozen=True)
class Resolved:
    """One end of a comparison, as this process could actually read it.

    `size` is the size of the REVISION, never the size of `payload`. When a
    budget cut the read the two differ, `truncated` is True and the reason says
    where the cut fell; a reader that took `len(data())` for the size would
    describe a 400 MB video as an 8 MB one and every ratio computed from it
    would be wrong in the direction that flatters the comparison.

    There is no `parse()`. A `Resolved` is the RESULT of a read, and building
    one from a mapping would be a claim that bytes were read when none were --
    the same reason `classification.Classified` has none. `to_dict()` exists
    because the reason and the sizes belong in a coverage note, and it omits
    the payload for `EvidenceRef`'s reason: evidence is referenced, never
    inlined.
    """

    revision: RevisionRef
    readable: bool
    reason: str = ""
    media_type: str = ""
    size: int = 0
    truncated: bool = False
    payload: Optional[bytes] = None

    def data(self) -> bytes:
        """The bytes that were read. Raises when there are none.

        Not `b""` on an unreadable revision: an empty file and a file nobody
        could open are different facts, and returning the same value for both
        would let "the checkpoint is gone" be compared byte-for-byte against
        "the file is empty" and reported as `unchanged`.
        """
        if not self.readable or self.payload is None:
            raise SourceError(
                f"revision.{self.revision.kind}",
                "was not read, so it has no bytes to hand over; the reason "
                f"was: {self.reason or 'not stated'}",
                got=self.revision.ref,
            )
        return self.payload

    def text(self, *, errors: str = "replace") -> str:
        """The bytes as UTF-8 text. Undecodable bytes are replaced by default.

        `replace` rather than `strict` because this is what a line-count or a
        block hash runs on, and a single invalid byte in a mostly-textual file
        should weaken the comparison, not abort it. A caller that needs the
        bytes to be real text passes `errors="strict"` and handles the failure
        -- `mapping()` is the one that does.
        """
        return self.data().decode("utf-8", errors)

    def mapping(self) -> Dict[str, Any]:
        """The payload as a JSON object. Raises when it is not one.

        A JSON array, a bare number and a truncated document are all refused by
        name. Truncation is refused FIRST and separately: a prefix of a JSON
        document either fails to parse -- an error that reads as "the file is
        corrupt" when the file is fine -- or, for a stream of concatenated
        values, parses into something real that is missing most of itself.
        """
        if self.truncated:
            raise SourceError(
                f"revision.{self.revision.kind}",
                f"was read only in part ({len(self.data())} of {self.size} "
                f"bytes), and a prefix of a JSON document is not that document",
                got=self.revision.ref,
            )
        try:
            loaded = json.loads(self.text(errors="strict"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceError(
                f"revision.{self.revision.kind}",
                f"is not readable as JSON: {exc}",
                got=self.revision.ref,
            ) from exc
        if not isinstance(loaded, dict):
            raise SourceError(
                f"revision.{self.revision.kind}",
                "is valid JSON but not an object; a caller asking for a mapping "
                "would index it and get an error two frames away from the cause",
                got=type(loaded).__name__,
            )
        return loaded

    def sha256(self) -> str:
        """The digest of what was READ. On a truncated read, of the part read.

        Which is why `verify` refuses a truncated `Resolved` outright instead of
        comparing this against `revision.hash`: the two would differ, and the
        honest reason for that difference is the budget, not a revision that
        moved.
        """
        return hashlib.sha256(self.data()).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        """Everything except the bytes -- what belongs in a coverage note."""
        out: Dict[str, Any] = {
            "revision": self.revision.identity(),
            "readable": self.readable,
            "size": self.size,
            "truncated": self.truncated,
        }
        if self.media_type:
            out["media_type"] = self.media_type
        if self.reason:
            out["reason"] = self.reason
        if self.payload is not None:
            out["read_bytes"] = len(self.payload)
        return out


# -- the process-local literal store ---------------------------------------
#
# NOTHING LIVES HERE. This dictionary exists so a caller holding a value in its
# hand -- a config blob it just rendered, a transcript it just received -- can
# compare it against another revision without first writing it somewhere. It is
# forgotten when the process ends, it is not written to disk, it is not shared
# between workers, and it is not persistence: `persistence.py` is persistence,
# and a caller that needs a value to survive this process stores it there or in
# the artifact store and hands over that reference instead.
#
# The consequence to be honest about: a `literal` revision resolved in one
# process is `readable=False` in the next, with a reason that says so rather
# than an empty payload that would read as an empty file.

_STASH_LOCK = threading.RLock()
_STASH: Dict[str, Tuple[bytes, str]] = {}


def _encode(payload: Any) -> Tuple[bytes, str]:
    """Bytes and a media type for a value handed in by a caller.

    Three shapes and no coercion beyond them: bytes are themselves, a string is
    UTF-8, and a mapping or a list is canonical JSON (sorted keys, no stray
    whitespace) so that two callers stashing the same object get the same
    digest and the comparison recognises it. Anything else is refused by name
    rather than `str()`-ed, because `str()` of an object is its repr and two
    equal objects with different reprs would compare as different revisions.
    """
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload), DEFAULT_MEDIA_TYPE
    if isinstance(payload, str):
        return payload.encode("utf-8"), "text/plain"
    if isinstance(payload, (dict, list, tuple)):
        as_json = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, default=str)
        return as_json.encode("utf-8"), "application/json"
    raise SourceError(
        "payload",
        "is not bytes, text, or a JSON-shaped object; stashing its repr would "
        "make two equal values compare as two different revisions",
        got=type(payload).__name__,
    )


def stash(payload: Any, *, media_type: str = "") -> RevisionRef:
    """Put a value in the process-local store and return the ref that names it.

    The returned `RevisionRef` carries the sha256 of the encoded bytes as its
    hash, so rule 2 holds for a literal exactly as it does for a file: two runs
    comparing "the same" literal can be told apart when they were not.

    Re-stashing an identical value is a no-op that returns the same ref: the
    digest IS the key, so there is nothing to overwrite and no second entry.
    """
    raw, guessed = _encode(payload)
    digest = hashlib.sha256(raw).hexdigest()
    kind = str(media_type or "").strip() or guessed
    with _STASH_LOCK:
        _STASH[digest] = (raw, kind)
    return RevisionRef.parse({
        "kind": "literal",
        "ref": f"literal:{digest}",
        "hash": digest,
        "label": f"{len(raw)} bytes of {kind}",
    })


def forget(digest: str) -> None:
    """Drop one stashed value. Missing is not an error.

    Missing is not an error because the store is explicitly ephemeral: a caller
    tidying up after a comparison that already ran in another process would
    otherwise have to catch an exception for the normal case.
    """
    key = str(digest or "").strip().lower()
    with _STASH_LOCK:
        _STASH.pop(key, None)


def stash_size() -> int:
    """How many values this process is holding. For tests and for a leak."""
    with _STASH_LOCK:
        return len(_STASH)


# -- the two things every resolver has to get right ------------------------


def _contained(root: str, candidate: str) -> Optional[str]:
    """`candidate` resolved inside `root`, or `None` when it escapes it.

    §25's "no permitir referencias arbitrarias fuera de scopes", and the only
    real defence this module has. Both sides go through `os.path.realpath`
    BEFORE they are compared, because the three ways out of a workspace --
    `..`, a symlink pointing elsewhere, and an absolute path on another drive
    -- all survive a string comparison and none of them survives a realpath.

    The boundary is checked against `root + os.sep` rather than against `root`
    alone, so that a sibling directory whose name merely starts with the root's
    (`C:\\work` and `C:\\workspace`) is refused instead of accepted.

    Same rule as `workspace_checkpoints._rel`, deliberately duplicated rather
    than imported: that function is private to a module that may stop exporting
    it, and a containment check that can disappear in someone else's refactor
    is not a defence.
    """
    if not root or not candidate:
        return None
    try:
        real_root = os.path.realpath(root)
        joined = candidate if os.path.isabs(candidate) else os.path.join(real_root, candidate)
        real = os.path.realpath(joined)
    except (OSError, ValueError):
        return None
    left, right = real_root, real
    if os.name == "nt":
        left, right = left.lower(), right.lower()
    if right != left and not right.startswith(left.rstrip(os.sep) + os.sep):
        return None
    return real


def _budgeted(revision: RevisionRef, raw: bytes, *, scope: Scope,
              media_type: str, size: Optional[int] = None) -> Resolved:
    """A readable `Resolved`, cut to `scope.budget.max_bytes` and saying so.

    Over budget the result is still `readable=True` -- something WAS read and
    the caller can compare it -- but `truncated` is set and `reason` names both
    numbers. The alternative, refusing the read, would make every large file
    `inconclusive` and hide the cases where the first megabyte already answers
    the question; the alternative in the other direction, cutting silently,
    would let a comparison of two prefixes be reported as a comparison of two
    files.
    """
    real_size = len(raw) if size is None else int(size)
    limit = scope.budget.max_bytes
    if limit is not None and real_size > limit:
        return Resolved(
            revision=revision, readable=True, media_type=media_type,
            size=real_size, truncated=True, payload=raw[:limit],
            reason=(f"read only the first {limit} of {real_size} bytes; "
                    f"`budget.max_bytes` stopped the read and everything past "
                    f"that offset was not compared"),
        )
    return Resolved(revision=revision, readable=True, media_type=media_type,
                    size=real_size, truncated=False, payload=raw)


def _media_type_for(path: str) -> str:
    """A media type guessed from the name, never from the bytes.

    Guessing from content would mean sniffing, and a sniffer that is wrong
    about one file makes the adapter report a format change that never
    happened. The name is what the filesystem asserts, and when it asserts
    nothing the answer is the default rather than an empty string.
    """
    guessed, _encoding = mimetypes.guess_type(path)
    return guessed or DEFAULT_MEDIA_TYPE


# -- the resolvers ---------------------------------------------------------


def _resolve_checkpoint(revision: RevisionRef, *, scope: Scope) -> Resolved:
    """`checkpoint:<sha>#<relative path>` out of the workspace's shadow repo.

    `scope.workspace` is the ABSOLUTE PATH of the workspace, never its display
    name. `workspace_checkpoints` keys its shadow repositories on the realpath
    of the root, so a display name resolves to a repository that does not
    exist, and the honest-looking answer that comes back -- "no checkpoint" --
    is indistinguishable from a real one. The State Mirror made exactly this
    mistake and reported `workspace_available: false` about a repository that
    was sitting in front of it.

    `has_checkpoint` is asked BEFORE `file_at` for the reason its own docstring
    gives: every read there answers an unknown sha with the same empty result
    it gives for "the file did not exist", and those are opposite facts.

    The budget is applied AFTER the read here, unlike in `_resolve_file`, and
    that is a limitation rather than a choice: `workspace_checkpoints` exposes
    no size-before-read for a blob, and the two ways around it -- shelling out
    to `git cat-file -s` from this module, or a `getattr` for a function that
    does not exist -- are both worse than naming the cost. A checkpointed blob
    is bounded by the shadow repo's own `max_file_mb` exclusion, so the
    unbounded window is bounded in practice.
    """
    # Imported here rather than at module scope so that importing the delta
    # package -- which `registry.discover()` does for every adapter, every time
    # -- does not drag in a git-shaped module and its subprocess machinery for
    # callers that never resolve a checkpoint.
    from src import workspace_checkpoints

    workspace = str(scope.workspace or "").strip()
    if not workspace:
        return Resolved(revision=revision, readable=False, reason=(
            "the scope names no workspace, and a checkpoint only exists "
            "relative to one; `scope.workspace` is the absolute path of the "
            "workspace and not its display name"))
    matched = CHECKPOINT_REF.match(revision.ref or "")
    if not matched:
        return Resolved(revision=revision, readable=False, reason=(
            "the ref is not shaped `checkpoint:<sha>#<relative path>`, so "
            "there is no sha to read and no path to read at it"))
    sha, rel = matched.group("sha"), matched.group("path")
    if _contained(workspace, rel) is None:
        return Resolved(revision=revision, readable=False, reason=(
            f"the path {rel!r} resolves outside the workspace; §25 forbids a "
            f"reference outside the scope it was granted, and `..`, a symlink "
            f"and another drive all reach outside one"))
    if not workspace_checkpoints.has_checkpoint(workspace, sha):
        return Resolved(revision=revision, readable=False, reason=(
            f"this workspace's shadow repository does not know checkpoint "
            f"{sha}; it may belong to another machine or another data "
            f"directory, or a reset threw it away"))
    raw = workspace_checkpoints.file_at(workspace, sha, rel)
    if raw is None:
        return Resolved(revision=revision, readable=False, reason=(
            f"{rel} did not exist at checkpoint {sha}"))
    return _budgeted(revision, raw, scope=scope, media_type=_media_type_for(rel))


def _resolve_file(revision: RevisionRef, *, scope: Scope) -> Resolved:
    """A path inside `scope.workspace`, as it is on disk right now.

    Two refusals before a single byte is read, and they are the point of this
    resolver:

    * a path that resolves outside the workspace, checked with `realpath`
      (§25). A string comparison would accept `workspace/../../etc/passwd`,
      a symlink out of the tree, and `E:\\secrets` on Windows;
    * a file whose size is over `budget.max_bytes`, which is read only up to
      the limit. `os.path.getsize` first, always: opening a 40 GB file and
      reading it into memory to discover it was too big is how a comparison
      takes a machine down.

    A `file` revision is by definition mutable -- it is the working tree, not a
    checkpoint -- so the caller that wants immutability calls `verify` after
    this and gets `(False, ...)` when the file moved under it.
    """
    workspace = str(scope.workspace or "").strip()
    if not workspace:
        return Resolved(revision=revision, readable=False, reason=(
            "the scope names no workspace, so there is nothing to resolve this "
            "path against and no boundary to check it does not cross"))
    real = _contained(workspace, revision.ref or "")
    if real is None:
        return Resolved(revision=revision, readable=False, reason=(
            f"{revision.ref!r} resolves outside the workspace; §25 forbids a "
            f"reference outside the scope it was granted"))
    if not os.path.isfile(real):
        return Resolved(revision=revision, readable=False, reason=(
            f"{revision.ref} is not a readable file in this workspace"))
    try:
        size = os.path.getsize(real)
        limit = scope.budget.max_bytes
        with open(real, "rb") as handle:
            raw = handle.read(size if limit is None else min(size, limit))
    except OSError as exc:
        return Resolved(revision=revision, readable=False,
                        reason=f"{revision.ref} could not be read: {exc}")
    # The declared size is the stat size only when the budget actually cut the
    # read; otherwise it is what came back. A file that shrank between the stat
    # and the read is a race a mutable `file` revision can lose, and reporting
    # the stale stat size would put a truncation-shaped gap in the coverage of a
    # read that was complete. `verify` is what catches the race itself.
    truncating = limit is not None and size > limit
    return _budgeted(revision, raw, scope=scope,
                     size=size if truncating else len(raw),
                     media_type=_media_type_for(real))


def _resolve_literal(revision: RevisionRef, *, scope: Scope) -> Resolved:
    """A value `stash()` put in this process's store, addressed by its digest.

    Nothing lives in that store -- see its comment above. A literal stashed by
    another process, or by this one before a restart, is `readable=False` with
    a reason that says the store is ephemeral, because the alternative (an
    empty payload) would be compared byte-for-byte against a real revision and
    reported as a wholesale deletion.
    """
    with _STASH_LOCK:
        entry = _STASH.get(revision.hash)
    if entry is None:
        return Resolved(revision=revision, readable=False, reason=(
            "nothing is stashed under this digest in this process; the literal "
            "store holds values the caller already has in hand, it is "
            "forgotten when the process ends, and it is not persistence"))
    raw, media_type = entry
    return _budgeted(revision, raw, scope=scope, media_type=media_type)


def _unresolvable(kind: str, reason: str) -> Callable[..., Resolved]:
    """A resolver for a kind this module cannot read yet, with a real reason.

    §7's honesty applied to I/O: `readable=False` WITH a reason is a correct
    result, and a reader guessed from the shape of a ref is not. The gap is
    left here, named, rather than filled with a `try: open(ref)` that would
    succeed on the wrong file often enough to be believed.
    """
    def _resolver(revision: RevisionRef, *, scope: Scope) -> Resolved:
        return Resolved(revision=revision, readable=False, reason=reason)

    _resolver.__name__ = f"_resolve_{kind}"
    _resolver.__doc__ = f"`{kind}` revisions are not readable here: {reason}"
    return _resolver


#: `REVISION_KINDS` -> the function that materialises one. Every kind in the
#: closed vocabulary has an entry, and the entries that cannot read anything
#: yet say what is missing instead of being absent: a missing key and a
#: deliberate gap look identical from the outside, and only one of them is a
#: decision somebody made.
#:
#: `state`, `workflow` and `skill` are the documented HOLE. Another agent is
#: writing adapters for those domains, and those adapters bring their own
#: content -- a State Mirror revision is a row this module has no reader for,
#: and a workflow revision is a manifest owned by the workflow store. When
#: those adapters land, they either hand their own bytes to `stash()` or a
#: resolver is added here against the store's real API; what must not happen in
#: the meantime is a guess from the shape of the ref.
RESOLVERS: Dict[str, Callable[..., Resolved]] = {
    "checkpoint": _resolve_checkpoint,
    "file": _resolve_file,
    "literal": _resolve_literal,
    "blob": _unresolvable("blob", (
        "a blob revision names bytes in a store this module has no handle on; "
        "the caller that has those bytes can `stash()` them and compare the "
        "literal it gets back")),
    "artifact": _unresolvable("artifact", (
        "artifact revisions are resolved by the artifact store, which owns "
        "their retention and their sensitivity; reading one by guessing a path "
        "would bypass both")),
    "document": _unresolvable("document", (
        "a document revision names an entry in the document store, and its "
        "text is an extraction from that entry rather than a file on disk")),
    "state": _unresolvable("state", (
        "a State Mirror revision is a row and not a byte stream; the state "
        "adapter brings its own observation of it")),
    "workflow": _unresolvable("workflow", (
        "a workflow revision is a manifest held by the workflow store; the "
        "workflow adapter reads it through that store's own API")),
    "skill": _unresolvable("skill", (
        "a skill revision is a manifest held by the skill store; the skill "
        "adapter reads it through that store's own API")),
}


def resolve(revision: RevisionRef, *, scope: Scope) -> Resolved:
    """Materialise one end of a comparison, or say honestly why not.

    Never raises for a revision it cannot read: an unreadable end is an answer.
    The single exception that does travel is `SourceError`, which means a
    resolver in this module asked a `Resolved` for bytes it had already said it
    did not have -- OUR bug, not a fact about the revision, and turning it into
    a `readable=False` would file it under the one label guaranteed never to be
    investigated.

    The one input it refuses outright is a `RevisionRef` whose kind has no
    entry in `RESOLVERS`. That cannot happen through `RevisionRef.parse` --
    `kind` is validated against
    the closed `REVISION_KINDS` -- and therefore means somebody built the
    dataclass by keyword and invented a kind. That is a caller bug and is
    reported as a `readable=False` naming it, so that even the impossible case
    produces a delta that says what went wrong rather than an exception in the
    middle of a comparison.
    """
    resolver = RESOLVERS.get(str(revision.kind or ""))
    if resolver is None:
        return Resolved(revision=revision, readable=False, reason=(
            f"{revision.kind!r} is not a revision kind this module resolves; "
            f"known kinds: {sorted(RESOLVERS)}"))
    try:
        return resolver(revision, scope=scope)
    except SourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - a reader that throws is a result
        # A resolver that raises has still told us something: which end failed
        # and why. Letting the exception out would discard the other end's work
        # and turn a one-sided failure into a comparison that never happened.
        logger.exception("delta sources: resolving %s raised", revision.identity())
        return Resolved(revision=revision, readable=False, reason=(
            f"reading this revision raised {type(exc).__name__}: {exc}"))


def verify(revision: RevisionRef, resolved: Resolved) -> Tuple[bool, str]:
    """Rule 2, as a check: are these the bytes the caller asked for?

    §3.6 -- "si cambian durante la comparación, invalidar o reiniciar". A
    `file` revision is the working tree and an agent may be writing to it while
    the delta runs; if the digest of what was read is not the digest that was
    asked for, the two ends of the comparison were never both current and the
    result would be a delta about a state that existed for nobody.

    Returns `(False, reason)` and NEVER raises: it is called between two reads
    that already succeeded, and an exception here would throw both away.

    The limitation to state out loud: this compares against `revision.hash`,
    which rule 2 requires to be a sha256 but does not require to be a digest of
    the CONTENT. A revision hashed with `contracts.revision_hash` -- a State
    Mirror row, an artifact version -- has an identity fingerprint there, and
    this function will correctly report that it does not match the content it
    read. Such a caller must not use `verify`; the reason returned says which
    two digests were compared so that the mistake is visible rather than read
    as tampering.
    """
    try:
        if resolved.revision.identity() != revision.identity():
            return False, (
                f"the resolved revision is {resolved.revision.identity()} and "
                f"the one asked about is {revision.identity()}; these are two "
                f"different revisions and comparing them proves nothing")
        if not resolved.readable:
            return False, (resolved.reason
                           or "the revision could not be read, so there is "
                              "nothing to verify it against")
        if resolved.truncated:
            return False, (
                f"only {len(resolved.data())} of {resolved.size} bytes were "
                f"read, so the digest covers a prefix and not the revision; "
                f"the budget cut it, nothing moved")
        digest = resolved.sha256()
        if digest != revision.hash:
            return False, (
                f"the content reads as sha256 {digest} but the revision "
                f"declares {revision.hash}; it moved between being named and "
                f"being read, and a comparison across that boundary was true "
                f"for nobody")
        return True, ""
    except Exception as exc:  # noqa: BLE001 - a verification never raises
        logger.exception("delta sources: verifying %s raised", revision.identity())
        return False, f"verification raised {type(exc).__name__}: {exc}"
