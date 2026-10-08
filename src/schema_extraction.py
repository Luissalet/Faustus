"""src/schema_extraction.py -- extract a document into a JSON Schema the user wrote (OBJ-24).

The user hands over a document (a path read through the family's document
service, or plain text) and a JSON Schema. What comes back is the data in that
shape, where every value can be traced to the words of the document that
contain it, and every value that cannot is null and listed as dropped.

The pipeline, and where each rule lives:

* :func:`schema_profile` measures the schema (fields, nesting, arrays of
  objects, unions, ``$ref``) and gives it a tier: ``simple``, ``medium`` or
  ``complex``. A very long input moves it up one tier; enums never do.
* :func:`route_for_schema` turns the tier into a model purpose: ``simple`` ->
  ``utility``, ``medium`` -> ``extraction`` (which falls back to utility, then
  default, in ``endpoint_resolver.resolve_endpoint``), ``complex`` ->
  ``default``. A model whose calibration says JSON mode fails is skipped, and
  the route never moves from a local model to a paid one on its own.
* :func:`envelope` wraps the user's schema as ``{"data": ..., "evidence":
  [{path, quote, unit}]}`` with every field nullable and present, and is sent
  as ``response_schema`` so a local engine decodes under it. Constraints
  (patterns, lengths, ranges, minItems) are left out of the envelope on
  purpose: a grammar that insists on a matching value is how a model is pushed
  into inventing one. They are checked afterwards, on the original schema.
* :func:`ground` keeps a value only when a quote for it appears in the source
  (accent/case/space-insensitive, OCR noise tolerated at a 0.9 similarity) and
  the value is in that quote (numbers read in both European and English
  notation, ISO dates against the usual written forms). Anything else becomes
  null and goes to ``dropped`` with the reason. Booleans and enum values that
  map a wording ("PAGADO" -> "paid") are kept as ``inferred``.
* :func:`finalize` validates against the user's ORIGINAL schema. A required
  field that ended up null stays null and is listed in ``missing_required``
  with ``schema_valid: false``; it is never filled in.
* :func:`merge_chunks` joins the per-chunk results of a long document: the
  first supported value wins, a different later value is a recorded conflict,
  arrays are concatenated and de-duplicated.

:func:`extract_to_schema` runs all of it: one model call per chunk, separate
from any tool-calling request, one repair through the workflow ``structured``
helper, at most one escalation to the next tier (never to a paid endpoint).
"""
from __future__ import annotations

import asyncio
import copy
import difflib
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "SchemaExtractionError", "TIERS", "PURPOSE_FOR_TIER", "schema_profile", "inline_refs", "prepare_schema",
    "envelope", "ground", "finalize", "merge_chunks", "route_for_schema", "units_from_text", "chunk_units",
    "extract_to_schema", "list_schemas", "get_schema", "save_schema", "delete_schema", "valid_schema_name",
]

TIERS = ("simple", "medium", "complex")
PURPOSE_FOR_TIER = {"simple": "utility", "medium": "extraction", "complex": "default"}
PURPOSE_LADDER = ("utility", "extraction", "default")

#: Above this many characters of input a schema is treated one tier harder.
LONG_INPUT_CHARS = 12_000
#: Characters of document per model call.
CHUNK_CHARS = 12_000
#: Characters of document read at most; the rest is reported as truncated.
DEFAULT_MAX_CHARS = 96_000
MAX_INPUT_CHARS = 400_000
MAX_SCHEMA_BYTES = 64_000
FUZZY_QUOTE_RATIO = 0.9
MAX_QUOTE_CHARS = 600
DEFAULT_TIMEOUT_S = 300.0

#: Keywords that only describe; stripped before validation and listed as unchecked.
_ANNOTATIONS = frozenset({"format", "$comment", "readOnly", "writeOnly", "deprecated", "contentMediaType",
                          "contentEncoding", "examples", "default", "title", "description", "$schema", "$id"})
#: Annotations the schema checker does not know about (the rest it accepts as annotations already).
_UNCHECKED = frozenset({"format", "$comment", "readOnly", "writeOnly", "deprecated", "contentMediaType",
                        "contentEncoding"})
_CONSTRAINTS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
                "pattern", "minItems", "maxItems", "format", "multipleOf", "uniqueItems")
#: Left out of the envelope (checked after grounding, on the original schema).
_ENVELOPE_DROP = frozenset(_CONSTRAINTS) | {"$schema", "$id", "$comment", "examples", "default", "readOnly",
                                           "writeOnly", "deprecated", "contentMediaType", "contentEncoding"}
_SCALARS = frozenset({"string", "number", "integer", "boolean", "null"})
_SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_ONE = ("items", "additionalProperties", "not", "contains", "if", "then", "else", "propertyNames",
               "additionalItems", "unevaluatedProperties", "unevaluatedItems")


class SchemaExtractionError(ValueError):
    """A request that cannot be served as asked; `code` is stable for callers."""

    def __init__(self, message: str, code: str = "invalid_schema"):
        super().__init__(message)
        self.code = code


# ── schema helpers ────────────────────────────────────────────────────────

def _types(node: Any) -> set:
    if not isinstance(node, Mapping):
        return set()
    declared = node.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, (list, tuple)):
        return {t for t in declared if isinstance(t, str)}
    if isinstance(node.get("properties"), Mapping):
        return {"object"}
    if "items" in node:
        return {"array"}
    return set()


def _is_object(node: Any) -> bool:
    return "object" in _types(node)


def _is_array(node: Any) -> bool:
    return "array" in _types(node)


def _pointer_token(name: str) -> str:
    return str(name).replace("~", "~0").replace("/", "~1")


def inline_refs(schema: Any) -> Dict[str, Any]:
    """`schema` with every local ``$ref`` (``#/$defs/X``, ``#/definitions/X``)
    replaced by a copy of its target and the definition blocks removed.

    A ``$ref`` beside other keywords keeps them (they override the target's).
    Raises :class:`SchemaExtractionError` for a remote or dangling reference
    and, with code ``recursive_schema``, for one that refers back to itself:
    a recursive shape cannot be written out as a finite schema."""
    if not isinstance(schema, Mapping):
        raise SchemaExtractionError("the schema must be a JSON object")
    defs: Dict[str, Any] = {}
    for block in ("$defs", "definitions"):
        found = schema.get(block)
        if isinstance(found, Mapping):
            for name, sub in found.items():
                defs[f"#/{block}/{_pointer_token(name)}"] = sub

    def resolve(node: Any, stack: Tuple[str, ...], depth: int) -> Any:
        if depth > 64:
            raise SchemaExtractionError("the schema is nested more than 64 levels deep", "schema_too_deep")
        if not isinstance(node, Mapping):
            return copy.deepcopy(node)
        ref = node.get("$ref")
        if ref is not None:
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise SchemaExtractionError(
                    f"only local references like '#/$defs/Name' can be resolved, not {ref!r}", "unsupported_ref")
            if ref not in defs:
                raise SchemaExtractionError(f"$ref {ref!r} points to no definition", "dangling_ref")
            if ref in stack:
                chain = " -> ".join(stack + (ref,))
                raise SchemaExtractionError(
                    f"$ref {ref!r} is recursive ({chain}); a recursive schema cannot be extracted as a finite "
                    "shape. Give the nesting a fixed depth instead", "recursive_schema")
            merged = dict(resolve(defs[ref], stack + (ref,), depth + 1))
            for key, value in node.items():
                if key != "$ref":
                    merged[key] = _resolve_child(key, value, stack, depth, resolve)
            return merged
        out: Dict[str, Any] = {}
        for key, value in node.items():
            if key in ("$defs", "definitions"):
                continue
            out[key] = _resolve_child(key, value, stack, depth, resolve)
        return out

    return resolve(schema, (), 0)


def _resolve_child(key: str, value: Any, stack, depth: int, resolve: Callable) -> Any:
    if key in _SCHEMA_MAPS and isinstance(value, Mapping):
        return {name: resolve(sub, stack, depth + 1) for name, sub in value.items()}
    if key in _SCHEMA_LISTS and isinstance(value, (list, tuple)):
        return [resolve(sub, stack, depth + 1) for sub in value]
    if key in _SCHEMA_ONE and isinstance(value, Mapping):
        return resolve(value, stack, depth + 1)
    if key == "items" and isinstance(value, (list, tuple)):
        return [resolve(sub, stack, depth + 1) for sub in value]
    return copy.deepcopy(value)


def _count_refs(node: Any) -> int:
    if isinstance(node, Mapping):
        own = 1 if isinstance(node.get("$ref"), str) else 0
        total = own
        for key, value in node.items():
            if key in _SCHEMA_MAPS and isinstance(value, Mapping):
                total += sum(_count_refs(sub) for sub in value.values())
            elif key in _SCHEMA_LISTS + ("items",) and isinstance(value, (list, tuple)):
                total += sum(_count_refs(sub) for sub in value)
            elif key in _SCHEMA_ONE and isinstance(value, Mapping):
                total += _count_refs(value)
        return total
    return 0


def _strip_refs(node: Any) -> Any:
    """For profiling a recursive schema: every ``$ref`` becomes an empty leaf."""
    if not isinstance(node, Mapping):
        return node
    if "$ref" in node:
        return {k: v for k, v in node.items() if k not in ("$ref",) and k not in _SCHEMA_MAPS}
    out = {}
    for key, value in node.items():
        if key in ("$defs", "definitions"):
            continue
        if key in _SCHEMA_MAPS and isinstance(value, Mapping):
            out[key] = {n: _strip_refs(s) for n, s in value.items()}
        elif key in _SCHEMA_LISTS + ("items",) and isinstance(value, (list, tuple)):
            out[key] = [_strip_refs(s) for s in value]
        elif key in _SCHEMA_ONE and isinstance(value, Mapping):
            out[key] = _strip_refs(value)
        else:
            out[key] = value
    return out


def _strip_unchecked(node: Any, found: set) -> Any:
    """`node` without the annotation keywords the checker would refuse."""
    if isinstance(node, list):
        return [_strip_unchecked(x, found) for x in node]
    if not isinstance(node, Mapping):
        return node
    out = {}
    for key, value in node.items():
        if key in _UNCHECKED:
            found.add(key)
            continue
        if key in _SCHEMA_MAPS and isinstance(value, Mapping):
            out[key] = {n: _strip_unchecked(s, found) for n, s in value.items()}
        elif key in _SCHEMA_LISTS + ("items",) and isinstance(value, (list, tuple)):
            out[key] = [_strip_unchecked(s, found) for s in value]
        elif key in _SCHEMA_ONE and isinstance(value, Mapping):
            out[key] = _strip_unchecked(value, found)
        else:
            out[key] = value
    return out


def prepare_schema(schema: Any) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """`(inlined, checkable, unchecked_keywords)` for a schema extraction can
    use, or :class:`SchemaExtractionError` saying why it cannot.

    `inlined` is the schema with local references resolved (what the model is
    shown); `checkable` additionally drops the annotation keywords the
    validator does not enforce (``format`` and the like), which are returned
    by name so the caller can say what was not checked."""
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except ValueError as exc:
            raise SchemaExtractionError(f"the schema is not valid JSON ({exc})")
    if not isinstance(schema, Mapping) or not schema:
        raise SchemaExtractionError("the schema must be a non-empty JSON object")
    if len(json.dumps(schema, ensure_ascii=False)) > MAX_SCHEMA_BYTES:
        raise SchemaExtractionError(f"the schema is larger than {MAX_SCHEMA_BYTES} bytes", "schema_too_large")
    inlined = inline_refs(schema)
    if not _is_object(inlined):
        raise SchemaExtractionError('the schema must describe an object ("type": "object") with named fields')
    found: set = set()
    checkable = _strip_unchecked(inlined, found)
    from src.workflows import schema_check
    problems = schema_check.schema_problems(checkable)
    if problems:
        raise SchemaExtractionError("the schema uses something extraction cannot check: " + "; ".join(problems[:6]),
                                    "unsupported_keyword")
    return inlined, checkable, sorted(found)


# ── profile and tier ──────────────────────────────────────────────────────

def _non_null_branches(node: Mapping[str, Any]) -> List[Any]:
    branches = node.get("anyOf") or node.get("oneOf") or []
    if not isinstance(branches, (list, tuple)):
        return []
    return [b for b in branches if not (isinstance(b, Mapping) and _types(b) == {"null"})]


def _walk_profile(node: Any, depth: int, st: Dict[str, int]) -> None:
    if not isinstance(node, Mapping):
        st["leaves"] += 1
        return
    st["constraints"] += sum(1 for key in _CONSTRAINTS if key in node)
    if "enum" in node or "const" in node:
        st["enums"] += 1
    branches = _non_null_branches(node)
    if branches:
        if len(branches) > 1:
            st["unions"] += 1
        shaped = [b for b in branches if _is_object(b) or _is_array(b)]
        if not shaped:
            st["leaves"] += 1
        for branch in shaped:
            _walk_profile(branch, depth, st)
        return
    for branch in node.get("allOf") or []:
        if isinstance(branch, Mapping) and (_is_object(branch) or _is_array(branch)):
            _walk_profile(branch, depth, st)
    types = _types(node)
    if "object" in types:
        st["max_depth"] = max(st["max_depth"], depth)
        st["required"] += len([r for r in (node.get("required") or []) if isinstance(r, str)])
        props = node.get("properties") if isinstance(node.get("properties"), Mapping) else {}
        for sub in props.values():
            _walk_profile(sub, depth + 1, st)
        extra = node.get("additionalProperties")
        if isinstance(extra, Mapping) and extra:
            _walk_profile(extra, depth + 1, st)
        if not props and not (isinstance(extra, Mapping) and extra):
            st["leaves"] += 1                      # a free-form object is one value
        return
    if "array" in types:
        items = node.get("items")
        if isinstance(items, Mapping) and _is_object(items):
            st["object_arrays"] += 1
            _walk_profile(items, depth + 1, st)
        elif isinstance(items, Mapping) and (_is_array(items) or _non_null_branches(items)):
            _walk_profile(items, depth + 1, st)
        else:
            st["leaves"] += 1                      # a list of scalars is one field
        return
    st["leaves"] += 1


def schema_profile(schema: Any, *, input_chars: int = 0) -> Dict[str, Any]:
    """How hard `schema` is to fill, and the tier that follows.

    complex: recursive ``$ref``, nesting depth >= 4, more than 30 fields, two or
    more arrays of objects, or any union (anyOf/oneOf with more than one
    non-null shape). simple: at most 8 fields, depth <= 2, no array of objects
    and no ``$ref``. Everything else is medium. More than ``LONG_INPUT_CHARS``
    of input moves simple and medium one tier up. Enums never change the tier:
    a closed list of answers is easier, not harder."""
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except ValueError as exc:
            raise SchemaExtractionError(f"the schema is not valid JSON ({exc})")
    if not isinstance(schema, Mapping) or not schema:
        raise SchemaExtractionError("the schema must be a non-empty JSON object")
    refs = _count_refs(schema)
    recursive = False
    try:
        body = inline_refs(schema)
    except SchemaExtractionError as exc:
        if exc.code != "recursive_schema":
            raise
        recursive = True
        body = _strip_refs(schema)
    st = {"leaves": 0, "max_depth": 0, "object_arrays": 0, "enums": 0, "unions": 0, "constraints": 0,
          "required": 0}
    _walk_profile(body, 1, st)

    hard: List[str] = []
    if recursive:
        hard.append("recursive $ref")
    if st["max_depth"] >= 4:
        hard.append(f"nesting depth {st['max_depth']} (4 or more)")
    if st["leaves"] > 30:
        hard.append(f"{st['leaves']} fields (more than 30)")
    if st["object_arrays"] >= 2:
        hard.append(f"{st['object_arrays']} arrays of objects (2 or more)")
    if st["unions"] > 0:
        hard.append(f"{st['unions']} union(s) of different shapes")
    if hard:
        tier, reasons = "complex", hard
    else:
        easy_misses = []
        if st["leaves"] > 8:
            easy_misses.append(f"{st['leaves']} fields (more than 8)")
        if st["max_depth"] > 2:
            easy_misses.append(f"nesting depth {st['max_depth']} (more than 2)")
        if st["object_arrays"]:
            easy_misses.append("an array of objects")
        if refs:
            easy_misses.append(f"{refs} $ref")
        if easy_misses:
            tier, reasons = "medium", easy_misses
        else:
            tier = "simple"
            reasons = [f"{st['leaves']} fields, depth {st['max_depth']}, no arrays of objects, no $ref"]
    base_tier = tier
    chars = max(0, int(input_chars or 0))
    if chars > LONG_INPUT_CHARS and tier != "complex":
        tier = TIERS[TIERS.index(tier) + 1]
        reasons = reasons + [f"long input ({chars} characters, more than {LONG_INPUT_CHARS}) moves it up one tier"]
    return {**st, "refs": refs, "recursive": recursive, "input_chars": chars, "base_tier": base_tier,
            "tier": tier, "reasons": reasons}


# ── envelope ──────────────────────────────────────────────────────────────

EVIDENCE_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "field path inside data, e.g. total or lines[0].amount"},
            "quote": {"type": "string", "description": "the exact words of the document that contain the value"},
            "unit": {"type": ["integer", "null"], "description": "page or part number where the quote is"},
        },
        "required": ["path", "quote", "unit"],
        "additionalProperties": False,
    },
}


def _nullable(node: Dict[str, Any]) -> Dict[str, Any]:
    node = dict(node)
    if "const" in node:
        return {"anyOf": [node, {"type": "null"}]}
    if isinstance(node.get("enum"), list) and None not in node["enum"]:
        node["enum"] = list(node["enum"]) + [None]
    declared = node.get("type")
    if isinstance(declared, str):
        if declared != "null":
            node["type"] = [declared, "null"]
        return node
    if isinstance(declared, list):
        if "null" not in declared:
            node["type"] = list(declared) + ["null"]
        return node
    for key in ("anyOf", "oneOf"):
        branches = node.get(key)
        if isinstance(branches, list):
            if not any(isinstance(b, Mapping) and _types(b) == {"null"} for b in branches):
                node[key] = list(branches) + [{"type": "null"}]
            return node
    if "enum" in node:
        return node
    return {"anyOf": [node, {"type": "null"}]}


def _envelope_node(node: Any) -> Any:
    if not isinstance(node, Mapping):
        return node
    out: Dict[str, Any] = {}
    hint = node.get("format")
    for key, value in node.items():
        if key in _ENVELOPE_DROP:
            continue
        if key == "properties" and isinstance(value, Mapping):
            out[key] = {name: _nullable(_envelope_node(sub)) if isinstance(sub, Mapping) else sub
                        for name, sub in value.items()}
        elif key in ("anyOf", "oneOf", "allOf") and isinstance(value, (list, tuple)):
            out[key] = [_envelope_node(sub) for sub in value]
        elif key in ("items", "additionalProperties") and isinstance(value, Mapping):
            out[key] = _envelope_node(value)
        else:
            out[key] = copy.deepcopy(value)
    if isinstance(hint, str) and hint:
        out["description"] = ((str(out.get("description") or "") + " ").lstrip()
                               + f"(format: {hint}" + ("; write it as YYYY-MM-DD)" if hint == "date" else ")"))
    if isinstance(out.get("properties"), Mapping):
        out["required"] = list(out["properties"].keys())
        if "additionalProperties" not in out:
            out["additionalProperties"] = False
    return out


def envelope(schema: Any) -> Dict[str, Any]:
    """The response schema sent to the model: ``{"data": <the user's schema,
    every field present and nullable>, "evidence": [{path, quote, unit}]}``.

    Every field is nullable, required ones included: a model that is not
    allowed to say "not in the document" is a model told to invent. A required
    field that comes back null is reported in ``missing_required`` instead."""
    inlined = inline_refs(schema) if not isinstance(schema, Mapping) or _count_refs(schema) or \
        "$defs" in schema or "definitions" in schema else dict(schema)
    if not _is_object(inlined):
        raise SchemaExtractionError('the schema must describe an object ("type": "object")')
    data = _envelope_node(inlined)
    data.pop("title", None)
    return {
        "type": "object",
        "properties": {"data": data, "evidence": copy.deepcopy(EVIDENCE_SCHEMA)},
        "required": ["data", "evidence"],
        "additionalProperties": False,
    }


# ── paths ─────────────────────────────────────────────────────────────────

_PATH_INDEX = re.compile(r"\.(\d+)(?=\.|\[|$)")


def normalise_path(path: Any) -> str:
    """`lines[0].amount` from the spellings models use: ``$.data.lines[0].amount``,
    ``data.lines.0.amount``, ``/data/lines/0/amount``."""
    text = str(path or "").strip().replace(" ", "")
    if text.startswith("/"):
        parts = [p.replace("~1", "/").replace("~0", "~") for p in text.strip("/").split("/") if p != ""]
        text = ""
        for part in parts:
            text += f"[{part}]" if part.isdigit() else (("." if text else "") + part)
    text = re.sub(r"^\$\.?", "", text)
    text = re.sub(r"^data(?=\.|\[|$)\.?", "", text)
    text = text.lstrip(".")
    text = _PATH_INDEX.sub(lambda m: f"[{m.group(1)}]", "." + text)[1:] if text else ""
    text = re.sub(r"\[['\"]?([^\]'\"]+)['\"]?\]", lambda m: f"[{m.group(1)}]" if m.group(1).isdigit()
                  else f".{m.group(1)}", text)
    return text.lstrip(".")


def _join(parent: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{parent}[{key}]"
    return f"{parent}.{key}" if parent else str(key)


def _ancestors(path: str) -> List[str]:
    """`a[0].b` -> [`a[0].b`, `a[0]`, `a`]: the paths a quote may be given for."""
    out = [path]
    current = path
    while current:
        cut = max(current.rfind("."), current.rfind("["))
        if cut <= 0:
            break
        current = current[:cut]
        out.append(current)
    return out


# ── text matching ─────────────────────────────────────────────────────────

_WS = re.compile(r"\s+")
_PUNCT_MAP = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201c": '"', "\u201d": '"',
                            "\u201e": '"', "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u00ab": '"',
                            "\u00bb": '"'})


def norm_text(text: Any) -> str:
    """NFKC, case-folded, typographic quotes and dashes made plain, whitespace collapsed."""
    value = unicodedata.normalize("NFKC", str(text or "")).translate(_PUNCT_MAP).casefold()
    return _WS.sub(" ", value).strip()


def _fuzzy_find(needle: str, hay: str, threshold: float = FUZZY_QUOTE_RATIO) -> float:
    """The best similarity of `needle` to a same-length window of `hay` near
    a place where a piece of it occurs exactly; 0.0 when nothing comes close.
    Anchored on short exact pieces so a long document is not compared
    character by character."""
    size = len(needle)
    if size < 6 or not hay:
        return 0.0
    gram = 4 if size < 16 else 6
    starts: set = set()
    step = max(1, (size - gram) // 10)
    for offset in range(0, max(1, size - gram + 1), step):
        piece = needle[offset:offset + gram]
        position = hay.find(piece)
        hits = 0
        while position >= 0 and hits < 8:
            starts.add(max(0, position - offset))
            position = hay.find(piece, position + 1)
            hits += 1
        if len(starts) > 80:
            break
    best = 0.0
    for start in starts:
        for shift in (-2, -1, 0, 1, 2):
            begin = max(0, start + shift)
            for length in (size - 1, size, size + 1):
                window = hay[begin:begin + length]
                if not window:
                    continue
                ratio = difflib.SequenceMatcher(None, needle, window, autojunk=False).ratio()
                if ratio > best:
                    best = ratio
                    if best >= 0.999:
                        return best
    return best if best >= threshold else 0.0


# Numbers as written: digits with optional thousands/decimal separators.
_NUM_TOKEN = re.compile(r"[-\u2212]?\d(?:[\d.,'\u00a0\u202f ]*\d)?")
_SPACE_GROUPED = re.compile(r"^[-\u2212]?\d{1,3}(?: \d{3})+(?:[.,]\d+)?$")


def _readings(token: str) -> List[Decimal]:
    """Every value a written number can mean (``1.234`` is 1234 or 1.234)."""
    from src.grounding_ledger import _candidates
    clean = token.replace("\u2212", "-").strip()
    negative = clean.startswith("-")
    body = clean.lstrip("-").strip()
    out: List[Decimal] = []
    for value, _decimals in _candidates(body):
        out.append(-value if negative else value)
        if negative:
            out.append(value)          # "- 12,00" in a column is often a layout dash
    return out


def _number_values(text: str) -> List[Decimal]:
    values: List[Decimal] = []
    for match in _NUM_TOKEN.finditer(text):
        token = match.group(0)
        values.extend(_readings(token))
        parts = re.split(r"[\s\u00a0\u202f]+", token)
        if len(parts) > 1 and _SPACE_GROUPED.match(" ".join(parts)):
            values.extend(_readings("".join(parts)))     # "1 234,56": a space as the thousands mark
        if len(parts) > 1:
            for part in parts:
                if part:
                    values.extend(_readings(part))
    return values


def _as_decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        except InvalidOperation:
            return None
    return None


def _number_in(value: Decimal, text: str) -> Optional[str]:
    """"number" when `text` writes `value`, "percent" when it writes it as a percentage."""
    tolerance = Decimal("0.000001") * max(Decimal(1), abs(value))
    readings = _number_values(text)
    for reading in readings:
        if abs(reading - value) <= tolerance:
            return "number"
    for reading in readings:
        if abs(reading / 100 - value) <= tolerance and "%" in text:
            return "percent"
    return None


_MONTHS = {
    1: ("enero", "january", "jan", "ene"), 2: ("febrero", "february", "feb"), 3: ("marzo", "march", "mar"),
    4: ("abril", "april", "apr", "abr"), 5: ("mayo", "may"), 6: ("junio", "june", "jun"),
    7: ("julio", "july", "jul"), 8: ("agosto", "august", "aug", "ago"),
    9: ("septiembre", "setiembre", "september", "sep", "sept"), 10: ("octubre", "october", "oct"),
    11: ("noviembre", "november", "nov"), 12: ("diciembre", "december", "dec", "dic"),
}
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")


def _date_in(value: str, text: str) -> bool:
    """An ISO date (``2026-03-15``) against the ways a document writes it."""
    match = _ISO_DATE.match(value.strip())
    if not match:
        return False
    year, month, day = (int(g) for g in match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return False
    d, m, y2 = rf"0?{day}", rf"0?{month}", f"{year % 100:02d}"
    sep = r"\s*[/.\-]\s*"
    patterns = [rf"(?<!\d){d}{sep}{m}{sep}(?:{year}|{y2})(?!\d)",
                rf"(?<!\d){year}{sep}{m}{sep}{d}(?!\d)",
                rf"(?<!\d){m}{sep}{d}{sep}{year}(?!\d)"]
    names = "|".join(re.escape(n) for n in _MONTHS[month])
    patterns.append(rf"(?<!\d){d}(?:\u00ba|o)?\s*(?:de\s+)?(?:{names})\.?\s*(?:de\s+|del\s+|,\s*)?{year}(?!\d)")
    patterns.append(rf"(?:{names})\.?\s+{d}(?:st|nd|rd|th)?,?\s*{year}(?!\d)")
    return any(re.search(p, text) for p in patterns)


def _enum_values(node: Any) -> List[Any]:
    if not isinstance(node, Mapping):
        return []
    out = list(node.get("enum") or []) if isinstance(node.get("enum"), (list, tuple)) else []
    if "const" in node:
        out.append(node["const"])
    for branch in _non_null_branches(node):
        out.extend(_enum_values(branch))
    return out


def _value_in_quote(value: Any, quote_n: str, node: Any) -> Optional[str]:
    """How `value` is supported by the (normalised) quote, or None:
    ``verbatim``, ``number``, ``percent``, ``date``, ``fuzzy`` or ``inferred``
    (a boolean, or an enum value the document words differently)."""
    if isinstance(value, bool):
        return "inferred"
    number = _as_decimal(value)
    if number is not None:
        return _number_in(number, quote_n)
    if not isinstance(value, str):
        return None
    value_n = norm_text(value)
    if not value_n:
        return None
    if value_n in quote_n:
        return "verbatim"
    if _date_in(value_n, quote_n):
        return "date"
    if re.fullmatch(r"[-\u2212]?[\d.,'\s]+%?", value_n) and any(ch.isdigit() for ch in value_n):
        readings = _readings(value_n.rstrip("%"))
        for reading in readings:
            if _number_in(reading, quote_n):
                return "number"
    if len(value_n) >= 5 and _fuzzy_find(value_n, quote_n):
        return "fuzzy"
    if value in _enum_values(node):
        return "inferred"
    return None


class _Source:
    """The document a chunk was extracted from: its text, its units and the
    normalised forms used to look quotes up."""

    def __init__(self, text: str, units: Optional[Sequence[Mapping[str, Any]]] = None):
        self.text = str(text or "")
        self.norm = norm_text(self.text)
        self.units = [(u.get("number"), norm_text(u.get("text") or "")) for u in (units or [])
                      if isinstance(u, Mapping)]

    def locate(self, quote: str) -> Optional[Tuple[Any, str]]:
        """`(unit number or None, "exact"|"fuzzy")` when the quote is in the document."""
        quote_n = norm_text(quote)
        if len(quote_n) < 1:
            return None
        for number, text in self.units:
            if quote_n in text:
                return number, "exact"
        if quote_n in self.norm:
            return None, "exact"
        if len(quote_n) < 6:
            return None
        for number, text in self.units:
            if _fuzzy_find(quote_n, text):
                return number, "fuzzy"
        if not self.units and _fuzzy_find(quote_n, self.norm):
            return None, "fuzzy"
        return None

    def find_value(self, value: Any) -> Optional[Dict[str, Any]]:
        """A quote for a value the model returned without one, taken from the
        document itself: the line that contains it, when it does. Short
        strings and small whole numbers are never looked up this way: they
        occur everywhere and would ground anything."""
        if isinstance(value, bool) or value is None:
            return None
        lines = [line for line in self.text.splitlines() if line.strip()]
        if isinstance(value, str):
            value_n = norm_text(value)
            if len(value_n) < 3:
                return None
            for line in lines:
                if value_n in norm_text(line):
                    return {"quote": line.strip()[:MAX_QUOTE_CHARS]}
            return None
        number = _as_decimal(value)
        if number is None or (number == number.to_integral_value() and abs(number) < 10):
            return None
        for line in lines:
            if _number_in(number, norm_text(line)):
                return {"quote": line.strip()[:MAX_QUOTE_CHARS]}
        return None


def _evidence_index(evidence: Any) -> Dict[str, List[Dict[str, Any]]]:
    index: Dict[str, List[Dict[str, Any]]] = {}
    for item in evidence if isinstance(evidence, list) else []:
        if not isinstance(item, Mapping):
            continue
        quote = item.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            continue
        path = normalise_path(item.get("path"))
        index.setdefault(path, []).append({"quote": quote.strip()[:MAX_QUOTE_CHARS], "unit": item.get("unit")})
    return index


def _pick_branch(value: Any, node: Any) -> Any:
    if not isinstance(node, Mapping):
        return node
    branches = _non_null_branches(node)
    if not branches:
        return node
    from src.workflows import schema_check
    for branch in branches:
        if isinstance(branch, Mapping) and not schema_check.validate(value, branch):
            return branch
    if isinstance(value, dict):
        return next((b for b in branches if _is_object(b)), branches[0])
    if isinstance(value, list):
        return next((b for b in branches if _is_array(b)), branches[0])
    return node


class _Grounding:
    def __init__(self, source: _Source, evidence: Any):
        self.source = source
        self.index = _evidence_index(evidence)
        self.evidence: List[Dict[str, Any]] = []
        self.dropped: List[Dict[str, Any]] = []
        self.inferred: List[str] = []

    def drop(self, path: str, value: Any, why: str) -> None:
        self.dropped.append({"path": path, "value": value, "why": why})

    def leaf(self, value: Any, node: Any, path: str) -> Any:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            self.drop(path, value, "a structured value where the schema expects a single value")
            return None
        candidates: List[Dict[str, Any]] = []
        for ancestor in _ancestors(path):
            candidates.extend(self.index.get(ancestor, []))
        synthesised = False
        if not candidates:
            found = self.source.find_value(value)
            if found is None:
                self.drop(path, value, "no quote from the document supports it")
                return None
            candidates, synthesised = [found], True
        why = "its quote is not in the document"
        for item in candidates:
            located = self.source.locate(item["quote"])
            if located is None:
                continue
            unit, match = located
            how = _value_in_quote(value, norm_text(item["quote"]), node)
            if how is None:
                why = "the value is not in the quote given for it"
                continue
            record = {"path": path, "quote": item["quote"],
                      "unit": unit if unit is not None else item.get("unit"),
                      "match": match, "value_match": how}
            if synthesised:
                record["synthesised"] = True
            self.evidence.append(record)
            if how == "inferred":
                self.inferred.append(path)
            return value
        self.drop(path, value, why)
        return None

    def walk(self, value: Any, node: Any, path: str) -> Any:
        node = _pick_branch(value, node)
        if isinstance(value, dict):
            props = node.get("properties") if isinstance(node, Mapping) and isinstance(node.get("properties"), Mapping) else {}
            extra = node.get("additionalProperties") if isinstance(node, Mapping) else None
            out: Dict[str, Any] = {}
            for key, sub in value.items():
                child = _join(path, key)
                if key in props:
                    out[key] = self.walk(sub, props[key], child)
                elif isinstance(extra, Mapping) and extra:
                    out[key] = self.walk(sub, extra, child)
                elif props or extra is False:
                    if sub is not None:
                        self.drop(child, sub, "not a field of the schema")
                else:
                    out[key] = self.walk(sub, None, child)
            return out
        if isinstance(value, list):
            items = node.get("items") if isinstance(node, Mapping) else None
            if isinstance(node, Mapping) and _types(node) and not _is_array(node):
                self.drop(path, value, "a list where the schema expects a single value")
                return None
            return [self.walk(item, items, _join(path, i)) for i, item in enumerate(value)]
        return self.leaf(value, node, path)


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return all(_is_empty(v) for v in value.values())
    return False


def _compact(value: Any, old: str, new: str, mapping: Dict[str, str]) -> Any:
    """Drop array items that grounding emptied, recording how item paths moved."""
    if isinstance(value, dict):
        return {k: _compact(v, _join(old, k), _join(new, k), mapping) for k, v in value.items()}
    if isinstance(value, list):
        out = []
        for i, item in enumerate(value):
            if _is_empty(item):
                continue
            old_path, new_path = _join(old, i), _join(new, len(out))
            if old_path != new_path:
                mapping[old_path] = new_path
            out.append(_compact(item, old_path, new_path, mapping))
        return out
    return value


def _remap(path: str, mapping: Mapping[str, str]) -> str:
    best = ""
    for prefix in mapping:
        if (path == prefix or path.startswith(prefix + ".") or path.startswith(prefix + "[")) \
                and len(prefix) > len(best):
            best = prefix
    return mapping[best] + path[len(best):] if best else path


def ground(data: Any, evidence: Any, source_text: str, units: Optional[Sequence[Mapping[str, Any]]] = None,
           *, schema: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Keep only the values the document supports.

    Every non-null leaf needs a quote (given for its path or for an enclosing
    object or list item) that occurs in `source_text`, and the value has to be
    in that quote. A value with no quote is looked up in the document itself
    (``synthesised`` evidence) unless it is a short string or a small whole
    number. Anything unsupported becomes null and is listed in ``dropped`` with
    the reason; nothing invented survives. With `schema` (the user's inlined
    schema) the result is also checked by :func:`finalize`."""
    source = _Source(source_text, units)
    state = _Grounding(source, evidence)
    node = inline_refs(schema) if isinstance(schema, Mapping) else None
    kept = state.walk(data if isinstance(data, dict) else {}, node, "")
    if not isinstance(data, dict) and data is not None:
        state.drop("", data, "the answer is not an object")
    mapping: Dict[str, str] = {}
    kept = _compact(kept, "", "", mapping)
    evidence_out = [{**e, "path": _remap(e["path"], mapping)} for e in state.evidence]
    result = {"data": kept, "evidence": evidence_out, "dropped": state.dropped,
              "inferred": [_remap(p, mapping) for p in state.inferred]}
    if node is not None:
        result.update(finalize(kept, node))
    return result


def _prune(value: Any, node: Any, path: str, missing: List[str]) -> Any:
    node = _pick_branch(value, node)
    if isinstance(value, dict):
        props = node.get("properties") if isinstance(node, Mapping) and isinstance(node.get("properties"), Mapping) else {}
        required = set(node.get("required") or []) if isinstance(node, Mapping) else set()
        out: Dict[str, Any] = {}
        for key, sub in value.items():
            child = _join(path, key)
            if sub is None:
                allows_null = "null" in _types(props.get(key)) if key in props else False
                if key in required:
                    out[key] = None
                    if not allows_null:
                        missing.append(child)
                elif allows_null:
                    out[key] = None
                continue
            out[key] = _prune(sub, props.get(key), child, missing)
        for key in required:
            if key not in out and key not in value:
                out[key] = None
                missing.append(_join(path, key))
        return out
    if isinstance(value, list):
        items = node.get("items") if isinstance(node, Mapping) else None
        return [_prune(item, items, _join(path, i), missing) for i, item in enumerate(value)]
    return value


def finalize(data: Any, schema: Mapping[str, Any]) -> Dict[str, Any]:
    """The grounded data checked against the user's ORIGINAL schema.

    Optional fields that ended up null are left out (unless the schema allows
    null); required ones stay as null and are listed in ``missing_required``
    with ``schema_valid: false``. Nothing is filled in to make it pass."""
    found: set = set()
    checkable = _strip_unchecked(inline_refs(schema), found)
    missing: List[str] = []
    pruned = _prune(data if isinstance(data, dict) else {}, checkable, "", missing)
    from src.workflows import schema_check
    errors = schema_check.validate(pruned, checkable)
    return {"data": pruned, "missing_required": missing, "errors": errors[:40],
            "schema_valid": not errors, "unchecked_keywords": sorted(found)}


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def merge_chunks(results: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """One result from the per-chunk results of a long document, in order.

    The first supported value of a field wins; a different value found later
    is kept out of the data and recorded in ``conflicts`` with its quote.
    Arrays are concatenated with duplicates removed, and every evidence path
    is moved to where its item ended up."""
    merged: Any = None
    evidence: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    inferred: List[str] = []
    conflicts: List[Dict[str, Any]] = []
    seen_evidence: set = set()
    for index, result in enumerate(results):
        mapping: Dict[str, str] = {}
        lost: Dict[str, Dict[str, Any]] = {}
        merged = _merge(merged, result.get("data"), "", mapping, lost, index)
        for item in result.get("evidence") or []:
            path = _remap(str(item.get("path") or ""), mapping)
            loser = next((lost[p] for p in _ancestors(path) if p in lost), None)
            if loser is not None:
                loser.setdefault("other_quotes", []).append(item.get("quote"))
                continue
            key = (path, norm_text(item.get("quote")))
            if key in seen_evidence:
                continue
            seen_evidence.add(key)
            evidence.append({**item, "path": path, "chunk": index})
        for item in result.get("dropped") or []:
            dropped.append({**item, "chunk": index})
        inferred.extend(_remap(p, mapping) for p in result.get("inferred") or [])
        conflicts.extend(lost.values())
    return {"data": merged if merged is not None else {}, "evidence": evidence, "dropped": dropped,
            "inferred": sorted(set(inferred)), "conflicts": conflicts}


def _merge(kept: Any, new: Any, path: str, mapping: Dict[str, str], lost: Dict[str, Dict[str, Any]],
           index: int) -> Any:
    if kept is None:
        return copy.deepcopy(new)
    if new is None:
        return kept
    if isinstance(kept, dict) and isinstance(new, dict):
        out = dict(kept)
        for key, value in new.items():
            out[key] = _merge(kept.get(key), value, _join(path, key), mapping, lost, index)
        return out
    if isinstance(kept, list) and isinstance(new, list):
        out = list(kept)
        keys = [_canon(item) for item in out]
        for j, item in enumerate(new):
            if item is None:
                continue
            canon = _canon(item)
            if canon in keys:
                mapping[_join(path, j)] = _join(path, keys.index(canon))
                continue
            mapping[_join(path, j)] = _join(path, len(out))
            out.append(copy.deepcopy(item))
            keys.append(canon)
        return out
    if _canon(kept) == _canon(new):
        return kept
    lost[path] = {"path": path, "kept": kept, "other": new, "chunk": index}
    return kept


# ── routing ───────────────────────────────────────────────────────────────

def _default_resolver(purpose: str, owner: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[Dict]]:
    from src.endpoint_resolver import resolve_endpoint
    return resolve_endpoint(purpose, owner=owner or None)


def _json_mode_ok(model: str) -> Optional[bool]:
    """What this install's calibration proved about the model's JSON mode:
    True, False (probed and failed) or None (never probed)."""
    try:
        from src import model_calibration
        from src.model_router import CAP_JSON_MODE, _capability_status
        manifest = model_calibration.get_effective_manifest(vendor="ollama", model_id=model)
        return _capability_status(manifest, CAP_JSON_MODE)[0]
    except Exception:  # noqa: BLE001 - a missing calibration is "unknown", never a refusal
        logger.debug("schema_extraction: calibration unavailable for %s", model, exc_info=True)
        return None


def _is_paid(url: Optional[str]) -> bool:
    try:
        from src.endpoint_resolver import endpoint_cost_tracked
        return bool(url) and endpoint_cost_tracked(str(url))
    except Exception:  # noqa: BLE001
        return False


def route_for_schema(profile: Mapping[str, Any], *, owner: Optional[str] = None, tier: Optional[str] = None,
                     resolver: Optional[Callable] = None, json_ok: Optional[Callable[[str], Optional[bool]]] = None,
                     start_after: Optional[str] = None) -> Dict[str, Any]:
    """The model purpose for a schema profile (or a forced `tier`).

    simple -> utility, medium -> extraction (falls back to utility, then
    default, when unset), complex -> default. Walking up from there, a purpose
    that resolves to no model, or to a model this install's calibration proved
    unable to answer in JSON mode, is skipped with the reason. The walk never
    moves from a local model to a paid endpoint: that only happens when the
    owner configured a paid model for the purpose itself."""
    chosen_tier = tier if tier in TIERS else str(profile.get("tier") or "medium")
    if chosen_tier not in TIERS:
        chosen_tier = "medium"
    requested = PURPOSE_FOR_TIER[chosen_tier]
    resolver = resolver or _default_resolver
    json_ok = json_ok or _json_mode_ok
    ladder = list(PURPOSE_LADDER[PURPOSE_LADDER.index(requested):])
    if start_after in PURPOSE_LADDER:
        ladder = [p for p in PURPOSE_LADDER if PURPOSE_LADDER.index(p) > PURPOSE_LADDER.index(start_after)]
    excluded: List[Dict[str, Any]] = []
    chosen: Optional[Dict[str, Any]] = None
    first_local: Optional[bool] = None
    for purpose in ladder:
        try:
            url, model, _headers = resolver(purpose, owner)
        except Exception as exc:  # noqa: BLE001
            excluded.append({"purpose": purpose, "why": f"could not resolve a model ({type(exc).__name__}: {exc})"})
            continue
        if not url or not model:
            excluded.append({"purpose": purpose, "why": "no model is configured"})
            continue
        paid = _is_paid(url)
        if first_local is None:
            first_local = not paid
        elif first_local and paid:
            excluded.append({"purpose": purpose, "model": model,
                             "why": "a paid endpoint; never used without the owner choosing it"})
            continue
        if json_ok(model) is False:
            excluded.append({"purpose": purpose, "model": model,
                             "why": "this install's calibration says its JSON mode fails"})
            continue
        chosen = {"purpose": purpose, "model": model, "url": url, "paid": paid}
        break
    reasons = list(profile.get("reasons") or [])
    if tier in TIERS and tier != profile.get("tier"):
        reasons.append(f"tier {tier} asked for by the caller")
    out = {"tier": chosen_tier, "reasons": reasons, "requested_purpose": requested,
           "purpose": chosen["purpose"] if chosen else requested, "model": chosen["model"] if chosen else None,
           "paid": bool(chosen and chosen["paid"]), "excluded": excluded}
    out["_url"] = chosen["url"] if chosen else None
    return out


def public_route(route: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in route.items() if not k.startswith("_")}


# ── chunks ────────────────────────────────────────────────────────────────

def _split_long(text: str, limit: int) -> List[str]:
    """`text` in pieces of at most `limit` characters, cut at paragraph, then
    line, then sentence boundaries when one is near."""
    pieces: List[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("\n\n"), -1)
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = max(window.rfind(". "), window.rfind("; "))
            cut = cut + 1 if cut >= limit // 2 else -1
        if cut < limit // 2:
            cut = limit
        pieces.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest.strip():
        pieces.append(rest)
    return pieces


def units_from_text(text: str, *, limit: int = CHUNK_CHARS) -> List[Dict[str, Any]]:
    """Plain text as numbered parts no longer than `limit`."""
    return [{"kind": "part", "number": i + 1, "title": "", "text": piece}
            for i, piece in enumerate(_split_long(str(text or ""), limit))]


def chunk_units(units: Sequence[Mapping[str, Any]], *, limit: int = CHUNK_CHARS) -> List[List[Dict[str, Any]]]:
    """Consecutive units grouped into chunks of at most `limit` characters; a
    unit longer than that is split and keeps its number."""
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    size = 0
    for index, unit in enumerate(units):
        text = str(unit.get("text") or "")
        if not text.strip():
            continue
        number = unit.get("number") or index + 1
        for piece in _split_long(text, limit):
            if current and size + len(piece) > limit:
                chunks.append(current)
                current, size = [], 0
            current.append({"kind": unit.get("kind") or "part", "number": number, "text": piece})
            size += len(piece)
    if current:
        chunks.append(current)
    return chunks


def _chunk_text(chunk: Sequence[Mapping[str, Any]]) -> str:
    return "\n\n".join(str(u["text"]) for u in chunk)


def _chunk_prompt_text(chunk: Sequence[Mapping[str, Any]]) -> str:
    parts = []
    for unit in chunk:
        label = "page" if unit.get("kind") == "page" else "part"
        parts.append(f"[[{label} {unit.get('number')}]]\n{unit['text']}")
    return "\n\n".join(parts)


# ── the extraction ────────────────────────────────────────────────────────

_SYSTEM = (
    "You extract data from a document into JSON. Reply with ONE JSON object of the form "
    '{"data": {...}, "evidence": [...]} and nothing else: no prose, no code fence.\n'
    "Rules:\n"
    "- `data` follows the schema you are given. Copy values as the document writes them; only write numbers "
    "as plain JSON numbers and dates as YYYY-MM-DD when the field is a date.\n"
    "- Never invent, guess or calculate a value the document does not state. When the document does not give "
    "a field, write null.\n"
    "- For every value that is not null, add one evidence item {\"path\": \"<field path, e.g. total or "
    "lines[0].amount>\", \"quote\": \"<the exact words of the document that contain the value, copied "
    "character by character, at most 200 characters>\", \"unit\": <the page or part number shown as "
    "[[page N]] or [[part N]], or null>}.")


def _shown_schema(inlined: Mapping[str, Any]) -> Dict[str, Any]:
    def clean(node: Any) -> Any:
        if isinstance(node, list):
            return [clean(x) for x in node]
        if not isinstance(node, Mapping):
            return node
        return {k: clean(v) for k, v in node.items() if k not in ("$schema", "$id", "$comment", "examples")}
    return clean(inlined)


def _messages(shown: Mapping[str, Any], instructions: str, chunk: Sequence[Mapping[str, Any]], index: int,
              total: int) -> List[Dict[str, str]]:
    part = (f" (part {index + 1} of {total}; a field this part does not mention is null)" if total > 1 else "")
    user = ("Schema of `data`:\n" + json.dumps(shown, ensure_ascii=False) +
            (f"\n\nInstructions from the user:\n{instructions.strip()}" if instructions and instructions.strip() else "") +
            f"\n\nDocument{part}:\n\"\"\"\n" + _chunk_prompt_text(chunk) + "\n\"\"\"")
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


def _max_tokens(profile: Mapping[str, Any]) -> int:
    leaves = int(profile.get("leaves") or 1)
    arrays = int(profile.get("object_arrays") or 0)
    return int(min(8192, 1200 + 120 * leaves + 1500 * arrays))


def _accepts_null(node: Any) -> bool:
    if not isinstance(node, Mapping):
        return False
    if "null" in _types(node) or None in (node.get("enum") or []):
        return True
    return any(isinstance(b, Mapping) and _types(b) == {"null"}
               for key in ("anyOf", "oneOf") for b in (node.get(key) or []))


def _fill_missing(value: Any, node: Any) -> Any:
    """A key left out where the envelope accepts null is the same answer as
    null: fill it in, so an omission does not cost a repair call."""
    if isinstance(value, dict) and isinstance(node, Mapping):
        props = node.get("properties") if isinstance(node.get("properties"), Mapping) else {}
        if not props:
            for branch in node.get("anyOf") or node.get("oneOf") or []:
                if isinstance(branch, Mapping) and isinstance(branch.get("properties"), Mapping):
                    props = branch["properties"]
                    break
        for key, sub in props.items():
            if key not in value and _accepts_null(sub):
                value[key] = None
            elif key in value:
                value[key] = _fill_missing(value[key], sub)
        return value
    if isinstance(value, list) and isinstance(node, Mapping):
        items = node.get("items")
        return [_fill_missing(item, items) for item in value]
    return value


def _normalise_reply(reply: Any, env: Mapping[str, Any]) -> str:
    """The reply as the envelope expects it, when it plainly means that: a bare
    data object is wrapped as ``{"data": ..., "evidence": []}`` (its values are
    then grounded against the document itself) and omitted nullable keys are
    filled with null. Anything else is passed through for the checker."""
    from src.workflows import schema_check
    value, why = schema_check.extract_json(reply)
    if why or not isinstance(value, dict):
        return str(reply or "")
    data_props = env["properties"]["data"].get("properties") or {}
    if "data" not in value and "evidence" not in value and value and set(value) <= set(data_props):
        value = {"data": value, "evidence": []}
    if "evidence" not in value:
        value["evidence"] = []
    return json.dumps(_fill_missing(value, env), ensure_ascii=False)


def _call_chunk(models: Any, messages: List[Dict[str, str]], env: Mapping[str, Any], *, owner: str, purpose: str,
                timeout_s: float, max_tokens: int) -> Dict[str, Any]:
    """One model call for one chunk, then at most one repair (the workflow
    `structured` helper). Synchronous: run in a worker thread."""
    from src.workflows.model_calls import ModelUnavailable
    from src.workflows.model_nodes import structured
    try:
        reply = models.complete(messages, owner=owner, purpose=purpose, timeout_s=timeout_s,
                                max_tokens=max_tokens, temperature=0.0, response_schema=dict(env))
    except ModelUnavailable as exc:
        return {"ok": False, "errors": [f"the model could not be reached: {exc}"], "unavailable": True}
    # The repair answer gets the same benefit of the doubt as the first one.
    repairer = _NormalisingModels(models, env)
    verdict = structured(_normalise_reply(reply, env), env, models=repairer, owner=owner, purpose=purpose,
                         task=messages[-1]["content"], timeout_s=min(float(timeout_s), 300.0),
                         response_schema=dict(env), max_tokens=max_tokens)
    verdict["raw"] = str(reply)[:1200]
    return verdict


class _NormalisingModels:
    """`ModelCalls`-shaped wrapper whose completions go through `_normalise_reply`."""

    def __init__(self, models: Any, env: Mapping[str, Any]):
        self._models = models
        self._env = env

    def complete(self, messages, **opts):
        return _normalise_reply(self._models.complete(messages, **opts), self._env)

    def decide(self, *args, **kwargs):
        return self._models.decide(*args, **kwargs)


def _escalation_target(current: Mapping[str, Any], *, owner: Optional[str], resolver: Callable,
                       json_ok: Callable) -> Tuple[Optional[Dict[str, Any]], str]:
    """The next purpose up the ladder that resolves to a different, local (or
    already-paid) model whose JSON mode is not known to fail."""
    purpose = str(current.get("purpose") or "")
    if purpose not in PURPOSE_LADDER:
        return None, "unknown purpose"
    why = "there is no other model above this tier"
    for candidate in PURPOSE_LADDER[PURPOSE_LADDER.index(purpose) + 1:]:
        try:
            url, model, _headers = resolver(candidate, owner)
        except Exception as exc:  # noqa: BLE001
            why = f"{candidate} could not be resolved ({exc})"
            continue
        if not url or not model:
            continue
        if (url, model) == (current.get("_url"), current.get("model")):
            continue
        paid = _is_paid(url)
        if paid and not current.get("paid"):
            why = f"{candidate} is a paid endpoint; never used without the owner choosing it"
            continue
        if json_ok(model) is False:
            why = f"{candidate} ({model}): this install's calibration says its JSON mode fails"
            continue
        return {"purpose": candidate, "model": model, "_url": url, "paid": paid}, ""
    return None, why


#: Reasons the family's document service could not take the job that say
#: nothing about the document itself: the file is then read here, without OCR.
_SERVICE_UNUSABLE = frozenset({"hub_down", "app_down", "app_missing", "tool_missing", "auth"})


def _default_reader(path: str, ocr: str) -> Dict[str, Any]:
    """The family's document service (readers + OCR through the hub). When the
    service cannot be used at all (no hub, no token, owner app missing) the
    same readers run in this process, without OCR, and the result says so; a
    document the service read and refused (damaged, unsupported) is not
    retried here."""
    from src import family_services
    got = family_services.extract(path, ocr=ocr, timeout_s=600.0)
    if not isinstance(got, Mapping) or got.get("ok") or got.get("kind") not in _SERVICE_UNUSABLE:
        return got
    try:
        from src.hoard_link.docs import readers_lite
        size = os.path.getsize(path)
        if size > 200_000_000:
            return got
        with open(path, "rb") as handle:
            local = dict(readers_lite.read_any(os.path.basename(path), handle.read()))
    except Exception as exc:  # noqa: BLE001
        logger.debug("schema_extraction: local reader failed for %s", path, exc_info=True)
        return {**got, "error": f"{got.get('error')}; local reader failed: {type(exc).__name__}: {exc}"}
    notes = [str(n) for n in (local.get("notes") or [])]
    notes.append(f"the family document service was not usable ({got.get('error')}); read here without OCR")
    return {**local, "ok": not local.get("error"), "via": "local", "notes": notes}


def _read_source(path: Optional[str], text: Optional[str], ocr: str, max_chars: int,
                 reader: Callable) -> Tuple[List[Dict[str, Any]], Dict[str, Any], bool]:
    if text is not None:
        units = units_from_text(text)
        source: Dict[str, Any] = {"kind": "text", "via": "inline", "pages": None, "needs_ocr": False}
    else:
        got = reader(path, ocr)
        if not isinstance(got, Mapping) or not got.get("ok"):
            error = (got or {}).get("error") if isinstance(got, Mapping) else None
            raise SchemaExtractionError(f"the document could not be read: {error or 'no answer'}", "unreadable")
        raw_units = [u for u in (got.get("units") or []) if isinstance(u, Mapping)]
        units = [{"kind": str(u.get("kind") or "part"), "number": u.get("number") or i + 1,
                  "text": str(u.get("text") or "")} for i, u in enumerate(raw_units)]
        if not any(u["text"].strip() for u in units):
            units = units_from_text(str(got.get("text") or ""))
        pages = sum(1 for u in units if u["kind"] == "page") or None
        source = {"kind": str(got.get("kind") or ""), "via": str(got.get("via") or ""), "pages": pages,
                  "needs_ocr": bool(got.get("needs_ocr")), "pages_ocr": int(got.get("pages_ocr") or 0),
                  "notes": [str(n) for n in (got.get("notes") or [])][:6]}
    kept: List[Dict[str, Any]] = []
    budget = max(1, int(max_chars))
    truncated = False
    for unit in units:
        if budget <= 0:
            truncated = True
            break
        body = unit["text"]
        if len(body) > budget:
            body, truncated = body[:budget], True
        kept.append({**unit, "text": body})
        budget -= len(body)
    if not any(u["text"].strip() for u in kept):
        hint = " (a scan, and no OCR could read it)" if source.get("needs_ocr") else ""
        raise SchemaExtractionError(f"the document has no readable text{hint}", "no_text")
    source["chars"] = sum(len(u["text"]) for u in kept)
    return kept, source, truncated


async def extract_to_schema(owner: Optional[str], *, path: Optional[str] = None, text: Optional[str] = None,
                            schema: Any = None, schema_name: Optional[str] = None, instructions: str = "",
                            ocr: str = "auto", tier: str = "auto", max_chars: int = DEFAULT_MAX_CHARS,
                            timeout_s: float = DEFAULT_TIMEOUT_S, models: Any = None,
                            resolver: Optional[Callable] = None, json_ok: Optional[Callable] = None,
                            reader: Optional[Callable] = None) -> Dict[str, Any]:
    """Extract `path` (a document) or `text` into `schema` (or the owner's
    saved `schema_name`). See the module docstring for every rule.

    `models`, `resolver`, `json_ok` and `reader` are seams for tests and
    previews; production uses the workflow model calls, the endpoint resolver,
    the calibration manifests and the family's document service."""
    started = time.monotonic()
    if (path is None) == (text is None):
        raise SchemaExtractionError("give exactly one of `path` (a document) or `text`", "invalid_arguments")
    if text is not None and not str(text).strip():
        raise SchemaExtractionError("`text` is empty", "invalid_arguments")
    if schema is None and schema_name:
        saved = get_schema(owner, schema_name)
        if saved is None:
            raise SchemaExtractionError(f"there is no saved schema called {schema_name!r}", "unknown_schema")
        schema = saved["schema"]
    if schema is None:
        raise SchemaExtractionError("give a `schema` or the name of a saved one (`schema_name`)", "invalid_arguments")
    mode = str(ocr or "auto").strip().lower()
    if mode not in ("auto", "off", "force"):
        raise SchemaExtractionError("ocr must be auto, off or force", "invalid_arguments")
    wanted_tier = str(tier or "auto").strip().lower()
    if wanted_tier not in TIERS + ("auto",):
        raise SchemaExtractionError("tier must be auto, simple, medium or complex", "invalid_arguments")
    try:
        max_chars = max(1000, min(int(max_chars or DEFAULT_MAX_CHARS), MAX_INPUT_CHARS))
    except (TypeError, ValueError):
        raise SchemaExtractionError("max_chars must be a whole number", "invalid_arguments")

    inlined, _checkable, unchecked = prepare_schema(schema)
    reader = reader or _default_reader
    units, source, truncated = await asyncio.to_thread(_read_source, path, text, mode, max_chars, reader)
    profile = schema_profile(inlined, input_chars=source["chars"])
    resolver = resolver or _default_resolver
    json_ok = json_ok or _json_mode_ok
    route = await asyncio.to_thread(route_for_schema, profile, owner=owner,
                                    tier=wanted_tier if wanted_tier in TIERS else None,
                                    resolver=resolver, json_ok=json_ok)
    if models is None:
        from src.workflows.model_calls import production
        models = production()
    env = envelope(inlined)
    shown = _shown_schema(inlined)
    chunks = chunk_units(units)
    current = dict(route)
    escalated, escalation = False, None
    repaired = False
    errors: List[str] = []
    failed: List[int] = []
    results: List[Dict[str, Any]] = []
    max_tokens = _max_tokens(profile)
    for index, chunk in enumerate(chunks):
        messages = _messages(shown, instructions, chunk, index, len(chunks))
        verdict = await asyncio.to_thread(_call_chunk, models, messages, env, owner=owner or "",
                                          purpose=current["purpose"], timeout_s=timeout_s, max_tokens=max_tokens)
        if not verdict.get("ok") and not escalated:
            target, why_not = await asyncio.to_thread(_escalation_target, current, owner=owner,
                                                      resolver=resolver, json_ok=json_ok)
            if target is None:
                escalation = {"attempted": False, "why_not": why_not, "chunk": index}
            else:
                escalated = True
                escalation = {"from": {"purpose": current["purpose"], "model": current.get("model")},
                              "to": {"purpose": target["purpose"], "model": target["model"]},
                              "why": "; ".join(str(e) for e in (verdict.get("errors") or [])[:3]), "chunk": index}
                current = {**current, **target}
                verdict = await asyncio.to_thread(_call_chunk, models, messages, env, owner=owner or "",
                                                  purpose=current["purpose"], timeout_s=timeout_s,
                                                  max_tokens=max_tokens)
        if not verdict.get("ok"):
            failed.append(index)
            label = f"part {index + 1}: " if len(chunks) > 1 else ""
            errors.append(label + "the model's answer does not fit the schema after one repair: "
                          + "; ".join(str(e) for e in (verdict.get("errors") or [])[:6])
                          + (f" ({verdict['repair']})" if verdict.get("repair") else ""))
            continue
        repaired = repaired or bool(verdict.get("repaired"))
        answer = verdict.get("data") if isinstance(verdict.get("data"), Mapping) else {}
        results.append(ground(answer.get("data"), answer.get("evidence"), _chunk_text(chunk), chunk,
                              schema=inlined))

    merged = merge_chunks(results)
    final = finalize(merged["data"], inlined)
    route_out = public_route(route)
    route_out.update({"purpose": current["purpose"], "model": current.get("model"),
                      "profile": {k: profile[k] for k in ("leaves", "max_depth", "object_arrays", "enums", "unions",
                                                          "refs", "recursive", "constraints", "required",
                                                          "input_chars", "base_tier")}})
    return {
        "data": final["data"],
        "evidence": merged["evidence"],
        "dropped": merged["dropped"],
        "inferred": merged["inferred"],
        "missing_required": final["missing_required"],
        "conflicts": merged["conflicts"],
        "schema_valid": final["schema_valid"],
        "complete": not failed,
        "errors": errors + final["errors"],
        "unchecked_keywords": sorted(set(unchecked) | set(final["unchecked_keywords"])),
        "route": route_out,
        "repaired": repaired,
        "escalated": escalated,
        "escalation": escalation,
        "input_truncated": truncated,
        "source": source,
        "chunks": {"total": len(chunks), "failed": failed},
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def preview_profile(schema: Any, *, text_chars: int = 0, owner: Optional[str] = None, tier: str = "auto",
                    resolver: Optional[Callable] = None, json_ok: Optional[Callable] = None) -> Dict[str, Any]:
    """The tier and route a schema would get, without calling any model."""
    inlined, _checkable, unchecked = prepare_schema(schema)
    profile = schema_profile(inlined, input_chars=text_chars)
    wanted = str(tier or "auto").lower()
    route = route_for_schema(profile, owner=owner, tier=wanted if wanted in TIERS else None,
                             resolver=resolver, json_ok=json_ok)
    return {"profile": profile, "route": public_route(route), "unchecked_keywords": unchecked,
            "envelope": envelope(inlined)}


# ── saved schemas ─────────────────────────────────────────────────────────

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def valid_schema_name(name: Any) -> bool:
    """Letters, digits, `_` and `-`, at most 64, starting with a letter or digit:
    no dots, no separators, so a name can never leave its folder."""
    return isinstance(name, str) and bool(_NAME_RE.match(name))


def _schemas_root() -> str:
    from src import constants
    return os.path.join(constants.DATA_DIR, "extraction_schemas")


def _owner_dir(owner: Optional[str]) -> str:
    safe = re.sub(r"[^A-Za-z0-9_@.-]", "_", str(owner or "")).strip(".")[:96] or "_local"
    return os.path.join(_schemas_root(), safe)


def _schema_file(owner: Optional[str], name: str) -> str:
    if not valid_schema_name(name):
        raise SchemaExtractionError("a schema name is 1-64 letters, digits, '_' or '-', starting with a letter "
                                    "or digit", "invalid_name")
    base = os.path.realpath(_owner_dir(owner))
    target = os.path.realpath(os.path.join(base, name + ".json"))
    if os.path.commonpath([base, target]) != base:
        raise SchemaExtractionError("the schema name leaves its folder", "invalid_name")
    return target


def get_schema(owner: Optional[str], name: str) -> Optional[Dict[str, Any]]:
    target = _schema_file(owner, name)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SchemaExtractionError(f"the saved schema {name!r} cannot be read ({exc})", "unreadable")
    return record if isinstance(record, dict) and isinstance(record.get("schema"), dict) else None


def list_schemas(owner: Optional[str]) -> List[Dict[str, Any]]:
    folder = _owner_dir(owner)
    out: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return out
    for entry in names:
        if not entry.endswith(".json") or not valid_schema_name(entry[:-5]):
            continue
        try:
            record = get_schema(owner, entry[:-5])
        except SchemaExtractionError:
            continue
        if record is None:
            continue
        try:
            tier = schema_profile(record["schema"])["tier"]
        except SchemaExtractionError:
            tier = None
        out.append({"name": record.get("name") or entry[:-5], "description": record.get("description") or "",
                    "updated_at": record.get("updated_at"), "tier": tier})
    return out


def save_schema(owner: Optional[str], name: str, schema: Any, description: str = "") -> Dict[str, Any]:
    target = _schema_file(owner, name)
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except ValueError as exc:
            raise SchemaExtractionError(f"the schema is not valid JSON ({exc})")
    prepare_schema(schema)
    if not isinstance(description, str) or len(description) > 500:
        raise SchemaExtractionError("description must be text of at most 500 characters", "invalid_arguments")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    previous = get_schema(owner, name)
    record = {"name": name, "description": description, "schema": schema,
              "created_at": (previous or {}).get("created_at") or now, "updated_at": now}
    os.makedirs(os.path.dirname(target), exist_ok=True)
    from core.atomic_io import atomic_write_json
    atomic_write_json(target, record, indent=2)
    return record


def delete_schema(owner: Optional[str], name: str) -> bool:
    target = _schema_file(owner, name)
    try:
        os.remove(target)
        return True
    except FileNotFoundError:
        return False
