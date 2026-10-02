"""context_engine/prompt_audit.py - what was compiled versus what was sent.

The compiler's manifest explains the packet it built.  The agent loop then adds
blocks of its own after the compiler (a harness note, a reply-language
directive, the skills index, the repository map, a compaction summary), trims
for the route, and the transport may rewrite the list once more.  The manifest
alone therefore cannot say what the model actually read, and a change in
behaviour between two rounds cannot be explained from it.

This module is the instrument for that gap.  Everything here is a pure function
over data the loop already holds; nothing reads a store, calls a model or
touches the messages it is given.

* ``fingerprint_prompt`` - one hash per block of the exact list about to be
  sent, plus cumulative prefix hashes, so two rounds can be compared block by
  block and the first divergence located.
* ``source_receipts`` - one row per compiled item: reference, version, content
  hash.  A source with no declared version says so (``revision_state``) and
  still carries its content hash, so every row is explainable.
* ``query_receipts`` - the learned-memory, document, objective and personal
  memory queries the compiler ran, as hashes and identities (never the query
  text or a path).
* ``reconcile`` - the manifest against the final prompt: was the packet
  delivered byte for byte, which compiled sources are present, and which
  blocks are in the prompt that the compiler never produced.
* ``diff_prompts`` - two consecutive rounds: the common prefix, the first
  divergence and its bucket, and what was added, removed or changed.

``PromptAuditRecorder`` keeps the per-turn state and is the only stateful
piece.  It never raises into a caller: an audit is an observation, and an
observation that can end a turn is one incident away from being switched off.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Rows kept per list in a persisted summary.
MAX_ROWS = 40
#: Rounds kept per turn.
MAX_ROUNDS = 60

_SHA_PREFIX = 16

#: ``revision_state`` values on a source receipt.
REVISION_DECLARED = "declared"
REVISION_CONTENT_HASH = "content_hash"
REVISION_UNVERSIONED = "unversioned"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), default=str)


def sha256_text(text: Any) -> str:
    return _sha(str(text if text is not None else "").encode("utf-8"))


# Only the fields a provider receives take part in a block hash.  Private loop
# markers (``_agent_injected``, ``_harness_note``) and ``metadata`` are
# bookkeeping, and hashing them would make an identical wire prompt look
# different from one round to the next.
_WIRE_FIELDS = ("role", "content", "name", "tool_call_id", "tool_calls")


def canonical_message(message: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: message.get(k) for k in _WIRE_FIELDS
            if isinstance(message, Mapping) and message.get(k) not in (None, "")}


def message_sha256(message: Mapping[str, Any]) -> str:
    return sha256_text(_canonical(canonical_message(message)))


def _content_text(message: Mapping[str, Any]) -> str:
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: List[str] = []
        for block in content:
            if isinstance(block, Mapping):
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _origin(message: Mapping[str, Any], bucket: str) -> str:
    """Who put this block in the prompt, as a short label.

    The loop tags what it injects; everything untagged is either the
    conversation itself or a block whose origin the loop never recorded."""
    tag = message.get("_agent_injected")
    if tag:
        return f"injected:{str(tag)[:40]}"
    if message.get("_harness_note"):
        return "harness_note"
    meta = message.get("metadata")
    if isinstance(meta, Mapping):
        if meta.get("compacted"):
            return "compaction_summary"
        source = str(meta.get("source") or "").strip()
        if source:
            return f"source:{source[:60]}"
        if meta.get("skill_disclosure"):
            return "skills"
    return bucket


def _bucket(message: Mapping[str, Any], *, is_last_user: bool) -> str:
    try:
        from src.context_ledger import classify
        return classify(dict(message), is_last_user=is_last_user)
    except Exception:  # noqa: BLE001 - an audit never breaks a turn
        return "conversation"


def _last_user_index(messages: Sequence[Mapping[str, Any]]) -> int:
    try:
        from src.context_engine.manifest import _last_user_index as pick
        return pick(messages)
    except Exception:  # noqa: BLE001
        return -1


# -- fingerprints --------------------------------------------------------------

def fingerprint_prompt(messages: Sequence[Mapping[str, Any]],
                       tool_schemas: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    """Per-block and cumulative hashes of the exact list about to be sent.

    ``prefix_sha256[i]`` covers blocks ``0..i``: two prompts share a KV-cache
    prefix exactly as far as their prefix hashes agree."""
    rows = [m for m in (messages or ()) if isinstance(m, Mapping)]
    last_user = _last_user_index(rows)
    blocks: List[Dict[str, Any]] = []
    running = hashlib.sha256()
    for index, message in enumerate(rows):
        digest = message_sha256(message)
        running.update(digest.encode("ascii"))
        bucket = _bucket(message, is_last_user=(index == last_user))
        text = _content_text(message)
        blocks.append({
            "index": index,
            "role": str(message.get("role") or ""),
            "bucket": bucket,
            "origin": _origin(message, bucket),
            "sha256": digest,
            "prefix_sha256": running.hexdigest(),
            "chars": len(text),
            "tokens_estimate": max(1, len(text) // 4) if text else 0,
        })
    tools = list(tool_schemas) if tool_schemas else []
    tools_sha = sha256_text(_canonical(tools)) if tools else ""
    prompt_sha = sha256_text(running.hexdigest() + ":" + tools_sha)
    return {
        "schema_version": SCHEMA_VERSION,
        "messages": len(blocks),
        "blocks": blocks,
        "tools": len(tools),
        "tools_sha256": tools_sha,
        "prompt_sha256": prompt_sha,
        "tokens_estimate": sum(b["tokens_estimate"] for b in blocks),
    }


def _system_prefix(messages: Sequence[Mapping[str, Any]], fingerprint: Mapping[str, Any]) -> str:
    """Cumulative hash of the leading system messages (empty when there are none).

    Two turns whose prompts were shaped by the same settings share it, whatever
    the user typed: it is what a pinned chat compares across turns."""
    count = 0
    for message in messages:
        if str(message.get("role") or "") != "system":
            break
        count += 1
    blocks = fingerprint.get("blocks") or []
    if not count or count > len(blocks):
        return ""
    return str(blocks[count - 1]["prefix_sha256"])[:_SHA_PREFIX]


def stable_prefix_blocks(previous: Mapping[str, Any], current: Mapping[str, Any]) -> int:
    """How many leading blocks of ``current`` are byte-identical to ``previous``."""
    left = previous.get("blocks") or []
    right = current.get("blocks") or []
    count = 0
    for a, b in zip(left, right):
        if a.get("prefix_sha256") != b.get("prefix_sha256"):
            break
        count += 1
    return count


def diff_prompts(previous: Optional[Mapping[str, Any]],
                 current: Mapping[str, Any]) -> Dict[str, Any]:
    """Explain the change between two rounds' prompts.

    ``prefix_intact`` is the property that decides whether a server can reuse
    its cached prefix: every block of the previous prompt is still there, in
    order, with the same bytes, and the new blocks are only appended.  The
    common tail (a tool result, a note) is the normal case.  A change before
    the end is a *prefix break* and is reported with the bucket that caused it."""
    if not previous:
        return {"first": True, "prefix_intact": True, "stable_prefix_blocks": 0,
                "added": [], "removed": [], "changed": [],
                "tools_changed": False}
    left = list(previous.get("blocks") or [])
    right = list(current.get("blocks") or [])
    common = stable_prefix_blocks(previous, current)
    prev_hashes = {b["sha256"] for b in left}
    cur_hashes = {b["sha256"] for b in right}

    def row(block: Mapping[str, Any]) -> Dict[str, Any]:
        return {"index": block["index"], "bucket": block["bucket"],
                "origin": block["origin"], "sha256": block["sha256"][:_SHA_PREFIX],
                "tokens_estimate": block["tokens_estimate"]}

    tail_old = left[common:]
    tail_new = right[common:]
    removed_at = {b["index"]: b for b in tail_old if b["sha256"] not in cur_hashes}
    added_at = {b["index"]: b for b in tail_new if b["sha256"] not in prev_hashes}
    changed: List[Dict[str, Any]] = []
    for position in sorted(set(removed_at) & set(added_at)):
        old, new = removed_at.pop(position), added_at.pop(position)
        changed.append({**row(new), "was": old["sha256"][:_SHA_PREFIX],
                        "was_bucket": old["bucket"]})
    removed = [row(b) for _, b in sorted(removed_at.items())]
    added = [row(b) for _, b in sorted(added_at.items())]
    prefix_intact = common == len(left)
    first_break = None
    if not prefix_intact and left:
        old = left[common] if common < len(left) else None
        new = right[common] if common < len(right) else None
        first_break = {"index": common,
                       "was_bucket": old["bucket"] if old else None,
                       "now_bucket": new["bucket"] if new else None,
                       "was_origin": old["origin"] if old else None,
                       "now_origin": new["origin"] if new else None}
    return {
        "first": False,
        "prefix_intact": prefix_intact,
        "stable_prefix_blocks": common,
        "previous_blocks": len(left),
        "current_blocks": len(right),
        "first_break": first_break,
        "added": added[:MAX_ROWS],
        "removed": removed[:MAX_ROWS],
        "changed": changed[:MAX_ROWS],
        "tools_changed": previous.get("tools_sha256") != current.get("tools_sha256"),
    }


# -- compiled sources ----------------------------------------------------------

def source_receipts(packet: Any) -> List[Dict[str, Any]]:
    """One receipt per delivered item of a packet, with version and hash.

    ``revision`` is what the source declared.  When it declared none the row
    is marked ``unversioned`` and the body hash is the only identity it has;
    a revision that is itself a captured content digest is marked as such."""
    out: List[Dict[str, Any]] = []
    try:
        sections = list(getattr(packet, "sections", ()) or ())
    except Exception:  # noqa: BLE001
        return out
    for section in sections:
        if getattr(section, "kind", "") == "recent_messages":
            continue
        for item in getattr(section, "items", ()) or ():
            body = str(getattr(item, "body", "") or "")
            revision = str(getattr(item, "source_revision", "") or "")
            if not revision:
                state = REVISION_UNVERSIONED
            elif revision.startswith(("captured_utf8_sha256:", "sha256:")):
                state = REVISION_CONTENT_HASH
            else:
                state = REVISION_DECLARED
            out.append({
                "section": str(getattr(section, "kind", "") or ""),
                "source_type": str(getattr(item, "source_type", "") or ""),
                "source_ref": str(getattr(item, "source_ref", "") or ""),
                "revision": revision[:128],
                "revision_state": state,
                "body_sha256": sha256_text(body.strip()),
                "chars": len(body),
                "tokens": int(getattr(item, "tokens", 0) or 0),
                "lanes": list(getattr(item, "lanes", ()) or ()),
                "transformation": str(getattr(item, "transformation", "") or ""),
                "trust_class": str(getattr(item, "trust_class", "") or ""),
                "degraded": bool(getattr(item, "degraded", False)),
                "_body": body.strip(),
            })
    return out


def public_source_receipts(rows: Iterable[Mapping[str, Any]],
                           limit: int = MAX_ROWS) -> List[Dict[str, Any]]:
    """Receipts without the private body, bounded for a report."""
    return [{k: v for k, v in row.items() if not k.startswith("_")}
            for row in list(rows)[:limit]]


_QUERY_FAMILIES = (
    ("objectives", "_objective_reuse_receipts"),
    ("personal_memory", "_personal_memory_reuse_receipts"),
    ("documents", "_document_reuse_receipts"),
    ("learned_memory", "_standing_memory_reuse_receipts"),
)


def _describe_receipt(receipt: Any) -> Dict[str, Any]:
    kind = type(receipt).__name__
    retrieval = getattr(receipt, "retrieval", None)
    query = str(getattr(retrieval, "query", "") or "")
    row: Dict[str, Any] = {
        "kind": kind,
        "query_sha256": sha256_text(query) if query else "",
        "lanes": list(getattr(retrieval, "lanes", ()) or ()),
        "limit": int(getattr(retrieval, "limit", 0) or 0),
        "projection_sha256": str(getattr(receipt, "digest", "") or ""),
    }
    path = getattr(receipt, "path", None)
    if isinstance(path, str) and path:
        row["store_sha256"] = sha256_text(path)[:32]
    if kind == "HybridQueryReceipt":
        row["path"] = "hybrid"
        identity = getattr(receipt, "identity", None)
        row["semantic_lane"] = "installed" if identity is not None else "vetoed"
        clock = getattr(receipt, "clock", None)
        row["scoring_clock"] = clock.isoformat() if hasattr(clock, "isoformat") else ""
    elif kind == "StandingQueryReceipt":
        row["path"] = "standing"
    return row


def query_receipts(delivery: Mapping[str, Any]) -> Dict[str, Any]:
    """The compiled queries of a delivery as identities, never as text.

    A family whose capture was incomplete is reported ``unknown`` rather than
    empty: an absent receipt and a receipt of nothing are different facts."""
    out: Dict[str, Any] = {}
    for family, key in _QUERY_FAMILIES:
        value = delivery.get(key) if isinstance(delivery, Mapping) else None
        if value is None:
            out[family] = {"state": "unknown"}
            continue
        try:
            out[family] = {"state": "captured",
                           "queries": [_describe_receipt(r) for r in value][:MAX_ROWS]}
        except Exception:  # noqa: BLE001
            out[family] = {"state": "unknown"}
    files = delivery.get("_file_reuse_receipts") if isinstance(delivery, Mapping) else None
    if files is None:
        out["files"] = {"state": "unknown"}
    else:
        out["files"] = {"state": "captured",
                        "refs": [{"source_ref": str(ref)[:256], "revision": str(rev)[:96]}
                                 for ref, rev in list(files)[:MAX_ROWS]]}
    return out


# -- manifest versus exact prompt ----------------------------------------------

def _is_packet_message(message: Mapping[str, Any], packet_id: str) -> bool:
    if message.get("_agent_injected") == "context_engine":
        meta = message.get("metadata")
        if not packet_id:
            return True
        return isinstance(meta, Mapping) and meta.get("context_packet_id") == packet_id
    return False


def reconcile(delivery: Optional[Mapping[str, Any]],
              messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The compiled manifest against the exact list that goes to the route.

    Answers, without inference:
      * ``packet_present`` / ``packet_intact`` - the packet message is in the
        prompt and its bytes equal the ones delivered;
      * ``sources`` - per compiled source, whether its body is in the packet
        text that is in the prompt;
      * ``added_after_compile`` - every other block, grouped by origin, that
        the compiler never produced;
      * ``lost_after_compile`` - compiled sources whose body is no longer in
        the prompt (trimmed, compacted or replaced after delivery)."""
    rows = [m for m in (messages or ()) if isinstance(m, Mapping)]
    report = (delivery or {}).get("report") if isinstance(delivery, Mapping) else None
    report = report if isinstance(report, Mapping) else {}
    packet_id = str(report.get("packet_id") or "")
    audit = (delivery or {}).get("_audit") if isinstance(delivery, Mapping) else None
    audit = audit if isinstance(audit, Mapping) else {}

    packet_index = -1
    for index, message in enumerate(rows):
        if _is_packet_message(message, packet_id):
            packet_index = index
            break

    fingerprint = fingerprint_prompt(rows)
    blocks = fingerprint["blocks"]
    result: Dict[str, Any] = {
        "packet_id": packet_id,
        "packet_present": packet_index >= 0,
        "packet_index": packet_index if packet_index >= 0 else None,
        "prompt_sha256": fingerprint["prompt_sha256"],
    }
    packet_text = ""
    if packet_index >= 0:
        packet_text = _content_text(rows[packet_index])
        expected = str(audit.get("message_sha256") or "")
        actual = blocks[packet_index]["sha256"]
        result["packet_sha256"] = actual[:_SHA_PREFIX * 2]
        result["packet_intact"] = (expected == actual) if expected else None
    else:
        result["packet_intact"] = None

    sources: List[Dict[str, Any]] = []
    lost: List[Dict[str, Any]] = []
    for receipt in audit.get("sources") or ():
        body = str(receipt.get("_body") or "")
        present: Optional[bool]
        if packet_index < 0:
            present = False
        elif not body:
            present = None
        else:
            present = body in packet_text
        entry = {"source_ref": receipt.get("source_ref"),
                 "revision": receipt.get("revision"),
                 "revision_state": receipt.get("revision_state"),
                 "body_sha256": receipt.get("body_sha256"),
                 "in_prompt": present}
        sources.append(entry)
        if present is False:
            lost.append(entry)
    result["sources"] = sources[:MAX_ROWS]
    result["lost_after_compile"] = lost[:MAX_ROWS]

    grouped: Dict[str, Dict[str, Any]] = {}
    for index, block in enumerate(blocks):
        if index == packet_index:
            continue
        slot = grouped.setdefault(block["origin"], {
            "origin": block["origin"], "bucket": block["bucket"],
            "blocks": 0, "tokens_estimate": 0, "sha256": []})
        slot["blocks"] += 1
        slot["tokens_estimate"] += block["tokens_estimate"]
        if len(slot["sha256"]) < 8:
            slot["sha256"].append(block["sha256"][:_SHA_PREFIX])
    result["added_after_compile"] = sorted(
        grouped.values(), key=lambda g: (-g["tokens_estimate"], g["origin"]))[:MAX_ROWS]
    compiled_tokens = int(report.get("packet_tokens") or 0)
    result["compiled_tokens"] = compiled_tokens
    result["final_tokens_estimate"] = fingerprint["tokens_estimate"]
    result["outside_compiler_tokens_estimate"] = sum(
        g["tokens_estimate"] for g in grouped.values())
    return result


# -- skills and instruction provenance carried on messages ---------------------

def skill_receipts(messages: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Skill fragments that reached the prompt, with version and hash.

    Read from the assembly receipt the skills renderer attaches to its
    message; a skills block without one is reported as such."""
    out: List[Dict[str, Any]] = []
    for message in messages or ():
        if not isinstance(message, Mapping):
            continue
        meta = message.get("metadata")
        disclosure = meta.get("skill_disclosure") if isinstance(meta, Mapping) else None
        if not isinstance(disclosure, Mapping):
            continue
        for fragment in disclosure.get("fragments") or ():
            if not isinstance(fragment, Mapping):
                continue
            out.append({
                "skill": fragment.get("skill"),
                "level": fragment.get("level"),
                "version": fragment.get("version") or "",
                "version_state": "declared" if fragment.get("version") else "unversioned",
                "fragment_sha256": fragment.get("fragment_sha256"),
                "source": fragment.get("source") or "",
                "source_ref": fragment.get("source_ref") or "",
            })
    return out[:MAX_ROWS]


def instruction_receipts(messages: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Instruction provenance rows the prompt builder attached to a message."""
    out: List[Dict[str, Any]] = []
    for message in messages or ():
        if not isinstance(message, Mapping):
            continue
        meta = message.get("metadata")
        rows = meta.get("instruction_provenance") if isinstance(meta, Mapping) else None
        if isinstance(rows, (list, tuple)):
            out.extend(dict(r) for r in rows if isinstance(r, Mapping))
    return out[:MAX_ROWS]


# -- configuration that shapes the context -------------------------------------

#: Settings that change what the prompt contains or how it is assembled.  A
#: closed list of non-secret keys: the snapshot is persisted with the turn.
CONFIG_KEYS: Tuple[str, ...] = (
    "agent_context_engine", "agent_context_engine_shadow", "agent_context_timeout_ms",
    "agent_context_cache_entries", "agent_project_instructions",
    "agent_project_instructions_files", "agent_project_instructions_max_chars",
    "agent_workspace_trust", "agent_tool_schema_slim", "agent_instruction_hierarchy",
)


def config_snapshot(keys: Sequence[str] = CONFIG_KEYS) -> Dict[str, Any]:
    """The values of the context-shaping settings and one hash over them.

    A key the settings layer cannot answer is recorded as ``None`` and listed
    under ``unreadable`` so a missing value is never read as a default."""
    values: Dict[str, Any] = {}
    unreadable: List[str] = []
    try:
        from src.settings import get_setting
    except Exception:  # noqa: BLE001
        get_setting = None  # type: ignore[assignment]
    for key in keys:
        try:
            if get_setting is None:
                raise LookupError(key)
            value = get_setting(key, None)
            values[key] = value if isinstance(value, (str, int, float, bool, type(None), list)) else str(value)
        except Exception:  # noqa: BLE001
            values[key] = None
            unreadable.append(key)
    return {"values": values, "unreadable": unreadable,
            "config_sha256": sha256_text(_canonical(values))}


# -- wire observation ----------------------------------------------------------

_WIRE_SINK: "contextvars.ContextVar[Optional[Tuple[Any, int]]]" = contextvars.ContextVar(
    "prompt_audit_wire_sink", default=None)


def observe_wire(payload: Any) -> None:
    """Called by the transport with the final request body.  No sink, no work."""
    sink = _WIRE_SINK.get()
    if sink is None:
        return
    try:
        recorder, round_num = sink
        recorder.observe_wire(round_num, payload)
    except Exception:  # noqa: BLE001
        logger.debug("prompt audit: wire observation skipped", exc_info=True)


class PromptAuditRecorder:
    """Per-turn audit state: one record per model round."""

    def __init__(self, *, turn_id: str = "", session_id: str = "") -> None:
        self.turn_id = str(turn_id or "")
        self.session_id = str(session_id or "")
        self.rounds: List[Dict[str, Any]] = []
        self._previous: Optional[Dict[str, Any]] = None
        self._by_round: Dict[int, Dict[str, Any]] = {}
        self._loop_messages_sha = ""
        self._config_sha = ""

    def observe_round(self, round_num: int, messages: Sequence[Mapping[str, Any]],
                      tool_schemas: Optional[Iterable[Any]] = None,
                      delivery: Optional[Mapping[str, Any]] = None,
                      candidate: int = 0) -> Optional[Dict[str, Any]]:
        """Record the final prompt of one round.  Never raises."""
        try:
            fingerprint = fingerprint_prompt(messages, tool_schemas)
            record: Dict[str, Any] = {
                "round": int(round_num),
                "candidate": int(candidate),
                "prompt_sha256": fingerprint["prompt_sha256"],
                "tools_sha256": fingerprint["tools_sha256"],
                "system_prefix_sha256": _system_prefix(messages, fingerprint),
                "messages": fingerprint["messages"],
                "tools": fingerprint["tools"],
                "tokens_estimate": fingerprint["tokens_estimate"],
                "diff": diff_prompts(self._previous, fingerprint),
                "manifest": reconcile(delivery, messages) if delivery else None,
                "config": self._config_record(),
                "skills": skill_receipts(messages),
                "instructions": instruction_receipts(messages),
                "wire": None,
            }
            self._previous = fingerprint
            if len(self.rounds) < MAX_ROUNDS:
                self.rounds.append(record)
            self._by_round[int(round_num)] = record
            _WIRE_SINK.set((self, int(round_num)))
            self._loop_messages_sha = _sha_messages(messages)
            return record
        except Exception:  # noqa: BLE001
            logger.debug("prompt audit: round skipped", exc_info=True)
            return None

    def _config_record(self) -> Dict[str, Any]:
        """Config hash each round; the values only when they changed."""
        snapshot = config_snapshot()
        changed = snapshot["config_sha256"] != self._config_sha
        record: Dict[str, Any] = {"config_sha256": snapshot["config_sha256"],
                                  "changed": bool(self._config_sha) and changed}
        if changed:
            record["values"] = snapshot["values"]
            if snapshot["unreadable"]:
                record["unreadable"] = snapshot["unreadable"]
        self._config_sha = snapshot["config_sha256"]
        return record

    def observe_wire(self, round_num: int, payload: Any) -> None:
        record = self._by_round.get(int(round_num))
        if record is None or not isinstance(payload, Mapping):
            return
        wire_messages = payload.get("messages")
        if not isinstance(wire_messages, (list, tuple)):
            record["wire"] = {"comparable": False, "reason": "no messages array in payload"}
            return
        wire = fingerprint_prompt(wire_messages, payload.get("tools"))
        same = _sha_messages(wire_messages) == self._loop_messages_sha
        record["wire"] = {
            "comparable": True,
            "messages": wire["messages"],
            "prompt_sha256": wire["prompt_sha256"],
            "same_messages_as_loop_view": same,
            "transformed_by_transport": not same,
        }

    def summary(self) -> Dict[str, Any]:
        """Bounded, hash-only summary for the saved message."""
        breaks = [r for r in self.rounds
                  if r["diff"] and not r["diff"].get("first") and not r["diff"].get("prefix_intact")]
        rounds: List[Dict[str, Any]] = []
        first_round_of: Dict[str, int] = {}
        for record in self.rounds[:MAX_ROUNDS]:
            manifest = record.get("manifest")
            if isinstance(manifest, dict) and manifest.get("packet_id"):
                seen = first_round_of.setdefault(manifest["packet_id"], record["round"])
                if seen != record["round"]:
                    # Same packet re-delivered: keep the verdicts, not the rows.
                    record = {**record, "manifest": {
                        **{k: v for k, v in manifest.items()
                           if k not in ("sources", "lost_after_compile")},
                        "sources_same_as_round": seen,
                        "lost_after_compile": manifest.get("lost_after_compile", [])}}
            rounds.append(record)
        return {
            "schema_version": SCHEMA_VERSION,
            "rounds": rounds,
            "prefix_breaks": len(breaks),
            "rounds_recorded": len(self.rounds),
        }


def _sha_messages(messages: Sequence[Mapping[str, Any]]) -> str:
    return sha256_text(_canonical([canonical_message(m) for m in messages
                                   if isinstance(m, Mapping)]))


__all__ = [
    "SCHEMA_VERSION", "MAX_ROWS", "MAX_ROUNDS", "REVISION_DECLARED",
    "REVISION_CONTENT_HASH", "REVISION_UNVERSIONED",
    "sha256_text", "canonical_message", "message_sha256", "fingerprint_prompt",
    "stable_prefix_blocks", "diff_prompts", "source_receipts",
    "public_source_receipts", "query_receipts", "reconcile", "skill_receipts",
    "instruction_receipts", "config_snapshot", "CONFIG_KEYS",
    "observe_wire", "PromptAuditRecorder",
]
