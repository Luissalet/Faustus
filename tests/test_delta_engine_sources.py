"""tests/test_delta_engine_sources.py -- reading one end of a comparison.

Every test fixes a RULE and not a snapshot. The wording of a refusal will
change, `METHOD_TIERS` will grow and the generic adapter will learn to address
more of a byte stream; none of that may turn an unread revision into a
preserved one, and none of these assertions pins a message or counts rows that
are meant to grow.

The guarantees, one test each:

* a path that RESOLVES outside the workspace is refused even when its raw
  spelling starts with the workspace -- the check is `realpath`, not a prefix;
* a sibling directory whose name merely extends the workspace's is outside it;
* a revision whose bytes do not hash to what it declares fails `verify` with a
  reason, and `verify` never raises;
* a file over `budget.max_bytes` is read with the cut declared, keeps the REAL
  size, and the cut reaches the coverage of the comparison built on it;
* a truncated read cannot be verified, and says the budget cut it;
* the literal store is process-local and says so when the value is gone;
* the generic adapter reports `modified` on two differing digests, declares
  that it cannot say what changed inside, and reports `semantic` coverage 0.0;
* the generic adapter never reports `preserved` without an exact hash equality
  -- not on differing bytes, and not on two truncated reads that agree;
* `registry.discover()` finds the generic adapter without anybody adding it to
  a list.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from src.delta_engine import registry, sources
from src.delta_engine.adapters import generic
from src.delta_engine.adapters.base import Scope
from src.delta_engine.contracts import Budget, IntentContract, RevisionRef

OWNER = "alice"
FROZEN = "2026-09-06T12:00:00Z"


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def workspace_with(tmp_path, name: str, body: bytes):
    """A workspace directory holding one file, and its absolute path."""
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return str(root), target


def scope_for(workspace: str, **budget) -> Scope:
    return Scope(owner=OWNER, workspace=workspace, budget=Budget(**budget))


def file_revision(ref: str, body: bytes) -> RevisionRef:
    return RevisionRef.parse({"kind": "file", "ref": ref, "hash": digest(body)})


def intent_with(*invariants) -> IntentContract:
    return IntentContract.parse({
        "id": "intent_test", "owner": OWNER, "domain": "binary",
        "frozen_at": FROZEN, "invariants": list(invariants),
    })


SECURITY = {"id": "security.permissions_not_widened", "class": "permissions",
            "source": "security"}


# -- §25: no reference outside the scope -----------------------------------


def test_a_path_that_resolves_outside_the_workspace_is_refused(tmp_path):
    """`..` escaping the workspace is refused, and a prefix check would not.

    The ref is spelled so that the RAW join starts with the workspace path:
    `workspace/inside/../../outside/secret.txt`. A containment check written as
    a string comparison accepts it, because the string really does begin with
    the workspace. Only `realpath` -- which is what §25 needs -- sees that it
    lands in a sibling directory.
    """
    root, _ = workspace_with(tmp_path, "inside/kept.txt", b"kept\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_bytes(b"not yours\n")

    escaping = "inside/../../outside/secret.txt"
    raw_join = os.path.join(root, escaping)
    assert raw_join.startswith(root), "the test is worthless unless a string check would pass"
    assert os.path.realpath(raw_join) == os.path.realpath(str(secret))

    resolved = sources.resolve(file_revision(escaping, b"not yours\n"),
                               scope=scope_for(root))
    assert resolved.readable is False
    assert "outside the workspace" in resolved.reason
    with pytest.raises(sources.SourceError):
        resolved.data()


def test_a_sibling_directory_sharing_a_name_prefix_is_outside_the_workspace(tmp_path):
    """`.../work` does not contain `.../work_extra`, however the strings sort.

    The boundary is the separator. Without it every directory whose name merely
    extends the workspace's would be readable, which is the second way a prefix
    check lets a reference out of its scope.
    """
    root = tmp_path / "work"
    root.mkdir()
    sibling = tmp_path / "work_extra"
    sibling.mkdir()
    (sibling / "secret.txt").write_bytes(b"not yours\n")

    resolved = sources.resolve(
        file_revision("../work_extra/secret.txt", b"not yours\n"),
        scope=scope_for(str(root)))
    assert resolved.readable is False
    assert "outside the workspace" in resolved.reason


# -- rule 2: the revision has to be the revision ---------------------------


def test_content_that_does_not_match_the_declared_hash_fails_verify(tmp_path):
    """§3.6: a revision that moved invalidates the comparison, it does not raise.

    `verify` runs between two reads that already succeeded, so an exception
    would throw both away. It answers `(False, reason)` and the reason names
    both digests, because "it moved" and "you hashed the wrong thing" are the
    two possibilities and the caller has to be able to tell them apart.
    """
    body = b"the real content\n"
    root, _ = workspace_with(tmp_path, "file.txt", body)
    lying = RevisionRef.parse({"kind": "file", "ref": "file.txt",
                               "hash": digest(b"something else entirely")})

    resolved = sources.resolve(lying, scope=scope_for(root))
    assert resolved.readable is True

    ok, reason = sources.verify(lying, resolved)
    assert ok is False
    assert digest(body) in reason and lying.hash in reason


def test_verify_accepts_the_revision_it_actually_read(tmp_path):
    """The other half of the rule: matching digests verify, with no reason."""
    body = b"the real content\n"
    root, _ = workspace_with(tmp_path, "file.txt", body)
    revision = file_revision("file.txt", body)
    ok, reason = sources.verify(revision, sources.resolve(revision, scope=scope_for(root)))
    assert (ok, reason) == (True, "")


# -- the budget is declared, never silent ----------------------------------


def test_a_file_over_the_budget_is_read_with_the_cut_declared(tmp_path):
    """Over `max_bytes` the read continues, the cut is named, the size is real.

    The size stays the size of the REVISION. A `Resolved` that reported the
    length of what it managed to read would describe a 5000-byte file as a
    1000-byte one, and every ratio computed from it would be wrong in the
    direction that flatters the comparison.
    """
    body = b"x" * 5000
    root, _ = workspace_with(tmp_path, "big.bin", body)
    resolved = sources.resolve(file_revision("big.bin", body),
                               scope=scope_for(root, max_bytes=1000))

    assert resolved.readable is True
    assert resolved.truncated is True
    assert resolved.size == 5000
    assert len(resolved.data()) == 1000
    assert "1000" in resolved.reason and "5000" in resolved.reason


def test_a_truncated_read_cannot_be_verified_and_blames_the_budget(tmp_path):
    """A digest over a prefix is not the revision's digest, and says why.

    Without this the caller reads a hash mismatch as tampering, goes looking
    for a revision that moved, and finds a budget.
    """
    body = b"x" * 5000
    root, _ = workspace_with(tmp_path, "big.bin", body)
    revision = file_revision("big.bin", body)
    resolved = sources.resolve(revision, scope=scope_for(root, max_bytes=1000))

    ok, reason = sources.verify(revision, resolved)
    assert ok is False
    assert "budget" in reason


def test_the_cut_reaches_the_coverage_of_the_comparison(tmp_path):
    """§17: a truncated comparison declares the gap where a reader looks for it.

    The reason travels `Resolved` -> `Snapshot.excluded` -> `Coverage.excluded`,
    and the structural ratio drops below 1.0. A delta that read a fifth of one
    end and reported full structural coverage is the failure this whole
    subsystem exists to prevent.
    """
    body = b"line\n" * 1000
    root, _ = workspace_with(tmp_path, "big.txt", body)
    scope = scope_for(root, max_bytes=1000)
    adapter = generic.GenericAdapter()

    revision = file_revision("big.txt", body)
    snapshot = adapter.snapshot(revision, scope=scope)
    assert snapshot.truncated is True
    assert any("max_bytes" in note for note in snapshot.excluded)

    extraction = adapter.compare(snapshot, snapshot, scope=scope)
    assert any("max_bytes" in note for note in extraction.coverage.excluded)
    structural = extraction.coverage.ratio("structural")
    assert structural is not None and structural < 1.0


# -- the literal store is not persistence ----------------------------------


def test_a_forgotten_literal_is_unreadable_and_not_an_empty_file():
    """The store is process-local, and a missing value says so.

    An empty payload would be compared byte for byte against the other end and
    reported as a wholesale deletion, which is a confident answer about a value
    nobody has.
    """
    revision = sources.stash({"b": 2, "a": 1})
    before = sources.stash_size()
    resolved = sources.resolve(revision, scope=Scope(owner=OWNER))
    assert resolved.readable is True
    assert resolved.mapping() == {"a": 1, "b": 2}
    assert sources.verify(revision, resolved) == (True, "")

    sources.forget(revision.hash)
    assert sources.stash_size() == before - 1
    gone = sources.resolve(revision, scope=Scope(owner=OWNER))
    assert gone.readable is False
    assert "not persistence" in gone.reason


def test_a_kind_with_no_reader_refuses_with_a_reason_rather_than_guessing():
    """§7 applied to I/O: a named gap, never a reader invented from a ref shape."""
    for kind in ("blob", "artifact", "document", "state", "workflow", "skill"):
        revision = RevisionRef.parse({"kind": kind, "ref": f"{kind}:whatever",
                                      "hash": digest(kind.encode())})
        resolved = sources.resolve(revision, scope=Scope(owner=OWNER))
        assert resolved.readable is False, kind
        assert resolved.reason, f"{kind} refused without saying why"


# -- the generic adapter: the floor, and what it refuses to claim ----------


def test_two_differing_digests_are_modified_and_say_what_they_cannot_say(tmp_path):
    """`exact` about the observation, explicit about the emptiness of it.

    The two are not in tension and the pairing is the whole design: a digest
    comparison has no margin, and "the bytes differ" is the entire content of
    the finding. Without the limitation a reader takes an `exact` finding for a
    complete one.
    """
    root, target_file = workspace_with(tmp_path, "a.txt", b"before\n")
    scope = scope_for(root)
    adapter = generic.GenericAdapter()

    source = adapter.snapshot(file_revision("a.txt", b"before\n"), scope=scope)
    target_file.write_bytes(b"after, and quite different\n")
    target = adapter.snapshot(file_revision("a.txt", b"after, and quite different\n"),
                              scope=scope)

    extraction = adapter.compare(source, target, scope=scope)
    content = [f for f in extraction.findings if f.path == generic.CONTENT_KEY]
    assert len(content) == 1
    finding = content[0]
    assert finding.operation == "modified"
    assert (finding.confidence, finding.tier) == ("exact", "hash")
    assert finding.limitations, "a modified digest must declare what it does not know"
    assert any("what changed inside" in note.lower() or "WHAT changed" in note
               for note in finding.limitations)


def test_the_generic_adapter_reports_no_semantic_coverage_at_all(tmp_path):
    """`semantic` is 0.0 and DECLARED, never omitted.

    `Coverage.ratio` answers `None` for an axis nobody measured and `0.0` for
    one measured as empty. This adapter did not fail to read meaning: it does
    not read meaning, and the zero is what makes `coverage.sufficient` refuse to
    rest a conclusion about content on a hash.
    """
    root, _ = workspace_with(tmp_path, "a.txt", b"same\n")
    scope = scope_for(root)
    adapter = generic.GenericAdapter()
    snapshot = adapter.snapshot(file_revision("a.txt", b"same\n"), scope=scope)

    coverage = adapter.compare(snapshot, snapshot, scope=scope).coverage
    assert coverage.ratio("semantic") == 0.0
    assert any("semantic" in note for note in coverage.notes)


def test_preserved_needs_an_exact_hash_equality_and_gets_it_nowhere_else(tmp_path):
    """Rule 1, in the one adapter with the strongest temptation to bend it.

    Identical untruncated bytes preserve every property of those bytes, so
    `preserved` is correct there and `exact` is the honest confidence. Differing
    bytes prove that something changed and nothing about WHICH property broke,
    so the answer is `unknown` -- not `violated`, which would be a claim about a
    structure this adapter never read.
    """
    root, target_file = workspace_with(tmp_path, "a.txt", b"same\n")
    scope = scope_for(root)
    adapter = generic.GenericAdapter()
    intent = intent_with(SECURITY)

    same = adapter.snapshot(file_revision("a.txt", b"same\n"), scope=scope)
    held = adapter.check_invariants(intent, same, same, scope=scope)
    assert [r.status for r in held] == ["preserved"]
    assert held[0].confidence == "exact" and held[0].tier == "hash"
    assert held[0].observations, "a preserved invariant is a measurement"

    target_file.write_bytes(b"different\n")
    other = adapter.snapshot(file_revision("a.txt", b"different\n"), scope=scope)
    changed = adapter.check_invariants(intent, same, other, scope=scope)
    assert [r.status for r in changed] == ["unknown"]
    assert changed[0].limitations


def test_two_truncated_reads_that_agree_are_not_preserved(tmp_path):
    """Agreeing prefixes are not identical files, however equal the digests.

    This is the most expensive `preserved` this module could emit: both ends cut
    at the same offset hash the same, and everything past the cut is exactly
    what nobody compared.
    """
    root, _ = workspace_with(tmp_path, "big.txt", b"same prefix\n" + b"x" * 5000)
    scope = scope_for(root, max_bytes=8)
    adapter = generic.GenericAdapter()
    body = b"same prefix\n" + b"x" * 5000

    snapshot = adapter.snapshot(file_revision("big.txt", body), scope=scope)
    assert snapshot.truncated is True

    results = adapter.check_invariants(intent_with(SECURITY), snapshot, snapshot,
                                       scope=scope)
    assert [r.status for r in results] == ["unknown"]
    assert any("part" in note for note in results[0].limitations)


def test_an_unreadable_end_produces_no_findings_and_no_preserved(tmp_path):
    """"We could not open it" and "it was emptied" must not look the same.

    A `missing` for every element is what a list of removals would say, and
    `verdict.assess` reads `coverage.both_readable` first precisely so that this
    case answers `inconclusive` instead.
    """
    root, _ = workspace_with(tmp_path, "a.txt", b"here\n")
    scope = scope_for(root)
    adapter = generic.GenericAdapter()

    present = adapter.snapshot(file_revision("a.txt", b"here\n"), scope=scope)
    absent = adapter.snapshot(file_revision("gone.txt", b"here\n"), scope=scope)
    assert absent.readable is False

    extraction = adapter.compare(present, absent, scope=scope)
    assert extraction.findings == ()
    assert extraction.coverage.both_readable is False
    results = adapter.check_invariants(intent_with(SECURITY), present, absent,
                                       scope=scope)
    assert [r.status for r in results] == ["unknown"]


# -- discovery: no list to forget ------------------------------------------


def test_the_registry_finds_the_generic_adapter_with_no_list_to_update():
    """Writing the module and declaring `ADAPTER_FACTORY` IS the registration.

    `state_mirror/adapters/__init__.py` records what the alternative cost: five
    correct adapters, green tests, no warning, and no existence as far as the
    running system was concerned, because nobody added them to a tuple. This
    test fails the moment discovery goes back to reading a list.
    """
    registry.reset()
    try:
        discovered = {factory.__module__ for factory in registry.discover()}
        assert generic.__name__ in discovered
        assert callable(getattr(generic, registry.ADAPTER_ATTR))
        assert registry.has_adapter("binary")
        assert registry.adapter_for("binary").domain == "binary"
    finally:
        registry.reset()


# -- checkpoints: the immutable end, read through the shadow repo ----------


def test_a_checkpoint_path_that_escapes_the_workspace_is_refused(tmp_path):
    """The containment check runs before git is ever asked.

    §25 is about the reference, not about whether the reference resolves to
    anything: a path out of the workspace is refused whether or not a shadow
    repository exists, and the refusal names the boundary rather than the
    missing checkpoint.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    revision = RevisionRef.parse({
        "kind": "checkpoint",
        "ref": "checkpoint:abc1234#inside/../../outside/secret.txt",
        "hash": digest(b"not yours"),
    })
    resolved = sources.resolve(revision, scope=scope_for(str(root)))
    assert resolved.readable is False
    assert "outside the workspace" in resolved.reason


def test_a_checkpoint_the_workspace_never_knew_is_named_as_such(tmp_path):
    """`has_checkpoint` before `file_at`: two opposite facts, one empty result.

    Every read in `workspace_checkpoints` answers a sha it never heard of with
    the same empty value it gives for "the file did not exist". A caller that
    could not tell those apart would report "no changes" about a checkpoint
    from another machine.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_bytes(b"here\n")
    revision = RevisionRef.parse({
        "kind": "checkpoint",
        "ref": "checkpoint:" + "0" * 40 + "#file.txt",
        "hash": digest(b"here\n"),
    })
    resolved = sources.resolve(revision, scope=scope_for(str(root)))
    assert resolved.readable is False
    assert "checkpoint" in resolved.reason


def test_a_checkpoint_reads_the_frozen_bytes_and_not_the_working_tree(tmp_path, monkeypatch):
    """Rule 2: a checkpoint is immutable, so editing the file must not move it.

    This is the whole reason a comparison names a checkpoint rather than a
    path. If the resolver read the working tree, the "before" side of every
    delta would be whatever the agent had most recently written, and the
    comparison would report nothing changed while it was changing.

    `scope.workspace` is the ABSOLUTE PATH here, never a display name: the
    shadow repository is keyed on the realpath of the root, so a name resolves
    to a repository that does not exist and answers with the same silence as a
    workspace that has no checkpoints.
    """
    from src import constants, workspace_checkpoints

    if not workspace_checkpoints.git_available():
        pytest.skip("git is not available on this machine")
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"), raising=False)

    root = tmp_path / "workspace"
    root.mkdir()
    frozen_body = b"the content at the checkpoint\n"
    (root / "file.txt").write_bytes(frozen_body)

    made = workspace_checkpoints.checkpoint(str(root), "delta engine test")
    assert made and made.get("sha"), "the shadow repo did not record a checkpoint"

    (root / "file.txt").write_bytes(b"the agent has since rewritten this\n")

    revision = RevisionRef.parse({
        "kind": "checkpoint",
        "ref": f"checkpoint:{made['sha']}#file.txt",
        "hash": digest(frozen_body),
    })
    resolved = sources.resolve(revision, scope=scope_for(str(root)))
    assert resolved.readable is True
    assert resolved.data() == frozen_body
    assert sources.verify(revision, resolved) == (True, "")
