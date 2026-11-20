"""Prompt mirror (src/prompt_mirror.py): typed vs machine traffic, the scorecard, suggestions,
redaction, saving and the comparison with the previous run, and the surfaces."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src import constants as constants_mod
from src import prompt_mirror as pm


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


def row(session, content, name="chat", meta=None, days=1):
    return {"session": session, "name": name, "content": content, "meta": meta or {}, "ts": datetime(2026, 11, 9) - timedelta(days=days)}


def history():
    users = [
        row("s1", "Arregla el arranque, tarda mucho. Terminado cuando el test de arranque pase en verde; para si tardas más de una hora."),
        row("s1", "sigue igual de lento, 3.2 s"),
        row("s1", "sigue igual, ahora 3.1 s"),
        row("s2", "¿cómo va?"),
        row("s2", "por qué paras? no pares hasta acabar"),
        row("s3", "Haz el informe del mes con los gastos y mándamelo a user@example.com"),
        row("s3", "por qué paras? sigue"),
        row("s4", "por qué paras ahora"),
        row("s4", "status?"),
        row("s5", '{"hours": 24}', name="[Task] Correo"),
        row("s6", "relay", meta={"group_turn_id": "x"}),
        row("s7", "Hermes's Hoard (el vigilante) te pide que mires esta tienda y le digas lo que ves, solo mirar"),
    ]
    for i in range(4):   # the same long prompt pasted in four chats is machine traffic
        users.append(row(f"p{i}", "Revisa el correo de las últimas 24 horas y resume lo importante en una lista corta por remitente"))
    assistants = [row("s1", "Tienes razón, lo miro."), row("s2", "Hecho."), row("s3", "You're right, fixing it.")]
    return users, assistants


def test_machine_traffic_is_left_out():
    assert pm.is_machine("[Task] Correo", {}, "x") and pm.is_machine("chat", {"dispatch_id": 1}, "x")
    assert pm.is_machine("chat", {}, '{"a": 1}') and not pm.is_machine("chat", {}, "hola, ¿qué tal?")
    s = pm.analyse(*history())
    assert s["typed"] == 9 and s["machine"] == 7 and s["chats"] == 4


def test_scorecard_and_suggestions():
    s = pm.analyse(*history())
    assert s["counts"]["status_ask"] == 2 and s["counts"]["premature_stop"] == 3 and s["counts"]["still_broken"] == 2
    assert s["class_chats"]["premature_stop"] == 3
    assert any(x["class"] == "premature_stop" and x["chats"] >= 3 for x in s["repeated_corrections"])
    assert s["loop_smells"] and s["loop_smells"][0]["still_broken"] == 2
    assert s["assistant"]["concessions"] == 2
    keys = [x["key"] for x in s["suggestions"]]
    assert len(keys) == 3 and "premature_stop" in keys and "still_broken" in keys
    sug = next(x for x in s["suggestions"] if x["key"] == "premature_stop")
    assert sug["before"] and "instinct" in sug["fix"]
    assert s["automation_candidates"][0]["times"] == 4
    text = pm.render(s)
    assert "Three suggestions" in text and "Status asks" in text


def test_redaction():
    out = pm.redact(r"mail user@example.com at https://x.io/a C:\Users\example-user\secret.txt token sk-abcdefghijklmnop Bruno", ["Bruno"])
    assert "@" not in out and "https" not in out and "Users" not in out and "sk-" not in out and "Bruno" not in out


def test_run_saves_and_compares_with_the_previous_run(monkeypatch):
    monkeypatch.setattr(pm, "_load", lambda owner, since, until: history())
    first = pm.run("alice", {"days": 30})
    assert first["saved"] and first["trend"] == []
    second = pm.run("alice", {"days": 30})
    assert second["trend"] and {t["verdict"] for t in second["trend"]} <= {"within noise", "better", "worse", "no data"}
    assert len(pm.history("alice")["runs"]) == 2


def test_surfaces(monkeypatch):
    import asyncio
    import json
    from src.agent_tools import TOOL_HANDLERS
    monkeypatch.setattr(pm, "_load", lambda owner, since, until: history())
    out = asyncio.run(TOOL_HANDLERS["prompt_mirror"](json.dumps({"days": 7}), {"owner": "alice"}))
    assert out["exit_code"] == 0 and "How you direct Faustus" in out["output"]
    hist = asyncio.run(TOOL_HANDLERS["prompt_mirror"](json.dumps({"action": "history"}), {"owner": "alice"}))
    assert "typed" in hist["output"]
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.loop_card_routes as lr
    monkeypatch.setattr(lr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(lr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(lr.setup_loop_card_routes())
    client = TestClient(app)
    r = client.post("/api/prompt-mirror", json={"days": 10})
    assert r.status_code == 200 and r.json()["typed"] == 9 and "report" in r.json()
    assert len(client.get("/api/prompt-mirror").json()["runs"]) == 2
