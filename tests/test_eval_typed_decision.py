"""scripts/eval_typed_decision.py runs offline with --fake (the CI mode) and
the labelled cases it reads are well formed. The live mode needs a model
server and is run by hand (see docs/evals/typed-decisions.md)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "eval_typed_decision.py"
CASES = REPO_ROOT / "docs" / "evals" / "typed_decision_cases.json"

_spec = importlib.util.spec_from_file_location("eval_typed_decision", SCRIPT)
evalmod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("eval_typed_decision", evalmod)
_spec.loader.exec_module(evalmod)  # type: ignore[union-attr]


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    from src import typed_decision
    yield
    typed_decision._TRANSPORT = None


def test_cases_are_labelled_and_bilingual():
    from src.brain.entities import TYPES
    data = json.loads(CASES.read_text(encoding="utf-8"))
    fresh, ents = data["freshness"], data["entity_types"]
    assert len(fresh) >= 60 and len(ents) >= 40
    assert {c["lang"] for c in fresh} == {"es", "en"} == {c["lang"] for c in ents}
    assert all(isinstance(c["label"], bool) and c["text"].strip() for c in fresh)
    assert 0.3 < sum(c["label"] for c in fresh) / len(fresh) < 0.7
    assert all(c["type"] in TYPES and c["name"] in c["sentence"] for c in ents)


def test_fake_run_reports_every_measure(capsys):
    assert evalmod.main(["--fake", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    fr, et = report["tasks"]["freshness"], report["tasks"]["entity_types"]
    assert fr["n"] == 60 and et["n"] == 40
    for task in (fr, et):
        assert set(task["accuracy"]) == {"rule", "typed", "combined"}
        assert task["calibration"] and task["latency_ms"]["first_p50"] is not None
        assert task["methods"] == {"logprobs": task["n"]}
    assert fr["latency_ms"]["second_p50"] is not None
    assert fr["cached_tokens_mean"]["second"] > fr["cached_tokens_mean"]["first"]
    assert fr["sanity_language_field_accuracy"] == 1.0
    # the combined behaviour never changes a case the rule was confident about
    for row in report["rows"]["freshness"]:
        if row["rule_confident"]:
            assert row["combined"] == row["rule"]
    # entity typing only ever moves "other" to a confident type
    for row in report["rows"]["entity_types"]:
        assert row["combined"] == "other" or row["accepted"]


def test_fake_run_is_deterministic(capsys):
    evalmod.main(["--fake", "--json"])
    first = json.loads(capsys.readouterr().out)
    evalmod.main(["--fake", "--json"])
    second = json.loads(capsys.readouterr().out)
    assert first["tasks"]["freshness"]["accuracy"] == second["tasks"]["freshness"]["accuracy"]
    assert [r["typed"] for r in first["rows"]["entity_types"]] == \
           [r["typed"] for r in second["rows"]["entity_types"]]


def test_markdown_output(capsys):
    assert evalmod.main(["--fake", "--markdown", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "| freshness | 5 |" in out and "Calibration" in out


def test_a_model_that_is_not_loaded_is_not_loaded(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("src.typed_decision.residency_reason",
                        lambda url, model: calls.append((url, model)) or "model_not_resident")
    assert evalmod.main(["--url", "http://127.0.0.1:11434", "--model", "some-model:8b"]) == 2
    assert calls and "not loaded" in capsys.readouterr().err
