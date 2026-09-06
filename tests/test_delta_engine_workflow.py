"""tests/test_delta_engine_workflow.py -- §28's "Workflow/skill" rows, one each.

Every test fixes a RULE and not a snapshot. The wording of a limitation will
change, `LOCAL_PROVIDERS` will grow and a future contract may carry fields these
adapters have to guess about today; none of that may turn a withdrawn approval
into an `info`, a renamed node title into an effect, or an unchecked property
into `preserved`.

The guarantees, one test each (§28's "Workflow/skill" list first):

* a new permission is blocking -- on a skill (`network` false -> true) and on a
  workflow (a `config.permissions` grant that was not there);
* a provider that was local and is not any more is blocking;
* a removed postcondition: `WorkflowNode` declares none, so the test proves the
  answer is `unknown` WITH the gap named, and never `preserved`;
* `max_attempts > 1` on an effectful node that stopped declaring
  `config.idempotent` is blocking -- the case `WorkflowNode.parse` refuses, read
  anyway because a definition that has degraded that way is one somebody is
  about to fix;
* a removed gate (a `human_approval` node) and a withdrawn approval trigger are
  both blocking, and the second is the one that looks like less;
* a cosmetic change -- a node title, a reordered config key -- is `modified` at
  severity `info`, points at no invariant, and leaves every security invariant
  `preserved` rather than violated;
* `filesystem` widening from `workspace` is blocking, and narrowing to `none`
  is not;
* `registry.discover()` finds both adapters with nobody adding them to a list;
* every invariant id these modules point findings at is an id `invariants.py`
  actually declares.
"""

from __future__ import annotations

import copy

import pytest

from src.delta_engine import invariants as invariants_mod
from src.delta_engine import registry, sources
from src.delta_engine.adapters import skill as skill_mod
from src.delta_engine.adapters import workflow as workflow_mod
from src.delta_engine.adapters.base import Scope
from src.delta_engine.contracts import (
    Budget,
    DeltaAssertion,
    IntentContract,
    RevisionRef,
    confidence_rank,
)

OWNER = "alice"
FROZEN = "2026-09-06T12:00:00Z"

EFFECTS = {"id": workflow_mod.EFFECTS_INVARIANT, "class": "security",
           "source": "security"}
PERMISSIONS = {"id": workflow_mod.PERMISSIONS_INVARIANT, "class": "permissions",
               "source": "security"}
NETWORK = {"id": workflow_mod.NETWORK_INVARIANT, "class": "security",
           "source": "security"}
SECRETS = {"id": workflow_mod.SECRETS_INVARIANT, "class": "security",
           "source": "security"}
SKILL_EFFECTS = {"id": skill_mod.EFFECTS_INVARIANT, "class": "security",
                 "source": "security"}

#: A workflow that gathers, waits for a person, then publishes. Every §14
#: property this adapter can check has an instance in it: an effectful node, a
#: gate, a retry with an idempotency declaration, a local provider and a
#: `config.permissions` map.
BASE_WORKFLOW = {
    "id": "nightly", "version": "1.0.0", "title": "Nightly digest",
    "description": "Gathers, asks, publishes.",
    "nodes": [
        {"id": "gather", "type": "skill", "title": "Gather",
         "config": {"idempotent": True, "provider": "ollama"},
         "max_attempts": 2},
        {"id": "approve", "type": "human_approval", "title": "Ask a person",
         "needs": ["gather"]},
        {"id": "publish", "type": "deliver", "title": "Publish",
         "needs": ["approve"], "max_attempts": 3,
         "config": {"idempotent": True, "permissions": {"network": False},
                    "postconditions": ["digest is on the site"]}},
    ],
}

#: A skill that renders locally and asks for a person before publishing.
BASE_MANIFEST = {
    "id": "render.video", "version": "1.2.0", "title": "Render video",
    "description": "Renders a clip.",
    "inputs": {"prompt": "text"}, "outputs": {"clip": "artifact:video"},
    "permissions": {"network": False, "filesystem": "workspace",
                    "backends": ["comfyui"]},
    "approval": {"required_when": ["publish", "destructive"]},
    "memory": {"read_scopes": ["project"], "write_scopes": ["run"]},
}


@pytest.fixture()
def scope() -> Scope:
    return Scope(owner=OWNER, budget=Budget())


@pytest.fixture()
def workflow_adapter() -> workflow_mod.WorkflowAdapter:
    return workflow_mod.WorkflowAdapter()


@pytest.fixture()
def skill_adapter() -> skill_mod.SkillAdapter:
    return skill_mod.SkillAdapter()


def edited(base: dict, mutate) -> dict:
    """A deep copy of `base` with `mutate` applied. Never edits the fixture.

    A test that mutated the module-level definition would pass alone and change
    what every later test compares against, which is the kind of failure that
    gets blamed on the code under test.
    """
    body = copy.deepcopy(base)
    mutate(body)
    return body


def node(body: dict, node_id: str) -> dict:
    return next(item for item in body["nodes"] if item["id"] == node_id)


def intent_with(domain: str, *invariants) -> IntentContract:
    return IntentContract.parse({
        "id": f"intent_{domain}", "owner": OWNER, "domain": domain,
        "frozen_at": FROZEN, "invariants": list(invariants),
    })


def results_by_id(results) -> dict:
    return {result.invariant_id: result for result in results}


def findings_by_path(extraction) -> dict:
    return {finding.path: finding for finding in extraction.findings}


def compare_workflows(adapter, scope, source_body: dict, target_body: dict):
    """`(extraction, source snapshot, target snapshot)` for two definitions."""
    source = adapter.snapshot(sources.stash(source_body), scope=scope)
    target = adapter.snapshot(sources.stash(target_body), scope=scope)
    return adapter.compare(source, target, scope=scope), source, target


def compare_manifests(adapter, scope, source_body: dict, target_body: dict):
    """`(extraction, source snapshot, target snapshot)` for two manifests."""
    source = adapter.snapshot(sources.stash(source_body), scope=scope)
    target = adapter.snapshot(sources.stash(target_body), scope=scope)
    return adapter.compare(source, target, scope=scope), source, target


# -- §28: a new permission is blocking -------------------------------------


def test_a_skill_that_turns_the_network_on_is_blocking(skill_adapter, scope):
    """`permissions.network` false -> true, on the row §14 puts first.

    The severity is not asserted as a literal: it comes from the invariant's
    CLASS, and `Invariant.parse` refuses a `security` or `permissions` invariant
    filed below `blocking`. Asserting the class and the status is asserting the
    rule; asserting the word alone would still pass if the floor were removed.
    """
    target = edited(BASE_MANIFEST,
                    lambda body: body["permissions"].update({"network": True}))
    extraction, source_snap, target_snap = compare_manifests(
        skill_adapter, scope, BASE_MANIFEST, target)

    finding = findings_by_path(extraction)["permission:network"]
    assert finding.operation == "modified"
    assert skill_mod.NETWORK_INVARIANT in finding.invariant_refs
    assert skill_mod.PERMISSIONS_INVARIANT in finding.invariant_refs

    intent = intent_with("skill", SKILL_EFFECTS, PERMISSIONS, NETWORK)
    results = results_by_id(skill_adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))
    for invariant_id in (skill_mod.NETWORK_INVARIANT, skill_mod.PERMISSIONS_INVARIANT,
                         skill_mod.EFFECTS_INVARIANT):
        result = results[invariant_id]
        assert result.status == "violated"
        assert result.severity == "blocking"
        assert result.blocking is True


def test_a_workflow_node_that_grants_a_permission_it_did_not_have_is_blocking(
        workflow_adapter, scope):
    """§14's "permiso ampliado", over the only place a workflow can express one.

    `WorkflowNode` has no permissions field, so the grant lives in
    `config.permissions` and the adapter says so in the finding's limitations.
    The test asserts BOTH: that the widening is blocking, and that the row
    admits what it rests on -- a blocking claim with no stated basis is the one
    a reviewer cannot check.
    """
    target = edited(BASE_WORKFLOW, lambda body: node(body, "publish")["config"]
                    ["permissions"].update({"network": True}))
    extraction, source_snap, target_snap = compare_workflows(
        workflow_adapter, scope, BASE_WORKFLOW, target)

    finding = findings_by_path(extraction)["param:publish#permissions.network"]
    assert finding.operation == "modified"
    assert finding.before == "false" and finding.after == "true"
    assert finding.invariant_refs == (workflow_mod.PERMISSIONS_INVARIANT,)
    assert any("no permissions field" in item for item in finding.limitations)

    intent = intent_with("workflow", PERMISSIONS)
    result = results_by_id(workflow_adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))[
            workflow_mod.PERMISSIONS_INVARIANT]
    assert result.status == "violated" and result.severity == "blocking"


def test_a_narrower_permission_is_not_reported_as_a_widening(
        workflow_adapter, scope):
    """The other direction, because an adapter that cries wolf protects nothing.

    A grant that goes from true to false is the workflow asking for less. If it
    were filed as blocking, a reader would learn to skip these rows -- and then
    the row in the test above is not read either.
    """
    source = edited(BASE_WORKFLOW, lambda body: node(body, "publish")["config"]
                    ["permissions"].update({"network": True}))
    extraction, _source_snap, _target_snap = compare_workflows(
        workflow_adapter, scope, source, BASE_WORKFLOW)
    finding = findings_by_path(extraction)["param:publish#permissions.network"]
    assert finding.operation == "modified"
    assert finding.invariant_refs == ()


# -- §28: a local provider that becomes external ---------------------------


def test_a_provider_that_was_local_and_is_not_any_more_is_blocking(
        workflow_adapter, scope):
    """§14's "proveedor externo donde antes era local", with its ceiling stated.

    Two assertions, and the second matters as much as the first: the finding is
    blocking, AND its confidence is not `exact`. `WorkflowNode` has no provider
    field and "external" is decided by a NAME LIST in the adapter, so a row that
    claimed identity here would be a reading of a string dressed up as a fact
    about where the work runs.
    """
    target = edited(BASE_WORKFLOW,
                    lambda body: node(body, "gather")["config"].update(
                        {"provider": "api.openai.com"}))
    extraction, source_snap, target_snap = compare_workflows(
        workflow_adapter, scope, BASE_WORKFLOW, target)

    finding = findings_by_path(extraction)["node:gather"]
    assert finding.operation == "modified"
    assert finding.invariant_refs == (workflow_mod.NETWORK_INVARIANT,)
    assert confidence_rank(finding.confidence) >= confidence_rank("medium")
    assert any("provider field" in item for item in finding.limitations)

    intent = intent_with("workflow", NETWORK)
    result = results_by_id(workflow_adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))[
            workflow_mod.NETWORK_INVARIANT]
    assert result.status == "violated" and result.severity == "blocking"
    assert confidence_rank(result.confidence) >= confidence_rank("medium")


def test_a_provider_the_module_does_not_recognise_makes_no_claim(
        workflow_adapter, scope):
    """The premise of the check is "it WAS local", and without it there is none.

    A source provider outside `LOCAL_PROVIDERS` is not evidence that the node
    ran on this machine, so a move away from it says nothing. Reporting one
    anyway would turn a name this module has not heard of into a security
    finding, which is how a list of provider names becomes a list of false
    alarms.
    """
    source = edited(BASE_WORKFLOW,
                    lambda body: node(body, "gather")["config"].update(
                        {"provider": "some-gateway"}))
    target = edited(source, lambda body: node(body, "gather")["config"].update(
        {"provider": "api.openai.com"}))
    extraction, _source_snap, _target_snap = compare_workflows(
        workflow_adapter, scope, source, target)
    finding = findings_by_path(extraction)["node:gather"]
    assert finding.operation == "modified"
    assert finding.invariant_refs == ()


# -- §28: a removed postcondition ------------------------------------------


def test_a_removed_postcondition_is_unknown_with_the_gap_named_never_preserved(
        workflow_adapter, scope):
    """The contract has no postcondition, so the honest answer is `unknown`.

    §14 calls a removed postcondition blocking and `WorkflowNode` declares no
    such field, so this adapter cannot make that call. What it must NOT do is
    answer `preserved`, which would be "we did not look" rendered as "it is
    fine" -- rule 1 of `contracts.py` and the reason the whole subsystem exists.

    The `config.postconditions` a caller supplied is still visible as an
    ordinary parameter, and the test asserts that too: the gap is in the
    MEANING, not in the reading.
    """
    target = edited(BASE_WORKFLOW,
                    lambda body: node(body, "publish")["config"].pop("postconditions"))
    extraction, source_snap, target_snap = compare_workflows(
        workflow_adapter, scope, BASE_WORKFLOW, target)

    removed = findings_by_path(extraction)["param:publish#postconditions.0"]
    assert removed.operation == "missing"

    intent = intent_with("workflow", {"id": "workflow.postconditions_kept",
                                      "class": "behavior"})
    result = results_by_id(workflow_adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))["workflow.postconditions_kept"]
    assert result.status == "unknown"
    assert result.status != "preserved"
    assert result.confidence == "unknown"
    assert any("postcondition" in item for item in result.limitations)


# -- §28: a retry that stopped being idempotent ----------------------------


def test_a_retried_effect_that_stops_declaring_idempotence_is_blocking(
        workflow_adapter, scope):
    """§14's "idempotencia degradada", in the words the contract already uses.

    `publish` is a `deliver` node retried three times. Dropping
    `config.idempotent` puts it in exactly the state `WorkflowNode.parse`
    refuses -- "the second attempt is the one that sends the email again" -- and
    the point of this test is that the adapter still SEES it. A definition the
    contract rejects is one somebody is about to fix, and an adapter that
    answered `readable=False` about it would be silent about the single case
    §14 names most concretely.
    """
    target = edited(BASE_WORKFLOW,
                    lambda body: node(body, "publish")["config"].pop("idempotent"))
    extraction, source_snap, target_snap = compare_workflows(
        workflow_adapter, scope, BASE_WORKFLOW, target)

    assert target_snap.readable is True
    assert any("refuse" in note for note in target_snap.notes)

    finding = findings_by_path(extraction)["retry:publish"]
    assert finding.operation == "modified"
    assert finding.invariant_refs == (workflow_mod.EFFECTS_INVARIANT,)
    assert "idempotent" in finding.detail

    intent = intent_with("workflow", EFFECTS)
    result = results_by_id(workflow_adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))[
            workflow_mod.EFFECTS_INVARIANT]
    assert result.status == "violated"
    assert result.severity == "blocking" and result.blocking is True


def test_a_new_effectful_node_is_blocking(workflow_adapter, scope):
    """§14's first row: an effect the source did not declare.

    Separate from the retry test because the two are different failures with one
    invariant: this one adds a place the workflow reaches, the other makes an
    existing reach unsafe to repeat, and a delta that could only report one of
    them would miss half of what `EFFECTFUL_TYPES` is for.
    """
    def _add(body):
        body["nodes"].append({"id": "archive", "type": "artifact_store",
                              "title": "Archive", "needs": ["publish"]})

    target = edited(BASE_WORKFLOW, _add)
    extraction, source_snap, target_snap = compare_workflows(
        workflow_adapter, scope, BASE_WORKFLOW, target)

    finding = findings_by_path(extraction)["effect:archive"]
    assert finding.operation == "added"
    assert finding.invariant_refs == (workflow_mod.EFFECTS_INVARIANT,)

    intent = intent_with("workflow", EFFECTS)
    result = results_by_id(workflow_adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))[
            workflow_mod.EFFECTS_INVARIANT]
    assert result.status == "violated" and result.severity == "blocking"


# -- §28: a removed gate, and a withdrawn approval -------------------------


def test_a_removed_human_approval_node_is_blocking(workflow_adapter, scope):
    """A gate that vanishes lets the workflow act unattended. §14's "gate removido".

    Filed against the PERMISSIONS invariant and not the effects one, because
    what changed is who has to say yes rather than where the workflow reaches --
    and the two lead to different conversations.
    """
    def _drop(body):
        body["nodes"] = [item for item in body["nodes"] if item["id"] != "approve"]
        node(body, "publish")["needs"] = ["gather"]

    target = edited(BASE_WORKFLOW, _drop)
    extraction, source_snap, target_snap = compare_workflows(
        workflow_adapter, scope, BASE_WORKFLOW, target)

    finding = findings_by_path(extraction)["node:approve"]
    assert finding.operation == "missing"
    assert finding.invariant_refs == (workflow_mod.PERMISSIONS_INVARIANT,)

    intent = intent_with("workflow", PERMISSIONS)
    result = results_by_id(workflow_adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))[
            workflow_mod.PERMISSIONS_INVARIANT]
    assert result.status == "violated"
    assert result.severity == "blocking" and result.blocking is True


def test_withdrawing_a_required_approval_is_widening_a_permission(
        skill_adapter, scope):
    """The case that looks like less and is more. §14, and the one to prove.

    `destructive` is not implied by any permission, so removing it from
    `approval.required_when` takes it out of `effective_approvals()` entirely:
    the card stops appearing and the skill may now overwrite the user's files
    with nobody asked. One fewer line in a manifest, one more thing it may do.
    """
    target = edited(BASE_MANIFEST,
                    lambda body: body["approval"].update({"required_when": ["publish"]}))
    extraction, source_snap, target_snap = compare_manifests(
        skill_adapter, scope, BASE_MANIFEST, target)

    finding = findings_by_path(extraction)["approval:destructive"]
    assert finding.operation == "missing"
    assert finding.invariant_refs == (skill_mod.PERMISSIONS_INVARIANT,)

    intent = intent_with("skill", PERMISSIONS)
    result = results_by_id(skill_adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))[
            skill_mod.PERMISSIONS_INVARIANT]
    assert result.status == "violated"
    assert result.severity == "blocking" and result.blocking is True


def test_an_approval_that_is_still_implied_is_not_blocking(skill_adapter, scope):
    """The symmetric half: `implied_approvals()` puts the card back.

    A manifest that asks for the network and drops `network` from its declared
    triggers still gets the approval card, because `SkillManifest` derives it
    from the permission. Reporting that as blocking would file a manifest that
    changed nothing operational as a security regression -- and an adapter that
    does that once is switched off, after which it protects nothing.
    """
    def _network_on(body):
        body["permissions"].update({"network": True})
        body["approval"]["required_when"] = ["publish", "destructive", "network"]

    source = edited(BASE_MANIFEST, _network_on)
    target = edited(source,
                    lambda body: body["approval"].update(
                        {"required_when": ["publish", "destructive"]}))
    extraction, source_snap, target_snap = compare_manifests(
        skill_adapter, scope, source, target)

    finding = findings_by_path(extraction)["approval:network"]
    assert finding.operation == "modified"
    assert finding.before == "declared" and finding.after == "implied"
    assert finding.invariant_refs == ()

    intent = intent_with("skill", PERMISSIONS)
    result = results_by_id(skill_adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))[
            skill_mod.PERMISSIONS_INVARIANT]
    assert result.status == "preserved"
    assert any("implied_approvals" in item for item in result.limitations)


# -- §28: a cosmetic change is not an effect -------------------------------


def test_a_cosmetic_change_is_info_and_touches_no_security_invariant(
        workflow_adapter, scope):
    """§28's symmetric row, and the one that keeps this adapter switched on.

    A renamed node title, a config written in a different key order, and an
    edited description. The title is the only one that produces a row at all --
    the node hash covers it -- and that row must point at no invariant and come
    out `info` once the classifier has looked at it. The reordered config
    produces nothing, because the hash is taken over a canonical rendering with
    sorted keys.

    `DeltaAssertion.parse` is used rather than a literal `severity` field on the
    finding, because an adapter has no severity to give: the assertion is where
    the two axes meet, and `info` is what an unclassified `modified` becomes
    there. Asserting it through the contract is asserting the rule.
    """
    def _cosmetic(body):
        body["description"] = "Gathers, asks a person, publishes."
        gather = node(body, "gather")
        gather["title"] = "Gather everything"
        gather["config"] = {"provider": "ollama", "idempotent": True}

    target = edited(BASE_WORKFLOW, _cosmetic)
    extraction, source_snap, target_snap = compare_workflows(
        workflow_adapter, scope, BASE_WORKFLOW, target)

    changed = [f for f in extraction.findings if f.operation != "unchanged"]
    assert [f.path for f in changed] == ["node:gather"]
    finding = changed[0]
    assert finding.operation == "modified"
    assert finding.invariant_refs == ()
    assert DeltaAssertion.parse(finding.to_dict(), "assertion").severity == "info"

    intent = intent_with("workflow", EFFECTS, PERMISSIONS, NETWORK, SECRETS)
    results = results_by_id(workflow_adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))
    assert results[workflow_mod.EFFECTS_INVARIANT].status == "preserved"
    assert results[workflow_mod.PERMISSIONS_INVARIANT].status == "preserved"
    for result in results.values():
        assert result.status != "violated"


def test_a_cosmetic_change_to_a_manifest_touches_no_security_invariant(
        skill_adapter, scope):
    """The same rule on the skill side: a retitled skill is not a wider skill."""
    target = edited(BASE_MANIFEST, lambda body: body.update(
        {"title": "Render a video", "description": "Renders one clip."}))
    extraction, source_snap, target_snap = compare_manifests(
        skill_adapter, scope, BASE_MANIFEST, target)

    changed = {f.path for f in extraction.findings if f.operation != "unchanged"}
    assert changed == {"meta:title", "meta:description"}
    assert all(f.invariant_refs == () for f in extraction.findings)

    intent = intent_with("skill", SKILL_EFFECTS, PERMISSIONS, NETWORK)
    results = results_by_id(skill_adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))
    assert all(result.status == "preserved" for result in results.values())


# -- §28: filesystem widening ----------------------------------------------


def test_filesystem_widening_from_workspace_is_blocking(skill_adapter, scope):
    """`workspace -> project` reaches everything the project owns. §14."""
    target = edited(BASE_MANIFEST,
                    lambda body: body["permissions"].update({"filesystem": "project"}))
    extraction, source_snap, target_snap = compare_manifests(
        skill_adapter, scope, BASE_MANIFEST, target)

    finding = findings_by_path(extraction)["permission:filesystem"]
    assert finding.operation == "modified"
    assert skill_mod.PERMISSIONS_INVARIANT in finding.invariant_refs
    assert skill_mod.EFFECTS_INVARIANT in finding.invariant_refs

    intent = intent_with("skill", PERMISSIONS, SKILL_EFFECTS)
    results = results_by_id(skill_adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))
    assert results[skill_mod.PERMISSIONS_INVARIANT].status == "violated"
    assert results[skill_mod.PERMISSIONS_INVARIANT].severity == "blocking"
    assert results[skill_mod.EFFECTS_INVARIANT].status == "violated"


def test_filesystem_narrowing_to_none_is_not_a_widening(skill_adapter, scope):
    """§14 words this row "workspace -> anything else"; `none` is narrower.

    The order the adapter uses is `none < workspace < project`, which is the
    departure its docstring names. A skill that gives up filesystem access is
    asking for less, and filing that as blocking would be the same wolf-crying
    the cosmetic test guards against from the other side.
    """
    target = edited(BASE_MANIFEST,
                    lambda body: body["permissions"].update({"filesystem": "none"}))
    extraction, source_snap, target_snap = compare_manifests(
        skill_adapter, scope, BASE_MANIFEST, target)

    finding = findings_by_path(extraction)["permission:filesystem"]
    assert finding.operation == "modified"
    assert finding.invariant_refs == ()

    intent = intent_with("skill", PERMISSIONS)
    result = results_by_id(skill_adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))[
            skill_mod.PERMISSIONS_INVARIANT]
    assert result.status == "preserved"


def test_a_new_secret_name_is_blocking(skill_adapter, scope):
    """§14's "secreto nuevo", over the field that holds names and never values."""
    def _ask(body):
        body["permissions"]["secrets"] = ["OPENAI_API_KEY"]

    target = edited(BASE_MANIFEST, _ask)
    extraction, source_snap, target_snap = compare_manifests(
        skill_adapter, scope, BASE_MANIFEST, target)

    finding = findings_by_path(extraction)["permission:secrets"]
    assert skill_mod.SECRETS_INVARIANT in finding.invariant_refs

    intent = intent_with("skill", SECRETS)
    result = results_by_id(skill_adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))[
            skill_mod.SECRETS_INVARIANT]
    assert result.status == "violated" and result.severity == "blocking"


# -- honesty about what could not be read ----------------------------------


def test_a_workflow_kind_revision_is_unreadable_with_the_resolvers_reason(
        workflow_adapter, scope):
    """The documented hole, asserted so it cannot close by accident.

    `sources.RESOLVERS` has no reader for the workflow store, and `sources.py`
    is not this adapter's to change. The refusal has to travel as a
    `readable=False` snapshot carrying that module's own reason -- which is what
    makes the delta `inconclusive` instead of an exception in the middle of a
    comparison, and what tells the caller WHICH end failed.
    """
    revision = RevisionRef.parse({
        "kind": "workflow", "ref": "workflow:nightly@1.0.0", "hash": "a" * 64})
    snapshot = workflow_adapter.snapshot(revision, scope=scope)
    assert snapshot.readable is False
    assert snapshot.elements == ()
    assert any("workflow store" in note for note in snapshot.notes)


def test_a_payload_that_is_not_a_definition_is_refused_rather_than_emptied(
        workflow_adapter, scope):
    """An empty snapshot would report every node as deleted. Refuse instead."""
    snapshot = workflow_adapter.snapshot(
        sources.stash({"title": "not a workflow"}), scope=scope)
    assert snapshot.readable is False
    assert any("workflow node" in note for note in snapshot.notes)


def test_an_unreadable_end_produces_no_findings_and_says_which_one(
        workflow_adapter, scope):
    """"We could not read it" and "every node was deleted" are opposite facts."""
    source = workflow_adapter.snapshot(sources.stash(BASE_WORKFLOW), scope=scope)
    target = workflow_adapter.snapshot(RevisionRef.parse({
        "kind": "workflow", "ref": "workflow:nightly@2.0.0",
        "hash": "b" * 64}), scope=scope)
    extraction = workflow_adapter.compare(source, target, scope=scope)
    assert extraction.findings == ()
    assert extraction.coverage.both_readable is False
    assert any("target revision could not be read" in item
               for item in extraction.limitations)


def test_an_invariant_this_adapter_cannot_answer_is_never_preserved(
        workflow_adapter, scope):
    """Rule 1 of `contracts.py`, on the class no method here observes."""
    extraction_intent = intent_with("workflow", {"id": "workflow.latency_kept",
                                                 "class": "performance"})
    source = workflow_adapter.snapshot(sources.stash(BASE_WORKFLOW), scope=scope)
    target = workflow_adapter.snapshot(sources.stash(BASE_WORKFLOW), scope=scope)
    result = results_by_id(workflow_adapter.check_invariants(
        extraction_intent, source, target, scope=scope))["workflow.latency_kept"]
    assert result.status == "unknown"
    assert result.confidence == "unknown"
    assert result.limitations


# -- discovery and the invariant catalogue ---------------------------------


def test_the_registry_finds_both_adapters_with_no_list_to_update():
    """The whole reason `ADAPTER_FACTORY` is a module symbol. `registry.py`'s
    docstring records what a tuple cost the State Mirror: five correct adapters
    that did not exist as far as the running system was concerned."""
    registry.reset()
    factories = registry.discover()
    assert workflow_mod.ADAPTER_FACTORY in factories
    assert skill_mod.ADAPTER_FACTORY in factories

    status = registry.status()
    for domain, module in (("workflow", "WorkflowAdapter"), ("skill", "SkillAdapter")):
        assert status[domain]["available"] is True
        assert module in status[domain]["module"]
        assert registry.adapter_for(domain).domain == domain


@pytest.mark.parametrize("invariant_id", [
    workflow_mod.EFFECTS_INVARIANT,
    workflow_mod.PERMISSIONS_INVARIANT,
    workflow_mod.NETWORK_INVARIANT,
    workflow_mod.SECRETS_INVARIANT,
    skill_mod.EFFECTS_INVARIANT,
    skill_mod.PERMISSIONS_INVARIANT,
    skill_mod.NETWORK_INVARIANT,
    skill_mod.SECRETS_INVARIANT,
])
def test_every_invariant_id_these_adapters_point_at_is_one_the_catalogue_declares(
        invariant_id):
    """A finding pointing at an id nobody declares is a finding nobody can act on.

    The constants are written out in the adapters rather than reached out of
    `invariants.py` by index, so this is the test that keeps the two in step: a
    rename over there fails here instead of quietly producing rows that refer to
    an invariant the catalogue has never heard of.
    """
    declared = {item["id"] for item in invariants_mod.SECURITY_INVARIANTS}
    for domain in ("workflow", "skill"):
        declared |= {item["id"]
                     for item in invariants_mod.DOMAIN_DEFAULTS.get(domain, ())}
    assert invariant_id in declared
