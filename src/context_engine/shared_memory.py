"""
context_engine/shared_memory.py — the blackboard, and why nothing on it is edited.

§11.  When a Council or a delegation splits one question across five workers,
each of them learns something the others need: a risk in the OAuth flow, a test
that already covers the case, a claim that turns out to be wrong.  Before this
module the only channel between them was the coordinator's summary — a lossy
re-telling by the one actor that did not do the reading.

The failure that argues for append-only is specific.  Two workers read the same
file, wrote opposite conclusions into a shared scratchpad, the later write won,
and the packet that reached the reviewer contained the wrong one with no trace
that the other had ever existed.  A shared note anybody may overwrite is not
shared memory; it is a race with a friendly name.

So there is no `update()` in this module, and there will not be one:

* a correction is a NEW row whose `supersedes` names the old one, and the old
  one moves to `status="superseded"`.  That is the only state change an
  existing row will ever see, along with an author withdrawing their own;
* an author may not supersede or withdraw somebody else's finding.  A peer who
  disagrees posts an `objection` — a row of its own, which reads as a
  disagreement instead of as history being rewritten;
* a claim about code or about a result carries `evidence_refs` or it is
  refused.  A question, a proposal and an objection do not: they are drafts by
  definition, and demanding a citation for "should we cache this?" would only
  teach agents to invent one;
* `promote()` promotes nothing.  It returns a proposal for a person or a
  service with authority, because §11.3 is explicit that the blackboard must
  not become durable memory by default.  Provisional notes that happen to
  survive a run are still provisional notes;
* near-duplicates are grouped, never deleted.  Two workers saying almost the
  same thing is information about agreement; silently keeping one of them is
  the same data loss as the overwrite, arriving by a politer route.

Timestamps are compared as strings on purpose: `store.now_iso()` renders a
fixed-width `YYYY-MM-DDTHH:MM:SSZ`, so `expires_at > now` is a byte comparison
SQLite and Python agree on, which is the same trick `media_runs` uses for its
cutoffs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.contracts.base import (
    ContractError,
    as_mapping,
    one_of,
    reject_unknown,
    text,
    text_list,
)

from . import store
from .contracts import ContextCandidate, new_id

logger = logging.getLogger(__name__)

#: What a worker may put on the board.  Closed, because a kind nobody can
#: enumerate is a kind no evidence policy can price and no reader can trust.
FINDING_KINDS: Tuple[str, ...] = (
    "fact", "risk", "question", "proposal", "objection", "result", "correction",
)

#: `superseded` and `withdrawn` are terminal: a row that reaches either is
#: history and is never served again except through `get()`.
FINDING_STATUSES: Tuple[str, ...] = ("open", "resolved", "superseded", "withdrawn")

#: Kinds that assert something checkable about code or about a run.  §11.2:
#: "evidence refs obligatorias para hechos sobre código o resultados".
EVIDENCE_REQUIRED_KINDS: Tuple[str, ...] = ("fact", "result")

#: Statuses a correction may still attach to.  Correcting a withdrawn row is
#: an argument with somebody who already left.
SUPERSEDABLE_STATUSES: Tuple[str, ...] = ("open", "resolved")

#: Kinds `promote()` will build a proposal for.  A question or a proposal is a
#: draft; promoting one to durable memory would store the asking, not the
#: answer.
PROMOTABLE_KINDS: Tuple[str, ...] = ("fact", "result", "correction", "risk")

#: Where a promotion may be proposed.  Names only — this module does not know
#: how any of them store anything, and that is the point.
PROMOTION_TARGETS: Tuple[str, ...] = (
    "memory", "project_memory", "experience", "decision", "block",
)

#: How long a provisional finding is served when nobody says otherwise.  The
#: real TTL is "until this execution ends" (`expire(scope)`); this is the
#: backstop for the executions that end by crashing.
DEFAULT_TTL_SECONDS = 24 * 3600

#: Jaccard overlap at which two claims are reported as saying the same thing.
#: A grouping threshold, never a deletion threshold.
SIMILARITY_THRESHOLD = 0.6

#: Ceiling on rows pulled out of SQLite for one search.  A blackboard scope
#: holds tens of rows; a query with no scope on a shared install could hold
#: everything, and a ranking pass is not a reason to read a whole table.
_SEARCH_SCAN_LIMIT = 2000

_WORD = re.compile(r"[a-z0-9_]+")

SCHEMA: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS findings (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL DEFAULT '',
        owner TEXT NOT NULL DEFAULT '',
        project_id TEXT NOT NULL DEFAULT '',
        author TEXT NOT NULL DEFAULT '',
        topic TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'fact',
        claim TEXT NOT NULL DEFAULT '',
        evidence_refs TEXT NOT NULL DEFAULT '[]',
        tags TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'open',
        supersedes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        expires_at TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_findings_scope ON findings(scope, status)",
    "CREATE INDEX IF NOT EXISTS idx_findings_owner ON findings(owner, project_id)",
    "CREATE INDEX IF NOT EXISTS idx_findings_topic ON findings(topic)",
    "CREATE INDEX IF NOT EXISTS idx_findings_author ON findings(author)",
    "CREATE INDEX IF NOT EXISTS idx_findings_supersedes ON findings(supersedes)",
)

store.register_schema("findings", SCHEMA)


class FindingRejected(ContractError):
    """The board refused a write, and says which field and why.

    A `ContractError`, therefore a `ValueError`: callers that already catch
    `ValueError` around a contract keep working, and callers that want to tell
    a blackboard rule from a malformed payload can catch this instead."""


def _rejected(exc: ContractError) -> FindingRejected:
    """Re-raise a field-level contract failure as the board's own error, so the
    public API has one exception type instead of two that mean the same thing."""
    if exc.has_got:
        return FindingRejected(exc.path, exc.message, got=exc.got)
    return FindingRejected(exc.path, exc.message)


def _moment(now: Any = None) -> str:
    """`now`, as the fixed-width ISO string every comparison here uses.

    Accepts a datetime (naive is read as UTC, because a naive local time in a
    shared database is a bug that only shows up in another timezone), an ISO
    string, or None for the clock."""
    if now is None:
        return store.now_iso()
    if isinstance(now, datetime):
        moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        return (moment.astimezone(timezone.utc).replace(microsecond=0)
                .isoformat().replace("+00:00", "Z"))
    parsed = store.parse_iso(now)
    return _moment(parsed) if parsed is not None else store.now_iso()


def _after(seconds: int, *, now: Any = None) -> str:
    base = store.parse_iso(_moment(now)) or datetime.now(timezone.utc)
    return _moment(base + timedelta(seconds=max(0, int(seconds))))


def _tokens(value: str) -> frozenset:
    return frozenset(_WORD.findall(str(value or "").lower()))


def _overlap(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


@dataclass(frozen=True)
class Finding:
    """One thing a worker put on the board, and the row it will stay in.

    Frozen because the whole module is: the only way this object changes is
    that a later row supersedes it, and a later row is a different object."""

    id: str = ""
    scope: str = ""
    owner: str = ""
    project_id: str = ""
    author: str = ""
    topic: str = ""
    kind: str = "fact"
    claim: str = ""
    evidence_refs: Tuple[str, ...] = ()
    tags: Tuple[str, ...] = ()
    status: str = "open"
    supersedes: str = ""
    created_at: str = ""
    expires_at: str = ""
    _KEYS = ("id", "scope", "owner", "project_id", "author", "topic", "kind",
             "claim", "evidence_refs", "tags", "status", "supersedes",
             "created_at", "expires_at")

    @classmethod
    def parse(cls, raw: Any, path: str = "finding") -> "Finding":
        """Validate the shape.  Deliberately does NOT enforce the evidence rule:
        that is a rule about writing a new claim, and this has to be able to read
        back every row already on disk, including rows written before a rule
        existed.  `post()` and `supersede()` own the write-time rules."""
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            id=text(data, "id", path, required=False,
                    default=new_id("finding"), max_len=128),
            scope=text(data, "scope", path, max_len=128),
            owner=text(data, "owner", path, required=False, max_len=256),
            project_id=text(data, "project_id", path, required=False, max_len=128),
            author=text(data, "author", path, max_len=128),
            topic=text(data, "topic", path, required=False, max_len=256),
            kind=one_of(data, "kind", path, choices=FINDING_KINDS,
                        required=False, default="fact") or "fact",
            claim=text(data, "claim", path, max_len=8192),
            evidence_refs=text_list(data, "evidence_refs", path,
                                    max_items=64, max_len=2048),
            tags=text_list(data, "tags", path, max_items=32, max_len=64),
            status=one_of(data, "status", path, choices=FINDING_STATUSES,
                          required=False, default="open") or "open",
            supersedes=text(data, "supersedes", path, required=False, max_len=128),
            created_at=text(data, "created_at", path, required=False, max_len=64),
            expires_at=text(data, "expires_at", path, required=False, max_len=64),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "scope": self.scope, "owner": self.owner,
            "project_id": self.project_id, "author": self.author,
            "topic": self.topic, "kind": self.kind, "claim": self.claim,
            "evidence_refs": list(self.evidence_refs), "tags": list(self.tags),
            "status": self.status, "supersedes": self.supersedes,
            "created_at": self.created_at, "expires_at": self.expires_at,
        }

    def has_evidence(self) -> bool:
        return bool(self.evidence_refs)

    def survives_ttl(self) -> bool:
        """§11.2 read strictly: a finding that was resolved AND can point at
        what resolved it outlives the execution that produced it.  Everything
        else stops being served when the execution ends — including a resolved
        one with nothing behind it, which is an opinion that got tidy."""
        return self.status == "resolved" and self.has_evidence()

    def is_current(self) -> bool:
        """Not history.  A superseded or withdrawn row is still readable — it
        is just no longer the board's answer to anything."""
        return self.status not in ("superseded", "withdrawn")

    def within_ttl(self, moment: str) -> bool:
        if not self.expires_at or self.expires_at > moment:
            return True
        return self.survives_ttl()

    def is_served(self, moment: str) -> bool:
        return self.is_current() and self.within_ttl(moment)


def _from_row(row: Mapping[str, Any]) -> Finding:
    """A stored row → a `Finding`, without re-validating it.

    A row that is already on disk has to come back even if a later version of
    the rules would refuse it; refusing to read your own history is how a
    schema change turns into data loss."""
    return Finding(
        id=str(row.get("id") or ""),
        scope=str(row.get("scope") or ""),
        owner=str(row.get("owner") or ""),
        project_id=str(row.get("project_id") or ""),
        author=str(row.get("author") or ""),
        topic=str(row.get("topic") or ""),
        kind=str(row.get("kind") or "fact"),
        claim=str(row.get("claim") or ""),
        evidence_refs=tuple(str(v) for v in store.loads_list(row.get("evidence_refs"))),
        tags=tuple(str(v) for v in store.loads_list(row.get("tags"))),
        status=str(row.get("status") or "open"),
        supersedes=str(row.get("supersedes") or ""),
        created_at=str(row.get("created_at") or ""),
        expires_at=str(row.get("expires_at") or ""),
    )


_INSERT = (
    "INSERT INTO findings (id, scope, owner, project_id, author, topic, kind, "
    "claim, evidence_refs, tags, status, supersedes, created_at, expires_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _insert_params(finding: Finding) -> Tuple[Any, ...]:
    return (finding.id, finding.scope, finding.owner, finding.project_id,
            finding.author, finding.topic, finding.kind, finding.claim,
            store.dumps(list(finding.evidence_refs)),
            store.dumps(list(finding.tags)), finding.status,
            finding.supersedes, finding.created_at, finding.expires_at)


def _require_evidence(finding: Finding, path: str = "finding") -> None:
    if finding.kind in EVIDENCE_REQUIRED_KINDS and not finding.has_evidence():
        raise FindingRejected(
            f"{path}.evidence_refs",
            f"a {finding.kind!r} carries at least one evidence ref (a file line, a "
            "run id, an artifact) — a claim about code or about a result that "
            "cannot be checked is an opinion, and the board already has a kind "
            "for those ('proposal', 'question', 'objection')",
        )


# ── writing: three verbs, none of them "update" ────────────────────────────

def post(**fields: Any) -> Finding:
    """Put a finding on the board.

    Raises `FindingRejected` and writes nothing when the payload is malformed
    or when a fact/result arrives without evidence.  A `ContextStoreError` from
    the store is left to propagate: a worker that believes it published a risk
    and did not has to find out."""
    try:
        finding = Finding.parse(fields)
    except ContractError as exc:
        raise _rejected(exc) from exc
    _require_evidence(finding)
    if finding.status not in ("open", "resolved"):
        # A finding is born open, or born resolved when it is the row that
        # closes something. `superseded` and `withdrawn` are histories, and a
        # new row does not get to claim one it has not had.
        raise FindingRejected("finding.status",
                              "a new finding is posted 'open' or 'resolved'; the "
                              "terminal states are reached through supersede() or "
                              "withdraw()",
                              got=finding.status)
    if finding.supersedes:
        raise FindingRejected("finding.supersedes",
                              "is set by supersede(), which also moves the row it "
                              "names — setting it here would leave the old row "
                              "still being served as current")
    created = finding.created_at or store.now_iso()
    finding = Finding(
        **{**finding.to_dict(),
           "evidence_refs": finding.evidence_refs, "tags": finding.tags,
           "created_at": created,
           "expires_at": finding.expires_at or _after(DEFAULT_TTL_SECONDS, now=created)}
    )
    with store.db() as conn:
        conn.execute(_INSERT, _insert_params(finding))
    logger.debug("finding %s posted by %s in %s (%s)",
                 finding.id, finding.author, finding.scope, finding.kind)
    return finding


def supersede(finding_id: str, *, author: str, claim: str,
              evidence_refs: Sequence[str] = (), reason: str = "") -> Finding:
    """Correct a finding by writing a new one, never by editing the old.

    Only its own author may do this.  A peer who disagrees has `objection`,
    which is a row of their own and reads as a disagreement rather than as the
    other worker having changed their mind.

    `reason` is for the operator's log.  The board's own record of why is the
    correction row itself, which is the thing other agents will read."""
    target = get(finding_id)
    if target is None:
        raise FindingRejected("supersede.finding_id", "names no finding on the board",
                              got=finding_id)
    if not str(author or "").strip():
        raise FindingRejected("supersede.author", "is required")
    if target.author != author:
        raise FindingRejected(
            "supersede.author",
            f"{author!r} may not supersede a finding by {target.author!r} — post an "
            "'objection' instead, so the disagreement is a row of its own rather "
            "than somebody else's claim rewritten",
            got=author,
        )
    if target.status not in SUPERSEDABLE_STATUSES:
        raise FindingRejected("supersede.status",
                              f"finding {finding_id} is already {target.status!r}",
                              got=target.status)
    # A correction inherits the row's TTL, except when that TTL has already
    # passed: a correction written after the execution ended would otherwise be
    # born retired, which is the one moment somebody most needs to read it.
    expires_at = target.expires_at
    if expires_at and expires_at <= store.now_iso():
        expires_at = _after(DEFAULT_TTL_SECONDS)
    try:
        correction = Finding.parse({
            "scope": target.scope, "owner": target.owner,
            "project_id": target.project_id, "author": author,
            "topic": target.topic, "kind": "correction", "claim": claim,
            "evidence_refs": list(evidence_refs), "tags": list(target.tags),
            "supersedes": target.id,
            "created_at": store.now_iso(),
            "expires_at": expires_at,
        })
    except ContractError as exc:
        raise _rejected(exc) from exc
    if target.kind in EVIDENCE_REQUIRED_KINDS and not correction.has_evidence():
        # Correcting a fact is asserting a fact. The rule follows the claim,
        # not the label on the row that carries it.
        raise FindingRejected(
            "supersede.evidence_refs",
            f"correcting a {target.kind!r} needs evidence of its own; without it "
            "the board would replace a checkable claim with an unchecked one",
        )
    with store.db() as conn:
        conn.execute(_INSERT, _insert_params(correction))
        conn.execute("UPDATE findings SET status = 'superseded' WHERE id = ?",
                     (target.id,))
    logger.info("finding %s superseded by %s (%s)", target.id, correction.id,
                reason or "no reason given")
    return correction


def withdraw(finding_id: str, *, author: str, reason: str = "") -> Finding:
    """Take back your own finding.  The row stays; its status changes once.

    Withdrawing is not deleting: everyone who already read it needs to be able
    to see that it was taken back, and by whom.  `reason` is logged; a
    withdrawal other agents must understand should be followed by a `post()`
    that explains it, because that is a row they will actually retrieve."""
    target = get(finding_id)
    if target is None:
        raise FindingRejected("withdraw.finding_id", "names no finding on the board",
                              got=finding_id)
    if target.author != author:
        raise FindingRejected(
            "withdraw.author",
            f"{author!r} may not withdraw a finding by {target.author!r}",
            got=author,
        )
    if target.status == "withdrawn":
        return target
    if target.status == "superseded":
        raise FindingRejected("withdraw.status",
                              "this finding has already been superseded; withdrawing "
                              "it now would hide the row its correction points at",
                              got=target.status)
    with store.db() as conn:
        conn.execute("UPDATE findings SET status = 'withdrawn' WHERE id = ?",
                     (target.id,))
    logger.info("finding %s withdrawn by %s (%s)", target.id, author,
                reason or "no reason given")
    return Finding(**{**target.to_dict(), "evidence_refs": target.evidence_refs,
                      "tags": target.tags, "status": "withdrawn"})


# ── reading: never raises, because a board that is down is not a turn that
#    fails.  A missing blackboard costs the worker a peer's note; a raised
#    exception costs the whole delegation. ──────────────────────────────────

def get(finding_id: str) -> Optional[Finding]:
    """One finding by id, in any status.  `get()` sees withdrawn and superseded
    rows on purpose: that is how a reader follows a correction back."""
    if not str(finding_id or "").strip():
        return None
    try:
        with store.db() as conn:
            row = conn.execute("SELECT * FROM findings WHERE id = ?",
                               (str(finding_id),)).fetchone()
    except store.ContextStoreError:
        logger.warning("blackboard unavailable for get(%s)", finding_id)
        return None
    return _from_row(dict(row)) if row else None


def search(*, scope: str = "", owner: str = "", topic: str = "", kind: str = "",
           tags: Sequence[str] = (), status: str = "open", query: str = "",
           k: int = 20, now: Any = None) -> List[Finding]:
    """Findings that are still being served, newest first.

    Every argument is a filter and an empty one means "do not filter" — `owner`
    included.  That is a deliberate, documented choice: the runtime supplies
    `owner` from `ContextExecution`, and a search argument that quietly meant
    "the rows with no owner" instead of "any" would be the kind of asymmetry a
    reviewer only finds after it has leaked something.

    The default `status="open"` is what a worker composing a packet wants.
    `status=""` drops the status filter but still hides the terminal rows: a
    superseded or withdrawn finding is history, and history is what `get()` and
    an explicit `status="superseded"` are for.

    Nothing is dropped for looking like something else.  Near-duplicates are
    reported by `as_candidates()`, which has a field to report them in."""
    moment = _moment(now)
    where: List[str] = []
    params: List[Any] = []
    if scope:
        where.append("scope = ?")
        params.append(str(scope))
    if owner:
        clause, owner_params = store.scope_clause(str(owner), "", include_global=False)
        where.append(clause)
        params.extend(owner_params)
    if topic:
        where.append("topic = ?")
        params.append(str(topic))
    if kind:
        if kind not in FINDING_KINDS:
            return []
        where.append("kind = ?")
        params.append(str(kind))
    if status:
        if status not in FINDING_STATUSES:
            return []
        where.append("status = ?")
        params.append(str(status))
    else:
        where.append("status NOT IN ('superseded', 'withdrawn')")
    sql = "SELECT * FROM findings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(_SEARCH_SCAN_LIMIT)
    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(sql, params))
    except store.ContextStoreError:
        logger.warning("blackboard unavailable for search(scope=%s)", scope)
        return []

    wanted_tags = {str(t) for t in tags if str(t).strip()}
    wanted_words = _tokens(query)
    limit = max(1, int(k or 1))
    out: List[Finding] = []
    for row in rows:
        finding = _from_row(row)
        if not finding.within_ttl(moment):
            continue
        if wanted_tags and not wanted_tags.issubset(set(finding.tags)):
            continue
        if wanted_words:
            haystack = _tokens(" ".join(
                (finding.claim, finding.topic, " ".join(finding.tags))))
            if not (wanted_words & haystack):
                continue
        out.append(finding)
        if len(out) >= limit:
            break
    return out


def as_candidates(findings: Iterable[Finding]) -> List[ContextCandidate]:
    """Findings → what the compiler ranks, with the duplicate grouping attached.

    The grouping lives here rather than on `Finding` because this is where
    there is a field to put it in.  A `Finding` is a row; a row does not get to
    grow an opinion about its neighbours, and `meta["similar_to"]` on a
    candidate is exactly the "deduplicación semántica como ayuda" §11.2 asks
    for — a hint to the reader, with both rows still present.

    Trust is deliberately low.  A peer's finding is a draft by another agent:
    `untrusted` unless it was resolved and can point at what resolved it, and
    even then it never outranks an observed state."""
    items = [f for f in findings if isinstance(f, Finding)]
    words = [_tokens(" ".join((f.claim, f.topic))) for f in items]
    out: List[ContextCandidate] = []
    for i, finding in enumerate(items):
        similar = [items[j].id for j in range(len(items))
                   if j != i and _overlap(words[i], words[j]) >= SIMILARITY_THRESHOLD]
        resolved = finding.survives_ttl()
        body = finding.claim
        if finding.evidence_refs:
            body = f"{body}\nevidence: " + ", ".join(finding.evidence_refs)
        out.append(ContextCandidate.parse({
            "source_type": "finding",
            "source_ref": f"finding:{finding.id}",
            "title": f"{finding.kind}: {finding.topic}" if finding.topic else finding.kind,
            "body": body,
            "section": "peer_findings",
            "lanes": ["lexical"],
            "scores": {"evidence": 1.0 if finding.has_evidence() else 0.0},
            "trust_class": "agent_validated" if resolved else "untrusted",
            "authority": "agent_claim",
            "observed_at": finding.created_at,
            "owner": finding.owner,
            "project_id": finding.project_id,
            "meta": {
                "finding_id": finding.id, "scope": finding.scope,
                "author": finding.author, "kind": finding.kind,
                "status": finding.status, "tags": list(finding.tags),
                "evidence_refs": list(finding.evidence_refs),
                "supersedes": finding.supersedes,
                "similar_to": similar,
            },
        }))
    return out


def expire(scope: str = "", *, now: Any = None) -> int:
    """Retire what the TTL caught up with.  Marks, never deletes.

    Naming a `scope` says that execution has ended, which is the TTL §11.2
    actually specifies: every provisional row in it has its `expires_at`
    brought forward to now and stops being served.  With no scope this is the
    periodic sweep, and the only rows it touches are the survivors — a finding
    that was resolved and carries evidence has its TTL cleared, so its survival
    becomes a fact in the row instead of a rule re-derived on every read.

    Returns how many rows changed.  Nothing is ever removed: an expired finding
    is still there for `get()`, for an audit and for a later promotion."""
    moment = _moment(now)
    changed = 0
    try:
        with store.db() as conn:
            sql = "SELECT id, status, evidence_refs, expires_at FROM findings"
            params: List[Any] = []
            if scope:
                sql += " WHERE scope = ?"
                params.append(str(scope))
            for row in store.rows(conn.execute(sql, params)):
                survives = (str(row.get("status") or "") == "resolved"
                            and bool(store.loads_list(row.get("evidence_refs"))))
                current = str(row.get("expires_at") or "")
                if survives:
                    if current:
                        conn.execute("UPDATE findings SET expires_at = '' WHERE id = ?",
                                     (row["id"],))
                        changed += 1
                elif scope and (not current or current > moment):
                    conn.execute("UPDATE findings SET expires_at = ? WHERE id = ?",
                                 (moment, row["id"]))
                    changed += 1
    except store.ContextStoreError:
        logger.warning("blackboard unavailable for expire(scope=%s)", scope)
        return 0
    if changed:
        logger.info("blackboard expiry touched %d finding(s) in scope %s",
                    changed, scope or "*")
    return changed


def promote(finding_id: str, *, target: str) -> Dict[str, Any]:
    """Build the case for moving a finding into durable memory.  Promote nothing.

    §11.3: "No convertir automáticamente todo el blackboard en memoria global."
    So this reads one row, checks it against the rules a durable store would
    apply anyway, and hands back `{"ok", "reason", "proposal"}` for a person or
    a service with authority to act on.  It opens no other store, writes no
    row, and enqueues no job — which is the only property of this function that
    a test can meaningfully pin, and there is one that does.

    `reason` is empty when `ok` is true, and names the refusal otherwise:
    `unknown_target`, `no_such_finding`, `status_<state>`, `expired`,
    `no_evidence`, `kind_not_promotable`."""
    empty: Dict[str, Any] = {"ok": False, "reason": "", "proposal": {}}
    if target not in PROMOTION_TARGETS:
        return {**empty, "reason": "unknown_target"}
    finding = get(finding_id)
    if finding is None:
        return {**empty, "reason": "no_such_finding"}
    if finding.status in ("superseded", "withdrawn"):
        return {**empty, "reason": f"status_{finding.status}"}
    if not finding.has_evidence():
        # §22: "Un hallazgo sin evidencia no se promociona a hecho validado."
        return {**empty, "reason": "no_evidence"}
    if finding.kind not in PROMOTABLE_KINDS:
        return {**empty, "reason": "kind_not_promotable"}
    return {
        "ok": True,
        "reason": "",
        "proposal": {
            "target": target,
            "finding_id": finding.id,
            "scope": finding.scope,
            "owner": finding.owner,
            "project_id": finding.project_id,
            "author": finding.author,
            "topic": finding.topic,
            "kind": finding.kind,
            "claim": finding.claim,
            "evidence_refs": list(finding.evidence_refs),
            "tags": list(finding.tags),
            "status": finding.status,
            "created_at": finding.created_at,
            "source_ref": f"finding:{finding.id}",
            "trust_class": "agent_validated" if finding.survives_ttl() else "untrusted",
            "requires_approval": True,
        },
    }


def stats(*, scope: str = "") -> Dict[str, Any]:
    """What is on the board right now.  For the doctor and for the council UI;
    never on the turn path, and never a reason to raise."""
    out: Dict[str, Any] = {
        "scope": scope, "total": 0, "served": 0, "with_evidence": 0,
        "by_kind": {}, "by_status": {}, "authors": [], "scopes": [],
    }
    moment = _moment()
    try:
        with store.db() as conn:
            sql = "SELECT * FROM findings"
            params: List[Any] = []
            if scope:
                sql += " WHERE scope = ?"
                params.append(str(scope))
            rows = store.rows(conn.execute(sql, params))
    except store.ContextStoreError:
        logger.warning("blackboard unavailable for stats(scope=%s)", scope)
        return out
    authors: Dict[str, int] = {}
    scopes: Dict[str, int] = {}
    for row in rows:
        finding = _from_row(row)
        out["total"] += 1
        out["by_kind"][finding.kind] = out["by_kind"].get(finding.kind, 0) + 1
        out["by_status"][finding.status] = out["by_status"].get(finding.status, 0) + 1
        if finding.has_evidence():
            out["with_evidence"] += 1
        if finding.is_served(moment):
            out["served"] += 1
        if finding.author:
            authors[finding.author] = authors.get(finding.author, 0) + 1
        if finding.scope:
            scopes[finding.scope] = scopes.get(finding.scope, 0) + 1
    out["authors"] = sorted(authors)
    out["scopes"] = sorted(scopes)
    return out


__all__ = [
    "FINDING_KINDS", "FINDING_STATUSES", "EVIDENCE_REQUIRED_KINDS",
    "SUPERSEDABLE_STATUSES", "PROMOTABLE_KINDS", "PROMOTION_TARGETS",
    "DEFAULT_TTL_SECONDS", "SIMILARITY_THRESHOLD", "SCHEMA",
    "Finding", "FindingRejected",
    "post", "supersede", "withdraw", "get", "search", "as_candidates",
    "expire", "promote", "stats",
]
