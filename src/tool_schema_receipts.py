"""Shadow identity of prepared native schemas; never tool authorization."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _schema_without_descriptions(schema: Any) -> Any:
    """Ignore annotations, not properties/default data named 'description'."""
    if not isinstance(schema, dict):
        return schema
    maps = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
    singles = {"items", "additionalItems", "additionalProperties", "contains", "not", "if", "then", "else",
               "propertyNames", "unevaluatedProperties", "unevaluatedItems", "contentSchema"}
    arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}
    out = {}
    for key, value in schema.items():
        if key == "description":
            continue
        if key in maps and isinstance(value, dict):
            out[key] = {name: _schema_without_descriptions(child) for name, child in value.items()}
        elif key in singles:
            out[key] = ([_schema_without_descriptions(child) for child in value] if isinstance(value, list)
                        else _schema_without_descriptions(value))
        elif key in arrays and isinstance(value, list):
            out[key] = [_schema_without_descriptions(child) for child in value]
        else:
            out[key] = value
    return out


def definition_hashes(definition: Mapping[str, Any]) -> tuple[str, str]:
    """Exact function definition and structural signature (descriptions apart)."""
    structural = {key: value for key, value in definition.items() if key != "description"}
    if "parameters" in structural:
        structural["parameters"] = _schema_without_descriptions(structural["parameters"])
    return _digest(definition), _digest(structural)


@dataclass(frozen=True)
class CandidateSchemaReceipt:
    candidate_index: int
    round_num: int
    catalogue_sha256: str
    # Scalar tuples retain no mutable schema, description, argument or owner.
    definitions: tuple[tuple[str, str, str], ...]
    text_only: bool = False


def capture_candidate(schemas: Any, *, candidate_index: int, round_num: int,
                      text_only: bool = False) -> CandidateSchemaReceipt:
    entries = []
    for schema in schemas or ():
        definition = schema.get("function") if isinstance(schema, dict) else None
        if not isinstance(definition, dict) or not isinstance(definition.get("name"), str):
            continue
        exact, semantic = definition_hashes(definition)
        entries.append((definition["name"], exact, semantic))
    return CandidateSchemaReceipt(candidate_index, round_num, _digest(schemas or []), tuple(entries), text_only)


def compare_pdf_binding(receipt: CandidateSchemaReceipt | None, binding: Mapping[str, Any]) -> dict:
    """Compare the answering candidate with a captured PDF call's metadata.

    Missing/unadvertised is observational: fences and other surfaces can still
    be legitimate. Runtime revocation/policy remain authoritative elsewhere.
    """
    out = {"stage": "candidate_prepared", "shadow": True, "status": "not_comparable"}
    if receipt is None:
        return {**out, "reason": "candidate_receipt_missing"}
    out.update(candidate_index=receipt.candidate_index, round_num=receipt.round_num,
               catalogue_sha256=receipt.catalogue_sha256)
    if receipt.text_only or not receipt.definitions:
        return {**out, "reason": "no_native_schema_receipt"}
    matches = [row for row in receipt.definitions if row[0] == binding.get("name")]
    if not matches:
        return {**out, "status": "not_advertised", "reason": "not_in_prepared_native_schemas"}
    if len(matches) != 1:
        return {**out, "reason": "duplicate_schema_name"}
    runtime_exact = binding.get("definition_sha256")
    runtime_semantic = binding.get("schema_semantics_sha256")
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
               for value in (runtime_exact, runtime_semantic)):
        return {**out, "reason": "binding_identity_missing"}
    _, announced_exact, announced_semantic = matches[0]
    out.update(announced_definition_sha256=announced_exact, binding_definition_sha256=runtime_exact,
               announced_semantics_sha256=announced_semantic, binding_semantics_sha256=runtime_semantic)
    status = ("mismatch" if announced_semantic != runtime_semantic else
              "match" if announced_exact == runtime_exact else "compatible_projection")
    return {**out, "status": status}


def observation_for_answer(states: Mapping[int, Mapping[str, Any]], candidate_index: Any,
                           binding: Mapping[str, Any], *, round_num: int | None = None) -> dict:
    """Never substitute candidate zero for a missing answering candidate."""
    state = states.get(candidate_index, {}) if isinstance(candidate_index, int) and not isinstance(candidate_index, bool) else {}
    receipt = state.get("schema_receipt")
    if isinstance(receipt, CandidateSchemaReceipt) and (
        receipt.candidate_index != candidate_index or
        (round_num is not None and receipt.round_num != round_num)
    ):
        return {"stage": "candidate_prepared", "shadow": True,
                "status": "not_comparable", "reason": "stale_candidate_receipt"}
    return compare_pdf_binding(receipt if isinstance(receipt, CandidateSchemaReceipt) else None, binding)
