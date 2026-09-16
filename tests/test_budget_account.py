"""src/budget_account.py — the A31 per-run token/cost ledger."""
from __future__ import annotations

from src import budget_account as ba


def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(ba, "default_path", lambda: tmp_path / "budget.sqlite3")


def test_snapshot_of_unopened_run_is_empty_not_an_error(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    snap = ba.snapshot("no-such-run")
    assert snap["opened"] is False
    assert snap["ceiling_tokens"] == 0
    assert snap["remaining_tokens"] is None  # no ceiling → no computed remainder
    assert snap["consumed_cost"] == 0.0
    assert snap["children"] == []


def test_reserve_rejected_before_it_would_exceed_the_ceiling(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    ba.open("run1", ceiling_tokens=1000)
    r1 = ba.reserve("run1", "childA", tokens=700)
    assert isinstance(r1, ba.Reservation)
    r2 = ba.reserve("run1", "childB", tokens=400)  # 700 + 400 > 1000
    assert isinstance(r2, ba.BudgetExceeded)
    assert r2.available_tokens == 300
    assert "budget exceeded" in r2.reason
    # The rejected reservation left no row: a third, smaller ask still fits.
    r3 = ba.reserve("run1", "childC", tokens=300)
    assert isinstance(r3, ba.Reservation)


def test_zero_ceiling_never_rejects_accounting_only(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    ba.open("run2", ceiling_tokens=0)
    r = ba.reserve("run2", "childA", tokens=10_000_000)
    assert isinstance(r, ba.Reservation)
    snap = ba.snapshot("run2")
    assert snap["remaining_tokens"] is None


def test_reconcile_releases_the_surplus_and_reports_real_usage(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    ba.open("run3", ceiling_tokens=1000)
    ba.reserve("run3", "childA", tokens=900)
    snap_before = ba.snapshot("run3")
    assert snap_before["reserved_tokens"] == 900
    assert snap_before["remaining_tokens"] == 100
    ba.reconcile("run3", "childA", used_tokens=150, used_cost=0.002)
    snap_after = ba.snapshot("run3")
    assert snap_after["reserved_tokens"] == 0
    assert snap_after["consumed_tokens"] == 150
    assert snap_after["remaining_tokens"] == 1000 - 150   # surplus (900-150) freed
    assert snap_after["consumed_cost"] == 0.002
    assert snap_after["unpriced_usage_tokens"] == 0


def test_unpriced_usage_never_reads_as_a_known_zero_cost(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    ba.open("run4", ceiling_tokens=0)
    ba.reserve("run4", "childA", tokens=500)
    ba.reconcile("run4", "childA", used_tokens=500, used_cost=None)  # provider gave no price
    snap = ba.snapshot("run4")
    assert snap["unpriced_usage_tokens"] == 500
    assert snap["consumed_cost"] == "unknown"   # not 0.0


def test_reconcile_without_a_prior_reserve_still_counts(tmp_path, monkeypatch):
    """A retry or a caller that skipped `reserve` must not vanish from the
    ledger — the sum a ceiling check compares against includes it."""
    _isolated(tmp_path, monkeypatch)
    ba.open("run5", ceiling_tokens=1000)
    ba.reconcile("run5", "retryA", used_tokens=200, used_cost=None)
    snap = ba.snapshot("run5")
    assert snap["consumed_tokens"] == 200
    assert snap["remaining_tokens"] == 800


def test_release_frees_a_reservation_with_no_usage_recorded(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    ba.open("run6", ceiling_tokens=1000)
    ba.reserve("run6", "childA", tokens=900)
    ba.release("run6", "childA")
    snap = ba.snapshot("run6")
    assert snap["reserved_tokens"] == 0
    assert snap["consumed_tokens"] == 0
    assert snap["remaining_tokens"] == 1000


def test_open_is_idempotent_a_second_call_does_not_widen_the_ceiling(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    ba.open("run7", ceiling_tokens=500)
    ba.open("run7", ceiling_tokens=999999)
    snap = ba.snapshot("run7")
    assert snap["ceiling_tokens"] == 500
