"""The expert review pipeline — typed span deltas, corpus anchoring, and the
honesty rule.

The point being tested throughout: a correction is never rewritten prose, it
never silently disappears (a rejected one carries its reason), and it may only
claim to come from a book when a deterministic check says the cited chunk
actually supports it. A page number is copied or it is "page unknown"; it is
never invented.

``services.experts`` is written by another agent against a fixed contract, so
every test here installs a stub for it. Nothing in this file needs a model:
``review()`` takes its ``llm_call`` injected.
"""

import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import expert_review as er  # noqa: E402


# ── the corpus stub (the contract with services/experts.py) ───────────

BLOCK_TEXT = (
    "[C1] El diálogo debe llevar la escena: evita los adverbios en las "
    "atribuciones, que delatan al autor.\n"
    "[C2] Las descripciones largas frenan el ritmo de un capítulo de acción."
)

CHUNKS = {
    "ch-1": {"source": "Brenner - El oficio.pdf", "page": 42,
             "start_line": 10, "end_line": 12,
             "excerpt": "evita los adverbios en las atribuciones"},
    # A chunk whose page is unknown: it must never render as a number.
    "ch-2": {"source": "Notas del taller.md", "page": None,
             "start_line": 1, "end_line": 4,
             "excerpt": "las descripciones largas frenan el ritmo"},
}


def _block(chunk_ids=("ch-1", "ch-2"), degraded=False, text=BLOCK_TEXT):
    return {"text": text, "chunk_ids": list(chunk_ids), "degraded": degraded}


@pytest.fixture()
def experts(monkeypatch):
    """A stub `services.experts` honouring the documented contract, installed
    in sys.modules so the lazy import inside src.expert_review picks it up
    whether or not the real module exists in this tree yet."""
    calls = {"blocks": [], "feedback": []}

    def load_expert(slug):
        if slug != "brenner_bot":
            return None
        return {"slug": "brenner_bot", "name": "Brenner bot",
                "instructions": "Corrige como Brenner: sin adornos.",
                "rubric": ["Adverbios en las atribuciones",
                           "Ritmo de la escena"],
                "model": "qwen3.5:9b", "temperature": 0.2, "top_p": 0.9}

    def expert_block(slug, query, char_budget):
        calls["blocks"].append((slug, query, char_budget))
        return _block()

    def search(slug, query, k):
        return {"hits": [], "tier": "exact", "degraded": False}

    def citation(slug, chunk_id):
        return dict(CHUNKS.get(chunk_id) or {})

    def record_feedback(slug, accepted, rejected):
        calls["feedback"].append((slug, accepted, rejected))

    module = types.ModuleType("services.experts")
    module.load_expert = load_expert
    module.expert_block = expert_block
    module.search = search
    module.citation = citation
    module.record_feedback = record_feedback
    monkeypatch.setitem(sys.modules, "services.experts", module)
    # `import services.experts as m` binds the ATTRIBUTE of the parent package,
    # which the real module already set on `services` the first time anything
    # imported it. Patching sys.modules alone therefore stubs nothing once
    # tests/test_experts.py has run in the same session — the stub has to
    # shadow the attribute too.
    import services as _services_pkg
    monkeypatch.setattr(_services_pkg, "experts", module, raising=False)
    module.calls = calls
    return module


@pytest.fixture()
def no_experts(monkeypatch):
    """The store is not there at all — the not-configured path."""
    monkeypatch.setattr(er, "_experts_module", lambda: None)


ORIGINAL = ("Marta caminaba lentamente hacia la puerta. "
            "—Vete —dijo ella furiosamente.")


# ── parsing: every shape a model actually emits ───────────────────────


def _one(payload):
    return er.parse_corrections(payload, ORIGINAL, ["ch-1"])


def _delta_json(**over):
    row = {"op": "EDIT", "start": 6, "end": 25, "quote": "caminaba lentamente",
           "replacement": "se arrastraba", "rationale": "adverbio innecesario",
           "rule": "Adverbios", "severity": "medium", "cite": ["C1"],
           "confidence": 0.8}
    row.update(over)
    return row


def test_the_documented_block_parses():
    payload = ":::deltas\n" + json.dumps([_delta_json(end=26)]) + "\n:::"
    result = _one(payload)
    assert not result["rejected"]
    delta = result["deltas"][0]
    assert delta["id"] == "D1" and delta["op"] == "EDIT"
    assert ORIGINAL[delta["span"]["start"]:delta["span"]["end"]] == "caminaba lentamente"
    assert delta["replacement"] == "se arrastraba"
    assert delta["severity"] == "medium" and delta["rule"] == "Adverbios"


@pytest.mark.parametrize("wrap", [
    lambda body: body,                                        # bare array
    lambda body: f"```json\n{body}\n```",                     # fenced
    lambda body: f"Aquí van:\n```\n{body}\n```\nEso es todo.",
    lambda body: f":::deltas\n{body}\n:::",
    lambda body: json.dumps({"deltas": json.loads(body)}),     # wrapped object
])
def test_every_accepted_shape_yields_the_same_delta(wrap):
    body = json.dumps([_delta_json(end=26)])
    result = er.parse_corrections(wrap(body), ORIGINAL, ["ch-1"])
    assert len(result["deltas"]) == 1 and not result["rejected"]
    assert result["deltas"][0]["replacement"] == "se arrastraba"


def test_trailing_commas_and_stray_fences_are_repaired():
    payload = "```json\n[\n" + json.dumps(_delta_json(end=26)) + ",\n]\n```"
    result = er.parse_corrections(payload, ORIGINAL, ["ch-1"])
    assert len(result["deltas"]) == 1


def test_a_single_delta_block_parses():
    payload = ":::delta" + json.dumps(_delta_json(end=26)) + ":::"
    assert len(er.parse_corrections(payload, ORIGINAL, ["ch-1"])["deltas"]) == 1


def test_a_span_object_is_accepted_instead_of_flat_offsets():
    row = _delta_json()
    row["span"] = {"start": row.pop("start"), "end": row.pop("end")}
    result = er.parse_corrections(json.dumps([row]), ORIGINAL, ["ch-1"])
    assert result["deltas"][0]["span"] == {"start": 6, "end": 25}
    assert result["deltas"][0]["relocated"] is False


def test_prose_around_the_answer_does_not_stop_the_parse():
    payload = ("Creo que hay una sola corrección.\n"
               + json.dumps(_delta_json(end=26))
               + "\nEspero que sirva.")
    assert len(er.parse_corrections(payload, ORIGINAL, ["ch-1"])["deltas"]) == 1


def test_nothing_to_correct_is_an_empty_result_not_an_error():
    assert er.parse_corrections(":::deltas\n[]\n:::", ORIGINAL, ["ch-1"]) == {
        "deltas": [], "rejected": []}
    assert er.parse_corrections("No he encontrado nada.", ORIGINAL, ["ch-1"]) == {
        "deltas": [], "rejected": []}


def test_parse_never_raises_on_junk():
    for junk in (None, "", 42, "{{{{", "```json\n{oops\n```", ["x"]):
        out = er.parse_corrections(junk, ORIGINAL, ["ch-1"])
        assert set(out) == {"deltas", "rejected"}


# ── validation: every rejection reason, and none of them silent ───────


def test_an_out_of_bounds_span_is_rejected_with_its_reason():
    row = _delta_json(start=5000, end=5010)
    result = _one(json.dumps([row]))
    assert result["deltas"] == []
    assert "outside the text" in result["rejected"][0]["reason"]
    assert result["rejected"][0]["id"] == "D1"


def test_a_reversed_span_is_rejected():
    row = _delta_json(start=26, end=7)
    result = _one(json.dumps([row]))
    assert "is after end" in result["rejected"][0]["reason"]


def test_a_quote_that_matches_nothing_is_rejected():
    row = _delta_json(quote="corría a toda velocidad")
    result = _one(json.dumps([row]))
    assert result["deltas"] == []
    assert "not found elsewhere" in result["rejected"][0]["reason"]


def test_an_ambiguous_quote_is_rejected_rather_than_guessed():
    text = "Dijo que sí. Dijo que sí. Y se fue."
    row = _delta_json(start=0, end=12, quote="Dijo que sí")
    result = er.parse_corrections(json.dumps([row]), text, ["ch-1"])
    assert result["deltas"] == []
    assert "ambiguous (2 occurrences)" in result["rejected"][0]["reason"]


def test_an_edit_without_a_quote_is_refused():
    row = _delta_json()
    row.pop("quote")
    result = _one(json.dumps([row]))
    assert "missing the quote" in result["rejected"][0]["reason"]


def test_an_unknown_op_is_rejected_by_name():
    result = _one(json.dumps([_delta_json(op="REWRITE_ALL")]))
    assert "unknown op 'REWRITE_ALL'" in result["rejected"][0]["reason"]


def test_an_add_with_nothing_to_insert_is_rejected():
    result = _one(json.dumps([{"op": "ADD", "start": 5, "end": 5,
                               "replacement": "", "rationale": "x"}]))
    assert "nothing to insert" in result["rejected"][0]["reason"]


def test_the_offsets_are_believed_only_when_the_quote_confirms_them():
    """Models miscount offsets constantly. When the quote occurs exactly once
    the span moves to it, and the move is recorded rather than hidden."""
    row = _delta_json(start=0, end=8, quote="caminaba lentamente")
    result = _one(json.dumps([row]))
    delta = result["deltas"][0]
    assert delta["relocated"] is True
    assert delta["span"] == {"start": ORIGINAL.index("caminaba lentamente"),
                             "end": ORIGINAL.index("caminaba lentamente") + 19}
    assert ORIGINAL[delta["span"]["start"]:delta["span"]["end"]] == delta["quote"]
    assert any("relocated" in n for n in delta["notes"])


def test_a_kill_keeps_no_replacement_and_an_add_keeps_no_span():
    payload = json.dumps([
        {"op": "KILL", "start": 7, "end": 26, "quote": "caminaba lentamente",
         "replacement": "algo", "rationale": "sobra", "severity": "low"},
        {"op": "ADD", "start": 0, "end": 5, "replacement": "Entonces ",
         "rationale": "hace falta una transición", "severity": "low"},
    ])
    result = _one(payload)
    kill = next(d for d in result["deltas"] if d["op"] == "KILL")
    add = next(d for d in result["deltas"] if d["op"] == "ADD")
    assert kill["replacement"] == "" and any("KILL carried" in n for n in kill["notes"])
    assert add["span"]["start"] == add["span"]["end"] == 0


def test_an_add_anchored_by_a_quote_lands_after_it():
    row = {"op": "ADD", "quote": "hacia la puerta", "replacement": " de la cocina",
           "rationale": "concreta el lugar", "severity": "low"}
    result = _one(json.dumps([row]))
    delta = result["deltas"][0]
    point = ORIGINAL.index("hacia la puerta") + len("hacia la puerta")
    assert delta["span"] == {"start": point, "end": point}


def test_overlapping_spans_keep_the_more_severe_one():
    payload = json.dumps([
        _delta_json(start=7, end=26, severity="low", confidence=0.9),
        _delta_json(start=16, end=26, quote="lentamente", replacement="",
                    severity="high", confidence=0.4, op="KILL"),
    ])
    result = _one(payload)
    assert [d["id"] for d in result["deltas"]] == ["D2"]
    assert result["rejected"][0]["id"] == "D1"
    assert result["rejected"][0]["reason"] == "overlaps D2"


def test_the_surviving_set_is_non_overlapping_and_sorted():
    payload = json.dumps([
        _delta_json(start=44, end=48, quote="Vete", replacement="Márchate"),
        _delta_json(start=7, end=26, quote="caminaba lentamente",
                    replacement="se arrastraba"),
    ])
    result = _one(payload)
    starts = [d["span"]["start"] for d in result["deltas"]]
    assert starts == sorted(starts)
    for a, b in zip(result["deltas"], result["deltas"][1:]):
        assert a["span"]["end"] <= b["span"]["start"]


def test_adjacent_spans_do_not_count_as_overlapping():
    text = "uno dos tres"
    payload = json.dumps([
        {"op": "EDIT", "start": 0, "end": 3, "quote": "uno", "replacement": "1",
         "rationale": "a"},
        {"op": "EDIT", "start": 4, "end": 7, "quote": "dos", "replacement": "2",
         "rationale": "b"},
    ])
    result = er.parse_corrections(payload, text, ["ch-1"])
    assert len(result["deltas"]) == 2 and not result["rejected"]


# ── applying ──────────────────────────────────────────────────────────


def test_apply_is_a_round_trip_through_the_accepted_ids():
    payload = json.dumps([
        _delta_json(start=7, end=26, quote="caminaba lentamente",
                    replacement="se arrastraba"),
        _delta_json(start=44, end=48, quote="Vete", replacement="Márchate"),
    ])
    deltas = _one(payload)["deltas"]
    both = er.apply_deltas(ORIGINAL, deltas)
    assert "se arrastraba" in both and "Márchate" in both
    assert "caminaba lentamente" not in both

    only_first = er.apply_deltas(ORIGINAL, deltas, ["D1"])
    assert "se arrastraba" in only_first and "Vete" in only_first
    assert er.apply_deltas(ORIGINAL, deltas, []) == ORIGINAL


def test_apply_is_right_to_left_so_later_spans_stay_valid():
    text = "AAAA BBBB CCCC"
    deltas = [
        {"id": "D1", "op": "EDIT", "span": {"start": 0, "end": 4},
         "replacement": "a much longer first word"},
        {"id": "D2", "op": "EDIT", "span": {"start": 5, "end": 9},
         "replacement": "b"},
        {"id": "D3", "op": "KILL", "span": {"start": 10, "end": 14},
         "replacement": "ignored"},
    ]
    assert er.apply_deltas(text, deltas) == "a much longer first word b "


def test_apply_handles_adjacent_and_zero_width_spans():
    text = "uno dos"
    deltas = [
        {"id": "D1", "op": "ADD", "span": {"start": 0, "end": 0}, "replacement": ">"},
        {"id": "D2", "op": "EDIT", "span": {"start": 0, "end": 3}, "replacement": "1"},
        {"id": "D3", "op": "ADD", "span": {"start": 7, "end": 7}, "replacement": "!"},
    ]
    assert er.apply_deltas(text, deltas) == ">1 dos!"


def test_apply_skips_a_broken_delta_instead_of_corrupting_the_text():
    text = "uno dos"
    deltas = [
        {"id": "D1", "op": "EDIT", "span": {"start": 99, "end": 120}, "replacement": "x"},
        {"id": "D2", "op": "EDIT", "span": {"start": "?", "end": 3}, "replacement": "x"},
        "not a delta",
        {"id": "D3", "op": "EDIT", "span": {"start": 0, "end": 3}, "replacement": "1"},
    ]
    assert er.apply_deltas(text, deltas) == "1 dos"
    assert er.apply_deltas(None, None) == ""


# ── anchoring: the honesty rule ───────────────────────────────────────


def _anchor(rationale, markers=("C1",), rule=""):
    delta = {"id": "D1", "op": "EDIT", "rationale": rationale, "rule": rule,
             "markers": list(markers)}
    return er.verify_anchoring(delta, ["ch-1", "ch-2"], "brenner_bot",
                               chunk_texts=er.split_block_excerpts(BLOCK_TEXT),
                               lookup=lambda slug, cid: dict(CHUNKS.get(cid) or {}))


def test_the_block_markers_map_to_the_chunk_ids_in_order():
    assert er.block_markers(["ch-1", "ch-2"]) == {"C1": "ch-1", "C2": "ch-2"}
    assert er.split_block_excerpts(BLOCK_TEXT)["C2"].startswith("Las descripciones")


def test_a_correction_the_chunk_states_literally_is_anchored():
    verdict = _anchor("los adverbios en las atribuciones delatan al autor")
    assert verdict["anchored"] is True
    assert verdict["anchor_layer"] == "literal"
    assert verdict["confidence"] == er.CONFIDENCE_BY_LAYER["literal"]
    assert verdict["label"] == er.LABEL_CORPUS
    assert verdict["citations"][0]["ref"] == "Brenner - El oficio.pdf, page 42"


def test_a_looser_overlap_still_anchors_but_at_the_fuzzy_layer():
    verdict = _anchor("las atribuciones con adverbios cansan al lector moderno")
    assert verdict["anchored"] is True and verdict["anchor_layer"] == "fuzzy"
    assert verdict["confidence"] == er.CONFIDENCE_BY_LAYER["fuzzy"]


def test_a_correction_the_chunk_does_not_support_is_the_models_opinion():
    verdict = _anchor("el punto de vista cambia de cabeza a mitad de escena")
    assert verdict["anchored"] is False and verdict["anchor_layer"] == "none"
    assert verdict["label"] == er.LABEL_OPINION
    # Not dropped — the user may still want it — but its citation is marked.
    assert verdict["citations"][0]["supports"] is False


def test_a_hallucinated_marker_can_never_pass_as_the_corpus():
    verdict = _anchor("los adverbios en las atribuciones delatan al autor",
                      markers=("C9",))
    assert verdict["anchored"] is False
    assert verdict["anchor_layer"] == "unknown_marker"
    assert verdict["label"] == er.LABEL_OPINION
    assert verdict["citations"][0]["known"] is False
    assert verdict["citations"][0]["chunk_id"] is None


def test_no_citation_at_all_is_the_models_opinion():
    verdict = _anchor("me suena mejor así", markers=())
    assert verdict["anchored"] is False and verdict["anchor_layer"] == "no_citations"
    assert verdict["citations"] == []


def test_an_empty_rationale_cannot_anchor_anything():
    assert _anchor("")["anchored"] is False


def test_a_chunk_without_a_page_renders_page_unknown_and_never_a_number():
    delta = {"id": "D1", "op": "EDIT", "markers": ["C2"],
             "rationale": "las descripciones largas frenan el ritmo del capítulo"}
    verdict = er.verify_anchoring(delta, ["ch-1", "ch-2"], "brenner_bot",
                                  chunk_texts=er.split_block_excerpts(BLOCK_TEXT),
                                  lookup=lambda slug, cid: dict(CHUNKS.get(cid) or {}))
    citation = verdict["citations"][0]
    assert verdict["anchored"] is True
    assert citation["page"] is None
    assert citation["page_label"] == er.PAGE_UNKNOWN
    assert citation["ref"] == "Notas del taller.md, page unknown"
    assert not any(ch.isdigit() for ch in citation["ref"])


def test_anchoring_survives_a_citation_lookup_that_explodes():
    def boom(slug, chunk_id):
        raise RuntimeError("the corpus is on fire")

    delta = {"id": "D1", "op": "EDIT", "markers": ["C1"],
             "rationale": "los adverbios en las atribuciones delatan al autor"}
    verdict = er.verify_anchoring(delta, ["ch-1"], "brenner_bot",
                                  chunk_texts=er.split_block_excerpts(BLOCK_TEXT),
                                  lookup=boom)
    assert verdict["anchored"] is True
    assert verdict["citations"][0]["page_label"] == er.PAGE_UNKNOWN


# ── the prompt ────────────────────────────────────────────────────────


def test_the_prompt_carries_the_rubric_the_markers_and_the_standing_rules(experts):
    expert = er.load_expert("brenner_bot")
    messages = er.build_review_prompt(expert, ORIGINAL, _block(), "Marta: ojos verdes")
    system, user = messages[0]["content"], messages[1]["content"]
    assert "Brenner bot" in system and "sin adornos" in system
    assert "1. Adverbios en las atribuciones" in system
    assert "2. Ritmo de la escena" in system
    assert "own judgement" in system
    assert "[C1]" in user and "Marta: ojos verdes" in user
    assert ORIGINAL in user and "quote is REQUIRED" in user


def test_a_pass_with_no_corpus_says_so_in_the_prompt():
    user = er.build_review_prompt({"name": "x"}, ORIGINAL, _block(text="", chunk_ids=[]),
                                  "")[1]["content"]
    assert "No reference passages" in user and "your own" in user


# ── review(), end to end, with a fake model ───────────────────────────

_TEXT_MARKER = "TEXT TO REVIEW (character offsets start at 0):\n"


def _chunk_from(messages):
    body = messages[1]["content"].split(_TEXT_MARKER, 1)[1]
    return body[: -(len(er.DELTA_FORMAT_HELP) + 2)]


def _answering(*rows_per_call):
    """A fake llm_call that answers with real offsets into the chunk it was
    handed, so the merge back into document offsets is really exercised."""
    seen = {"n": 0}

    async def _call(messages):
        chunk = _chunk_from(messages)
        rows = rows_per_call[min(seen["n"], len(rows_per_call) - 1)]
        seen["n"] += 1
        out = []
        for row in rows:
            row = dict(row)
            quote = row.get("quote") or ""
            # A quote the chunk does not contain keeps bogus offsets — that is
            # exactly the delta the validator has to catch.
            index = max(0, chunk.find(quote))
            row.setdefault("start", index if quote else 0)
            row.setdefault("end", index + len(quote) if quote else 0)
            out.append(row)
        return ":::deltas\n" + json.dumps(out, ensure_ascii=False) + "\n:::"

    _call.calls = seen
    return _call


async def test_review_returns_a_mixed_batch_with_the_honesty_labels(experts):
    llm = _answering([
        {"op": "EDIT", "quote": "caminaba lentamente", "replacement": "se arrastraba",
         "rationale": "los adverbios en las atribuciones delatan al autor",
         "rule": "Adverbios", "severity": "high", "cite": ["C1"], "confidence": 0.9},
        {"op": "EDIT", "quote": "furiosamente", "replacement": "",
         "rationale": "no me gusta cómo suena",
         "rule": "", "severity": "low", "cite": [], "confidence": 0.5},
        {"op": "EDIT", "quote": "no existe en el texto", "replacement": "x",
         "rationale": "inventado", "cite": ["C1"]},
    ])
    result = await er.review("brenner_bot", ORIGINAL, llm_call=llm)

    assert result["expert"]["slug"] == "brenner_bot"
    assert result["chunks"] == 1 and result["degraded"] is False
    assert result["anchored_count"] == 1 and result["opinion_count"] == 1
    anchored = next(d for d in result["deltas"] if d["anchored"])
    opinion = next(d for d in result["deltas"] if not d["anchored"])
    assert anchored["label"] == er.LABEL_CORPUS
    assert anchored["citations"][0]["ref"] == "Brenner - El oficio.pdf, page 42"
    assert opinion["label"] == er.LABEL_OPINION and opinion["citations"] == []
    # The third one is gone from the deltas but not from the answer.
    assert len(result["rejected"]) == 1
    assert "quote" in result["rejected"][0]["reason"]
    assert result["citations"][0]["chunk_id"] == "ch-1"


async def test_review_chunks_long_text_by_scene_and_keeps_global_offsets(experts):
    scene = ("Marta cruzó el patio y no miró atrás. " * 40).strip()
    text = "\n\n".join([scene + " PRIMERA.", scene + " SEGUNDA.", scene + " TERCERA."])
    llm = _answering(
        [{"op": "EDIT", "quote": "PRIMERA", "replacement": "UNA",
          "rationale": "los adverbios en las atribuciones delatan al autor",
          "cite": ["C1"], "severity": "medium"}],
        [{"op": "EDIT", "quote": "SEGUNDA", "replacement": "DOS",
          "rationale": "mi propia opinión", "cite": [], "severity": "low"}],
        [{"op": "EDIT", "quote": "TERCERA", "replacement": "TRES",
          "rationale": "los adverbios en las atribuciones delatan al autor",
          "cite": ["C1"], "severity": "medium"}],
    )
    result = await er.review("brenner_bot", text, llm_call=llm, max_chars=1600)

    assert result["chunks"] == 3 and llm.calls["n"] == 3
    assert len(result["deltas"]) == 3
    # Every span is a DOCUMENT offset, not a chunk-local one.
    for delta in result["deltas"]:
        assert text[delta["span"]["start"]:delta["span"]["end"]] == delta["quote"]
    assert [d["chunk"] for d in result["deltas"]] == [0, 1, 2]
    assert er.apply_deltas(text, result["deltas"]).count("UNA") == 1
    assert "PRIMERA" not in er.apply_deltas(text, result["deltas"])
    # Ids stay unique across chunks.
    assert len({d["id"] for d in result["deltas"]}) == 3


async def test_a_scene_whose_model_call_fails_degrades_the_pass_not_the_rest(experts):
    calls = {"n": 0}

    async def flaky(messages):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("model timed out")
        chunk = _chunk_from(messages)
        quote = "PRIMERA" if "PRIMERA" in chunk else "TERCERA"
        index = chunk.find(quote)
        return json.dumps([{"op": "EDIT", "start": index, "end": index + len(quote),
                            "quote": quote, "replacement": "X",
                            "rationale": "mi opinión", "severity": "low"}])

    scene = ("Marta cruzó el patio y no miró atrás. " * 40).strip()
    text = "\n\n".join([scene + " PRIMERA.", scene + " SEGUNDA.", scene + " TERCERA."])
    result = await er.review("brenner_bot", text, llm_call=flaky, max_chars=1600)
    assert result["degraded"] is True
    assert result["errors"][0]["chunk"] == 1
    assert len(result["deltas"]) == 2


async def test_a_degraded_corpus_is_reported_and_the_review_still_runs(experts, monkeypatch):
    monkeypatch.setattr(experts, "expert_block",
                        lambda slug, q, b: {"text": "", "chunk_ids": [], "degraded": True})
    llm = _answering([{"op": "EDIT", "quote": "caminaba lentamente",
                       "replacement": "se arrastraba", "rationale": "sobra el adverbio",
                       "cite": ["C1"], "severity": "low"}])
    result = await er.review("brenner_bot", ORIGINAL, llm_call=llm)
    assert result["degraded"] is True
    # With no block, [C1] cannot be anything but the model's own opinion.
    assert result["anchored_count"] == 0 and result["opinion_count"] == 1
    assert result["deltas"][0]["anchor_layer"] == "unknown_marker"


async def test_review_feeds_the_story_bible_into_the_prompt(experts):
    seen = {}

    async def capture(messages):
        seen["user"] = messages[1]["content"]
        return "[]"

    bible = {"characters": [{"name": "Marta", "aliases": [],
                             "facts": [{"text": "Marta tenía los ojos verdes",
                                        "source": "cap. 3, p. 41"}]}],
             "timeline": [], "facts": [], "places": []}
    text = "Marta entró en la sala. Marta tenía los ojos azules."
    await er.review("brenner_bot", text, llm_call=capture, story=bible)
    assert "Story bible" in seen["user"]
    assert "ojos verdes" in seen["user"] and "ojos azules" in seen["user"]


async def test_review_refuses_what_it_cannot_do(experts):
    with pytest.raises(er.ExpertReviewError):
        await er.review("brenner_bot", "   ", llm_call=_answering([]))
    with pytest.raises(er.ExpertReviewError):
        await er.review("nobody_home", ORIGINAL, llm_call=_answering([]))
    with pytest.raises(er.ExpertReviewError):
        await er.review("brenner_bot", ORIGINAL, llm_call=None)


async def test_without_the_expert_store_the_error_says_so(no_experts):
    with pytest.raises(er.ExpertReviewError) as exc:
        await er.review("brenner_bot", ORIGINAL, llm_call=_answering([]))
    assert "not configured" in str(exc.value)
    # The other entry points degrade instead of raising.
    assert er.fetch_block("brenner_bot", "x", 100)["degraded"] is True
    assert er.record_feedback("brenner_bot", 1, 0)["recorded"] is False


def test_record_feedback_reaches_the_expert(experts):
    assert er.record_feedback("brenner_bot", 3, 1) == {
        "recorded": True, "slug": "brenner_bot", "accepted": 3, "rejected": 1}
    assert experts.calls["feedback"] == [("brenner_bot", 3, 1)]
    # Junk in is a reported failure, never an exception.
    assert er.record_feedback("brenner_bot", "many", 0)["recorded"] is False


# ── scene splitting ───────────────────────────────────────────────────


def test_scenes_split_at_breaks_and_cover_the_whole_text():
    text = "Escena uno.\n\n* * *\n\nEscena dos.\n\nEscena tres."
    chunks = er.split_scenes(text, 20)
    assert "".join(c["text"] for c in chunks) == text
    assert [c["start"] for c in chunks] == sorted(c["start"] for c in chunks)
    for chunk in chunks:
        assert text[chunk["start"]:chunk["end"]] == chunk["text"]


def test_a_paragraph_longer_than_the_budget_is_cut_at_sentences():
    text = ("Una frase corta. " * 200).strip()
    chunks = er.split_scenes(text, 500)
    assert len(chunks) > 1
    assert "".join(c["text"] for c in chunks) == text
    assert all(len(c["text"]) <= 700 for c in chunks)


def test_short_text_is_one_chunk_and_junk_never_raises():
    assert er.split_scenes("corto", 3000) == [{"start": 0, "end": 5, "text": "corto"}]
    assert er.split_scenes("", 3000) == []
    assert er.split_scenes(None, None) == []
    assert len(er.split_scenes("x" * 50, "nonsense")) == 1


# ── rendering ─────────────────────────────────────────────────────────


async def test_the_rendered_review_states_the_label_in_words(experts):
    llm = _answering([
        {"op": "EDIT", "quote": "caminaba lentamente", "replacement": "se arrastraba",
         "rationale": "los adverbios en las atribuciones delatan al autor",
         "rule": "Adverbios", "cite": ["C1"], "severity": "high"},
        {"op": "EDIT", "quote": "furiosamente", "replacement": "con rabia",
         "rationale": "me suena mejor", "cite": [], "severity": "low"},
    ])
    result = await er.review("brenner_bot", ORIGINAL, llm_call=llm)
    rendered = er.format_review(result)
    assert "1 anchored to the corpus" in rendered
    assert "1 the model's own opinion" in rendered
    assert er.LABEL_OPINION in rendered
    assert "Brenner - El oficio.pdf, page 42" in rendered
    assert "se arrastraba" in rendered


# ── the agent tool ────────────────────────────────────────────────────


def _tool_block(args):
    from src.agent_tools import ToolBlock
    return ToolBlock(tool_type="expert_review", content=json.dumps(args))


@pytest.fixture()
def project(tmp_path, monkeypatch):
    ws = tmp_path / "novela"
    ws.mkdir()
    project = {"id": "p1", "name": "Novela", "workspace": str(ws)}
    import services.projects as projects_mod
    monkeypatch.setattr(projects_mod, "project_for_session",
                        lambda sid, owner=None: project, raising=False)
    return project


def test_the_tool_is_wired_everywhere_a_tool_must_be():
    from src.agent_tools import TOOL_TAGS
    from src.agent_loop import TOOL_SECTIONS
    from src.tool_capabilities import KNOWN_CAPABILITY_TOOLS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block

    assert "expert_review" in TOOL_TAGS
    assert "expert_review" in KNOWN_CAPABILITY_TOOLS
    assert "expert_review" in TOOL_SECTIONS
    assert len(BUILTIN_TOOL_DESCRIPTIONS["expert_review"]) > 40
    schema = next(s["function"] for s in FUNCTION_TOOL_SCHEMAS
                  if s["function"]["name"] == "expert_review")
    assert set(schema["parameters"]["properties"]["action"]["enum"]) == {
        "review", "experts", "bible", "apply", "feedback"}
    block = function_call_to_tool_block("expert_review", {"action": "bible"})
    assert json.loads(block.content) == {"action": "bible"}


async def test_tool_review_dispatch(experts, project, monkeypatch):
    llm = _answering([
        {"op": "EDIT", "quote": "caminaba lentamente", "replacement": "se arrastraba",
         "rationale": "los adverbios en las atribuciones delatan al autor",
         "rule": "Adverbios", "cite": ["C1"], "severity": "high"},
    ])
    import src.tool_execution as te
    monkeypatch.setattr(te, "_expert_llm_call", lambda expert, owner, sid: llm)
    desc, result = await te._execute_tool_block_impl(
        _tool_block({"action": "review", "slug": "brenner_bot", "text": ORIGINAL}),
        session_id="s1", owner="luis")
    assert desc == "expert_review: review"
    assert result["anchored_count"] == 1
    assert er.LABEL_CORPUS in result["summary"]
    assert result["rejected_count"] == 0
    # The compact result keeps where a correction came from and drops the
    # excerpt text it would otherwise repeat once per delta.
    citation = result["deltas"][0]["citations"][0]
    assert citation["ref"] == "Brenner - El oficio.pdf, page 42"
    assert "excerpt" not in citation
    assert result["citations"][0]["excerpt"]


async def test_tool_bible_dispatch(experts, project):
    import src.tool_execution as te
    from src import story_bible as sb

    sb.apply_deltas(project, [{"op": "ADD", "kind": "character", "name": "Marta",
                               "facts": [{"text": "ojos verdes", "key": "eyes",
                                          "value": "verdes",
                                          "source": "cap. 3, p. 41"}]}], "user")

    desc, result = await te._execute_tool_block_impl(
        _tool_block({"action": "bible"}), session_id="s1", owner="luis")
    assert desc == "expert_review: bible"
    assert result["counts"]["characters"] == 1

    _, checked = await te._execute_tool_block_impl(
        _tool_block({"action": "bible",
                     "text": "Marta llegó. Marta tenía los ojos azules."}),
        session_id="s1", owner="luis")
    assert checked["findings"][0]["kind"] == "contradiction"
    assert checked["findings"][0]["bible_fact"]["source"] == "cap. 3, p. 41"

    _, applied = await te._execute_tool_block_impl(
        _tool_block({"action": "bible",
                     "deltas": [{"op": "ADD", "kind": "character", "name": "Nuria",
                                 "rationale": "aparece en el capítulo 2"}]}),
        session_id="s1", owner="luis")
    assert applied["applied"][0]["id"] == "CHAR-2"


async def test_tool_bible_outside_a_project_is_an_error(experts, monkeypatch):
    import services.projects as projects_mod
    import src.tool_execution as te
    monkeypatch.setattr(projects_mod, "project_for_session",
                        lambda sid, owner=None: None, raising=False)
    _, result = await te._execute_tool_block_impl(
        _tool_block({"action": "bible"}), session_id="s1", owner="luis")
    assert result["exit_code"] == 1 and "not attached to a project" in result["error"]


async def test_tool_experts_apply_and_feedback(experts, project):
    import src.tool_execution as te

    _, profile = await te._execute_tool_block_impl(
        _tool_block({"action": "experts", "slug": "brenner_bot"}),
        session_id="s1", owner="luis")
    assert profile["expert"]["name"] == "Brenner bot"

    deltas = _one(json.dumps([_delta_json(start=7, end=26)]))["deltas"]
    _, applied = await te._execute_tool_block_impl(
        _tool_block({"action": "apply", "text": ORIGINAL,
                     "deltas": deltas, "accept": ["D1"]}),
        session_id="s1", owner="luis")
    assert "se arrastraba" in applied["text"]

    _, fed = await te._execute_tool_block_impl(
        _tool_block({"action": "feedback", "slug": "brenner_bot",
                     "accepted": 2, "rejected": 1}),
        session_id="s1", owner="luis")
    assert fed["recorded"] is True
    assert experts.calls["feedback"][-1] == ("brenner_bot", 2, 1)


async def test_tool_reports_a_missing_expert_store_instead_of_raising(no_experts, project):
    import src.tool_execution as te
    _, result = await te._execute_tool_block_impl(
        _tool_block({"action": "review", "slug": "brenner_bot", "text": ORIGINAL}),
        session_id="s1", owner="luis")
    assert result["exit_code"] == 1 and "not configured" in result["error"]

    _, listing = await te._execute_tool_block_impl(
        _tool_block({"action": "experts"}), session_id="s1", owner="luis")
    assert listing["exit_code"] == 1 and "does not expose a listing" in listing["error"]


async def test_tool_rejects_garbage_without_raising(experts, project):
    import src.tool_execution as te
    for content in ("not json", json.dumps([1, 2]), json.dumps({"action": "nuke"}),
                    json.dumps({"action": "apply", "text": "x"}),
                    json.dumps({"action": "review", "slug": "", "text": "x"})):
        from src.agent_tools import ToolBlock
        block = ToolBlock(tool_type="expert_review", content=content)
        _, result = await te._execute_tool_block_impl(block, session_id="s1", owner="luis")
        assert result["exit_code"] == 1 and result["error"]
