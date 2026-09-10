"""L29 (integrates L21's "necessary in src/command_guard.py" note):
`append_receipt()` gains an additive, optional `call_id` kwarg — its own
record field, distinct from `note` (which already carries other free-form
text such as a degraded-tier reason) — so a receipt can be traced back to
the exact tool call it was made for.

Reverting the `call_id` handling makes `test_call_id_lands_on_the_receipt_record`
fail: the returned/stored record has no `call_id` key at all.
`test_call_id_is_optional_and_leaves_the_record_shape_unchanged` pins the
back-compat side (COMUN.md rule 3): omitting it must reproduce exactly what
`tests/test_command_guard.py`'s own receipt tests already assert.
"""
from __future__ import annotations

import json
import os

import pytest

from src import command_guard


@pytest.fixture
def guard_env(tmp_path, monkeypatch):
    monkeypatch.setattr(command_guard, "DATA_DIR", str(tmp_path))
    command_guard._last_hash_cache.clear()
    yield tmp_path
    command_guard._last_hash_cache.clear()


def test_call_id_lands_on_the_receipt_record(guard_env):
    record = command_guard.append_receipt(
        session="s", tool="bash", command="rm -rf x", tier="DANGEROUS",
        rule="fs.rm_force_recursive", action="blocked", call_id="call_xyz",
    )
    assert record["call_id"] == "call_xyz"
    path = os.path.join(str(guard_env), "command_guard_log.jsonl")
    stored = json.loads(open(path, encoding="utf-8").read().splitlines()[-1])
    assert stored["call_id"] == "call_xyz"
    # The chain is still valid with the new field folded in — it is hashed
    # like every other field, not bolted on outside the chain.
    assert command_guard.verify_chain(path) == {"ok": True, "length": 1, "broken_at": None}


def test_call_id_is_optional_and_leaves_the_record_shape_unchanged(guard_env):
    record = command_guard.append_receipt(command="rm a", tier="CAUTION", action="allowed")
    assert "call_id" not in record
