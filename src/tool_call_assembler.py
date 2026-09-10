"""
tool_call_assembler.py

Reassembles streamed native tool-call deltas (index/id/name/arguments
fragments, as sent by OpenAI-compatible chat-completions streams) into
complete, validated tool calls.

Deltas can arrive:
  - split at ANY point in the `arguments` JSON text, including inside a
    multi-byte UTF-8 codepoint (an emoji or accented character straddling
    two chunks) or inside a JSON string literal;
  - as `str` (already-decoded text, the common path once httpx/json have
    parsed an SSE line) or as `bytes` (a transport that hands over raw wire
    bytes before decoding);
  - interleaved across several `index` slots, i.e. parallel tool calls whose
    deltas arrive out of order relative to each other.

This module ONLY reassembles deltas and reports the *shape* of the result
(complete / incomplete / invalid). It never executes anything and a
fragment is never surfaced as an actionable call — a call is reported
"complete" only once its accumulated `arguments` text is balanced, valid
JSON. It also never turns free-form text into a tool call: the only
parser that maps model TEXT to a tool invocation is
`tool_parsing.parse_tool_blocks`, and that function is applied solely to
the model's own `round_response` (see src/agent_loop.py) — never to
external/untrusted text and never by this module. A tool argument whose
*value* happens to contain prose or a JSON-looking blob is stored verbatim
as a string; this module never re-interprets it as a nested call.
"""

import codecs
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# Ceilings are deliberately generous defaults — real tool arguments are small
# (paths, short strings, small structured payloads) — but configurable per
# instance so a caller can tighten them for a specific model/tool if needed.
DEFAULT_MAX_ARG_BYTES = 1_000_000
DEFAULT_MAX_DEPTH = 64

# Statuses an AssembledCall can be in. "complete" is the only state where the
# call's `parsed_arguments` may be trusted/executed.
STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete"
STATUS_INVALID = "invalid"


class _DuplicateKeyError(ValueError):
    """Raised by the object_pairs_hook when a JSON object repeats a key."""


def _reject_duplicate_keys(pairs):
    """`object_pairs_hook` for json.loads: refuse arguments with a repeated
    key anywhere in the structure, instead of silently keeping the last
    value (the stdlib default) — a duplicated key is far more likely to be a
    malformed/adversarial payload than an intentional override."""
    seen = set()
    out = {}
    for k, v in pairs:
        if k in seen:
            raise _DuplicateKeyError(f"duplicate key {k!r} in tool arguments")
        seen.add(k)
        out[k] = v
    return out


def _json_depth(obj, _cur: int = 0) -> int:
    """Nesting depth of a parsed JSON value (an empty object/array counts as
    one level so `{}`/`[]` register as depth 1, not 0)."""
    if isinstance(obj, dict):
        if not obj:
            return _cur + 1
        return max(_json_depth(v, _cur + 1) for v in obj.values())
    if isinstance(obj, list):
        if not obj:
            return _cur + 1
        return max(_json_depth(v, _cur + 1) for v in obj)
    return _cur


@dataclass
class AssembledCall:
    """One reconstructed (or still-reconstructing) tool call."""

    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""  # raw accumulated JSON text, in provider order
    extra_content: Optional[dict] = None
    status: str = STATUS_INCOMPLETE
    reason: str = ""
    parsed_arguments: Optional[dict] = None
    last_appended: str = field(default="", repr=False)  # text this feed() call added, if any
    _terminal: bool = field(default=False, repr=False)  # invalid state that further deltas can't fix

    @property
    def resolved_id(self) -> str:
        """The provider call_id, or a stable id derived from the slot index
        when the provider never sent one (some OpenAI-compatible gateways
        omit `id` on every delta of a call)."""
        return self.id or f"assembled:{self.index}"

    def as_legacy_dict(self) -> Dict[str, Any]:
        """Shape matching the pre-assembler accumulator record
        (`{"id", "name", "arguments"[, "extra_content"]}`) so callers that
        only need the old wire format — e.g. llm_core's `_emit_tool_calls`
        SSE event — see byte-for-byte unchanged output."""
        d = {"id": self.id, "name": self.name, "arguments": self.arguments}
        if self.extra_content is not None:
            d["extra_content"] = self.extra_content
        return d


class ToolCallAssembler:
    """Reassembles streamed tool-call deltas into complete calls.

    One instance per in-flight model response. Feed it every delta the
    provider sends via `feed()`; read accumulated state at any time with
    `get()` / `sorted_calls()`. Calls are keyed by slot `index` — the same
    scheme OpenAI-compatible providers use for parallel tool calls — with a
    fallback for providers (observed from Gemini's OpenAI-compat layer) that
    omit `index` on every delta: a delta carrying a function `name` starts a
    new call in the next free slot; an arguments-only continuation with no
    index attaches to the most-recently-touched slot. `id` is carried
    through as metadata (and exposed via `resolved_id`) but never used to
    key or merge slots, since a provider is not guaranteed to repeat it on
    every delta of a call.
    """

    def __init__(self, *, max_arg_bytes: int = DEFAULT_MAX_ARG_BYTES, max_depth: int = DEFAULT_MAX_DEPTH):
        self._max_arg_bytes = max_arg_bytes
        self._max_depth = max_depth
        self._calls: Dict[int, AssembledCall] = {}
        self._decoders: Dict[int, "codecs.IncrementalDecoder"] = {}
        self._last_idx = -1

    def _slot_for(self, raw_index: Optional[int], has_name: bool) -> int:
        if raw_index is not None:
            return raw_index
        if has_name or self._last_idx < 0:
            # Next free slot ABOVE any existing key (not len()) so a
            # provider mixing integer indices with index=None can never
            # collide with a sparse index already in use.
            return max(self._calls, default=-1) + 1
        return self._last_idx

    def feed(self, delta: Optional[Dict[str, Any]]) -> Optional[AssembledCall]:
        """Consume one raw tool_call delta:
        `{index?, id?, type?, function: {name?, arguments?}, extra_content?}`
        (the OpenAI-compatible streaming shape). `function.arguments` may be
        `str`, `bytes`, or `None`/absent. Returns the AssembledCall for the
        slot this delta landed in, re-evaluated for completeness, or None if
        the delta itself was empty/absent.
        """
        if not delta:
            return None
        func = delta.get("function") or {}
        name = func.get("name")
        idx = self._slot_for(delta.get("index"), bool(name))
        self._last_idx = idx

        call = self._calls.get(idx)
        if call is None:
            call = AssembledCall(index=idx)
            self._calls[idx] = call

        call.last_appended = ""
        if delta.get("id"):
            call.id = delta["id"]
        if delta.get("extra_content"):
            call.extra_content = delta["extra_content"]
        if name:
            call.name = name

        if "arguments" in func and not call._terminal:
            self._append_arguments(idx, call, func["arguments"])

        if not call._terminal:
            self._evaluate(call)
        return call

    def _append_arguments(self, idx: int, call: AssembledCall, chunk: Union[str, bytes, None]) -> None:
        if chunk is None:
            return
        if isinstance(chunk, (bytes, bytearray)):
            decoder = self._decoders.get(idx)
            if decoder is None:
                decoder = codecs.getincrementaldecoder("utf-8")()
                self._decoders[idx] = decoder
            try:
                # final=False: buffer a trailing incomplete multi-byte
                # sequence internally instead of raising — it completes
                # (or fails) once the rest arrives in a later delta.
                chunk_text = decoder.decode(bytes(chunk), False)
            except UnicodeDecodeError:
                call.status = STATUS_INVALID
                call.reason = "arguments contain invalid UTF-8 byte sequence"
                call._terminal = True
                return
        else:
            chunk_text = chunk
        if not chunk_text:
            return
        call.arguments += chunk_text
        call.last_appended = chunk_text
        if len(call.arguments.encode("utf-8", errors="replace")) > self._max_arg_bytes:
            call.status = STATUS_INVALID
            call.reason = f"arguments exceed {self._max_arg_bytes} bytes"
            call._terminal = True

    def _evaluate(self, call: AssembledCall) -> None:
        text = call.arguments.strip()
        if not text:
            call.status = STATUS_INCOMPLETE
            return
        try:
            parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except _DuplicateKeyError as e:
            call.status = STATUS_INVALID
            call.reason = str(e)
            call._terminal = True
            return
        except json.JSONDecodeError:
            # Could still close correctly once more chunks arrive — a
            # fragment is never treated as invalid just for being partial.
            call.status = STATUS_INCOMPLETE
            call.reason = ""
            return
        if not isinstance(parsed, dict):
            call.status = STATUS_INVALID
            call.reason = "arguments must be a JSON object"
            call._terminal = True
            return
        depth = _json_depth(parsed)
        if depth > self._max_depth:
            call.status = STATUS_INVALID
            call.reason = f"arguments nesting exceeds max depth {self._max_depth}"
            call._terminal = True
            return
        call.parsed_arguments = parsed
        call.status = STATUS_COMPLETE
        call.reason = ""

    def finalize(self) -> None:
        """Call once the provider's stream has definitively ended. Any call
        still `incomplete` at that point cannot possibly receive more
        deltas, so it is reclassified as `invalid` (empty arguments, or
        arguments JSON that never closed). Calls already `complete` or
        `invalid` are untouched. Optional — llm_core's compatibility path
        does not need it since it only reads the legacy string shape, but
        any caller that wants a definitive judgment (e.g. before running
        validate_tool_arguments) should call this first.
        """
        for call in self._calls.values():
            if call.status != STATUS_INCOMPLETE:
                continue
            if not call.arguments.strip():
                call.status = STATUS_INVALID
                call.reason = "no arguments received before stream ended"
            else:
                call.status = STATUS_INVALID
                call.reason = "arguments JSON never closed before stream ended"

    def get(self, index: int) -> Optional[AssembledCall]:
        return self._calls.get(index)

    def sorted_calls(self) -> List[AssembledCall]:
        return [self._calls[i] for i in sorted(self._calls)]

    def legacy_calls(self) -> List[Dict[str, Any]]:
        """`sorted_calls()` rendered in the old accumulator's wire shape —
        see `AssembledCall.as_legacy_dict`."""
        return [c.as_legacy_dict() for c in self.sorted_calls()]

    def has_calls(self) -> bool:
        return bool(self._calls)
