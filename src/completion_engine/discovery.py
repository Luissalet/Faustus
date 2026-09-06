"""Where improvements come from, and why none of them come from a model.

Section 7 of `inspiration/PLAN_GREEDY_COMPLETION_ENGINE_FAUSTUS.md`. Every
generator in this module is DETERMINISTIC: it reads the record of what already
happened -- the ledger, the static analyser, the test runner, the change set,
`prove`, the Universal Delta, the State Mirror -- and turns a fact that is
already written down into an `ImprovementCandidate`. Run twice over the same
inputs it answers the same thing, and it answers it offline.

**§7's assisted sources are deliberately absent.** The plan lists a reviewer, a
council, a domain expert, creative models and a Taste Engine, and this phase
builds none of them. Two reasons, and the second is the one that matters:

*   §30: "no usar un LLM como único estimador de valor". A model is allowed to
    be one voice about what is worth doing; it is not allowed to be the only
    one, and a package whose first generator was a model would make it the only
    one on the day the deterministic ones return nothing.
*   A generator that proposes work AND scores its own proposals has no
    contrast. Nothing in this module scores at all -- `expected_value`,
    `estimated_cost`, `risk` and `confidence` are left at their defaults for
    the frontier to fill in from evidence it holds and this module does not.
    Discovery says "here is a fact"; pricing that fact is somebody else's
    signature.

`DiscoveryInput.review` is carried and never read here for exactly that reason:
an auto-review verdict is an assisted source, so the generator that consumes it
belongs to the phase that also builds the contrast for it. Carrying the field
now means the shape does not change when that phase arrives.

Two more rules the file exists to hold:

*   **Evidence or nothing.** Every candidate names what produced it in
    `evidence_refs`, and the `core` and `professional` layers are refused by
    `ImprovementCandidate.parse` without one. §30: "no afirmar required sin
    relación demostrable" -- a layer that can block the close, resting on
    nobody's evidence, is an opinion with a veto.
*   **A resolved candidate is retired, not re-proposed.** `resolved_by` is §9's
    "eliminar las resueltas indirectamente". Without it the list only ever
    grows, and a frontier that re-proposes what the last round already fixed
    spends its budget arguing with itself.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, List, Mapping, Optional, Sequence, Set,
                    Tuple)

from src.completion_engine import scope
from src.completion_engine.contracts import (
    DISCOVERY_SOURCES,
    CompletionContract,
    CompletionError,
    ImprovementCandidate,
    ScopeEnvelope,
)
from src.prove import VERDICTS, top_uncertainty

__all__ = [
    "DiscoveryInput",
    "GENERATORS",
    "PLAYBOOK_MODULE",
    "PLAYBOOK_HOOK",
    "discover",
    "dedupe",
    "resolved_by",
]


@dataclass(frozen=True)
class DiscoveryInput:
    """Everything the generators may look at. Read-only, and all of it evidence.

    The mappings are the compact forms the turn already produces -- there is no
    second serialisation here, because a second one would drift from the first
    and the whole value of these sources is that they are what the run actually
    recorded:

      * `ledger_summary` is `agent_harness.TurnLedger.summary()`;
      * `static_checks` is `static_checks.compact()`;
      * `tests` is `project_tests.compact()`;
      * `review` is `auto_review.compact()` -- carried, not read; see above;
      * `changeset` is the `harness_summary["changeset"]` block, `verdict`,
        `unsupported_claims` and `unclaimed_changes` included;
      * `proof` is a `prove.Proof`;
      * `delta` is `UniversalDelta.to_dict()`, optionally carrying the
        `unmet` and `out_of_scope` lists of `delta_engine.Classified`;
      * `situations` maps a `state_mirror.queries.SITUATIONS` name to its rows.

    A key that is absent produces NO candidates. An absent report is not
    evidence that the thing it reports on is fine, and it is not evidence that
    it is broken either -- an integration that did not run belongs in
    `CompletionDecision.degraded_integrations`, not in the frontier.
    """

    contract: CompletionContract
    envelope: ScopeEnvelope
    ledger_summary: Mapping[str, Any] = field(default_factory=dict)
    static_checks: Mapping[str, Any] = field(default_factory=dict)
    tests: Mapping[str, Any] = field(default_factory=dict)
    review: Mapping[str, Any] = field(default_factory=dict)
    changeset: Mapping[str, Any] = field(default_factory=dict)
    proof: Mapping[str, Any] = field(default_factory=dict)
    delta: Mapping[str, Any] = field(default_factory=dict)
    situations: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    workspace: str = ""

    def mutated(self) -> Tuple[str, ...]:
        """The paths this turn actually wrote, in the ledger's own spelling."""
        rows = (self.ledger_summary or {}).get("mutations") or []
        out: List[str] = []
        for row in rows:
            path = _norm(row)
            if path and path not in out:
                out.append(path)
        return tuple(out)

    def relation_of(self, resource: str) -> str:
        """The candidate's distance, computed once, the same way everywhere."""
        return scope.relation_of(resource, self.envelope, touched=self.mutated(),
                                 goal_terms=scope.resources_named(self.envelope.goal,
                                                                  self.workspace))


# -- small shared helpers --------------------------------------------------


def _norm(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip().rstrip("/")


def _rows(container: Any, key: str) -> Tuple[Any, ...]:
    """A list under `key`, or empty. A mapping or a string is not a list here."""
    raw = (container or {}).get(key) if isinstance(container, Mapping) else None
    if raw is None or isinstance(raw, (str, bytes, Mapping)):
        return ()
    try:
        return tuple(raw)
    except TypeError:
        return ()


def _candidate(*, title: str, layer: str, category: str, source: str,
               relation: str, evidence: Sequence[str],
               resources: Sequence[str] = (), detail: str = "",
               verification: Sequence[str] = (), dedupe_key: str = "",
               required_effects: Sequence[str] = (),
               reversibility: str = "full") -> ImprovementCandidate:
    """One candidate, parsed by the contract so a malformed one cannot escape.

    No score is passed. §30 and the module docstring: a generator that priced
    its own proposals would be the only voice on their value, and the contract
    keeps `expected_value` at 0.0 until something with a different vantage
    point fills it in.
    """
    payload: Dict[str, Any] = {
        "title": str(title)[:300],
        "layer": layer,
        "category": category,
        "source": source,
        "relation": relation,
        "reversibility": reversibility,
        "evidence_refs": _clean(evidence, limit=32),
        "resources": _clean(resources, limit=64),
    }
    if detail:
        payload["detail"] = str(detail)[:2000]
    if verification:
        payload["verification"] = _clean(verification, limit=16)
    if dedupe_key:
        payload["dedupe_key"] = str(dedupe_key)[:300]
    if required_effects:
        payload["required_effects"] = _clean(required_effects, limit=16)
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


def _touches(path: str, mutated: Sequence[str]) -> bool:
    """Whether a mutated path is the one named, at a path boundary.

    Suffix-tolerant in both directions for `changeset._matches`'s reason: a
    contract says `cart.py` for `src/cart.py` constantly, and calling that a
    miss would leave every definition-of-done line permanently unmet. It is not
    prefix-tolerant, so `cart.py` never matches `shopping_cart.py`.
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


_TEST_MARKERS: Tuple[str, ...] = (".test.", ".spec.")


def _is_test_path(path: str) -> bool:
    """The three test-file conventions `project_tests.py` already looks for."""
    target = _norm(path)
    base = target.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    if target.startswith(("tests/", "test/")) or "/tests/" in target or "/test/" in target:
        return True
    return (stem.startswith("test_") or stem.endswith("_test")
            or any(marker in base for marker in _TEST_MARKERS))


def _is_code(path: str) -> bool:
    base = _norm(path).rsplit("/", 1)[-1].lower()
    return any(base.endswith(ext) for ext in scope.DEFAULT_DOMAINS["code"])


def _expected_test_for(path: str) -> str:
    """`src/cart.py` -> `tests/test_cart.py`.

    The project's OWN convention, not a guess: `project_tests.related_test_files`
    looks for `test_<stem>` under `tests/` and `test/`, so a candidate that
    proposes any other spelling proposes a file the runner would not pick up.
    """
    target = _norm(path)
    base = target.rsplit("/", 1)[-1]
    stem, _, ext = base.rpartition(".")
    return f"tests/test_{stem or base}{('.' + ext) if stem and ext else ''}"


# -- the deterministic generators, one per source --------------------------


def _from_definition_of_done(data: DiscoveryInput) -> Sequence[ImprovementCandidate]:
    """A line of the contract that nothing in the ledger covers. §7's first source.

    `core`, because the definition of done was written before the work and is
    the errand itself; the line is its own evidence, which is what makes the
    `core` layer legal here at all.

    A line that names no resource is reported as unmet. The ledger can only
    show that a named path changed, so "nothing here can be shown done" is the
    honest answer, and the opposite default would let a contract be satisfied
    by a turn that touched something else entirely.
    """
    mutated = data.mutated()
    out: List[ImprovementCandidate] = []
    for line in data.contract.definition_of_done:
        named = scope.resources_named(line, data.workspace)
        if named and any(_touches(path, mutated) for path in named):
            continue
        out.append(_candidate(
            title=f"definition of done not met: {line}"[:300],
            layer="core", category="correctness", source="definition_of_done",
            relation="direct", resources=named, detail=line,
            evidence=[f"definition_of_done:{line}"],
            dedupe_key=f"definition_of_done:{line}",
        ))
    return tuple(out)


def _from_static_check(data: DiscoveryInput) -> Sequence[ImprovementCandidate]:
    """A real analyser finding on a line this turn added. §7, `warnings/errors`.

    `static_checks.run_for_turn` has ALREADY adjudicated `findings` against the
    turn's diff -- everything still in that list sits on a line this turn added,
    and `ignored` counts the ones that did not. Re-deriving the attribution here
    would be a second adjudication that could disagree with the first, and the
    disagreement would show up as a fix round nobody could explain.

    Nothing is produced from an inconclusive run: "the analyser could not be
    interpreted" is not a finding, and `static_checks` is careful to keep those
    two apart precisely so that a consumer does not merge them again.
    """
    report = data.static_checks or {}
    if not report.get("ran") or report.get("inconclusive"):
        return ()
    out: List[ImprovementCandidate] = []
    for finding in _rows(report, "findings"):
        if not isinstance(finding, Mapping):
            continue
        path = _norm(finding.get("path"))
        line = finding.get("line")
        code = str(finding.get("code") or "").strip()
        tool = str(finding.get("tool") or "static analysis").strip()
        message = str(finding.get("msg") or "").strip()
        out.append(_candidate(
            title=f"{tool} {code or 'finding'} in {path}:{line}"[:300],
            layer="professional", category="correctness", source="static_check",
            relation=data.relation_of(path), resources=[path] if path else (),
            detail=message,
            evidence=[f"static_check:{tool}:{path}:{line}:{code}"],
            verification=[f"re-run {tool} on {path}"] if path else (),
            dedupe_key=f"static_check:{path}:{line}:{code}",
        ))
    return tuple(out)


def _from_tests(data: DiscoveryInput) -> Sequence[ImprovementCandidate]:
    """Tests that did not run, tests that failed, and a change with no test.

    Three shapes of the same professional gap (§13's `regression test` first
    under Professional), and one thing deliberately not done: an EMPTY `tests`
    mapping produces nothing. `project_tests.run_for_turn` answers `None` when
    the feature is off, so an absent report and a report saying "did not run"
    are different facts -- the first belongs in `degraded_integrations`, and
    turning it into a candidate would charge every run with a gap it was
    configured not to have.

    An inconclusive run produces nothing either, for the reason
    `project_tests` gives when it sets the flag: it must cost no fix round.
    """
    report = data.tests or {}
    if not report:
        return ()
    if report.get("inconclusive"):
        return ()
    out: List[ImprovementCandidate] = []
    label = str(report.get("label") or report.get("kind") or "the project tests")
    if not report.get("ran"):
        out.append(_candidate(
            title=f"the tests did not run this turn ({label})",
            layer="professional", category="verification", source="tests",
            relation="direct",
            detail=str(report.get("summary") or ""),
            evidence=[f"tests:ran=false:{label}"],
            verification=[f"run {label}"],
            dedupe_key="tests:not_run",
        ))
        return tuple(out)

    if report.get("ok") is False:
        for failure in _rows(report, "failures")[:10]:
            text = str(failure)[:300]
            out.append(_candidate(
                title=f"failing test: {text}"[:300],
                layer="professional", category="verification", source="tests",
                relation="direct",
                resources=scope.resources_named(text, data.workspace),
                detail=str(report.get("summary") or ""),
                evidence=[f"tests:failure:{text}"],
                verification=[f"run {label}"],
                dedupe_key=f"tests:failure:{text}",
            ))

    related = [_norm(p) for p in _rows(report, "related_files")]
    for path in data.mutated():
        if not _is_code(path) or _is_test_path(path):
            continue
        if any(_is_test_path(t) and _shares_stem(t, path) for t in related):
            continue
        expected = _expected_test_for(path)
        out.append(_candidate(
            title=f"no test covers the change to {path}",
            layer="professional", category="coverage", source="tests",
            relation=data.relation_of(expected), resources=[expected],
            detail=f"{path} changed this turn and no related test file was found",
            evidence=[f"tests:no_related_test:{path}"],
            verification=[f"run {label}"],
            dedupe_key=f"tests:no_related_test:{path}",
        ))
    return tuple(out)


def _shares_stem(test_path: str, source_path: str) -> bool:
    """Whether a test file is, by name, the test of a source file."""
    tb = _norm(test_path).rsplit("/", 1)[-1]
    sb = _norm(source_path).rsplit("/", 1)[-1]
    ts = tb.rsplit(".", 1)[0] if "." in tb else tb
    ss = sb.rsplit(".", 1)[0] if "." in sb else sb
    if not ss:
        return False
    return ts in (f"test_{ss}", f"{ss}_test") or tb.startswith((f"{ss}.test.",
                                                                f"{ss}.spec."))


def _from_changeset(data: DiscoveryInput) -> Sequence[ImprovementCandidate]:
    """What was claimed and cannot be seen, and what changed and was not said.

    The two most valuable rows in this module. Everything else compares the
    code against a standard; these two compare the SENTENCE THE USER IS ABOUT
    TO READ against what the checkpoint saw, and that gap is the one failure a
    reader cannot detect for themselves.

    `unsupported_claims` is `verification`: the claim may well be true and
    nothing has shown it. `unclaimed_changes` is `consistency`: the change is
    real and the report and the disk disagree about it. Filing both under one
    category would lose the difference between "prove it" and "say it".

    `ChangeSet.unsupported_claims` already returns `()` when the change list is
    not exact, so nothing here has to re-check that: a truncated or
    mtime-derived list cannot contradict a claim, and this module inherits that
    restraint rather than restating it.
    """
    report = data.changeset or {}
    verdict = str(report.get("verdict") or "")
    out: List[ImprovementCandidate] = []
    for problem in _rows(report, "unsupported_claims"):
        if not isinstance(problem, Mapping):
            continue
        path = _norm(problem.get("path"))
        claimed = str(problem.get("claimed") or "")
        reason = str(problem.get("reason") or "")
        out.append(_candidate(
            title=f"the answer claims {path} was {claimed} and it cannot be seen",
            layer="professional", category="verification", source="changeset",
            relation=data.relation_of(path), resources=[path] if path else (),
            detail=reason,
            evidence=[f"changeset:{verdict or 'unknown'}:unsupported_claim:"
                      f"{path}:{claimed}:{reason}"],
            dedupe_key=f"changeset:unsupported_claim:{path}:{claimed}",
        ))
    for path in _rows(report, "unclaimed_changes"):
        target = _norm(path)
        if not target:
            continue
        out.append(_candidate(
            title=f"{target} changed and the answer does not mention it",
            layer="professional", category="consistency", source="changeset",
            relation=data.relation_of(target), resources=[target],
            evidence=[f"changeset:{verdict or 'unknown'}:unclaimed_change:{target}"],
            dedupe_key=f"changeset:unclaimed_change:{target}",
        ))
    return tuple(out)


def _from_proof(data: DiscoveryInput) -> Sequence[ImprovementCandidate]:
    """A verdict short of `proved`, carrying the heaviest reason it is short.

    One candidate, not one per uncertainty: the run does not have four separate
    problems, it has one verdict with a heaviest cause, and `prove` already
    orders the list so that `top_uncertainty` is that cause. Reusing that
    function rather than re-sorting the list here is the difference between one
    ranking and two that agree until somebody changes a penalty.
    """
    proof = data.proof or {}
    verdict = str(proof.get("verdict") or "")
    if not verdict or verdict not in VERDICTS or verdict == VERDICTS[0]:
        return ()
    top = top_uncertainty(proof)
    kind = str((top or {}).get("kind") or "")
    detail = str((top or {}).get("detail") or "")
    return (_candidate(
        title=f"the change is `{verdict}`: {kind or 'no evidence recorded'}"[:300],
        layer="professional", category="verification", source="proof",
        relation="direct", detail=detail,
        evidence=[f"proof:{verdict}:{kind or 'unnamed'}:{detail}"],
        dedupe_key=f"proof:{verdict}:{kind}",
    ),)


#: `(evidence_ref, kind, path, detail)` is built in ONE place so that
#: `_from_delta` and `resolved_by` cannot disagree about what a delta row's
#: reference string looks like. If they could, a candidate would never match
#: its own evidence and nothing would ever be retired.
_DELTA_KINDS: Dict[str, Tuple[str, str]] = {
    # kind -> (layer, category)
    "regression": ("core", "correctness"),
    "incidental": ("bonus", "consistency"),
    "unmet": ("core", "correctness"),
    "out_of_scope": ("professional", "consistency"),
}


def _delta_rows(delta: Mapping[str, Any]) -> Tuple[Tuple[str, str, str, str], ...]:
    """Every row of a delta this module knows how to turn into work."""
    out: List[Tuple[str, str, str, str]] = []
    for assertion in _rows(delta, "assertions"):
        if not isinstance(assertion, Mapping):
            continue
        kind = str(assertion.get("classification") or "")
        if kind not in ("regression", "incidental"):
            continue
        ident = str(assertion.get("id") or "")
        path = _norm(assertion.get("path"))
        detail = str(assertion.get("detail") or assertion.get("operation") or "")
        out.append((f"delta:assertion:{ident}:{kind}:{path}", kind, path, detail))
    for item in _rows(delta, "unmet"):
        text = str(item)
        out.append((f"delta:unmet:{text}", "unmet", "", text))
    for item in _rows(delta, "out_of_scope"):
        text = str(item)
        out.append((f"delta:out_of_scope:{text}", "out_of_scope", _norm(text), text))
    return tuple(out)


def _from_delta(data: DiscoveryInput) -> Sequence[ImprovementCandidate]:
    """What the Universal Delta saw that nobody asked for, and what it missed.

    A REGRESSION is `core`. §11: repairing it is not an extra that a run may
    trade away for budget -- the request is not met while the thing it broke is
    broken, and filing it as `bonus` would let a run report success with a
    regression in the receipt's optional half.

    An `incidental` change is `bonus`, and only when the envelope covers it: an
    unasked change outside the boundary is not an opportunity, it is drift, and
    `out_of_scope` is where the delta already files it.

    `unmet` is `core` for `Classified`'s own reason: a requested change that no
    observation satisfies is an errand nobody ran, and a delta full of
    successful assertions with one silently missing request looks exactly like
    a delta that did everything.
    """
    out: List[ImprovementCandidate] = []
    for ref, kind, path, detail in _delta_rows(data.delta or {}):
        if kind == "incidental" and path and not data.envelope.covers(path):
            continue
        layer, category = _DELTA_KINDS[kind]
        titles = {
            "regression": f"repair the regression at {path}",
            "incidental": f"account for the unasked change at {path}",
            "unmet": f"requested change nobody observed: {detail}",
            "out_of_scope": f"{path or detail} changed outside the intent's scope",
        }
        # An `out_of_scope` row names no resource of its own. The work it asks
        # for is to ACCOUNT for a change outside the intent, not to edit the
        # file -- the file is outside the envelope by definition, so a
        # candidate carrying it as a resource would be refused `out_of_scope`
        # by `admits` and the run would lose the obligation to mention it.
        # §30: "no ocultar extras dentro del resumen del core".
        carries_resource = kind != "out_of_scope" and bool(path)
        out.append(_candidate(
            title=titles[kind][:300],
            layer=layer, category=category, source="delta",
            relation=data.relation_of(path) if carries_resource else "direct",
            resources=[path] if carries_resource else (), detail=detail or path,
            evidence=[ref], dedupe_key=ref,
        ))
    return tuple(out)


def _from_state_mirror(data: DiscoveryInput) -> Sequence[ImprovementCandidate]:
    """Rows of `unverified_changes` and `blocked_work`. §7's read-model source.

    `bonus`, and the relation is the honest part: a row about THIS run's own
    entity is `downstream` -- verifying it is what makes the result usable --
    and every other row is `opportunistic`, which no mode reaches. That is not
    a wasted candidate: a rejection with a recorded reason is what §19 hands to
    the Opportunity Engine, and §30's "no confundir una oportunidad futura con
    trabajo actual" is the rule it keeps.
    """
    mine = {str(v) for v in (data.contract.run_id, data.contract.session_id,
                             data.contract.correlation_id) if v}
    out: List[ImprovementCandidate] = []
    for row in _rows(data.situations, "unverified_changes")[:20]:
        if not isinstance(row, Mapping):
            continue
        entity = str(row.get("entity_id") or "")
        name = str(row.get("field") or "")
        reason = str(row.get("reason") or "")
        out.append(_candidate(
            title=f"unverified: {name} of {entity} ({reason or 'unstated'})"[:300],
            layer="bonus", category="verification", source="state_mirror",
            relation="downstream" if entity in mine else "opportunistic",
            detail=str(row.get("refresh") or ""),
            evidence=[f"state_mirror:unverified_changes:{entity}:{name}:{reason}"],
            dedupe_key=f"state_mirror:unverified:{entity}:{name}",
        ))
    for row in _rows(data.situations, "blocked_work")[:20]:
        if not isinstance(row, Mapping):
            continue
        entity = str(row.get("entity_id") or "")
        reason = str(row.get("reason") or "")
        blockers = ", ".join(str(b) for b in (row.get("blocked_by") or ()))
        out.append(_candidate(
            title=f"blocked: {entity} ({reason or 'unstated'})"[:300],
            layer="bonus", category="analysis", source="state_mirror",
            relation="downstream" if entity in mine else "opportunistic",
            detail=blockers,
            evidence=[f"state_mirror:blocked_work:{entity}:{reason}:{blockers}"],
            dedupe_key=f"state_mirror:blocked:{entity}",
        ))
    return tuple(out)


def _from_similar_case(data: DiscoveryInput) -> Sequence[ImprovementCandidate]:
    """Nothing, on purpose, and this docstring is the deliverable.

    §7's `similar_case` source is "casos similares por índice estructural" --
    the same pattern found somewhere else by an INDEX over the tree. This phase
    has no such index, and none of the inputs contains one: `static_checks`
    reports only on files the turn changed (`run_for_turn` adjudicates against
    the diff and drops the rest as a COUNT, not a list), the delta compares two
    revisions of the same work, and the ledger knows only what was touched. So
    there is no evidence here from which a second occurrence of a pattern could
    be observed.

    The alternative would be a name heuristic -- "this file is called
    `validators.py`, so are those three, they probably share the bug" -- and
    that is precisely what §13 and §30 forbid: a relation asserted from a name
    is `required` claimed without a demonstrable relation. An empty generator
    that says why is worth more than a plausible one nobody can check, and
    `relation_of` still classifies a resource as `similar_case` when a caller
    supplies the pattern from an index it does have.
    """
    return ()


#: Where a domain playbook lives, and the one function this module will call on
#: it. Declared here so the agent writing `playbooks/code.py` has a name to
#: implement rather than a shape to guess:
#:
#:     def candidates(data: DiscoveryInput) -> Sequence[ImprovementCandidate]
#:
#: It must be deterministic for the same reason everything else here is, and it
#: must set `source="playbook"` on what it returns so `dedupe` can rank it.
PLAYBOOK_MODULE = "src.completion_engine.playbooks"
PLAYBOOK_HOOK = "candidates"


def _from_playbook(data: DiscoveryInput) -> Sequence[ImprovementCandidate]:
    """The domain's own checklist, or the contract's expectations in its place.

    A missing playbook module is not an error -- most domains have none yet --
    but a playbook module WITHOUT the hook is, and it is raised rather than
    swallowed: a checklist that silently contributes nothing is worse than no
    checklist, because the closeout would report a professional layer that was
    never actually checked.

    With no playbook, `professional_expectations` from the contract stands in.
    It is the field that holds the same kind of thing -- what a professional
    would not ship without -- and it is deliberately NOT
    `definition_of_done`, which already has a generator of its own; reading it
    twice would produce two candidates for one line under two different keys.
    """
    module = None
    name = str(data.contract.playbook or "").strip()
    if name:
        try:
            module = importlib.import_module(f"{PLAYBOOK_MODULE}.{name}")
        except ImportError:
            module = None
    if module is not None:
        if not hasattr(module, PLAYBOOK_HOOK):
            raise CompletionError(
                f"playbook.{name}",
                f"has no `{PLAYBOOK_HOOK}` function; a playbook that cannot be "
                f"called contributes nothing while the closeout reports a "
                f"professional layer as checked",
                got=name)
        produced = getattr(module, PLAYBOOK_HOOK)(data)
        for item in produced or ():
            if not isinstance(item, ImprovementCandidate):
                raise CompletionError(
                    f"playbook.{name}",
                    "returned something that is not an ImprovementCandidate",
                    got=item)
        return tuple(produced or ())

    mutated = data.mutated()
    out: List[ImprovementCandidate] = []
    for line in data.contract.professional_expectations:
        named = scope.resources_named(line, data.workspace)
        if named and any(_touches(path, mutated) for path in named):
            continue
        out.append(_candidate(
            title=f"professional expectation not met: {line}"[:300],
            layer="professional", category="correctness", source="playbook",
            relation="direct", resources=named, detail=line,
            evidence=[f"playbook:professional_expectation:{line}"],
            dedupe_key=f"playbook:professional_expectation:{line}",
        ))
    return tuple(out)


# -- the registry ----------------------------------------------------------

#: Source -> the generator that reads it. The five sources of
#: `DISCOVERY_SOURCES` with no entry here -- `opportunity`, `reviewer`,
#: `council`, `model`, `user` -- are §7's assisted and external ones, and their
#: absence is the module docstring's first rule made visible: asking for one by
#: name returns nothing rather than failing, because a caller enumerating every
#: source it might one day have should not break the day one of them is added.
_BY_SOURCE: Dict[str, Callable[[DiscoveryInput], Sequence[ImprovementCandidate]]] = {
    "definition_of_done": _from_definition_of_done,
    "static_check": _from_static_check,
    "tests": _from_tests,
    "delta": _from_delta,
    "proof": _from_proof,
    "state_mirror": _from_state_mirror,
    "changeset": _from_changeset,
    "playbook": _from_playbook,
    "similar_case": _from_similar_case,
}

#: In `DISCOVERY_SOURCES` order, which the contract states is the TRUST order.
GENERATORS: Tuple[Callable[[DiscoveryInput], Sequence[ImprovementCandidate]], ...] = \
    tuple(_BY_SOURCE[name] for name in DISCOVERY_SOURCES if name in _BY_SOURCE)


def _source_rank(source: Any) -> int:
    """Position in `DISCOVERY_SOURCES`; an unknown source ranks LAST.

    Last, not first: a source this build does not recognise is the least
    trusted thing in the round, which is the direction that cannot let an
    unrecognised word win a dedupe against a static analyser.
    """
    name = str(source or "")
    return DISCOVERY_SOURCES.index(name) if name in DISCOVERY_SOURCES \
        else len(DISCOVERY_SOURCES)


def discover(data: DiscoveryInput, *,
             sources: Sequence[str] = ()) -> Tuple[ImprovementCandidate, ...]:
    """Every candidate the deterministic sources can see right now, deduplicated.

    The generators are walked in `DISCOVERY_SOURCES` order and not in the
    caller's, so two callers asking for the same sources in different orders
    get the same list back. That matters because `dedupe` resolves a collision
    by source rank and a stable input order is what makes the whole answer
    reproducible -- which is the property this module is for.

    An unknown source name is an error, never a silent no-op: a caller that
    misspells `static_checks` for `static_check` would otherwise get an empty
    frontier and a clean-looking run.
    """
    wanted = tuple(dict.fromkeys(str(s) for s in sources)) or DISCOVERY_SOURCES
    unknown = [s for s in wanted if s not in DISCOVERY_SOURCES]
    if unknown:
        raise CompletionError(
            "sources", f"names discovery sources that do not exist: {unknown}; "
                       f"known: {list(DISCOVERY_SOURCES)}", got=list(sources))
    found: List[ImprovementCandidate] = []
    for name in DISCOVERY_SOURCES:
        if name not in wanted:
            continue
        generator = _BY_SOURCE.get(name)
        if generator is None:
            continue
        found.extend(generator(data))
    return dedupe(found)


def dedupe(candidates: Sequence[ImprovementCandidate]) -> Tuple[ImprovementCandidate, ...]:
    """One candidate per `key`; the most deterministic source wins the collision.

    Two generators that found the same missing test are one piece of work, and
    `ImprovementCandidate.key` is what says so structurally rather than by
    comparing two sentences. When they collide:

    * the survivor is the one whose `source` sits earliest in
      `DISCOVERY_SOURCES`, which the contract states is the TRUST order. Keeping
      whichever arrived first would make the answer depend on the order the
      generators happen to run in -- a fact about this file, not about the code
      being improved -- and that dependency is exactly what a reproducible
      frontier cannot have;
    * the evidence is the UNION, winner's first. A collision means two
      independent things pointed at the same work, and dropping one of them
      would throw away the stronger half of the argument for doing it.

    Nothing else is merged. `resources` in particular are not unioned: with an
    explicit `dedupe_key` two candidates can carry different resources, and
    merging those would silently widen the footprint of the survivor past what
    either generator observed.
    """
    groups: Dict[str, List[ImprovementCandidate]] = {}
    order: List[str] = []
    for candidate in candidates or ():
        key = candidate.key
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(candidate)

    out: List[ImprovementCandidate] = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            out.append(group[0])
            continue
        winner = min(group, key=lambda c: _source_rank(c.source))
        evidence: List[str] = list(winner.evidence_refs)
        for other in group:
            if other is winner:
                continue
            for ref in other.evidence_refs:
                if ref not in evidence:
                    evidence.append(ref)
        payload = winner.to_dict()
        payload["evidence_refs"] = evidence[:256]
        out.append(ImprovementCandidate.parse(payload, "candidate"))
    return tuple(out)


#: Sources whose candidate is "this does not exist yet", so that a write at the
#: path it named IS the doing of it. Everything else is deliberately absent:
#: retiring a static-analysis finding because somebody wrote to the file would
#: drop a real defect on the strength of a timestamp, and retiring an unclaimed
#: change because the file changed again is a tautology.
_MUTATION_SETTLES: Tuple[str, ...] = ("definition_of_done", "tests", "playbook")


def resolved_by(candidates: Sequence[ImprovementCandidate], *,
                ledger_summary: Mapping[str, Any],
                delta: Optional[Mapping[str, Any]] = None) -> Tuple[str, ...]:
    """The ids of candidates whose problem is gone. §9's "resueltas indirectamente".

    Two rules, and both require that the EVIDENCE be gone rather than that the
    work look plausible:

    * a candidate from a source in `_MUTATION_SETTLES` whose every named
      resource now appears in the turn's mutations, and which the delta does
      not report as a regression at one of them. Those candidates name a thing
      that had to come to exist; once it exists the candidate is finished, and a
      regression at the same path is the one signal that says otherwise;
    * a `delta` candidate none of whose evidence references the supplied delta
      would still produce. That is literally "the finding disappeared", and it
      works because `_delta_rows` builds those reference strings in one place,
      so a candidate cannot fail to recognise its own evidence.

    Retiring is not the same as succeeding. This returns ids for the frontier
    to mark `rejected` with reason `resolved`; §1.8's rule is that a rejection
    is RECORDED, because one nobody wrote down comes back every round.
    """
    mutated = tuple(_norm(p) for p in _rows(ledger_summary or {}, "mutations"))
    regressed: Set[str] = set()
    for _ref, kind, path, _detail in _delta_rows(delta or {}):
        if kind == "regression" and path:
            regressed.add(path)
    live = {ref for ref, _k, _p, _d in _delta_rows(delta or {})}

    out: List[str] = []
    for candidate in candidates or ():
        if candidate.source in _MUTATION_SETTLES and candidate.resources:
            paths = [_norm(r) for r in candidate.resources]
            if all(_touches(p, mutated) for p in paths) \
                    and not any(p in regressed for p in paths):
                out.append(candidate.id)
                continue
        if candidate.source == "delta" and delta is not None:
            refs = [r for r in candidate.evidence_refs if r.startswith("delta:")]
            if refs and not any(r in live for r in refs):
                out.append(candidate.id)
    return tuple(dict.fromkeys(out))
