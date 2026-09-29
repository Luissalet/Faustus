"""Per-call PDF binding. No catalogue registration or permission is granted here.

Capture runs without await on the event loop; this is not a cross-thread
hot-reload transaction. Serialized contracts prevent mutable export aliasing.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from src.pdf_tool_contracts import function_definition, tool_names


@dataclass(frozen=True)
class PdfCallBinding:
    name: str
    handler: Callable | None
    definition_json: str
    descriptor_json: str

    def definition(self) -> dict:
        return json.loads(self.definition_json)

    def metadata(self) -> dict:
        descriptor = json.loads(self.descriptor_json)
        return {
            "scope": "call",
            "name": self.name,
            "version": descriptor["version"],
            "descriptor_sha256": hashlib.sha256(self.descriptor_json.encode()).hexdigest(),
        }


def capture_pdf_call(name: str) -> PdfCallBinding | None:
    if name not in tool_names():
        return None
    # The actual executable registration remains authoritative. A descriptive
    # catalogue entry alone must never make a missing handler executable.
    from src.agent_tools import TOOL_HANDLERS
    from src.tool_registry import _descriptor_for_builtin

    handler = TOOL_HANDLERS.get(name)
    definition = function_definition(name)
    descriptor = _descriptor_for_builtin(name, {
        name: (definition["description"], definition["parameters"]),
    })
    serialize = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return PdfCallBinding(name, handler, serialize(definition), serialize(descriptor.to_mapping()))
