from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from src import approval_store
from src.contracts import Approval
from tests.test_approval_store import store, PLAN  # noqa: F401


@pytest.mark.parametrize('uses', [1, 2])
def test_two_workers_cannot_spend_the_same_use(store, monkeypatch, uses):
    card = approval_store.request(PLAN, owner='alice', uses=uses)
    approval_store.decide(card.id, granted=True, by='alice')
    gate = threading.Barrier(2)
    seen = threading.local()
    original = Approval.covers
    def simultaneous(self, plan, **kwargs):
        result = original(self, plan, **kwargs)
        if not getattr(seen, 'waited', False):
            seen.waited = True
            gate.wait(timeout=5)
        return result
    monkeypatch.setattr(Approval, 'covers', simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approval_store.consume, card.id, PLAN) for _ in range(2)]
        results = [f.result(timeout=10) for f in futures]
    assert sum(r['ok'] for r in results) == uses
    assert approval_store.get(card.id).uses_left == 0
    assert approval_store.get(card.id).status == 'consumed'
