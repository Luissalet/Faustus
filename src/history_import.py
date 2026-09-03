"""Import your past: somebody else's chat export, normalised and searchable.

A user arriving at Faustus has years of conversation somewhere else. The
migration feature nobody offers locally is "bring your whole past here" — so
this module takes a ChatGPT data export, a Claude data export, an LM Studio
chat folder or one of Faustus's own JSON exports, and normalises all of them
to one model:

    Conversation(source, external_id, title, started_at, ended_at, model)
      └─ Message(role, content, ts, ordinal)

in one SQLite file of its own, ``DATA_DIR/history.db`` (WAL, short-lived
connections under a module lock, a corrupt file moved to ``.corrupt`` and
recreated) — the same posture as :mod:`src.memory_engine`, and for the same
reason: an optional feature must never be able to take the app's database
with it.

The five rules this module is built around
------------------------------------------
1. **A parser that does not recognise a file returns False.** Guessing is how
   an importer silently mangles somebody's archive. :meth:`detect` looks for
   the structural markers of its own format and says no to everything else.
2. **A malformed conversation is skipped WITH a reason, never dropped.**
   Parsers yield either a :class:`Conversation` or a :class:`Skipped`, and
   every ``Skipped`` reaches the caller as ``{"why", "where"}``. A file with
   one broken conversation still imports the other four hundred.
3. **Import is idempotent by ``(source, external_id)``.** Re-importing the
   same export updates the rows it already has instead of doubling them; the
   result says how many were ``created`` and how many ``updated``.
4. **A timestamp that cannot be parsed becomes ``None``, never "now".** A
   NULL is an honest "we do not know when this was said". Stamping it with
   the import time would make every imported conversation look like it
   happened today, which corrupts every ordering built on it afterwards.
5. **Large exports stream.** A real ChatGPT ``conversations.json`` is a
   top-level array that can run to hundreds of megabytes; it is read one
   conversation at a time through :class:`_JsonArrayStream` rather than
   loaded whole.

Search is :mod:`src.two_tier_search` over the imported messages, so it works
with no model and no network and reports its tier.

Which shapes are verified and which are inferred
------------------------------------------------
* **ChatGPT** — verified: a top-level array of conversations, each with
  ``title``, ``create_time``, ``update_time``, ``current_node`` and a
  ``mapping`` of ``{id: {message, parent, children}}`` whose messages carry
  ``author.role``, ``content.parts`` and ``create_time``. The current path is
  the ``current_node`` walked up its ``parent`` chain and reversed, which is
  the documented way to read one branch out of the tree.
* **Claude** — verified against exporter documentation: conversations with
  ``uuid``, ``name``, ``created_at`` and ``chat_messages``, each message with
  ``sender`` ("human"/"assistant"), ``text`` and/or a ``content`` list of
  typed blocks.
* **Faustus** — its own export, verified in-tree against
  ``src.chat_export.transcript_to_dict``; upstream produces the same shape, so
  an export made before the fork was renamed imports unchanged.
* **LM Studio — INFERRED.** LM Studio's own documentation gives the folder
  (``~/.lmstudio/conversations/``) and the fact that the files are JSON, and
  then says in as many words that the structure is not to be relied on. Both
  shapes this parser accepts are therefore inferred, not verified, and the
  parser is written to fail closed: if neither shape is present ``detect``
  returns False and nothing is imported.

Stdlib only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from src import two_tier_search

logger = logging.getLogger(__name__)

try:  # pragma: no cover - constants always import in the app
    from src.constants import DATA_DIR as _DEFAULT_DATA_DIR
except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
    _DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# Module-level so tests can point the store somewhere disposable.
DATA_DIR = _DEFAULT_DATA_DIR
DB_FILENAME = "history.db"
UPLOAD_DIRNAME = "history_uploads"

SOURCE_CHATGPT = "chatgpt"
SOURCE_CLAUDE = "claude"
SOURCE_LMSTUDIO = "lmstudio"
SOURCE_FAUSTUS = "faustus"
SOURCES: Tuple[str, ...] = (SOURCE_CHATGPT, SOURCE_CLAUDE, SOURCE_LMSTUDIO, SOURCE_FAUSTUS)

ROLES: Tuple[str, ...] = ("user", "assistant", "system", "tool")

MAX_TITLE_CHARS = 300
MAX_CONTENT_CHARS = 200_000
MAX_PATH_CHARS = 1000
# A conversation tree with a parent cycle must not spin for ever.
MAX_TREE_DEPTH = 20_000
# How deep a folder import walks, and how many files it will look at.
MAX_WALK_DEPTH = 6
MAX_WALK_FILES = 20_000
# detect() reads only a prefix: recognising a 400 MB file must not read it.
DETECT_BYTES = 65_536
# The streamer refills in blocks; a single conversation must fit in memory
# (they are kilobytes), the file never has to.
STREAM_BLOCK = 1 << 20
# Give up looking for the "conversations" key of a wrapper object after this.
WRAPPER_SCAN_BYTES = 4 << 20

# Search: how many rows the ranker is allowed to see. Bounded on purpose — a
# ten-year archive is hundreds of thousands of messages and BM25 over all of
# them on every keystroke is not a feature, it is a hang.
CANDIDATE_LIMIT = 4000
RECENT_FALLBACK = 800
DEFAULT_SEARCH_K = 10
MAX_SEARCH_K = 100


class HistoryImportError(ValueError):
    """Invalid input or an unusable store — routes map this to a 400."""


# ---------------------------------------------------------------------------
# The canonical model
# ---------------------------------------------------------------------------


@dataclass
class Message:
    role: str
    content: str
    ts: Optional[str] = None            # ISO-8601 UTC, or None when unknown
    ordinal: int = 0


@dataclass
class Conversation:
    source: str
    external_id: str
    title: str = ""
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    model: str = ""
    path: str = ""
    messages: List[Message] = field(default_factory=list)


@dataclass
class Skipped:
    """A conversation that could not be read, and why.

    Parsers yield this instead of raising, so one broken record in a 400 MB
    export costs that record and nothing else. It is never dropped: every one
    reaches the caller in ``import_path()["skipped"]``.
    """

    why: str
    where: str = ""


Parsed = Union[Conversation, Skipped]


# ---------------------------------------------------------------------------
# Time — the rule is "None, never now"
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: Any) -> Optional[str]:
    """Any of the shapes these exports use → ``"YYYY-MM-DDTHH:MM:SSZ"``.

    Accepts a unix timestamp in seconds or milliseconds (int, float, or a
    numeric string) and an ISO-8601 string with or without a ``Z``.

    Returns **None** for anything it cannot read — a blank, a ``None``, a
    sentinel like ``0``, a word, an out-of-range number. Never the current
    time: a conversation whose date is unknown must not be recorded as having
    happened at the moment of the import.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number: Optional[float] = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            number = None
        if number is None:
            # ISO-8601. Python accepts "+00:00" but not "Z" before 3.11.
            candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    # Milliseconds if it is far too large to be seconds (≈ year 5138).
    if abs(number) > 1e11:
        number = number / 1000.0
    # 0 is the sentinel every one of these exports uses for "not recorded",
    # and a negative epoch here is corruption, not 1969.
    if number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------


def _clean_title(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_TITLE_CHARS]


def _clean_content(value: Any) -> str:
    text = str(value or "")
    # Keep newlines (a transcript is shaped by them) but drop the control
    # characters an export sometimes carries.
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return text.strip()[:MAX_CONTENT_CHARS]


def _clean_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    if role in ("human", "user"):
        return "user"
    if role in ("assistant", "ai", "model", "bot", "machine"):
        return "assistant"
    if role in ("system", "developer"):
        return "system"
    if role in ("tool", "function"):
        return "tool"
    return role or "user"


def _stable_id(*parts: Any) -> str:
    raw = "\x00".join(str(p if p is not None else "") for p in parts)
    return hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()[:24]


def conversation_key(source: Any, external_id: Any) -> str:
    """The primary key. Deterministic, so a re-import addresses the same row
    without a lookup — which is what makes the whole thing idempotent."""
    return _stable_id(str(source or ""), str(external_id or ""))


# ---------------------------------------------------------------------------
# Streaming JSON: one array element at a time
# ---------------------------------------------------------------------------


class _JsonArrayStream:
    """Yield the elements of a JSON array without holding the file in memory.

    ``json.JSONDecoder.raw_decode`` decodes one value from a string and
    reports where it ended, so the buffer only ever needs to hold the element
    being decoded. A ChatGPT export is a top-level array of conversations,
    each a few kilobytes; the file may be hundreds of megabytes.

    The one shape this could get wrong is a **bare number** at the array's top
    level, where a buffer boundary could cut ``1234`` into a perfectly valid
    ``123``. Every array these parsers read holds objects, which cannot be
    truncated into something valid (the closing brace would be missing), and
    non-dict elements are rejected by the callers anyway.
    """

    def __init__(self, handle: Any, block: int = STREAM_BLOCK):
        self._fh = handle
        self._buf = ""
        self._pos = 0
        self._block = max(4096, int(block))
        self._decoder = json.JSONDecoder()
        self._eof = False

    def _fill(self) -> bool:
        if self._eof:
            return False
        chunk = self._fh.read(self._block)
        if not chunk:
            self._eof = True
            return False
        self._buf += chunk
        return True

    def _compact(self) -> None:
        if self._pos:
            self._buf = self._buf[self._pos:]
            self._pos = 0

    def _peek(self) -> str:
        """The next non-whitespace character, or "" at end of input."""
        while True:
            while self._pos < len(self._buf) and self._buf[self._pos] in " \t\r\n":
                self._pos += 1
            if self._pos < len(self._buf):
                return self._buf[self._pos]
            self._compact()
            if not self._fill():
                return ""

    def _decode_one(self) -> Any:
        while True:
            self._compact()
            try:
                value, end = self._decoder.raw_decode(self._buf, self._pos)
            except ValueError:
                if not self._fill():
                    raise
                continue
            self._pos = end
            return value

    def seek_array(self, key: Optional[str] = None) -> bool:
        """Position the stream just after the opening ``[``.

        With no ``key`` the document must BE an array. With one, an object
        wrapper is accepted and scanned for that key — bounded, so a file that
        never mentions it is rejected instead of read to the end.
        """
        first = self._peek()
        if first == "[":
            self._pos += 1
            return True
        if first != "{" or not key:
            return False
        needle = f'"{key}"'
        scanned = 0
        while True:
            found = self._buf.find(needle, self._pos)
            if found >= 0:
                self._pos = found + len(needle)
                if self._peek() != ":":
                    continue
                self._pos += 1
                if self._peek() != "[":
                    return False
                self._pos += 1
                return True
            # Keep the tail in case the needle straddles the boundary.
            keep = len(needle)
            if len(self._buf) > keep:
                scanned += len(self._buf) - keep
                self._buf = self._buf[-keep:]
                self._pos = 0
            if scanned > WRAPPER_SCAN_BYTES or not self._fill():
                return False

    def items(self) -> Iterator[Any]:
        while True:
            char = self._peek()
            if char in ("", "]"):
                return
            if char == ",":
                self._pos += 1
                continue
            yield self._decode_one()


def _iter_json_array(path: str, key: Optional[str] = "conversations") -> Iterator[Any]:
    """Elements of the top-level array in ``path`` (or of ``path[key]``)."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        stream = _JsonArrayStream(handle)
        if not stream.seek_array(key):
            return
        for item in stream.items():
            yield item


def _read_head(path: str, limit: int = DETECT_BYTES) -> str:
    """The first ``limit`` characters — what detect() is allowed to read."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


class Parser:
    """``detect(path) -> bool`` plus ``parse(path) -> Iterator[Parsed]``.

    ``detect`` must be conservative: it recognises the structural markers of
    its own format and returns False for everything else, because guessing is
    how an importer mangles an archive it should have refused.
    """

    source = ""
    label = ""
    verified = True

    def detect(self, path: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def parse(self, path: str) -> Iterator[Parsed]:  # pragma: no cover
        raise NotImplementedError


def _is_json_file(path: str) -> bool:
    return os.path.isfile(path) and path.lower().endswith(".json")


class ChatGPTParser(Parser):
    """OpenAI's ``conversations.json`` (verified shape).

    The mapping is a tree, not a list: an edited or regenerated turn leaves a
    branch behind. ``current_node`` names the leaf of the branch the UI shows,
    so the imported path is that node walked up its ``parent`` chain and
    reversed. Without a usable ``current_node`` the nodes are ordered by
    ``create_time`` instead, which is the best available answer rather than a
    guess at which branch was meant.
    """

    source = SOURCE_CHATGPT
    label = "ChatGPT export"

    def detect(self, path: str) -> bool:
        if not _is_json_file(path):
            return False
        head = _read_head(path)
        if not head:
            return False
        # Both markers, so a file that merely has an "author" somewhere is not
        # mistaken for an export.
        return '"mapping"' in head and '"author"' in head

    def parse(self, path: str) -> Iterator[Parsed]:
        for raw in _iter_json_array(path):
            if not isinstance(raw, dict):
                yield Skipped("not a conversation object", os.path.basename(path))
                continue
            yield self._one(raw, path)

    def _one(self, raw: Dict[str, Any], path: str) -> Parsed:
        title = _clean_title(raw.get("title"))
        external = str(raw.get("conversation_id") or raw.get("id") or "").strip()
        if not external:
            # No id in the export: derive a stable one from what identifies
            # the conversation, so re-importing still updates rather than
            # duplicates. Stable, not random.
            external = "derived-" + _stable_id(title, raw.get("create_time"))
        where = f"{os.path.basename(path)}#{external}"
        mapping = raw.get("mapping")
        if not isinstance(mapping, dict) or not mapping:
            return Skipped("conversation has no mapping of message nodes", where)

        node_ids = self._current_path(mapping, raw.get("current_node"))
        if node_ids is None:
            node_ids = self._by_time(mapping)

        messages: List[Message] = []
        for node_id in node_ids:
            node = mapping.get(node_id)
            if not isinstance(node, dict):
                continue
            message = self._message(node.get("message"))
            if message is None:
                continue
            message.ordinal = len(messages)
            messages.append(message)
        if not messages:
            return Skipped("conversation has no readable messages", where)

        return Conversation(
            source=self.source,
            external_id=external,
            title=title,
            started_at=parse_timestamp(raw.get("create_time")),
            ended_at=parse_timestamp(raw.get("update_time")),
            model=str(raw.get("default_model_slug") or "").strip()[:120],
            path=path[:MAX_PATH_CHARS],
            messages=messages,
        )

    @staticmethod
    def _current_path(mapping: Dict[str, Any], current: Any) -> Optional[List[str]]:
        """The chain from ``current_node`` to the root, reversed. Cycle-safe."""
        node_id = str(current or "").strip()
        if not node_id or node_id not in mapping:
            return None
        chain: List[str] = []
        seen: set = set()
        depth = 0
        while node_id and node_id in mapping and node_id not in seen and depth < MAX_TREE_DEPTH:
            seen.add(node_id)
            chain.append(node_id)
            node = mapping.get(node_id)
            node_id = str((node or {}).get("parent") or "") if isinstance(node, dict) else ""
            depth += 1
        chain.reverse()
        return chain

    @staticmethod
    def _by_time(mapping: Dict[str, Any]) -> List[str]:
        """Every node that has a message, oldest first. A node with no time
        sorts after the timed ones rather than being dated for it."""
        timed: List[Tuple[int, float, str]] = []
        for node_id, node in mapping.items():
            if not isinstance(node, dict) or not isinstance(node.get("message"), dict):
                continue
            raw_time = (node["message"] or {}).get("create_time")
            try:
                when = float(raw_time)
                bucket = 0
            except (TypeError, ValueError):
                when = 0.0
                bucket = 1
            timed.append((bucket, when, str(node_id)))
        timed.sort()
        return [node_id for _, _, node_id in timed]

    @staticmethod
    def _message(raw: Any) -> Optional[Message]:
        if not isinstance(raw, dict):
            return None
        author = raw.get("author")
        role = _clean_role((author or {}).get("role") if isinstance(author, dict) else None)
        content = raw.get("content")
        text = ""
        if isinstance(content, dict):
            parts = content.get("parts")
            if isinstance(parts, list):
                # A part can be an image/audio dict in a multimodal turn; only
                # the strings are text, and a non-string is not invented into
                # one.
                chunks = [p for p in parts if isinstance(p, str) and p.strip()]
                text = "\n\n".join(chunks)
            if not text and isinstance(content.get("text"), str):
                text = content["text"]
        elif isinstance(content, str):
            text = content
        text = _clean_content(text)
        if not text:
            # The root node, and the empty system/tool nodes the export is
            # full of. Nothing was said, so nothing is imported.
            return None
        return Message(role=role, content=text, ts=parse_timestamp(raw.get("create_time")))


class ClaudeParser(Parser):
    """claude.ai's ``conversations.json`` (verified shape).

    A message carries ``text``, or a ``content`` list of typed blocks, or
    both. When both are present ``text`` wins, because that is the flattened
    form the export itself provides; when it is missing the text blocks are
    joined and non-text blocks (tool use, attachments) are left out rather
    than rendered as prose they are not.
    """

    source = SOURCE_CLAUDE
    label = "Claude export"

    def detect(self, path: str) -> bool:
        if not _is_json_file(path):
            return False
        head = _read_head(path)
        if not head:
            return False
        return '"chat_messages"' in head and '"sender"' in head

    def parse(self, path: str) -> Iterator[Parsed]:
        for raw in _iter_json_array(path):
            if not isinstance(raw, dict):
                yield Skipped("not a conversation object", os.path.basename(path))
                continue
            yield self._one(raw, path)

    def _one(self, raw: Dict[str, Any], path: str) -> Parsed:
        title = _clean_title(raw.get("name") or raw.get("title"))
        external = str(raw.get("uuid") or raw.get("id") or "").strip()
        if not external:
            external = "derived-" + _stable_id(title, raw.get("created_at"))
        where = f"{os.path.basename(path)}#{external}"
        chat_messages = raw.get("chat_messages")
        if not isinstance(chat_messages, list):
            return Skipped("conversation has no chat_messages list", where)

        messages: List[Message] = []
        for entry in chat_messages:
            message = self._message(entry)
            if message is None:
                continue
            message.ordinal = len(messages)
            messages.append(message)
        if not messages:
            return Skipped("conversation has no readable messages", where)

        return Conversation(
            source=self.source,
            external_id=external,
            title=title,
            started_at=parse_timestamp(raw.get("created_at")),
            ended_at=parse_timestamp(raw.get("updated_at")),
            model=str(raw.get("model") or "").strip()[:120],
            path=path[:MAX_PATH_CHARS],
            messages=messages,
        )

    @staticmethod
    def _message(raw: Any) -> Optional[Message]:
        if not isinstance(raw, dict):
            return None
        role = _clean_role(raw.get("sender") or raw.get("role"))
        text = raw.get("text")
        text = text if isinstance(text, str) else ""
        if not text.strip():
            blocks = raw.get("content")
            if isinstance(blocks, list):
                chunks = []
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    if str(block.get("type") or "text") != "text":
                        continue
                    body = block.get("text")
                    if isinstance(body, str) and body.strip():
                        chunks.append(body)
                text = "\n\n".join(chunks)
            elif isinstance(blocks, str):
                text = blocks
        text = _clean_content(text)
        if not text:
            return None
        return Message(role=role, content=text,
                       ts=parse_timestamp(raw.get("created_at") or raw.get("timestamp")))


class LMStudioParser(Parser):
    """LM Studio's per-chat JSON files — **INFERRED SHAPE**.

    LM Studio documents the folder (``~/.lmstudio/conversations/``) and that
    the files are JSON, and then states outright that the structure is not
    documented and should not be relied on. Neither shape below is verified
    against a specification, and this docstring is the label that says so.

    Two shapes are accepted, both fail-closed:

    * the flat one the report describes —
      ``{"name", "messages": [{"role", "content"}], "createdAt"}``; and
    * the versioned one the app is understood to write, where each turn is
      ``{"versions": [{"role", "content": [{"type": "text", "text": …}]}],
      "currentlySelected": <index>}``. When several versions exist the
      selected one is taken, and the index is bounds-checked rather than
      trusted.

    ``content`` may be a plain string or a list of typed blocks in either
    shape. If neither shape is present :meth:`detect` returns False and this
    parser imports nothing at all.
    """

    source = SOURCE_LMSTUDIO
    label = "LM Studio chat"
    verified = False

    def detect(self, path: str) -> bool:
        if not _is_json_file(path):
            return False
        head = _read_head(path)
        if not head or '"messages"' not in head:
            return False
        # Faustus's own export also has "messages"; it is claimed by its own
        # parser and must not be claimed here.
        if '"session_id"' in head or '"exported"' in head:
            return False
        if '"mapping"' in head or '"chat_messages"' in head:
            return False
        return '"versions"' in head or '"role"' in head

    def parse(self, path: str) -> Iterator[Parsed]:
        try:
            raw = _load_json(path)
        except (OSError, ValueError) as exc:
            yield Skipped(f"file is not readable JSON: {exc}", os.path.basename(path))
            return
        entries = raw if isinstance(raw, list) else [raw]
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                yield Skipped("not a chat object", os.path.basename(path))
                continue
            yield self._one(entry, path, index, len(entries))

    def _one(self, raw: Dict[str, Any], path: str, index: int, total: int) -> Parsed:
        base = os.path.splitext(os.path.basename(path))[0]
        # ".conversation.json" leaves a ".conversation" tail behind.
        if base.endswith(".conversation"):
            base = base[: -len(".conversation")]
        external = str(raw.get("id") or raw.get("uuid") or "").strip()
        if not external:
            external = base if total == 1 else f"{base}#{index}"
        where = f"{os.path.basename(path)}#{external}"
        turns = raw.get("messages")
        if not isinstance(turns, list):
            return Skipped("chat has no messages list", where)

        messages: List[Message] = []
        for turn in turns:
            message = self._message(turn)
            if message is None:
                continue
            message.ordinal = len(messages)
            messages.append(message)
        if not messages:
            return Skipped("chat has no readable messages", where)

        created = parse_timestamp(raw.get("createdAt") or raw.get("created_at"))
        return Conversation(
            source=self.source,
            external_id=external,
            title=_clean_title(raw.get("name") or raw.get("title") or base),
            started_at=created,
            ended_at=parse_timestamp(raw.get("updatedAt") or raw.get("updated_at")) or created,
            model=str(raw.get("model") or raw.get("modelKey") or "").strip()[:120],
            path=path[:MAX_PATH_CHARS],
            messages=messages,
        )

    @classmethod
    def _message(cls, raw: Any) -> Optional[Message]:
        if not isinstance(raw, dict):
            return None
        turn = raw
        versions = raw.get("versions")
        if isinstance(versions, list) and versions:
            chosen = raw.get("currentlySelected")
            # An index has to BE an index: a bool, a fraction or a word is
            # corruption, not a selection, so it falls back to the first
            # version rather than being truncated into a plausible one.
            if isinstance(chosen, bool):
                position = 0
            elif isinstance(chosen, int):
                position = chosen
            elif isinstance(chosen, float) and chosen.is_integer():
                position = int(chosen)
            else:
                position = 0
            if not 0 <= position < len(versions):
                position = 0
            candidate = versions[position]
            if isinstance(candidate, dict):
                turn = candidate
        role = _clean_role(turn.get("role") or turn.get("sender"))
        text = _clean_content(cls._text_of(turn.get("content")))
        if not text:
            return None
        return Message(role=role, content=text,
                       ts=parse_timestamp(turn.get("createdAt") or turn.get("created_at")
                                          or raw.get("createdAt")))

    @staticmethod
    def _text_of(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = []
            for block in content:
                if isinstance(block, str):
                    chunks.append(block)
                elif isinstance(block, dict) and str(block.get("type") or "text") == "text":
                    body = block.get("text")
                    if isinstance(body, str):
                        chunks.append(body)
            return "\n\n".join(c for c in chunks if c.strip())
        return ""


class FaustusParser(Parser):
    """Faustus's own ``fmt=json`` export (verified in-tree).

    ``src.chat_export.transcript_to_dict`` writes ``{"name", "model",
    "exported", "session_id", "project", "workspace", "message_count",
    "extra", "messages": [{"role", "content", "timestamp", "model",
    "attachments", "tool_calls", "blocks"}]}``. Only the transcript is
    imported — the rendered blocks are a projection of ``content`` and the
    tool calls belong to the session that ran them, not to a search corpus.
    Round-tripping your own archive is the point.
    """

    source = SOURCE_FAUSTUS
    label = "Faustus export"

    def detect(self, path: str) -> bool:
        if not _is_json_file(path):
            return False
        head = _read_head(path)
        if not head or '"messages"' not in head:
            return False
        return '"session_id"' in head or '"exported"' in head

    def parse(self, path: str) -> Iterator[Parsed]:
        try:
            raw = _load_json(path)
        except (OSError, ValueError) as exc:
            yield Skipped(f"file is not readable JSON: {exc}", os.path.basename(path))
            return
        entries = raw if isinstance(raw, list) else [raw]
        for entry in entries:
            if not isinstance(entry, dict):
                yield Skipped("not an export object", os.path.basename(path))
                continue
            yield self._one(entry, path)

    def _one(self, raw: Dict[str, Any], path: str) -> Parsed:
        base = os.path.splitext(os.path.basename(path))[0]
        external = str(raw.get("session_id") or "").strip() or base
        where = f"{os.path.basename(path)}#{external}"
        turns = raw.get("messages")
        if not isinstance(turns, list):
            return Skipped("export has no messages list", where)

        messages: List[Message] = []
        stamps: List[str] = []
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            text = _clean_content(turn.get("content"))
            if not text:
                continue
            ts = parse_timestamp(turn.get("timestamp"))
            if ts:
                stamps.append(ts)
            messages.append(Message(role=_clean_role(turn.get("role")), content=text,
                                    ts=ts, ordinal=len(messages)))
        if not messages:
            return Skipped("export has no readable messages", where)

        # `exported` is when the FILE was written, not when the chat happened,
        # so it is only used as the end stamp when the turns carry none.
        exported = parse_timestamp(raw.get("exported"))
        return Conversation(
            source=self.source,
            external_id=external,
            title=_clean_title(raw.get("name") or base),
            started_at=(min(stamps) if stamps else None),
            ended_at=(max(stamps) if stamps else exported),
            model=str(raw.get("model") or "").strip()[:120],
            path=path[:MAX_PATH_CHARS],
            messages=messages,
        )


# Order matters: the two parsers that both look for "messages" are asked
# after the two that have unmistakable markers of their own, and Faustus is
# asked before LM Studio because LM Studio's detect explicitly stands down
# for a Faustus export.
PARSERS: Tuple[Parser, ...] = (
    ChatGPTParser(), ClaudeParser(), FaustusParser(), LMStudioParser(),
)
PARSERS_BY_SOURCE: Dict[str, Parser] = {p.source: p for p in PARSERS}


def detect_source(path: str) -> Optional[str]:
    """Which parser claims ``path``, or None when none of them does."""
    for parser in PARSERS:
        try:
            if parser.detect(path):
                return parser.source
        except Exception as exc:  # noqa: BLE001 - detection must never raise
            logger.debug("history import: %s.detect(%s) raised: %s",
                         parser.source, path, exc)
    return None


# ---------------------------------------------------------------------------
# Storage — DATA_DIR/history.db, its own file, never a core migration
# ---------------------------------------------------------------------------

_DB_LOCK = threading.RLock()

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id            TEXT PRIMARY KEY,
        source        TEXT NOT NULL DEFAULT '',
        external_id   TEXT NOT NULL DEFAULT '',
        title         TEXT NOT NULL DEFAULT '',
        started_at    TEXT,
        ended_at      TEXT,
        model         TEXT NOT NULL DEFAULT '',
        message_count INTEGER NOT NULL DEFAULT 0,
        imported_at   TEXT NOT NULL DEFAULT '',
        path          TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id              TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        role            TEXT NOT NULL DEFAULT '',
        content         TEXT NOT NULL DEFAULT '',
        ts              TEXT,
        ordinal         INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_key ON conversations(source, external_id)",
    "CREATE INDEX IF NOT EXISTS idx_conv_source ON conversations(source, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, ordinal)",
)

_CONV_COLUMNS = ("id", "source", "external_id", "title", "started_at", "ended_at",
                 "model", "message_count", "imported_at", "path")


def db_path() -> str:
    return os.path.join(DATA_DIR, DB_FILENAME)


def uploads_dir() -> str:
    return os.path.join(DATA_DIR, UPLOAD_DIRNAME)


def _quarantine(path: str, reason: Any) -> None:
    """Move an unreadable database aside so a fresh one can be created.

    Nothing is deleted while it can be kept: a `.corrupt` copy stays on disk
    for anyone who wants to try `sqlite3 .recover` on it later.
    """
    for suffix in ("", "-wal", "-shm"):
        victim = path + suffix
        if not os.path.exists(victim):
            continue
        try:
            os.replace(victim, victim + ".corrupt")
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(victim)
    logger.warning("history.db was unusable (%s); moved aside and recreated", reason)


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    try:
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            # A filesystem that refuses WAL (some network mounts) is not a
            # reason to lose the feature — the default journal works fine.
            pass
        for statement in _SCHEMA:
            conn.execute(statement)
        conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        conn.commit()
        return conn
    except Exception:
        with contextlib.suppress(Exception):
            conn.close()
        raise


def _open() -> sqlite3.Connection:
    path = db_path()
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        return _connect(path)
    except sqlite3.DatabaseError as exc:
        # "file is not a database", a truncated header, a half-written copy:
        # recreate rather than fail every call from here to the next restart.
        _quarantine(path, exc)
        try:
            return _connect(path)
        except sqlite3.Error as exc2:      # pragma: no cover - dead disk
            raise HistoryImportError(f"history store unusable: {exc2}") from exc2


@contextlib.contextmanager
def _db():
    """Short-lived connection under the module lock, committed on success."""
    with _DB_LOCK:
        conn = _open()
        try:
            yield conn
            conn.commit()
        finally:
            with contextlib.suppress(Exception):
                conn.close()


def _row_to_conversation(row: sqlite3.Row) -> Dict[str, Any]:
    out = {key: row[key] for key in _CONV_COLUMNS}
    out["message_count"] = int(out.get("message_count") or 0)
    return out


def _upsert(conn: sqlite3.Connection, conversation: Conversation) -> str:
    """Write one conversation and its messages. Returns "created"/"updated".

    Idempotent by ``(source, external_id)``: the row id is derived from that
    pair, the messages of an existing conversation are replaced wholesale, and
    ``imported_at`` is refreshed. Re-importing the same export twice therefore
    leaves exactly the same number of rows.
    """
    conv_id = conversation_key(conversation.source, conversation.external_id)
    existing = conn.execute("SELECT id FROM conversations WHERE id = ?",
                            (conv_id,)).fetchone()
    verb = "updated" if existing else "created"
    conn.execute(
        """
        INSERT INTO conversations
            (id, source, external_id, title, started_at, ended_at, model,
             message_count, imported_at, path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            started_at = excluded.started_at,
            ended_at = excluded.ended_at,
            model = excluded.model,
            message_count = excluded.message_count,
            imported_at = excluded.imported_at,
            path = excluded.path
        """,
        (conv_id, conversation.source, conversation.external_id, conversation.title,
         conversation.started_at, conversation.ended_at, conversation.model,
         len(conversation.messages), now_iso(), conversation.path),
    )
    conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
    conn.executemany(
        "INSERT INTO messages (id, conversation_id, role, content, ts, ordinal) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(f"{conv_id}-{message.ordinal:06d}", conv_id, message.role,
          message.content, message.ts, message.ordinal)
         for message in conversation.messages],
    )
    return verb


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _walk(path: str) -> List[str]:
    """Every candidate file under ``path``, sorted, bounded, hidden pruned."""
    if os.path.isfile(path):
        return [path]
    files: List[str] = []
    root_depth = path.rstrip(os.sep).count(os.sep)
    for root, dirs, names in os.walk(path):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        if root.count(os.sep) - root_depth >= MAX_WALK_DEPTH:
            dirs[:] = []
        for name in sorted(names):
            if name.startswith(".") or not name.lower().endswith(".json"):
                continue
            files.append(os.path.join(root, name))
            if len(files) >= MAX_WALK_FILES:
                return files
    return files


def import_path(path: Any, *, source: Optional[str] = None,
                dry_run: bool = False) -> Dict[str, Any]:
    """Import a file or a folder of exports.

    ``source`` forces a parser (skipping detection) — the escape hatch for a
    file whose markers a conservative ``detect`` refuses. ``dry_run`` reports
    exactly what a real run would do and writes **nothing**: no database, no
    rows, no timestamps.

    Returns ``{"detected", "sources", "files", "conversations", "messages",
    "created", "updated", "skipped": [{"why", "where"}], "dry_run",
    "seconds"}``.
    """
    started = time.monotonic()
    target = str(path or "").strip()
    result: Dict[str, Any] = {
        "detected": "", "sources": {}, "files": 0, "conversations": 0,
        "messages": 0, "created": 0, "updated": 0, "skipped": [],
        "dry_run": bool(dry_run), "seconds": 0.0,
    }
    if not target:
        raise HistoryImportError("give a path to a file or a folder to import")
    target = os.path.expanduser(target)
    if not os.path.exists(target):
        raise HistoryImportError(f"no such file or folder: {target}")

    forced: Optional[Parser] = None
    if source:
        forced = PARSERS_BY_SOURCE.get(str(source).strip().lower())
        if forced is None:
            raise HistoryImportError(
                f"unknown source {source!r}; known: {', '.join(SOURCES)}")

    candidates = _walk(target)
    if not candidates:
        raise HistoryImportError(f"no .json files to import under {target}")

    # A dry run still has to answer "created or updated?", so it reads the
    # keys already stored — without opening (and therefore creating) a store
    # that is not there yet.
    existing = _existing_keys() if dry_run else set()

    def _record(conv: Conversation, conn: Optional[sqlite3.Connection]) -> None:
        result["conversations"] += 1
        result["messages"] += len(conv.messages)
        result["sources"][conv.source] = result["sources"].get(conv.source, 0) + 1
        if conn is None:
            key = conversation_key(conv.source, conv.external_id)
            result["updated" if key in existing else "created"] += 1
            # The same conversation twice in one run updates the second time,
            # which is what the real path would do.
            existing.add(key)
            return
        result[_upsert(conn, conv)] += 1

    def _run(conn: Optional[sqlite3.Connection]) -> None:
        for candidate in candidates:
            parser = forced or _parser_for(candidate)
            if parser is None:
                result["skipped"].append({
                    "why": "no parser recognised this file",
                    "where": os.path.basename(candidate),
                })
                continue
            result["files"] += 1
            try:
                for parsed in parser.parse(candidate):
                    if isinstance(parsed, Skipped):
                        result["skipped"].append({"why": parsed.why,
                                                  "where": parsed.where})
                    elif isinstance(parsed, Conversation):
                        _record(parsed, conn)
            except Exception as exc:  # noqa: BLE001 - one bad file, not the batch
                logger.warning("history import: %s failed: %s", candidate, exc)
                result["skipped"].append({
                    "why": f"file could not be read: {exc}",
                    "where": os.path.basename(candidate),
                })

    if dry_run:
        _run(None)
    else:
        with _db() as conn:
            _run(conn)

    counted = sorted(result["sources"].items(), key=lambda pair: (-pair[1], pair[0]))
    result["detected"] = counted[0][0] if len(counted) == 1 else (
        "mixed" if counted else "")
    result["seconds"] = round(time.monotonic() - started, 4)
    return result


def _parser_for(path: str) -> Optional[Parser]:
    for parser in PARSERS:
        try:
            if parser.detect(path):
                return parser
        except Exception as exc:  # noqa: BLE001
            logger.debug("history import: %s.detect raised: %s", parser.source, exc)
    return None


def _existing_keys() -> set:
    """``{conversation id}`` already stored — read-only, for the dry run.

    Deliberately does NOT go through :func:`_db`: that would create the
    database file, and "a dry run writes nothing" has to include the store
    itself. No file yet means nothing is stored yet.
    """
    if not os.path.exists(db_path()):
        return set()
    try:
        with _db() as conn:
            return {row["id"] for row in conn.execute("SELECT id FROM conversations")}
    except Exception as exc:  # noqa: BLE001 - a dry run must not fail on the store
        logger.debug("history import: could not read existing keys: %s", exc)
        return set()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_conversations(source: Optional[str] = None, q: Optional[str] = None,
                       limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Conversations newest-first. ``q`` filters on the title, not the body —
    the body is what :func:`search` is for."""
    try:
        limit = max(1, min(int(limit or 100), 1000))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    clauses: List[str] = []
    params: List[Any] = []
    if source:
        clauses.append("source = ?")
        params.append(str(source).strip().lower())
    title = str(q or "").strip()
    if title:
        clauses.append("LOWER(title) LIKE ?")
        params.append(f"%{title.lower()}%")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    # NULL started_at sorts last: an undated conversation is not "the oldest".
    sql = (f"SELECT * FROM conversations{where} "
           "ORDER BY (started_at IS NULL), started_at DESC, title ASC "
           "LIMIT ? OFFSET ?")
    params.extend([limit, offset])
    try:
        with _db() as conn:
            return [_row_to_conversation(row) for row in conn.execute(sql, params)]
    except HistoryImportError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("history import: listing failed: %s", exc)
        return []


def get_conversation(conversation_id: Any) -> Optional[Dict[str, Any]]:
    """One conversation with its messages in order, or None."""
    conv_id = str(conversation_id or "").strip()
    if not conv_id:
        return None
    with _db() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id = ?",
                           (conv_id,)).fetchone()
        if row is None:
            return None
        payload = _row_to_conversation(row)
        payload["messages"] = [
            {"id": m["id"], "role": m["role"], "content": m["content"],
             "ts": m["ts"], "ordinal": int(m["ordinal"] or 0)}
            for m in conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY ordinal ASC",
                (conv_id,))
        ]
        return payload


def delete_conversation(conversation_id: Any) -> bool:
    conv_id = str(conversation_id or "").strip()
    if not conv_id:
        return False
    with _db() as conn:
        cursor = conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
        return cursor.rowcount > 0


def stats() -> Dict[str, Any]:
    """Counts per source plus the date range — never raises."""
    empty = {"conversations": 0, "messages": 0, "sources": [], "oldest": None,
             "newest": None, "enabled": enabled()}
    try:
        with _db() as conn:
            totals = conn.execute(
                "SELECT COUNT(*) AS conversations, "
                "COALESCE(SUM(message_count), 0) AS messages, "
                "MIN(started_at) AS oldest, MAX(started_at) AS newest "
                "FROM conversations").fetchone()
            rows = conn.execute(
                "SELECT source, COUNT(*) AS conversations, "
                "COALESCE(SUM(message_count), 0) AS messages "
                "FROM conversations GROUP BY source ORDER BY source").fetchall()
        return {
            "conversations": int(totals["conversations"] or 0),
            "messages": int(totals["messages"] or 0),
            "oldest": totals["oldest"],
            "newest": totals["newest"],
            "sources": [{"source": r["source"],
                         "conversations": int(r["conversations"] or 0),
                         "messages": int(r["messages"] or 0)} for r in rows],
            "enabled": enabled(),
        }
    except Exception as exc:  # noqa: BLE001 - a stats card never costs a page
        logger.warning("history import: stats failed: %s", exc)
        return empty


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


_TERM_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _candidates(conn: sqlite3.Connection, query: str,
                source: Optional[str]) -> List[sqlite3.Row]:
    """The rows the ranker is allowed to see.

    A LIKE over the query's terms first, because ranking a decade of messages
    on every keystroke is a hang, not a feature. When that finds nothing the
    most recent messages are used instead, so the vector lane still has
    something to say about a query with no literal overlap — and the caller
    is never told "no results" when the ranker simply was not given any.
    """
    terms = [t.lower() for t in _TERM_RE.findall(query) if len(t) > 1][:8]
    base = ("SELECT m.id, m.conversation_id, m.role, m.content, m.ts, m.ordinal, "
            "c.title, c.source, c.started_at FROM messages m "
            "JOIN conversations c ON c.id = m.conversation_id")
    params: List[Any] = []
    clauses: List[str] = []
    if source:
        clauses.append("c.source = ?")
        params.append(str(source).strip().lower())
    if terms:
        clauses.append("(" + " OR ".join("LOWER(m.content) LIKE ?" for _ in terms) + ")")
        params.extend(f"%{term}%" for term in terms)
    sql = base + (" WHERE " + " AND ".join(clauses) if clauses else "")
    sql += " ORDER BY (c.started_at IS NULL), c.started_at DESC LIMIT ?"
    rows = list(conn.execute(sql, params + [CANDIDATE_LIMIT]))
    if rows or not terms:
        return rows
    fallback_params: List[Any] = []
    fallback = base
    if source:
        fallback += " WHERE c.source = ?"
        fallback_params.append(str(source).strip().lower())
    fallback += " ORDER BY (c.started_at IS NULL), c.started_at DESC LIMIT ?"
    return list(conn.execute(fallback, fallback_params + [RECENT_FALLBACK]))


def search(query: Any, k: int = DEFAULT_SEARCH_K, *, source: Optional[str] = None,
           embedder: Any = None) -> Dict[str, Any]:
    """Two-tier search over every imported message. Never raises.

    Returns ``{"hits", "tier", "degraded", "elapsed_ms", "query", "candidates"}``
    where each hit carries the conversation's title, source and timestamp
    alongside the matching snippet and **its offsets into the message**, so a
    reader can highlight the real span rather than re-finding it.
    """
    text = str(query or "").strip()
    try:
        k = max(1, min(int(k or DEFAULT_SEARCH_K), MAX_SEARCH_K))
    except (TypeError, ValueError):
        k = DEFAULT_SEARCH_K
    empty = {"hits": [], "tier": "lexical", "degraded": False, "elapsed_ms": 0.0,
             "query": text, "candidates": 0}
    if not text:
        return empty
    try:
        with _db() as conn:
            rows = _candidates(conn, text, source)
    except Exception as exc:  # noqa: BLE001 - a sick store degrades, never 500s
        logger.warning("history import: search could not read the store: %s", exc)
        return dict(empty, degraded=True)

    corpus = [{
        "id": row["id"],
        "text": row["content"],
        "conversation_id": row["conversation_id"],
        "title": row["title"],
        "source": row["source"],
        "role": row["role"],
        "ts": row["ts"],
        "conversation_started_at": row["started_at"],
        "ordinal": int(row["ordinal"] or 0),
    } for row in rows]

    found = two_tier_search.search(corpus, text, k, embedder=embedder)
    hits = []
    for hit in found.get("hits") or []:
        excerpt = two_tier_search.snippet(hit.get("text"), text)
        hits.append({
            "message_id": hit.get("id"),
            "conversation_id": hit.get("conversation_id"),
            "title": hit.get("title") or "",
            "source": hit.get("source") or "",
            "role": hit.get("role") or "",
            "ts": hit.get("ts"),
            "conversation_started_at": hit.get("conversation_started_at"),
            "ordinal": hit.get("ordinal"),
            "score": hit.get("score"),
            "rank": hit.get("rank"),
            "snippet": excerpt["text"],
            "snippet_start": excerpt["start"],
            "snippet_end": excerpt["end"],
            "match_start": excerpt["match_start"],
            "match_end": excerpt["match_end"],
        })
    return {"hits": hits, "tier": found.get("tier"),
            "degraded": bool(found.get("degraded")),
            "elapsed_ms": found.get("elapsed_ms"), "query": text,
            "candidates": len(corpus)}


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


def enabled() -> bool:
    """``agent_history_import``. Never raises — a broken settings file must
    not take the importer's own CRUD down with it."""
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_history_import", True))
    except Exception:  # noqa: BLE001
        return True
