"""PLAN-03 / QA-16 — `ResourceOwnershipRegistry`: a TTL'd lease over any
resource (file, browser session, model), reconciled on expiry, with file
identity/conflict bookkeeping reused from `FileLockRegistry` rather than
duplicated.
"""
from src.agent_tools.subagent_tools import FileLockRegistry
from src.resource_ownership import ResourceOwnershipRegistry


# ---------------------------------------------------------------------------
# Two owners
# ---------------------------------------------------------------------------

def test_a_second_owner_cannot_acquire_a_held_lease():
    reg = ResourceOwnershipRegistry(default_ttl_seconds=100)
    assert reg.acquire("model", "llama-70b", "workerA", now=0) is True
    assert reg.acquire("model", "llama-70b", "workerB", now=1) is False
    assert reg.owner_of("model", "llama-70b", now=1) == "workerA"


def test_the_same_owner_re_acquiring_renews_instead_of_failing():
    reg = ResourceOwnershipRegistry(default_ttl_seconds=10)
    assert reg.acquire("model", "m", "workerA", now=0) is True
    assert reg.acquire("model", "m", "workerA", now=8) is True  # renew, not a new claim
    # Renewed at t=8 with a 10s TTL: still held at t=15 (would have expired
    # at t=10 without the renewal).
    assert reg.owner_of("model", "m", now=15) == "workerA"


def test_releasing_lets_a_different_owner_acquire_immediately():
    reg = ResourceOwnershipRegistry()
    reg.acquire("browser_session", "sess-1", "workerA", now=0)
    assert reg.release("browser_session", "sess-1", "workerA") is True
    assert reg.acquire("browser_session", "sess-1", "workerB", now=0) is True


def test_release_by_a_non_owner_fails_and_changes_nothing():
    reg = ResourceOwnershipRegistry()
    reg.acquire("browser_session", "sess-1", "workerA", now=0)
    assert reg.release("browser_session", "sess-1", "workerB") is False
    assert reg.owner_of("browser_session", "sess-1", now=0) == "workerA"


# ---------------------------------------------------------------------------
# TTL / expiration and reconciliation
# ---------------------------------------------------------------------------

def test_an_expired_lease_is_reclaimable_and_reconciled():
    reg = ResourceOwnershipRegistry(default_ttl_seconds=10)
    reg.acquire("model", "m", "workerA", now=0)
    reg.note_activity("model", "m", "workerA", "loaded weights")
    reg.note_activity("model", "m", "workerA", "ran 3 prompts")

    # Still held just before expiry.
    assert reg.acquire("model", "m", "workerB", now=9) is False
    # Expired: workerB can now take it.
    assert reg.acquire("model", "m", "workerB", now=11) is True

    history = reg.history()
    assert len(history) == 1
    record = history[0]
    assert record.previous_owner == "workerA"
    assert record.resource == "m" and record.kind == "model"
    assert record.reason == "lease TTL elapsed with no renewal"
    # "qué hizo": the activity trail survives into the reconciliation record.
    assert record.activity == ["loaded weights", "ran 3 prompts"]
    assert reg.owner_of("model", "m", now=11) == "workerB"


def test_sweep_expired_reaps_every_lapsed_lease_at_once():
    reg = ResourceOwnershipRegistry(default_ttl_seconds=5)
    reg.acquire("generic", "res-1", "A", now=0)
    reg.acquire("generic", "res-2", "B", now=0)
    reg.acquire("generic", "res-3", "C", now=100, ttl_seconds=1000)  # not expired

    produced = reg.sweep_expired(now=10)

    resources_ended = {r.resource for r in produced}
    assert resources_ended == {"res-1", "res-2"}
    assert reg.owner_of("generic", "res-3", now=10) == "C"  # untouched


def test_a_cleanly_released_lease_is_recorded_too_with_its_own_reason():
    reg = ResourceOwnershipRegistry()
    reg.acquire("browser_session", "sess-1", "workerA", now=0)
    reg.release("browser_session", "sess-1", "workerA", note="closed the tab cleanly")

    history = reg.history()
    assert len(history) == 1
    assert history[0].reason == "released"
    assert history[0].activity == ["closed the tab cleanly"]


def test_note_activity_on_a_lease_not_held_by_that_owner_fails():
    reg = ResourceOwnershipRegistry()
    reg.acquire("model", "m", "workerA", now=0)
    assert reg.note_activity("model", "m", "workerB", "did something") is False


# ---------------------------------------------------------------------------
# "file" reuses FileLockRegistry rather than duplicating it (PLAN-03)
# ---------------------------------------------------------------------------

def test_file_leases_are_mirrored_into_the_wrapped_filelockregistry():
    files = FileLockRegistry(workspace="/tmp/some/workspace")
    reg = ResourceOwnershipRegistry(file_registry=files, default_ttl_seconds=100)

    assert reg.acquire("file", "notes.md", "workerA", now=0) is True
    normalized = files.norm("notes.md")
    assert files.owner.get(normalized) == "workerA"

    # A second worker is blocked both via this registry AND via the
    # underlying FileLockRegistry other code already reads.
    assert reg.acquire("file", "notes.md", "workerB", now=1) is False
    assert files.blocked_by("workerB", ["notes.md"]) == "workerA"


def test_a_file_already_owned_directly_in_filelockregistry_blocks_acquire_here():
    """The reuse goes both ways: a claim made through the raw
    FileLockRegistry (e.g. by existing delegation code this registry did not
    create) is respected here too, not silently overridden."""
    files = FileLockRegistry(workspace=None)
    files.claim("legacyWorker", ["report.md"])
    reg = ResourceOwnershipRegistry(file_registry=files)

    assert reg.acquire("file", "report.md", "newWorker", now=0) is False


def test_file_lease_expiry_releases_it_in_the_wrapped_filelockregistry_too():
    files = FileLockRegistry(workspace=None)
    reg = ResourceOwnershipRegistry(file_registry=files, default_ttl_seconds=5)
    reg.acquire("file", "draft.md", "workerA", now=0)

    reg.sweep_expired(now=10)

    normalized = files.norm("draft.md")
    assert normalized not in files.owner  # freed in the underlying registry too


def test_without_a_supplied_file_registry_one_is_created_automatically():
    reg = ResourceOwnershipRegistry()
    assert isinstance(reg.files, FileLockRegistry)
    assert reg.acquire("file", "a.md", "w1", now=0) is True
