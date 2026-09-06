"""Tests for `src/context_engine/experiences.py` (plan §9).

The thing under test is not "does it store rows".  It is the discipline: that
the verdict comes from `prove` and never from the text, that a success with no
evidence is refused by name, that an `unproved` run is kept as history and
never recommended, that a contradicted one comes back labelled as the
anti-pattern it is, and that code moving under an experience degrades it
instead of deleting it.
"""

import pytest

from src import changesets, prove
from src.context_engine import experiences as ex
from src.context_engine import store


@pytest.fixture()
def ce_store(tmp_path):
    """A disposable store.  `use_path` exists for exactly this, so no constant
    that a dozen modules already imported by value has to be monkeypatched."""
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        store.use_path(None)


def _fields(**overrides):
    base = {
        "owner": "u1",
        "project_id": "p1",
        "intent": "bugfix",
        "technologies": ["python", "fastapi"],
        "concepts": ["oauth", "csrf", "state"],
        "problem": "the oauth state was consumed before the token exchange",
        "preconditions": ["the callback route exists"],
        "strategy": ["move the state check after the exchange", "add a regression test"],
        "key_decisions": ["DEC-4"],
        "touched_symbols": ["src/auth/github.py", "exchange_code"],
        "result": "success",
        "verdict": "proved",
        "verification_refs": ["changeset:chg_1"],
        "failure_modes": [],
        "lesson": "consume the state after the exchange, not before",
        "source_run": "run_1",
        "source_revision": "rev_1",
    }
    base.update(overrides)
    return base


# ── the verdict is an input ────────────────────────────────────────────────

def test_verdicts_mirror_prove():
    """The store writes this tuple into a column, so it has to stay the same
    four words `prove` answers with."""
    assert ex.VERDICTS == prove.VERDICTS


def test_a_success_without_evidence_is_rejected_by_name(ce_store):
    with pytest.raises(ex.ExperienceRejected) as caught:
        ex.admit(_fields(verification_refs=[]))
    assert caught.value.field == "verification_refs"
    assert ex.stats(owner="u1", project_id="p1")["total"] == 0


def test_a_success_a_partial_verdict_cannot_support_is_rejected(ce_store):
    with pytest.raises(ex.ExperienceRejected) as caught:
        ex.admit(_fields(verdict="partial"))
    assert caught.value.field == "verdict"


def test_an_unknown_verdict_is_rejected(ce_store):
    with pytest.raises(ex.ExperienceRejected) as caught:
        ex.admit(_fields(verdict=""))
    assert caught.value.field == "verdict"


def test_an_experience_without_a_problem_is_rejected(ce_store):
    with pytest.raises(ex.ExperienceRejected) as caught:
        ex.admit(_fields(problem=""))
    assert caught.value.field == "problem"


def test_a_partial_verdict_still_needs_a_reference(ce_store):
    with pytest.raises(ex.ExperienceRejected) as caught:
        ex.admit(_fields(result="partial", verdict="partial", verification_refs=[]))
    assert caught.value.field == "verification_refs"
    # ...and is admitted the moment it has one.
    kept = ex.admit(_fields(result="partial", verdict="partial",
                            verification_refs=["proof:abc"]))
    assert ex.get(kept.id).verdict == "partial"


def test_unproved_needs_no_reference_and_is_kept_as_history(ce_store):
    kept = ex.admit(_fields(result="abandoned", verdict="unproved",
                            verification_refs=[]))
    assert ex.get(kept.id) is not None
    assert ex.stats(owner="u1", project_id="p1")["by_verdict"]["unproved"] == 1
    # History, not advice: search is the recommendation surface.
    assert ex.search("oauth state exchange", owner="u1", project_id="p1") == []
    assert ex.stats(owner="u1", project_id="p1")["recommendable"] == 0


# ── retrieval (§9.4) ───────────────────────────────────────────────────────

def test_ranking_weights_are_the_ones_the_plan_names():
    assert (ex.W_PROBLEM, ex.W_TECHNOLOGY, ex.W_SYMBOLS,
            ex.W_VERDICT, ex.W_FRESHNESS, ex.W_USEFULNESS) == (
        0.30, 0.20, 0.15, 0.15, 0.10, 0.10)
    total = (ex.W_PROBLEM + ex.W_TECHNOLOGY + ex.W_SYMBOLS
             + ex.W_VERDICT + ex.W_FRESHNESS + ex.W_USEFULNESS)
    assert total == pytest.approx(1.0)


def test_a_hit_reports_every_component_of_its_score(ce_store):
    ex.admit(_fields())
    hit = ex.search("oauth state exchange", owner="u1", project_id="p1")[0]
    assert set(hit["scores"]) == {"problem", "technology", "symbols",
                                  "verdict", "freshness", "usefulness"}
    assert hit["score"] == pytest.approx(sum(
        weight * hit["scores"][name] for name, weight in (
            ("problem", ex.W_PROBLEM), ("technology", ex.W_TECHNOLOGY),
            ("symbols", ex.W_SYMBOLS), ("verdict", ex.W_VERDICT),
            ("freshness", ex.W_FRESHNESS), ("usefulness", ex.W_USEFULNESS))),
        abs=1e-6)


def test_technologies_narrow_the_ranking(ce_store):
    ex.admit(_fields(technologies=["python", "fastapi"]))
    matched = ex.search("state", owner="u1", project_id="p1",
                        technologies=["fastapi"])[0]
    missed = ex.search("state", owner="u1", project_id="p1",
                       technologies=["rust"])[0]
    assert matched["scores"]["technology"] == 1.0
    assert missed["scores"]["technology"] == 0.0
    assert matched["score"] > missed["score"]


def test_a_contradicted_experience_comes_back_as_an_anti_pattern(ce_store):
    ex.admit(_fields(problem="the oauth state check was moved into the router",
                     result="failure", verdict="contradicted",
                     verification_refs=["changeset:chg_2"],
                     failure_modes=["the state was consumed twice"]))
    hit = ex.search("oauth state router", owner="u1", project_id="p1")[0]
    assert hit["role"] == "anti_pattern"

    candidate = ex.as_candidates([hit])[0]
    assert candidate.meta["role"] == "anti_pattern"
    # It must not be able to overrule a validated pattern in a contradiction.
    assert candidate.authority == "agent_claim"
    assert candidate.body.startswith("ANTI-PATTERN")
    assert candidate.section == "past_experiences"
    assert candidate.source_ref == f"experience:{hit['id']}"


def test_a_failed_result_is_an_anti_pattern_even_when_it_was_proved(ce_store):
    """A run can prove that an approach does not work.  That is evidence, and
    it is still not a recipe."""
    kept = ex.admit(_fields(result="failure", verdict="proved",
                            verification_refs=["proof:abc"]))
    assert kept.role() == "anti_pattern"


def test_search_returns_a_few_contrasting_experiences_not_k(ce_store):
    for n in range(4):
        ex.admit(_fields(problem=f"oauth state problem number {n}",
                         source_run=f"run_p{n}", verification_refs=[f"changeset:c{n}"]))
    for n in range(2):
        ex.admit(_fields(problem=f"oauth state failure number {n}",
                         result="failure", verdict="contradicted",
                         source_run=f"run_f{n}", verification_refs=[f"changeset:f{n}"]))

    hits = ex.search("oauth state", owner="u1", project_id="p1", k=5)
    roles = [hit["role"] for hit in hits]
    assert roles.count("pattern") == ex.MAX_PATTERNS == 2
    assert roles.count("anti_pattern") == ex.MAX_ANTI_PATTERNS == 1
    assert len(hits) == 3        # fewer than k, and that is the right answer


def test_scope_isolates_owners_and_projects(ce_store):
    ex.admit(_fields(owner="u1", project_id="p1"))
    ex.admit(_fields(owner="u2", project_id="p2", problem="a different oauth bug"))
    mine = ex.search("oauth", owner="u1", project_id="p1")
    assert [hit["owner"] for hit in mine] == ["u1"]


# ── going stale (§9.4's freshness term, driven by §10.4) ───────────────────

def test_degrade_for_revision_marks_stale_without_deleting(ce_store):
    kept = ex.admit(_fields())
    before = ex.search("oauth state exchange", owner="u1", project_id="p1")[0]

    marked = ex.degrade_for_revision("p1", changed_files=["src/auth/github.py"])
    assert marked == 1

    after_row = ex.get(kept.id)
    assert after_row is not None                       # degraded, not deleted
    assert after_row.source_revision == f"{ex.STALE_REVISION_PREFIX}rev_1"
    assert after_row.stale() is True

    after = ex.search("oauth state exchange", owner="u1", project_id="p1")[0]
    assert after["stale"] is True
    assert after["scores"]["freshness"] == 0.0
    assert after["score"] < before["score"]

    candidate = ex.as_candidates([after])[0]
    assert candidate.degraded is True
    assert "changed since" in candidate.body

    # Idempotent: a second commit touching the same file marks nothing new.
    assert ex.degrade_for_revision("p1", changed_files=["src/auth/github.py"]) == 0


def test_degrade_for_revision_matches_a_symbol_and_a_path_tail(ce_store):
    ex.admit(_fields(source_run="run_sym"))
    assert ex.degrade_for_revision("p1", changed_symbols=["exchange_code"]) == 1
    ex.admit(_fields(source_run="run_tail", verification_refs=["changeset:chg_9"]))
    assert ex.degrade_for_revision("p1", changed_files=["auth/github.py"]) == 1


def test_degrade_for_revision_leaves_other_projects_alone(ce_store):
    ex.admit(_fields(project_id="p2"))
    assert ex.degrade_for_revision("p1", changed_files=["src/auth/github.py"]) == 0


# ── feedback (§9.5) ────────────────────────────────────────────────────────

def test_harm_outweighs_help(ce_store):
    """The `memory_engine` policy, kept as a declared constant rather than a
    copied module: one harmful report costs four helpful ones."""
    assert ex.HARM_WEIGHT == 4.0
    kept = ex.admit(_fields())
    assert kept.usefulness() == pytest.approx(0.5)

    helped = ex.feedback(kept.id, "helpful", ref="ctxpkt_1")
    assert helped is not None and helped.helpful == 1
    assert helped.usefulness() > 0.5

    harmed = ex.feedback(kept.id, "harmful", ref="ctxpkt_2")
    assert harmed.harmful == 1
    # One harmful outweighs the one helpful that came before it.
    assert harmed.usefulness() < 0.5


def test_feedback_is_ignored_for_unknown_kinds_and_ids(ce_store):
    kept = ex.admit(_fields())
    assert ex.feedback(kept.id, "interesting") is None
    assert ex.feedback("exp_nope", "helpful") is None
    assert ex.get(kept.id).helpful == 0


def test_re_admitting_an_id_does_not_erase_what_the_store_learned(ce_store):
    kept = ex.admit(_fields())
    ex.feedback(kept.id, "harmful", ref="proof:abc")
    ex.feedback(kept.id, "helpful", ref="proof:def")

    again = ex.admit(_fields(id=kept.id, lesson="a better sentence"))
    assert again.lesson == "a better sentence"
    assert (again.helpful, again.harmful) == (1, 1)
    assert again.created_at == kept.created_at


def test_feedback_keeps_the_evidence_row(ce_store):
    kept = ex.admit(_fields())
    ex.feedback(kept.id, "harmful", ref="proof:deadbeef")
    with store.db() as conn:
        rows = store.rows(conn.execute(
            "SELECT kind, ref FROM experience_feedback WHERE experience_id = ?",
            (kept.id,)))
    assert rows == [{"kind": "harmful", "ref": "proof:deadbeef"}]


# ── distilling a real run ──────────────────────────────────────────────────

def _changeset(*, verified: bool):
    return changesets.build(
        intent="fix",
        workspace="/tmp/ws",
        checkpoint="abc123",
        changes={"source": "checkpoint", "modified": ["src/auth/github.py"],
                 "checkpoint": "abc123"},
        verification=({"mode": "tests", "ran": True, "ok": True,
                       "summary": "12 passed", "command": "pytest -q"}
                      if verified else {"mode": "none", "ran": False}),
        claims=[{"path": "src/auth/github.py", "kind": "modified"}],
        title="the oauth state was consumed before the token exchange",
        run_id="run_9", owner="u1", project_id="p1")


def test_from_changeset_takes_the_verdict_from_the_proof(ce_store):
    changeset = _changeset(verified=True)
    proof = changesets.judge(changeset)
    assert proof["verdict"] == "proved"

    kept = ex.from_changeset(
        changeset, proof, lesson="consume the state after the exchange",
        technologies=["python"], concepts=["oauth"],
        strategy=["move the check"])

    assert kept.verdict == "proved"
    assert kept.result == "success"
    assert kept.source_run == "run_9"
    assert kept.source_revision == "abc123"
    assert kept.touched_symbols == ("src/auth/github.py",)
    assert f"changeset:{changeset.id}" in kept.verification_refs
    assert "checkpoint:abc123" in kept.verification_refs
    assert f"proof:{proof['identity']}" in kept.verification_refs
    assert ex.get(kept.id) is not None


def test_from_changeset_will_not_let_a_caller_call_it_a_success(ce_store):
    """The point of the whole module: the narrative fields are the caller's,
    the verdict is not.  A run nothing verified cannot be filed as a success
    however confidently it is described."""
    changeset = _changeset(verified=False)
    proof = changesets.judge(changeset)
    assert proof["verdict"] == "partial"

    with pytest.raises(ex.ExperienceRejected) as caught:
        ex.from_changeset(changeset, proof, result="success",
                          lesson="it definitely works now")
    assert caught.value.field == "verdict"

    # Filed honestly, it is kept — with the change set as its reference.
    kept = ex.from_changeset(changeset, proof, lesson="it may work; nothing ran")
    assert (kept.result, kept.verdict) == ("partial", "partial")
    assert kept.verification_refs[0].startswith("changeset:")


# ── the candidate handed to the compiler ───────────────────────────────────

def test_candidates_carry_provenance_and_the_verdict_trust(ce_store):
    ex.admit(_fields())
    hit = ex.search("oauth state exchange", owner="u1", project_id="p1")[0]
    candidate = ex.as_candidates([hit])[0]

    assert candidate.source_type == "experience"
    assert candidate.section == "past_experiences"
    assert candidate.trust_class == "proved"
    assert candidate.authority == "validated_experience"
    assert candidate.owner == "u1" and candidate.project_id == "p1"
    assert candidate.scores["total"] == pytest.approx(hit["score"])
    assert candidate.meta["verification_refs"] == ["changeset:chg_1"]
    assert "Lesson: consume the state after the exchange" in candidate.body


def test_as_candidates_survives_a_malformed_hit(ce_store):
    assert ex.as_candidates([None, 42]) == []


# ── housekeeping ───────────────────────────────────────────────────────────

def test_stats_counts_what_could_not_be_proved(ce_store):
    ex.admit(_fields())
    ex.admit(_fields(result="abandoned", verdict="unproved", verification_refs=[]))
    ex.admit(_fields(result="failure", verdict="contradicted",
                     verification_refs=["changeset:chg_3"]))
    report = ex.stats(owner="u1", project_id="p1")
    assert report["total"] == 3
    assert report["by_verdict"] == {"proved": 1, "partial": 0,
                                    "unproved": 1, "contradicted": 1}
    assert report["by_role"] == {"pattern": 1, "anti_pattern": 2}
    assert report["recommendable"] == 2


def test_delete_removes_the_experience_and_its_feedback(ce_store):
    kept = ex.admit(_fields())
    ex.feedback(kept.id, "helpful")
    assert ex.delete(kept.id) is True
    assert ex.get(kept.id) is None
    assert ex.delete(kept.id) is False
    with store.db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM experience_feedback").fetchone()["n"] == 0


def test_reads_degrade_instead_of_raising_when_the_store_is_gone(tmp_path):
    """Everything except `admit` is on a read path: a broken store costs the
    section, never the turn."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    store.use_path(str(blocker / "ce.db"))
    try:
        assert ex.search("anything") == []
        assert ex.get("exp_1") is None
        assert ex.stats()["total"] == 0
        assert ex.degrade_for_revision("p1", changed_files=["a.py"]) == 0
        assert ex.feedback("exp_1", "helpful") is None
        assert ex.delete("exp_1") is False
    finally:
        store.use_path(None)
