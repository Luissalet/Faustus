"""CTX-04 — cache correctness and reindexing.

Two things this lote's `src/context_source_versions.py` and
`src/context_engine/cache.py` (`compose_cache_key`) must prove:

1. A source that edits (or is revoked) invalidates the working-set entries
   scoped to it — `on_event("source_version_changed", ...)` — and does so
   without leaking across projects/owners (`WorkingSet`'s mandatory scope
   argument, already pinned by `test_context_engine_cache.py`, is exercised
   again here at the level a real caller uses it).
2. The composite cache key changes when ANY of model/tokenizer/template/
   policy/user/source-version changes — a stale entry cannot be reused just
   because five of the six matched.

Revert either change (see the report) and `test_edit_invalidates_scoped_cache_only`
and `test_key_changes_with_every_dimension` both fail.
"""

import pytest

from src.context_engine import cache
from src import context_source_versions as versions


@pytest.fixture()
def ce_store(tmp_path):
    from src.context_engine import store
    store.use_path(str(tmp_path / "ce.db"))
    cache.reset_working_set()
    try:
        yield
    finally:
        store.use_path(None)
        cache.reset_working_set()


def test_first_sighting_is_not_a_change(ce_store):
    result = versions.record_seen("file:a.txt", "hash1", owner="u1", project_id="p1")
    assert result["changed"] is False


def test_edit_invalidates_scoped_cache_only(ce_store):
    ws = cache.working_set()
    scope_a = "u1|p1|c|b"
    scope_b = "u2|p2|c|b"
    ws.put(scope_a, cache.PREFIX_CANDIDATES + "file:a.txt", "stale-candidates-a")
    ws.put(scope_b, cache.PREFIX_CANDIDATES + "file:a.txt", "unrelated-b")

    versions.record_seen("file:a.txt", "hash1", owner="u1", project_id="p1")
    assert ws.get(scope_a, cache.PREFIX_CANDIDATES + "file:a.txt") == "stale-candidates-a"

    # The file was edited: same ref, new hash.
    result = versions.record_seen("file:a.txt", "hash2", owner="u1", project_id="p1")
    assert result["changed"] is True
    assert result["previous_revision"] == "hash1"

    # Project p1's cached candidates for that ref are gone...
    assert ws.get(scope_a, cache.PREFIX_CANDIDATES + "file:a.txt") is None
    # ...but project p2's unrelated entry (rule 3: no cross-project leak) is not.
    assert ws.get(scope_b, cache.PREFIX_CANDIDATES + "file:a.txt") == "unrelated-b"


def test_revoke_invalidates_and_marks_inaccessible(ce_store):
    ws = cache.working_set()
    scope = "u1|p1|c|b"
    ws.put(scope, cache.PREFIX_PACKET + "pkt1", "old-packet")
    versions.record_seen("file:secret.txt", "hash1", owner="u1", project_id="p1")

    assert versions.is_accessible("file:secret.txt", owner="u1", project_id="p1")
    assert versions.revoke("file:secret.txt", owner="u1", project_id="p1") is True
    assert not versions.is_accessible("file:secret.txt", owner="u1", project_id="p1")
    assert ws.get(scope, cache.PREFIX_PACKET + "pkt1") is None

    # A re-seen ref with an unchanged hash is still reported changed once
    # revoked, because the earlier grant is what actually moved.
    result = versions.record_seen("file:secret.txt", "hash1", owner="u1", project_id="p1")
    assert result["changed"] is True


def test_revoking_an_unknown_ref_still_records_the_revocation(ce_store):
    # revoke() is an explicit act — "this ref is no longer shared" — so it
    # takes effect even for a ref this store never saw a hash for; "harmless"
    # means it never raises, not that it is silently ignored.
    assert versions.revoke("file:never-seen.txt", owner="u1", project_id="p1") is True
    assert not versions.is_accessible("file:never-seen.txt", owner="u1", project_id="p1")
    # A ref genuinely never mentioned has no opinion recorded either way.
    assert versions.is_accessible("file:truly-unmentioned.txt", owner="u1", project_id="p1")


# ── PERF-03: the composite key ──────────────────────────────────────────────

def test_key_changes_with_every_dimension():
    base = cache.compose_cache_key(model="m1", tokenizer="t1", template_version="v1",
                                   policy_version="pol1", user="alice",
                                   source_version="h1")
    variants = [
        cache.compose_cache_key(model="m2", tokenizer="t1", template_version="v1",
                                policy_version="pol1", user="alice", source_version="h1"),
        cache.compose_cache_key(model="m1", tokenizer="t2", template_version="v1",
                                policy_version="pol1", user="alice", source_version="h1"),
        cache.compose_cache_key(model="m1", tokenizer="t1", template_version="v2",
                                policy_version="pol1", user="alice", source_version="h1"),
        cache.compose_cache_key(model="m1", tokenizer="t1", template_version="v1",
                                policy_version="pol2", user="alice", source_version="h1"),
        cache.compose_cache_key(model="m1", tokenizer="t1", template_version="v1",
                                policy_version="pol1", user="bob", source_version="h1"),
        cache.compose_cache_key(model="m1", tokenizer="t1", template_version="v1",
                                policy_version="pol1", user="alice", source_version="h2"),
    ]
    assert len(set(variants) | {base}) == len(variants) + 1, \
        "every single-dimension change must produce a distinct key"
    assert base == cache.compose_cache_key(
        model="m1", tokenizer="t1", template_version="v1",
        policy_version="pol1", user="alice", source_version="h1"), \
        "identical inputs must be the same key (a cache hit)"
    assert base.startswith(cache.PREFIX_RESPONSE)


def test_changing_an_option_param_also_changes_the_key():
    a = cache.compose_cache_key(model="m1", params={"temperature": 0.2})
    b = cache.compose_cache_key(model="m1", params={"temperature": 0.9})
    assert a != b


def test_cached_or_compute_does_not_reuse_a_stale_entry_after_a_param_change(ce_store):
    calls = []

    def compute(tag):
        calls.append(tag)
        return f"answer-for-{tag}"

    scope = "u1|p1|c|b"
    key_v1 = cache.compose_cache_key(model="m1", policy_version="v1", user="u1")
    key_v2 = cache.compose_cache_key(model="m1", policy_version="v2", user="u1")

    value1, cached1 = cache.cached_or_compute(scope, key_v1, lambda: compute("v1"))
    assert value1 == "answer-for-v1" and cached1 is False
    value1b, cached1b = cache.cached_or_compute(scope, key_v1, lambda: compute("v1-again"))
    assert value1b == "answer-for-v1" and cached1b is True  # served from cache

    # Changing the policy (a permission/template change, PERF-03's own
    # example) is a different key, so it is computed fresh — never the old
    # policy's answer.
    value2, cached2 = cache.cached_or_compute(scope, key_v2, lambda: compute("v2"))
    assert value2 == "answer-for-v2" and cached2 is False
    assert calls == ["v1", "v2"]


def test_cached_or_compute_never_leaks_across_scope():
    cache.reset_working_set()
    key = cache.compose_cache_key(model="m1", user="alice")
    v_alice, _ = cache.cached_or_compute("alice|proj", key, lambda: "alice's answer")
    v_bob, cached_bob = cache.cached_or_compute("bob|proj", key, lambda: "bob's answer")
    assert v_alice == "alice's answer"
    assert v_bob == "bob's answer"
    assert cached_bob is False  # same key, different scope: never a hit
    cache.reset_working_set()
