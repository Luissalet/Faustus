"""tests/test_delta_engine_code.py -- what the `code` adapter must never get wrong.

Every test fixes a RULE and not a snapshot. The wording of a limitation will
change, `METHOD_TIERS` will grow and the extractors will learn more languages;
none of that may turn a rename into a deletion, an unparsed file into a file
with no functions, or a lexical scan into a proof about what the code does.

The guarantees, one test each (§28's "Código" rows first):

* a renamed file is `moved`, ONCE, and never `missing` + `added`;
* a changed signature is its own finding and points at the compatibility
  invariant, so the classifier can reach `regression` without guessing;
* an unexpected dependency and an unexpected configuration key each get their
  own finding, addressed at the thing that changed;
* a withdrawn PUBLIC symbol violates compatibility and a withdrawn private one
  does not -- the promise is about the surface;
* a new `subprocess` import violates the permissions invariant, blocking, and
  at confidence `medium` at the very most: a lexical scan over an AST is not a
  proof that the code runs;
* a file that does not parse stays in the result, labelled `parser_degraded`,
  at tier `algorithm`;
* an unrelated file that did not change comes out `unchanged` at tier `hash` --
  the row that lets anything downstream say `preserved` with an observation
  behind it;
* behavioural coverage is 0.0 and the delta says so out loud;
* a symbol found by a line-start regex is tier `algorithm`, never `parser`;
* a checkpoint revision reads the bytes AT THE SHA and not the ones on disk;
* `registry.discover()` finds this adapter with nobody adding it to a list;
* every invariant id this module points findings at is an id `invariants.py`
  actually declares.
"""

from __future__ import annotations

import hashlib

import pytest

from src.context_engine.code_index import SYMBOL_KINDS
from src.delta_engine import invariants as invariants_mod
from src.delta_engine import registry, sources
from src.delta_engine.adapters import code as code_mod
from src.delta_engine.adapters.base import Scope
from src.delta_engine.contracts import (
    Budget,
    IntentContract,
    RevisionRef,
    confidence_rank,
)

OWNER = "alice"
FROZEN = "2026-09-06T12:00:00Z"

COMPATIBILITY = {"id": code_mod.COMPATIBILITY_INVARIANT, "class": "compatibility"}
PERMISSIONS = {"id": code_mod.PERMISSIONS_INVARIANT, "class": "permissions",
               "source": "security"}
NETWORK = {"id": code_mod.NETWORK_INVARIANT, "class": "security", "source": "security"}


@pytest.fixture()
def adapter() -> code_mod.CodeAdapter:
    return code_mod.CodeAdapter()


@pytest.fixture()
def scope() -> Scope:
    return Scope(owner=OWNER, budget=Budget())


def tree(files: dict) -> RevisionRef:
    """A whole source tree handed in by the caller, as one immutable revision.

    `stash` takes the media type from the caller rather than sniffing it,
    which is the same decision the adapter makes: a JSON object of
    {path: text} and a JSON config file have the same shape, and guessing
    between them would turn `{"a": "b"}` into a tree holding a file called `a`.
    """
    return sources.stash(files, media_type=code_mod.CODE_TREE_MEDIA_TYPE)


def compare(adapter, scope, source_files: dict, target_files: dict):
    """`(extraction, source snapshot, target snapshot)` for two trees."""
    source = adapter.snapshot(tree(source_files), scope=scope)
    target = adapter.snapshot(tree(target_files), scope=scope)
    return adapter.compare(source, target, scope=scope), source, target


def operations(extraction) -> dict:
    """`path -> operation`, for assertions that name what was NOT said."""
    return {finding.path: finding.operation for finding in extraction.findings}


def intent_with(*invariants) -> IntentContract:
    return IntentContract.parse({
        "id": "intent_code", "owner": OWNER, "domain": "code",
        "frozen_at": FROZEN, "invariants": list(invariants),
    })


def results_by_id(results) -> dict:
    return {result.invariant_id: result for result in results}


# -- §28: rename/move against delete+add -----------------------------------


def test_a_renamed_file_is_moved_and_not_a_deletion_plus_an_addition(adapter, scope):
    """The first mandatory row of §28, and the reason the layer exists.

    Reported as `missing` + `added`, a rename is two assertions about a file
    that was never deleted, and the first of them reads as data loss. The test
    asserts the absence of those two rows as hard as it asserts the presence of
    the `moved` one: a `moved` emitted ALONGSIDE them would still leave a
    reader believing a file was lost.
    """
    body = "def handler(request):\n    return request\n"
    extraction, _source, _target = compare(
        adapter, scope,
        {"src/old_name.py": body, "src/kept.py": "VALUE = 1\n"},
        {"src/new_name.py": body, "src/kept.py": "VALUE = 1\n"},
    )
    ops = operations(extraction)
    assert ops.get("file:src/new_name.py") == "moved"
    assert "file:src/old_name.py" not in ops
    assert "missing" not in {ops.get("file:src/old_name.py"), ops.get("file:src/new_name.py")}
    moved = next(f for f in extraction.findings if f.path == "file:src/new_name.py")
    assert moved.tier == "hash" and moved.confidence == "exact"
    assert moved.before == "file:src/old_name.py"


# -- §28: signature / API modified -----------------------------------------


def test_a_changed_signature_is_its_own_finding_pointing_at_compatibility(adapter, scope):
    """Two facts, two rows: the body changed, and the CONTRACT changed.

    Folded into one row the second reads as the first, and the compatibility
    invariant never gets pointed at -- which is the only channel
    `classification.classify` accepts as justification (§16: a required change
    needs a traceable justification, and a name that looks related is not one).
    """
    extraction, source, target = compare(
        adapter, scope,
        {"a.py": "def consume_state(state):\n    return bool(state)\n"},
        {"a.py": "def consume_state(state, strict=False):\n    return bool(state)\n"},
    )
    signature = next(f for f in extraction.findings
                     if f.path == "a.py#signature" or f.path.endswith("#signature"))
    assert signature.operation == "modified"
    assert code_mod.COMPATIBILITY_INVARIANT in signature.invariant_refs
    assert "strict" in signature.after and "strict" not in signature.before

    results = results_by_id(adapter.check_invariants(
        intent_with(COMPATIBILITY), source, target, scope=scope))
    assert results[code_mod.COMPATIBILITY_INVARIANT].status == "violated"
    assert results[code_mod.COMPATIBILITY_INVARIANT].severity == "material"


def test_reformatting_a_signature_is_not_a_signature_change(adapter, scope):
    """The other half of the same rule: a rendering, not the source text.

    Both signatures are rendered from the AST, so re-wrapping a parameter list
    across three lines compares equal. Without this, running a formatter over a
    repository would produce a wall of compatibility violations and nobody
    would read the next real one.
    """
    extraction, source, target = compare(
        adapter, scope,
        {"a.py": "def f(a, b=1, *, c=2):\n    return a\n"},
        {"a.py": "def f(\n    a,\n    b=1,\n    *,\n    c=2,\n):\n    return a\n"},
    )
    assert not [f for f in extraction.findings if f.path.endswith("#signature")]
    results = results_by_id(adapter.check_invariants(
        intent_with(COMPATIBILITY), source, target, scope=scope))
    assert results[code_mod.COMPATIBILITY_INVARIANT].status == "preserved"


# -- §28: an unexpected dependency or configuration key --------------------


def test_a_dependency_and_a_config_key_each_get_their_own_finding(adapter, scope):
    """§9's layers 4 and 5, addressed at the thing that changed.

    A dependency bump and a timeout cut are the two changes a file-level diff
    hides best: both are one line inside a file the request legitimately
    touched. Addressed as `dependency:...#httpx` and `config:...#timeout` they
    are two rows a reader can act on, and the `before`/`after` carry the values
    rather than a digest nobody can compare by eye.
    """
    extraction, _source, _target = compare(
        adapter, scope,
        {"requirements.txt": "httpx==0.27.0\nrich>=13\n",
         "app.json": '{"timeout": 30, "retries": 2}\n'},
        {"requirements.txt": "httpx==0.28.0\nrich>=13\npickleshare==0.7\n",
         "app.json": '{"timeout": 5, "retries": 2}\n'},
    )
    findings = {f.path: f for f in extraction.findings}

    bumped = findings["dependency:requirements.txt#httpx"]
    assert bumped.operation == "modified"
    assert bumped.before == "==0.27.0" and bumped.after == "==0.28.0"

    assert findings["dependency:requirements.txt#pickleshare"].operation == "added"
    assert findings["dependency:requirements.txt#rich"].operation == "unchanged"

    timeout = findings["config:app.json#timeout"]
    assert timeout.operation == "modified"
    assert (timeout.before, timeout.after) == ("30", "5")
    assert findings["config:app.json#retries"].operation == "unchanged"


def test_a_dependency_change_is_not_reported_twice_as_a_config_key(adapter, scope):
    """One fact, one address. `package.json` is a manifest AND a config file.

    Emitting `dependency:package.json#httpx` and `config:package.json#dependencies.httpx`
    for one bump is not thoroughness: it is a reader reconciling a delta
    against itself, and the second row makes the first look like a duplicate
    that can be skipped.
    """
    extraction, _source, _target = compare(
        adapter, scope,
        {"package.json": '{"name": "app", "dependencies": {"axios": "1.0.0"}}\n'},
        {"package.json": '{"name": "app", "dependencies": {"axios": "1.1.0"}}\n'},
    )
    paths = {f.path for f in extraction.findings}
    assert "dependency:package.json#axios" in paths
    assert not [path for path in paths if path.startswith("config:package.json#dependencies")]
    assert "config:package.json#name" in paths


# -- §28: a withdrawn public symbol ----------------------------------------


def test_a_withdrawn_public_symbol_violates_compatibility(adapter, scope):
    """A withdrawn export breaks callers this comparison cannot see."""
    extraction, source, target = compare(
        adapter, scope,
        {"api.py": "def public_one():\n    return 1\n\n\ndef public_two():\n    return 2\n"},
        {"api.py": "def public_one():\n    return 1\n"},
    )
    missing = next(f for f in extraction.findings
                   if f.path == "symbol:api.py#public_two")
    assert missing.operation == "missing"
    assert code_mod.COMPATIBILITY_INVARIANT in missing.invariant_refs

    results = results_by_id(adapter.check_invariants(
        intent_with(COMPATIBILITY), source, target, scope=scope))
    assert results[code_mod.COMPATIBILITY_INVARIANT].status == "violated"


def test_a_withdrawn_private_symbol_does_not_violate_compatibility(adapter, scope):
    """The other half, and the reason the rule is about the SURFACE.

    A private helper carries no promise to anyone outside the module. Reporting
    its removal as a compatibility break would bury the removals that really do
    break a caller, which is the same failure as not reporting them.
    """
    extraction, source, target = compare(
        adapter, scope,
        {"api.py": "def public_one():\n    return 1\n\n\ndef _helper():\n    return 2\n"},
        {"api.py": "def public_one():\n    return 1\n"},
    )
    missing = next(f for f in extraction.findings if f.path == "symbol:api.py#_helper")
    assert missing.operation == "missing"
    assert code_mod.COMPATIBILITY_INVARIANT not in missing.invariant_refs

    results = results_by_id(adapter.check_invariants(
        intent_with(COMPATIBILITY), source, target, scope=scope))
    assert results[code_mod.COMPATIBILITY_INVARIANT].status == "preserved"
    assert results[code_mod.COMPATIBILITY_INVARIANT].observations


# -- §28: permissions, and the ceiling on what a lexical scan may claim -----


def test_a_new_subprocess_import_is_a_blocking_permission_violation(adapter, scope):
    """§9's security layer, with the confidence the method actually justifies.

    Blocking, because `_CLASS_SEVERITY_FLOOR` refuses a `permissions`
    invariant filed any lower -- a security property that can be waived by
    lowering its severity is not an invariant. And `medium` at the very most,
    because this is a lexical scan over an AST: it sees the token, not the
    execution. An `exact` here would be a reading of the text presented as a
    proof about the behaviour, and nobody who read `exact` would go and check.
    """
    _extraction, source, target = compare(
        adapter, scope,
        {"job.py": "import os\n\n\ndef run(cmd):\n    return cmd\n"},
        {"job.py": "import os\nimport subprocess\n\n\ndef run(cmd):\n    return subprocess.run(cmd)\n"},
    )
    results = results_by_id(adapter.check_invariants(
        intent_with(PERMISSIONS, NETWORK), source, target, scope=scope))

    permissions = results[code_mod.PERMISSIONS_INVARIANT]
    assert permissions.status == "violated"
    assert permissions.severity == "blocking"
    assert confidence_rank(permissions.confidence) >= confidence_rank("medium")
    assert permissions.tier == "algorithm"
    assert any("subprocess" in observation for observation in permissions.observations)
    assert permissions.limitations

    # The network invariant watches other tokens and is not dragged along by a
    # violation of its neighbour: two properties, two answers.
    assert results[code_mod.NETWORK_INVARIANT].status == "preserved"


def test_a_new_network_import_violates_the_network_invariant(adapter, scope):
    """The same scan, the other token set. `requests` is reach, not execution."""
    _extraction, source, target = compare(
        adapter, scope,
        {"client.py": "def fetch(url):\n    return url\n"},
        {"client.py": "import requests\n\n\ndef fetch(url):\n    return requests.get(url)\n"},
    )
    results = results_by_id(adapter.check_invariants(
        intent_with(PERMISSIONS, NETWORK), source, target, scope=scope))
    assert results[code_mod.NETWORK_INVARIANT].status == "violated"
    assert results[code_mod.PERMISSIONS_INVARIANT].status == "preserved"


def test_secrets_are_never_reported_preserved_by_an_import_scan(adapter, scope):
    """Rule 1 of `contracts.py`, in the place it is cheapest to break.

    Nothing here scans for credentials. Answering `preserved` about secrets
    because no import moved would be the exact shape of the lie the whole
    subsystem exists to refuse, so the id this adapter cannot check answers
    `unknown` with the reason named.
    """
    _extraction, source, target = compare(
        adapter, scope, {"a.py": "X = 1\n"}, {"a.py": "X = 2\n"})
    results = results_by_id(adapter.check_invariants(
        intent_with({"id": code_mod.SECRETS_INVARIANT, "class": "security",
                     "source": "security"}),
        source, target, scope=scope))
    result = results[code_mod.SECRETS_INVARIANT]
    assert result.status == "unknown"
    assert result.confidence == "unknown"
    assert any("credential" in limitation for limitation in result.limitations)


# -- §28: a degraded parser, labelled ---------------------------------------


def test_a_file_that_does_not_parse_stays_in_the_result_and_says_so(adapter, scope):
    """§28's "parser degradado etiquetado", both halves of it.

    Labelled, so a reader knows the row rests on bytes and not on structure;
    and PRESENT, because a degraded file that vanished from the output would
    look exactly like a file nobody touched. The tier drops to `algorithm`:
    the hash comparison still ran, and what it is worth fell when the structure
    behind those bytes went unread.
    """
    extraction, source, target = compare(
        adapter, scope,
        {"broken.py": "def ok():\n    return 1\n", "fine.py": "def kept():\n    return 2\n"},
        {"broken.py": "def ok(:\n    return 1\n", "fine.py": "def kept():\n    return 2\n"},
    )
    degraded = next(f for f in extraction.findings if f.path == "file:broken.py")
    assert degraded.operation == "modified"
    assert degraded.tier == "algorithm"
    assert any(limitation.startswith(code_mod.PARSER_DEGRADED)
               for limitation in degraded.limitations)
    assert confidence_rank(degraded.confidence) >= confidence_rank("high")

    # The file that DID parse keeps its own strength: one broken file does not
    # lower the evidence about the rest of the tree.
    assert next(f for f in extraction.findings
                if f.path == "file:fine.py").tier == "hash"
    assert dict(target.detail)["parse_failures"].get("broken.py")


def test_a_broken_parser_leaves_every_invariant_unknown_and_never_preserved(adapter, scope):
    """A property nobody could check is `unknown`, with the gap named.

    The symbols of the unparsed file were never read, so "no public symbol was
    withdrawn" is a statement about a set that is missing a member. `preserved`
    there would be a conclusion drawn from a detector that did not run.
    """
    _extraction, source, target = compare(
        adapter, scope,
        {"api.py": "def public_one():\n    return 1\n"},
        {"api.py": "def public_one(:\n    return 1\n"},
    )
    result = results_by_id(adapter.check_invariants(
        intent_with(COMPATIBILITY), source, target, scope=scope))[
        code_mod.COMPATIBILITY_INVARIANT]
    assert result.status == "unknown"
    assert result.confidence == "unknown"
    assert any("parse" in limitation for limitation in result.limitations)


# -- §28: an unrelated file that did not change -----------------------------


def test_an_unrelated_unchanged_file_is_reported_unchanged_at_tier_hash(adapter, scope):
    """The row that costs nothing to emit and everything to omit.

    `unchanged` says we LOOKED. Without it the honest answer about every file
    the change did not touch is `unknown`, and `classification.classify` can
    never reach `preserved` -- which `DeltaAssertion.parse` refuses to spell
    without an observation behind it.
    """
    untouched = "SETTINGS = {'a': 1}\n"
    extraction, _source, _target = compare(
        adapter, scope,
        {"edited.py": "def f():\n    return 1\n", "untouched.py": untouched},
        {"edited.py": "def f():\n    return 2\n", "untouched.py": untouched},
    )
    finding = next(f for f in extraction.findings if f.path == "file:untouched.py")
    assert finding.operation == "unchanged"
    assert finding.tier == "hash"
    assert finding.confidence == "exact"
    assert not finding.limitations


# -- §28: behavioural coverage ----------------------------------------------


def test_behavioural_coverage_is_zero_and_the_delta_says_it_out_loud(adapter, scope):
    """§9: a green test does not prove the absence of collateral change.

    The zero is DECLARED and not omitted: `Coverage.ratio` returns `None` for a
    dimension nobody measured and `0.0` for one measured as empty, and this
    adapter did not fail to run tests -- it does not run them. That difference
    is what makes `coverage.sufficient(..., dimensions=("behavioral",))` refuse
    to rest a conclusion about behaviour on a symbol table.
    """
    extraction, _source, _target = compare(
        adapter, scope, {"a.py": "def f():\n    return 1\n"},
        {"a.py": "def f():\n    return 2\n"})
    assert extraction.coverage.ratio("behavioral") == 0.0
    assert any("behavioural" in note or "test" in note
               for note in extraction.coverage.notes)
    assert extraction.coverage.ratio("semantic") == 1.0


def test_an_unreadable_end_produces_no_findings_and_says_which_end(adapter, scope):
    """"We could not open it" and "it was emptied" are opposite facts.

    A `missing` row for every element would say the second. The coverage keeps
    the first, which is what `verdict.assess` reads before anything else to
    answer `inconclusive`.
    """
    source = adapter.snapshot(tree({"a.py": "X = 1\n"}), scope=scope)
    gone = RevisionRef.parse({
        "kind": "literal", "ref": "literal:" + "0" * 64, "hash": "0" * 64})
    target = adapter.snapshot(gone, scope=scope)
    assert target.readable is False

    extraction = adapter.compare(source, target, scope=scope)
    assert extraction.findings == ()
    assert extraction.coverage.both_readable is False
    assert any("target" in limitation for limitation in extraction.limitations)


# -- the determinism ladder, per language -----------------------------------


def test_a_symbol_found_by_a_regex_is_algorithm_and_never_parser(adapter, scope):
    """§3.2: a regex over line starts is not a parser, and the tier says so.

    The Python symbols in the same comparison stay at `parser`. One tree, two
    tiers, decided per element by which extractor actually ran -- a snapshot
    that graded them together would either promote the regex or demote the AST,
    and both directions lose information a reader needs.
    """
    extraction, source, _target = compare(
        adapter, scope,
        {"app.js": "export function handler(a) {\n  return a;\n}\n",
         "app.py": "def handler(a):\n    return a\n"},
        {"app.js": "export function handler(a, b) {\n  return a;\n}\n",
         "app.py": "def handler(a, b):\n    return a\n"},
    )
    by_path = {f.path: f for f in extraction.findings}
    assert by_path["symbol:app.js#handler"].tier == "algorithm"
    assert by_path["symbol:app.py#handler"].tier == "parser"

    # And the semantic coverage counts the JavaScript file in the denominator:
    # its symbols were found, and not by anything that read the language.
    assert 0.0 < extraction.coverage.ratio("semantic") < 1.0


def test_every_symbol_kind_comes_from_the_structural_index_vocabulary(adapter, scope):
    """One vocabulary for symbols across this repository, not two.

    `code_index.SYMBOL_KINDS` already names what a symbol can be. An adapter
    inventing `func` next to its `function` would make two indexes of the same
    tree incomparable for a reason nobody would find by reading either of them.
    """
    snapshot = adapter.snapshot(tree({
        "m.py": "CONSTANT = 1\n\n\nclass Thing:\n    def method(self):\n        return 1\n\n\ndef free():\n    return 2\n",
    }), scope=scope)
    kinds = {element.kind for element in snapshot.elements
             if element.key.startswith("symbol:")}
    assert kinds <= set(SYMBOL_KINDS)
    assert {"class", "method", "function", "constant"} <= kinds


# -- revisions: a checkpoint is not the working tree ------------------------


def test_a_checkpoint_revision_reads_the_bytes_at_the_sha(adapter, tmp_path,
                                                          monkeypatch):
    """The whole reason a checkpoint is a revision and the tree is not.

    The file on disk holds the NEW body here. If the snapshot read the disk it
    would compare the target against itself and report that nothing changed --
    a delta that was true for nobody. The checkpoint's own bytes are what the
    comparison is anchored to, which is rule 2 of `contracts.py` reaching the
    filesystem.

    A path deleted since the checkpoint is still part of it, and comes back
    from the `D` rows of `changed_since`: a file list taken from the tree alone
    would lose exactly the files whose disappearance matters most.
    """
    from src import workspace_checkpoints

    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    at_sha = {
        "src/a.py": b"def f():\n    return 1\n",
        "src/gone.py": b"REMOVED = 1\n",
    }
    monkeypatch.setattr(workspace_checkpoints, "git_available", lambda: True)
    monkeypatch.setattr(workspace_checkpoints, "has_checkpoint",
                        lambda workspace, sha: True)
    monkeypatch.setattr(workspace_checkpoints, "changed_since",
                        lambda workspace, sha, paths=None: [
                            {"status": "D", "path": "src/gone.py"}])
    monkeypatch.setattr(workspace_checkpoints, "file_at",
                        lambda workspace, sha, path: at_sha.get(path))

    revision = RevisionRef.parse({
        "kind": "checkpoint", "ref": "checkpoint:abc1234",
        "hash": hashlib.sha256(b"abc1234").hexdigest()})
    snapshot = adapter.snapshot(revision, scope=Scope(owner=OWNER, workspace=str(root)))

    index = snapshot.index()
    assert snapshot.readable is True
    assert index["file:src/a.py"].hash == hashlib.sha256(at_sha["src/a.py"]).hexdigest()
    assert "file:src/gone.py" in index
    assert "return 1" in "".join(at_sha["src/a.py"].decode())


def test_an_unknown_checkpoint_is_unreadable_with_a_reason(adapter, tmp_path,
                                                           monkeypatch):
    """"No such checkpoint" and "nothing changed" are opposite facts.

    `workspace_checkpoints` answers an unknown sha with the same empty result
    it gives for "the file did not exist", so the adapter asks
    `has_checkpoint` before it reads anything and reports a REASON rather than
    an empty snapshot that reads as a revision with no files in it.
    """
    from src import workspace_checkpoints

    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(workspace_checkpoints, "git_available", lambda: True)
    monkeypatch.setattr(workspace_checkpoints, "has_checkpoint",
                        lambda workspace, sha: False)
    revision = RevisionRef.parse({
        "kind": "checkpoint", "ref": "checkpoint:deadbee",
        "hash": hashlib.sha256(b"deadbee").hexdigest()})
    snapshot = adapter.snapshot(revision, scope=Scope(owner=OWNER, workspace=str(root)))
    assert snapshot.readable is False
    assert snapshot.elements == ()
    assert any("checkpoint" in note for note in snapshot.notes)


def test_a_file_revision_and_a_literal_align_by_a_stable_address(adapter, tmp_path):
    """Two ends of one file have to produce one address or nothing compares.

    A `file` revision is addressed by its path relative to the workspace on
    both sides. Without that, an unchanged file would be a `missing` plus an
    `added` -- the same failure as the rename, arriving through the naming of
    the element instead of through alignment.
    """
    root = tmp_path / "ws"
    root.mkdir()
    path = root / "a.py"
    path.write_text("def f():\n    return 1\n", encoding="utf-8")
    scope = Scope(owner=OWNER, workspace=str(root))
    body = path.read_bytes()
    revision = RevisionRef.parse({
        "kind": "file", "ref": "a.py", "hash": hashlib.sha256(body).hexdigest()})
    source = adapter.snapshot(revision, scope=scope)

    path.write_text("def f():\n    return 2\n", encoding="utf-8")
    changed = path.read_bytes()
    target = adapter.snapshot(RevisionRef.parse({
        "kind": "file", "ref": "a.py",
        "hash": hashlib.sha256(changed).hexdigest()}), scope=scope)

    extraction = adapter.compare(source, target, scope=scope)
    assert operations(extraction)["file:a.py"] == "modified"


# -- budgets ----------------------------------------------------------------


def test_a_budget_that_cuts_the_snapshot_is_reported_and_violates_its_invariant(adapter):
    """§22: past the budget the delta describes part of the change.

    The part nobody read is not evidence that nothing happened there, so the
    cut is reported in `excluded`, the snapshot says `truncated`, and the
    budget invariant is `violated` rather than the comparison quietly
    describing a smaller repository than the one it was given.
    """
    scope = Scope(owner=OWNER, budget=Budget(max_elements=2))
    files = {f"m{index}.py": f"def f{index}():\n    return {index}\n"
             for index in range(6)}
    source = adapter.snapshot(tree(files), scope=scope)
    assert source.truncated is True
    assert source.excluded

    # The file layer survives the cut: those hashes are what every later step
    # reads, and a budget that removed one would make a large tree look like a
    # tree missing a file.
    assert all(element.key.startswith("file:") for element in source.elements)

    target = adapter.snapshot(tree(files), scope=scope)
    result = results_by_id(adapter.check_invariants(
        intent_with({"id": code_mod.BUDGET_INVARIANT, "class": "budget"}),
        source, target, scope=scope))[code_mod.BUDGET_INVARIANT]
    assert result.status == "violated"
    assert result.confidence == "exact"


# -- registration and the invariant catalogue -------------------------------


def test_the_registry_finds_this_adapter_without_anybody_updating_a_list():
    """§28 in spirit: a module that declares `ADAPTER_FACTORY` IS registered.

    `state_mirror/adapters/__init__.py` records what a tuple cost -- five
    correct adapters that did not exist as far as the running system was
    concerned, with green tests and no warning. This test fails the moment the
    factory stops being discoverable, which is the only way that failure
    becomes visible before somebody goes looking.
    """
    domains = {getattr(factory(), "domain", "") for factory in registry.discover()}
    assert {"code", "document"} <= domains
    assert registry.has_adapter("code")
    assert registry.adapter_for("code").domain == "code"


def test_every_invariant_id_this_adapter_names_is_one_the_catalogue_declares():
    """A finding must never point at an invariant nobody declared.

    The ids live in `code.py` as constants so that nothing indexes into
    `invariants.DOMAIN_DEFAULTS` by position. This test is the other half of
    that decision: rename one over there and a test fails here, instead of the
    adapter quietly producing `invariant_refs` that `classification` can never
    match against a result.
    """
    declared = {item["id"] for item in invariants_mod.SECURITY_INVARIANTS}
    declared |= {item["id"] for item in invariants_mod.DOMAIN_DEFAULTS["code"]}
    for named in (code_mod.COMPATIBILITY_INVARIANT, code_mod.PERMISSIONS_INVARIANT,
                  code_mod.NETWORK_INVARIANT, code_mod.SECRETS_INVARIANT,
                  code_mod.BUDGET_INVARIANT):
        assert named in declared
