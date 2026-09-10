"""HW-06 - nodos remotos como servicio controlado (src/remote_worker_registry.py).

Reuses, never reimplements:
  * pairing/health transport: src.ssh_trust.is_paired / ssh_argv (registration
    is refused outright for a host that is not already SSH-trusted);
  * capabilities/hardware: src.hardware_profiles.collect_profile(host=...)
    (HW-05) is how a caller learns what a node has -- this module only
    tracks reachability and reservations, it does not probe GPUs itself.

QA-27 ("Caída de nodo"): `reconcile()` is exercised directly here with a fake
probe standing in for "the node vanished mid-generation" -- no second real
machine, per COMUN's "no real loads in tests" rule. The existing
`tests/qa/test_qa_27_caida_de_nodo.py` stays `xfail` on purpose: flipping
`capability_registry.DECLARATIONS`'s `remote_worker.implemented` to True
would desync that test (and tests/test_capability_registry.py,
tests/test_contracts_mcp_and_routes.py, tests/test_doctor.py, all outside
this lot) -- see this lot's report for the exact line to change there.
"""
from __future__ import annotations

import pytest

from src import remote_worker_registry as rwr


@pytest.fixture(autouse=True)
def clean_state():
    rwr._NODES.clear()
    rwr._RESERVATIONS.clear()
    yield
    rwr._NODES.clear()
    rwr._RESERVATIONS.clear()


@pytest.fixture
def paired(monkeypatch):
    """gpu-box is SSH-paired; anything else is not."""
    monkeypatch.setattr("src.ssh_trust.is_paired", lambda remote, ssh_port=None: remote == "gpu-box")


# ── registration is gated on real pairing ───────────────────────────────────


def test_registering_an_unpaired_host_is_refused(paired):
    with pytest.raises(ValueError, match="paired"):
        rwr.register_node("random-host")
    assert rwr.list_nodes() == []


def test_registering_a_paired_host_succeeds(paired):
    node = rwr.register_node("gpu-box", label="Render box")
    assert node["status"] == "unknown"
    assert node["last_seen"] is None
    assert [n["id"] for n in rwr.list_nodes()] == [node["id"]]


def test_forget_node_releases_its_reservations(paired):
    node = rwr.register_node("gpu-box")
    rwr._NODES[node["id"]]["status"] = "healthy"
    rid = rwr.reserve_node(node["id"], job_id="job-1")
    assert rwr.forget_node(node["id"]) is True
    assert rwr.get_node(node["id"]) is None
    assert rwr.release_reservation(rid) is False  # already gone with the node


# ── health + reservations ───────────────────────────────────────────────────


def test_check_health_updates_status_from_the_probe(paired):
    node = rwr.register_node("gpu-box")
    healthy = rwr.check_health(node["id"], probe=lambda host, port: True)
    assert healthy["status"] == "healthy" and healthy["last_seen"] is not None

    gone = rwr.check_health(node["id"], probe=lambda host, port: False)
    assert gone["status"] == "unreachable"
    assert gone["last_error"]


def test_reserving_an_unreachable_node_is_refused(paired):
    node = rwr.register_node("gpu-box")
    rwr.check_health(node["id"], probe=lambda host, port: False)
    with pytest.raises(RuntimeError, match="unreachable"):
        rwr.reserve_node(node["id"], job_id="job-1")


# ── QA-27: reconcile() on a node that vanishes mid-job ──────────────────────


def test_reconcile_releases_reservations_of_a_vanished_node(paired):
    """The acceptance text, almost literally: diagnosis, release/reconcile,
    an uncertain result -- never a silent/false success."""
    node = rwr.register_node("gpu-box", label="Render box")
    rwr.check_health(node["id"], probe=lambda host, port: True)
    rid = rwr.reserve_node(node["id"], job_id="job-mid-generation")
    assert rwr.reservations_for_node(node["id"]) != []

    # The node vanishes mid-generation: every future probe now fails.
    reports = rwr.reconcile(probe=lambda host, port: False)

    assert len(reports) == 1
    r = reports[0]
    assert r["node_id"] == node["id"]
    assert r["status"] == "unreachable"
    assert r["result"] == "uncertain"          # never "success"
    assert r["released_job_ids"] == ["job-mid-generation"]
    assert rwr.reservations_for_node(node["id"]) == []   # released, not stuck forever
    assert rwr.release_reservation(rid) is False          # already released


def test_reconcile_reports_an_unreachable_idle_node_too(paired):
    """No reservation held is not the same as nothing to report — an
    unreachable node stays visible, it does not silently disappear."""
    node = rwr.register_node("gpu-box")
    reports = rwr.reconcile(probe=lambda host, port: False)
    assert reports[0]["result"] == "unreachable_idle"
    assert reports[0]["released_job_ids"] == []


def test_reconcile_leaves_a_healthy_node_alone(paired):
    node = rwr.register_node("gpu-box")
    rwr.check_health(node["id"], probe=lambda host, port: True)
    rid = rwr.reserve_node(node["id"], job_id="job-1")
    assert rwr.reconcile(probe=lambda host, port: True) == []
    assert rwr.reservations_for_node(node["id"]) == [
        {"id": rid, "node_id": node["id"], "job_id": "job-1", "created": rwr._RESERVATIONS[rid]["created"]}
    ]


# ── capability_registry integration stays honest (no flip in this lot) ─────


def test_remote_worker_probe_is_wired_but_still_gated_unimplemented(paired):
    """src.capability_registry._probe_remote_worker is real code and answers
    correctly when called directly -- but `observe()` never reaches it,
    because DECLARATIONS still says `implemented=False` for remote_worker
    (see this lot's report for why that flip belongs to a different lot)."""
    from src import capability_registry as registry

    assert registry.observe("remote_worker").state == "unavailable"
    assert "not implemented" in registry.observe("remote_worker").evidence

    node = rwr.register_node("gpu-box")
    rwr.check_health(node["id"], probe=lambda host, port: True)
    direct = registry._probe_remote_worker("2026-09-10T00:00:00Z")
    assert direct.state == "available"
    assert "1 node" in direct.evidence


def test_declared_backends_lists_remote_worker_honestly():
    from src import capability_registry as registry
    catalogue = registry.declared_backends()
    remote = next(b for b in catalogue["backends"] if b["id"] == "remote_worker")
    assert remote["implemented"] is False  # unchanged by this lot, on purpose
