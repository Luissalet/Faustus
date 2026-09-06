"""The L1 working set (src/context_engine/cache.py).

The test that matters most is the leak: two people on one install share one
process and one dictionary, and a key of "memories" is the same key for both of
them.  A cache hit that ignored the scope would hand the second person the
first person's memories with no store ever consulted and no authorisation check
ever run — a whole class of isolation bug that never touches the code that was
written to prevent it.

The rest pins what makes the layer safe to lose: LRU + TTL + a byte ceiling,
counters that say whether it is earning its memory, and §1.7's event table,
where an unknown event is a no-op rather than an outage.
"""

import threading

import pytest

from src.context_engine import cache
from src.context_engine.cache import CacheStats, WorkingSet
from src.context_engine.contracts import ContextExecution, ContextRequest


class _Clock:
    """A monotonic clock a test can move.  Real time in a TTL test is how a
    suite gets a failure that only happens on a loaded CI box."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture()
def clean_singleton():
    cache.reset_working_set()
    try:
        yield
    finally:
        cache.reset_working_set()


# ── isolation: the reason this file exists ─────────────────────────────────

def test_the_same_key_in_two_scopes_is_two_entries():
    ws = WorkingSet()
    ws.put("owner_a|proj||", "candidates:memory", ["a's memories"])
    ws.put("owner_b|proj||", "candidates:memory", ["b's memories"])

    assert ws.get("owner_a|proj||", "candidates:memory") == ["a's memories"]
    assert ws.get("owner_b|proj||", "candidates:memory") == ["b's memories"]


def test_a_hit_in_one_scope_is_a_miss_in_another():
    ws = WorkingSet()
    ws.put("owner_a|proj||", "candidates:memory", ["private"])

    assert ws.get("owner_b|proj||", "candidates:memory") is None
    assert ws.get("", "candidates:memory") is None
    # And the miss was counted as a miss, not silently swallowed.
    assert ws.stats().misses == 2


def test_a_crafted_key_cannot_reach_into_another_scope():
    """The separator is not something a caller can spell its way past."""
    ws = WorkingSet()
    ws.put("owner_a", "secret", "value")
    for attempt in ("owner_a" + cache.SEP + "secret", "owner_a|secret",
                    cache.SEP + "secret"):
        assert ws.get("owner_b", attempt) is None


def test_scope_of_is_the_executions_own_scope_key():
    request = ContextRequest(execution=ContextExecution(
        owner="luis", project_id="p1", council_id="c1", branch_id="b1"))
    assert cache.scope_of(request) == request.execution.scope_key()
    # Deliberately excludes run and turn: reuse across turns of one session is
    # the point; reuse across owners is the bug.
    assert cache.scope_of(request) == "luis|p1|c1|b1"


# ── bounds ─────────────────────────────────────────────────────────────────

def test_lru_evicts_the_least_recently_used_entry():
    ws = WorkingSet(max_entries=3)
    for name in ("a", "b", "c"):
        ws.put("s", name, name)
    ws.get("s", "a")               # a is now the most recent
    ws.put("s", "d", "d")          # evicts b

    assert ws.get("s", "b") is None
    assert ws.get("s", "a") == "a"
    assert ws.get("s", "d") == "d"
    assert ws.stats().evictions == 1


def test_the_byte_ceiling_evicts_even_when_the_entry_count_fits():
    ws = WorkingSet(max_entries=100, max_bytes=1000)
    ws.put("s", "one", "x", cost_bytes=600)
    ws.put("s", "two", "y", cost_bytes=600)

    stats = ws.stats()
    assert stats.entries == 1 and stats.bytes == 600
    assert ws.get("s", "one") is None


def test_an_entry_bigger_than_the_whole_cache_is_refused_not_stored():
    ws = WorkingSet(max_entries=10, max_bytes=1000)
    ws.put("s", "huge", "x", cost_bytes=5000)

    assert ws.get("s", "huge") is None
    assert ws.stats().entries == 0 and ws.stats().bytes == 0


def test_a_refused_oversize_write_replaces_nothing_it_cannot_keep():
    ws = WorkingSet(max_entries=10, max_bytes=1000)
    ws.put("s", "k", "small", cost_bytes=10)
    ws.put("s", "k", "enormous", cost_bytes=5000)
    # The old value is gone rather than left behind as a stale answer to a
    # write that was meant to replace it.
    assert ws.get("s", "k") is None


def test_ttl_expiry_is_a_miss_and_frees_the_bytes():
    clock = _Clock()
    ws = WorkingSet(ttl_s=60.0, clock=clock)
    ws.put("s", "k", "v", cost_bytes=100)

    clock.advance(59)
    assert ws.get("s", "k") == "v"
    clock.advance(2)
    assert ws.get("s", "k") is None
    assert ws.stats().entries == 0 and ws.stats().bytes == 0


def test_a_per_entry_ttl_overrides_the_default():
    clock = _Clock()
    ws = WorkingSet(ttl_s=1000.0, clock=clock)
    ws.put("s", "short", "v", ttl_s=10.0)
    clock.advance(11)
    assert ws.get("s", "short") is None


# ── invalidation ───────────────────────────────────────────────────────────

def _seed(ws):
    ws.put("a|p||", cache.PREFIX_CANDIDATES + "memory", 1)
    ws.put("a|p||", cache.PREFIX_STATE + "mirror", 2)
    ws.put("a|p||", cache.PREFIX_TOOLS + "guide", 3)
    ws.put("b|p||", cache.PREFIX_CANDIDATES + "memory", 4)
    return ws


def test_invalidate_by_key_scope_prefix_and_everything():
    ws = _seed(WorkingSet())
    assert ws.invalidate("a|p||", key=cache.PREFIX_STATE + "mirror") == 1
    assert ws.invalidate("a|p||", prefix=cache.PREFIX_TOOLS) == 1
    assert ws.invalidate("a|p||") == 1
    assert ws.get("b|p||", cache.PREFIX_CANDIDATES + "memory") == 4
    assert ws.invalidate() == 1
    assert ws.stats().entries == 0


def test_invalidate_with_a_prefix_and_no_scope_crosses_scopes_on_purpose():
    ws = _seed(WorkingSet())
    # A capability's health is not per person, and neither is forgetting it.
    assert ws.invalidate(prefix=cache.PREFIX_CANDIDATES) == 2


def test_stats_and_clear():
    ws = _seed(WorkingSet())
    ws.get("a|p||", cache.PREFIX_STATE + "mirror")
    ws.get("a|p||", "nothing here")
    stats = ws.stats()
    assert isinstance(stats, CacheStats)
    assert stats.hits == 1 and stats.misses == 1 and stats.entries == 4
    assert stats.hit_rate() == 0.5
    assert stats.to_dict()["entries"] == 4

    ws.clear()
    empty = ws.stats()
    assert (empty.entries, empty.hits, empty.misses, empty.bytes) == (0, 0, 0, 0)


# ── §1.7: the event table ──────────────────────────────────────────────────

def test_an_unknown_event_is_a_no_op_not_an_error(clean_singleton):
    ws = _seed(cache.working_set())
    assert cache.on_event("something_new_shipped", {"owner": "a"}) == 0
    assert ws.stats().entries == 4


def test_project_context_updated_forgets_the_whole_project_including_branches(
        clean_singleton):
    ws = cache.working_set()
    ws.put("luis|p1||", cache.PREFIX_CANDIDATES + "docs", 1)
    ws.put("luis|p1|council-3|", cache.PREFIX_PACKET + "x", 2)
    ws.put("luis|p1||branch-7", cache.PREFIX_STATE + "s", 3)
    ws.put("luis|other||", cache.PREFIX_CANDIDATES + "docs", 4)
    ws.put("someone_else|p1||", cache.PREFIX_CANDIDATES + "docs", 5)

    dropped = cache.on_event("project_context_updated",
                             {"owner": "luis", "project_id": "p1"})
    assert dropped == 3
    assert ws.get("luis|other||", cache.PREFIX_CANDIDATES + "docs") == 4
    assert ws.get("someone_else|p1||", cache.PREFIX_CANDIDATES + "docs") == 5


def test_a_state_projection_only_touches_the_operational_blocks(clean_singleton):
    ws = cache.working_set()
    ws.put("luis|p1||", cache.PREFIX_STATE + "mirror", 1)
    ws.put("luis|p1||", cache.PREFIX_BLOCKS + "ops", 2)
    ws.put("luis|p1||", cache.PREFIX_CANDIDATES + "docs", 3)

    assert cache.on_event("state_projection_updated",
                          {"owner": "luis", "project_id": "p1"}) == 2
    assert ws.get("luis|p1||", cache.PREFIX_CANDIDATES + "docs") == 3


def test_capability_health_is_global_and_only_drops_tool_guidance(clean_singleton):
    ws = cache.working_set()
    ws.put("a|p||", cache.PREFIX_TOOLS + "guide", 1)
    ws.put("b|q||", cache.PREFIX_TOOLS + "guide", 2)
    ws.put("b|q||", cache.PREFIX_CANDIDATES + "docs", 3)

    assert cache.on_event("capability_health_changed", {}) == 2
    assert ws.get("b|q||", cache.PREFIX_CANDIDATES + "docs") == 3


def test_an_event_with_no_owner_or_project_flushes_nothing(clean_singleton):
    ws = _seed(cache.working_set())
    assert cache.on_event("project_context_updated", {}) == 0
    assert cache.on_event("branch_selected", {"timestamp": "now"}) == 0
    assert ws.stats().entries == 4


def test_every_event_in_the_table_is_handled_and_none_raises(clean_singleton):
    for name in cache.EVENT_PREFIXES:
        _seed(cache.working_set())
        assert cache.on_event(name, {"owner": "a", "project_id": "p"}) >= 0
    # A hostile payload is data, not an instruction.
    assert cache.on_event("project_context_updated", "not a mapping") == 0


def test_workspace_stands_in_for_project_id_in_a_payload(clean_singleton):
    ws = cache.working_set()
    ws.put("luis|C:/repo||", cache.PREFIX_CANDIDATES + "docs", 1)
    assert cache.on_event("project_context_detached",
                          {"owner": "luis", "workspace": "C:/repo"}) == 1
    assert ws.stats().entries == 0


# ── the singleton ──────────────────────────────────────────────────────────

def test_working_set_is_one_per_process_and_resettable(clean_singleton):
    first = cache.working_set()
    first.put("s", "k", "v")
    assert cache.working_set() is first
    assert cache.working_set().get("s", "k") == "v"

    cache.reset_working_set()
    assert cache.working_set() is not first
    assert cache.working_set().get("s", "k") is None


def test_the_configured_entry_ceiling_is_read_from_settings(monkeypatch,
                                                            clean_singleton):
    import src.settings as settings_mod

    monkeypatch.setattr(settings_mod, "load_settings",
                        lambda: {"agent_context_cache_entries": 2})
    ws = cache.working_set()
    for name in ("a", "b", "c"):
        ws.put("s", name, name)
    assert ws.stats().entries == 2


def test_a_broken_settings_read_falls_back_to_the_default(monkeypatch,
                                                          clean_singleton):
    import src.settings as settings_mod

    def boom():
        raise RuntimeError("settings file is a directory")

    monkeypatch.setattr(settings_mod, "load_settings", boom)
    assert cache.working_set() is not None


# ── concurrency ────────────────────────────────────────────────────────────

def test_concurrent_writers_do_not_corrupt_the_map():
    """The compiler runs its sources in threads and maintenance runs on
    another; an OrderedDict mutated from both without a lock raises
    'dictionary changed size during iteration' in the middle of a turn."""
    ws = WorkingSet(max_entries=64, max_bytes=1_000_000)
    errors = []

    def worker(index):
        try:
            for n in range(200):
                ws.put(f"scope{index}", f"k{n}", n, cost_bytes=8)
                ws.get(f"scope{index}", f"k{n}")
                ws.stats()
                if n % 50 == 0:
                    ws.invalidate(f"scope{index}", prefix="k1")
        except Exception as exc:  # noqa: BLE001 - the whole point of the test
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert ws.stats().entries <= 64
