"""Lot S — hybrid skill selector (`src/skills_runtime/selector.py`).

Covers: the semantic lane ranking a lexically-orthogonal paraphrase above a
lexically-matching but semantically off-topic distractor; a graceful
lexical-only fallback when the embedder is unavailable; en/es trigger
extraction; the outcome prior changing rank order; the combined threshold
filtering an off-topic query to nothing; `min_confidence`/`max_items`
passed through; the on-disk vector cache being reused for an unchanged
skill and invalidated the moment its content changes; `mode="lexical"`
being byte-identical to the old `SkillsManager.get_relevant_skills`; and
the three `routes/skill_selector_routes.py` endpoints.

A fake embedder (deterministic "topic bucket" bag-of-words, no real model,
no network) keeps every case fast and offline — see `_FakeEmbedder` below.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

# Stub heavy deps so importing the skills manager doesn't pull DB / FastAPI,
# matching the other skills tests in this repo (test_skills_tag_token_match.py
# etc).
for _mod in ("sqlalchemy", "sqlalchemy.orm", "sqlalchemy.ext", "sqlalchemy.ext.declarative"):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = MagicMock()

from services.memory.skills import SkillsManager  # noqa: E402
from src.skills_runtime import selector  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_selector():
    """Every test starts from a clean embedder/cache/latch — a fake
    embedder or a "semantic unavailable" latch from one test must never
    leak into the next."""
    selector.reset_selector_state()
    yield
    selector.reset_selector_state()


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    """Pin the selector settings to the documented defaults for every test
    unless a test overrides `src.settings.get_setting` itself."""
    import src.settings as settings_mod

    values = {
        "skill_selector_mode": "hybrid",
        "skill_selector_threshold": selector.DEFAULT_THRESHOLD,
        "skill_selector_weights": dict(selector.DEFAULT_WEIGHTS),
    }

    def fake_get_setting(key, default=None):
        return values.get(key, default)

    monkeypatch.setattr(settings_mod, "get_setting", fake_get_setting)
    # routes/skill_selector_routes.py does `from src.settings import
    # get_setting` at module top (same shape as routes/tool_arg_policy_routes.py),
    # so it holds its own bound reference the patch above never reaches —
    # patch that module's copy too whenever it's already importable.
    try:
        import routes.skill_selector_routes as routes_mod
        monkeypatch.setattr(routes_mod, "get_setting", fake_get_setting)
    except Exception:
        pass
    return values


# ── fake embedder: deterministic "topic bucket" bag-of-words ──────────────

#: dim0 = "cancellation" topic, dim1 = "flight-schedule" topic, dims 2..10 =
#: an md5-hashed bag-of-words bucket for every other word (so two texts that
#: share no words land near-orthogonal instead of both piling into one
#: shared "everything else" bucket), dim 11 = tiny noise so an all-zero
#: vector never divides by zero. A word hits dim0/dim1 by SUBSTRING (so
#: "cancellation" hits the same bucket as "cancel", modelling a real
#: embedder generalizing past exact tokens) — this is what lets a skill
#: share a topic with the query while sharing zero literal tokens with it.
_TOPIC0_SUBSTRINGS = ("cancel", "abort", "terminat")
_TOPIC1_SUBSTRINGS = ("schedule", "timetable", "departure", "gate")
_DIM = 67
_BOW_BUCKETS = _DIM - 3  # dims 0,1 = topics, last dim = noise only — plenty
# of buckets so two genuinely unrelated short phrases rarely hash-collide.


def _word_bucket(word: str) -> int:
    import hashlib
    return 2 + (int(hashlib.md5(word.encode("utf-8")).hexdigest(), 16) % _BOW_BUCKETS)


#: A topic hit counts far more than one incidental bag-of-words bucket —
#: a real sentence embedding is dominated by its overall meaning, not
#: diluted to nothing by every filler word around it.
_TOPIC_WEIGHT = 8.0


def _fake_vector(text: str) -> np.ndarray:
    import re
    v = np.zeros(_DIM, dtype="float32")
    for w in re.findall(r"[a-z0-9']+", (text or "").lower()):
        if any(s in w for s in _TOPIC0_SUBSTRINGS):
            v[0] += _TOPIC_WEIGHT
        elif any(s in w for s in _TOPIC1_SUBSTRINGS):
            v[1] += _TOPIC_WEIGHT
        else:
            v[_word_bucket(w)] += 1.0
    v[-1] += 0.01
    norm = np.linalg.norm(v)
    return v / norm if norm else v


class _FakeEmbedder:
    """Stand-in for `src.embeddings.FastEmbedClient` — same `.encode()`
    surface, no ONNX, no download. `calls` counts embedded texts so cache
    tests can assert nothing was re-embedded."""

    def __init__(self):
        self.calls = 0

    def encode(self, texts, normalize_embeddings=True):
        self.calls += len(texts)
        return np.array([_fake_vector(t) for t in texts], dtype="float32")


def _patch_embedder(monkeypatch, embedder=None):
    embedder = embedder or _FakeEmbedder()
    import src.embedding_lanes as embedding_lanes

    monkeypatch.setattr(embedding_lanes, "_build_fastembed_client", lambda: embedder)
    return embedder


def _patch_data_dir(monkeypatch, tmp_path):
    import src.constants as constants

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(constants, "DATA_DIR", str(data_dir), raising=False)
    return str(data_dir)


def _skill(name, description, **extra):
    base = {
        "id": name, "name": name, "description": description,
        "when_to_use": extra.pop("when_to_use", ""),
        "tags": extra.pop("tags", []),
        "procedure": extra.pop("procedure", []),
        "status": extra.pop("status", "published"),
        "confidence": extra.pop("confidence", 0.9),
        "owner": extra.pop("owner", None),
        "uses": extra.pop("uses", 0),
    }
    base.update(extra)
    return base


# ── 1. semantic lane beats a lexical-only distractor ──────────────────────

def test_semantic_lane_ranks_paraphrase_above_lexical_distractor(monkeypatch, tmp_path):
    _patch_embedder(monkeypatch)
    _patch_data_dir(monkeypatch, tmp_path)
    sm = SkillsManager(str(tmp_path / "data"))

    query = "abort my reservation now issue refund"

    # Paraphrase: shares ZERO literal tokens with the query but hits the
    # same "cancellation" topic bucket via "termination" (substring match
    # on "terminat"), same as a real embedder generalizing past exact words.
    paraphrase = _skill(
        "trip-termination",
        "Process a passenger's termination of a purchased itinerary and "
        "grant monetary compensation.",
    )
    # Lexical distractor: shares the literal token "reservation" with the
    # query (real Jaccard overlap) but is about an unrelated topic (front
    # desk note-taking), so it should score near zero semantically.
    distractor = _skill(
        "reservation-notes",
        "Add internal notes to a reservation profile for the front desk team.",
    )

    skills = [paraphrase, distractor]

    # Plain lexical scoring never surfaces the paraphrase at all — it shares
    # no tokens with the query.
    lexical_only = sm.get_relevant_skills(query, skills=skills, threshold=0.05, max_items=5)
    assert "trip-termination" not in {s["name"] for s in lexical_only}

    # The hybrid selector, with the semantic lane on, ranks the paraphrase
    # FIRST despite that zero lexical overlap.
    out = selector.select(sm, None, query, skills=skills, threshold=0.0, max_items=5)
    assert [s["name"] for s in out][0] == "trip-termination"
    names = [s["name"] for s in out]
    assert names.index("trip-termination") < names.index("reservation-notes")
    assert out[0]["_selector"]["semantic"] > 0.6


# ── 2. lexical fallback when the embedder is unavailable ──────────────────

def test_lexical_fallback_when_embedder_missing(monkeypatch, tmp_path):
    import src.embedding_lanes as embedding_lanes

    def _boom():
        raise RuntimeError("fastembed not installed")

    monkeypatch.setattr(embedding_lanes, "_build_fastembed_client", _boom)
    _patch_data_dir(monkeypatch, tmp_path)
    sm = SkillsManager(str(tmp_path / "data"))

    query = "help me rebase my git branch"
    skills = [_skill("git-helper", "version control stuff", tags=["git"])]

    out = selector.select(sm, None, query, skills=skills, threshold=0.0, max_items=5)
    assert any(s["name"] == "git-helper" for s in out)
    assert out[0]["_selector"]["semantic"] == 0.0

    # The failure is latched: a second call does not try to rebuild it.
    calls = {"n": 0}
    real_boom = embedding_lanes._build_fastembed_client

    def _boom_counting():
        calls["n"] += 1
        return real_boom()

    monkeypatch.setattr(embedding_lanes, "_build_fastembed_client", _boom_counting)
    selector.select(sm, None, query, skills=skills, threshold=0.0, max_items=5)
    assert calls["n"] == 0, "embedder must not be retried once it has failed this process"


# ── 3. trigger extraction, en/es ───────────────────────────────────────────

def test_extract_trigger_english():
    text = "A helper for release notes. Use when the user asks to draft release notes."
    assert selector.extract_trigger(text) == "Use when the user asks to draft release notes."


def test_extract_trigger_spanish():
    text = "Ayuda con reservas. Cuando el usuario pida cancelar una reserva."
    assert selector.extract_trigger(text) == "Cuando el usuario pida cancelar una reserva."


def test_extract_trigger_none_found():
    assert selector.extract_trigger("Just a plain description with no trigger clause.") == ""
    assert selector.extract_trigger("") == ""


# ── 4. outcome prior changes rank order ────────────────────────────────────

def test_outcome_prior_changes_order(monkeypatch, tmp_path):
    import src.embedding_lanes as embedding_lanes
    monkeypatch.setattr(embedding_lanes, "_build_fastembed_client",
                        lambda: (_ for _ in ()).throw(RuntimeError("off")))
    _patch_data_dir(monkeypatch, tmp_path)
    sm = SkillsManager(str(tmp_path / "data"))

    # Identical text -> identical lexical/trigger scores; only the outcome
    # prior can tell them apart.
    query = "send a follow up email to the client"
    skill_a = _skill("email-followup-a", "Send a follow up email to the client")
    skill_b = _skill("email-followup-b", "Send a follow up email to the client")

    baseline = selector.select(sm, "alice", query, skills=[skill_a, skill_b],
                               threshold=0.0, max_items=5)
    assert baseline[0]["_selector"]["score"] == pytest.approx(baseline[1]["_selector"]["score"])

    for _ in range(3):
        sm.record_outcome("email-followup-b", owner="alice", positive=True)
    sm.record_outcome("email-followup-a", owner="alice", positive=False)

    out = selector.select(sm, "alice", query, skills=[skill_a, skill_b],
                          threshold=0.0, max_items=5)
    assert [s["name"] for s in out][0] == "email-followup-b"
    assert out[0]["_selector"]["prior"] > out[1]["_selector"]["prior"]


# ── 5. threshold filters an off-topic query to nothing ─────────────────────

def test_threshold_filters_off_topic_query(monkeypatch, tmp_path):
    _patch_embedder(monkeypatch)
    _patch_data_dir(monkeypatch, tmp_path)
    sm = SkillsManager(str(tmp_path / "data"))

    skills = [_skill("git-helper", "Help rebase and squash git commits", tags=["git"])]
    out = selector.select(sm, None, "what is the weather like on mars today",
                          skills=skills, max_items=5)
    assert out == []


# ── 6. min_confidence and max_items pass through ───────────────────────────

def test_min_confidence_and_max_items(monkeypatch, tmp_path):
    _patch_embedder(monkeypatch)
    _patch_data_dir(monkeypatch, tmp_path)
    sm = SkillsManager(str(tmp_path / "data"))

    skills = [
        _skill(f"notes-{i}", "Take meeting notes and summarize action items",
               status="draft", confidence=0.1)
        for i in range(5)
    ]
    query = "take meeting notes and summarize action items"

    # High min_confidence knocks out every low-confidence draft.
    out = selector.select(sm, None, query, skills=skills, threshold=0.0,
                          max_items=10, min_confidence=0.5)
    assert out == []

    out = selector.select(sm, None, query, skills=skills, threshold=0.0,
                          max_items=2, min_confidence=0.0)
    assert len(out) == 2


# ── 7. vector cache: reused when unchanged, invalidated on content change ──

def test_vector_cache_reused_then_invalidated_on_change(monkeypatch, tmp_path):
    embedder = _patch_embedder(monkeypatch)
    _patch_data_dir(monkeypatch, tmp_path)
    sm = SkillsManager(str(tmp_path / "data"))

    query = "cancel my flight booking"
    skill = _skill("cancel-flight", "Cancel a flight and refund the fare")

    selector.select(sm, "bob", query, skills=[skill], threshold=0.0, max_items=5)
    after_first = embedder.calls  # 1 skill + 1 query embedded

    selector.select(sm, "bob", query, skills=[skill], threshold=0.0, max_items=5)
    after_second = embedder.calls
    # Only the query gets re-embedded; the unchanged skill is served from cache.
    assert after_second - after_first == 1

    # A fresh in-process cache (simulating a restart) still finds the skill's
    # vector on disk instead of re-embedding it.
    selector.reset_selector_state()
    _patch_embedder(monkeypatch, embedder)
    before_restart = embedder.calls
    selector.select(sm, "bob", query, skills=[skill], threshold=0.0, max_items=5)
    assert embedder.calls - before_restart == 1  # query only, skill hit the disk cache

    # Now change the skill's content -> its cached vector must be invalidated.
    skill["description"] = "Cancel a flight and grant monetary compensation for the delay"
    before_change = embedder.calls
    selector.select(sm, "bob", query, skills=[skill], threshold=0.0, max_items=5)
    assert embedder.calls - before_change == 2  # skill re-embedded + query


# ── 8. mode="lexical" is byte-identical to the old function ───────────────

def test_lexical_mode_matches_old_function_exactly(monkeypatch, tmp_path):
    _patch_embedder(monkeypatch)  # even with a healthy embedder available...
    _patch_data_dir(monkeypatch, tmp_path)
    sm = SkillsManager(str(tmp_path / "data"))

    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: "lexical" if key == "skill_selector_mode" else default)

    skills = [
        _skill("git-helper", "version control stuff", tags=["git"]),
        _skill("ml-helper", "machine learning helper", tags=["ai"]),
    ]
    query = "help me with git rebase"

    expected = sm.get_relevant_skills(query, skills=skills, threshold=0.25, max_items=3,
                                      min_confidence=0.0)
    actual = selector.select(sm, None, query, skills=skills, threshold=0.25, max_items=3,
                             min_confidence=0.0)
    assert actual == expected
    assert all("_selector" not in s for s in actual)


# ── 9. record_outcome_from_reaction ─────────────────────────────────────────

def test_record_outcome_from_reaction_positive_and_negative(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    sm = SkillsManager(str(tmp_path / "data"))
    sm.record_outcome("s1", owner="alice", positive=True)  # seed the sidecar row

    selector.record_outcome_from_reaction("alice", ["s1"], "thanks, that fixed it")
    entry = sm.usage_entry("s1", owner="alice")
    assert entry.get("positive") == 2

    selector.record_outcome_from_reaction("alice", ["s1"], "no, that's wrong")
    entry = sm.usage_entry("s1", owner="alice")
    assert entry.get("negative") == 1

    # Neutral reactions record nothing.
    before = dict(sm.usage_entry("s1", owner="alice"))
    selector.record_outcome_from_reaction("alice", ["s1"], "ok, what about tuesday instead")
    assert sm.usage_entry("s1", owner="alice") == before


# ── 10. routes ───────────────────────────────────────────────────────────────

def _make_client(monkeypatch, owner=None, admin=False):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.skill_selector_routes as routes_mod

    monkeypatch.setattr(routes_mod, "get_current_user", lambda request: owner)
    if admin:
        monkeypatch.setattr(routes_mod, "require_admin", lambda request: None)
    else:
        def _deny(request):
            from fastapi import HTTPException
            raise HTTPException(403, "Admin only")
        monkeypatch.setattr(routes_mod, "require_admin", _deny)

    app = FastAPI()
    app.include_router(routes_mod.setup_skill_selector_routes())
    return TestClient(app)


def test_explain_route_returns_ranked_candidates(monkeypatch, tmp_path):
    _patch_embedder(monkeypatch)
    data_dir = _patch_data_dir(monkeypatch, tmp_path)
    sm = SkillsManager(data_dir)
    sm.add_skill(name="git-helper", description="version control stuff",
                tags=["git"], status="published", source="user", owner="alice")

    client = _make_client(monkeypatch, owner="alice")
    resp = client.post("/api/skills/selector/explain", json={"query": "help me with git rebase"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidates"]
    assert "_selector" in body["candidates"][0]


def test_get_selector_settings_route(monkeypatch, tmp_path):
    _patch_data_dir(monkeypatch, tmp_path)
    client = _make_client(monkeypatch)
    resp = client.get("/api/skills/selector")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "hybrid"
    assert body["threshold"] == pytest.approx(selector.DEFAULT_THRESHOLD)
    assert body["weights"] == selector.DEFAULT_WEIGHTS


def test_put_selector_settings_requires_admin(monkeypatch, tmp_path):
    _patch_data_dir(monkeypatch, tmp_path)
    client = _make_client(monkeypatch, admin=False)
    resp = client.put("/api/skills/selector", json={"mode": "lexical"})
    assert resp.status_code == 403


def test_put_selector_settings_validates_and_applies(monkeypatch, tmp_path):
    _patch_data_dir(monkeypatch, tmp_path)

    import routes.skill_selector_routes as routes_mod
    saved = {}
    monkeypatch.setattr(routes_mod, "update_settings",
                        lambda patch, **_kw: saved.update(patch) or {"settings": saved, "revision": 1})
    monkeypatch.setattr(routes_mod, "get_setting", lambda key, default=None: saved.get(key, default))

    client = _make_client(monkeypatch, admin=True)

    bad = client.put("/api/skills/selector", json={"mode": "bogus"})
    assert bad.status_code == 400

    bad_weights = client.put("/api/skills/selector", json={"weights": {"semantic": -1, "lexical": 0.3, "trigger": 0.15}})
    assert bad_weights.status_code == 400

    good = client.put("/api/skills/selector", json={
        "mode": "lexical", "threshold": 0.3,
        "weights": {"semantic": 0.5, "lexical": 0.3, "trigger": 0.2},
    })
    assert good.status_code == 200
    body = good.json()
    assert body["mode"] == "lexical"
    assert body["threshold"] == 0.3
    assert body["weights"] == {"semantic": 0.5, "lexical": 0.3, "trigger": 0.2}
