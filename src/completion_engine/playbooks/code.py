"""The coding domain's definition of done, beside the chain that already checks it.

§13 of `inspiration/PLAN_GREEDY_COMPLETION_ENGINE_FAUSTUS.md` lists what a
coding run owes at each layer. This module holds that list as data -- and holds
it next to the chain this repository ALREADY runs inside every turn, because
the "professional layer" of Faustus is not a process waiting to be built. It is
`src/agent_loop.py`'s existing sequence, and a playbook that described a second
one would leave two definitions of "checked" that disagree the first time
either is edited.

**The real chain, in the order the loop runs it, with the names to grep for:**

1.  **syntax** -- `src.agent_harness.static_check_files` writes one entry per
    changed file into `TurnLedger.static_checks`. It proves the file PARSES,
    which the static_checks module's own docstring calls a very low bar.
2.  **static analysis** -- `src.static_checks.run_for_turn` runs the project's
    correctness linter over the changed files, keeps only findings on lines the
    turn ADDED (`changed_lines` + `parse_hunks` against the checkpoint diff),
    and stores `src.static_checks.compact()` in `TurnLedger.static_analysis`.
    `src.static_checks.fix_message` is the one bounded fix round;
    `src.static_checks.ledger_entries` and `merge_static_checks` fold the
    per-file verdicts into the syntax list in place.
3.  **project tests** -- `src.project_tests.run_for_turn` detects the project's
    runner and runs it, `src.project_tests.compare_with_baseline` is what stops
    a pre-existing failure from spending a fix round,
    `src.project_tests.failure_message` is that round, and
    `src.project_tests.compact()` lands in `TurnLedger.tests`.
4.  **review** -- `src.auto_review.review_turn` reads the diff with a fresh
    context, `src.auto_review.ground_findings` drops every finding the reviewer
    cannot point at, `src.auto_review.fix_message` is the round, and
    `src.auto_review.compact()` lands in `TurnLedger.review`.
5.  **proof** -- `src.changesets.from_turn` turns `TurnLedger.summary()` into a
    ChangeSet and `src.changesets.judge` hands it to `src.prove.prove`, whose
    verdict is the `prove` that §13 ends its professional list with.

`CHAIN` states all of that as DATA, and `tests/test_completion_engine_playbooks.py`
imports every name in it. A docstring alone goes stale in silence, and a stale
description of the professional layer is precisely what makes the next person
build a second one.

**What this module deliberately does not do.** It does not re-read the reports
`discovery.py` already reads. `_from_static_check` owns the analyser's
findings, `_from_tests` owns failing and missing tests, `_from_delta`,
`_from_proof`, `_from_changeset` and `_from_state_mirror` own their sources.
Producing a second candidate for the same fact would not be caught by `dedupe`
-- two candidates on different layers have different keys -- and the closeout
would report one problem twice. What is left for a playbook is the half of §13
that the deterministic generators cannot see:

*   the contract's `professional_expectations`, which `_from_playbook` checks
    ONLY when no playbook module exists. Once this module is here that fallback
    never runs, so a playbook that ignored those lines would make the engine
    check strictly LESS than it did without one;
*   the steps of the chain that ran and could not answer -- an analyser that is
    not installed, a run that could not be attributed to the turn, a test run
    that came back inconclusive. `discovery.py` returns nothing for all three
    on purpose (they must cost no fix round), and "the gate could not be
    interpreted" is still a professional gap that a closeout has to name;
*   the contract's own `verification` lines when the ledger shows that nothing
    ran at all.

Two steps of `CHAIN` are described and never read, each for its own reason.
The **syntax** step needs no candidate: `agent_harness` refuses the write when
a file stops parsing, so a turn that got as far as a completion decision has
already passed it, and a candidate for it would be work nobody could do. The
**review** step is an ASSISTED source, and `DiscoveryInput.review` is carried
and deliberately not read for the reason `discovery.py` gives at length -- a
reviewer's verdict is a model's opinion, and the generator that consumes one
belongs to the phase that also builds the contrast for it. A playbook that read
it would be an assisted source wearing a deterministic checklist's name, which
is worse than the same code in a module that admits what it is.

**The limit is code, not a comment.** §13's Límite -- no global refactor, no
framework change, no mass dependency update without changing the Scope Envelope
-- lives in `crosses_the_limit`, and a candidate whose text asks for one of the
three is emitted with `relation="unrelated"`. It is NOT filtered out here. The
envelope refuses `unrelated` by itself (`GREEDY_RELATIONS` leaves it out, so
`scope.admits` answers `out_of_scope`), the refusal is recorded with a reason,
and a recorded refusal is the version anybody can audit. A candidate this
module dropped quietly would leave no trace that the boundary was ever tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Sequence, Tuple

from src.agent_profiles.completion import policy_for
from src.completion_engine import scope as scope_mod
from src.completion_engine.contracts import (
    LAYERS,
    CompletionError,
    ImprovementCandidate,
)

if TYPE_CHECKING:  # pragma: no cover - the annotation only
    from src.completion_engine.discovery import DiscoveryInput

__all__ = [
    "DOMAIN",
    "CHAIN",
    "ChainStep",
    "CORE",
    "PROFESSIONAL",
    "BONUS",
    "LIMITS",
    "crosses_the_limit",
    "definition_of_done",
    "expectations",
    "candidates",
]

DOMAIN = "code"


# -- the chain this repository already runs --------------------------------


@dataclass(frozen=True)
class ChainStep:
    """One step of the turn's own verification chain, as importable names.

    `module` and every name beside it are strings so a test can import the
    module and assert the attribute exists. That is the point of the class: the
    module docstring describes the chain in prose for a reader, and this
    describes it in a form that FAILS when somebody renames a function. Prose
    that has quietly stopped being true is worse than no prose, because it is
    what the next author builds a parallel process on top of.
    """

    step: str            # short name, used in evidence refs and titles
    module: str          # the importable module that owns the step
    entry: str           # the function the agent loop calls
    compactor: str       # what shapes the result for storage, "" when there is none
    ledger_slot: str     # the `TurnLedger.summary()` key it lands in
    fix_round: str       # the function that builds the fix message, "" when none
    discharges: str      # the §13 professional line this step is evidence for

    def names(self) -> Tuple[str, ...]:
        """Every attribute of `module` this step claims exists."""
        return tuple(n for n in (self.entry, self.compactor, self.fix_round) if n)


#: The chain, in the order `src/agent_loop.py` runs it. Read the module
#: docstring for what each one does and why the order is what it is (failing in
#: 0.2 s beats failing in 40 s, which is why static analysis precedes tests).
CHAIN: Tuple[ChainStep, ...] = (
    ChainStep(step="syntax", module="src.agent_harness",
              entry="static_check_files", compactor="", ledger_slot="static_checks",
              fix_round="", discharges="the changed files still parse"),
    ChainStep(step="static_analysis", module="src.static_checks",
              entry="run_for_turn", compactor="compact",
              ledger_slot="static_analysis", fix_round="fix_message",
              discharges="no new warnings, and every name resolves"),
    ChainStep(step="tests", module="src.project_tests",
              entry="run_for_turn", compactor="compact", ledger_slot="tests",
              fix_round="failure_message",
              discharges="the regression test, and the minimal relevant test, pass"),
    ChainStep(step="review", module="src.auto_review",
              entry="review_turn", compactor="compact", ledger_slot="review",
              fix_round="fix_message",
              discharges="a second pass over the diff for consistency"),
    ChainStep(step="proof", module="src.changesets",
              entry="from_turn", compactor="judge", ledger_slot="",
              fix_round="", discharges="`prove`"),
)


# -- §13's three layers, as data -------------------------------------------

#: §13 Core: the request taken literally, and what makes it TRUE.
CORE: Tuple[str, ...] = (
    "reproduce and understand the failure before changing anything",
    "implement the change the request names",
    "read the diff of what was changed",
    "run the minimal test that exercises the change",
)

#: §13 Professional: what this repository would not ship without. Every line
#: here is discharged by a step of `CHAIN` or by the contract, which is what
#: makes the list checkable rather than aspirational.
PROFESSIONAL: Tuple[str, ...] = (
    "add a regression test that fails before the change and passes after it",
    "cover the edge cases adjacent to the one that broke",
    "raise typed errors rather than bare ones",
    "keep the change consistent with the code around it",
    "introduce no new static-analysis warning",
    "update the documentation when the contract changes",
    "prove the result: run `prove` over the change set",
)

#: §13 Greedy, which is the `bonus` layer of `LAYERS`. Named `BONUS` after the
#: layer and not after the mode: `greedy` is a completion MODE and `bonus` is a
#: LAYER, and the contracts module keeps the two axes apart deliberately.
BONUS: Tuple[str, ...] = (
    "look for the same pattern elsewhere in the module or the feature",
    "fix the duplicates that are safe to fix",
    "run the tests related to what changed",
    "review the security and the performance the change touches",
    "remove the code the change left dead",
    "improve the observability of the failure that was fixed",
)

#: Layer name -> its list. Keyed by `LAYERS` names so `definition_of_done` can
#: walk the contract's own order instead of a second one written here.
#: `exploratory` is absent and that absence is deliberate -- see
#: `definition_of_done`.
_LAYER_LISTS: Dict[str, Tuple[str, ...]] = {
    "core": CORE,
    "professional": PROFESSIONAL,
    "bonus": BONUS,
}


# -- §13's Límite, as a check ----------------------------------------------

#: Words that turn a change into a GLOBAL one. A refactor of one handler is
#: ordinary work; a refactor of "the whole codebase" is a different request.
_GLOBAL_WORDS = frozenset({
    "global", "globally", "whole", "entire", "everywhere", "codebase",
    "repository", "repo", "repo-wide", "project-wide", "system-wide",
    "systemwide", "all", "every", "everything",
})

#: Words that turn "framework" into a change OF framework rather than work
#: inside one. "fix the serializer in the framework" must not match; "migrate
#: to another framework" must.
_SWAP_WORDS = frozenset({
    "switch", "switching", "migrate", "migrating", "migration", "move",
    "moving", "port", "porting", "change", "changing", "swap", "swapping",
    "replace", "replacing", "replacement", "another", "different", "rewrite",
})

_BULK_WORDS = frozenset({"all", "every", "mass", "bulk", "whole", "entire"})

_UPDATE_WORDS = frozenset({
    "update", "updates", "updating", "upgrade", "upgrades", "upgrading",
    "bump", "bumping", "refresh", "refreshing",
})

_REFACTOR_WORDS = frozenset({"refactor", "refactoring", "refactors", "rewrite",
                             "rewriting", "restructure", "restructuring"})

_DEPENDENCY_WORDS = frozenset({"dependency", "dependencies", "deps", "packages",
                               "requirements", "lockfile"})

_FRAMEWORK_WORDS = frozenset({"framework", "frameworks", "stack", "runtime"})

#: §13's Límite, as `(name, phrase)` pairs. `crosses_the_limit` returns the
#: name; the phrase is what a receipt prints.
LIMITS: Tuple[Tuple[str, str], ...] = (
    ("global_refactor", "a global refactor"),
    ("framework_change", "a change of framework"),
    ("mass_dependency_update", "a mass dependency update"),
)

_LIMIT_PHRASES: Dict[str, str] = dict(LIMITS)


def _words(text: Any) -> frozenset:
    """The words of a sentence, lowercased, with punctuation as separators."""
    cleaned = "".join(ch.lower() if (ch.isalnum() or ch in "-_") else " "
                      for ch in str(text or ""))
    return frozenset(cleaned.split())


def crosses_the_limit(text: Any) -> str:
    """Which of §13's three boundary changes `text` asks for, or `""`.

    Word sets rather than a regex, because the rule is genuinely about which
    words appear together and a regex would encode word ORDER that the rule
    does not care about: "refactor the whole package" and "a global refactor of
    the package" are the same request.

    The error direction is chosen and it is not symmetric. A false positive
    marks a candidate `unrelated`, the envelope refuses it with reason
    `out_of_scope`, and the refusal is written down where a person can see the
    engine declined something it maybe should not have. A false negative lets a
    framework change through labelled as ordinary adjacent work, which is the
    outcome §13's Límite exists to prevent. So a match is deliberately easy:
    `all`, `every` and `everything` count as globality words even though they
    also appear in innocent sentences.
    """
    words = _words(text)
    if not words:
        return ""
    if (words & _REFACTOR_WORDS) and (words & _GLOBAL_WORDS):
        return "global_refactor"
    if (words & _FRAMEWORK_WORDS) and (words & _SWAP_WORDS):
        return "framework_change"
    if (words & _DEPENDENCY_WORDS) and (words & _BULK_WORDS) and (words & _UPDATE_WORDS):
        return "mass_dependency_update"
    return ""


def limit_phrase(name: str) -> str:
    """The receipt's wording for one of `LIMITS`, or `""` for anything else."""
    return _LIMIT_PHRASES.get(str(name or ""), "")


# -- the checklist, read out -----------------------------------------------


def definition_of_done(*, layer: str = "professional") -> Tuple[str, ...]:
    """Everything owed up to and including `layer`, in `LAYERS` order.

    Cumulative because the layers are: a `professional` finish includes the
    core, and a contract whose `definition_of_done` held only the professional
    lines would let a run report a finished job with the change never made.

    An `exploratory` layer raises rather than returning the `bonus` list.
    §13's maximalist column -- Branching Futures, benchmarks, an alternative
    refactor, adversarial tests -- is not written in this module, and answering
    with somebody else's list would report work as checked that nobody listed.
    That is the same failure `_from_playbook` raises over when a playbook has
    no hook, and it deserves the same answer.
    """
    wanted = str(layer or "").strip()
    if wanted not in _LAYER_LISTS:
        raise CompletionError(
            "layer",
            f"has no checklist in the `{DOMAIN}` playbook; it defines "
            f"{sorted(_LAYER_LISTS)} and returning another layer's list for "
            f"`{wanted}` would report work as checked that nobody wrote down",
            got=layer)
    out: List[str] = []
    for name in LAYERS:
        out.extend(_LAYER_LISTS.get(name, ()))
        if name == wanted:
            break
    return tuple(out)


def expectations(*, mode: str) -> Tuple[str, ...]:
    """What a run in `mode` owes BEYOND the core. §5.3's field, computed.

    The layers a mode may open come from `CompletionPolicy.max_extra_layers`
    via `policy_for`, exactly as `CompletionContract.layers()` computes them --
    read off the policy rather than from a second table keyed by mode name,
    because a second table agrees with `POLICIES` until somebody edits one.

    `literal` therefore answers `()`: it opens the core alone, and a literal
    run that carried professional expectations would be a mode quietly granting
    depth. `maximalist` answers the same as `greedy`, because this playbook has
    no exploratory list -- see `definition_of_done` for why that is silence
    rather than a copy of the bonus one.

    An unknown mode raises out of `policy_for`. Falling back to `greedy` here
    would answer a typo with the second-deepest mode in the product.
    """
    policy = policy_for(mode)
    allowed = LAYERS[:min(1 + max(0, int(policy.max_extra_layers)), len(LAYERS))]
    out: List[str] = []
    for name in allowed:
        if name == "core":
            continue
        out.extend(_LAYER_LISTS.get(name, ()))
    return tuple(out)


# -- turning the checklist into candidates ---------------------------------


def _norm(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip().rstrip("/")


def _touches(path: str, mutated: Sequence[str]) -> bool:
    """Whether one of `mutated` is the path named, at a path boundary.

    Suffix-tolerant in both directions, which is `discovery._touches`'s rule
    and `changesets._matches`'s before it: a contract line says `cart.py` for
    `src/cart.py` constantly, and calling that a miss would leave the line
    permanently unmet. Not prefix-tolerant, so `cart.py` never matches
    `shopping_cart.py`.
    """
    target = _norm(path)
    if not target:
        return False
    for row in mutated:
        other = _norm(row)
        if not other:
            continue
        if other == target or other.endswith("/" + target) or target.endswith("/" + other):
            return True
    return False


def _is_code(path: str) -> bool:
    """Whether a path is a code resource, by `scope.DEFAULT_DOMAINS`'s own list.

    Imported rather than restated: a second extension table would classify
    `.kt` as code in one module and not in the other, and the two answers would
    be used by the same run.
    """
    base = _norm(path).rsplit("/", 1)[-1].lower()
    return any(base.endswith(ext) for ext in scope_mod.DEFAULT_DOMAINS["code"])


def _relation_for(text: Any) -> str:
    """`unrelated` when the text asks for a boundary change, `direct` otherwise.

    `direct` and not something computed per resource, because every candidate
    this module emits is about the errand itself: a line of the contract, or a
    step of the chain that verifies the errand. None of them is about a
    neighbouring file, so a relation computed from paths would only ever
    restate `direct` with more machinery.

    `unrelated` is the load-bearing half. It is not a filter -- the candidate
    is emitted, `scope.admits` refuses it as `out_of_scope`, and §1.8's rule
    that a rejection is RECORDED does the rest. See the module docstring.
    """
    return "unrelated" if crosses_the_limit(text) else "direct"


def _candidate(*, title: str, layer: str, category: str, relation: str,
               evidence: Sequence[str], detail: str = "",
               resources: Sequence[str] = (), verification: Sequence[str] = (),
               dedupe_key: str) -> ImprovementCandidate:
    """One candidate, parsed by the contract so a malformed one cannot escape.

    `source="playbook"` on every row, which `discovery.PLAYBOOK_MODULE`'s
    comment requires and `dedupe` needs: the source is the trust rank that
    decides a collision, and a playbook row that claimed `static_check` would
    win an argument against the analyser itself.

    No score is passed, for `discovery`'s stated reason: a generator that
    priced its own proposals would be the only voice on their value.
    """
    payload: Dict[str, Any] = {
        "title": str(title)[:300],
        "layer": layer,
        "category": category,
        "source": "playbook",
        "relation": relation,
        "evidence_refs": _clean(evidence, limit=32),
        "resources": _clean(resources, limit=64),
        "dedupe_key": str(dedupe_key)[:300],
    }
    if detail:
        payload["detail"] = str(detail)[:2000]
    if verification:
        payload["verification"] = _clean(verification, limit=16)
    return ImprovementCandidate.parse(payload, "candidate")


def _clean(values: Sequence[Any], *, limit: int) -> List[str]:
    """Non-blank, unique, capped, in order. `text_list` refuses the rest."""
    out: List[str] = []
    for value in values or ():
        item = str(value or "").strip()[:512]
        if item and item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _report(data: "DiscoveryInput", name: str) -> Mapping[str, Any]:
    raw = getattr(data, name, None) or {}
    return raw if isinstance(raw, Mapping) else {}


def _from_expectations(data: "DiscoveryInput") -> Tuple[ImprovementCandidate, ...]:
    """The contract's `professional_expectations` that the turn did not cover.

    This generator EXISTS because of the shape of `_from_playbook`: when a
    playbook module is found, it returns that module's output and its own
    fallback loop over `professional_expectations` never runs. Without this,
    adding a playbook to a domain would make the engine check strictly less
    than it checked before the playbook existed -- the worst possible outcome
    for a file whose whole purpose is a checklist.

    The dedupe key is `_from_playbook`'s own spelling, so if both ever ran the
    two rows would be one candidate rather than two descriptions of one gap.
    """
    mutated = data.mutated()
    out: List[ImprovementCandidate] = []
    for line in data.contract.professional_expectations:
        named = scope_mod.resources_named(line, data.workspace)
        if named and any(_touches(path, mutated) for path in named):
            continue
        crossed = crosses_the_limit(line)
        detail = str(line)
        if crossed:
            detail = (f"{line} -- this asks for {limit_phrase(crossed)}, which "
                      f"§13's Límite puts outside the scope envelope until the "
                      f"envelope itself is changed")
        out.append(_candidate(
            title=f"professional expectation not met: {line}"[:300],
            layer="professional", category="correctness",
            relation=_relation_for(line), resources=named, detail=detail,
            evidence=[f"playbook:professional_expectation:{line}"],
            dedupe_key=f"playbook:professional_expectation:{line}",
        ))
    return tuple(out)


def _from_static_gate(data: "DiscoveryInput") -> Tuple[ImprovementCandidate, ...]:
    """The static-analysis gate ran and could not answer. §13: "no warnings".

    `discovery._from_static_check` returns nothing when the report says
    `inconclusive` or did not run, and its docstring gives the reason: an
    analyser that could not be interpreted is not a finding, and those two must
    not be merged. Correct -- and it leaves a real gap unnamed. The chain's
    second step is what discharges "introduce no new warning", and a turn where
    it could not run has not discharged it. That is not a finding about the
    code, it is a hole in the evidence, so it is filed as `verification`.

    Two shapes are reported and one is deliberately not:

    * `unavailable` is set -- `run_for_turn` only fills it when a file with a
      known checker extension changed and no checker was installed, so it
      carries `_install_hint`'s text and is genuinely actionable;
    * `ran` and `inconclusive` -- the tool ran and its output, or the turn's
      diff, could not be attributed. `summary` says which.

    Not reported: a report with neither, which means nothing analysable
    changed. "There was nothing to check" is not a gap, and reporting it would
    put a candidate on every documentation-only turn.
    """
    report = _report(data, "static_checks")
    if not report:
        return ()
    unavailable = str(report.get("unavailable") or "").strip()
    inconclusive = bool(report.get("ran")) and bool(report.get("inconclusive"))
    if not unavailable and not inconclusive:
        return ()
    summary = str(report.get("summary") or "").strip()
    tools = ", ".join(str(t) for t in (report.get("tools") or ()) if t)
    reason = "unavailable" if unavailable else "inconclusive"
    return (_candidate(
        title=f"the static-analysis gate could not check this turn ({reason})",
        layer="professional", category="verification", relation="direct",
        detail=unavailable or summary,
        evidence=[f"playbook:static_analysis:{reason}:{summary or unavailable}"],
        verification=["re-run src.static_checks.run_for_turn"
                      + (f" ({tools})" if tools else "")],
        dedupe_key=f"playbook:static_analysis:{reason}",
    ),)


def _from_test_gate(data: "DiscoveryInput") -> Tuple[ImprovementCandidate, ...]:
    """The project tests ran and the result could not be believed.

    `discovery._from_tests` returns nothing for an inconclusive run, for the
    reason `project_tests` states when it sets the flag: it must cost no fix
    round. This does not ask for a fix round either -- it is a `verification`
    candidate, which is what §9's reserve funds, and the closeout has to be
    able to say that the tests were run and told us nothing rather than
    printing the same sentence it prints for a green suite.

    A report that is absent produces nothing: `run_for_turn` answers `None`
    when the feature is off or the project has no runner, and charging a turn
    with a gap it was configured not to have is the mistake `DiscoveryInput`'s
    docstring names.
    """
    report = _report(data, "tests")
    if not report or not report.get("inconclusive"):
        return ()
    label = str(report.get("label") or report.get("kind") or "the project tests")
    summary = str(report.get("summary") or "").strip()
    command = str(report.get("command") or "").strip()
    return (_candidate(
        title=f"the project tests came back inconclusive ({label})"[:300],
        layer="professional", category="verification", relation="direct",
        detail=summary or "the runner's output could not be interpreted",
        evidence=[f"playbook:tests:inconclusive:{label}:{summary}"],
        verification=[f"re-run {command or label}"],
        dedupe_key="playbook:tests:inconclusive",
    ),)


def _from_verification_promise(data: "DiscoveryInput") -> Tuple[ImprovementCandidate, ...]:
    """The contract named a verification and the ledger shows nothing ran.

    §30's "no terminar antes de verificar core", checked against the two
    records that can answer it: `CompletionContract.verification`, written
    before the work, and `TurnLedger.summary()`, written by the work.

    The guard is that BOTH the static-analysis slot and the tests slot are
    empty. Either one alone would fire on a project with no test runner, which
    is a configuration and not a broken promise; both empty means nothing in
    the chain ran at all, and a contract that promised verification is then
    demonstrably unmet. An empty `ledger_summary` produces nothing, because
    then there is no record to read rather than a record that says nothing
    happened.
    """
    ledger = _report(data, "ledger_summary")
    if not ledger or not data.contract.verification:
        return ()
    if ledger.get("static_analysis") or ledger.get("tests"):
        return ()
    slots = ", ".join(step.ledger_slot for step in CHAIN if step.ledger_slot)
    out: List[ImprovementCandidate] = []
    for line in data.contract.verification:
        out.append(_candidate(
            title=f"promised verification did not run: {line}"[:300],
            layer="professional", category="verification",
            relation=_relation_for(line), detail=str(line),
            evidence=[f"playbook:verification_promised:{line}",
                      f"ledger:no_verification_recorded:{slots}"],
            verification=[str(line)],
            dedupe_key=f"playbook:verification_promised:{line}",
        ))
    return tuple(out)


def _from_bonus(data: "DiscoveryInput") -> Tuple[ImprovementCandidate, ...]:
    """Nothing, on purpose, and this docstring is the deliverable.

    §13's greedy column is `BONUS`, and every line of it needs evidence this
    build does not produce:

    * "the same pattern elsewhere" and "the duplicates that are safe to fix"
      both need a structural index over the tree.
      `discovery._from_similar_case` explains at length that nothing in a
      `DiscoveryInput` contains one, and that a relation asserted from a
      filename instead is exactly what §30 forbids;
    * "the code the change left dead" and "the security and performance the
      change touches" are already the analyser's and the reviewer's findings.
      A second candidate for the same finding would NOT collide in `dedupe` --
      a different layer makes a different `key` -- so the closeout would report
      one problem twice, once as professional and once as bonus;
    * "run the tests related to what changed" is `project_tests`'s own
      `scope="related"`, which `run_for_turn` already applies.

    What is left would be to emit the six lines with the playbook itself as
    their only evidence, which is §30's "no afirmar required sin relación
    demostrable" wearing a checklist. `BONUS` is published instead, for a
    contract to copy into its `definition_of_done` where a person has decided
    the work is wanted -- and there `_from_definition_of_done` picks it up with
    the contract as its evidence, which is a real one.
    """
    return ()


_GENERATORS = (
    _from_expectations,
    _from_static_gate,
    _from_test_gate,
    _from_verification_promise,
    _from_bonus,
)


def candidates(data: "DiscoveryInput") -> Tuple[ImprovementCandidate, ...]:
    """`discovery.PLAYBOOK_HOOK`: the coding checklist, read against the record.

    Deterministic and offline, like every generator in `discovery.py`: run
    twice over the same `DiscoveryInput` it answers the same thing, and it
    answers it without a model, a network call or a file read.

    The order is `_GENERATORS`' order and is stable, because `dedupe` resolves
    a collision by source rank and every row here carries the same source --
    so a stable input order is the only thing that makes the answer
    reproducible.
    """
    out: List[ImprovementCandidate] = []
    for generator in _GENERATORS:
        out.extend(generator(data))
    return tuple(out)
