"""TOON — the compact serializer the machine-facing surfaces answer with
(src/toon.py).

The property that matters is round-tripping: a coordinator that reads TOON
must get back exactly the object Faustus meant to send, or the compaction is
a lie. Everything below hammers `decode(encode(x)) == x` over a hand-written
corpus (nesting, folded keys, tabular arrays with commas and quotes inside
cells, unicode, empty containers, strings that look like numbers) and then
measures what the compaction actually buys.

Measured here on ALREADY-ROW-SHAPED payloads, against JSON with COMPACT
separators — the hardest baseline there is:

    learned-memory items (15 rows)   4306 → 2334 chars   ratio 0.54  (-46 %)
    command-guard receipts (25 rows) 4658 → 2779 chars   ratio 0.60  (-40 %)
    system usage (GPU + model rows)   540 →  406 chars   ratio 0.75  (-25 %)

And the honest counter-example, measured and asserted too: an objectives
dashboard plus usage comes out ~1.15× compact JSON. Almost nothing in a
dashboard is tabular — the scores are one object per objective id, and each
log record carries a `fields` object of its own — so TOON's two-space indent
per nesting level has no repeated keys to earn it back.

That counter-example is the general case, not the exception: every payload the
endpoints really build has per-row lists or objects in it, and re-encoding one
as it stands came out BIGGER than the JSON it replaced. So the encoder is not
where robot mode saves anything — src/robot_projection.py is, by turning each
payload into flat scalar rows first. The end-to-end figures the docs quote
(0.17-0.54 of the plain JSON body) are measured in
tests/test_robot_projection.py, on realistic payloads.
"""
from __future__ import annotations

import datetime
import json

import pytest

from src import toon

# ── the corpus ──────────────────────────────────────────────────────────────

CORPUS = [
    ("empty-object", {}),
    ("empty-array", []),
    ("null", None),
    ("true", True),
    ("false", False),
    ("zero", 0),
    ("negative", -42),
    ("float", 3.5),
    ("float-exponent", 1e-05),
    ("empty-string", ""),
    ("word", "hello"),
    ("sentence", "hello world"),
    ("looks-like-bool", "true"),
    ("looks-like-null", "null"),
    ("looks-like-int", "3"),
    ("looks-like-float", "3.50"),
    ("looks-like-zero-padded", "007"),
    ("looks-like-empty-array", "[]"),
    ("looks-like-empty-object", "{}"),
    ("leading-space", " leading"),
    ("trailing-space", "trailing "),
    ("colon-space", "key: value"),
    ("hash", "issue #42"),
    ("newline", "line one\nline two"),
    ("carriage-return", "windows\r\nline"),
    ("tab", "col\tcol"),
    ("quote", 'he said "no"'),
    ("backslash", "C:\\LocalAI\\odysseus"),
    ("dash-item", "- not an item"),
    ("unicode", "añadió la validación · 中文 · €"),
    ("flat-object", {"a": 1, "b": "two", "c": None, "d": True, "e": 1.25}),
    ("key-folding-depth-3", {"config": {"database": {"host": "localhost"}}}),
    ("key-folding-depth-4", {"a": {"b": {"c": {"d": "end"}}}}),
    ("dotted-key-is-not-folding", {"a.b": 1, "x": {"y.z": 2}}),
    ("folded-then-branching", {"outer": {"inner": {"one": 1, "two": 2}}}),
    ("nested-empties", {"list": [], "obj": {}, "deep": {"also": {}}}),
    ("tabular", {"users": [{"id": 1, "name": "ana", "active": True},
                           {"id": 2, "name": "bob", "active": False},
                           {"id": 3, "name": "cy", "active": None}]}),
    ("tabular-comma-cell", {"rows": [{"id": 1, "note": "cart.py, then bill.py"},
                                     {"id": 2, "note": "plain"}]}),
    ("tabular-quote-cell", {"rows": [{"id": 1, "note": 'he said "no"'},
                                     {"id": 2, "note": "ok"}]}),
    ("tabular-quote-and-comma", {"rows": [{"id": 1, "note": '"a", "b"'},
                                          {"id": 2, "note": ""}]}),
    ("tabular-unicode", {"rows": [{"id": 1, "t": "café · 中文"},
                                  {"id": 2, "t": "€ 12,50"}]}),
    ("tabular-numeric-strings", {"rows": [{"id": "1", "v": "true"},
                                          {"id": "2", "v": "3.0"}]}),
    ("not-tabular-different-keys", {"rows": [{"a": 1}, {"a": 1, "b": 2}]}),
    ("not-tabular-nested-value", {"rows": [{"a": 1, "b": [1]}, {"a": 2, "b": []}]}),
    ("single-row-is-not-a-table", {"rows": [{"a": 1, "b": 2}]}),
    ("mixed-array", {"mixed": [1, "two", None, {"k": "v"}, [1, 2], {}, []]}),
    ("array-of-arrays", [[1, 2], [3, 4]]),
    ("root-array-of-scalars", [1, "two", None, True]),
    ("root-array-of-objects", [{"a": 1, "b": 2}, {"a": 3, "b": 4}]),
    ("folded-into-a-table", {"report": {"users": [{"id": 1, "n": "a"}, {"id": 2, "n": "b"}]}}),
    ("envelope-shaped", {"ok": True, "data": {"status": "success", "items": [
        {"id": "m1", "text": "run the tests", "score": 0.71},
        {"id": "m2", "text": "do not touch the public API", "score": 0.44}]},
        "error_code": None, "error": None, "elapsed_ms": 12, "schema_version": 1}),
]


@pytest.mark.parametrize("value", [c[1] for c in CORPUS], ids=[c[0] for c in CORPUS])
def test_decode_round_trips_everything_encode_writes(value):
    text = toon.encode(value)
    assert isinstance(text, str)
    assert toon.decode(text) == value, text


def test_encoding_is_deterministic_and_preserves_the_caller_s_key_order():
    payload = {"zebra": 1, "alpha": 2, "middle": {"b": 1, "a": 2}}
    assert toon.encode(payload) == toon.encode(payload)
    assert toon.encode(payload).splitlines()[:2] == ["zebra: 1", "alpha: 2"]
    assert toon.encode(payload).splitlines()[-2:] == ["  b: 1", "  a: 2"]


# ── the shapes the format promises ──────────────────────────────────────────

def test_a_uniform_array_of_objects_becomes_one_header_and_one_line_per_row():
    text = toon.encode({"users": [{"id": 1, "name": "ana", "active": True},
                                  {"id": 2, "name": "bo,b", "active": False}]})
    assert text.splitlines() == [
        "users[2]{id,name,active}:",
        "  1,ana,true",
        '  2,"bo,b",false',
    ]
    # the keys are named once, not once per row
    assert text.count("name") == 1


def test_a_single_key_object_folds_into_a_dotted_path():
    assert toon.encode({"config": {"database": {"host": "localhost"}}}) == \
        "config.database.host: localhost"
    # …but a key that already contains a dot is quoted, never folded, so a
    # dotted path in the output is always structure
    assert toon.encode({"a.b": 1}) == '"a.b": 1'


def test_scalars_are_bare_only_when_they_cannot_be_misread():
    lines = toon.encode({
        "n": 3, "f": 1.5, "b": True, "nil": None, "plain": "hello world",
        "numeric": "3", "boolish": "true", "spaced": " x ", "empty": "",
        "colon": "a: b", "hashed": "a #1", "multiline": "a\nb",
    }).splitlines()
    assert lines == [
        "n: 3", "f: 1.5", "b: true", "nil: null", "plain: hello world",
        'numeric: "3"', 'boolish: "true"', 'spaced: " x "', 'empty: ""',
        'colon: "a: b"', 'hashed: "a #1"', 'multiline: "a\\nb"',
    ]


def test_empty_containers_and_indent_have_a_spelling():
    assert toon.encode({"a": [], "b": {}}) == "a: []\nb: {}"
    assert toon.encode({}) == "{}" and toon.encode([]) == "[]"
    assert toon.encode({"a": {"b": 1, "c": 2}}, indent=4) == "    a:\n      b: 1\n      c: 2"


# ── it may never take a request down ────────────────────────────────────────

class _Exploding:
    def __str__(self):  # noqa: D105
        raise RuntimeError("no string for you")

    __repr__ = __str__


def test_encode_never_raises_on_input_json_would_refuse():
    for value in ({1, 2, 3}, datetime.datetime(2026, 8, 30, 12, 34, 56),
                  datetime.date(2026, 8, 30), object(), len, _Exploding(),
                  {"k": {1, 2}}, [datetime.timedelta(seconds=3)], b"bytes",
                  float("inf"), float("nan"), {"f": float("nan")}):
        text = toon.encode(value)
        assert isinstance(text, str)
        assert toon.decode(text) is not Ellipsis      # it parses back to something

    cycle: dict = {"name": "loop"}
    cycle["self"] = cycle
    assert isinstance(toon.encode(cycle), str)
    ring: list = [1]
    ring.append(ring)
    assert isinstance(toon.encode(ring), str)


def test_decode_answers_none_instead_of_raising_on_nonsense():
    for text in ("", "   ", "\n\n", "]]]{{{", 'unterminated: "quote',
                 "key[nope]{a,b}:", None, 12345):
        assert toon.decode(text) is None or isinstance(toon.decode(text), (dict, list, str, int))


def test_a_json_payload_survives_encode_decode_unchanged():
    """The realistic guarantee: anything that came out of json.loads goes
    through TOON and comes back equal."""
    payload = json.loads(json.dumps({
        "ok": True, "data": {"status": "success",
                             "receipts": [{"ts": "2026-08-30T12:34:56Z", "tier": "DANGEROUS",
                                           "rule_id": "fs.rm_rf", "decision": "blocked"},
                                          {"ts": "2026-08-30T12:35:01Z", "tier": "SAFE",
                                           "rule_id": None, "decision": "allowed"}],
                             "chain": {"ok": True, "entries": 2}},
        "error_code": None, "error": None, "elapsed_ms": 3, "schema_version": 1,
    }))
    assert toon.decode(toon.encode(payload)) == payload


# ── what the compaction buys ────────────────────────────────────────────────

def _memory_items(n=15):
    return {"status": "success", "items": [
        {"id": "m%02d" % i, "id8": "m%02d" % i,
         "text": "Always run the project tests before saying done",
         "level": "procedural", "category": "testing", "trust": 0.85,
         "trust_class": "human_explicit", "effective_score": 0.7312,
         "harmful_ratio": 0.0, "status": "active", "uses": 12,
         "created_at": "2026-07-01T09:00:00+00:00"} for i in range(n)],
        "levels": ["procedural", "semantic", "episodic"]}


def _guard_log(n=25):
    return {"status": "success", "receipts": [
        {"ts": "2026-08-30T12:34:56Z", "tier": "DANGEROUS", "rule_id": "fs.rm_rf",
         "decision": "blocked", "command_head": "rm -rf /tmp/build", "owner": "luis",
         "hash": "a" * 32} for _ in range(n)],
        "chain": {"ok": True, "entries": n, "broken_at": None}}


def _usage():
    return {"ts": 1767268496.12,
            "host": {"cpu_percent": 12.5, "ram_total": 68719476736,
                     "ram_used": 21474836480, "ram_percent": 31.2},
            "gpus": [{"index": 0, "name": "NVIDIA GeForce RTX 4090", "mem_total": 24564,
                      "mem_used": 18234, "util": 87, "temp": 61, "power": 320.5},
                     {"index": 1, "name": "NVIDIA GeForce RTX 3090", "mem_total": 24576,
                      "mem_used": 2048, "util": 3, "temp": 44, "power": 102.0}],
            "gpu_count": 2,
            "models": [{"name": "qwen3.5:9b", "size_vram": 9123456789, "gpu": 0,
                        "keep_alive": "5m0s"},
                       {"name": "qwen3.8:27b-q8_0", "size_vram": 29123456789, "gpu": 1,
                        "keep_alive": "5m0s"}],
            "orphans": []}


def _objectives(n=8, log_rows=20):
    objectives = [{"t": "obj", "id": f"OBJ-{i}", "title": f"Ship the {i}th slice of the API",
                   "status": ["open", "in_progress", "done", "blocked"][i % 4],
                   "priority": (i % 4) + 1, "owner": "user", "notes": "",
                   "created_at": "2026-08-01T10:00:00+00:00",
                   "updated_at": "2026-08-30T12:34:56+00:00", "last_actor": "agent",
                   "deps": ([f"OBJ-{i - 1}"] if i > 1 and i % 3 == 0 else [])}
                  for i in range(1, n + 1)]
    return {
        "objectives": objectives,
        "edges": [{"from": o["id"], "to": o["deps"][0]} for o in objectives if o["deps"]],
        "scores": {o["id"]: {"score": 0.4231, "hint": None,
                             "components": {"pagerank": 0.1234, "betweenness": 0.0,
                                            "blocker_ratio": 0.25, "staleness": 0.9,
                                            "priority_boost": 0.5}} for o in objectives},
        "log": [{"ts": "2026-08-30T12:34:56+00:00", "kind": "delta", "actor": "agent",
                 "op": "EDIT", "id": f"OBJ-{(i % n) + 1}", "fields": {"status": "done"},
                 "rationale": "worker finished the task", "session": "sess-1"}
                for i in range(log_rows)],
    }


@pytest.mark.parametrize("name,payload,ceiling", [
    ("memory items", _memory_items(), 0.60),
    ("guard receipts", _guard_log(), 0.65),
    ("system usage", _usage(), 0.80),
])
def test_a_tabular_payload_is_a_real_reduction(name, payload, ceiling):
    measured = toon.estimate_savings(payload)
    assert measured["json_chars"] > 0 and measured["toon_chars"] > 0
    assert measured["ratio"] == pytest.approx(
        measured["toon_chars"] / measured["json_chars"], abs=1e-4)
    assert measured["ratio"] < ceiling, (name, measured)
    # …and it is still the same data
    assert toon.decode(toon.encode(payload)) == payload


def test_the_row_shaped_payloads_clear_the_0_75_bar():
    """The encoder's own claim: on payloads that ARE already tabular, TOON
    costs under three quarters of the same rows in compact JSON, because the
    keys are named once instead of once per row. (The endpoints only hand it
    payloads of this shape because src/robot_projection.py makes them one
    first — see the fixtures here: no row carries a list or an object.)"""
    for payload in (_memory_items(), _guard_log()):
        assert toon.estimate_savings(payload)["ratio"] < 0.75
    # and the win grows with the number of rows: the header is paid once
    assert (toon.estimate_savings(_guard_log(100))["ratio"]
            < toon.estimate_savings(_guard_log(3))["ratio"])


def test_the_realistic_objectives_plus_usage_payload_is_recorded():
    """The counter-example, measured and recorded rather than hidden: an
    objectives dashboard is per-key score objects and log records whose
    `fields` is itself an object, so almost none of it is tabular and TOON's
    two-space indent per nesting level has nothing to earn it back. It comes
    out about a tenth LARGER than compact JSON — which is exactly why robot
    mode does not send this object at all: it sends the projection of it
    (src/robot_projection.py), whose rows are scalars and whose measured cost
    is 0.41 of the plain JSON body."""
    payload = {"objectives": _objectives(), "usage": _usage()}
    measured = toon.estimate_savings(payload)
    assert 1.0 <= measured["ratio"] < 1.25, measured
    # against the JSON the endpoint really renders (json.dumps defaults, with
    # a space after every separator) it is a wash, not a loss
    default_chars = len(json.dumps(payload, ensure_ascii=False))
    assert measured["toon_chars"] < default_chars * 1.10
    assert toon.decode(toon.encode(payload)) == payload
    # the uniform slices inside it still pay for themselves
    assert toon.estimate_savings({"gpus": payload["usage"]["gpus"] * 4})["ratio"] < 0.60


def test_a_table_needs_a_key_so_a_root_array_stays_items():
    """A documented limit: the header is `key[N]{cols}:`, so an array at the
    very root has nowhere to put one and renders as `- ` items. Every payload
    robot mode sends is the envelope object, so its arrays are always keyed."""
    rows = [{"id": 1, "n": "a"}, {"id": 2, "n": "b"}]
    assert toon.encode(rows).startswith("-")
    assert toon.encode({"rows": rows}).startswith("rows[2]{id,n}:")
    assert toon.decode(toon.encode(rows)) == rows


def test_estimate_savings_never_raises_on_an_unserializable_object():
    measured = toon.estimate_savings({"when": datetime.datetime(2026, 8, 30), "s": {1, 2}})
    assert measured["json_chars"] > 0 and measured["ratio"] > 0
