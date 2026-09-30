"""Elo ratings from comparison votes: math, topics, storage shapes, routes,
and the router's opt-in read."""
from __future__ import annotations

import json
import os
import sys
import types
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, Comparison
from src import compare_elo as elo
from src import model_router, prompt_topics


def _vote(a, b, winner, prompt="hola", mode=None):
    return {"keys": [a, b], "labels": {a: a, b: b}, "winner": winner, "prompt": prompt, "mode": mode}


# ── math ────────────────────────────────────────────────────────────────────

def test_expected_and_first_vote_between_equals():
    assert elo.expected(1000, 1000) == pytest.approx(0.5)
    res = elo.compute([_vote("a", "b", "a")])
    rows = {r["key"]: r for r in res["overall"]}
    assert rows["a"]["rating"] == pytest.approx(1016.0)   # K * (1 - 0.5)
    assert rows["b"]["rating"] == pytest.approx(984.0)
    assert (rows["a"]["wins"], rows["b"]["losses"], rows["a"]["games"]) == (1, 1, 1)
    assert res["k"] == 32 and res["start"] == 1000


def test_tie_between_equals_changes_nothing_and_between_unequals_moves_toward_each_other():
    res = elo.compute([_vote("a", "b", "tie")])
    assert all(r["rating"] == 1000.0 and r["ties"] == 1 for r in res["overall"])
    res = elo.compute([_vote("a", "b", "a"), _vote("a", "b", "tie")])
    rows = {r["key"]: r["rating"] for r in res["overall"]}
    assert rows["a"] < 1016.0 and rows["b"] > 984.0


def test_upset_moves_more_than_expected_win():
    base = [_vote("a", "b", "a")] * 5
    expected_win = elo.compute(base + [_vote("a", "b", "a")])
    upset = elo.compute(base + [_vote("a", "b", "b")])
    a_before = elo.compute(base)["overall"][0]["rating"]
    gain = {r["key"]: r["rating"] for r in expected_win["overall"]}["a"] - a_before
    loss = a_before - {r["key"]: r["rating"] for r in upset["overall"]}["a"]
    assert loss > gain > 0


def test_multi_model_vote_is_pairwise_from_pre_vote_ratings():
    vote = {"keys": ["a", "b", "c"], "labels": {}, "winner": "b", "prompt": "x"}
    res = elo.compute([vote])
    rows = {r["key"]: r for r in res["overall"]}
    assert rows["b"]["rating"] == pytest.approx(1032.0)    # beat two equals
    assert rows["a"]["rating"] == pytest.approx(984.0) and rows["c"]["rating"] == pytest.approx(984.0)
    # the order of the models inside a vote does not matter
    other = elo.compute([{"keys": ["c", "b", "a"], "labels": {}, "winner": "b", "prompt": "x"}])
    assert {r["key"]: r["rating"] for r in other["overall"]} == {k: v["rating"] for k, v in rows.items()}


def test_total_rating_is_conserved():
    votes = [_vote("a", "b", "a"), _vote("b", "c", "tie"), _vote("a", "c", "c"),
             {"keys": ["a", "b", "c"], "labels": {}, "winner": "a", "prompt": "q"}]
    res = elo.compute(votes)
    assert sum(r["rating"] for r in res["overall"]) == pytest.approx(3000.0, abs=0.5)


def test_invalid_votes_are_ignored():
    votes = [_vote("a", "b", "nobody"), {"keys": ["a"], "winner": "a", "prompt": ""},
             {"keys": ["a", "a"], "winner": "a", "prompt": ""}, _vote("a", "b", "a")]
    res = elo.compute(votes)
    assert res["votes"] == 1 and len(res["overall"]) == 2


def test_mode_filter():
    votes = [_vote("a", "b", "a", mode="chat"), _vote("a", "b", "b", mode="agent")]
    assert elo.compute(votes, mode="agent")["votes"] == 1
    assert {r["key"]: r["wins"] for r in elo.compute(votes, mode="agent")["overall"]}["b"] == 1
    assert elo.compute(votes)["votes"] == 2


# ── topics ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prompt, topic", [
    ("Escribe una función en Python que ordene una lista", "code"),
    ("Fix this bug: Traceback (most recent call last)", "code"),
    ("Calcula la integral de x^2", "math"),
    ("Traduce este párrafo al inglés", "translation"),
    ("Resume el siguiente texto en tres puntos", "summarization"),
    ("Redacta un correo formal para mi casero", "writing"),
    ("¿Cuál es la capital de Australia?", "knowledge"),
    ("hola qué tal", "general"),
    ("", "general"),
])
def test_prompt_topics(prompt, topic):
    assert prompt_topics.classify(prompt) == topic


def test_topic_classifier_is_deterministic_and_word_bounded():
    p = "Explain the maintain and retain and explain again"
    assert prompt_topics.classify(p) == prompt_topics.classify(p)
    assert prompt_topics.classify("a mail waits") == "general"     # 'ai' inside words must not fire code/AI


def test_per_topic_tables_split_votes():
    votes = [
        _vote("a", "b", "a", prompt="Escribe una función en Python"),
        _vote("a", "b", "b", prompt="Traduce esto al inglés"),
        _vote("a", "b", "b", prompt="Traduce otra frase al inglés"),
    ]
    res = elo.compute(votes)
    code = {r["key"]: r["rating"] for r in res["topics"]["code"]}
    tr = {r["key"]: r["rating"] for r in res["topics"]["translation"]}
    assert code["a"] > code["b"] and tr["b"] > tr["a"]
    assert res["topic_votes"] == {"code": 1, "translation": 2}


# ── stored rows ─────────────────────────────────────────────────────────────

def _row(**kw):
    base = dict(id="x", prompt="p", model_a="m1", model_b="m2", endpoint_a="", endpoint_b="",
                winner=None, blind_mapping=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_votes_from_comparisons_handles_every_stored_shape():
    rows = [
        _row(winner="a"),                                                        # /start + /vote
        _row(winner="m2"),                                                       # /record, 2 models, label
        _row(winner="tie"),
        _row(winner="m3", model_a="m1", model_b="m2",
             blind_mapping=json.dumps({"models": ["m1", "m2", "m3"]})),          # /record, N>2
        _row(winner="M1 · Ollama", blind_mapping=json.dumps(
            {"models": ["M1 · Ollama", "M2 · Ollama"], "model_ids": ["m1", "m2"], "mode": "agent"})),
        _row(winner=None),                                                       # never voted
        _row(winner="stranger"),                                                 # names nobody: skipped
    ]
    votes = elo.votes_from_comparisons(rows)
    assert [v["winner"] for v in votes] == ["m1", "m2", "tie", "m3", "m1"]
    assert votes[3]["keys"] == ["m1", "m2", "m3"]
    assert votes[4]["keys"] == ["m1", "m2"] and votes[4]["labels"]["m1"] == "M1 · Ollama"
    assert votes[4]["mode"] == "agent"


# ── routes ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Maker = sessionmaker(bind=engine)
    import core.database as cdb
    import routes.compare.compare_routes as rt
    monkeypatch.setattr(rt, "SessionLocal", Maker)
    monkeypatch.setattr(cdb, "SessionLocal", Maker)
    current = {"user": "alice"}
    monkeypatch.setattr(rt, "get_current_user", lambda request: current["user"])
    app = FastAPI()
    app.include_router(rt.setup_compare_routes(types.SimpleNamespace()))
    c = TestClient(app)
    c.current = current
    c.Maker = Maker
    return c


def _record(client, winner, models=("m1", "m2"), prompt="Escribe una función en Python", **extra):
    r = client.post("/api/compare/record", json={"prompt": prompt, "models": list(models), "winner": winner, **extra})
    assert r.status_code == 200
    return r.json()


def test_record_then_elo(client):
    _record(client, "m1", model_ids=["id1", "id2"], mode="chat")
    _record(client, "m1", model_ids=["id1", "id2"], mode="chat")
    _record(client, "tie", model_ids=["id1", "id2"], mode="agent")
    body = client.get("/api/compare/elo").json()
    assert body["votes"] == 3 and body["overall"][0]["key"] == "id1"
    assert body["overall"][0]["games"] == 3 and body["overall"][0]["label"] == "m1"
    assert "code" in body["topics"]
    chat_only = client.get("/api/compare/elo?mode=chat").json()
    assert chat_only["votes"] == 2


def test_elo_is_per_owner(client):
    _record(client, "m1")
    client.current["user"] = "bob"
    assert client.get("/api/compare/elo").json()["votes"] == 0
    _record(client, "m2")
    assert client.get("/api/compare/elo").json()["overall"][0]["key"] == "m2"
    client.current["user"] = "alice"
    assert client.get("/api/compare/elo").json()["overall"][0]["key"] == "m1"


def test_batch_sync_is_idempotent_and_validates(client):
    votes = [
        {"prompt": "Traduce esto al inglés", "models": ["m1", "m2"], "winner": "m2", "timestamp": 1700000000000, "mode": "chat"},
        {"prompt": "x", "models": ["only-one"], "winner": "only-one", "timestamp": 1},
        {"prompt": "y", "models": ["m1", "m2"], "winner": "", "timestamp": 2},
    ]
    first = client.post("/api/compare/record-batch", json={"votes": votes}).json()
    assert first == {"added": 1, "skipped": 2}
    again = client.post("/api/compare/record-batch", json={"votes": votes}).json()
    assert again == {"added": 0, "skipped": 3}
    assert client.get("/api/compare/elo").json()["votes"] == 1
    too_many = client.post("/api/compare/record-batch", json={"votes": votes[:1] * 501})
    assert too_many.status_code == 422


def test_deleting_a_comparison_changes_the_ratings(client):
    cid = _record(client, "m1")["id"]
    assert client.get("/api/compare/elo").json()["votes"] == 1
    assert client.delete(f"/api/compare/{cid}").status_code == 200
    assert client.get("/api/compare/elo").json()["votes"] == 0


# ── router: opt-in, read-only, bounded ──────────────────────────────────────

@pytest.fixture()
def router_env(tmp_path, monkeypatch):
    from src import constants as constants_mod, model_calibration
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(model_calibration, "get_manifest",
                        lambda key, *, data_dir=None: {"announced": {}, "tested": {}, "degraded": [], "updated_at": ""})
    monkeypatch.setattr(model_router, "local_speed", lambda m: None)
    return tmp_path


def test_router_setting_defaults_off_and_validates(router_env):
    assert model_router.RouterConfig().use_elo is False
    assert model_router.RouterConfig().to_dict()["use_elo"] is False
    cfg = model_router.update_router_config({"use_elo": True})
    assert cfg.use_elo is True
    with pytest.raises(ValueError, match="use_elo"):
        model_router.update_router_config({"use_elo": "yes"})


def test_router_never_reads_ratings_when_off(router_env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("ratings must not be read while use_elo is off")
    monkeypatch.setattr(elo, "router_ratings", boom)
    d = model_router.choose(model_router.Requirements(), installed=["a", "b"],
                            config=model_router.RouterConfig(), log=False)
    assert d.model in ("a", "b")


def test_router_uses_ratings_when_on_with_a_bounded_bonus(router_env, monkeypatch):
    monkeypatch.setattr(elo, "router_ratings", lambda owner, topic=None: {
        "b": {"rating": 1200.0, "games": 9, "scope": "overall"},
        "a": {"rating": 100.0, "games": 9, "scope": "overall"},
    })
    cfg = model_router.RouterConfig(use_elo=True)
    d = model_router.choose(model_router.Requirements(), installed=["a", "b", "c"], config=cfg, log=False)
    assert d.model == "b"
    scores = {s.model: s.score for s in d.alternatives}
    assert scores["b"] == pytest.approx(1.0) and scores["a"] == pytest.approx(-1.5)   # capped
    assert any("Elo 1200" in w for s in d.alternatives if s.model == "b" for w in s.why)
    # a model with no rating is untouched
    assert scores["c"] == pytest.approx(0.0)


def test_router_ratings_need_enough_games_and_prefer_topic(monkeypatch):
    votes = [_vote("a", "b", "a", prompt="Escribe una función en Python")] * 6
    monkeypatch.setattr(elo, "load_votes", lambda owner: votes)
    out = elo.router_ratings("alice", "code")
    assert out["a"]["scope"] == "topic:code" and out["a"]["games"] == 6
    few = elo.router_ratings("alice", "code", min_games=7)
    assert few == {}
    monkeypatch.setattr(elo, "load_votes", lambda owner: (_ for _ in ()).throw(RuntimeError("db down")))
    assert elo.router_ratings("alice") == {}
