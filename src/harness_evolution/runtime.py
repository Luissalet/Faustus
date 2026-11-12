"""Typed, owner/project-scoped sidecars over the existing active harness revision.

The base files never change. A turn freezes a snapshot; trial runs supply the
same shape explicitly so evaluation and normal use apply identical edits.
"""
from __future__ import annotations

import copy
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConfigEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["system_section", "model_parameter", "tool_description", "tool_selection", "skill", "skill_text", "context"]
    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    value: Any

    @model_validator(mode="after")
    def bounded_value(self):
        if self.kind == "skill_text":
            if not isinstance(self.value, str) or not self.value.strip() or len(self.value) > 60000:
                raise ValueError("skill text must contain 1–60000 characters")
        elif self.kind in ("system_section", "tool_description"):
            if not isinstance(self.value, str) or not self.value.strip() or len(self.value) > 2000 or len(self.value.splitlines()) > 30:
                raise ValueError("text must contain 1–2000 characters and at most 30 lines")
            if self.kind == "system_section" and self.key not in ("response", "reasoning", "workflow", "verification"):
                raise ValueError("unknown editable system section")
        elif self.kind == "model_parameter":
            if self.key not in ("temperature", "max_tokens"):
                raise ValueError("parameter must be temperature or max_tokens")
            if type(self.value) not in (int, float) or not math.isfinite(self.value):
                raise ValueError("parameter must be a finite number")
            if self.key == "temperature" and not 0 <= self.value <= 2:
                raise ValueError("temperature must be in [0, 2]")
            if self.key == "max_tokens" and (type(self.value) is not int or not 64 <= self.value <= 32768):
                raise ValueError("max_tokens must be an integer in [64, 32768]")
        elif self.kind in ("tool_selection", "skill"):
            if type(self.value) is not bool:
                raise ValueError("selection must be a boolean")
        elif self.kind == "context":
            if self.key in ("no_memory", "no_skills"):
                if type(self.value) is not bool:
                    raise ValueError("context switch must be a boolean")
            elif self.key == "input_token_budget":
                if type(self.value) is not int or not 4096 <= self.value <= 131072:
                    raise ValueError("input_token_budget must be an integer in [4096, 131072]")
            else:
                raise ValueError("unknown context setting")
        return self

    @property
    def high_risk(self):
        return self.kind in ("tool_selection", "skill", "skill_text", "context") or (self.kind == "system_section" and self.key == "workflow")


def slot(changes):
    edit = changes["edit"]
    return json.dumps([changes["owner"], changes.get("project_id", ""), edit["kind"], edit["key"]])


def snapshot(owner: str, project_id: str = "", *, store=None):
    if not owner:
        return []
    from .store import HarnessEvolutionStore, DEFAULT_DB_PATH
    import os
    # A normal chat with no cycles yet must not create a database.
    if store is None and not os.path.isfile(DEFAULT_DB_PATH):
        return []
    refs = (store or HarnessEvolutionStore()).active_revision().refs
    entries = [v for v in refs.get("runtime_config", {}).values()
               if v.get("owner") == owner and v.get("project_id", "") in ("", project_id)]
    entries.sort(key=lambda v: bool(v.get("project_id")))
    merged = {}
    for entry in entries:
        edit = ConfigEdit.model_validate(entry["edit"])
        merged[(edit.kind, edit.key)] = edit.model_dump()
    return list(merged.values())


def with_edit(edits, edit):
    edit = ConfigEdit.model_validate(edit).model_dump()
    return [e for e in edits if (e["kind"], e["key"]) != (edit["kind"], edit["key"])] + [edit]


def options(edits, hopts, temperature, max_tokens, *, temperature_explicit=False):
    hopts = dict(hopts)
    for raw in edits:
        edit = ConfigEdit.model_validate(raw)
        if edit.kind == "context":
            hopts.setdefault(edit.key, edit.value)  # explicit turn settings win
        elif edit.kind == "model_parameter":
            if edit.key == "temperature" and not temperature_explicit: temperature = edit.value
            if edit.key == "max_tokens": max_tokens = edit.value
    return hopts, temperature, max_tokens


def messages(edits, original, owner=""):
    out = copy.deepcopy(original)
    sections = []
    for raw in edits:
        edit = ConfigEdit.model_validate(raw)
        if edit.kind == "system_section":
            sections.append(f"## {edit.key}\n{edit.value}")
        elif edit.kind == "skill_text" or (edit.kind == "skill" and edit.value):
            from src.harness_refinement import targets
            replacement = next((e["value"] for e in edits if e["kind"] == "skill_text" and e["key"] == edit.key), None)
            if edit.kind == "skill" and replacement is not None:
                continue
            content = replacement if replacement is not None else targets.read_current("skill", f"skill:{edit.key}", owner)
            if content:
                from src.prompt_security import untrusted_context_message
                out.append(untrusted_context_message("skills", f"## Skill {edit.key}\n{content}"))
    if sections:
        text = "\n\nEvaluated configuration:\n" + "\n\n".join(sections)
        if out and out[0].get("role") == "system" and isinstance(out[0].get("content"), str):
            out[0]["content"] += text
        else:
            out.insert(0, {"role": "system", "content": text})
    return out


def tools(edits, original):
    out = copy.deepcopy(original)
    for raw in edits:
        edit = ConfigEdit.model_validate(raw)
        if edit.kind == "tool_selection" and not edit.value:
            out = [s for s in out if s.get("function", s).get("name") != edit.key]
        elif edit.kind == "tool_description":
            for schema in out:
                fn = schema.get("function", schema)
                if fn.get("name") == edit.key:
                    fn["description"] = edit.value
    return out
