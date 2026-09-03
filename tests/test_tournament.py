"""Tournament — the same prompt to N models blind, then explicit fusion
(src/tournament.py, routes/tournament_routes.py).

What these tests are actually about:

  * **Round 0 is blind.** If any other answer reaches a model in round 0, the
    whole premise (independent first drafts) is gone.
  * **The fusion rounds are anonymous.** A model name is a reputation, and a
    reputation biases the fusion — so no contestant's name may appear in any
    prompt, not even inside another model's answer.
  * **The parallelism is real only across DISTINCT models.** Two entries naming
    the same model must take their turns; two different models must overlap.
  * **A judge score is never invented.** A malformed judgement yields `null`,
    and the ranking that replaces it says out loud that it is deterministic.
  * **One model failing is not the tournament failing** — and a model the user
    stopped is `cancelled`, not an error.

``llm_call`` is injected, so none of it needs a model.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src import tournament


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(tournament, "_data_dir", lambda: str(tmp_path / "tournament"))
    monkeypatch.setattr(tournament, "gpu_slots", lambda url="", override=None: override)
    tournament.reset_for_tests()
    yield
    tournament.reset_for_tests()


class Recorder:
    """An llm_call that remembers every prompt it was given and answers from a
    script keyed by (model, round-ish call index)."""

    def __init__(self, answer=None, fail=(), cancel=()):
        self.prompts = []           # [(model, user text)]
        self.calls = 0
        self._answer = answer or (lambda model, n: f"answer {n} for the task")
        self._fail = set(fail)
        self._cancel = set(cancel)

    async def __call__(self, messages, model):
        user = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        n = sum(1 for m, _ in self.prompts if m == model)
        self.prompts.append((model, user))
        self.calls += 1
        await asyncio.sleep(0)
        if model in self._cancel:
            raise tournament.ModelCancelled("stopped by the user")
        if model in self._fail:
            raise RuntimeError("the endpoint refused the connection")
        return self._answer(model, n)

    def user_prompts_for(self, model):
        return [text for m, text in self.prompts if m == model]


JUDGE_JSON = json.dumps({"scores": [
    {"solution": "A", "correctness": 90, "completeness": 80, "sophistication": 70, "note": "solid"},
    {"solution": "B", "correctness": 40, "completeness": 30, "sophistication": 20, "note": "thin"},
]})


# ── round 0 is blind ────────────────────────────────────────────────────────

async def test_round_zero_is_blind_and_carries_nothing_but_the_task():
    rec = Recorder(answer=lambda model, n: f"{model.split(':')[0]}-secret-{n} lorem ipsum dolor")
    result = await tournament.run("build a CSV parser", ["qwen3.5:9b", "llama3:8b"],
                                  rounds=1, llm_call=rec)
    round0 = [text for _, text in rec.prompts][:2]
    assert len(round0) == 2
    for text in round0:
        assert "build a CSV parser" in text
        # no other answer, and no hint that other answers exist at all
        assert "Solution" not in text and "secret" not in text
        assert tournament.FUSION_INSTRUCTION not in text
    assert result["rounds_run"] == 1
    assert [a["round"] for a in result["answers"]] == [0, 0]
    assert result["answers"][0]["tokens"] > 0
    assert result["answers"][0]["tokens_source"] == "estimated"


async def test_a_reported_token_count_is_not_relabelled_as_an_estimate():
    async def call(messages, model):
        return {"content": "an answer", "output_tokens": 41}
    result = await tournament.run("task", ["a:1b", "b:2b"], rounds=1, llm_call=call)
    assert [a["tokens"] for a in result["answers"]] == [41, 41]
    assert {a["tokens_source"] for a in result["answers"]} == {"reported"}


# ── the fusion rounds carry every answer, anonymised ────────────────────────

async def test_fusion_rounds_carry_all_previous_answers_under_neutral_labels():
    rec = Recorder(answer=lambda model, n: f"round {n} from {model}: alpha beta gamma")
    await tournament.run("write a parser", ["qwen3.5:9b", "llama3:8b", "mistral:7b"],
                         rounds=2, llm_call=rec)
    later = [text for _, text in rec.prompts if tournament.FUSION_INSTRUCTION in text]
    assert len(later) == 3, "every model gets a fusion prompt in round 1"
    for text in later:
        # all three previous answers, under labels, never under names
        assert "--- Solution A ---" in text and "--- Solution B ---" in text
        assert "--- Solution C ---" in text
        assert text.count("--- Solution") == 3
        assert "alpha beta gamma" in text
        assert tournament.FUSION_INSTRUCTION in text
        assert "complementary, not conflicting" in text


async def test_no_contestant_name_ever_reaches_a_prompt_even_through_an_answer():
    """The answers here open with the model's own name — exactly what a chatty
    local model does — and it still must not leak into the next round."""
    names = ["qwen3.5:9b", "llama3:8b"]
    rec = Recorder(answer=lambda model, n: f"I am {model} and here is my answer, take it")
    await tournament.run("a task", names, rounds=3, llm_call=rec)
    for model, text in rec.prompts:
        for name in names:
            assert name not in text, (name, text[:200])
            assert name.split(":")[0] not in text
    # …and what replaced it is neutral, not an empty hole
    fusion = [t for _, t in rec.prompts if tournament.FUSION_INSTRUCTION in t]
    assert fusion and "the model" in fusion[0]


# ── convergence: `rounds` is a maximum ──────────────────────────────────────

async def test_rounds_stop_early_when_the_answers_stop_changing():
    rec = Recorder(answer=lambda model, n: "exactly the same answer every single round")
    result = await tournament.run("t", ["a:1b", "b:2b"], rounds=5, llm_call=rec)
    assert result["stopped_by"] == "convergence"
    assert result["rounds_run"] == 2, "round 0 + one round that changed nothing"
    assert result["convergence"]["converged"] is True
    assert result["convergence"]["models_assessed"] == 2
    assert "converged" in result["convergence"]["reason"] or "changed almost nothing" in result["convergence"]["reason"]


async def test_rounds_do_not_stop_early_while_the_answers_keep_changing():
    words = ["alpha beta gamma delta", "totally different words entirely here now",
             "a third unrelated response with other vocabulary", "and a fourth divergence"]
    rec = Recorder(answer=lambda model, n: words[n % len(words)] * (n + 1))
    result = await tournament.run("t", ["a:1b", "b:2b"], rounds=4, llm_call=rec)
    assert result["stopped_by"] == "rounds"
    assert result["rounds_run"] == 4
    assert result["convergence"] is not None and result["convergence"]["converged"] is False


async def test_convergence_needs_two_answers_before_it_says_anything():
    rec = Recorder()
    result = await tournament.run("t", ["a:1b", "b:2b"], rounds=1, llm_call=rec)
    assert result["convergence"] is None and result["stopped_by"] == "rounds"


# ── a model that fails, and a model the user stopped ────────────────────────

async def test_a_failing_model_is_recorded_and_the_rest_finish():
    rec = Recorder(fail={"broken:1b"})
    result = await tournament.run("t", ["good:9b", "broken:1b"], rounds=2, llm_call=rec)
    assert [e["model"] for e in result["errors"]] == ["broken:1b"]
    assert "refused the connection" in result["errors"][0]["error"]
    assert result["errors"][0]["outcome"] == "expected_error"
    assert result["degraded"] is True
    # the healthy model ran every round and is the only finalist
    assert [a["model"] for a in result["answers"]] == ["good:9b", "good:9b"]
    assert [f["model"] for f in result["final"]] == ["good:9b"]
    # the broken one is never asked again
    assert rec.user_prompts_for("broken:1b") == rec.user_prompts_for("broken:1b")[:1]


async def test_a_model_that_fails_later_keeps_the_answer_it_already_gave():
    state = {"n": 0}

    async def call(messages, model):
        state["n"] += 1
        if model == "flaky:1b" and state["n"] > 2:
            raise RuntimeError("gone")
        return f"an answer from round {state['n']}"
    result = await tournament.run("t", ["solid:9b", "flaky:1b"], rounds=3, llm_call=call)
    finals = {f["model"]: f for f in result["final"]}
    assert set(finals) == {"solid:9b", "flaky:1b"}
    assert finals["flaky:1b"]["round"] == 0 and finals["flaky:1b"]["outcome"] == "error"
    assert finals["solid:9b"]["outcome"] == "success"


async def test_a_cancelled_model_is_cancelled_not_an_error():
    rec = Recorder(cancel={"stopped:1b"})
    result = await tournament.run("t", ["good:9b", "stopped:1b"], rounds=2, llm_call=rec)
    assert result["errors"] == []
    assert [c["model"] for c in result["cancelled"]] == ["stopped:1b"]
    assert result["degraded"] is True
    assert [f["model"] for f in result["final"]] == ["good:9b"]
    kinds = [e["event"] for e in result["events"]]
    assert "model_cancelled" in kinds and "model_error" not in kinds


async def test_cancelling_the_run_stops_it_between_rounds_and_keeps_the_answers():
    cancel = asyncio.Event()
    seen = {"n": 0}

    async def call(messages, model):
        seen["n"] += 1
        if seen["n"] >= 2:                       # both of round 0 answered, then Stop
            cancel.set()
        return f"one answer from {model}, then the user pressed stop"
    result = await tournament.run("t", ["a:1b", "b:2b"], rounds=4, llm_call=call,
                                  cancel_event=cancel)
    assert result["stopped_by"] == "cancelled"
    assert result["rounds_run"] == 1 and len(result["answers"]) == 2
    assert result["errors"] == [] and result["cancelled"] == []
    # a cancelled run is never judged — a half-run tournament is not a verdict
    assert result["judge"] is None and result["ranking"] == "deterministic"
    assert [f["rank"] for f in result["final"]] == [1, 2]


# ── judging: strict JSON in, or nothing ─────────────────────────────────────

async def test_the_judge_scores_every_answer_and_the_ranking_follows_it():
    async def call(messages, model):
        text = "\n".join(m["content"] for m in messages)
        if "Score EVERY solution" in text:
            return JUDGE_JSON
        return f"an answer from {model} about parsers and grammars"
    result = await tournament.run("t", ["first:9b", "second:8b"], rounds=1,
                                  llm_call=call, judge_model="first:9b")
    assert result["judge"]["ok"] is True and result["judge"]["attempts"] == 1
    assert result["ranking"] == "judge" and result["ranking_note"] == ""
    winner, loser = result["final"]
    assert winner["model"] == "first:9b" and winner["rank"] == 1
    assert winner["scores"] == {"correctness": 90, "completeness": 80, "sophistication": 70}
    assert winner["total"] == 240 and winner["note"] == "solid"
    assert loser["model"] == "second:8b" and loser["total"] == 90
    assert result["degraded"] is False


async def test_a_malformed_judgement_is_retried_once_then_reported_as_null():
    seen = {"judge": 0}

    async def call(messages, model):
        text = "\n".join(m["content"] for m in messages)
        if "Score EVERY solution" in text:
            seen["judge"] += 1
            return "Solution A is clearly the best one, I'd give it about 95 out of 100."
        return f"an answer from {model} about parsers"
    result = await tournament.run("t", ["a:9b", "b:8b"], rounds=1, llm_call=call)
    assert seen["judge"] == 2, "one retry, and only one"
    assert result["judge"]["ok"] is False and result["judge"]["scores"] is None
    assert "JSON" in result["judge"]["error"]
    # not one fabricated number anywhere in the finals
    for row in result["final"]:
        assert row["scores"] is None and row["total"] is None
        assert row["rank"] in (1, 2)
    assert "95" not in json.dumps([r["scores"] for r in result["final"]])
    assert result["ranking"] == "deterministic"
    assert "no judge available" in result["ranking_note"]
    assert result["degraded"] is True


async def test_a_judge_that_scores_only_some_answers_leaves_the_others_null():
    partial = json.dumps({"scores": [{"solution": "A", "correctness": 70,
                                      "completeness": 70, "sophistication": 70}]})

    async def call(messages, model):
        text = "\n".join(m["content"] for m in messages)
        return partial if "Score EVERY solution" in text else f"answer from {model}"
    result = await tournament.run("t", ["a:9b", "b:8b"], rounds=1, llm_call=call)
    assert result["ranking"] == "mixed" and "1 of 2" in result["ranking_note"]
    scored = [f for f in result["final"] if f["total"] is not None]
    unscored = [f for f in result["final"] if f["total"] is None]
    assert len(scored) == 1 and len(unscored) == 1
    assert scored[0]["rank"] == 1, "a scored answer outranks an unscored one"
    assert unscored[0]["scores"] is None


async def test_a_judge_that_cannot_be_reached_at_all_is_not_a_failed_run():
    async def call(messages, model):
        text = "\n".join(m["content"] for m in messages)
        if "Score EVERY solution" in text:
            raise RuntimeError("the judge endpoint is down")
        return f"answer from {model}"
    result = await tournament.run("t", ["a:9b", "b:8b"], rounds=1, llm_call=call)
    assert result["judge"]["ok"] is False and result["judge"]["attempts"] == 1
    assert "down" in result["judge"]["error"]
    assert result["ranking"] == "deterministic"
    assert [f["rank"] for f in result["final"]] == [1, 2]


@pytest.mark.parametrize("raw,expected", [
    (JUDGE_JSON, {"A": 240, "B": 90}),
    ("```json\n" + JUDGE_JSON + "\n```", {"A": 240, "B": 90}),
    ("Here you go:\n" + JUDGE_JSON + "\nThat's my call.", {"A": 240, "B": 90}),
    (json.dumps({"A": {"correctness": 1, "completeness": 2, "sophistication": 3},
                 "B": {"correctness": 4, "completeness": 5, "sophistication": 6}}),
     {"A": 6, "B": 15}),
    (json.dumps([{"solution": "Solution A", "correctness": 10, "completeness": 10,
                  "sophistication": 10}]), {"A": 30}),
])
def test_parse_judgement_reads_the_shapes_models_actually_send(raw, expected):
    scores = tournament.parse_judgement(raw, ["A", "B"])
    assert scores is not None
    assert {k: v["total"] for k, v in scores.items()} == expected


@pytest.mark.parametrize("raw", ["", None, "no json here at all", "{", "[]", "{}",
                                 json.dumps({"scores": []}),
                                 json.dumps({"scores": [{"solution": "Z", "correctness": 5}]})])
def test_parse_judgement_returns_none_rather_than_a_guess(raw):
    assert tournament.parse_judgement(raw, ["A", "B"]) is None


def test_a_non_numeric_axis_is_null_and_an_out_of_range_one_is_clamped():
    raw = json.dumps({"scores": [
        {"solution": "A", "correctness": "excellent", "completeness": 80, "sophistication": 70},
        {"solution": "B", "correctness": 150, "completeness": -20, "sophistication": 50}]})
    scores = tournament.parse_judgement(raw, ["A", "B"])
    assert scores["A"]["correctness"] is None and scores["A"]["total"] is None
    assert scores["A"]["completeness"] == 80
    assert scores["B"] == {"correctness": 100, "completeness": 0, "sophistication": 50,
                           "total": 150, "note": ""}


# ── the deterministic tiebreak ──────────────────────────────────────────────

def test_the_tiebreak_prefers_the_answer_that_covers_the_others():
    hybrid = ("parsing tokenizer grammar recursion backtracking whitespace escaping "
              "quoting delimiters newline handling errors")
    narrow = "quoting delimiters"
    other = "recursion backtracking whitespace escaping"
    rows = tournament.tiebreak_scores([hybrid, narrow, other])
    assert rows[0]["score"] > rows[1]["score"] and rows[0]["score"] > rows[2]["score"]
    assert rows[0]["coverage"] == 1.0
    assert 0.0 <= rows[1]["length_percentile"] <= 1.0
    assert [r["chars"] for r in rows] == [len(hybrid), len(narrow), len(other)]


def test_the_tiebreak_orders_answers_when_there_is_no_judge_at_all():
    async def call(messages, model):
        text = "\n".join(m["content"] for m in messages)
        if "Score EVERY solution" in text:
            raise RuntimeError("no judge")
        return {"a:9b": "parsing tokenizer grammar recursion escaping quoting delimiters",
                "b:8b": "quoting"}.get(model, "x")
    result = asyncio.run(tournament.run("t", ["a:9b", "b:8b"], rounds=1, llm_call=call))
    assert result["ranking"] == "deterministic"
    assert [f["model"] for f in result["final"]] == ["a:9b", "b:8b"]
    assert [f["rank"] for f in result["final"]] == [1, 2]
    assert result["final"][0]["tiebreak"] > result["final"][1]["tiebreak"]


def test_the_tiebreak_is_total_on_junk():
    assert tournament.tiebreak_scores(None) == []
    assert tournament.tiebreak_scores([]) == []
    one = tournament.tiebreak_scores(["only one"])
    assert one[0]["coverage"] == 1.0 and one[0]["length_percentile"] == 0.5
    assert tournament.tiebreak_scores([None, ""])[0]["score"] >= 0.0


# ── the scheduler: same model serialises, different models overlap ──────────

class FakeSem:
    """A semaphore that remembers how many holders it ever had at once."""

    def __init__(self, size=99):
        self.size = size
        self.held = 0
        self.peak = 0
        self.acquired = 0
        self._waiters = []

    async def acquire(self):
        while self.held >= self.size:
            ev = asyncio.Event()
            self._waiters.append(ev)
            await ev.wait()
        self.held += 1
        self.acquired += 1
        self.peak = max(self.peak, self.held)

    def release(self):
        self.held -= 1
        if self._waiters:
            self._waiters.pop(0).set()


class Concurrency:
    """An llm_call that measures how many calls are in flight, per model and
    overall. Two `await` points guarantee the tasks interleave."""

    def __init__(self):
        self.now = 0
        self.peak = 0
        self.per_model = {}
        self.peak_per_model = {}

    async def __call__(self, messages, model):
        self.now += 1
        self.peak = max(self.peak, self.now)
        self.per_model[model] = self.per_model.get(model, 0) + 1
        self.peak_per_model[model] = max(self.peak_per_model.get(model, 0), self.per_model[model])
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.now -= 1
        self.per_model[model] -= 1
        return f"an answer from {model}"


async def test_two_entries_on_one_model_serialise_while_two_models_overlap():
    """The measurement this whole scheduler exists for: two requests to the
    SAME model queue behind its one slot; two DIFFERENT models really do
    generate at the same time."""
    clock = Concurrency()
    sem = FakeSem(size=99)
    result = await tournament.run("t", ["same:9b", "same:9b", "other:8b"], rounds=1,
                                  llm_call=clock, slots=sem)
    assert clock.peak_per_model["same:9b"] == 1, "the same model never generated twice at once"
    assert clock.peak >= 2, "two different models did overlap"
    assert sem.acquired >= 3 and sem.peak >= 2
    assert sem.held == 0, "every slot was given back"
    assert len(result["final"]) == 3


async def test_the_shared_gpu_semaphore_is_what_bounds_the_overlap():
    clock = Concurrency()
    sem = FakeSem(size=1)
    await tournament.run("t", ["a:9b", "b:8b", "c:7b"], rounds=1, llm_call=clock, slots=sem)
    assert clock.peak == 1, "one GPU slot means one generation at a time, whatever the models"
    assert sem.peak == 1 and sem.held == 0


async def test_a_failing_call_gives_back_the_slot_and_the_model_lock():
    sem = FakeSem(size=1)
    rec = Recorder(fail={"broken:1b"})
    await tournament.run("t", ["broken:1b", "fine:9b"], rounds=2, llm_call=rec, slots=sem)
    assert sem.held == 0
    assert not tournament.model_lock("broken:1b").locked()


# ── determinism ─────────────────────────────────────────────────────────────

def test_the_same_seed_produces_the_same_prompts_and_the_same_ranking():
    def once(seed):
        # each model writes something recognisably its own, so a different
        # label permutation really does change the fusion prompts
        words = {"a:9b": "alpha", "b:8b": "beta", "c:7b": "gamma"}
        rec = Recorder(answer=lambda model, n: f"answer {n} {words.get(model, 'x')} words words")
        result = asyncio.run(
            tournament.run("t", ["a:9b", "b:8b", "c:7b"], rounds=2, llm_call=rec, seed=seed))
        return [t for _, t in rec.prompts], [f["model"] for f in result["final"]]

    first_prompts, first_rank = once(1234)
    again_prompts, again_rank = once(1234)
    other_prompts, _ = once(4321)
    assert first_prompts == again_prompts
    assert first_rank == again_rank
    assert any("--- Solution A ---" in p for p in first_prompts)
    # a different seed permutes the labels, so the anonymised order differs
    assert first_prompts != other_prompts


# ── every entry point is defensive ──────────────────────────────────────────

async def test_bad_input_is_a_clear_error_not_a_crash():
    async def call(messages, model):
        return "x"
    for prompt, models, kwargs in [
        ("", ["a:1b", "b:2b"], {}),
        ("   ", ["a:1b", "b:2b"], {}),
        ("t", ["only:1b"], {}),
        ("t", [], {}),
        ("t", None, {}),
        ("t", ["a:1b"] * 9, {}),
    ]:
        with pytest.raises(tournament.TournamentError):
            await tournament.run(prompt, models, llm_call=call, **kwargs)
    with pytest.raises(tournament.TournamentError):
        await tournament.run("t", ["a:1b", "b:2b"], llm_call=None)


async def test_out_of_range_rounds_are_clamped_not_refused():
    rec = Recorder(answer=lambda model, n: f"different answer {n} " * (n + 1))
    for asked, expected in [(0, 1), (-5, 1), (99, tournament.MAX_ROUNDS), ("nope", None)]:
        result = await tournament.run("t", ["a:1b", "b:2b"], rounds=asked, llm_call=rec)
        assert result["rounds"] == (expected if expected is not None else tournament.DEFAULT_ROUNDS)


def test_the_pure_helpers_never_raise_on_junk():
    assert tournament.anonymize(None, None) == []
    assert tournament.anonymize([{"entry": 0, "answers": []}], ["a"]) == []
    assert tournament.assess_convergence(None) is None
    assert tournament.assess_convergence([{"answers": [{"text": "one"}]}]) is None
    assert tournament.label_for(-1).startswith("S")
    assert tournament.label_for(0) == "A" and tournament.label_for(99) == "S99"
    assert tournament.strongest(None) == "" and tournament.strongest([None, ""]) == ""
    assert tournament.strongest(["small:7b", "big:70b", "mid:9b"]) == "big:70b"
    assert tournament.strongest(["nameless", "alsonone"]) == ""
    assert tournament.key_terms(None) == set()
    assert tournament.estimate_tokens(None) == 0
    assert tournament.synthesis_prompt("", []) == ""       # nothing to merge, no prompt
    assert tournament.synthesis_prompt("t", [{"text": "  "}]) == ""
    assert tournament.build_round0_messages(None)[1]["content"] == ""
    assert tournament.build_fusion_messages("t", None, 1)[1]["content"]
    assert tournament.build_judge_messages("t", None)[1]["content"]
    assert tournament._answer_of(None) == ("", None)
    assert tournament._answer_of({"text": "x", "tokens": "seven"}) == ("x", None)
    assert tournament._answer_of(["a", "b"]) == ("a", None)


def test_the_merge_prompt_carries_the_finalists_and_the_fusion_instruction():
    prompt = tournament.synthesis_prompt("build a parser", [
        {"model": "a:9b", "text": "the first answer", "rank": 1, "total": 240},
        {"model": "b:8b", "text": "the second answer", "rank": 2, "total": None},
        {"model": "c:7b", "text": "   ", "rank": 3},
    ])
    assert "build a parser" in prompt
    assert "--- Solution A (ranked 1, judged 240/300) ---" in prompt
    assert "--- Solution B (ranked 2) ---" in prompt
    assert "the first answer" in prompt and "the second answer" in prompt
    assert "Solution C" not in prompt          # an empty answer is not a finalist
    assert tournament.FUSION_INSTRUCTION in prompt
    assert "a:9b" not in prompt and "b:8b" not in prompt


async def test_on_event_is_called_for_every_state_change_and_never_breaks_a_run():
    seen = []

    def sink(event):
        seen.append(event["event"])
        if event["event"] == "answer":
            raise RuntimeError("the UI blew up")
    rec = Recorder()
    result = await tournament.run("t", ["a:1b", "b:2b"], rounds=1, llm_call=rec, on_event=sink)
    assert result["answers"], "a broken event sink does not lose the answers"
    for kind in ("start", "round_start", "model_start", "answer", "round_end",
                 "judge_start", "judge", "ranked"):
        assert kind in seen, kind
    assert [e["event"] for e in result["events"]][0] == "start"


async def test_a_one_argument_llm_call_still_works():
    """The expert_review shape: a function already bound to one model."""
    calls = []

    async def bound(messages):
        calls.append(messages)
        return "an answer from the bound model"
    result = await tournament.run("t", ["a:1b", "b:2b"], rounds=1, llm_call=bound)
    assert len(result["answers"]) == 2 and calls


async def test_a_synchronous_llm_call_still_works():
    result = await tournament.run("t", ["a:1b", "b:2b"], rounds=1,
                                  llm_call=lambda messages, model: f"sync answer from {model}")
    assert len(result["answers"]) == 2


# ── the background run, its mirror and its rotation ─────────────────────────

def _fake_llm(answer="an answer with several words in it"):
    async def call(messages, model):
        text = "\n".join(m["content"] for m in messages)
        if "Score EVERY solution" in text:
            return JUDGE_JSON
        return f"{answer} from {model}"
    return call


async def test_a_started_run_finishes_persists_and_can_be_read_back():
    run_obj = await tournament.start("luis", {"prompt": "a task", "models": ["a:9b", "b:8b"],
                                              "rounds": 1}, llm_call=_fake_llm())
    assert run_obj.status in ("queued", "running")
    assert await tournament.wait(run_obj, 5)
    assert run_obj.status == "done"
    data = tournament.summary(run_obj)
    assert data["result"]["final"][0]["rank"] == 1
    assert data["result"]["ranking"] == "judge"
    # the JSON mirror is readable after the run left memory
    tournament._runs.clear()
    again = tournament.get(run_obj.id)
    assert again is not None and again.status == "done"
    assert again.result["final"][0]["model"] == run_obj.result["final"][0]["model"]
    assert tournament.get("nope") is None and tournament.get("") is None


async def test_a_run_that_was_running_at_a_restart_reads_back_as_interrupted():
    run_obj = await tournament.start("luis", {"prompt": "t", "models": ["a:9b", "b:8b"]},
                                     llm_call=_fake_llm())
    run_obj.status = "running"
    run_obj._persist()
    tournament._runs.clear()
    again = tournament.get(run_obj.id)
    assert again.status == "interrupted" and "restart" in again.error
    run_obj.task.cancel()
    await asyncio.sleep(0.05)


async def test_the_listing_is_owner_scoped_and_names_the_winner():
    mine = await tournament.start("luis", {"prompt": "mine", "models": ["a:9b", "b:8b"],
                                           "rounds": 1}, llm_call=_fake_llm())
    theirs = await tournament.start("eve", {"prompt": "theirs", "models": ["a:9b", "b:8b"],
                                            "rounds": 1}, llm_call=_fake_llm())
    assert await tournament.wait(mine, 5) and await tournament.wait(theirs, 5)
    rows = tournament.list_runs("luis")
    assert [r["prompt"] for r in rows] == ["mine"]
    assert rows[0]["winner"] and rows[0]["ranking"] == "judge"
    assert len(tournament.list_runs(None)) == 2      # single-user mode sees everything
    assert tournament.visible_to(mine, "eve") is False


async def test_cancelling_a_run_keeps_what_it_had_and_settles_it():
    gate = asyncio.Event()

    async def call(messages, model):
        await gate.wait()
        return "an answer"
    run_obj = await tournament.start("luis", {"prompt": "t", "models": ["a:9b", "b:8b"],
                                              "rounds": 3}, llm_call=call)
    await asyncio.sleep(0.02)
    assert tournament.cancel(run_obj) is True
    assert run_obj.status == "cancelling"
    gate.set()
    assert await tournament.wait(run_obj, 5)
    assert run_obj.status == "cancelled"
    assert run_obj.result["stopped_by"] == "cancelled"
    assert tournament.cancel(run_obj) is False       # a finished run cannot be cancelled again


async def test_start_validates_the_body_before_it_spends_a_gpu():
    for body in [None, {}, {"prompt": "  "}, {"prompt": "t"},
                 {"prompt": "t", "models": ["one:1b"]},
                 {"prompt": "t", "models": ["a:1b", "b:2b"], "rounds": "many"}]:
        with pytest.raises(tournament.TournamentError):
            await tournament.start("luis", body, llm_call=_fake_llm())


async def test_the_settings_switch_and_the_model_cap_are_read_from_settings(monkeypatch):
    values = {"agent_tournament": False, "agent_tournament_max_models": 2}
    monkeypatch.setattr(tournament, "_setting", lambda key, default: values.get(key, default))
    assert tournament.enabled() is False
    assert tournament.max_models() == 2
    values["agent_tournament_max_models"] = 99
    assert tournament.max_models() == tournament.HARD_MAX_MODELS
    values["agent_tournament_max_models"] = "nonsense"
    assert tournament.max_models() == tournament.DEFAULT_MAX_MODELS


# ── the HTTP routes ─────────────────────────────────────────────────────────

_OPEN_CLIENTS = []


@pytest.fixture(autouse=True)
def _close_clients():
    yield
    while _OPEN_CLIENTS:
        try:
            _OPEN_CLIENTS.pop().__exit__(None, None, None)
        except Exception:
            pass


def _client(monkeypatch, *, user="luis"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.middleware import require_admin
    import routes.tournament_routes as tr

    app = FastAPI()
    app.dependency_overrides[require_admin] = lambda: None
    monkeypatch.setattr(tr, "effective_user", lambda request: user)
    monkeypatch.setattr(tournament, "default_llm_call", lambda owner=None, **kw: _fake_llm())
    app.include_router(tr.setup_tournament_routes())
    client = TestClient(app).__enter__()
    _OPEN_CLIENTS.append(client)
    return client


def test_the_route_runs_a_tournament_and_answers_the_status(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/api/tournament", json={"prompt": "a task", "models": ["a:9b", "b:8b"],
                                        "rounds": 1})
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]
    got = c.get(f"/api/tournament/{run_id}/wait?timeout=5").json()
    assert got["status"] == "done"
    assert got["result"]["final"][0]["rank"] == 1
    assert got["result"]["ranking"] == "judge"
    assert got["result"]["merge_prompt"].startswith("Here are the final answers")
    # the listing, the config and the events
    assert c.get("/api/tournament").json()["runs"][0]["id"] == run_id
    cfg = c.get("/api/tournament/config").json()
    assert cfg["axes"] == list(tournament.AXES) and cfg["min_models"] == 2
    evs = c.get(f"/api/tournament/{run_id}/events").json()["events"]
    assert evs and evs[0]["event"] == "start"
    # another user does not see it
    other = _client(monkeypatch, user="eve")
    assert other.get(f"/api/tournament/{run_id}").status_code == 404
    assert other.get("/api/tournament").json()["runs"] == []


def test_the_route_refuses_bad_bodies_with_a_reason(monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/api/tournament", json={"models": ["a:9b", "b:8b"]}).status_code == 400
    r = c.post("/api/tournament", json={"prompt": "t", "models": ["one:1b"]})
    assert r.status_code == 400 and "at least" in r.json()["detail"]
    assert c.post("/api/tournament", content=b"not json").status_code == 400
    assert c.post("/api/tournament", json=["a"]).status_code == 400
    assert c.get("/api/tournament/zzzzzzzzzzzz").status_code == 404


def test_the_route_refuses_to_start_while_the_feature_is_off(monkeypatch):
    c = _client(monkeypatch)
    monkeypatch.setattr(tournament, "enabled", lambda: False)
    r = c.post("/api/tournament", json={"prompt": "t", "models": ["a:9b", "b:8b"]})
    assert r.status_code == 400 and "switched off" in r.json()["detail"]
    # …but what is already recorded stays readable
    assert c.get("/api/tournament").status_code == 200


def test_cancel_over_http(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.middleware import require_admin
    import routes.tournament_routes as tr

    gate = asyncio.Event()

    async def slow(messages, model):
        await gate.wait()
        return "an answer"
    app = FastAPI()
    app.dependency_overrides[require_admin] = lambda: None
    monkeypatch.setattr(tr, "effective_user", lambda request: "luis")
    monkeypatch.setattr(tournament, "default_llm_call", lambda owner=None, **kw: slow)
    app.include_router(tr.setup_tournament_routes())
    with TestClient(app) as c:
        run_id = c.post("/api/tournament", json={"prompt": "t", "models": ["a:9b", "b:8b"],
                                                 "rounds": 3}).json()["id"]
        out = c.post(f"/api/tournament/{run_id}/cancel").json()
        assert out["cancelled"] is True and out["status"] == "cancelling"
        gate.set()
        done = c.get(f"/api/tournament/{run_id}/wait?timeout=5").json()
        assert done["status"] == "cancelled"
        assert done["result"]["stopped_by"] == "cancelled"


def test_robot_mode_projects_the_run_into_flat_rows(monkeypatch):
    c = _client(monkeypatch)
    run_id = c.post("/api/tournament", json={"prompt": "a task about parsers",
                                             "models": ["a:9b", "b:8b"], "rounds": 1}).json()["id"]
    c.get(f"/api/tournament/{run_id}/wait?timeout=5")
    env = c.get(f"/api/tournament/{run_id}?robot=1").json()
    assert env["ok"] is True and env["error_code"] is None
    data = env["data"]
    assert data["id"] == run_id and data["ranking"] == "judge"
    assert data["prompt_chars"] == len("a task about parsers")
    assert "prompt" not in data, "the coordinator sent the prompt; it does not need it back"
    # uniform, all-scalar rows — exactly TOON's tabular case
    keys = {tuple(sorted(row)) for row in data["final"]}
    assert len(keys) == 1
    for row in data["final"] + data["answers"]:
        assert all(not isinstance(v, (dict, list)) for v in row.values())
    assert data["final"][0]["correctness"] == 90
    # the same read as TOON text
    toon = c.get(f"/api/tournament/{run_id}?format=toon")
    assert toon.status_code == 200 and "text/plain" in toon.headers["content-type"]
    assert "final" in toon.text
    # events project too
    evs = c.get(f"/api/tournament/{run_id}/events?robot=1").json()["data"]
    assert {tuple(sorted(row)) for row in evs["events"]} and evs["id"] == run_id


def test_robot_projection_of_a_shape_it_did_not_expect_comes_back_unchanged():
    import routes.tournament_routes as tr
    assert tr.lean_status("not a payload") == "not a payload"
    assert tr.lean_events(None) is None
    assert tr.lean_status({"result": "not a dict"})["final"] == []
    assert tr.lean_events({"events": "nope"})["events"] == []
    assert tr.lean_status({})["final"] == [] and tr.lean_status({})["answers"] == []


def test_the_event_stream_ends_with_an_end_frame(monkeypatch):
    """Progress frames are UNNAMED and the terminal one is named `end` — the
    same shape /api/dispatch/{id}/events?stream=1 uses.

    This is not cosmetic: a frame carrying an `event: <name>` line never reaches
    `EventSource.onmessage`, only a listener registered for that exact name. Two
    SSE endpoints in one app disagreeing about it means a page written against
    one silently receives nothing from the other.
    """
    c = _client(monkeypatch)
    run_id = c.post("/api/tournament", json={"prompt": "t", "models": ["a:9b", "b:8b"],
                                             "rounds": 1}).json()["id"]
    c.get(f"/api/tournament/{run_id}/wait?timeout=5")
    r = c.get(f"/api/tournament/{run_id}/events?stream=1")
    assert r.status_code == 200 and "text/event-stream" in r.headers["content-type"]
    # progress frames reach onmessage: a bare `data:` line, no `event:` before it
    assert "\ndata: " in ("\n" + r.text)
    assert "event: event" not in r.text
    # exactly one named frame, the terminal one
    assert r.text.count("event: ") == 1 and "event: end" in r.text
    assert '"status": "done"' in r.text
