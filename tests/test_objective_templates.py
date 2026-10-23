from fastapi import FastAPI, HTTPException
import httpx
import pytest

from src.objective_templates import catalog
from src.conversation_objective import ObjectiveStore
from routes import conversation_objective_routes as routes


def test_both_catalogs_produce_valid_editable_briefs_without_shared_mutation():
    for locale in ("en", "es"):
        value = catalog(locale)
        assert [item["id"] for item in value["templates"]] == ["opportunities", "offer", "editorial", "weekly"]
        for item in value["templates"]:
            brief = ObjectiveStore.validate(item["brief"])
            assert brief["context"] and brief["deliverable"] and not brief["refs"]
        value["templates"][0]["brief"]["refs"].append({"kind": "schedule", "id": "private"})
        assert catalog(locale)["templates"][0]["brief"]["refs"] == []
    with pytest.raises(ValueError): catalog("unsupported")


@pytest.mark.asyncio
async def test_http_discovery_never_creates_objective_and_keeps_conversation_ownership(tmp_path, monkeypatch):
    store = ObjectiveStore(tmp_path/"objectives.db")
    from src import conversation_objective
    monkeypatch.setattr(conversation_objective, "get_store", lambda: store)
    def owner(request, sid):
        if sid != "own": raise HTTPException(404, "Session not found")
        return "qa"
    monkeypatch.setattr(routes, "objective_owner", owner)
    app = FastAPI(); app.include_router(routes.setup_conversation_objective_routes())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/api/session/own/objective/templates?locale=es")
        assert result.status_code == 200 and result.json()["templates"][-1]["title"] == "Resumen semanal"
        assert store.get("qa", "own") is None
        assert (await client.get("/api/session/other/objective/templates")).status_code == 404
        assert (await client.get("/api/session/own/objective/templates?locale=bad")).status_code == 400
