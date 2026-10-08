"""OBJ-24: extraction into the user's JSON Schema (src/schema_extraction.py).

What is pinned here:

* the profile and tier of a schema (flat, nested, $defs, recursive, union,
  arrays of objects, long input) and the purpose each tier routes to;
* local $ref inlining, and a clear refusal for recursive or remote ones;
* the envelope: every field present and nullable, constraints left out;
* grounding: a value the document text at the quoted place does not contain is
  dropped (also when the model rewrote the quote to match it), an OCR-noisy
  quote with intact figures is kept, the page is the reader's (never the
  model's), European and English numbers and written dates match, a required
  field the document lacks stays null with `schema_valid: false`, and the
  result says the check is lexical (`limits`);
* a `$ref` with sibling keywords is a conjunction (the stricter bound wins) or
  the schema is refused by name; a sibling never loosens the definition;
* one repair and at most one escalation, never to a paid endpoint;
* merging the chunks of a long document, with conflicts recorded;
* `resolve_endpoint("extraction")` falling back to the utility model.

The model is always a scripted fake; nothing here reaches a network.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from src import schema_extraction as se
from src.workflows.model_calls import ModelUnavailable

LOCAL = "http://127.0.0.1:8081/v1/chat/completions"
PAID = "https://api.example.com/v1/chat/completions"


def obj(props: Dict[str, Any], required=()) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(required)}


S = {"type": "string"}
N = {"type": "number"}

INVOICE = obj({"invoice_number": S, "issue_date": {"type": "string", "format": "date"}, "total": N,
               "currency": {"type": "string", "enum": ["EUR", "USD"]}, "paid": {"type": "boolean"}},
              required=("invoice_number", "total"))

NESTED = obj({
    "invoice_number": S,
    "seller": obj({"name": S, "tax_id": S, "address": obj({"street": S, "city": S})}),
    "lines": {"type": "array", "items": obj({"description": S, "quantity": N, "amount": N})},
    "total": N,
}, required=("invoice_number", "total"))


# ── profile and tier ──────────────────────────────────────────────────────

def _deep(levels: int) -> Dict[str, Any]:
    node: Dict[str, Any] = obj({"value": S})
    for _ in range(levels - 1):
        node = obj({"inner": node})
    return node


@pytest.mark.parametrize("schema,tier,needle", [
    (INVOICE, "simple", "5 fields"),
    (obj({f"f{i}": S for i in range(8)}), "simple", "8 fields"),
    (obj({f"f{i}": S for i in range(9)}), "medium", "9 fields"),
    (_deep(3), "medium", "nesting depth 3"),
    (_deep(4), "complex", "nesting depth 4"),
    (NESTED, "medium", "an array of objects"),
    ({"type": "object", "properties": {"buyer": {"$ref": "#/$defs/Party"}},
      "$defs": {"Party": obj({"name": S})}}, "medium", "$ref"),
    ({"type": "object", "properties": {"root": {"$ref": "#/$defs/Node"}},
      "$defs": {"Node": obj({"name": S, "children": {"type": "array", "items": {"$ref": "#/$defs/Node"}}})}},
     "complex", "recursive"),
    (obj({"amount": {"anyOf": [N, obj({"value": N, "unit": S})]}}), "complex", "union"),
    (obj({"a": {"type": "array", "items": obj({"x": S})}, "b": {"type": "array", "items": obj({"y": S})}}),
     "complex", "2 arrays of objects"),
    (obj({f"f{i}": S for i in range(31)}), "complex", "31 fields"),
])
def test_the_profile_gives_each_shape_its_tier(schema, tier, needle):
    profile = se.schema_profile(schema)
    assert profile["tier"] == tier
    assert needle in " | ".join(profile["reasons"])


def test_enums_never_move_the_tier_and_a_nullable_field_is_not_a_union():
    schema = obj({"status": {"type": "string", "enum": ["paid", "due", "void"]},
                  "kind": {"enum": ["a", "b"]}, "note": {"anyOf": [S, {"type": "null"}]}})
    profile = se.schema_profile(schema)
    assert profile["tier"] == "simple" and profile["enums"] == 2 and profile["unions"] == 0


def test_a_long_input_moves_the_schema_up_one_tier_and_no_further():
    assert se.schema_profile(INVOICE, input_chars=12_001)["tier"] == "medium"
    assert se.schema_profile(INVOICE, input_chars=12_000)["tier"] == "simple"
    assert se.schema_profile(NESTED, input_chars=50_000)["tier"] == "complex"
    assert se.schema_profile(_deep(5), input_chars=50_000)["tier"] == "complex"
    assert se.schema_profile(INVOICE, input_chars=20_000)["base_tier"] == "simple"


def test_each_tier_routes_to_its_purpose_and_skips_what_cannot_serve_it():
    models = {"utility": (LOCAL, "small", {}), "extraction": (LOCAL, "small", {}), "default": (LOCAL, "big", {})}
    resolver = lambda purpose, owner: models[purpose]   # noqa: E731
    ok = lambda model: None                              # noqa: E731
    simple = se.route_for_schema(se.schema_profile(INVOICE), resolver=resolver, json_ok=ok)
    assert (simple["purpose"], simple["model"]) == ("utility", "small")
    medium = se.route_for_schema(se.schema_profile(NESTED), resolver=resolver, json_ok=ok)
    assert (medium["purpose"], medium["model"]) == ("extraction", "small")
    hard = se.route_for_schema(se.schema_profile(_deep(4)), resolver=resolver, json_ok=ok)
    assert (hard["purpose"], hard["model"]) == ("default", "big")
    # A model whose calibration says JSON mode fails is skipped, with the reason.
    moved = se.route_for_schema(se.schema_profile(INVOICE), resolver=resolver,
                                json_ok=lambda model: False if model == "small" else True)
    assert (moved["purpose"], moved["model"]) == ("default", "big")
    assert "JSON mode fails" in json.dumps(moved["excluded"])
    # Never from a local model to a paid one on its own.
    paid = {**models, "default": (PAID, "cloud", {})}
    stuck = se.route_for_schema(se.schema_profile(INVOICE), resolver=lambda p, o: paid[p],
                                json_ok=lambda model: False if model == "small" else True)
    assert stuck["model"] is None and "paid endpoint" in json.dumps(stuck["excluded"])
    # A forced tier is honoured and said.
    forced = se.route_for_schema(se.schema_profile(INVOICE), tier="complex", resolver=resolver, json_ok=ok)
    assert forced["purpose"] == "default" and "asked for by the caller" in " ".join(forced["reasons"])


def test_resolve_endpoint_for_extraction_falls_back_to_the_utility_model(monkeypatch):
    from src import endpoint_resolver as er

    settings = {"utility_endpoint_id": "ep-utility", "utility_model": "qwen-small",
                "default_endpoint_id": "ep-default", "default_model": "qwen-big"}
    monkeypatch.setattr("src.settings.load_settings", lambda: dict(settings))
    monkeypatch.setattr("src.settings.get_user_setting", lambda key, owner, default=None: settings.get(key, default))
    asked: List[str] = []

    class Endpoint:
        def __init__(self, ep_id):
            self.id = ep_id

    class Query:
        def filter(self, *conditions):
            for condition in conditions:
                value = getattr(getattr(condition, "right", None), "value", None)
                if isinstance(value, str):
                    asked.append(value)
            return self

        def first(self):
            return Endpoint(asked[-1])

    class Session:
        def query(self, *_):
            return Query()

        def close(self):
            pass

    monkeypatch.setattr(er, "SessionLocal", lambda: Session())
    monkeypatch.setattr(er, "resolve_endpoint_runtime", lambda ep, owner=None: (f"http://127.0.0.1:8081/{ep.id}", ""))
    monkeypatch.setattr(er, "_endpoint_hidden_models", lambda ep: set())
    url, model, _ = er.resolve_endpoint("extraction")
    assert model == "qwen-small" and asked == ["ep-utility"] and "ep-utility" in url
    settings["extraction_endpoint_id"], settings["extraction_model"] = "ep-default", "qwen-big"
    asked.clear()
    url, model, _ = er.resolve_endpoint("extraction")
    assert model == "qwen-big" and asked == ["ep-default"]


# ── $ref inlining ─────────────────────────────────────────────────────────

def test_local_refs_are_inlined_with_sibling_keywords_and_the_definitions_removed():
    schema = {"type": "object", "properties": {
        "seller": {"$ref": "#/$defs/Party", "description": "who issues it"},
        "buyer": {"$ref": "#/definitions/Party"}},
        "$defs": {"Party": obj({"name": S, "address": {"$ref": "#/$defs/Address"}}),
                  "Address": obj({"city": S})},
        "definitions": {"Party": obj({"name": S})}}
    out = se.inline_refs(schema)
    assert "$defs" not in out and "definitions" not in out
    seller = out["properties"]["seller"]
    assert seller["description"] == "who issues it"
    assert seller["properties"]["address"]["properties"]["city"] == S
    assert "$ref" not in json.dumps(out)


@pytest.mark.parametrize("schema,code", [
    (obj({"a": {"$ref": "#/$defs/Missing"}}), "dangling_ref"),
    (obj({"a": {"$ref": "https://example.com/schema.json"}}), "unsupported_ref"),
    ({"type": "object", "properties": {"n": {"$ref": "#/$defs/N"}},
      "$defs": {"N": obj({"next": {"$ref": "#/$defs/N"}})}}, "recursive_schema"),
])
def test_refs_that_cannot_be_inlined_are_refused_by_name(schema, code):
    with pytest.raises(se.SchemaExtractionError) as err:
        se.inline_refs(schema)
    assert err.value.code == code
    if code == "recursive_schema":
        assert "#/$defs/N -> #/$defs/N" in str(err.value)


def _with_def(definition, beside):
    return {"type": "object", "$defs": {"D": definition},
            "properties": {"n": {"$ref": "#/$defs/D", **beside}}, "required": ["n"]}


def test_a_ref_sibling_never_loosens_the_definition_it_points_to():
    schema = _with_def({"type": "integer", "maximum": 5}, {"maximum": 10})
    assert se.inline_refs(schema)["properties"]["n"] == {"type": "integer", "maximum": 5}
    for value, valid in ((3, True), (5, True), (8, False), (11, False)):
        assert se.finalize({"n": value}, schema)["schema_valid"] is valid, value
    # ...and a stricter sibling tightens it, so the answer must satisfy both.
    schema = _with_def({"type": "integer", "maximum": 10}, {"maximum": 5, "minimum": 2})
    assert se.inline_refs(schema)["properties"]["n"] == {"type": "integer", "maximum": 5, "minimum": 2}
    assert [se.finalize({"n": v}, schema)["schema_valid"] for v in (1, 2, 5, 6)] == [False, True, True, False]
    # The shape the model is shown and the envelope carry the merged bounds as well.
    assert se.inline_refs(schema) == se.prepare_schema(schema)[0]


@pytest.mark.parametrize("definition,beside,expected", [
    ({"type": "string", "minLength": 3}, {"minLength": 6}, {"type": "string", "minLength": 6}),
    ({"type": "string", "maxLength": 9}, {"maxLength": 4}, {"type": "string", "maxLength": 4}),
    ({"type": "number", "minimum": 0, "exclusiveMaximum": 100}, {"exclusiveMaximum": 50, "minimum": -5},
     {"type": "number", "minimum": 0, "exclusiveMaximum": 50}),
    ({"type": ["string", "null"]}, {"type": "string"}, {"type": "string"}),
    ({"type": "number"}, {"type": "integer"}, {"type": "integer"}),
    ({"enum": ["a", "b", "c"]}, {"enum": ["c", "b", "z"]}, {"enum": ["b", "c"]}),
    ({"type": "string", "description": "def"}, {"description": "mine"}, {"type": "string", "description": "mine"}),
])
def test_ref_siblings_combine_by_the_stricter_keyword(definition, beside, expected):
    assert se.inline_refs(_with_def(definition, beside))["properties"]["n"] == expected


def test_required_and_properties_are_combined_across_a_ref():
    party = obj({"name": S, "tax_id": S}, required=("name",))
    schema = {"type": "object", "$defs": {"Party": party},
              "properties": {"seller": {"$ref": "#/$defs/Party", "required": ["tax_id"],
                                        "properties": {"tax_id": {"type": "string", "minLength": 9},
                                                       "email": S}}}}
    seller = se.inline_refs(schema)["properties"]["seller"]
    assert seller["required"] == ["name", "tax_id"] and set(seller["properties"]) == {"name", "tax_id", "email"}
    assert seller["properties"]["tax_id"] == {"type": "string", "minLength": 9}
    assert se.finalize({"seller": {"name": "A", "tax_id": "123"}}, schema)["schema_valid"] is False
    assert se.finalize({"seller": {"name": "A", "tax_id": "123456789"}}, schema)["schema_valid"] is True
    assert se.finalize({"seller": {"name": "A"}}, schema)["missing_required"] == ["seller.tax_id"]


def test_a_condition_that_cannot_be_merged_is_kept_as_an_extra_requirement():
    schema = _with_def({"type": "string", "pattern": "^[A-Z]+$"}, {"pattern": "^.{3}$"})
    node = se.inline_refs(schema)["properties"]["n"]
    assert node["pattern"] == "^[A-Z]+$" and node["allOf"] == [{"pattern": "^.{3}$"}]
    assert [se.finalize({"n": v}, schema)["schema_valid"] for v in ("ABC", "ABCD", "abc")] == [True, False, False]


@pytest.mark.parametrize("definition,beside", [
    ({"type": "integer", "maximum": 5}, {"minimum": 10}),
    ({"type": "string"}, {"type": "number"}),
    ({"enum": ["a"]}, {"enum": ["b"]}),
    ({"const": "x"}, {"const": "y"}),
    ({"type": "string", "minLength": 5}, {"maxLength": 2}),
    ({"type": "array", "items": S, "minItems": 3}, {"maxItems": 1}),
    ({"type": "integer"}, {"const": 2.5}),
])
def test_contradictory_ref_siblings_are_refused_by_name(definition, beside):
    with pytest.raises(se.SchemaExtractionError) as err:
        se.inline_refs(_with_def(definition, beside))
    assert err.value.code == "contradictory_schema" and "#/$defs/D" in str(err.value)
    with pytest.raises(se.SchemaExtractionError):
        se.finalize({"n": 1}, _with_def(definition, beside))


def test_siblings_whose_meaning_depends_on_the_enclosing_schema_are_refused():
    base = obj({"a": S})
    for beside in ({"additionalProperties": False}, {"unevaluatedProperties": False},
                   {"patternProperties": {"^x": S}}):
        with pytest.raises(se.SchemaExtractionError) as err:
            se.inline_refs(_with_def(base, beside))
        assert err.value.code == "unsupported_ref_siblings"
    closed = {**obj({"a": S}), "additionalProperties": False}
    with pytest.raises(se.SchemaExtractionError) as err:
        se.inline_refs(_with_def(closed, {"properties": {"b": S}}))
    assert err.value.code == "unsupported_ref_siblings"
    # The same keyword written identically on both sides is not a conflict.
    se.inline_refs(_with_def(closed, {"additionalProperties": False}))


def test_the_extraction_itself_refuses_a_contradictory_schema_before_calling_a_model():
    with pytest.raises(se.SchemaExtractionError) as err:
        asyncio.run(se.extract_to_schema("u", text="Total 5", schema=_with_def({"type": "integer", "maximum": 5},
                                                                               {"minimum": 10}),
                                         models=object()))
    assert err.value.code == "contradictory_schema"


def test_prepare_refuses_what_it_cannot_check_and_names_what_it_did_not():
    with pytest.raises(se.SchemaExtractionError) as err:
        se.prepare_schema(obj({"x": {"type": "string", "if": {"const": "a"}}}))
    assert err.value.code == "unsupported_keyword" and "if" in str(err.value)
    with pytest.raises(se.SchemaExtractionError):
        se.prepare_schema({"type": "array", "items": S})
    _inlined, _checkable, unchecked = se.prepare_schema(INVOICE)
    assert unchecked == ["format"]


# ── envelope ──────────────────────────────────────────────────────────────

def test_the_envelope_makes_every_field_present_and_nullable_and_drops_constraints():
    schema = obj({"code": {"type": "string", "pattern": "^A-[0-9]+$", "minLength": 3},
                  "qty": {"type": "integer", "minimum": 1},
                  "status": {"type": "string", "enum": ["paid", "due"]},
                  "kind": {"const": "invoice"},
                  "seller": obj({"name": S}, required=("name",)),
                  "lines": {"type": "array", "minItems": 1, "items": obj({"amount": N}, required=("amount",))}},
                 required=("code",))
    env = se.envelope(schema)
    assert env["required"] == ["data", "evidence"] and env["additionalProperties"] is False
    data = env["properties"]["data"]
    assert sorted(data["required"]) == sorted(schema["properties"]) and data["additionalProperties"] is False
    props = data["properties"]
    assert props["code"]["type"] == ["string", "null"] and "pattern" not in props["code"]
    assert "minLength" not in props["code"] and "minimum" not in props["qty"]
    assert props["status"]["enum"] == ["paid", "due", None]
    assert {"type": "null"} in props["kind"]["anyOf"]
    assert props["seller"]["type"] == ["object", "null"]
    assert props["seller"]["properties"]["name"]["type"] == ["string", "null"]
    assert "minItems" not in props["lines"] and props["lines"]["type"] == ["array", "null"]
    assert props["lines"]["items"]["properties"]["amount"]["type"] == ["number", "null"]
    evidence = env["properties"]["evidence"]["items"]
    assert evidence["required"] == ["path", "quote", "unit"]
    from src.workflows import schema_check
    assert schema_check.validate({"data": {k: None for k in props}, "evidence": []}, env) == []


# ── grounding ─────────────────────────────────────────────────────────────

INVOICE_TEXT = ("FACTURA N.º F-2026-0042\n"
                "Fecha de emisión: 15 de marzo de 2026\n"
                "Base imponible: 1.020,30 €\n"
                "Total factura: 1.234,56 €\n"
                "Estado: PAGADO")


def test_an_unsupported_value_is_dropped_and_says_why():
    data = {"invoice_number": "F-2026-0042", "total": 1234.56, "currency": "USD", "issue_date": "2026-04-01"}
    evidence = [{"path": "invoice_number", "quote": "FACTURA N.º F-2026-0042", "unit": 1},
                {"path": "total", "quote": "Total factura: 1.234,56 €", "unit": 1},
                {"path": "currency", "quote": "Total in US dollars", "unit": 1},
                {"path": "issue_date", "quote": "Fecha de emisión: 15 de marzo de 2026", "unit": 1}]
    out = se.ground(data, evidence, INVOICE_TEXT, schema=INVOICE)
    assert out["data"] == {"invoice_number": "F-2026-0042", "total": 1234.56}
    why = {d["path"]: d["why"] for d in out["dropped"]}
    assert why["currency"] == "its quote is not in the document"
    assert why["issue_date"] == "the value is not in the quote given for it"
    assert out["schema_valid"] is True and out["missing_required"] == []


def test_a_value_with_no_quote_is_only_kept_when_the_document_itself_has_it():
    out = se.ground({"invoice_number": "F-2026-0042", "total": 999.0, "paid": True}, [], INVOICE_TEXT,
                    schema=INVOICE)
    assert out["data"] == {"invoice_number": "F-2026-0042", "total": None}
    kept = out["evidence"][0]
    assert kept["synthesised"] is True and "F-2026-0042" in kept["quote"]
    assert {d["path"] for d in out["dropped"]} == {"total", "paid"}
    assert out["missing_required"] == ["total"] and out["schema_valid"] is False


def test_an_ocr_noisy_quote_is_accepted_and_marked_fuzzy():
    # Noise in the words around the figures; the figures themselves are intact.
    scanned = "FACTURA N.º F-2026-0042\nTotaI factura: 1.234,56 €\nCIiente: Distribuciones Arnedo S.L."
    out = se.ground({"invoice_number": "F-2026-0042", "total": 1234.56},
                    [{"path": "invoice_number", "quote": "FACTURA N.º F-2026-0042", "unit": 1},
                     {"path": "total", "quote": "Total factura: 1.234,56 €", "unit": 1}],
                    scanned, [{"number": 1, "text": scanned}], schema=INVOICE)
    assert out["data"]["total"] == 1234.56
    total = next(e for e in out["evidence"] if e["path"] == "total")
    assert total["match"] == "fuzzy" and total["unit"] == 1
    assert "1.234,56" in total["document_text"] and "totai" in total["document_text"]
    # An amount the scan garbled ("l.234,56") is not "read" by the model into 1234.56: the document does not
    # contain that number, so it is dropped rather than corrected.
    garbled = scanned.replace("1.234,56", "l.234,56")
    bad = se.ground({"invoice_number": "F-2026-0042", "total": 1234.56},
                    [{"path": "total", "quote": "Total factura: 1.234,56 €", "unit": 1}],
                    garbled, [{"number": 1, "text": garbled}], schema=INVOICE)
    assert bad["data"]["total"] is None and bad["missing_required"] == ["total"]
    # ...but a quote that is merely similar to nothing in the page is not.
    off = se.ground({"invoice_number": "F-2026-0042", "total": 1234.56},
                    [{"path": "total", "quote": "Importe pendiente: 1.234,56 €", "unit": 1}],
                    scanned, [{"number": 1, "text": scanned}], schema=INVOICE)
    assert off["data"]["total"] is None and off["missing_required"] == ["total"]


@pytest.mark.parametrize("written,value", [
    ("Total: 1.234,56 €", 1234.56), ("Total: 1,234.56 USD", 1234.56), ("Total: 1 234,56 €", 1234.56),
    ("Importe: 12.500 €", 12500), ("IVA (21 %)", 0.21), ("Total: -45,00", -45.0),
])
def test_numbers_are_read_in_european_and_english_notation(written, value):
    out = se.ground({"total": value}, [{"path": "total", "quote": written, "unit": None}], written,
                    schema=obj({"total": N}))
    assert out["data"] == {"total": value}, out["dropped"]


@pytest.mark.parametrize("written", ["15/03/2026", "15-03-26", "15 de marzo de 2026", "March 15, 2026",
                                     "2026-03-15"])
def test_an_iso_date_matches_the_usual_written_forms(written):
    out = se.ground({"issue_date": "2026-03-15"}, [{"path": "issue_date", "quote": f"Fecha: {written}"}],
                    f"Fecha: {written}", schema=INVOICE)
    assert out["data"].get("issue_date") == "2026-03-15", out["dropped"]


def test_a_boolean_or_an_enum_worded_differently_is_kept_as_inferred():
    out = se.ground({"invoice_number": "F-2026-0042", "total": 1234.56, "paid": True, "currency": "EUR"},
                    [{"path": "invoice_number", "quote": "F-2026-0042"},
                     {"path": "total", "quote": "1.234,56 €"},
                     {"path": "paid", "quote": "Estado: PAGADO"},
                     {"path": "currency", "quote": "Total factura: 1.234,56 €"}],
                    INVOICE_TEXT, schema=INVOICE)
    assert out["data"]["paid"] is True and out["data"]["currency"] == "EUR"
    assert sorted(out["inferred"]) == ["currency", "paid"]


def test_array_items_are_grounded_by_their_own_quote_and_emptied_items_go():
    text = "1 x Tornillo M8 ...... 12,40\n2 x Arandela ...... 3,10\nTotal 15,50"
    data = {"invoice_number": None, "total": 15.5,
            "lines": [{"description": "Tuerca", "quantity": 4, "amount": 99.0},
                      {"description": "Tornillo M8", "quantity": 1, "amount": 12.4},
                      {"description": "Arandela", "quantity": 2, "amount": 3.1}]}
    evidence = [{"path": "total", "quote": "Total 15,50"},
                {"path": "lines[1]", "quote": "1 x Tornillo M8 ...... 12,40"},
                {"path": "data.lines.2", "quote": "2 x Arandela ...... 3,10"}]
    out = se.ground(data, evidence, text, schema=NESTED)
    assert out["data"]["lines"] == [{"description": "Tornillo M8", "quantity": 1, "amount": 12.4},
                                    {"description": "Arandela", "quantity": 2, "amount": 3.1}]
    paths = {e["path"] for e in out["evidence"]}
    assert {"lines[0].description", "lines[1].amount"} <= paths and "lines[2].amount" not in paths
    assert {d["path"] for d in out["dropped"]} >= {"lines[0].description", "lines[0].amount"}
    assert out["missing_required"] == ["invoice_number"] and out["schema_valid"] is False
    assert out["data"]["invoice_number"] is None


def test_a_required_field_the_document_lacks_is_never_filled():
    out = se.ground({"invoice_number": None, "total": None}, [], "nothing useful here", schema=INVOICE)
    assert out["data"] == {"invoice_number": None, "total": None}
    assert sorted(out["missing_required"]) == ["invoice_number", "total"]
    assert out["schema_valid"] is False and out["errors"]


TOTAL = obj({"total": N}, required=("total",))
BANK = obj({"iban": S}, required=("iban",))


def test_a_quote_rewritten_to_carry_a_changed_amount_does_not_ground_it():
    source = "Invoice total payable is 100.00 EUR for the supplied services."
    quote = "Invoice total payable is 900.00 EUR for the supplied services."
    out = se.ground({"total": 900}, [{"path": "total", "quote": quote, "unit": 77}], source,
                    [{"number": 1, "text": source}], schema=TOTAL)
    assert out["data"] == {"total": None} and out["evidence"] == []
    assert out["dropped"] == [{"path": "total", "value": 900,
                               "why": "the value is not in the document text the quote points to"}]
    assert out["missing_required"] == ["total"] and out["schema_valid"] is False
    # What the document does say at that place is accepted, whatever the quote says.
    real = se.ground({"total": 100}, [{"path": "total", "quote": quote, "unit": 1}], source,
                     [{"number": 1, "text": source}], schema=TOTAL)
    assert real["data"] == {"total": 100}
    kept = real["evidence"][0]
    assert kept["match"] == "fuzzy" and "100.00" in kept["document_text"] and "900" not in kept["document_text"]


def test_a_quote_rewritten_to_carry_a_changed_identifier_does_not_ground_it():
    source = "The bank account to pay is ES1234567890123456789012 for this invoice."
    forged = source.replace("ES1234567890123456789012", "ES9234567890123456789012")
    out = se.ground({"iban": "ES9234567890123456789012"}, [{"path": "iban", "quote": forged}], source,
                    [{"number": 1, "text": source}], schema=BANK)
    assert out["data"] == {"iban": None} and out["evidence"] == []
    assert out["dropped"][0]["why"] == "the value is not in the document text the quote points to"
    assert out["schema_valid"] is False
    # The same, one character at a time over a long identifier: none of them is read as the real one.
    for position in (2, 8, 14, 23):
        digit = "9" if source[source.index("ES12") + position] != "9" else "8"
        twisted = list("ES1234567890123456789012")
        twisted[position] = digit
        twisted = "".join(twisted)
        got = se.ground({"iban": twisted}, [{"path": "iban", "quote": source.replace("ES1234567890123456789012",
                                                                                 twisted)}],
                        source, [{"number": 1, "text": source}], schema=BANK)
        assert got["data"] == {"iban": None}, twisted


def test_a_changed_date_in_a_fuzzy_quote_is_dropped():
    source = "Fecha de vencimiento: 15/03/2026 según el contrato firmado entre las partes."
    quote = "Fecha de vencimiento: 25/03/2026 según el contrato firmado entre las partes."
    schema = obj({"due": {"type": "string", "format": "date"}})
    out = se.ground({"due": "2026-03-25"}, [{"path": "due", "quote": quote}], source,
                    [{"number": 1, "text": source}], schema=schema)
    assert out["data"] == {} and out["dropped"][0]["path"] == "due"
    ok = se.ground({"due": "2026-03-15"}, [{"path": "due", "quote": quote}], source,
                   [{"number": 1, "text": source}], schema=schema)
    assert ok["data"] == {"due": "2026-03-15"}


def test_ocr_noise_around_an_intact_identifier_or_amount_is_still_accepted():
    source = "TheI bank acc0unt to pay is ES1234567890123456789012 for thiS invoice. Totaal: 1.234,56 EUR"
    out = se.ground({"iban": "ES1234567890123456789012", "total": 1234.56},
                    [{"path": "iban", "quote": "The bank account to pay is ES1234567890123456789012 for this invoice."},
                     {"path": "total", "quote": "Total: 1.234,56 EUR"}],
                    source, [{"number": 3, "text": source}], schema=obj({"iban": S, "total": N}))
    assert out["data"] == {"iban": "ES1234567890123456789012", "total": 1234.56}, out["dropped"]
    assert {e["match"] for e in out["evidence"]} == {"fuzzy"} and {e["unit"] for e in out["evidence"]} == {3}
    # An identifier the document prints in blocks is the same identifier.
    grouped = se.ground({"iban": "ES1234567890123456789012"},
                        [{"path": "iban", "quote": "IBAN: ES12 3456 7890 1234 5678 9012"}],
                        "IBAN: ES12 3456 7890 1234 5678 9012", schema=BANK)
    assert grouped["data"] == {"iban": "ES1234567890123456789012"}


def test_a_number_is_not_found_inside_a_longer_number():
    assert se._contains("total: 1100,00", "100") is False
    assert se._contains("total: 100,00", "100") is True
    out = se.ground({"ref": "100"}, [{"path": "ref", "quote": "Ref 1100"}], "Ref 1100", schema=obj({"ref": S}))
    assert out["data"] == {} and out["dropped"][0]["path"] == "ref"


def test_a_page_the_model_invents_is_never_reported():
    out = se.ground({"value": "Alpha Beta"}, [{"path": "value", "quote": "Alpha Beta", "unit": 777}],
                    "Alpha Beta", schema=obj({"value": S}, required=("value",)))
    assert out["data"] == {"value": "Alpha Beta"}
    assert [e["unit"] for e in out["evidence"]] == [None]
    assert "777" not in json.dumps(out)
    assert {lim["code"] for lim in out["limits"]} >= {"no_units", "lexical_check_only"}
    # With pages, a page that does not exist is replaced by the one the reader gave.
    paged = se.ground({"value": "Alpha Beta"}, [{"path": "value", "quote": "Alpha Beta", "unit": 777}],
                      "Alpha Beta", [{"number": 4, "text": "Alpha Beta"}], schema=obj({"value": S}))
    assert paged["evidence"][0]["unit"] == 4 and paged["evidence"][0]["unit_claim_ignored"] is True
    assert "777" not in json.dumps(paged) and "no_units" not in {lim["code"] for lim in paged["limits"]}


def test_a_quote_on_page_one_is_reported_on_page_one_when_the_model_says_two():
    units = [{"number": 1, "text": "Invoice F-77 total 500.00 EUR"}, {"number": 2, "text": "Terms and conditions"}]
    text = "\n\n".join(u["text"] for u in units)
    out = se.ground({"total": 500}, [{"path": "total", "quote": "total 500.00 EUR", "unit": 2}], text, units,
                    schema=TOTAL)
    ev = out["evidence"][0]
    assert ev["unit"] == 1 and ev["unit_claim_ignored"] is True
    # A quote found on several pages goes to the page the model named when it is one of them.
    twice = [{"number": 1, "text": "total 500.00 EUR"}, {"number": 2, "text": "total 500.00 EUR"}]
    again = se.ground({"total": 500}, [{"path": "total", "quote": "total 500.00 EUR", "unit": 2}],
                      "total 500.00 EUR\n\ntotal 500.00 EUR", twice, schema=TOTAL)
    assert again["evidence"][0]["unit"] == 2 and "unit_claim_ignored" not in again["evidence"][0]


def test_a_quote_that_spans_two_pages_reports_both_and_a_fuzzy_one_too():
    units = [{"number": 7, "text": "Total payable carried over"}, {"number": 8, "text": "to next page: 880.00 EUR"}]
    text = "\n\n".join(u["text"] for u in units)
    out = se.ground({"total": 880}, [{"path": "total", "quote": "carried over to next page: 880.00 EUR",
                                      "unit": 8}], text, units, schema=TOTAL)
    ev = out["evidence"][0]
    assert ev["unit"] == 7 and ev["units"] == [7, 8] and "unit_claim_ignored" not in ev
    fuzzy = se.ground({"total": 880}, [{"path": "total", "quote": "carried 0ver to next page: 880.00 EUR"}],
                      text, units, schema=TOTAL)
    assert fuzzy["evidence"][0]["units"] == [7, 8] and fuzzy["evidence"][0]["match"] == "fuzzy"
    # ...and a changed amount in such a quote is dropped as everywhere else.
    forged = se.ground({"total": 980}, [{"path": "total", "quote": "carried 0ver to next page: 980.00 EUR"}],
                       text, units, schema=TOTAL)
    assert forged["data"] == {"total": None}


def test_the_result_says_the_check_is_lexical():
    out = se.ground({"total": 500}, [{"path": "total", "quote": "Total 500.00 EUR (other invoice)"}],
                    "Total 500.00 EUR (other invoice)", schema=TOTAL)
    note = next(lim["note"] for lim in out["limits"] if lim["code"] == "lexical_check_only")
    assert "does not prove" in note and "another invoice" in note


def test_paths_are_normalised_from_the_spellings_models_use():
    for spelling in ("$.data.lines[0].amount", "data.lines.0.amount", "/data/lines/0/amount",
                     "lines[0].amount", "$.lines.0.amount"):
        assert se.normalise_path(spelling) == "lines[0].amount", spelling
    assert se.normalise_path("data") == ""


# ── chunks ────────────────────────────────────────────────────────────────

def test_merging_chunks_keeps_the_first_value_records_conflicts_and_concatenates_arrays():
    first = {"data": {"invoice_number": "F-1", "total": 10.0, "lines": [{"description": "A", "amount": 4.0}]},
             "evidence": [{"path": "invoice_number", "quote": "Factura F-1"},
                          {"path": "lines[0].amount", "quote": "A 4,00"}],
             "dropped": [], "inferred": []}
    second = {"data": {"invoice_number": "F-2", "total": None,
                       "lines": [{"description": "A", "amount": 4.0}, {"description": "B", "amount": 6.0}]},
              "evidence": [{"path": "invoice_number", "quote": "Factura F-2"},
                           {"path": "lines[1].amount", "quote": "B 6,00"},
                           {"path": "lines[0].amount", "quote": "A 4,00"}],
              "dropped": [{"path": "total", "value": 1, "why": "x"}], "inferred": []}
    out = se.merge_chunks([first, second])
    assert out["data"]["invoice_number"] == "F-1" and out["data"]["total"] == 10.0
    assert out["data"]["lines"] == [{"description": "A", "amount": 4.0}, {"description": "B", "amount": 6.0}]
    assert out["conflicts"] == [{"path": "invoice_number", "kept": "F-1", "other": "F-2", "chunk": 1,
                                 "other_quotes": ["Factura F-2"]}]
    paths = [(e["path"], e["quote"]) for e in out["evidence"]]
    assert ("lines[1].amount", "B 6,00") in paths
    assert paths.count(("lines[0].amount", "A 4,00")) == 1
    assert out["dropped"][0]["chunk"] == 1


def test_units_are_grouped_into_chunks_and_long_text_is_split_at_paragraphs():
    units = [{"kind": "page", "number": i + 1, "text": f"page {i + 1}\n" + "x" * 5000} for i in range(5)]
    chunks = se.chunk_units(units, limit=12_000)
    assert [[u["number"] for u in c] for c in chunks] == [[1, 2], [3, 4], [5]]
    parts = se.units_from_text(("párrafo " * 300 + "\n\n") * 20, limit=5000)
    assert len(parts) > 1 and all(len(p["text"]) <= 5000 for p in parts)
    assert [p["number"] for p in parts] == list(range(1, len(parts) + 1))


# ── the whole extraction, with a scripted model ───────────────────────────

class Script:
    """`ModelCalls`-shaped fake: answers in order, records what it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: List[Dict[str, Any]] = []

    def complete(self, messages, **opts):
        self.calls.append({"messages": messages, **opts})
        if not self.answers:
            raise ModelUnavailable("the script ran out of answers")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)

    def decide(self, *a, **k):
        return {}


ROUTES = {"utility": (LOCAL, "small", {}), "extraction": (LOCAL, "small", {}), "default": (LOCAL, "big", {})}


def run(coro):
    return asyncio.run(coro)


def extract(models, *, routes=ROUTES, **kwargs):
    kwargs.setdefault("schema", INVOICE)
    kwargs.setdefault("text", INVOICE_TEXT)
    return run(se.extract_to_schema("alice", models=models, resolver=lambda p, o: routes[p],
                                    json_ok=lambda m: None, **kwargs))


GOOD = {"data": {"invoice_number": "F-2026-0042", "issue_date": "2026-03-15", "total": 1234.56,
                 "currency": None, "paid": None},
        "evidence": [{"path": "invoice_number", "quote": "FACTURA N.º F-2026-0042", "unit": 1},
                     {"path": "issue_date", "quote": "Fecha de emisión: 15 de marzo de 2026", "unit": 1},
                     {"path": "total", "quote": "Total factura: 1.234,56 €", "unit": 1}]}


def test_a_clean_answer_is_grounded_and_routed_with_the_schema_on_the_wire():
    models = Script(GOOD)
    out = extract(models)
    assert out["data"] == {"invoice_number": "F-2026-0042", "issue_date": "2026-03-15", "total": 1234.56}
    assert out["schema_valid"] and out["complete"] and not out["repaired"] and not out["escalated"]
    assert out["route"]["tier"] == "simple" and out["route"]["purpose"] == "utility"
    assert out["route"]["model"] == "small" and "_url" not in out["route"]
    call = models.calls[0]
    assert call["purpose"] == "utility" and call["temperature"] == 0.0
    assert call["response_schema"]["properties"]["data"]["properties"]["total"]["type"] == ["number", "null"]
    assert "tools" not in call
    assert out["source"]["kind"] == "text" and out["input_truncated"] is False


def test_one_repair_with_the_exact_errors_then_success():
    models = Script("not json at all", GOOD)
    out = extract(models)
    assert out["repaired"] is True and out["escalated"] is False and out["data"]["total"] == 1234.56
    repair = json.dumps(models.calls[1]["messages"])
    assert "not valid JSON" in repair or "no JSON value" in repair
    assert models.calls[1]["response_schema"] == models.calls[0]["response_schema"]


def test_a_failed_repair_escalates_once_to_the_next_tier_and_no_further():
    models = Script("nope", "still nope", GOOD)
    out = extract(models)
    assert out["escalated"] is True and out["data"]["invoice_number"] == "F-2026-0042"
    assert out["escalation"]["from"] == {"purpose": "utility", "model": "small"}
    assert out["escalation"]["to"] == {"purpose": "default", "model": "big"}
    assert [c["purpose"] for c in models.calls] == ["utility", "utility", "default"]
    assert out["route"]["model"] == "big"

    stubborn = Script("nope", "nope", "nope", "nope")
    failed = extract(stubborn)
    assert len(stubborn.calls) == 4 and failed["escalated"] is True
    assert failed["complete"] is False and failed["schema_valid"] is False
    assert sorted(failed["missing_required"]) == ["invoice_number", "total"]
    assert "after one repair" in failed["errors"][0]


def test_escalation_never_moves_to_a_paid_endpoint():
    routes = {**ROUTES, "default": (PAID, "cloud", {})}
    models = Script("nope", "nope")
    out = extract(models, routes=routes)
    assert out["escalated"] is False and len(models.calls) == 2
    assert "paid endpoint" in out["escalation"]["why_not"]


def test_an_unreachable_model_escalates_like_a_bad_answer():
    models = Script(ModelUnavailable("connection refused"), GOOD)
    out = extract(models)
    assert out["escalated"] is True and out["schema_valid"] is True


def test_a_long_document_is_extracted_in_chunks_and_merged():
    page1 = "FACTURA N.º F-2026-0042\n" + ("Condiciones generales. " * 400)
    page2 = ("Detalle de servicios. " * 400) + "\nTotal factura: 1.234,56 €"
    pages = {"ok": True, "kind": "pdf", "via": "local", "needs_ocr": False,
             "units": [{"kind": "page", "number": 1, "text": page1}, {"kind": "page", "number": 2, "text": page2}]}
    first = {"data": {"invoice_number": "F-2026-0042", "total": None},
             "evidence": [{"path": "invoice_number", "quote": "FACTURA N.º F-2026-0042", "unit": 1}]}
    second = {"data": {"invoice_number": "F-2026-0099", "total": 1234.56},
              "evidence": [{"path": "total", "quote": "Total factura: 1.234,56 €", "unit": 2},
                           {"path": "invoice_number", "quote": "Total factura", "unit": 2}]}
    models = Script(first, second)
    out = run(se.extract_to_schema("alice", path="/docs/factura.pdf", schema=INVOICE, models=models,
                                   resolver=lambda p, o: ROUTES[p], json_ok=lambda m: None,
                                   reader=lambda path, ocr: pages))
    assert out["chunks"]["total"] == 2 and out["source"]["pages"] == 2
    assert out["data"] == {"invoice_number": "F-2026-0042", "total": 1234.56}
    assert next(e for e in out["evidence"] if e["path"] == "total")["unit"] == 2
    # The second chunk's invoice number had a quote without it: dropped, not a conflict.
    assert any(d["path"] == "invoice_number" and d["chunk"] == 1 for d in out["dropped"])
    assert out["route"]["tier"] == "medium" and "long input" in " ".join(out["route"]["reasons"])
    assert "[[page 2]]" in models.calls[1]["messages"][-1]["content"]


def test_an_unreadable_document_and_bad_arguments_are_refused_by_name():
    with pytest.raises(se.SchemaExtractionError) as err:
        run(se.extract_to_schema("a", path="/x.pdf", schema=INVOICE, models=Script(),
                                 resolver=lambda p, o: ROUTES[p], json_ok=lambda m: None,
                                 reader=lambda path, ocr: {"ok": False, "error": "damaged file"}))
    assert err.value.code == "unreadable" and "damaged file" in str(err.value)
    with pytest.raises(se.SchemaExtractionError) as err:
        run(se.extract_to_schema("a", path="/scan.pdf", schema=INVOICE, models=Script(),
                                 resolver=lambda p, o: ROUTES[p], json_ok=lambda m: None,
                                 reader=lambda path, ocr: {"ok": True, "units": [], "text": "", "needs_ocr": True}))
    assert err.value.code == "no_text" and "OCR" in str(err.value)
    for bad in ({"text": "x", "path": "/y"}, {"text": " "}, {"text": "x", "tier": "huge"},
                {"text": "x", "ocr": "maybe"}):
        with pytest.raises(se.SchemaExtractionError):
            run(se.extract_to_schema("a", schema=INVOICE, models=Script(), **bad))


def test_the_document_is_read_locally_when_the_family_service_cannot_be_used(monkeypatch, tmp_path):
    doc = tmp_path / "factura.txt"
    doc.write_text(INVOICE_TEXT, encoding="utf-8")
    monkeypatch.setattr("src.family_services.extract", lambda path, **kw: {
        "ok": False, "kind": "auth", "error": "the hub refused this app's token", "via": "kafka"})
    got = se._default_reader(str(doc), "auto")
    assert got["ok"] is True and got["via"] == "local" and "F-2026-0042" in got["text"]
    assert "not usable" in got["notes"][-1]
    # A document the service read and refused is not retried behind its back.
    monkeypatch.setattr("src.family_services.extract", lambda path, **kw: {
        "ok": False, "kind": "tool_error", "error": "damaged file", "via": "kafka"})
    assert se._default_reader(str(doc), "auto")["error"] == "damaged file"


# ── saved schemas ─────────────────────────────────────────────────────────

def test_saved_schemas_live_under_the_owner_and_names_cannot_leave_the_folder(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    record = se.save_schema("alice", "factura", INVOICE, "Facturas de proveedor")
    assert record["name"] == "factura"
    assert (tmp_path / "extraction_schemas" / "alice" / "factura.json").is_file()
    assert se.get_schema("alice", "factura")["schema"] == INVOICE
    assert se.get_schema("bob", "factura") is None
    assert [s["name"] for s in se.list_schemas("alice")] == ["factura"]
    assert se.list_schemas("alice")[0]["tier"] == "simple"
    for bad in ("../x", "..", "a/b", "a\\b", ".hidden", "x.json", "", "a" * 65, "C:x"):
        with pytest.raises(se.SchemaExtractionError) as err:
            se.save_schema("alice", bad, INVOICE)
        assert err.value.code == "invalid_name"
    with pytest.raises(se.SchemaExtractionError):
        se.save_schema("alice", "loop", {"type": "object", "properties": {"n": {"$ref": "#/$defs/N"}},
                                         "$defs": {"N": obj({"next": {"$ref": "#/$defs/N"}})}})
    weird = se.save_schema("../../etc", "x", INVOICE)
    assert weird["name"] == "x"
    assert all(p.is_relative_to(tmp_path / "extraction_schemas") for p in tmp_path.rglob("*.json"))
    assert se.delete_schema("alice", "factura") is True and se.delete_schema("alice", "factura") is False
