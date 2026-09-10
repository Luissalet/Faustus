"""src/branching_futures/narrative_canon.py — canon vs. discarded alternative
for creative writing (WRITE-02/WRITE-04, QA-47).
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import routes.branching_futures_routes as branching_routes
from src.branching_futures.narrative_canon import (
    ALT_STATUS_DISCARDED,
    ALT_STATUS_DRAFT,
    ALT_STATUS_PROMOTED,
    alternative_status,
    canon_state,
    discarded_alternatives,
)
from src.branching_futures.service import BranchingService
from src.durable_feature_store import DurableFeatureStore


@pytest.fixture
def novel(tmp_path):
    return BranchingService(DurableFeatureStore(str(tmp_path / "novel.db")))


def _alternate_ending_future(novel, project_id="the-novel"):
    return novel.create(owner="author", project_id=project_id, request={
        "title": "Chapter 12 — the ending",
        "intent": "Decide how the confrontation in chapter 12 resolves",
        "mode": "simulate",
        "strategies": [
            {"id": "original", "title": "Elena forgives her brother"},
            {"id": "alt_death", "title": "Elena's brother dies in the fire"},
        ],
    })


def _submit(novel, future, branch_id, summary, quality=1):
    # The default criterion ("quality") is required for a branch to be
    # eligible in evaluate()/select() — omitting it would fail every
    # submission for a reason unrelated to canon/discard, so every submitted
    # result here carries a score for it.
    novel.start_branch(owner="author", future_id=future["id"], branch_id=branch_id)
    return novel.submit_result(owner="author", future_id=future["id"], branch_id=branch_id, result={
        "status": "completed", "summary": summary, "scores": {"quality": quality},
    })


class TestQA47DiscardedAlternateEndingNeverBecomesCanon:
    """Reproduces the QA-47 stimulus literally: create an alternate ending,
    discard it, write the next chapter — the discarded ending must not come
    back as confirmed fact."""

    def test_selecting_the_original_discards_the_alternative_and_keeps_canon(self, novel):
        future = _alternate_ending_future(novel)
        original, alt = future["branches"]
        assert (original["strategy"]["id"], alt["strategy"]["id"]) == ("original", "alt_death")

        _submit(novel, future, original["id"], "Elena forgives her brother by the riverbank.")
        _submit(novel, future, alt["id"], "Elena's brother perishes in the warehouse fire.")
        novel.evaluate(owner="author", future_id=future["id"])
        novel.select(owner="author", future_id=future["id"], branch_id=original["id"],
                    rationale="Keeps the reconciliation arc the rest of the book sets up.")

        # Canon original conservado.
        canon = canon_state(owner="author", project_id="the-novel", svc=novel)
        assert len(canon["chapters"]) == 1
        chapter = canon["chapters"][0]
        assert chapter["branch_id"] == original["id"]
        assert "forgives her brother" in chapter["summary"]

        # La alternativa descartada no vuelve como hecho confirmado: su texto
        # no aparece en NINGUN lugar de canon_state.
        canon_text = repr(canon)
        assert "perishes" not in canon_text
        assert "fire" not in canon_text
        assert alt["id"] not in canon_text

        # Y está explícitamente marcada, no simplemente ausente.
        discarded = discarded_alternatives(owner="author", project_id="the-novel", svc=novel)
        assert len(discarded) == 1
        assert discarded[0]["branch_id"] == alt["id"]
        assert discarded[0]["status"] == ALT_STATUS_DISCARDED
        assert "perishes" in discarded[0]["summary"]

        # Escribir el capitulo siguiente: una nueva exploracion (nuevo
        # future_id) para el mismo proyecto no ve la alternativa descartada
        # en ningun resumen que pudiera alimentar su contexto.
        next_chapter = novel.create(owner="author", project_id="the-novel", request={
            "title": "Chapter 13", "intent": "Continue from the reconciliation",
            "mode": "simulate",
            "strategies": [{"id": "a", "title": "Scene A"}, {"id": "b", "title": "Scene B"}],
        })
        assert next_chapter["id"] != future["id"]
        # Canon is still just the one confirmed chapter — chapter 13 has not
        # been confirmed yet (no selection made for it).
        canon_after = canon_state(owner="author", project_id="the-novel", svc=novel)
        assert len(canon_after["chapters"]) == 1
        assert canon_after["chapters"][0]["branch_id"] == original["id"]

    def test_abandoning_the_future_without_selecting_also_discards_both_branches(self, novel):
        future = _alternate_ending_future(novel)
        original, alt = future["branches"]
        _submit(novel, future, original["id"], "Original ending text.")
        _submit(novel, future, alt["id"], "Alternative ending text.")

        novel.cancel(owner="author", future_id=future["id"])

        canon = canon_state(owner="author", project_id="the-novel", svc=novel)
        assert canon["chapters"] == []

        discarded = discarded_alternatives(owner="author", project_id="the-novel", svc=novel)
        assert {row["branch_id"] for row in discarded} == {original["id"], alt["id"]}
        assert all(row["status"] == ALT_STATUS_DISCARDED for row in discarded)

    def test_other_projects_and_other_owners_never_see_this_canon(self, novel):
        future = _alternate_ending_future(novel, project_id="the-novel")
        original, alt = future["branches"]
        _submit(novel, future, original["id"], "Original ending text.")
        _submit(novel, future, alt["id"], "Alternative ending text.")
        novel.evaluate(owner="author", future_id=future["id"])
        novel.select(owner="author", future_id=future["id"], branch_id=original["id"], rationale="keep it")

        assert canon_state(owner="author", project_id="another-novel", svc=novel)["chapters"] == []
        assert canon_state(owner="ghostwriter", project_id="the-novel", svc=novel)["chapters"] == []


class TestAlternativeStatus:
    def test_undecided_branch_is_a_draft(self, novel):
        future = _alternate_ending_future(novel)
        original, _alt = future["branches"]
        assert alternative_status(original, future) == ALT_STATUS_DRAFT

    def test_selected_branch_is_promoted(self, novel):
        future = _alternate_ending_future(novel)
        original, alt = future["branches"]
        _submit(novel, future, original["id"], "Original.")
        _submit(novel, future, alt["id"], "Alt.")
        novel.evaluate(owner="author", future_id=future["id"])
        novel.select(owner="author", future_id=future["id"], branch_id=original["id"], rationale="r")
        full = novel.future(owner="author", future_id=future["id"])
        promoted = next(b for b in full["branches"] if b["id"] == original["id"])
        loser = next(b for b in full["branches"] if b["id"] == alt["id"])
        assert alternative_status(promoted, full) == ALT_STATUS_PROMOTED
        assert alternative_status(loser, full) == ALT_STATUS_DISCARDED


class TestCanonRoutesOverHttp:
    """routes/branching_futures_routes.py's additive GET endpoints, exercised
    against the real FastAPI app object (no mocked HTTP layer) — same harness
    tests/test_capability_system_routes.py already uses for this router."""

    def _client(self, service, monkeypatch):
        monkeypatch.setattr(branching_routes, "require_admin", lambda _request: None)
        monkeypatch.setattr(branching_routes, "_owner", lambda _request: "author")
        monkeypatch.setattr(branching_routes, "branching_service", lambda: service)
        app = FastAPI()
        app.include_router(branching_routes.setup_branching_futures_routes())
        return TestClient(app)

    def test_canon_and_alternatives_endpoints_reflect_a_selection(self, novel, monkeypatch):
        client = self._client(novel, monkeypatch)
        future = _alternate_ending_future(novel)
        original, alt = future["branches"]
        _submit(novel, future, original["id"], "Original ending text.")
        _submit(novel, future, alt["id"], "Alternative ending text.")
        novel.evaluate(owner="author", future_id=future["id"])
        novel.select(owner="author", future_id=future["id"], branch_id=original["id"], rationale="r")

        canon = client.get("/api/futures/canon/state", params={"project_id": "the-novel"})
        assert canon.status_code == 200 and canon.json()["ok"] is True
        chapters = canon.json()["canon"]["chapters"]
        assert len(chapters) == 1 and chapters[0]["branch_id"] == original["id"]
        assert "Alternative ending text" not in str(canon.json())

        alternatives = client.get("/api/futures/canon/alternatives", params={"project_id": "the-novel"})
        assert alternatives.status_code == 200 and alternatives.json()["ok"] is True
        rows = alternatives.json()["alternatives"]
        assert [row["branch_id"] for row in rows] == [alt["id"]]
        assert rows[0]["status"] == ALT_STATUS_DISCARDED

    def test_project_id_is_required(self, novel, monkeypatch):
        client = self._client(novel, monkeypatch)
        assert client.get("/api/futures/canon/state").status_code == 400
        assert client.get("/api/futures/canon/alternatives").status_code == 400
