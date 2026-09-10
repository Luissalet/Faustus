"""QA-48 · Migracion de carpeta (docs/spec/v2/acceptance_scenarios.json).

Estimulo: mover proyecto de disco, conservar nombre y abrir artefactos.
Resultado exigido (literal): "Identidad, recuerdos y relaciones continuan;
rutas ausentes se resuelven explicitamente."

Requisitos: OPS-04, IDX-01.

Estado: verde. `services/projects.py::ProjectStore` (IDX-01, parcial pero
con esto ya cubierto) identifica un proyecto por `project_id` estable, no
por la ruta de carpeta - moverlo a otro disco y actualizar `workspace` no
rompe los chats/memorias que apuntan a `project_id` (ver
tests/test_project_identity.py::test_project_id_survives_renaming_the_folder,
el caso base de esta garantia). Este test simula una migracion de disco
completa: cambia `workspace` a una ruta totalmente distinta conservando el
nombre, y comprueba que la sesion sigue resolviendo al MISMO proyecto.
Cuando la nueva ruta no existe todavia en disco, `studio/src/screens/Project.tsx`
la muestra explicitamente como "Source missing" en vez de fallar en
silencio (IDX-01, no re-probado aqui por ser frontend).
"""
import dataclasses
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base, Session as DbSession
from services import projects as projects_mod
from services.projects import ProjectStore

pytestmark = pytest.mark.qa_state("green")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.session_manager as sm_mod
    url = "sqlite:///" + (tmp_path / "identity.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    monkeypatch.setattr(sm_mod, "SessionLocal", Session)
    yield Session
    engine.dispose()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    st = ProjectStore(str(tmp_path / "data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    return st


def test_moving_the_project_to_another_disk_path_keeps_its_identity(db, store, tmp_path):
    old_ws = str(tmp_path / "D_old" / "Faustus")
    os.makedirs(old_ws, exist_ok=True)
    project = store.create("Faustus", folder="Faustus", workspace=old_ws)

    session = DbSession(id="s1", name="s1", endpoint_url="http://ep", model="m",
                        folder="Faustus", project_id=project["id"])
    with db() as conn:
        conn.add(session)
        conn.commit()

    # The migration: a different drive/path entirely, same project name. The
    # folder exists at the new location (as it would after a real disk copy).
    new_ws = str(tmp_path / "E_new_disk" / "Faustus")
    os.makedirs(new_ws, exist_ok=True)
    updated = store.update(project["id"], {"workspace": new_ws})
    assert updated["workspace"] == new_ws
    assert updated["id"] == project["id"]  # identity unchanged by the move

    resolved, source = projects_mod._resolve_project_for_session("s1")
    assert resolved["id"] == project["id"]
    assert resolved["workspace"] == new_ws
    assert source == "direct"
