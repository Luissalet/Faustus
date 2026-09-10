"""QA-15 · Edicion concurrente (docs/spec/v2/acceptance_scenarios.json).

Estimulo: usuario cambia el archivo despues de que el agente lo lea.
Resultado exigido (literal): "Conflicto con base/current/propuesta; ningun
cambio del usuario desaparece."

Requisitos: EDIT-01.

Estado: verde. `routes/document/document_routes.py` PUT /api/document/{id}
usa CAS por contenido (`expected_content` = lo que el agente leyo como
"base"): si el `current` ya cambio (el usuario edito mientras tanto),
devuelve 409 y el contenido actual del usuario permanece intacto (ver
tests/test_document_editor_conflict.py, EDIT-01 existente/parcial). Este
test repite el caso literal con la narrativa de la escena: el agente lee
"base", el usuario edita a "user edit" mientras tanto, y la propuesta del
agente (basada en la base vieja) es rechazada sin pisar el cambio del
usuario.
"""
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.qa_state("green")


def test_a_stale_agent_proposal_is_rejected_and_the_users_edit_survives(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as db
    from routes.document import document_routes as routes

    engine = create_engine("sqlite:///" + str(tmp_path / "qa15.db"),
                            connect_args={"check_same_thread": False})
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(routes, "SessionLocal", sessions)
    monkeypatch.setattr(routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(routes, "_verify_doc_owner", lambda session, doc, owner: None)
    monkeypatch.setattr(routes, "_assert_pdf_marker_upload_owned", lambda *args: None)
    with sessions() as session:
        session.add(db.Document(id="doc1", title="Notes", language="markdown",
                                 current_content="base", version_count=1,
                                 owner="alice", is_active=True))
        session.commit()

    app = FastAPI()
    app.include_router(routes.setup_document_routes(MagicMock()))
    with TestClient(app) as client:
        # The agent reads "base" and, meanwhile, the user edits the file.
        user_edit = client.put("/api/document/doc1",
                                json={"content": "user edit", "expected_content": "base"})
        assert user_edit.status_code == 200

        # The agent's proposal, built against the "base" it read, arrives
        # after the user's change - a genuine base/current/proposal conflict.
        agent_proposal = client.put("/api/document/doc1",
                                     json={"content": "agent proposal", "expected_content": "base"})
        assert agent_proposal.status_code == 409, agent_proposal.text

    with sessions() as session:
        doc = session.get(db.Document, "doc1")
        # The user's change never disappears.
        assert doc.current_content == "user edit"
    engine.dispose()
