"""QA-13 · Pregunta abandonada (docs/spec/v2/acceptance_scenarios.json).

Estimulo: caduca una aprobacion de envio sin respuesta.
Resultado exigido (literal): "No envia por defecto ni interpreta silencio
como consentimiento."

Requisitos: AUTO-02, SEC-01.

Estado: verde. `src/approval_store.py` distingue `expired` de `granted`
explicitamente (`decide()`/`expire_stale()`, ver
docs/spec/v2/MAPA_REUTILIZACION.md AUTO-02: "Caducar nunca es 'granted'
(verificado en codigo)"), y `check()`/`consume()` solo autorizan sobre
`status == "granted"`. Este test abre una aprobacion con TTL corto, la deja
sin respuesta, la barre con `expire_stale()` y comprueba que sigue sin
autorizar el envio - el silencio nunca se convierte en un si.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src import approval_store
from src.contracts import ApprovalPlan

pytestmark = pytest.mark.qa_state("green")

PLAN = {
    "action": "deliver", "skill_id": "mail.send", "skill_version": "1.0.0",
    "backend": "docker_workspace", "recipients": ["ana@example.com"],
    "cost_units": 0, "secret_names": ["smtp"], "output_kinds": ["document"],
    "detail": "Send the invoice to Ana.",
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "approvals.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                         sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


def test_an_expired_approval_never_authorizes_the_send(store):
    card = approval_store.request(PLAN, owner="luis", ttl_seconds=1)
    assert card.status == "pending"

    # Time passes; nobody answers. The sweep marks it expired - not granted,
    # not denied-by-timeout-treated-as-a-decision, just expired.
    expired_stamp = "2999-01-01T00:00:00Z"  # far enough past any TTL
    changed = approval_store.expire_stale(now=expired_stamp)
    assert changed >= 1

    reloaded = approval_store.get(card.id)
    assert reloaded.status == "expired"
    assert reloaded.status != "granted"

    # Nothing downstream treats the abandoned question as consent: sending
    # is still refused, exactly as it was before expiry.
    result = approval_store.check(PLAN, owner="luis")
    assert result["ok"] is False

    consumed = approval_store.consume(card.id, PLAN, owner="luis")
    assert consumed.get("ok") is not True
