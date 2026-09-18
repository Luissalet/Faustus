"""tests/test_state_mirror_adapters_local.py -- the five local-machine adapters,
with the machine taken away.

Nothing here reads a real GPU, a real Ollama, a real integrations file or the
real git repository this file is checked into. Every source is a double
installed over the module-level reader the adapter goes through, and the one
real filesystem in the suite is `tmp_path`. A test that measured the machine it
ran on would be a test whose answer changed when somebody plugged in a card.

What is asserted, in the order it matters:

1.  **A missing, broken or nonsensical source costs an empty list, never an
    exception.** These adapters run inside a sweep that walks all of them in
    turn, and one raise takes down the description of a machine that is mostly
    working.
2.  **`hardware` claims a complete snapshot only when it read every field.**
    `device_state.v1` is the only schema in `contracts.SNAPSHOT_SCHEMAS`, so
    `partial=False` there licenses the reducer to DELETE the fields the
    observation does not carry. One failed read has to demote the whole
    observation to partial or a missing nvidia-smi silently erases the RAM
    reading beside it.
3.  **`connections` never emits a secret, and that includes a truncated one.**
    The double's integration carries a live-looking key; the whole observation
    is serialised and searched for it.
4.  **`workspace` reports `dirty` as absent, not false, when git could not be
    read.** "Nothing to commit" and "nobody looked" are the two facts
    `contracts._flag` exists to keep apart.
5.  **`services` maps `disabled` to `unknown` and not to `unavailable`.** A
    feature switched off has not been found to be broken.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from src.state_mirror.adapters import connections as ad_connections
from src.state_mirror.adapters import hardware as ad_hardware
from src.state_mirror.adapters import models as ad_models
from src.state_mirror.adapters import services as ad_services
from src.state_mirror.adapters import workspace as ad_workspace
from src.state_mirror.adapters.base import Scope
from src.state_mirror.contracts import SCHEMA_FIELDS, StateObservation


# -- helpers ---------------------------------------------------------------

#: A live-looking credential. The point of the connections tests is that this
#: string never appears in an observation, in any form.
TOKEN = "sk-abcdef123456"

#: A fixed instant, so an assertion about a stamp is an assertion and not a
#: race with the clock. 2023-11-14T22:13:20Z.
EPOCH = 1_700_000_000.0
STAMP = "2023-11-14T22:13:20Z"

SCOPE = Scope(owner="ada", project_id="odysseus", workspace="")


def boom(*_args: Any, **_kwargs: Any) -> Any:
    """A source that fails the way real ones do: loudly, and at the worst moment."""
    raise RuntimeError("the source is on fire")


def only(rows: Sequence[Any]) -> Any:
    """The single element, or a failure that says how many there really were."""
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}: {rows!r}"
    return rows[0]


def body(observations: Sequence[StateObservation]) -> Dict[str, Any]:
    return dict(only(observations).state)


@dataclass(frozen=True)
class Probe:
    """The shape `capability_registry.Observation` presents to this adapter."""

    backend_id: str
    state: str
    evidence: str = ""
    checked_at: str = STAMP


@pytest.fixture(autouse=True)
def _clean_caches() -> Any:
    """No test inherits another's cached reading of a machine that is not there."""
    ad_workspace.reset_cache()
    ad_services.reset_cache()
    ad_hardware.reset_cache()
    yield
    ad_workspace.reset_cache()
    ad_services.reset_cache()
    ad_hardware.reset_cache()


# -- workspace -------------------------------------------------------------

def _workspace_scope(path: Any) -> Scope:
    return Scope(owner="ada", project_id="odysseus", workspace=str(path))


def _fake_git(monkeypatch, *, branch: str = "", changes: Any = None,
              checkpoints: Any = None) -> None:
    monkeypatch.setattr(ad_workspace, "read_branch", lambda _ws: branch)
    monkeypatch.setattr(ad_workspace, "read_changes", lambda _ws: changes)
    monkeypatch.setattr(ad_workspace, "read_checkpoints", lambda _ws: checkpoints)


def test_workspace_says_nothing_without_a_workspace() -> None:
    bare = Scope(owner="ada")
    assert ad_workspace.WorkspaceAdapter().observe(bare) == []
    assert ad_workspace.WorkspaceAdapter().discover(bare) == []


def test_workspace_survives_a_reader_that_raises(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ad_workspace, "read", boom)
    assert ad_workspace.WorkspaceAdapter().observe(_workspace_scope(tmp_path)) == []


def test_workspace_survives_all_three_sources_raising(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ad_workspace, "read_branch", boom)
    monkeypatch.setattr(ad_workspace, "read_changes", boom)
    monkeypatch.setattr(ad_workspace, "read_checkpoints", boom)
    state = body(ad_workspace.WorkspaceAdapter().observe(_workspace_scope(tmp_path)))
    # The folder is there and that is all anybody managed to find out.
    assert state == {"workspace_available": True}


def test_workspace_reports_a_missing_checkout_as_missing(tmp_path) -> None:
    scope = _workspace_scope(tmp_path / "never-cloned")
    state = body(ad_workspace.WorkspaceAdapter().observe(scope))
    assert state == {"workspace_available": False}


def test_workspace_leaves_dirty_absent_when_git_could_not_be_read(monkeypatch, tmp_path) -> None:
    """`git_change_summary` answers None for "not a repo, or no git".

    The failure this pins: turning that None into `dirty=False` would have the
    mirror say "nothing to commit" about a working tree nothing has read.
    """
    _fake_git(monkeypatch, branch="", changes=None, checkpoints=None)
    state = body(ad_workspace.WorkspaceAdapter().observe(_workspace_scope(tmp_path)))
    assert state["workspace_available"] is True
    assert "dirty" not in state
    assert state.get("dirty") is None
    assert "changed_count" not in state
    assert "current_branch" not in state


def test_workspace_reads_a_clean_tree_as_clean(monkeypatch, tmp_path) -> None:
    _fake_git(monkeypatch, branch="main",
              changes={"changed": [], "changed_count": 0, "shortstat": ""})
    state = body(ad_workspace.WorkspaceAdapter().observe(_workspace_scope(tmp_path)))
    # `False` here is a measurement and survives; only `None` is dropped.
    assert state["dirty"] is False
    assert state["changed_count"] == 0
    assert state["current_branch"] == "main"


def test_workspace_counts_beyond_the_path_cap(monkeypatch, tmp_path) -> None:
    """`changed_paths` is capped at 200 by the source; `changed_count` is not."""
    _fake_git(
        monkeypatch,
        branch="feat/state-mirror",
        changes={"changed": [{"status": "M", "path": "src/a.py"}],
                 "changed_count": 300, "shortstat": "1 file changed"},
        checkpoints={"present": True, "head": "abc123def"},
    )
    state = body(ad_workspace.WorkspaceAdapter().observe(_workspace_scope(tmp_path)))
    assert state["dirty"] is True
    assert state["changed_count"] == 300
    assert state["changed_paths"] == ["src/a.py"]
    assert state["checkpoint_head"] == "abc123def"


def test_workspace_separates_two_checkouts_with_the_same_folder_name(tmp_path) -> None:
    left = tmp_path / "one" / "api"
    right = tmp_path / "two" / "api"
    left.mkdir(parents=True)
    right.mkdir(parents=True)
    bare = lambda p: Scope(owner="ada", workspace=str(p))     # noqa: E731
    assert (ad_workspace.workspace_identifier(bare(left))
            != ad_workspace.workspace_identifier(bare(right)))


def test_workspace_runs_git_once_per_ttl(monkeypatch, tmp_path) -> None:
    """A sweep every few seconds must not be three git processes every few seconds."""
    calls: List[str] = []

    def counted(workspace: str) -> Any:
        calls.append(workspace)
        return {"changed": [], "changed_count": 0, "shortstat": ""}

    monkeypatch.setattr(ad_workspace, "read_branch", lambda _ws: "main")
    monkeypatch.setattr(ad_workspace, "read_changes", counted)
    monkeypatch.setattr(ad_workspace, "read_checkpoints", lambda _ws: None)
    adapter = ad_workspace.WorkspaceAdapter()
    scope = _workspace_scope(tmp_path)
    first = only(adapter.observe(scope))
    second = only(adapter.observe(scope))
    assert len(calls) == 1
    # The cached answer keeps the time the git commands actually ran, so the
    # reducer reads the second look as a confirmation and not as news.
    assert first.observed_at == second.observed_at


# -- services --------------------------------------------------------------

@pytest.mark.parametrize("status, expected", [
    ("ok", "available"),
    ("available", "available"),
    ("degraded", "degraded"),
    ("down", "unavailable"),
    ("unavailable", "unavailable"),
    ("disabled", "unknown"),
    ("unknown", "unknown"),
    ("a word from a newer build", "unknown"),
    ("", "unknown"),
])
def test_services_health_mapping(status: str, expected: str) -> None:
    assert ad_services.health_for(status) == expected


def _fake_services(monkeypatch, *, backends: Any = (), readiness: Any = None) -> None:
    monkeypatch.setattr(ad_services, "read_backends", lambda: backends)
    monkeypatch.setattr(ad_services, "read_readiness",
                        lambda: (STAMP, readiness if readiness is not None else {}))


def test_services_publishes_disabled_as_unknown_not_unavailable(monkeypatch) -> None:
    """The whole point of the mapping table, end to end.

    A backend that is switched off must not read the same as one that is
    broken: a router choosing where to send work can start a daemon, and
    cannot start a feature nobody enabled.
    """
    _fake_services(monkeypatch, backends=(
        Probe("media_worker", "disabled", "declared but not implemented in this build"),
        Probe("docker_workspace", "down", "daemon not running"),
    ))
    by_id = {o.entity_id.rsplit("/", 1)[-1]: dict(o.state)
             for o in ad_services.ServicesAdapter().observe(SCOPE)}
    assert by_id["backend:media_worker"]["health"] == "unknown"
    assert by_id["backend:docker_workspace"]["health"] == "unavailable"
    # A verdict of `unknown` supports neither timestamp: nothing succeeded and
    # nothing was found to have failed.
    assert "last_failure_at" not in by_id["backend:media_worker"]
    assert "last_success_at" not in by_id["backend:media_worker"]
    assert by_id["backend:docker_workspace"]["last_failure_at"] == STAMP


def test_services_records_when_a_backend_last_worked(monkeypatch) -> None:
    _fake_services(monkeypatch, backends=(Probe("local", "available", "this process"),))
    state = body(ad_services.ServicesAdapter().observe(SCOPE))
    assert state["health"] == "available"
    assert state["last_success_at"] == STAMP
    assert state["reason"] == "this process"
    assert "last_failure_at" not in state


def test_services_reads_readiness_without_repeating_its_error_text(monkeypatch) -> None:
    """A SQLAlchemy failure carries the connection URL. It stays in the log."""
    secret_url = "postgresql://ada:hunter2@db.example/faustus"
    _fake_services(monkeypatch, readiness={"ready": False, "checks": {
        "database": {"ok": False, "error": f"could not connect to {secret_url}"},
        "data_dir": {"ok": True, "path": "/var/faustus"},
        "local_first": {"ok": True, "local": True},
    }})
    observations = ad_services.ServicesAdapter().observe(SCOPE)
    by_id = {o.entity_id.rsplit("/", 1)[-1]: dict(o.state) for o in observations}
    assert by_id["readiness:database"]["health"] == "unavailable"
    assert by_id["readiness:data_dir"]["health"] == "available"
    # `local_first` is informational and always ok; it is not a service.
    assert "readiness:local_first" not in by_id
    assert secret_url not in json.dumps([o.to_dict() for o in observations])
    assert all("reason" not in state for state in by_id.values())


def test_services_returns_nothing_when_both_sources_raise(monkeypatch) -> None:
    monkeypatch.setattr(ad_services, "read_backends", boom)
    monkeypatch.setattr(ad_services, "read_readiness", boom)
    assert ad_services.ServicesAdapter().observe(SCOPE) == []
    assert ad_services.ServicesAdapter().discover(SCOPE) == []


def test_services_loses_only_the_source_that_failed(monkeypatch) -> None:
    monkeypatch.setattr(ad_services, "read_backends", boom)
    monkeypatch.setattr(ad_services, "read_readiness",
                        lambda: (STAMP, {"checks": {"data_dir": {"ok": True}}}))
    state = body(ad_services.ServicesAdapter().observe(SCOPE))
    assert state["health"] == "available"


def test_services_skips_nonsense_rows(monkeypatch) -> None:
    _fake_services(
        monkeypatch,
        backends=("not an observation", Probe("", "ok", "no id at all"),
                  Probe("local", "available", "this process")),
        readiness={"checks": "not a mapping"},
    )
    assert len(ad_services.ServicesAdapter().observe(SCOPE)) == 1


# -- models ----------------------------------------------------------------

def _usage(models: Sequence[Dict[str, Any]], *, reachable: bool = True,
           shared: Optional[int] = None, ts: Any = EPOCH) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "ts": ts,
        "ollama": {"reachable": reachable, "base": "http://127.0.0.1:11434",
                   "models": list(models)},
    }
    if shared is not None:
        document["gpu_mem"] = {"supported": True, "ollama": {"shared": shared}}
    return document


ONE_MODEL = {"name": "qwen3.5:9b", "size": 9_000_000_000,
             "size_vram": 8_000_000_000, "context_length": 32768,
             "expires_at": "2023-11-14T22:18:20Z", "placement": "single"}
OTHER_MODEL = {"name": "llama3:8b", "size": 8_000_000_000,
               "size_vram": 4_000_000_000, "placement": "split"}


def test_models_says_nothing_without_a_collected_document(monkeypatch) -> None:
    monkeypatch.setattr(ad_models, "read_usage", lambda: None)
    assert ad_models.ModelsAdapter().observe(SCOPE) == []
    assert ad_models.ModelsAdapter().discover(SCOPE) == []


def test_models_says_nothing_when_the_document_cannot_be_dated(monkeypatch) -> None:
    """An observation that cannot be aged is one nothing may act on."""
    monkeypatch.setattr(ad_models, "read_usage", lambda: _usage([ONE_MODEL], ts=None))
    assert ad_models.ModelsAdapter().observe(SCOPE) == []


def test_models_says_nothing_when_ollama_was_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(ad_models, "read_usage",
                        lambda: _usage([], reachable=False))
    assert ad_models.ModelsAdapter().observe(SCOPE) == []


def test_models_survives_a_source_that_raises_or_lies(monkeypatch) -> None:
    monkeypatch.setattr(ad_models, "read_usage", boom)
    assert ad_models.ModelsAdapter().observe(SCOPE) == []
    monkeypatch.setattr(ad_models, "read_usage",
                        lambda: {"ts": EPOCH, "ollama": "not a mapping"})
    assert ad_models.ModelsAdapter().observe(SCOPE) == []
    monkeypatch.setattr(ad_models, "read_usage", lambda: "not a document at all")
    assert ad_models.ModelsAdapter().observe(SCOPE) == []


def test_models_stamps_the_observation_with_the_document_time(monkeypatch) -> None:
    """The cache's own `ts`, not the sweep's clock.

    This is what makes reading a cache honest: a document collected ten minutes
    ago produces an observation `freshness` will rate stale, rather than one
    that looks as if it had just been taken.
    """
    monkeypatch.setattr(ad_models, "read_usage", lambda: _usage([ONE_MODEL]))
    obs = only(ad_models.ModelsAdapter().observe(SCOPE))
    assert obs.observed_at == STAMP
    assert obs.entity_id == "model://ada/real/qwen3.5:9b"
    assert obs.state["loaded"] is True
    assert obs.state["availability"] == "available"
    assert obs.state["backend"] == ad_models.BACKEND
    assert obs.state["vram_bytes"] == 8_000_000_000
    assert obs.state["context_capacity"] == 32768
    assert obs.state["placement"] == "single"
    assert obs.state["expires_at"] == "2023-11-14T22:18:20Z"


def test_models_never_claims_a_rate_or_a_request_count(monkeypatch) -> None:
    monkeypatch.setattr(ad_models, "read_usage", lambda: _usage([ONE_MODEL]))
    state = body(ad_models.ModelsAdapter().observe(SCOPE))
    for field in ad_models.UNOBSERVED_FIELDS:
        assert field not in state


def test_models_attributes_shared_memory_only_when_one_model_is_resident(monkeypatch) -> None:
    """Two models cannot share one number between them without inventing a split."""
    monkeypatch.setattr(ad_models, "read_usage",
                        lambda: _usage([ONE_MODEL], shared=2_000_000_000))
    assert body(ad_models.ModelsAdapter().observe(SCOPE))["shared_memory_bytes"] == 2_000_000_000

    monkeypatch.setattr(ad_models, "read_usage",
                        lambda: _usage([ONE_MODEL, OTHER_MODEL], shared=2_000_000_000))
    observations = ad_models.ModelsAdapter().observe(SCOPE)
    assert len(observations) == 2
    assert all("shared_memory_bytes" not in o.state for o in observations)


# -- hardware --------------------------------------------------------------

HOST = {"cpu_percent": 12.5, "ram_used_bytes": 40_100_000_000,
        "ram_total_bytes": 137_000_000_000}
DISK = {"disk_free_bytes": 900_000_000_000}
CARDS = {"gpu_count": 2, "gpu_used_bytes": 13_000_000_000,
         "gpu_total_bytes": 28_000_000_000, "gpu_utilisation": 44.0}


def _fake_machine(monkeypatch, *, host: Any = HOST, disk: Any = DISK,
                  cards: Any = CARDS) -> None:
    monkeypatch.setattr(ad_hardware, "read_host",
                        boom if host is boom else lambda: dict(host))
    monkeypatch.setattr(ad_hardware, "read_disk",
                        boom if disk is boom else lambda: dict(disk))
    monkeypatch.setattr(ad_hardware, "read_gpu",
                        boom if cards is boom else (lambda: (STAMP, dict(cards))))


def test_hardware_covers_every_field_the_schema_declares() -> None:
    """The completeness check is only as good as the list it checks against."""
    assert set(ad_hardware.DEVICE_FIELDS) == set(SCHEMA_FIELDS["device_state.v1"])


def test_hardware_declares_a_complete_snapshot_only_when_it_read_everything(monkeypatch) -> None:
    _fake_machine(monkeypatch)
    obs = only(ad_hardware.HardwareAdapter().observe(SCOPE))
    assert obs.partial is False
    assert obs.complete() is True
    assert set(obs.state) == set(ad_hardware.DEVICE_FIELDS)
    assert obs.observed_at == STAMP


@pytest.mark.parametrize("failed", ["host", "disk", "cards"])
def test_hardware_stays_partial_when_one_read_fails(monkeypatch, failed: str) -> None:
    """`device_state.v1` is the only snapshot schema, so this is the load-bearing one.

    A complete snapshot licenses the reducer to DELETE every field it does not
    carry. If one failed read still produced `partial=False`, a missing
    nvidia-smi for one sweep would erase the RAM reading beside it.
    """
    _fake_machine(monkeypatch, **{failed: boom})
    obs = only(ad_hardware.HardwareAdapter().observe(SCOPE))
    assert obs.partial is True
    assert obs.complete() is False
    assert set(obs.state) < set(ad_hardware.DEVICE_FIELDS)


def test_hardware_stays_partial_on_a_box_with_no_card(monkeypatch) -> None:
    _fake_machine(monkeypatch, cards={})
    obs = only(ad_hardware.HardwareAdapter().observe(SCOPE))
    assert obs.partial is True
    assert set(obs.state) == set(HOST) | set(DISK)


def test_hardware_stays_partial_when_a_reader_answers_half(monkeypatch) -> None:
    _fake_machine(monkeypatch, cards={"gpu_count": 2, "gpu_utilisation": 44.0})
    obs = only(ad_hardware.HardwareAdapter().observe(SCOPE))
    assert obs.partial is True
    assert "gpu_used_bytes" not in obs.state
    assert obs.state["gpu_count"] == 2


def test_hardware_says_nothing_when_the_whole_machine_is_unreadable(monkeypatch) -> None:
    _fake_machine(monkeypatch, host=boom, disk=boom, cards=boom)
    assert ad_hardware.HardwareAdapter().observe(SCOPE) == []


def test_hardware_ignores_a_field_the_schema_does_not_declare(monkeypatch) -> None:
    _fake_machine(monkeypatch, disk={"disk_free_bytes": 1, "fan_rpm": 2200})
    state = body(ad_hardware.HardwareAdapter().observe(SCOPE))
    assert "fan_rpm" not in state
    assert state["disk_free_bytes"] == 1


def test_hardware_forks_nvidia_smi_once_per_ttl(monkeypatch) -> None:
    """A sweep every five seconds must not be a process every five seconds."""
    calls: List[int] = []

    def counted() -> Dict[str, Any]:
        calls.append(1)
        return dict(CARDS)

    ad_hardware.reset_cache()
    monkeypatch.setattr(ad_hardware, "_gpu_uncached", counted)
    assert ad_hardware.read_gpu()[1] == CARDS
    assert ad_hardware.read_gpu()[1] == CARDS
    assert len(calls) == 1


def test_hardware_caches_a_failed_gpu_read_too(monkeypatch) -> None:
    """Otherwise a box with no card pays for the failure on every single sweep."""
    calls: List[int] = []

    def counted() -> Dict[str, Any]:
        calls.append(1)
        raise RuntimeError("nvidia-smi: not found")

    ad_hardware.reset_cache()
    monkeypatch.setattr(ad_hardware, "_gpu_uncached", counted)
    assert ad_hardware.read_gpu()[1] == {}
    assert ad_hardware.read_gpu()[1] == {}
    assert len(calls) == 1


# -- connections -----------------------------------------------------------

INTEGRATION = {
    "id": "miniflux1", "name": "Miniflux", "preset": "miniflux",
    "base_url": "https://rss.private.example/v1", "auth_type": "header",
    "auth_header": "X-Auth-Token", "enabled": True,
    # `load_integrations` hands this back DECRYPTED. Nothing may carry it out.
    "api_key": TOKEN,
}
ENDPOINT = {"id": "ep1", "name": "Local vLLM", "owner": None, "enabled": True,
            "has_base_url": True, "has_key": True, "has_session": False}


def _fake_stores(monkeypatch, *, integrations: Any = (INTEGRATION,),
                 endpoints: Any = (ENDPOINT,)) -> None:
    monkeypatch.setattr(ad_connections, "read_integrations",
                        boom if integrations is boom else lambda: list(integrations))
    monkeypatch.setattr(ad_connections, "read_endpoints",
                        boom if endpoints is boom else lambda: list(endpoints))


def test_connections_never_emits_anything_token_shaped(monkeypatch) -> None:
    """The credential is in the record. It must not be anywhere in the output.

    Serialised and searched rather than checked field by field, because the
    way a secret escapes is never through the field somebody was watching.
    Truncation is not a defence: `mask_integration_secret` produces `sk-a****`,
    which is still four characters of a live key.
    """
    _fake_stores(monkeypatch)
    observations = ad_connections.ConnectionsAdapter().observe(SCOPE)
    assert observations, "the doubles should have produced rows"
    blob = json.dumps([o.to_dict() for o in observations])
    for fragment in (TOKEN, TOKEN[:8], TOKEN[:4], "abcdef123456",
                     "rss.private.example", "X-Auth-Token"):
        assert fragment not in blob, f"{fragment!r} escaped into a state row"
    for obs in observations:
        assert set(obs.state) == {"configured"}


def test_connections_keeps_the_three_facts_apart(monkeypatch) -> None:
    """A stored credential is not an accepted one, and a base URL is not a socket."""
    _fake_stores(monkeypatch)
    for obs in ad_connections.ConnectionsAdapter().observe(SCOPE):
        assert obs.state["configured"] is True
        for field in ad_connections.UNOBSERVED_FIELDS:
            assert field not in obs.state
            assert obs.state.get(field) is None


@pytest.mark.parametrize("record, expected", [
    ({"base_url": "https://x", "auth_type": "none"}, True),
    ({"base_url": "https://x", "auth_type": ""}, True),
    ({"base_url": "https://x", "auth_type": "bearer", "api_key": TOKEN}, True),
    ({"base_url": "https://x", "auth_type": "bearer"}, False),
    ({"base_url": "https://x", "auth_type": "header", "api_key": "  "}, False),
    ({"base_url": "", "auth_type": "none", "api_key": TOKEN}, False),
    ({}, False),
])
def test_connections_configured_is_computed_and_never_copied(record, expected) -> None:
    answer = ad_connections.integration_configured(record)
    assert answer is expected
    assert isinstance(answer, bool)


def test_connections_returns_nothing_when_both_stores_fail(monkeypatch) -> None:
    _fake_stores(monkeypatch, integrations=boom, endpoints=boom)
    assert ad_connections.ConnectionsAdapter().observe(SCOPE) == []
    assert ad_connections.ConnectionsAdapter().discover(SCOPE) == []


def test_connections_loses_only_the_store_that_failed(monkeypatch) -> None:
    _fake_stores(monkeypatch, integrations=boom)
    obs = only(ad_connections.ConnectionsAdapter().observe(SCOPE))
    assert obs.entity_id == "connection://ada/real/endpoint:ep1"


def test_connections_skips_nonsense_rows(monkeypatch) -> None:
    _fake_stores(monkeypatch,
                 integrations=("not a record", {"name": "no id"}, INTEGRATION),
                 endpoints=(None, {"id": ""}, ENDPOINT))
    assert len(ad_connections.ConnectionsAdapter().observe(SCOPE)) == 2


def test_connections_keeps_one_owner_out_of_anothers_answer(monkeypatch) -> None:
    """A NULL owner is the shared row and belongs to everybody; a named one does not."""
    _fake_stores(monkeypatch, integrations=(), endpoints=(
        dict(ENDPOINT, id="shared", owner=None),
        dict(ENDPOINT, id="hers", owner="grace"),
        dict(ENDPOINT, id="his", owner="ada"),
    ))
    seen = {o.entity_id.rsplit("/", 1)[-1]
            for o in ad_connections.ConnectionsAdapter().observe(SCOPE)}
    assert seen == {"endpoint:shared", "endpoint:his"}


def test_connections_marks_the_rows_restricted(monkeypatch) -> None:
    """Section 19: a field whose EXISTENCE is private travels no further than it must."""
    _fake_stores(monkeypatch)
    rows = ad_connections.ConnectionsAdapter().discover(SCOPE)
    assert rows
    assert all(row.sensitivity == "restricted" for row in rows)
    assert all(o.sensitivity == "restricted"
               for o in ad_connections.ConnectionsAdapter().observe(SCOPE))


# -- what holds across all five -------------------------------------------

def _every_adapter(monkeypatch, tmp_path) -> List[Tuple[Any, Scope]]:
    """The five adapters with every source doubled and answering."""
    _fake_git(monkeypatch, branch="main",
              changes={"changed": [{"status": "M", "path": "src/a.py"}],
                       "changed_count": 1, "shortstat": ""},
              checkpoints={"present": True, "head": "abc123def"})
    _fake_services(monkeypatch, backends=(Probe("local", "available", "this process"),),
                   readiness={"checks": {"data_dir": {"ok": True}}})
    monkeypatch.setattr(ad_models, "read_usage", lambda: _usage([ONE_MODEL]))
    _fake_machine(monkeypatch)
    _fake_stores(monkeypatch)
    workspace_scope = _workspace_scope(tmp_path)
    return [
        (ad_workspace.WorkspaceAdapter(), workspace_scope),
        (ad_services.ServicesAdapter(), SCOPE),
        (ad_models.ModelsAdapter(), SCOPE),
        (ad_hardware.HardwareAdapter(), SCOPE),
        (ad_connections.ConnectionsAdapter(), SCOPE),
    ]


def test_every_observation_round_trips_through_its_contract(monkeypatch, tmp_path) -> None:
    """Anything that will not re-parse is a row the store cannot read back."""
    for adapter, scope in _every_adapter(monkeypatch, tmp_path):
        observations = adapter.observe(scope)
        assert observations, f"{adapter.name} produced nothing with every source answering"
        for obs in observations:
            again = StateObservation.parse(obs.to_dict())
            assert again.identity() == obs.identity()
            assert again.schema in adapter.schemas
            assert again.source == adapter.name
            # Nothing in this package reports; every field above was read off
            # the machine or off a store that owns it.
            assert again.epistemic == "observed"
            assert again.owner == scope.owner
            assert again.namespace == "real"


def test_every_adapter_answers_an_empty_scope_without_raising(monkeypatch) -> None:
    """A sweep run before anything is configured is the commonest first run."""
    monkeypatch.setattr(ad_workspace, "read", boom)
    monkeypatch.setattr(ad_services, "read_backends", boom)
    monkeypatch.setattr(ad_services, "read_readiness", boom)
    monkeypatch.setattr(ad_models, "read_usage", boom)
    monkeypatch.setattr(ad_hardware, "read_host", boom)
    monkeypatch.setattr(ad_hardware, "read_disk", boom)
    monkeypatch.setattr(ad_hardware, "read_gpu", boom)
    monkeypatch.setattr(ad_connections, "read_integrations", boom)
    monkeypatch.setattr(ad_connections, "read_endpoints", boom)
    bare = Scope()
    for adapter in (ad_workspace.WorkspaceAdapter(), ad_services.ServicesAdapter(),
                    ad_models.ModelsAdapter(), ad_hardware.HardwareAdapter(),
                    ad_connections.ConnectionsAdapter()):
        assert adapter.observe(bare) == []
        assert adapter.relations(bare) == []
        assert adapter.available() is True
        # `discover` is a separate question from `observe`. The machine exists
        # whether or not anything could be read off it, so `hardware` still
        # names the device; the four whose entities come out of a store they
        # could not read name nothing.
        assert isinstance(adapter.discover(bare), list)
    assert len(ad_hardware.HardwareAdapter().discover(bare)) == 1
    for adapter in (ad_workspace.WorkspaceAdapter(), ad_services.ServicesAdapter(),
                    ad_models.ModelsAdapter(), ad_connections.ConnectionsAdapter()):
        assert adapter.discover(bare) == []
