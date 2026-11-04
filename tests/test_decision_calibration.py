"""Calibrated thresholds for typed decisions: the Clopper-Pearson bound, the threshold search, the store."""
import asyncio
import json

import pytest

from src import decision_calibration as dc


def test_bound_with_no_errors_matches_the_closed_form():
    # 0 errors in n: upper bound = 1 - delta**(1/n)
    for n in (10, 59, 200):
        assert dc.clopper_pearson_upper(0, n, 0.05) == pytest.approx(1 - 0.05 ** (1 / n), abs=1e-4)


def test_bound_grows_with_errors_and_shrinks_with_samples():
    assert dc.clopper_pearson_upper(2, 100) > dc.clopper_pearson_upper(1, 100) > dc.clopper_pearson_upper(0, 100)
    assert dc.clopper_pearson_upper(2, 400) < dc.clopper_pearson_upper(2, 100)
    assert dc.clopper_pearson_upper(5, 5) == 1.0
    assert dc.clopper_pearson_upper(0, 0) == 1.0
    assert dc.clopper_pearson_upper("x", 5) == 1.0


def _samples():
    out = []
    for i in range(120):  # confident and right
        out.append({"confidence": 0.9 + (i % 10) / 100, "correct": True})
    for i in range(60):  # middling and mostly wrong
        out.append({"confidence": 0.6 + (i % 10) / 100, "correct": i % 3 == 0})
    return out


def test_calibrate_picks_the_lowest_threshold_that_keeps_the_bound():
    r = dc.calibrate(_samples(), max_error=0.05, delta=0.05)
    assert r["ok"] is True
    assert 0.85 <= r["threshold"] <= 0.9
    assert r["upper_bound"] <= 0.05
    assert r["errors"] == 0 and r["accepted"] == 120
    assert r["coverage"] == round(120 / 180, 4)


def test_calibrate_refuses_a_sample_too_small_to_prove_anything():
    r = dc.calibrate([{"confidence": 0.99, "correct": True}] * 20)
    assert r["ok"] is False and r["threshold"] is None
    assert "too few" in r["reason"]


def test_calibrate_refuses_a_model_that_is_not_accurate_enough():
    r = dc.calibrate([{"confidence": 0.9, "correct": i % 4 != 0} for i in range(400)], max_error=0.05)
    assert r["ok"] is False and "no threshold" in r["reason"]


def test_calibrate_ignores_junk_and_accepts_pairs():
    r = dc.calibrate([[0.99, True]] * 80 + [None, "x", {"confidence": 7, "correct": True}, [0.5]])
    assert r["ok"] is True and r["samples"] == 80
    assert dc.calibrate([])["reason"] == "no usable samples"
    assert dc.calibrate(_samples(), max_error=2)["ok"] is False


def test_store_roundtrip_and_threshold_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "DATA_DIR", str(tmp_path))
    result = dc.calibrate(_samples())
    assert dc.threshold_for("is_greeting") is None
    saved = dc.save("is_greeting", result)
    assert saved["ok"] and dc.threshold_for("is_greeting") == result["threshold"]
    assert "is_greeting" in dc.all_calibrations()
    assert dc.save("", result)["ok"] is False
    assert dc.save("x", {"ok": False, "reason": "nope"})["ok"] is False
    assert dc.forget("is_greeting") is True and dc.forget("is_greeting") is False
    assert dc.threshold_for("is_greeting") is None


def test_audit_draw(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "DATA_DIR", str(tmp_path))
    dc.save("q", dc.calibrate(_samples(), audit_rate=0.1))
    assert dc.should_audit("q", roll=0.05) is True
    assert dc.should_audit("q", roll=0.5) is False
    assert dc.should_audit("other", roll=0.0) is False


def test_decide_uses_the_calibrated_threshold(tmp_path, monkeypatch):
    from src import typed_decision

    monkeypatch.setattr(dc, "DATA_DIR", str(tmp_path))
    dc.save("q", {"ok": True, "threshold": 0.95, "accepted": 1, "errors": 0, "upper_bound": 0.01,
                  "coverage": 1, "samples": 1, "max_error": 0.05, "delta": 0.05, "audit_rate": 0.02})
    seen = {}
    real = typed_decision.interpret

    def spy(fld, data, wire, *, min_confidence, min_mass, ms=0.0, model=""):
        seen["min_confidence"] = min_confidence
        return real(fld, data, wire, min_confidence=min_confidence, min_mass=min_mass, ms=ms, model=model)

    monkeypatch.setattr(typed_decision, "interpret", spy)
    monkeypatch.setattr(typed_decision, "enabled", lambda: True)
    monkeypatch.setattr(typed_decision, "_prepare", lambda purpose, owner: {
        "wire": "openai", "url": "http://x/v1/chat/completions", "model": "m", "headers": {}})

    async def fake_post(url, payload, headers, timeout):
        return {"choices": [{"logprobs": {"content": [{"top_logprobs": [
            {"token": "A", "logprob": -0.1}, {"token": "B", "logprob": -2.5}]}]}, "message": {"content": "A"}}]}

    monkeypatch.setattr(typed_decision, "_post", fake_post)
    fld = typed_decision.Field(name="q", question="Is it?", choices=["yes", "no"])
    asyncio.run(typed_decision.decide("text", [fld]))
    assert seen["min_confidence"] == 0.95
    asyncio.run(typed_decision.decide("text", [fld], min_confidence=0.5))
    assert seen["min_confidence"] == 0.5


def test_mcp_server_tools(tmp_path, monkeypatch):
    from mcp_servers import decision_calibration_server as srv

    monkeypatch.setattr(dc, "DATA_DIR", str(tmp_path))
    out = asyncio.run(srv.call_tool("decision_calibrate", {"samples": _samples(), "apply": True, "field": "f"}))
    payload = json.loads(out[0].text)
    assert payload["ok"] and payload["saved"]["ok"]
    listed = json.loads(asyncio.run(srv.call_tool("decision_calibrations", {}))[0].text)
    assert "f" in listed["calibrations"]
    assert "needs samples" in asyncio.run(srv.call_tool("decision_calibrate", {}))[0].text


def test_a_large_noisy_sample_is_calibrated_in_reasonable_time():
    import random
    import time
    rnd = random.Random(3)
    samples = [(rnd.random(), rnd.random() > 0.2) for _ in range(3000)]  # 20 % wrong everywhere
    t0 = time.monotonic()
    out = dc.calibrate(samples)
    assert time.monotonic() - t0 < 3.0  # it took 37 s with a bisection per threshold
    assert out["ok"] is False


def test_bound_check_agrees_with_the_bisected_bound():
    for errors, n in ((0, 59), (0, 58), (1, 100), (3, 200), (10, 500), (7, 40)):
        for max_error in (0.02, 0.05, 0.1, 0.3):
            holds = dc._bound_holds(errors, n, max_error, 0.05)
            assert holds == (dc.clopper_pearson_upper(errors, n, 0.05) <= max_error + 1e-6) or \
                abs(dc.clopper_pearson_upper(errors, n, 0.05) - max_error) < 1e-5


def test_the_native_provider_also_uses_the_calibrated_threshold(monkeypatch):
    import asyncio
    from src import systemone_decision as sd
    from src import typed_decision as td

    seen = {}

    async def fake_native(context, fields, *, budget, min_confidence, instructions="", owner=None,
                          field_threshold=None):
        seen["cut"] = field_threshold("q") if field_threshold else None
        return {}

    monkeypatch.setattr(sd, "decide_native", fake_native)
    monkeypatch.setattr(td, "_setting", lambda k: "ollaya" if k == "typed_decision_provider" else td.DEFAULTS.get(k))
    monkeypatch.setattr(td, "enabled", lambda: True)
    monkeypatch.setattr(td, "_calibrated_threshold", lambda name: 0.42)
    asyncio.run(td.decide("ctx", [td.Field("q", "which?", ["a", "b"])]))
    assert seen["cut"] == 0.42
    seen.clear()
    asyncio.run(td.decide("ctx", [td.Field("q", "which?", ["a", "b"])], min_confidence=0.9))
    assert seen["cut"] is None  # the caller's own threshold wins