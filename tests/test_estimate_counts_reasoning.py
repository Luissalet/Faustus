"""estimate_tokens counts reasoning echoed back on assistant turns (Qwen3
templates render it), and calibration ratios learned against the older
estimator are dropped instead of inflating every later estimate."""

import json
import os

import pytest

from src import token_calibration as tc
from src.model_context import estimate_tokens


def test_reasoning_content_is_counted():
    plain = [{"role": "assistant", "content": "ok"}]
    with_reasoning = [{"role": "assistant", "content": "ok", "reasoning_content": "r" * 10_000}]
    assert estimate_tokens(with_reasoning) - estimate_tokens(plain) == 3_000


def test_non_string_reasoning_is_ignored():
    assert estimate_tokens([{"role": "assistant", "content": "ok", "reasoning_content": None}]) == \
        estimate_tokens([{"role": "assistant", "content": "ok"}])


@pytest.fixture
def calib_file(tmp_path, monkeypatch):
    path = os.path.join(str(tmp_path), "token_calibration.json")
    monkeypatch.setattr(tc, "_file_path", lambda: path)
    tc._reset_for_tests()
    yield path
    tc._reset_for_tests()


def test_ratios_from_the_old_estimator_are_dropped(calib_file):
    with open(calib_file, "w", encoding="utf-8") as f:
        json.dump({
            "stale-model": {"ratio_ema": 2.17, "samples": 1702, "last_update": 1},
            "fresh-model": {"ratio_ema": 0.9, "samples": 40, "last_update": 1,
                            "estimator": tc.ESTIMATOR_VERSION},
        }, f)
    assert tc.factor("stale-model") == 1.0
    assert tc.get_info("stale-model")["samples"] == 0
    assert tc.factor("fresh-model") == 0.9


def test_new_samples_are_stamped_and_survive_a_reload(calib_file):
    msgs = [{"role": "user", "content": "x" * 3000}]
    tc.observe(model="m", request_messages=msgs, usage={"input_tokens": estimate_tokens(msgs)})
    tc.observe(model="m", request_messages=msgs, usage={"input_tokens": estimate_tokens(msgs)})
    tc.flush_for_tests()
    with open(calib_file, encoding="utf-8") as f:
        assert json.load(f)["m"]["estimator"] == tc.ESTIMATOR_VERSION
    tc._reset_for_tests()
    assert tc.get_info("m")["samples"] == 2


def test_ledger_shows_reasoning_on_its_own_line():
    from src.context_ledger import build_ledger
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "ok", "reasoning_content": "r" * 10_000},
    ]
    ledger = build_ledger(msgs)
    by = {s["key"]: s["tokens"] for s in ledger["sections"]}
    assert by["reasoning"] == 3_000
    assert ledger["total"] == estimate_tokens(msgs)
