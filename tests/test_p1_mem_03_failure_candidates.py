"""MEM-03 — memoria de fallos con promocion controlada.

`src/memory_failures.py` sits on top of `src/memory_engine.py`'s existing
store (rule 4: no second store for "things the agent learned not to do").
Three things the acceptance criterion needs proven:

1. One isolated failure never becomes an active, injectable anti-pattern —
   it stays a candidate (``status="deprecated"``, outside `search()`/`pack()`'s
   default statuses) no matter how it is registered.
2. Promotion needs BOTH a regression-test reference AND (repetition OR
   explicit confirmation) — missing either one blocks it, and the report
   says which.
3. `sweep_expired()` demotes (never deletes) a promoted rule nobody
   reconfirmed within its window — the caducidad half of the requirement.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src import memory_engine as engine
from src import memory_failures as failures

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    yield tmp_path
    engine.reset_vector_store()
    engine.clear_injected()


def test_a_single_failure_stays_a_candidate_never_active(store):
    result = failures.register_failure(
        "deploy script crashes on missing env var", "the deploy script needs FOO_KEY set",
        owner="luis", project="p1", test_ref="tests/test_deploy.py::test_requires_foo_key",
        now=NOW)
    assert result["outcome"] == "registered"
    assert result["item"]["status"] == failures.CANDIDATE_STATUS
    assert result["occurrences"] == 1
    assert "occurrences" in result["blocked_reason"] or "confirmation" in result["blocked_reason"]

    # And a candidate is NOT what default search/pack would ever serve —
    # this is what "no produce una prohibicion global" means mechanically.
    served = engine.scoped_items("luis", "p1")  # default statuses: active, anti_pattern
    assert all(item["id"] != result["item"]["id"] for item in served)


def test_promotion_requires_a_test_ref_even_after_enough_repeats(store):
    sig = "editor tool truncates unicode filenames"
    for _ in range(failures.MIN_OCCURRENCES_TO_PROPOSE):
        result = failures.register_failure(sig, "truncates filenames with emoji",
                                           owner="luis", project="p1", now=NOW)
    assert result["outcome"] == "registered"
    assert "regression test" in result["blocked_reason"]
    assert result["item"]["status"] == failures.CANDIDATE_STATUS


def test_promotion_requires_repetition_or_confirmation_even_with_a_test_ref(store):
    sig = "search tool ignores case-insensitive flag"
    result = failures.register_failure(
        sig, "case-insensitive search flag ignored", owner="luis", project="p1",
        test_ref="tests/test_search.py::test_case_insensitive", now=NOW)
    assert result["outcome"] == "registered"
    assert "occurrences" in result["blocked_reason"]


def test_repetition_plus_test_ref_promotes(store):
    sig = "upload tool double-encodes filenames"
    result = None
    for i in range(failures.MIN_OCCURRENCES_TO_PROPOSE):
        result = failures.register_failure(
            sig, "filenames get double url-encoded on upload", owner="luis", project="p1",
            test_ref="tests/test_upload.py::test_filename_not_double_encoded", now=NOW)
    assert result["outcome"] == "promoted"
    assert result["item"]["status"] == failures.ACTIVE_STATUS
    assert result["item"]["provenance"]["expires_at"]

    # Promoted items ARE served by default retrieval now.
    served_ids = {item["id"] for item in engine.scoped_items("luis", "p1")}
    assert result["item"]["id"] in served_ids


def test_explicit_confirmation_promotes_a_single_occurrence_with_a_test_ref(store):
    sig = "renderer crashes on empty markdown table"
    result = failures.register_failure(
        sig, "empty markdown table crashes the renderer", owner="luis", project="p1",
        test_ref="tests/test_renderer.py::test_empty_table", confirm=True, now=NOW)
    assert result["outcome"] == "promoted"
    assert result["occurrences"] == 1


def test_an_isolated_failure_does_not_ban_it_for_a_different_project(store):
    """The acceptance line itself: one bad run in one project must not
    degrade an unrelated task — here, an unrelated PROJECT."""
    sig = "flaky network retry"
    for _ in range(failures.MIN_OCCURRENCES_TO_PROPOSE):
        failures.register_failure(sig, "network retries exhausted", owner="luis",
                                  project="proj-a",
                                  test_ref="tests/test_retry.py::test_backoff", now=NOW)
    served_b = engine.scoped_items("luis", "proj-b")
    assert all("AVOID: network retries exhausted" != item["text"] for item in served_b)


def test_sweep_expired_demotes_not_deletes(store):
    sig = "linter false positive on f-strings"
    result = None
    for _ in range(failures.MIN_OCCURRENCES_TO_PROPOSE):
        result = failures.register_failure(
            sig, "linter flags valid f-strings", owner="luis", project="p1",
            test_ref="tests/test_lint.py::test_fstring_ok", now=NOW)
    assert result["outcome"] == "promoted"
    item_id = result["item"]["id"]

    later = NOW + timedelta(days=failures.DEFAULT_EXPIRES_DAYS + 1)
    swept = failures.sweep_expired(owner="luis", project="p1", now=later)
    assert swept["demoted"] == 1

    still_there = engine.get_item(item_id)
    assert still_there is not None  # never deleted
    assert still_there["status"] == failures.CANDIDATE_STATUS


def test_list_candidates_only_returns_unpromoted_ones(store):
    failures.register_failure("sig-a", "summary a", owner="luis", project="p1", now=NOW)
    promoted = None
    for _ in range(failures.MIN_OCCURRENCES_TO_PROPOSE):
        promoted = failures.register_failure(
            "sig-b", "summary b", owner="luis", project="p1",
            test_ref="tests/test_b.py::test_b", now=NOW)
    candidates = failures.list_candidates(owner="luis", project="p1")
    texts = {c["text"] for c in candidates}
    assert "AVOID: summary a" in texts
    assert "AVOID: summary b" not in texts  # promoted, no longer a candidate
