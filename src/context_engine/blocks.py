"""
context_engine/blocks.py — the context you connect on purpose, not by accident.

The failure this exists to fix has two halves, and they are the same mistake
seen from both ends.

Half one: project memory under `.odysseus/` grows.  Six notes become twenty,
`services.projects` injects the index into the system prompt of every turn,
and on a 32k local model the window is a third gone before the user has typed
anything.  Nobody decided that.  It happened one Markdown file at a time.

Half two: the rule the user stated once — "never touch the migrations folder" —
lives in a note nothing loads, so the agent breaks it every third session and
gets told off again.  Also nobody's decision.

A block is the thing in between: a small, typed, priced piece of standing
context with an owner, a scope and a priority, that is either *connected* to a
session/agent (deliberately, possibly with an expiry) or *always loaded* — and
the always-loaded lane is **rationed**, because "always" is a budget line and
not an adjective.  Going over the ration is not a user error to reject at
write time; it is a fact `audit()` reports and `blocks_for()` acts on, keeping
the highest-priority blocks and leaving the rest connectable on demand.

Three more rules earned the hard way:

* **A stale revision never wins.**  `update_block(..., expected_revision=N)`
  raises `BlockConflict` carrying the revision that was actually there, the
  same posture as `services.objectives`: a conflict is recorded, not resolved
  by whoever wrote last.

* **Never store a secret.**  Content goes through the project's existing
  detector (`core.log_safety`) before it is written, and a hit is refused with
  the pattern named.  Blocks are the one store designed to be pasted into a
  prompt; a key in here is a key on its way to a model endpoint.

* **Import proposes, a person disposes.**  `import_project_memory()` defaults
  to `dry_run=True`, proposes one block per `.odysseus/` file and marks none of
  them `always_loaded`, because §7.3 of the plan says in as many words: do not
  automatically migrate every Markdown note to `always_loaded`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.contracts.base import (
    ContractError,
    as_mapping,
    fingerprint,
    flag,
    one_of,
    reject_unknown,
    text,
    text_list,
    timestamp,
    whole,
)
# Hard import on purpose.  This module's job includes refusing to store a
# credential; if the detector cannot be imported we cannot enforce that, and a
# silent fallback to "nothing looks secret" is the worst of the three options.
from core.log_safety import SECRET_KEY_NAMES, redact_secrets

from . import store
from .contracts import TRUST_CLASSES, ContextCandidate, new_id

logger = logging.getLogger(__name__)

# ── vocabularies and limits ────────────────────────────────────────────────
#
# Closed on purpose, same reason as `contracts.SECTION_KINDS`: a type anyone
# can invent is a type no routing rule can price and no audit can check.

BLOCK_TYPES: Tuple[str, ...] = (
    "identity", "user_profile", "project_rules", "active_goal",
    "working_state", "decision_log", "known_failures", "tool_policy",
    "style_profile", "generation_profile", "shared_team_state",
)

BLOCK_SCOPES: Tuple[str, ...] = ("global", "owner", "project", "session", "agent")

#: The ration.  Per (scope, owner, project): at most this many always-loaded
#: blocks, and at most this many characters of them, whichever comes first.
#: Both numbers are small deliberately — five short blocks is roughly 2-3% of a
#: 32k window, which is what "always" can afford to cost.
ALWAYS_LOADED_MAX_BLOCKS = 5
ALWAYS_LOADED_MAX_CHARS = 8000

DEFAULT_MAX_CHARS = 3000
#: A block is standing context, not a document store.  Anything longer belongs
#: in the RAG corpus with a block pointing at it.
MAX_CONTENT_CHARS = 100_000
DEFAULT_PRIORITY = 50

#: Scopes that can be served without anyone attaching them.  `session` and
#: `agent` blocks are only ever served through an attachment: a session-scoped
#: block nobody connected is dead weight, not standing context.
AUTO_SCOPES: Tuple[str, ...] = ("global", "owner", "project")

#: Which block types an intent asks for, matched as a substring of the intent
#: so that `code_edit`, `code_review` and `write_code` all hit "code".  §7.3:
#: automatic connection based on project, intent and role.
INTENT_BLOCK_TYPES: Dict[str, Tuple[str, ...]] = {
    "chat": ("identity", "user_profile"),
    "code": ("project_rules", "known_failures", "tool_policy"),
    "review": ("project_rules", "known_failures", "decision_log"),
    "plan": ("active_goal", "decision_log", "project_rules"),
    "verify": ("known_failures", "active_goal", "working_state"),
    "generate": ("style_profile", "generation_profile"),
    "team": ("shared_team_state",),
}

#: Where a block lands in a packet.  A block is standing context, so it goes to
#: the section that already owns that kind of statement rather than inventing a
#: "blocks" section nothing else can budget against.
SECTION_BY_TYPE: Dict[str, str] = {
    "identity": "role_and_permissions",
    "user_profile": "role_and_permissions",
    "project_rules": "project_rules",
    "active_goal": "active_goal",
    "working_state": "current_state",
    "decision_log": "decisions",
    "known_failures": "past_experiences",
    "tool_policy": "tool_guidance",
    "style_profile": "project_rules",
    "generation_profile": "multimodal_recipes",
    "shared_team_state": "peer_findings",
}

#: Who wins when two items disagree (`contracts.AUTHORITY_ORDER`).  A block is
#: not automatically authoritative: a `working_state` block is an observation,
#: a `user_profile` block is what the user said, and a `known_failures` block
#: is a validated experience — three very different claims to being right.
AUTHORITY_BY_TYPE: Dict[str, str] = {
    "identity": "system_policy",
    "user_profile": "user_instruction",
    "project_rules": "system_policy",
    "active_goal": "binding_decision",
    "working_state": "observed_state",
    "decision_log": "binding_decision",
    "known_failures": "validated_experience",
    "tool_policy": "system_policy",
    "style_profile": "user_instruction",
    "generation_profile": "user_instruction",
    "shared_team_state": "agent_claim",
}

#: Types where two simultaneously loaded blocks contradict by construction:
#: there is one identity, one active goal, one style the output must have.
SINGLETON_TYPES: Tuple[str, ...] = (
    "identity", "user_profile", "active_goal", "style_profile", "generation_profile",
)

#: Fields `update_block` will change.  `id`, `owner`, `revision`, `created_at`
#: and `updated_at` are not among them — an update is not a way to move a block
#: to another owner.
MUTABLE_FIELDS: Tuple[str, ...] = (
    "type", "scope", "project_id", "title", "content", "priority", "max_chars",
    "always_loaded", "trust_class", "source_refs",
)

PROJECT_MEMORY_DIRNAME = ".odysseus"
MAX_IMPORT_FILES = 50


# ── errors ─────────────────────────────────────────────────────────────────

class BlockError(ValueError):
    """Invalid block input or an unusable store.  Routes map this to a 400."""


class BlockConflict(BlockError):
    """`expected_revision` did not match what is stored.

    Carries `revision`, the revision that was actually there, so the caller can
    re-read, merge and retry instead of guessing.  Being a `BlockError` it is
    also a `ValueError`; routes that want a 409 catch this one first."""

    def __init__(self, block_id: str, expected: Optional[int], actual: int) -> None:
        self.block_id = str(block_id)
        self.expected = expected
        self.revision = int(actual)
        super().__init__(
            f"block {self.block_id} is at revision {self.revision}, not {expected}; "
            "re-read it and re-apply your change"
        )


class SecretInBlock(BlockError):
    """The content carries something shaped like a credential.

    Names the pattern.  "Rejected: contains a secret" is an error nobody can
    act on; "matches api_key" is one you can go and look at."""

    def __init__(self, field: str, pattern: str) -> None:
        self.field = str(field)
        self.pattern = str(pattern)
        super().__init__(
            f"{self.field} looks like it carries a credential (matched {self.pattern!r}); "
            "store a reference to the secret, never the secret"
        )


# ── the secret gate ────────────────────────────────────────────────────────

def secret_pattern(value: Any) -> str:
    """Name the credential pattern in `value`, or `""` if there is none.

    The gate itself is `core.log_safety.redact_secrets`, the same detector the
    log handlers and `external_worker.task_looks_sensitive` use: if redacting
    the text changes it, the text contains a credential-shaped value.  This
    adds only the naming, because a refusal has to say what it saw.
    """
    raw = str(value or "")
    if not raw:
        return ""
    try:
        if redact_secrets(raw) == raw:
            return ""
    except Exception:  # noqa: BLE001 - a detector that throws must not pass the text
        logger.warning("blocks: secret detector failed; refusing the write")
        return "unreadable content"
    lowered = raw.lower()
    for name in SECRET_KEY_NAMES:
        if name in lowered:
            return name
    if "authorization" in lowered:
        return "authorization header"
    return "bearer/basic credential"


def _refuse_secrets(block: "ContextBlock") -> None:
    for field_name, value in (("content", block.content), ("title", block.title)):
        found = secret_pattern(value)
        if found:
            raise SecretInBlock(f"block.{field_name}", found)


# ── schema ─────────────────────────────────────────────────────────────────
#
# Registered at import time; `store.db()` applies it on every connection, so
# there is no "the table did not exist yet" failure mode.  `owner` and
# `project_id` are on both tables and indexed: every query in this module is
# scoped by them, and a scan that has to read another owner's rows to discard
# them is one bug away from serving them.

store.register_schema("blocks", (
    """
    CREATE TABLE IF NOT EXISTS blocks (
        id            TEXT PRIMARY KEY,
        type          TEXT NOT NULL DEFAULT 'project_rules',
        scope         TEXT NOT NULL DEFAULT 'project',
        owner         TEXT NOT NULL DEFAULT '',
        project_id    TEXT NOT NULL DEFAULT '',
        title         TEXT NOT NULL DEFAULT '',
        content       TEXT NOT NULL DEFAULT '',
        priority      INTEGER NOT NULL DEFAULT 50,
        max_chars     INTEGER NOT NULL DEFAULT 3000,
        always_loaded INTEGER NOT NULL DEFAULT 0,
        trust_class   TEXT NOT NULL DEFAULT 'human_explicit',
        source_refs   TEXT NOT NULL DEFAULT '[]',
        revision      INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT NOT NULL DEFAULT '',
        updated_at    TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_blocks_scope ON blocks(owner, project_id, scope)",
    "CREATE INDEX IF NOT EXISTS idx_blocks_type ON blocks(owner, type)",
    "CREATE INDEX IF NOT EXISTS idx_blocks_always ON blocks(owner, always_loaded, priority)",
    """
    CREATE TABLE IF NOT EXISTS block_attachments (
        block_id   TEXT NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        agent_id   TEXT NOT NULL DEFAULT '',
        owner      TEXT NOT NULL DEFAULT '',
        project_id TEXT NOT NULL DEFAULT '',
        expires_at TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (block_id, session_id, agent_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_block_att_session ON block_attachments(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_block_att_agent ON block_attachments(agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_block_att_owner ON block_attachments(owner, project_id)",
))

_COLUMNS: Tuple[str, ...] = (
    "id", "type", "scope", "owner", "project_id", "title", "content", "priority",
    "max_chars", "always_loaded", "trust_class", "source_refs", "revision",
    "created_at", "updated_at",
)


# ── the contract ───────────────────────────────────────────────────────────

def _ts(data: Mapping[str, Any], key: str, path: str) -> str:
    """An ISO-8601 timestamp, or `""` for "not recorded".

    `to_dict()` writes `""` when there is none, so `parse()` has to read `""`
    back as absent — a contract that cannot re-read its own output is a
    one-way serialiser, and these round-trip through SQLite on every read."""
    raw = data.get(key, None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    return timestamp(data, key, path, default="") or ""


@dataclass(frozen=True)
class ContextBlock:
    """§7.2.  One connectable piece of standing context.

    `max_chars` is not a validation limit on `content`; it is the slice of the
    block that may enter a packet.  Keeping the whole content and trimming at
    render time means the block stays editable and greppable while the prompt
    stays cheap — and `audit()` can say which blocks are being truncated."""

    id: str = ""
    type: str = "project_rules"
    scope: str = "project"
    owner: str = ""
    project_id: str = ""
    title: str = ""
    content: str = ""
    priority: int = DEFAULT_PRIORITY
    max_chars: int = DEFAULT_MAX_CHARS
    always_loaded: bool = False
    trust_class: str = "human_explicit"
    source_refs: Tuple[str, ...] = ()
    revision: int = 1
    created_at: str = ""
    updated_at: str = ""
    _KEYS = _COLUMNS

    @classmethod
    def parse(cls, raw: Any, path: str = "block") -> "ContextBlock":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            id=text(data, "id", path, required=False, default=new_id("block"), max_len=128),
            type=one_of(data, "type", path, choices=BLOCK_TYPES,
                        required=False, default="project_rules") or "project_rules",
            scope=one_of(data, "scope", path, choices=BLOCK_SCOPES,
                         required=False, default="project") or "project",
            owner=text(data, "owner", path, required=False, max_len=256),
            project_id=text(data, "project_id", path, required=False, max_len=128),
            title=text(data, "title", path, required=False, max_len=512),
            content=text(data, "content", path, required=False,
                         max_len=MAX_CONTENT_CHARS, allow_blank=True),
            priority=whole(data, "priority", path, default=DEFAULT_PRIORITY,
                           minimum=0, maximum=100) or 0,
            max_chars=whole(data, "max_chars", path, default=DEFAULT_MAX_CHARS,
                            minimum=1, maximum=MAX_CONTENT_CHARS) or DEFAULT_MAX_CHARS,
            always_loaded=flag(data, "always_loaded", path, default=False),
            trust_class=one_of(data, "trust_class", path, choices=tuple(TRUST_CLASSES),
                               required=False, default="human_explicit") or "human_explicit",
            source_refs=text_list(data, "source_refs", path, max_items=64, max_len=1024),
            revision=whole(data, "revision", path, default=1, minimum=1) or 1,
            created_at=_ts(data, "created_at", path),
            updated_at=_ts(data, "updated_at", path),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "type": self.type, "scope": self.scope,
            "owner": self.owner, "project_id": self.project_id,
            "title": self.title, "content": self.content,
            "priority": self.priority, "max_chars": self.max_chars,
            "always_loaded": self.always_loaded, "trust_class": self.trust_class,
            "source_refs": list(self.source_refs), "revision": self.revision,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    def body(self) -> str:
        """What actually enters a packet: the content, capped at `max_chars`."""
        return self.content[:self.max_chars]

    def truncated(self) -> bool:
        return len(self.content) > self.max_chars

    def digest(self) -> str:
        """Identity of the *statement*, not of the row.  Two blocks with the
        same type and the same normalised content are one duplicate however
        differently they are titled."""
        return fingerprint((("type", self.type),
                            ("content", " ".join(self.content.split()).casefold())))

    def section(self) -> str:
        return SECTION_BY_TYPE.get(self.type, "retrieved_memory")

    def authority(self) -> str:
        return AUTHORITY_BY_TYPE.get(self.type, "human_memory")


# ── row <-> block ──────────────────────────────────────────────────────────

def _to_row(block: ContextBlock) -> Tuple[Any, ...]:
    return (
        block.id, block.type, block.scope, block.owner, block.project_id,
        block.title, block.content, block.priority, block.max_chars,
        1 if block.always_loaded else 0, block.trust_class,
        store.dumps(list(block.source_refs)), block.revision,
        block.created_at, block.updated_at,
    )


def _from_row(row: Mapping[str, Any]) -> Optional[ContextBlock]:
    """A row that cannot be parsed is dropped, loudly.

    Reads are on the turn path.  One hand-edited row must cost that row, not
    the whole standing context of the session."""
    data = dict(row)
    data["always_loaded"] = bool(data.get("always_loaded"))
    data["source_refs"] = [str(r) for r in store.loads_list(data.get("source_refs"))]
    try:
        return ContextBlock.parse(data)
    except ContractError as exc:
        logger.warning("blocks: unusable row %s (%s); skipped", data.get("id"), exc)
        return None


_INSERT = (
    "INSERT INTO blocks (" + ", ".join(_COLUMNS) + ") VALUES ("
    + ", ".join("?" for _ in _COLUMNS) + ")"
)
_UPDATE = (
    "UPDATE blocks SET " + ", ".join(f"{c} = ?" for c in _COLUMNS if c != "id")
    + " WHERE id = ?"
)


# ── create, read, update, delete ───────────────────────────────────────────

def create_block(**fields: Any) -> ContextBlock:
    """Validate, refuse secrets, store.  Raises `BlockError` on bad input."""
    data = dict(fields)
    now = store.now_iso()
    data.setdefault("id", new_id("block"))
    data.setdefault("created_at", now)
    data.setdefault("updated_at", now)
    data.setdefault("revision", 1)
    try:
        block = ContextBlock.parse(data)
    except ContractError as exc:
        raise BlockError(str(exc)) from exc
    _refuse_secrets(block)
    try:
        with store.db() as conn:
            conn.execute(_INSERT, _to_row(block))
    except sqlite3.IntegrityError as exc:
        raise BlockError(f"block {block.id} already exists") from exc
    except (sqlite3.Error, store.ContextStoreError) as exc:
        raise BlockError(f"could not store block {block.id}: {exc}") from exc
    return block


def get_block(block_id: str) -> Optional[ContextBlock]:
    """The block, or None.  Never raises: this is read on the turn path."""
    if not str(block_id or "").strip():
        return None
    try:
        with store.db() as conn:
            row = conn.execute("SELECT * FROM blocks WHERE id = ?",
                               (str(block_id),)).fetchone()
    except (sqlite3.Error, store.ContextStoreError) as exc:
        logger.warning("blocks: get_block(%s) failed: %s", block_id, exc)
        return None
    return _from_row(row) if row else None


def update_block(block_id: str, updates: Mapping[str, Any], *,
                 expected_revision: Optional[int] = None) -> ContextBlock:
    """Apply `updates` and bump the revision.

    `expected_revision` is optimistic concurrency, not advice: if it does not
    match what is stored the write is refused with `BlockConflict` carrying the
    revision that was there.  Two agents editing the same `working_state` block
    is the normal case, and last-write-wins loses the first one silently."""
    try:
        changes = dict(as_mapping(updates, "updates")) if updates is not None else {}
    except ContractError as exc:
        raise BlockError(str(exc)) from exc
    if expected_revision is not None and (isinstance(expected_revision, bool)
                                          or not isinstance(expected_revision, int)):
        raise BlockError(f"expected_revision must be a whole number, got {expected_revision!r}")
    unknown = sorted(k for k in changes if k not in MUTABLE_FIELDS)
    if unknown:
        raise BlockError(
            f"cannot update {', '.join(unknown)}; updatable fields are {list(MUTABLE_FIELDS)}"
        )
    try:
        with store.db() as conn:
            row = conn.execute("SELECT * FROM blocks WHERE id = ?",
                               (str(block_id),)).fetchone()
            if row is None:
                raise BlockError(f"block {block_id} does not exist")
            current = _from_row(row)
            if current is None:
                raise BlockError(f"block {block_id} is stored corrupt and cannot be updated")
            if expected_revision is not None and int(expected_revision) != current.revision:
                raise BlockConflict(block_id, expected_revision, current.revision)
            merged = current.to_dict()
            merged.update(changes)
            merged["revision"] = current.revision + 1
            merged["updated_at"] = store.now_iso()
            try:
                block = ContextBlock.parse(merged)
            except ContractError as exc:
                raise BlockError(str(exc)) from exc
            _refuse_secrets(block)
            values = _to_row(block)
            conn.execute(_UPDATE, tuple(values[1:]) + (block.id,))
    except (sqlite3.Error, store.ContextStoreError) as exc:
        raise BlockError(f"could not update block {block_id}: {exc}") from exc
    return block


def delete_block(block_id: str) -> bool:
    """Remove the block and every attachment pointing at it.

    Attachments go with it: an attachment to a block that no longer exists is a
    row that can only ever produce a confusing miss."""
    try:
        with store.db() as conn:
            conn.execute("DELETE FROM block_attachments WHERE block_id = ?", (str(block_id),))
            cur = conn.execute("DELETE FROM blocks WHERE id = ?", (str(block_id),))
            return bool(cur.rowcount)
    except (sqlite3.Error, store.ContextStoreError) as exc:
        raise BlockError(f"could not delete block {block_id}: {exc}") from exc


def list_blocks(*, owner: str = "", project_id: str = "", type: str = "",  # noqa: A002
                scope: str = "", limit: int = 200) -> List[ContextBlock]:
    """Blocks visible to `owner`, newest-priority first.

    `type` shadows the builtin; that is the name in the contract and renaming
    it here would only move the confusion to the caller.  Never raises."""
    where, params = store.scope_clause(owner, project_id)
    if type:
        where += " AND type = ?"
        params.append(str(type))
    if scope:
        where += " AND scope = ?"
        params.append(str(scope))
    capped = max(1, min(int(limit or 200), 1000))
    sql = (f"SELECT * FROM blocks WHERE {where} "
           f"ORDER BY priority DESC, updated_at DESC, id LIMIT ?")
    try:
        with store.db() as conn:
            rows = conn.execute(sql, tuple(params) + (capped,)).fetchall()
    except (sqlite3.Error, store.ContextStoreError) as exc:
        logger.warning("blocks: list_blocks failed: %s", exc)
        return []
    return [b for b in (_from_row(r) for r in rows) if b is not None]


# ── attachments ────────────────────────────────────────────────────────────

def attach(block_id: str, *, session_id: str = "", agent_id: str = "",
           expires_at: str = "") -> None:
    """Connect a block to a session and/or an agent.

    `expires_at` is what makes a temporary connection safe to make: "load the
    incident notes for the next hour" should stop being true in an hour without
    anyone remembering to detach it.  The row survives its expiry (maintenance
    reaps it); `blocks_for()` simply stops serving it."""
    session_id = str(session_id or "").strip()
    agent_id = str(agent_id or "").strip()
    if not session_id and not agent_id:
        raise BlockError("attach needs a session_id, an agent_id, or both")
    expiry = ""
    if str(expires_at or "").strip():
        try:
            expiry = timestamp({"expires_at": expires_at}, "expires_at", "attach") or ""
        except ContractError as exc:
            raise BlockError(str(exc)) from exc
    try:
        with store.db() as conn:
            row = conn.execute("SELECT owner, project_id FROM blocks WHERE id = ?",
                               (str(block_id),)).fetchone()
            if row is None:
                raise BlockError(f"block {block_id} does not exist")
            conn.execute(
                "INSERT OR REPLACE INTO block_attachments "
                "(block_id, session_id, agent_id, owner, project_id, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(block_id), session_id, agent_id, row["owner"], row["project_id"],
                 expiry, store.now_iso()),
            )
    except (sqlite3.Error, store.ContextStoreError) as exc:
        raise BlockError(f"could not attach block {block_id}: {exc}") from exc


def detach(block_id: str, *, session_id: str = "", agent_id: str = "") -> None:
    """Disconnect.  Detaching what was never attached is not an error — a
    caller cleaning up after a run should not have to know what it attached."""
    try:
        with store.db() as conn:
            conn.execute(
                "DELETE FROM block_attachments "
                "WHERE block_id = ? AND session_id = ? AND agent_id = ?",
                (str(block_id), str(session_id or "").strip(), str(agent_id or "").strip()),
            )
    except (sqlite3.Error, store.ContextStoreError) as exc:
        raise BlockError(f"could not detach block {block_id}: {exc}") from exc


def attachments(block_id: str) -> List[Dict[str, Any]]:
    """Every connection of this block, expired ones included and flagged.

    Expired rows are listed on purpose: "why is this block not loading?" is
    answered by seeing the attachment with `expired: true`, not by an empty
    list that looks like it was never attached at all."""
    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(
                "SELECT * FROM block_attachments WHERE block_id = ? "
                "ORDER BY created_at DESC, session_id, agent_id", (str(block_id),)))
    except (sqlite3.Error, store.ContextStoreError) as exc:
        logger.warning("blocks: attachments(%s) failed: %s", block_id, exc)
        return []
    for row in rows:
        row["expired"] = _is_expired(row.get("expires_at"), None)
    return rows


def _is_expired(expires_at: Any, now: Optional[datetime]) -> bool:
    """No expiry means never expires.  An *unreadable* expiry expires now:
    a temporary connection whose deadline we cannot read is not a permanent
    one, and failing closed is the only safe reading of a broken timestamp."""
    raw = str(expires_at or "").strip()
    if not raw:
        return False
    parsed = store.parse_iso(raw)
    if parsed is None:
        return True
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return parsed <= moment


# ── selection ──────────────────────────────────────────────────────────────

def types_for_intent(intent: str) -> Tuple[str, ...]:
    """Which block types this intent asks for, matched as substrings so that
    `code_edit`, `review_code` and `plan_refactor` all route without a table of
    every intent string the app will ever invent.  Empty intent asks for
    nothing: automatic connection is a convenience, never a default."""
    needle = str(intent or "").strip().lower()
    if not needle:
        return ()
    wanted: List[str] = []
    for key, types in INTENT_BLOCK_TYPES.items():
        if key in needle:
            wanted.extend(t for t in types if t not in wanted)
    return tuple(wanted)


def _visible(block: ContextBlock, project_id: str) -> bool:
    """Can this block be served without an explicit attachment?"""
    if block.scope == "project":
        return bool(project_id) and block.project_id == project_id
    return block.scope in ("global", "owner")


def _ration_key(block: ContextBlock) -> Tuple[str, str, str]:
    return (block.scope, block.owner, block.project_id)


def _ration_order(block: ContextBlock) -> Tuple[int, int, str]:
    """Highest priority first, then the cheapest block, then the id.

    Cheapest-first among equals is deliberate: when two blocks tie on priority
    and only one fits, taking the small one leaves room for a third."""
    return (-block.priority, len(block.body()), block.id)


def ration(blocks: Sequence[ContextBlock]) -> Tuple[List[ContextBlock], List[ContextBlock]]:
    """Split always-loaded blocks into `(granted, demoted)` by the ration.

    Pure and exported so that `audit()` and `blocks_for()` cannot drift: what
    the audit says is over the ration is exactly what the selector drops."""
    granted: List[ContextBlock] = []
    demoted: List[ContextBlock] = []
    used: Dict[Tuple[str, str, str], Tuple[int, int]] = {}
    for block in sorted(blocks, key=_ration_order):
        key = _ration_key(block)
        count, chars = used.get(key, (0, 0))
        cost = len(block.body())
        if count + 1 > ALWAYS_LOADED_MAX_BLOCKS or chars + cost > ALWAYS_LOADED_MAX_CHARS:
            demoted.append(block)
            continue
        used[key] = (count + 1, chars + cost)
        granted.append(block)
    return granted, demoted


def _attached_ids(session_id: str, agent_id: str, now: Optional[datetime]) -> set:
    session_id = str(session_id or "").strip()
    agent_id = str(agent_id or "").strip()
    clauses: List[str] = []
    params: List[Any] = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if agent_id:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if not clauses:
        return set()
    sql = ("SELECT block_id, expires_at FROM block_attachments WHERE "
           + " OR ".join(clauses))
    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(sql, tuple(params)))
    except (sqlite3.Error, store.ContextStoreError) as exc:
        logger.warning("blocks: attachment lookup failed: %s", exc)
        return set()
    return {str(r["block_id"]) for r in rows
            if not _is_expired(r.get("expires_at"), now)}


def blocks_for(*, owner: str = "", project_id: str = "", session_id: str = "",
               agent_id: str = "", intent: str = "",
               now: Optional[datetime] = None) -> List[ContextBlock]:
    """The blocks this actor should be given, highest priority first.

    Three lanes, in this order:

    1. **always_loaded**, subject to the ration.  What does not fit is *not*
       served — it stays connectable on demand, and `audit()` says so.
    2. **attached**, and it wins over the ration: somebody asked for this block
       for this session, which is a stronger signal than a priority number.
       An expired attachment is not an attachment.
    3. **intent**, for ordinary (not always-loaded) blocks whose type this
       intent asks for.  A block the ration demoted is never readmitted here;
       that would make the cap a suggestion.

    Never raises: with the store unreachable an actor gets no standing context,
    which is worse than usual and much better than a failed turn."""
    blocks = list_blocks(owner=owner, project_id=project_id, limit=1000)
    if not blocks:
        return []
    attached = _attached_ids(session_id, agent_id, now)

    always = [b for b in blocks if b.always_loaded and _visible(b, project_id)]
    kept, dropped = ration(always)
    demoted = {b.id for b in dropped}
    chosen: Dict[str, ContextBlock] = {b.id: b for b in kept}

    for block in blocks:
        if block.id in attached:
            chosen[block.id] = block
            demoted.discard(block.id)

    wanted = types_for_intent(intent)
    if wanted:
        for block in blocks:
            if block.id in chosen or block.id in demoted or block.always_loaded:
                continue
            if block.type in wanted and _visible(block, project_id):
                chosen[block.id] = block

    return sorted(chosen.values(), key=lambda b: (-b.priority, b.type, b.id))


def as_candidates(blocks: Sequence[ContextBlock]) -> List[ContextCandidate]:
    """Blocks as compiler input.  Nothing here decides whether a block makes it
    into a packet — that is the compiler's budget to spend.  A block longer
    than its own `max_chars` arrives `degraded=True`, because the reader is
    seeing a prefix and has to be told."""
    out: List[ContextCandidate] = []
    for block in blocks or ():
        try:
            out.append(ContextCandidate(
                candidate_id=f"ctxcand_{block.id}",
                source_type="block",
                source_ref=f"block:{block.id}",
                title=block.title or block.type,
                body=block.body(),
                section=block.section(),
                lanes=("mandatory",) if block.always_loaded else ("exact",),
                scores={"priority": round(block.priority / 100.0, 4)},
                trust_class=block.trust_class,
                authority=block.authority(),
                source_revision=str(block.revision),
                observed_at=block.updated_at,
                owner=block.owner,
                project_id=block.project_id,
                degraded=block.truncated(),
                meta={"block_type": block.type, "scope": block.scope,
                      "always_loaded": block.always_loaded,
                      "truncated": block.truncated()},
            ))
        except Exception as exc:  # noqa: BLE001 - one bad block, not the batch
            logger.warning("blocks: %s could not become a candidate: %s", block.id, exc)
    return out


# ── audit ──────────────────────────────────────────────────────────────────

def audit(*, owner: str = "", project_id: str = "") -> Dict[str, Any]:
    """Size, duplication and contradiction, as §7.3 asks for.

    This is the report that makes the ration humane.  Going over the cap is not
    rejected at write time — that would mean losing the block someone just
    wrote — it is *reported here* and *acted on by `blocks_for()`*, so the
    answer to "why is my block not loading?" is a list, not a mystery."""
    blocks = list_blocks(owner=owner, project_id=project_id, limit=1000)
    always = [b for b in blocks if b.always_loaded]
    kept, dropped = ration(always)

    over: List[Dict[str, Any]] = []
    by_key: Dict[Tuple[str, str, str], List[ContextBlock]] = {}
    for block in always:
        by_key.setdefault(_ration_key(block), []).append(block)
    demoted_ids = {b.id for b in dropped}
    for key, group in sorted(by_key.items()):
        lost = [b.id for b in group if b.id in demoted_ids]
        if not lost:
            continue
        over.append({
            "scope": key[0], "owner": key[1], "project_id": key[2],
            "blocks": len(group), "chars": sum(len(b.body()) for b in group),
            "max_blocks": ALWAYS_LOADED_MAX_BLOCKS, "max_chars": ALWAYS_LOADED_MAX_CHARS,
            "demoted": sorted(lost),
        })

    oversized = [
        {"id": b.id, "title": b.title, "chars": len(b.content), "max_chars": b.max_chars}
        for b in blocks if b.truncated()
    ]

    seen: Dict[str, List[str]] = {}
    for block in blocks:
        seen.setdefault(block.digest(), []).append(block.id)
    duplicates = [{"digest": d, "ids": sorted(ids)}
                  for d, ids in sorted(seen.items()) if len(ids) > 1]

    # A contradiction we can actually prove: two blocks of a type that can only
    # have one answer, both loaded at once, in the same scope.  Anything richer
    # than that needs a model, and a heuristic that guesses at disagreement is
    # worse than one that reports the structural case and stops.
    competing: Dict[Tuple[str, str, str, str], List[str]] = {}
    for block in always:
        if block.type in SINGLETON_TYPES:
            competing.setdefault((block.type,) + _ration_key(block), []).append(block.id)
    contradictions = [
        {"type": key[0], "scope": key[1], "project_id": key[3], "ids": sorted(ids)}
        for key, ids in sorted(competing.items()) if len(ids) > 1
    ]

    return {
        "owner": owner, "project_id": project_id,
        "blocks": len(blocks),
        "always_loaded": len(always),
        "always_loaded_granted": len(kept),
        "always_loaded_chars": sum(len(b.body()) for b in always),
        "over_ration": over,
        "oversized": oversized,
        "duplicates": duplicates,
        "contradictions": contradictions,
        "ok": not (over or oversized or duplicates or contradictions),
    }


# ── importing project memory ───────────────────────────────────────────────

#: Filename stem -> block type.  Everything else is `project_rules`, which is
#: the least surprising thing a project note can be.
_IMPORT_TYPE_HINTS: Tuple[Tuple[str, str], ...] = (
    ("decision", "decision_log"),
    ("failure", "known_failures"),
    ("pitfall", "known_failures"),
    ("gotcha", "known_failures"),
    ("tool", "tool_policy"),
    ("style", "style_profile"),
    ("goal", "active_goal"),
    ("objective", "active_goal"),
    ("state", "working_state"),
    ("team", "shared_team_state"),
)


def _import_type(filename: str) -> str:
    stem = os.path.splitext(str(filename or ""))[0].lower()
    for needle, block_type in _IMPORT_TYPE_HINTS:
        if needle in stem:
            return block_type
    return "project_rules"


def import_project_memory(project: Mapping[str, Any], *, owner: str = "",
                          dry_run: bool = True) -> Dict[str, Any]:
    """Propose one block per `.odysseus/` note.  Conservative by construction.

    §7.3 of the plan states the rule outright: do not automatically migrate
    every Markdown note to `always_loaded`.  So: `dry_run=True` by default,
    nothing is marked `always_loaded`, every proposal is priced and shown,
    files already imported are skipped rather than duplicated, and a note
    carrying something that looks like a credential is refused with the pattern
    named instead of being quietly copied into a store whose whole purpose is
    to be pasted into prompts.

    Imported blocks get `trust_class="legacy_import"` — a note somebody wrote
    at some point is not the same claim as a rule somebody stated today, and
    the ranking layer is entitled to know the difference."""
    try:
        data = dict(as_mapping(project or {}, "project"))
    except ContractError as exc:
        raise BlockError(str(exc)) from exc
    workspace = str(data.get("workspace") or "").strip()
    project_id = str(data.get("id") or "").strip()
    report: Dict[str, Any] = {
        "workspace": workspace, "project_id": project_id, "owner": owner,
        "dry_run": bool(dry_run), "proposed": [], "created": [], "skipped": [],
        "note": ("no note is imported as always_loaded; a person promotes a block "
                 "deliberately, one at a time"),
    }
    if not workspace:
        report["skipped"].append({"path": "", "reason": "project has no workspace bound"})
        return report

    root = os.path.join(workspace, PROJECT_MEMORY_DIRNAME)
    try:
        names = sorted(n for n in os.listdir(root) if n.lower().endswith(".md"))
    except OSError as exc:
        report["skipped"].append({"path": root, "reason": f"unreadable: {exc}"})
        return report

    existing = {ref for block in list_blocks(owner=owner, project_id=project_id, limit=1000)
                for ref in block.source_refs}

    for name in names[:MAX_IMPORT_FILES]:
        rel = f"{PROJECT_MEMORY_DIRNAME}/{name}"
        source_ref = f"file:{rel}"
        if source_ref in existing:
            report["skipped"].append({"path": rel, "reason": "already imported"})
            continue
        try:
            with open(os.path.join(root, name), "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read(MAX_CONTENT_CHARS)
        except OSError as exc:
            report["skipped"].append({"path": rel, "reason": f"unreadable: {exc}"})
            continue
        if not content.strip():
            report["skipped"].append({"path": rel, "reason": "empty"})
            continue
        found = secret_pattern(content)
        if found:
            report["skipped"].append({"path": rel, "reason": f"matched secret pattern {found!r}"})
            continue
        proposal = {
            "path": rel,
            "source_ref": source_ref,
            "type": _import_type(name),
            "scope": "project" if project_id else "owner",
            "title": os.path.splitext(name)[0],
            "chars": len(content),
            "priority": DEFAULT_PRIORITY,
            "always_loaded": False,
            "trust_class": "legacy_import",
        }
        report["proposed"].append(proposal)
        if dry_run:
            continue
        try:
            block = create_block(
                type=proposal["type"], scope=proposal["scope"], owner=owner,
                project_id=project_id, title=proposal["title"], content=content,
                priority=DEFAULT_PRIORITY, always_loaded=False,
                trust_class="legacy_import", source_refs=[source_ref],
            )
        except BlockError as exc:
            report["skipped"].append({"path": rel, "reason": str(exc)})
            continue
        proposal["id"] = block.id
        report["created"].append(block.id)

    if len(names) > MAX_IMPORT_FILES:
        report["skipped"].append({
            "path": root,
            "reason": f"{len(names) - MAX_IMPORT_FILES} more files not considered "
                      f"(one import proposes at most {MAX_IMPORT_FILES})",
        })
    return report


__all__ = [
    "BLOCK_TYPES", "BLOCK_SCOPES", "AUTO_SCOPES", "SECTION_BY_TYPE",
    "AUTHORITY_BY_TYPE", "SINGLETON_TYPES", "INTENT_BLOCK_TYPES", "MUTABLE_FIELDS",
    "ALWAYS_LOADED_MAX_BLOCKS", "ALWAYS_LOADED_MAX_CHARS", "DEFAULT_MAX_CHARS",
    "MAX_CONTENT_CHARS", "DEFAULT_PRIORITY", "PROJECT_MEMORY_DIRNAME",
    "BlockError", "BlockConflict", "SecretInBlock", "ContextBlock",
    "secret_pattern", "create_block", "get_block", "update_block", "delete_block",
    "list_blocks", "attach", "detach", "attachments", "types_for_intent",
    "ration", "blocks_for", "as_candidates", "audit", "import_project_memory",
]
