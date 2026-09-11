"""QA-27 · Caida de nodo (docs/spec/v2/acceptance_scenarios.json).

Estimulo: nodo remoto desaparece a mitad de generacion o render.
Resultado exigido (literal): "Diagnostico, liberacion/reconciliacion y
resultado parcial/incierto; no exito falso."

Requisitos: HW-06, MEDIA-04.

Lote 66: la primitiva que respalda esta aceptacion (`src/remote_worker_registry.py`
-- `register_node`, `check_health`, `reconcile`) ya existe y ya cierra el
enunciado literal por si misma -- probado aqui directamente contra ella, sin
pasar por un nodo SSH real (`register_node` exige `ssh_trust.is_paired`,
mockeado; `check_health`/`reconcile` aceptan un `probe` inyectable para
"el nodo desaparecio" sin una segunda maquina, exactamente como documenta
`reconcile`'s propio docstring).

Lo que SIGUE sin cerrar, y por que este test no lo exige: `remote_worker` en
`src/capability_registry.py::DECLARATIONS` sigue con `implemented: False`
A PROPOSITO -- cuatro ficheros de test ajenos a este lote afirman
explicitamente `implemented is False` (ver tests/test_contracts_mcp_and_routes.py
y los otros tres citados en docs/spec/v2 huecos.md bajo HW-06). Cambiar ese
flag es una decision de producto (que criterio de fiabilidad hace a un nodo
remoto "implementado"), no solo codigo, y este lote no la toma. Por eso este
test prueba la aceptacion contra el REGISTRO (la pieza que HW-06 ya construyo
y que un backend `remote_worker` real usaria), no contra un pipeline de
inferencia/render remoto de extremo a extremo -- ese pipeline no existe hasta
que la decision de producto declare el backend implementado, y NINGUN test
puede demostrar que algo inexistente se comporta bien. Lo que SI se puede, y
se hace aqui, es demostrar que la pieza que existiria detras de ese backend ya
cumple, hoy, la letra de QA-27: un nodo perdido nunca deja una reserva viva
para siempre y nunca se reporta como exito.
"""
import pytest

from src import remote_worker_registry as registry

pytestmark = pytest.mark.qa_state("green")


@pytest.fixture(autouse=True)
def _clean_registry():
    with registry._LOCK:
        registry._NODES.clear()
        registry._RESERVATIONS.clear()
    yield
    with registry._LOCK:
        registry._NODES.clear()
        registry._RESERVATIONS.clear()


def _register_paired_node(monkeypatch, host="worker-1.lan"):
    monkeypatch.setattr("src.ssh_trust.is_paired", lambda h, p: True)
    return registry.register_node(host, label="GPU box")


def test_a_node_lost_mid_job_releases_its_reservation_and_is_never_reported_a_success(monkeypatch):
    node = _register_paired_node(monkeypatch)
    # The node answered its last health probe -- this is the state a job
    # mid-generation/render on it would see right before it vanishes.
    up = registry.check_health(node["id"], probe=lambda host, port: True)
    assert up["status"] == "healthy"

    reservation_id = registry.reserve_node(node["id"], job_id="job-42")
    assert registry.reservations_for_node(node["id"]) != []

    # Estimulo: the node disappears mid-job (probe now fails every time,
    # exactly as an unplugged/crashed/network-partitioned machine would).
    reports = registry.reconcile(probe=lambda host, port: False)

    row = next((r for r in reports if r["node_id"] == node["id"]), None)
    assert row is not None, "a node that stopped answering must be diagnosed, not silently dropped"

    # "no exito falso": nothing here is allowed to read as the job succeeding.
    assert row["status"] == "unreachable"
    assert row["result"] != "success"
    assert row["result"] == "uncertain"
    assert row["diagnosis"]  # a human-readable reason, not an empty/blank verdict

    # "liberacion/reconciliacion": the reservation the vanished node held is
    # gone -- it is not reserved forever for a machine that will never finish
    # the job.
    assert "job-42" in row["released_job_ids"]
    assert registry.reservations_for_node(node["id"]) == []
    assert registry.release_reservation(reservation_id) is False  # already released, not double-counted


def test_reconcile_still_diagnoses_a_lost_node_that_held_nothing(monkeypatch):
    """An idle node going unreachable is not silence either -- QA-27's
    "diagnostico" applies even when there was no job to strand."""
    node = _register_paired_node(monkeypatch, host="worker-2.lan")
    registry.check_health(node["id"], probe=lambda host, port: True)

    reports = registry.reconcile(probe=lambda host, port: False)

    row = next((r for r in reports if r["node_id"] == node["id"]), None)
    assert row is not None
    assert row["status"] == "unreachable"
    assert row["result"] == "unreachable_idle"
    assert row["released_job_ids"] == []


def test_a_healthy_node_is_left_out_of_the_reconcile_report(monkeypatch):
    """The other half of "no falso exito ni falso fallo": reconcile() must
    not manufacture a diagnosis for a node that is actually still there."""
    node = _register_paired_node(monkeypatch, host="worker-3.lan")
    reports = registry.reconcile(probe=lambda host, port: True)
    assert all(r["node_id"] != node["id"] for r in reports)


def test_register_node_refuses_a_host_that_is_not_ssh_paired(monkeypatch):
    """A node can never enter the registry — and so can never be reserved for
    a job in the first place — behind the person's back."""
    monkeypatch.setattr("src.ssh_trust.is_paired", lambda h, p: False)
    with pytest.raises(ValueError):
        registry.register_node("unpaired.lan")
