"""Learned memory — the scored store (src/memory_engine.py), the deterministic
Curator (src/memory_curator.py), and every hook they hang off: the agent tool,
the prompt injection with its per-turn injected ids, the outcome attribution
from the turn's verification signal, and the HTTP API.

The point being tested throughout: the score is a PURE function of the record
(so it can always be explained), the Curator is arithmetic and not a model
(so deleting a memory is trustworthy), and nothing here may ever raise into a
chat hot path — a missing vector store or a corrupt database costs the block,
not the turn.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import memory_curator as curator  # noqa: E402
from src import memory_engine as engine  # noqa: E402

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ago(days: float, now: datetime = NOW) -> str:
    return engine._iso(now - timedelta(days=days))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A disposable database with the semantic lane explicitly absent."""
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    yield tmp_path
    engine.reset_vector_store()
    engine.clear_injected()


class FakeVectors:
    """A vector store that is healthy and answers with a fixed ranking."""

    healthy = True

    def __init__(self, scores=None):
        self.scores = scores or {}
        self.added = []
        self.removed = []

    def add(self, memory_id, text):
        self.added.append((memory_id, text))

    def remove(self, memory_id):
        self.removed.append(memory_id)

    def search(self, query, k=8):
        return [{"memory_id": mid, "score": score}
                for mid, score in sorted(self.scores.items(), key=lambda kv: -kv[1])][:k]


def _rule(text="Always run the project tests before claiming done", **kw):
    kw.setdefault("owner", "luis")
    kw.setdefault("level", "procedural")
    kw.setdefault("trust_class", "human_explicit")
    kw.setdefault("now", NOW)
    return engine.add_item(text, **kw)


def _events(count, days_ago=0.0, weight=1.0, ref_prefix="sess-", now=NOW):
    return [{"ts": _ago(days_ago, now), "weight": weight,
             "reason": "", "ref": f"{ref_prefix}{i}"} for i in range(count)]


# ── scoring: hand-computed values ─────────────────────────────────────


def test_feedback_decays_by_half_every_ninety_days():
    assert engine.decayed(_events(1, 0), NOW) == pytest.approx(1.0)
    assert engine.decayed(_events(1, 90), NOW) == pytest.approx(0.5)
    assert engine.decayed(_events(1, 180), NOW) == pytest.approx(0.25)
    # Weights add, each with its own age.
    mixed = _events(1, 0) + _events(1, 90, ref_prefix="other-")
    assert engine.decayed(mixed, NOW) == pytest.approx(1.5)
    # A future or unparseable timestamp is "just now", never "infinitely old".
    assert engine.decayed([{"ts": "banana", "weight": 2.0}], NOW) == pytest.approx(2.0)


def test_item_freshness_follows_the_level_half_life():
    assert engine.freshness("working", _ago(1), NOW) == pytest.approx(0.5)
    assert engine.freshness("episodic", _ago(30), NOW) == pytest.approx(0.5)
    assert engine.freshness("semantic", _ago(180), NOW) == pytest.approx(0.5)
    assert engine.freshness("semantic", _ago(360), NOW) == pytest.approx(0.25)


def test_a_procedural_rule_never_decays_with_time():
    """A rule does not become less true in June — it only dies by being
    contradicted, which is what the harmful term is for."""
    for age in (0, 1, 180, 3650):
        assert engine.freshness("procedural", _ago(age), NOW) == 1.0
    old = {"level": "procedural", "trust": 0.85, "updated_at": _ago(3650),
           "helpful": [], "harmful": []}
    assert engine.effective_score(old, NOW) == pytest.approx(0.85)


def test_one_harmful_event_outweighs_four_helpful_ones():
    item = {"level": "procedural", "trust": 0.85, "updated_at": _ago(0),
            "helpful": _events(4, 0), "harmful": _events(1, 0, ref_prefix="bad-")}
    # 0.85 * 1.0 - 4 * 1.0 + 4 * 1.0 == 0.85, exactly break-even.
    assert engine.effective_score(item, NOW) == pytest.approx(0.85)
    item["harmful"] = _events(2, 0, ref_prefix="bad-")
    assert engine.effective_score(item, NOW) == pytest.approx(0.85 - 4.0)


def test_harmful_ratio_is_zero_without_any_feedback():
    """No evidence of harm is not the same as maximum harm."""
    assert engine.harmful_ratio({"helpful": [], "harmful": []}, NOW) == 0.0
    item = {"helpful": _events(1, 0), "harmful": _events(3, 0, ref_prefix="bad-")}
    assert engine.harmful_ratio(item, NOW) == pytest.approx(0.75)


def test_distinct_refs_counts_sources_not_events():
    """Eight events from one runaway session are one vouch, not eight."""
    same = [{"ts": _ago(0), "weight": 1, "ref": "sess-1"} for _ in range(8)]
    assert engine.distinct_refs(same) == 1
    assert engine.distinct_refs(_events(8, 0)) == 8


# ── the store ─────────────────────────────────────────────────────────


def test_an_item_round_trips_with_its_trust_class_ceiling(store):
    item = _rule()
    assert item["trust"] == engine.TRUST_CLASSES["human_explicit"] == 0.85
    stored = engine.get_item(item["id"])
    assert stored["text"] == item["text"] and stored["level"] == "procedural"
    assert stored["status"] == "active" and stored["maturity"] == "candidate"
    assert stored["helpful"] == [] and stored["harmful"] == []
    assert os.path.isfile(os.path.join(str(store), engine.DB_FILENAME))


def test_bad_input_is_rejected_with_a_readable_error(store):
    for kwargs, needle in (
        ({"text": "  "}, "empty"),
        ({"text": "x", "level": "eidetic"}, "level"),
        ({"text": "x", "trust_class": "divine"}, "trust_class"),
        ({"text": "x", "status": "wobbly"}, "status"),
    ):
        with pytest.raises(engine.MemoryEngineError) as exc:
            engine.add_item(**kwargs)
        assert needle in str(exc.value)


def test_feedback_is_appended_and_never_overwritten(store):
    item = _rule()
    engine.add_feedback(item["id"], "helpful", reason="it worked", ref="sess-1", now=NOW)
    engine.add_feedback(item["id"], "harmful", reason="wrong here", ref="sess-2", now=NOW)
    stored = engine.get_item(item["id"])
    assert [e["reason"] for e in stored["helpful"]] == ["it worked"]
    assert [e["ref"] for e in stored["harmful"]] == ["sess-2"]
    with pytest.raises(engine.MemoryEngineError):
        engine.add_feedback(item["id"], "meh")
    assert engine.add_feedback("nope", "helpful") is None


def test_an_id8_prefix_resolves_but_an_ambiguous_one_does_not(store):
    item = _rule()
    assert engine.resolve_id(item["id"][:8]) == item["id"]
    assert engine.resolve_id(item["id"]) == item["id"]
    assert engine.resolve_id("zzzzzzzz") is None
    assert engine.resolve_id("") is None


def test_scope_includes_the_unscoped_ones(store):
    _rule("Global rule", owner="", project="")
    _rule("Mine here", owner="luis", project="/repo")
    _rule("Someone else's", owner="ana", project="/repo")
    texts = {i["text"] for i in engine.scoped_items("luis", "/repo")}
    assert texts == {"Global rule", "Mine here"}


# ── hybrid retrieval and its explicit degradation ─────────────────────


def test_search_renormalises_the_lexical_lane_when_vectors_are_absent(store):
    item = _rule("Prefer edit_file over rewriting the whole module")
    hits = engine.search("edit_file rewriting", "luis", "", now=NOW)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["degraded"] is True and hit["semantic"] == 0.0
    # 0.45 renormalised to 0.90; the top lexical hit is 1.0 after normalisation.
    assert hit["relevance"] == pytest.approx(engine.W_LEXICAL_DEGRADED * hit["lexical"])
    assert hit["score"] == pytest.approx(hit["relevance"] * hit["effective_score"])
    assert hit["id"] == item["id"]


def test_a_missing_vector_store_is_a_degradation_and_never_an_error(store, monkeypatch):
    """Absence, an unimportable module and a sick store all take the same
    lexical-only path — the feature keeps working, and says so."""
    engine.reset_vector_store()

    class Exploding:
        healthy = True

        def add(self, *a, **k):
            raise RuntimeError("chroma is on fire")

        def remove(self, *a, **k):
            raise RuntimeError("chroma is on fire")

        def search(self, *a, **k):
            raise RuntimeError("chroma is on fire")

    engine.set_vector_store(Exploding())
    item = _rule("Prefer edit_file over rewriting the whole module")   # add() explodes
    hits = engine.search("edit_file", "luis", "", now=NOW)
    assert hits and hits[0]["degraded"] is True and hits[0]["id"] == item["id"]
    assert engine.delete_item(item["id"]) is True                      # remove() explodes


def test_the_semantic_lane_is_used_at_full_weight_when_present(store):
    engine.reset_vector_store()
    quiet = _rule("An entirely unrelated note about coffee", level="semantic")
    engine.set_vector_store(FakeVectors({quiet["id"]: 1.0}))
    hits = engine.search("coffee", "luis", "", now=NOW)
    assert hits[0]["degraded"] is False
    assert hits[0]["semantic"] == 1.0
    expected = engine.W_LEXICAL * hits[0]["lexical"] + engine.W_SEMANTIC * 1.0
    assert hits[0]["relevance"] == pytest.approx(expected)


def test_the_graph_lane_joins_on_evidence_refs(store):
    plain = _rule("Nothing to do with anything", level="semantic")
    linked = _rule("The cart total is computed in cart.py", level="semantic",
                   evidence=[{"kind": "file", "ref": "src/cart.py"},
                             {"kind": "dispatch", "ref": "OBJ-7"}])
    hits = {h["id"]: h for h in engine.search("what changed in src/cart.py for OBJ-7",
                                              "luis", "", now=NOW)}
    assert hits[linked["id"]]["graph"] > 0
    assert plain["id"] not in hits or hits[plain["id"]]["graph"] == 0.0


def test_retrieval_touches_only_the_items_it_surfaced(store):
    hit = _rule("Prefer edit_file over rewriting the whole module")
    miss = _rule("Completely different subject matter here")
    engine.search("edit_file", "luis", "", now=NOW)
    assert engine.get_item(hit["id"])["access_count"] >= 1
    assert engine.get_item(miss["id"])["access_count"] == 0


# ── pack(): determinism, budget, anti-patterns ────────────────────────


def test_pack_puts_rules_first_then_memories_then_anti_patterns(store):
    _rule("Always run the project tests before claiming done")
    _rule("The deploy script lives in ops/deploy.sh", level="semantic")
    anti = _rule("Rewrite whole files with bash heredocs")
    for i in range(3):
        engine.add_feedback(anti["id"], "harmful", ref=f"sess-{i}", now=NOW)
    curator.curate("luis", "", now=NOW)

    block = engine.pack("luis", "", "deploy script", now=NOW)
    assert block.index(engine.PACK_RULES_HEADER) < block.index(engine.PACK_MEMORIES_HEADER)
    assert block.index(engine.PACK_MEMORIES_HEADER) < block.index(engine.PACK_ANTI_HEADER)
    assert "- [" in block and "] Always run the project tests" in block
    assert "AVOID: Rewrite whole files with bash heredocs" in block
    assert "ops/deploy.sh" in block
    # Anti-patterns are never score-filtered: inversion leaves the score deeply
    # negative BY CONSTRUCTION, and the warning is the whole point of keeping it.
    assert engine.public_item(engine.get_item(anti["id"]), NOW)["effective_score"] < 0


def test_pack_is_byte_identical_for_the_same_store_and_clock(store):
    for i in range(6):
        _rule(f"Rule number {i} about testing and deploying")
    first = engine.pack("luis", "", "testing", now=NOW)
    second = engine.pack("luis", "", "testing", now=NOW)
    assert first == second and first


def test_pack_respects_a_hard_character_budget(store):
    for i in range(40):
        _rule(f"Rule number {i} about testing, deploying and {'padding ' * 8}")
    for budget in (120, 400, 1800):
        block = engine.pack("luis", "", "testing", budget, now=NOW)
        assert len(block) <= budget, budget
    assert engine.pack("luis", "", "testing", 0, now=NOW) == ""


def test_pack_is_empty_when_nothing_qualifies(store):
    assert engine.pack("luis", "", "anything", now=NOW) == ""
    stale = _rule("A working note that has long expired", level="working")
    engine.save_item({**engine.get_item(stale["id"]), "updated_at": _ago(30)})
    assert engine.pack("luis", "", "working note", now=NOW) == ""


def test_pack_reports_the_ids_it_injected(store):
    a = _rule("Always run the project tests before claiming done")
    b = _rule("The deploy script lives in ops/deploy.sh", level="semantic")
    detail = engine.pack_detail("luis", "", "deploy script", now=NOW)
    assert set(detail["ids"]) == {a["id"], b["id"]}
    assert all(f"[{i[:8]}]" in detail["text"] for i in detail["ids"])


# ── the maturity ladder ───────────────────────────────────────────────


def test_three_distinct_refs_promote_a_candidate_to_established(store):
    item = _rule()
    for i in range(2):
        engine.add_feedback(item["id"], "helpful", ref=f"sess-{i}", now=NOW)
    curator.curate("luis", "", now=NOW)
    assert engine.get_item(item["id"])["maturity"] == "candidate"

    engine.add_feedback(item["id"], "helpful", ref="sess-2", now=NOW)
    report = curator.curate("luis", "", now=NOW)
    assert engine.get_item(item["id"])["maturity"] == "established"
    assert report["promoted"] == 1


def test_eight_clean_refs_promote_established_to_proven(store):
    item = _rule()
    for i in range(8):
        engine.add_feedback(item["id"], "helpful", ref=f"sess-{i}", now=NOW)
    curator.curate("luis", "", now=NOW)
    assert engine.get_item(item["id"])["maturity"] == "proven"


def test_proven_needs_a_harmful_ratio_under_twenty_percent(store):
    item = _rule()
    for i in range(8):
        engine.add_feedback(item["id"], "helpful", ref=f"sess-{i}", now=NOW)
    for i in range(2):                       # 2/10 = 20%, not < 20%
        engine.add_feedback(item["id"], "harmful", ref=f"bad-{i}", now=NOW)
    curator.curate("luis", "", now=NOW)
    assert engine.get_item(item["id"])["maturity"] == "established"


def test_a_faded_item_is_deprecated_by_score(store):
    item = _rule("A working note nobody ever used again", level="working")
    engine.save_item({**engine.get_item(item["id"]), "updated_at": _ago(30)})
    report = curator.curate("luis", "", now=NOW)
    stored = engine.get_item(item["id"])
    assert stored["status"] == "deprecated" and stored["maturity"] == "deprecated"
    assert report["demoted"] == 1 and report["total_active"] == 0
    # Idempotent: a second run does not re-count the same demotion.
    assert curator.curate("luis", "", now=NOW)["demoted"] == 0


def test_a_procedural_rule_with_recent_help_survives_a_bad_score(store):
    """Procedural memory decays only through contradiction — so a rule that
    is still helping right now is not deleted for a negative number."""
    saved = _rule("Contested but still useful")
    engine.add_feedback(saved["id"], "harmful", ref="bad-1", now=NOW)
    engine.add_feedback(saved["id"], "helpful", ref="good-1", now=NOW)

    doomed = _rule("Contested and no longer helping anyone")
    engine.add_feedback(doomed["id"], "harmful", ref="bad-2", now=NOW)
    stored = engine.get_item(doomed["id"])
    stored["helpful"] = _events(1, 100, ref_prefix="ancient-")
    engine.save_item(stored)

    curator.curate("luis", "", now=NOW)
    assert engine.get_item(saved["id"])["status"] == "active"
    assert engine.get_item(doomed["id"])["status"] == "deprecated"


# ── inversion: the signature move ─────────────────────────────────────


def test_a_mostly_harmful_rule_is_inverted_not_deleted(store):
    item = _rule("Rewrite whole files with bash heredocs")
    engine.add_feedback(item["id"], "helpful", ref="good-1", now=NOW)
    for i in range(3):                       # 3/4 = 75% harmful, n >= 3
        engine.add_feedback(item["id"], "harmful", ref=f"bad-{i}", now=NOW)
    report = curator.curate("luis", "", now=NOW)

    stored = engine.get_item(item["id"])
    assert report["inverted"] == 1
    assert stored["status"] == "anti_pattern"
    assert stored["text"] == "AVOID: Rewrite whole files with bash heredocs"
    assert stored["inverted_from"] == "Rewrite whole files with bash heredocs"
    assert stored["maturity"] == "candidate"
    # The original survives inside the evidence too, never silently lost.
    assert any("inverted from" in (e.get("excerpt") or "") for e in stored["evidence"])
    assert "AVOID: Rewrite whole files" in engine.pack("luis", "", "heredocs", now=NOW)


def test_inversion_needs_both_the_ratio_and_three_events(store):
    two_bad = _rule("Only twice contradicted")
    for i in range(2):
        engine.add_feedback(two_bad["id"], "harmful", ref=f"bad-{i}", now=NOW)

    mostly_good = _rule("Contradicted three times but helped ten")
    for i in range(10):
        engine.add_feedback(mostly_good["id"], "helpful", ref=f"good-{i}", now=NOW)
    for i in range(3):
        engine.add_feedback(mostly_good["id"], "harmful", ref=f"bad-{i}", now=NOW)

    assert curator.curate("luis", "", now=NOW)["inverted"] == 0
    assert engine.get_item(two_bad["id"])["status"] != "anti_pattern"
    assert engine.get_item(mostly_good["id"])["status"] != "anti_pattern"


def test_inversion_happens_once(store):
    item = _rule("Rewrite whole files with bash heredocs")
    for i in range(3):
        engine.add_feedback(item["id"], "harmful", ref=f"bad-{i}", now=NOW)
    assert curator.curate("luis", "", now=NOW)["inverted"] == 1
    assert curator.curate("luis", "", now=NOW)["inverted"] == 0
    assert engine.get_item(item["id"])["text"].count("AVOID:") == 1


# ── the Curator: dedupe, conflict, prune ──────────────────────────────


def test_dedupe_keeps_the_better_item_and_merges_the_events(store):
    keep = _rule("Run the linter before every commit")
    drop = _rule("Run the linter before every commit", trust_class="agent_assertion")
    engine.add_feedback(keep["id"], "helpful", ref="keep-1", now=NOW)
    engine.add_feedback(drop["id"], "helpful", ref="drop-1", now=NOW)
    engine.add_feedback(drop["id"], "harmful", ref="drop-bad", now=NOW)
    engine.add_evidence(drop["id"], [{"kind": "file", "ref": "Makefile"}])

    report = curator.curate("luis", "", now=NOW)
    assert report["deduped"] == 1
    assert engine.get_item(drop["id"]) is None
    survivor = engine.get_item(keep["id"])
    assert {e["ref"] for e in survivor["helpful"]} == {"keep-1", "drop-1"}
    assert {e["ref"] for e in survivor["harmful"]} == {"drop-bad"}
    assert any(e.get("ref") == "Makefile" for e in survivor["evidence"])


def test_dedupe_also_catches_near_duplicates_and_is_idempotent(store):
    first = _rule("Always run the project tests before claiming a task done")
    engine.add_item("Always run the project tests before claiming a task done now",
                    owner="luis", level="procedural", now=NOW)
    assert curator.curate("luis", "", now=NOW)["deduped"] == 1
    assert curator.curate("luis", "", now=NOW)["deduped"] == 0
    assert engine.get_item(first["id"]) is not None


def test_an_anti_pattern_deprecates_the_active_rule_it_contradicts(store):
    text = "Rewrite whole files with bash heredocs when in a hurry"
    inverted = _rule(text)
    for i in range(3):
        engine.add_feedback(inverted["id"], "harmful", ref=f"bad-{i}", now=NOW)
    curator.curate("luis", "", now=NOW)          # inverts it

    reasserted = _rule(text)                     # the agent asserts it again
    report = curator.curate("luis", "", now=NOW)
    assert report["conflicts"] == 1
    assert engine.get_item(inverted["id"])["status"] == "anti_pattern"
    assert engine.get_item(reasserted["id"])["status"] == "deprecated"


def test_deprecated_items_are_pruned_after_ninety_untouched_days(store):
    item = _rule("A rule from another era", level="semantic")
    stale = engine.get_item(item["id"])
    stale.update({"status": "deprecated", "maturity": "deprecated",
                  "updated_at": _ago(400), "last_accessed": _ago(400)})
    engine.save_item(stale)
    fresh = _rule("Recently deprecated, still on probation", level="semantic")
    warm = engine.get_item(fresh["id"])
    warm.update({"status": "deprecated", "maturity": "deprecated",
                 "updated_at": _ago(10), "last_accessed": _ago(10)})
    engine.save_item(warm)

    report = curator.curate("luis", "", now=NOW)
    assert report["pruned"] == 1
    assert engine.get_item(item["id"]) is None
    assert engine.get_item(fresh["id"]) is not None


def test_the_curator_report_has_every_counter(store):
    _rule()
    report = curator.curate("luis", "", now=NOW)
    assert set(report) == {"deduped", "conflicts", "inverted", "promoted",
                           "demoted", "pruned", "total_active"}
    assert report["total_active"] == 1


def test_safe_curate_swallows_a_broken_store(store, monkeypatch):
    monkeypatch.setattr(engine, "scoped_items",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("banana")))
    report = curator.safe_curate("luis", "")
    assert report["total_active"] == 0 and "banana" in report["error"]


# ── the learning loop: injected ids → outcome attribution ─────────────


def test_a_passing_turn_credits_the_rules_it_was_shown(store):
    rule = _rule()
    fact = _rule("The deploy script lives in ops/deploy.sh", level="semantic")
    detail = engine.pack_detail("luis", "", "deploy script", now=NOW)
    engine.note_injected("sess-42", detail["ids"])
    assert set(engine.peek_injected("sess-42")) == {rule["id"], fact["id"]}

    result = engine.record_outcome("sess-42", "pass", ref="sess-42", now=NOW)
    assert result["kind"] == "helpful" and result["ids"] == [rule["id"]]
    assert len(engine.get_item(rule["id"])["helpful"]) == 1
    assert engine.get_item(rule["id"])["helpful"][0]["ref"] == "sess-42"
    # Facts are not rules: a green test run is not evidence about a fact.
    assert engine.get_item(fact["id"])["helpful"] == []
    # The ids are consumed, so one turn is credited exactly once.
    assert engine.peek_injected("sess-42") == []
    assert engine.record_outcome("sess-42", "pass", now=NOW)["applied"] == 0


def test_a_failing_turn_blames_them(store):
    rule = _rule()
    engine.note_injected("sess-7", [rule["id"]])
    result = engine.record_outcome("sess-7", "fail", now=NOW)
    assert result["kind"] == "harmful" and result["applied"] == 1
    assert len(engine.get_item(rule["id"])["harmful"]) == 1


def test_an_unmeasured_turn_invents_no_feedback(store):
    rule = _rule()
    engine.note_injected("sess-9", [rule["id"]])
    result = engine.record_outcome("sess-9", None, now=NOW)
    assert result == {"kind": None, "ids": [], "applied": 0}
    stored = engine.get_item(rule["id"])
    assert stored["helpful"] == [] and stored["harmful"] == []
    # ...but the turn's ids are released, so they cannot be credited later.
    assert engine.peek_injected("sess-9") == []


def test_the_outcome_signal_is_read_from_the_harness_summary():
    assert engine.outcome_from_harness({"tests": {"ran": True, "ok": True}}) == "pass"
    assert engine.outcome_from_harness({"tests": {"ran": True, "ok": False}}) == "fail"
    assert engine.outcome_from_harness(
        {"tests": {"ran": True, "ok": False, "inconclusive": True}}) is None
    assert engine.outcome_from_harness({"tests": {"ran": False}}) is None
    # Auto-review is the fallback when no tests ran.
    assert engine.outcome_from_harness({"review": {"verdict": "ok"}}) == "pass"
    assert engine.outcome_from_harness({"review": {"verdict": "issues"}}) == "fail"
    assert engine.outcome_from_harness({"review": {"verdict": "skipped"}}) is None
    assert engine.outcome_from_harness(None) is None
    assert engine.outcome_from_harness({}) is None
    # Tests win over the reviewer when both spoke.
    assert engine.outcome_from_harness(
        {"tests": {"ran": True, "ok": True}, "review": {"verdict": "issues"}}) == "pass"


def test_injected_ids_expire_and_never_grow_without_bound(store):
    engine.note_injected("old", ["a"], now_ts=1000.0)
    engine.note_injected("new", ["b"], now_ts=1000.0 + engine.INJECTED_TTL_S + 1)
    assert engine.peek_injected("old") == []
    assert engine.peek_injected("new") == ["b"]
    for i in range(engine.INJECTED_MAX_KEYS + 20):
        engine.note_injected(f"k{i}", ["x"], now_ts=2000.0)
    assert len(engine._INJECTED) <= engine.INJECTED_MAX_KEYS


def test_record_outcome_never_raises(store, monkeypatch):
    engine.note_injected("sess-1", ["whatever"])
    monkeypatch.setattr(engine, "get_item",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("banana")))
    assert engine.record_outcome("sess-1", "pass")["applied"] == 0


# ── defensive: a corrupt database costs the block, not the turn ───────


def test_a_corrupt_database_is_moved_aside_and_recreated(store):
    item = _rule()
    path = engine.db_path()
    with open(path, "wb") as fh:
        fh.write(b"this is definitely not a sqlite database")

    assert engine.list_items() == []                 # recreated, empty
    assert os.path.isfile(path + ".corrupt")         # kept, never destroyed
    again = _rule("Life goes on")
    assert engine.get_item(again["id"]) is not None
    assert engine.get_item(item["id"]) is None


def test_pack_never_raises_into_the_prompt_path(store, monkeypatch):
    _rule()
    assert engine.pack("luis", "", "tests", now=NOW)
    monkeypatch.setattr(engine, "scoped_items",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("banana")))
    assert engine.pack("luis", "", "tests", now=NOW) == ""


def test_pack_survives_a_database_that_cannot_be_opened(store, monkeypatch):
    _rule()
    monkeypatch.setattr(engine, "DATA_DIR", "\0 not a directory")
    assert engine.pack("luis", "", "tests", now=NOW) == ""
    assert engine.stats("luis")["total"] == 0


def test_note_injected_never_raises(store):
    engine.note_injected(None, ["a"])                # no key: nothing recorded
    engine.note_injected("k", [])                    # no ids: nothing recorded
    assert engine.peek_injected("k") == []


# ── the agent tool ────────────────────────────────────────────────────


def _block(args):
    from src.agent_tools import ToolBlock
    return ToolBlock(tool_type="memory_rules", content=json.dumps(args))


async def test_tool_add_search_feedback_and_list(store, monkeypatch):
    import services.projects as projects_mod
    from src.tool_execution import _execute_tool_block_impl
    monkeypatch.setattr(projects_mod, "project_for_session", lambda sid, owner=None: None)

    desc, result = await _execute_tool_block_impl(
        _block({"action": "add", "text": "Prefer edit_file over write_file for edits"}),
        session_id="sess-1", owner="luis")
    assert desc == "memory_rules: add"
    added = result["added"]
    # An agent assertion is worth half a human's word, and it says so.
    assert added["trust_class"] == "agent_assertion" and added["level"] == "procedural"
    assert added["score"] == pytest.approx(engine.TRUST_CLASSES["agent_assertion"])
    assert len(added["id"]) == 8

    desc, result = await _execute_tool_block_impl(
        _block({"action": "search", "query": "edit_file"}), session_id="sess-1", owner="luis")
    assert desc == "memory_rules: search"
    assert result["results"][0]["id"] == added["id"] and result["degraded"] is True

    desc, result = await _execute_tool_block_impl(
        _block({"action": "feedback", "id": added["id"], "kind": "helpful",
                "reason": "it was right"}), session_id="sess-1", owner="luis")
    assert desc == "memory_rules: feedback"
    assert result["updated"]["score"] > added["score"]
    stored = engine.get_item(added["full_id"])
    assert stored["helpful"][0]["ref"] == "sess-1"       # attributed to the turn

    desc, result = await _execute_tool_block_impl(
        _block({"action": "list"}), session_id="sess-1", owner="luis")
    assert desc == "memory_rules: list"
    assert [r["id"] for r in result["items"]] == [added["id"]] and result["total"] == 1


async def test_tool_scopes_new_rules_to_the_session_project(store, monkeypatch):
    import services.projects as projects_mod
    from src.tool_execution import _execute_tool_block_impl
    monkeypatch.setattr(projects_mod, "project_for_session",
                        lambda sid, owner=None: {"workspace": "/repo/covernet"})
    _, result = await _execute_tool_block_impl(
        _block({"action": "add", "text": "This project pins numpy", "level": "semantic"}),
        session_id="sess-1", owner="luis")
    assert engine.get_item(result["added"]["full_id"])["project"] == "/repo/covernet"


async def test_tool_rejects_garbage_without_raising(store, monkeypatch):
    import services.projects as projects_mod
    from src.agent_tools import ToolBlock
    from src.tool_execution import _execute_tool_block_impl
    monkeypatch.setattr(projects_mod, "project_for_session", lambda sid, owner=None: None)
    for content in ("not json", json.dumps([1, 2]),
                    json.dumps({"action": "obliterate"}),
                    json.dumps({"action": "add", "text": "   "}),
                    json.dumps({"action": "add", "text": "x", "level": "eidetic"}),
                    json.dumps({"action": "feedback", "id": "zzzzzzzz", "kind": "helpful"}),
                    json.dumps({"action": "feedback", "id": "", "kind": "sideways"})):
        block = ToolBlock(tool_type="memory_rules", content=content)
        _, result = await _execute_tool_block_impl(block, session_id="s", owner="luis")
        assert result["exit_code"] == 1 and result["error"], content


def test_the_tool_is_wired_everywhere_it_has_to_be():
    from src.agent_loop import TOOL_SECTIONS
    from src.agent_tools import TOOL_TAGS
    from src.tool_capabilities import ToolEffect, capabilities_for_tool
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block

    assert "memory_rules" in TOOL_TAGS
    assert "memory_rules" in TOOL_SECTIONS
    assert "memory_rules" in BUILTIN_TOOL_DESCRIPTIONS
    schema = next(s for s in FUNCTION_TOOL_SCHEMAS
                  if s["function"]["name"] == "memory_rules")
    assert schema["function"]["parameters"]["properties"]["action"]["enum"] == [
        "add", "search", "feedback", "list"]
    # Its writes are typed rows in its own store, not workspace content, so it
    # is classed with the private readers and never trips the write gate.
    effects = capabilities_for_tool("memory_rules").effects
    assert ToolEffect.READ_PRIVATE in effects
    assert ToolEffect.WRITE_WORKSPACE not in effects
    block = function_call_to_tool_block("memory_rules", {"action": "list"})
    assert block.tool_type == "memory_rules"
    assert json.loads(block.content) == {"action": "list"}


# ── settings ──────────────────────────────────────────────────────────


def test_the_settings_exist_and_are_described(store, monkeypatch):
    from src.agent_settings_schema import GROUPS, schema_problems
    from src.settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["agent_learned_memory"] is True
    assert DEFAULT_SETTINGS["agent_learned_memory_chars"] == engine.DEFAULT_PACK_CHARS
    described = {f["key"] for g in GROUPS for f in g["fields"]}
    assert {"agent_learned_memory", "agent_learned_memory_chars"} <= described
    assert schema_problems() == []

    values = {"agent_learned_memory": False, "agent_learned_memory_chars": 999_999}
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: values.get(key, default))
    assert engine.injection_enabled() is False
    assert engine.injection_budget() == 20000        # clamped, never unbounded


def test_the_budget_setting_is_what_pack_gets(store, monkeypatch):
    for i in range(20):
        _rule(f"Rule number {i} with enough text to matter for the budget")
    values = {"agent_learned_memory": True, "agent_learned_memory_chars": 200}
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: values.get(key, default))
    assert len(engine.pack("luis", "", "rule", engine.injection_budget(), now=NOW)) <= 200


# ── prompt injection + the turn-end hook, as the agent loop calls them ──


def test_the_prompt_block_carries_the_rules_and_records_the_ids(store):
    """The two halves of the loop, exercised the way src/agent_loop.py calls
    them: pack_detail() into an untrusted context message, note_injected()
    against the session, then the harness verdict at the end of the turn."""
    rule = _rule()
    detail = engine.pack_detail("luis", "", "run the tests", engine.injection_budget())
    assert rule["id"] in detail["ids"]

    from src.prompt_security import untrusted_context_message
    message = untrusted_context_message("learned memory", detail["text"])
    assert message["role"] == "user"                 # never the trusted system role
    assert message["metadata"]["trusted"] is False
    assert "Always run the project tests" in message["content"]

    engine.note_injected("sess-hook", detail["ids"])
    outcome = engine.outcome_from_harness({"tests": {"ran": True, "ok": True}})
    assert engine.record_outcome("sess-hook", outcome, ref="sess-hook")["applied"] == 1
    assert engine.get_item(rule["id"])["helpful"][0]["ref"] == "sess-hook"


def test_build_system_prompt_injects_the_block_behind_its_setting(store, monkeypatch):
    from src import agent_loop
    _rule()
    messages = [{"role": "user", "content": "please run the project tests"}]

    def _run():
        agent_loop._cached_base_prompt = None
        agent_loop._cached_base_prompt_key = None
        return agent_loop._build_system_prompt(
            list(messages), "test-model", None, None,
            owner="luis", relevant_tools={"read_file"}, session_id="sess-prompt")[0]

    on = {"agent_learned_memory": True, "agent_learned_memory_chars": 1800}
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: on.get(key, default))
    built = _run()
    injected = [m for m in built if "Always run the project tests" in str(m.get("content"))]
    assert injected and injected[0]["role"] == "user"
    assert injected[0].get("metadata", {}).get("trusted") is False
    assert engine.peek_injected("sess-prompt")

    engine.clear_injected()
    off = {"agent_learned_memory": False, "agent_learned_memory_chars": 1800}
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: off.get(key, default))
    built = _run()
    assert not [m for m in built if "Always run the project tests" in str(m.get("content"))]
    assert engine.peek_injected("sess-prompt") == []


def test_a_broken_store_costs_the_block_not_the_prompt(store, monkeypatch):
    from src import agent_loop
    _rule()
    monkeypatch.setattr(engine, "pack_detail",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("banana")))
    agent_loop._cached_base_prompt = None
    agent_loop._cached_base_prompt_key = None
    built = agent_loop._build_system_prompt(
        [{"role": "user", "content": "hello"}], "test-model", None, None,
        owner="luis", relevant_tools={"read_file"}, session_id="sess-x")[0]
    assert built and any(m.get("role") == "user" for m in built)


# ── the HTTP API ──────────────────────────────────────────────────────


@pytest.fixture()
def client(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.middleware import require_admin
    from routes import memory_engine_routes

    monkeypatch.setattr(memory_engine_routes, "effective_user", lambda request: "luis")
    app = FastAPI()
    app.include_router(memory_engine_routes.setup_memory_engine_routes())
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def test_api_creates_reads_votes_and_deletes(client, store):
    created = client.post("/api/memory-engine/items",
                          json={"text": "Always run the project tests",
                                "level": "procedural", "category": "testing"})
    assert created.status_code == 200
    item = created.json()["item"]
    # A human wrote it, so it lands at the top trust class.
    assert item["trust_class"] == "human_explicit" and item["trust"] == 0.85
    assert item["effective_score"] == pytest.approx(0.85)
    assert item["harmful_ratio"] == 0.0 and item["id8"] == item["id"][:8]

    listed = client.get("/api/memory-engine/items").json()
    assert [i["id"] for i in listed["items"]] == [item["id"]]
    assert listed["stats"]["active"] == 1 and listed["stats"]["semantic_lane"] is False

    voted = client.post(f"/api/memory-engine/items/{item['id']}/feedback",
                        json={"kind": "harmful", "reason": "wrong for this repo"})
    assert voted.status_code == 200
    assert voted.json()["item"]["effective_score"] < 0
    assert voted.json()["item"]["harmful_ratio"] == 1.0

    assert client.delete(f"/api/memory-engine/items/{item['id']}").json()["deleted"] is True
    assert client.get("/api/memory-engine/items").json()["items"] == []


def test_api_filters_and_reports_bad_input(client, store):
    client.post("/api/memory-engine/items", json={"text": "A rule", "level": "procedural"})
    client.post("/api/memory-engine/items",
                json={"text": "A fact", "level": "semantic", "project": "/repo"})
    assert len(client.get("/api/memory-engine/items?level=semantic").json()["items"]) == 1
    assert len(client.get("/api/memory-engine/items?project=/repo").json()["items"]) == 1
    assert len(client.get("/api/memory-engine/items?status=deprecated").json()["items"]) == 0

    assert client.post("/api/memory-engine/items", json={"text": "  "}).status_code == 400
    assert client.post("/api/memory-engine/items",
                       json={"text": "x", "level": "eidetic"}).status_code == 400
    assert client.post("/api/memory-engine/items/nope/feedback",
                       json={"kind": "helpful"}).status_code == 404
    assert client.post("/api/memory-engine/items/nope/feedback",
                       json={"kind": "sideways"}).status_code == 400
    assert client.delete("/api/memory-engine/items/nope").status_code == 404


def test_api_curate_returns_the_report(client, store):
    made = client.post("/api/memory-engine/items",
                       json={"text": "Rewrite whole files with bash heredocs"}).json()["item"]
    for i in range(3):
        client.post(f"/api/memory-engine/items/{made['id']}/feedback",
                    json={"kind": "harmful", "reason": f"broke run {i}"})
    report = client.post("/api/memory-engine/curate", json={}).json()["report"]
    assert report["inverted"] == 1
    assert set(report) == {"deduped", "conflicts", "inverted", "promoted",
                           "demoted", "pruned", "total_active"}
    assert client.get("/api/memory-engine/items").json()["items"][0]["status"] == "anti_pattern"


def test_api_pack_is_the_exact_block_the_model_would_see(client, store):
    client.post("/api/memory-engine/items", json={"text": "Always run the project tests"})
    client.post("/api/memory-engine/items",
                json={"text": "This repo pins numpy to 1.26", "project": "/repo"})
    body = client.get("/api/memory-engine/pack?query=tests").json()
    assert body["pack"] == engine.pack("luis", None, "tests", body["budget"])
    assert engine.PACK_RULES_HEADER in body["pack"]
    assert body["ids"] and body["chars"] == len(body["pack"])
    assert body["enabled"] is True and body["degraded"] is False

    # An unscoped rule reaches every project; a project's own rule does not
    # leak into another one.
    other = client.get("/api/memory-engine/pack?query=numpy&project=/elsewhere").json()
    assert "Always run the project tests" in other["pack"]
    assert "numpy" not in other["pack"]
    same = client.get("/api/memory-engine/pack?query=numpy&project=/repo").json()
    assert "numpy" in same["pack"]


def test_api_endpoints_are_admin_only(store, monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from core.middleware import require_admin
    from routes import memory_engine_routes

    app = FastAPI()
    app.include_router(memory_engine_routes.setup_memory_engine_routes())

    def _deny():
        raise HTTPException(403, "Admin only")

    app.dependency_overrides[require_admin] = _deny
    client = TestClient(app)
    assert client.get("/api/memory-engine/items").status_code == 403
    assert client.post("/api/memory-engine/items", json={"text": "x"}).status_code == 403
    assert client.post("/api/memory-engine/curate", json={}).status_code == 403
    assert client.get("/api/memory-engine/pack").status_code == 403


# ── the MCP tool for an outside coordinator ───────────────────────────


def test_mcp_memory_pack_renders_the_block(monkeypatch):
    from tests.test_dispatch import _load_workers_server
    ws = _load_workers_server(monkeypatch)
    assert "memory_pack" in [t.name for t in ws.TOOLS]

    text = ws.render_pack({"pack": "## Learned rules\n- [ab12cd34] Run the tests (proven)",
                           "chars": 47, "budget": 1800, "enabled": True, "degraded": False})
    assert "learned memory · 47 of 1800 chars" in text
    assert "- [ab12cd34] Run the tests (proven)" in text

    assert "nothing learned yet" in ws.render_pack({"pack": "", "enabled": True})
    assert "injection is OFF" in ws.render_pack({"pack": "x", "enabled": False})
    assert "semantic lane unavailable" in ws.render_pack(
        {"pack": "x", "enabled": True, "degraded": True, "chars": 1, "budget": 10})
