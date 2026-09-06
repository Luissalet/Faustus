"""
context_engine/experiences.py — what a run proved, kept as a pattern; what it
only claimed, kept as history.

The failure this exists for is a specific one.  A worker finished a job and
wrote "fixed the OAuth state check, tests pass" in its summary, and that
sentence became the thing the next run remembered.  Nothing on disk backed it.
The next agent found the same bug and re-derived the same fix; a third one read
the same summary, picked up the approach the evidence had already
*contradicted*, and applied it again — because prose does not carry a verdict.

So this module refuses to take the verdict from the text.  `src/prove.py`
already reconciles what was observed with what was claimed and answers
`proved`, `partial`, `unproved` or `contradicted`, and
`src/contracts/changeset.py` already refuses a report whose evidence cannot
support it.  An `Experience` is what is left when a run has been through both:
a problem, a strategy, a lesson, and the deterministic verdict with the
references that earned it.

Three rules, none of them negotiable by the experience's own text:

1. **The verdict is an input, never a conclusion.**  :func:`admit` demands a
   verdict from :data:`VERDICTS` and, for anything but ``unproved``, at least
   one ``verification_refs`` entry.  A run with no ChangeSet and no equivalent
   evidence does not enter as a success — it is rejected, by field name.  §9.3
   of the plan says generated text may not raise its own confidence; this is
   where that stops being a hope.
2. **`unproved` is history, not advice.**  It is stored, it is counted by
   :func:`stats`, it can be read back by id — and :func:`search` never returns
   it, because search is the recommendation surface.  ``contradicted`` *is*
   returned, labelled ``anti_pattern``: "we tried that and the disk disagreed"
   is the most expensive sentence to have to learn twice.
3. **Retrieval signals add up; validity does not.**  The weighted sum in
   :func:`search` (§9.4) mixes problem similarity, technology, symbols, verdict
   quality, freshness and historical usefulness.  Summing is legitimate here
   precisely *because the hard validity filter already ran in `admit`*: the
   ranking can only order admissible experiences, never promote an
   inadmissible one into the packet.

Deterministic and offline.  Distilling an experience out of a run may well use
a model to write the ``lesson`` — this module never calls one.  It receives the
sentence already written and stores it next to the evidence that decides what
it is worth (§9.5).  Everything except :func:`admit` sits on a read path and
degrades to an empty answer rather than raising.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.memory import tokenize

from . import store
from .contracts import ContextCandidate, new_id

logger = logging.getLogger(__name__)


# ── closed vocabularies ────────────────────────────────────────────────────

#: What the run ended up doing.  Kept separate from the verdict on purpose: a
#: `result` is what the actor set out to report, a `verdict` is what the
#: evidence supports, and collapsing them is the bug this module exists for.
RESULTS: Tuple[str, ...] = ("success", "partial", "failure", "abandoned")

#: `src.prove.VERDICTS`, restated rather than imported: this tuple is part of
#: the store's schema (it is written into a column) and must not change under
#: the database because a proof module grew a fifth word.  The mirror is
#: asserted by tests/test_context_engine_experiences.py.
VERDICTS: Tuple[str, ...] = ("proved", "partial", "unproved", "contradicted")

FEEDBACK_KINDS: Tuple[str, ...] = ("helpful", "harmful")

#: How a hit is offered to the agent.  `anti_pattern` is not a weaker pattern;
#: it is the opposite instruction, and a consumer that renders both the same
#: way has turned a warning into a recommendation.
ROLES: Tuple[str, ...] = ("pattern", "anti_pattern")

#: The verdict a run's result maps to when :func:`from_changeset` is given a
#: proof and no explicit result.  Deliberately a table and not an `if`: the
#: mapping is a policy decision and it should be readable in one place.
RESULT_FOR_VERDICT: Dict[str, str] = {
    "proved": "success",
    "partial": "partial",
    "contradicted": "failure",
    "unproved": "abandoned",
}

#: §9.4, with the plan's weights.  They sum to 1.0 and every one of them is a
#: *retrieval* signal — how likely this experience is to help with this query —
#: never a claim about how true it is.  Validity was settled in `admit`.
W_PROBLEM = 0.30
W_TECHNOLOGY = 0.20
W_SYMBOLS = 0.15
W_VERDICT = 0.15
W_FRESHNESS = 0.10
W_USEFULNESS = 0.10

#: How much a verdict is worth as a retrieval signal.  `unproved` is 0.0 and
#: never reached: search filters it out before scoring (rule 2).  `contradicted`
#: is not 0 because a relevant anti-pattern is worth showing.
VERDICT_QUALITY: Dict[str, float] = {
    "proved": 1.0, "partial": 0.6, "contradicted": 0.4, "unproved": 0.0,
}

#: Freshness half-life.  An experience does not become false with age, it
#: becomes less likely to still describe this repository — which is why this is
#: 10% of the score and not a filter.
FRESHNESS_HALF_LIFE_S = 45.0 * 24 * 3600.0

#: The `memory_engine` policy, borrowed rather than copied: one harmful report
#: cancels four helpful ones.  A pattern that broke three runs should be gone
#: long before twelve good ones could have saved it.  Only the constant and its
#: reason live here; the decay ladder, inversion and maturity model stay in
#: `src/memory_engine.py`, which owns them.
HARM_WEIGHT = 4.0

#: §9.4's last line: "limit to a few contrasting experiences".  At most two
#: successful patterns and one relevant failure, whatever `k` asks for.
MAX_PATTERNS = 2
MAX_ANTI_PATTERNS = 1

#: What :func:`degrade_for_revision` writes into `source_revision`.  The
#: experience is not deleted and not hidden — it is marked as describing a
#: revision of the code that no longer exists, which costs it the freshness
#: term and flags every candidate it produces as degraded.
STALE_REVISION_PREFIX = "stale:"


# ── schema ─────────────────────────────────────────────────────────────────
#
# Registered at import time so that every connection opened afterwards has the
# tables, per the contract in `store.register_schema`.  `owner` and
# `project_id` are indexed together because every query in this module filters
# on both and a table scan per turn is how a context engine becomes the reason
# the turn is slow.

store.register_schema("experiences", (
    """
    CREATE TABLE IF NOT EXISTS experiences (
        id                TEXT PRIMARY KEY,
        owner             TEXT NOT NULL DEFAULT '',
        project_id        TEXT NOT NULL DEFAULT '',
        intent            TEXT NOT NULL DEFAULT '',
        technologies      TEXT NOT NULL DEFAULT '[]',
        concepts          TEXT NOT NULL DEFAULT '[]',
        problem           TEXT NOT NULL DEFAULT '',
        preconditions     TEXT NOT NULL DEFAULT '[]',
        strategy          TEXT NOT NULL DEFAULT '[]',
        key_decisions     TEXT NOT NULL DEFAULT '[]',
        touched_symbols   TEXT NOT NULL DEFAULT '[]',
        result            TEXT NOT NULL DEFAULT 'partial',
        verdict           TEXT NOT NULL DEFAULT 'unproved',
        verification_refs TEXT NOT NULL DEFAULT '[]',
        failure_modes     TEXT NOT NULL DEFAULT '[]',
        lesson            TEXT NOT NULL DEFAULT '',
        source_run        TEXT NOT NULL DEFAULT '',
        source_revision   TEXT NOT NULL DEFAULT '',
        helpful           INTEGER NOT NULL DEFAULT 0,
        harmful           INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT NOT NULL DEFAULT '',
        updated_at        TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_experiences_scope ON experiences(owner, project_id)",
    "CREATE INDEX IF NOT EXISTS ix_experiences_verdict ON experiences(verdict)",
    "CREATE INDEX IF NOT EXISTS ix_experiences_intent ON experiences(intent)",
    # Feedback is kept as events, not only as a counter.  The counters answer
    # "how useful was this"; the events answer "says who", which is the only
    # version of the question worth acting on.
    """
    CREATE TABLE IF NOT EXISTS experience_feedback (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        experience_id TEXT NOT NULL,
        kind          TEXT NOT NULL,
        weight        REAL NOT NULL DEFAULT 1.0,
        ref           TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_experience_feedback_exp "
    "ON experience_feedback(experience_id)",
))

_COLUMNS: Tuple[str, ...] = (
    "id", "owner", "project_id", "intent", "technologies", "concepts", "problem",
    "preconditions", "strategy", "key_decisions", "touched_symbols", "result",
    "verdict", "verification_refs", "failure_modes", "lesson", "source_run",
    "source_revision", "helpful", "harmful", "created_at", "updated_at",
)
_LIST_FIELDS: Tuple[str, ...] = (
    "technologies", "concepts", "preconditions", "strategy", "key_decisions",
    "touched_symbols", "verification_refs", "failure_modes",
)


# ── normalisation (total: a normaliser never raises) ───────────────────────

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_MAX_TEXT = 8000
_MAX_ITEMS = 64
_MAX_ITEM = 512


def _text(value: Any, *, limit: int = _MAX_TEXT) -> str:
    try:
        out = "" if value is None else str(value)
    except Exception:  # noqa: BLE001 - a normaliser never raises
        return ""
    out = out.strip()
    return out[:limit]


def _tuple(value: Any, *, limit: int = _MAX_ITEMS) -> Tuple[str, ...]:
    """A tuple of short strings, de-duplicated, order preserved.

    A bare string is one item, not a sequence of characters — passing
    ``technologies="python"`` is a mistake a caller makes once and would
    otherwise be stored as twenty-six technologies."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        value = [value]
    if not isinstance(value, Iterable):
        return ()
    out: List[str] = []
    for raw in value:
        item = _text(raw, limit=_MAX_ITEM)
        if item and item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return tuple(out)


def _whole(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _tokens(value: Any) -> set:
    """Words worth matching on, identifiers split into their parts.

    `src.memory.tokenize` is the house tokenizer (`memory_engine` scores with
    it too); it splits on whitespace and trims punctuation, which is right for
    prose and useless for `refresh_oauth_token`.  So each token is split again
    on non-identifier characters and on underscores, and both the whole
    identifier and its parts are kept — a query for "oauth" has to reach an
    experience that touched `refresh_oauth_token`."""
    out: set = set()
    for raw in tokenize(_text(value).lower()):
        for piece in _WORD_RE.findall(raw):
            if len(piece) < 2:
                continue
            out.add(piece)
            for part in piece.split("_"):
                if len(part) >= 2:
                    out.add(part)
    return out


def _coverage(query: set, target: set) -> float:
    """How much of the query the target accounts for, in [0, 1].

    Coverage rather than Jaccard: a long, rich experience should not be
    punished for containing more words than the question did."""
    if not query or not target:
        return 0.0
    return min(1.0, len(query & target) / float(len(query)))


def _same_path(a: str, b: str) -> bool:
    """`src.prove._same_path`, applied to symbols and files alike: equal, or one
    is the other's tail.  A changed file reported as `src/auth/github.py` has to
    match an experience that recorded `auth/github.py`."""
    x = a.replace("\\", "/").strip().strip("/").lower()
    y = b.replace("\\", "/").strip().strip("/").lower()
    if not x or not y:
        return False
    return x == y or x.endswith("/" + y) or y.endswith("/" + x)


# ── the unit ───────────────────────────────────────────────────────────────

class ExperienceRejected(ValueError):
    """§9.3 said no, and says which field.

    Carries `field` and `reason` separately so a caller can report the failure
    without regex-ing the sentence: the whole point of rejecting by name is
    that the writer can fix exactly one thing and try again."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = str(field)
        self.reason = str(reason)
        super().__init__(f"experiences.{self.field}: {self.reason}")


@dataclass(frozen=True)
class Experience:
    """§9.2.  One way a problem was approached, and what the evidence said.

    Frozen because an experience is a record of something that already
    happened: the only fields that legitimately move afterwards are the
    feedback counters and `source_revision`, and both are written through
    functions in this module that build a new value."""

    id: str = ""
    owner: str = ""
    project_id: str = ""
    intent: str = ""
    technologies: Tuple[str, ...] = ()
    concepts: Tuple[str, ...] = ()
    problem: str = ""
    preconditions: Tuple[str, ...] = ()
    strategy: Tuple[str, ...] = ()
    key_decisions: Tuple[str, ...] = ()
    touched_symbols: Tuple[str, ...] = ()
    result: str = "partial"
    verdict: str = "unproved"
    verification_refs: Tuple[str, ...] = ()
    failure_modes: Tuple[str, ...] = ()
    lesson: str = ""
    source_run: str = ""
    source_revision: str = ""
    helpful: int = 0
    harmful: int = 0
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def parse(cls, raw: Any) -> "Experience":
        """Normalise anything into the shape, without judging it.

        Tolerant on purpose — judgement belongs to :func:`admit`, and a parser
        that also rejected would make "read a row back from SQLite" and "accept
        a new experience" the same operation with two different answers."""
        data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        return cls(
            id=_text(data.get("id"), limit=128) or new_id("exp"),
            owner=_text(data.get("owner"), limit=256),
            project_id=_text(data.get("project_id"), limit=128),
            intent=_text(data.get("intent"), limit=64),
            technologies=_tuple(data.get("technologies")),
            concepts=_tuple(data.get("concepts")),
            problem=_text(data.get("problem")),
            preconditions=_tuple(data.get("preconditions")),
            strategy=_tuple(data.get("strategy")),
            key_decisions=_tuple(data.get("key_decisions")),
            touched_symbols=_tuple(data.get("touched_symbols"), limit=256),
            result=_text(data.get("result"), limit=32),
            verdict=_text(data.get("verdict"), limit=32),
            verification_refs=_tuple(data.get("verification_refs")),
            failure_modes=_tuple(data.get("failure_modes")),
            lesson=_text(data.get("lesson")),
            source_run=_text(data.get("source_run"), limit=128),
            source_revision=_text(data.get("source_revision"), limit=256),
            helpful=_whole(data.get("helpful")),
            harmful=_whole(data.get("harmful")),
            created_at=_text(data.get("created_at"), limit=64),
            updated_at=_text(data.get("updated_at"), limit=64),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "owner": self.owner, "project_id": self.project_id,
            "intent": self.intent,
            "technologies": list(self.technologies), "concepts": list(self.concepts),
            "problem": self.problem,
            "preconditions": list(self.preconditions), "strategy": list(self.strategy),
            "key_decisions": list(self.key_decisions),
            "touched_symbols": list(self.touched_symbols),
            "result": self.result, "verdict": self.verdict,
            "verification_refs": list(self.verification_refs),
            "failure_modes": list(self.failure_modes), "lesson": self.lesson,
            "source_run": self.source_run, "source_revision": self.source_revision,
            "helpful": self.helpful, "harmful": self.harmful,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    # ── derived views ──────────────────────────────────────────────────────

    def role(self) -> str:
        """`pattern` or `anti_pattern`.

        A contradicted verdict is an anti-pattern by definition, and so is a
        run that ended in failure or was abandoned even when its evidence is
        solid: "this is what we tried and it did not work" is exactly the thing
        that must not be handed over as a recipe."""
        if self.verdict == "contradicted" or self.result in ("failure", "abandoned"):
            return "anti_pattern"
        return "pattern"

    def stale(self) -> bool:
        return self.source_revision.startswith(STALE_REVISION_PREFIX)

    def usefulness(self) -> float:
        """Historical usefulness in [0, 1], 0.5 with no feedback at all.

        Harm outweighs help four to one (:data:`HARM_WEIGHT`); the result is
        squashed rather than clipped so that the tenth helpful report still
        moves the number a little and no amount of them reaches 1.0."""
        net = float(self.helpful) - HARM_WEIGHT * float(self.harmful)
        return 0.5 + 0.5 * (net / (1.0 + abs(net)))


# ── persistence ────────────────────────────────────────────────────────────

def _from_row(row: Mapping[str, Any]) -> Experience:
    data = dict(row)
    for field in _LIST_FIELDS:
        data[field] = store.loads_list(data.get(field))
    return Experience.parse(data)


def _to_row(exp: Experience) -> Dict[str, Any]:
    data = exp.to_dict()
    for field in _LIST_FIELDS:
        data[field] = store.dumps(data.get(field) or [])
    return data


def _write(exp: Experience) -> Experience:
    row = _to_row(exp)
    columns = ", ".join(_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS if c != "id")
    with store.db() as conn:
        conn.execute(
            f"INSERT INTO experiences ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            row,
        )
    return exp


# ── admission (§9.3) ───────────────────────────────────────────────────────

def admit(fields: Mapping[str, Any]) -> Experience:
    """Apply §9.3 and store the experience, or refuse it by name.

    This is the *only* write path, and it is the only function here that
    raises.  The rules, in the order a caller trips over them:

    * `problem` has to say what the run was about.  An experience nobody can
      match a future task against is a row, not a memory.
    * `verdict` has to be one of :data:`VERDICTS`.  It comes from `prove`, and
      a caller that has no proof has to say `unproved` rather than leave it
      blank and let a default decide.
    * anything other than `unproved` needs at least one `verification_refs`
      entry — a ChangeSet id, a checkpoint sha, a proof identity, a test
      command.  This is the sentence the plan repeats: a verdict without a
      reference is the text grading its own homework.
    * a `success` needs a `proved` verdict *and* those references.  A run that
      wants to be remembered as a success and can only show a `partial` is
      stored as a `partial`; the caller is told which field to change.

    Everything else — an `unproved` attempt, a `contradicted` approach, an
    abandoned run with a real finding — is admitted, because §9.3 keeps them
    deliberately.  What they may be used *for* is decided in :func:`search`."""
    supplied: Mapping[str, Any] = fields if isinstance(fields, Mapping) else {}
    exp = Experience.parse(supplied)

    if not exp.problem:
        raise ExperienceRejected(
            "problem", "an experience with no problem statement cannot be matched "
                       "against a future task; say what the run was about")
    if exp.verdict not in VERDICTS:
        raise ExperienceRejected(
            "verdict", f"expected one of {', '.join(VERDICTS)} — taken from "
                       f"src.prove, never written by hand; got {exp.verdict!r}")
    if exp.result not in RESULTS:
        raise ExperienceRejected(
            "result", f"expected one of {', '.join(RESULTS)}; got {exp.result!r}")
    if exp.verdict != "unproved" and not exp.verification_refs:
        raise ExperienceRejected(
            "verification_refs", f"a verdict of {exp.verdict!r} claims something was "
                                 "checked, so it has to name the check: a changeset "
                                 "id, a checkpoint sha, a proof identity or the "
                                 "command that ran")
    if exp.result == "success":
        # The rule the plan repeats: no ChangeSet and no equivalent evidence
        # means it does not enter as a success.  Naming the field that is
        # missing matters — "rejected" without it just gets retried verbatim.
        if not exp.verification_refs:
            raise ExperienceRejected(
                "verification_refs", "a success with nothing to verify it is a claim, "
                                     "not an experience; attach the changeset or "
                                     "record the result as 'partial'")
        if exp.verdict != "proved":
            raise ExperienceRejected(
                "verdict", f"a result of 'success' needs a 'proved' verdict; "
                           f"{exp.verdict!r} does not support it — record the result "
                           "as 'partial' or 'failure' instead")

    stamp = store.now_iso()
    overrides: Dict[str, Any] = {"created_at": exp.created_at or stamp,
                                 "updated_at": stamp}
    existing = get(exp.id)
    if existing is not None:
        # Re-admitting an id keeps what the store learned about it.  The
        # feedback counters are outcomes, not part of the submitted record, and
        # a second extraction of the same run must not silently erase the three
        # times somebody said this experience made things worse.
        overrides["created_at"] = existing.created_at or overrides["created_at"]
        if "helpful" not in supplied:
            overrides["helpful"] = existing.helpful
        if "harmful" not in supplied:
            overrides["harmful"] = existing.harmful
    return _write(Experience.parse({**exp.to_dict(), **overrides}))


# ── distilling a run ───────────────────────────────────────────────────────

def _changeset_of(raw: Any) -> Any:
    """A `ChangeSet`, whether the caller had the object or the dict.

    Imported lazily: this module is on the retrieval path of every turn and
    `src.contracts` is only needed by the one function that reads a run."""
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        from src.contracts.changeset import ChangeSet
        return ChangeSet.parse(raw)
    return raw


def _evidence_refs(changeset: Any, proof: Mapping[str, Any]) -> Tuple[str, ...]:
    """The references that earn a verdict, built from deterministic sources.

    Every entry names something a person can go and fetch: the change set that
    holds the diff by reference, the checkpoint the diff is against, the proof
    packet's identity hash, and the command that was actually run.  None of it
    is written by a model."""
    refs: List[str] = []

    def add(value: str) -> None:
        if value and value not in refs:
            refs.append(value)

    if changeset is not None:
        changeset_id = _text(getattr(changeset, "id", ""), limit=64)
        checkpoint = _text(getattr(changeset, "checkpoint", ""), limit=64)
        if changeset_id:
            add(f"changeset:{changeset_id}")
        if checkpoint:
            add(f"checkpoint:{checkpoint}")
        verification = getattr(changeset, "verification", None)
        if verification is not None and getattr(verification, "ran", False):
            command = _text(getattr(verification, "command", ""), limit=_MAX_ITEM)
            mode = _text(getattr(verification, "mode", ""), limit=32)
            add(f"verification:{command or mode or 'ran'}")
        for artifact_id in tuple(getattr(changeset, "artifact_ids", ()) or ())[:8]:
            add(f"artifact:{_text(artifact_id, limit=64)}")
    identity = _text((proof or {}).get("identity"), limit=128)
    if identity:
        add(f"proof:{identity}")
    return tuple(refs)


def from_changeset(changeset: Any, proof: Mapping[str, Any], *,
                   owner: str = "", project_id: str = "", lesson: str = "",
                   intent: str = "", problem: str = "",
                   technologies: Sequence[str] = (), concepts: Sequence[str] = (),
                   preconditions: Sequence[str] = (), strategy: Sequence[str] = (),
                   key_decisions: Sequence[str] = (),
                   touched_symbols: Sequence[str] = (),
                   failure_modes: Sequence[str] = (), result: str = "",
                   source_run: str = "", source_revision: str = "",
                   experience_id: str = "") -> Experience:
    """Distil one run into an experience and admit it.

    `changeset` is a `src.contracts.changeset.ChangeSet` (or its dict) and
    `proof` is a `src.prove.prove()` packet.  The narrative fields — the
    problem, the strategy, the lesson — are the caller's, because writing them
    well may take a model and this module never calls one (§9 is explicit:
    extraction may be generated, the verdict may not).  Everything that decides
    how much the experience is *worth* is read out of the two evidence objects:

    * `verdict` comes from the proof and from nowhere else;
    * `result` defaults through :data:`RESULT_FOR_VERDICT` rather than from any
      summary text, and an explicit `result` still has to survive `admit`;
    * `verification_refs` are built from the change set and the proof identity;
    * `touched_symbols` falls back to the paths the change set observed, which
      is what :func:`degrade_for_revision` later matches against.

    Raises `ExperienceRejected` exactly where :func:`admit` would."""
    cs = _changeset_of(changeset)
    proof = proof if isinstance(proof, Mapping) else {}
    verdict = _text(proof.get("verdict"), limit=32)
    files = getattr(cs, "files", None)
    observed = tuple(getattr(files, "paths", ()) or ()) if files is not None else ()

    return admit({
        "id": experience_id or new_id("exp"),
        "owner": owner or _text(getattr(cs, "owner", ""), limit=256),
        "project_id": project_id or _text(getattr(cs, "project_id", ""), limit=128),
        "intent": intent or _text(getattr(cs, "intent", ""), limit=64),
        "technologies": technologies,
        "concepts": concepts,
        "problem": problem or _text(getattr(cs, "title", "") or getattr(cs, "plan", "")),
        "preconditions": preconditions,
        "strategy": strategy,
        "key_decisions": key_decisions,
        "touched_symbols": touched_symbols or observed,
        "result": result or RESULT_FOR_VERDICT.get(verdict, "partial"),
        "verdict": verdict,
        "verification_refs": _evidence_refs(cs, proof),
        "failure_modes": failure_modes,
        "lesson": lesson,
        "source_run": source_run or _text(getattr(cs, "run_id", ""), limit=128),
        "source_revision": source_revision or _text(getattr(cs, "checkpoint", ""), limit=256),
    })


# ── reading ────────────────────────────────────────────────────────────────

def get(exp_id: str) -> Optional[Experience]:
    """One experience by id, `unproved` ones included — this is the history
    door, not the recommendation door."""
    key = _text(exp_id, limit=128)
    if not key:
        return None
    try:
        with store.db() as conn:
            row = conn.execute(
                "SELECT * FROM experiences WHERE id = ?", (key,)).fetchone()
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("experiences.get(%s) failed: %s", key, exc)
        return None
    return _from_row(row) if row else None


def delete(exp_id: str) -> bool:
    key = _text(exp_id, limit=128)
    if not key:
        return False
    try:
        with store.db() as conn:
            cursor = conn.execute("DELETE FROM experiences WHERE id = ?", (key,))
            conn.execute("DELETE FROM experience_feedback WHERE experience_id = ?", (key,))
            return cursor.rowcount > 0
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("experiences.delete(%s) failed: %s", key, exc)
        return False


def _score(exp: Experience, *, query_tokens: set, technologies: Sequence[str],
           now: Optional[datetime]) -> Dict[str, float]:
    """§9.4's six signals, each in [0, 1], reported separately.

    Returned as a bag rather than a number so the packet manifest can say why
    an experience was chosen — `ContextItem.scores` exists for exactly this and
    a single opaque float is not auditable six months later."""
    problem = _coverage(query_tokens, _tokens(" ".join(
        (exp.problem, exp.lesson, " ".join(exp.concepts), " ".join(exp.strategy)))))

    exp_tech = {t.lower() for t in exp.technologies}
    wanted = {_text(t, limit=64).lower() for t in (technologies or ()) if _text(t, limit=64)}
    if wanted:
        technology = len(wanted & exp_tech) / float(len(wanted))
    else:
        # No technology filter: the query text is the only evidence of what
        # stack the caller is in, so match it against the recorded ones.
        technology = _coverage(query_tokens, _tokens(" ".join(exp.technologies))) if exp_tech else 0.0

    symbols = _coverage(query_tokens, _tokens(" ".join(exp.touched_symbols)))

    freshness = 0.0 if exp.stale() else 1.0
    if freshness:
        age = store.age_seconds(exp.updated_at or exp.created_at, now=now)
        # An unreadable timestamp is not "brand new": it scores like an
        # experience one half-life old rather than a fresh one.
        freshness = 0.5 if age is None else 0.5 ** (age / FRESHNESS_HALF_LIFE_S)

    return {
        "problem": round(problem, 4),
        "technology": round(technology, 4),
        "symbols": round(symbols, 4),
        "verdict": VERDICT_QUALITY.get(exp.verdict, 0.0),
        "freshness": round(freshness, 4),
        "usefulness": round(exp.usefulness(), 4),
    }


def _total(scores: Mapping[str, float]) -> float:
    return round(
        W_PROBLEM * scores.get("problem", 0.0)
        + W_TECHNOLOGY * scores.get("technology", 0.0)
        + W_SYMBOLS * scores.get("symbols", 0.0)
        + W_VERDICT * scores.get("verdict", 0.0)
        + W_FRESHNESS * scores.get("freshness", 0.0)
        + W_USEFULNESS * scores.get("usefulness", 0.0),
        6,
    )


def search(query: str, *, owner: str = "", project_id: str = "", intent: str = "",
           technologies: Sequence[str] = (), k: int = 5,
           now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """A few contrasting experiences for this task, ranked by §9.4.

    Two things this deliberately does not do.  It does not return `unproved`
    experiences: they are kept as history and search is where a pattern gets
    recommended, so they are filtered in SQL before anything is scored.  And it
    does not fill `k`: the answer is capped at :data:`MAX_PATTERNS` successful
    patterns plus :data:`MAX_ANTI_PATTERNS` relevant failure, so `k=5` normally
    returns three or fewer.  Fewer is the correct answer — five near-identical
    experiences are a budget spent on repetition.

    Each hit is the experience's `to_dict()` plus `role`, `score`, `scores` and
    `stale`, so a consumer can rank, explain or render it without a second
    query.  Never raises: an unreadable store costs the section, not the turn."""
    tokens = _tokens(query)
    where, params = store.scope_clause(owner, project_id)
    sql = ("SELECT * FROM experiences WHERE " + where
           + " AND verdict != 'unproved'")
    wanted_intent = _text(intent, limit=64)
    if wanted_intent:
        sql += " AND intent = ?"
        params = list(params) + [wanted_intent]
    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(sql, params))
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("experiences.search failed: %s", exc)
        return []

    scored: List[Tuple[float, Experience, Dict[str, float]]] = []
    for row in rows:
        exp = _from_row(row)
        scores = _score(exp, query_tokens=tokens, technologies=technologies, now=now)
        scored.append((_total(scores), exp, scores))
    # Ties broken by id so that two runs against the same store agree on the
    # order; an unstable ranking makes a packet impossible to diff.
    scored.sort(key=lambda item: (-item[0], item[1].id))

    patterns: List[Tuple[float, Experience, Dict[str, float]]] = []
    anti: List[Tuple[float, Experience, Dict[str, float]]] = []
    for entry in scored:
        if entry[1].role() == "anti_pattern":
            if len(anti) < MAX_ANTI_PATTERNS:
                anti.append(entry)
        elif len(patterns) < MAX_PATTERNS:
            patterns.append(entry)
        if len(patterns) >= MAX_PATTERNS and len(anti) >= MAX_ANTI_PATTERNS:
            break

    chosen = sorted(patterns + anti, key=lambda item: (-item[0], item[1].id))
    try:
        limit = int(k)
    except (TypeError, ValueError):
        limit = MAX_PATTERNS + MAX_ANTI_PATTERNS
    if limit <= 0:
        return []
    return [
        {**exp.to_dict(), "role": exp.role(), "score": total,
         "scores": dict(scores), "stale": exp.stale()}
        for total, exp, scores in chosen[:limit]
    ]


# ── handing them to the compiler ───────────────────────────────────────────

#: Trust ceiling per verdict, mapped onto `contracts.TRUST_CLASSES`.  `proved`
#: is the class that exists for exactly this case ("a `prove` verdict backs
#: it"); a `partial` or a `contradicted` had real evidence too, but not enough
#: of it to carry that word.
_TRUST_FOR_VERDICT: Dict[str, str] = {
    "proved": "proved",
    "partial": "agent_validated",
    "contradicted": "agent_validated",
    "unproved": "agent_assertion",
}

#: Authority per role, and the reason it is not symmetric: when a pattern and
#: an anti-pattern contradict each other, the validated pattern is the one
#: presented as current (`validated_experience` 30 > `agent_claim` 20).  A
#: warning drawn from a failed attempt is worth showing and must never
#: overrule an approach the evidence supports.
_AUTHORITY_FOR_ROLE: Dict[str, str] = {
    "pattern": "validated_experience",
    "anti_pattern": "agent_claim",
}

_ANTI_PATTERN_HEADER = ("ANTI-PATTERN — this approach was contradicted by the "
                        "evidence; do not repeat it:")


def _render(exp: Experience, role: str) -> str:
    """The experience as the model should read it: what happened, what was
    decided, what it cost.  Short on purpose — an experience is a pointer to a
    run, and `verification_refs` is how the run is opened."""
    lines: List[str] = []
    if role == "anti_pattern":
        lines.append(_ANTI_PATTERN_HEADER)
    lines.append(f"Problem: {exp.problem}")
    if exp.preconditions:
        lines.append("Preconditions: " + "; ".join(exp.preconditions))
    if exp.strategy:
        lines.append("Strategy:")
        lines.extend(f"  - {step}" for step in exp.strategy)
    if exp.key_decisions:
        lines.append("Decisions: " + ", ".join(exp.key_decisions))
    if exp.touched_symbols:
        lines.append("Touched: " + ", ".join(exp.touched_symbols[:12]))
    if exp.failure_modes:
        lines.append("Failure modes:")
        lines.extend(f"  - {mode}" for mode in exp.failure_modes)
    if exp.lesson:
        lines.append(f"Lesson: {exp.lesson}")
    lines.append(f"Outcome: {exp.result} / {exp.verdict}")
    if exp.verification_refs:
        lines.append("Evidence: " + ", ".join(exp.verification_refs))
    if exp.stale():
        lines.append("Note: the code this experience touched has changed since; "
                     "confirm against the current files before reusing it.")
    return "\n".join(lines)


def as_candidates(hits: Iterable[Any]) -> List[ContextCandidate]:
    """Search hits as `ContextCandidate`s for the compiler.

    The role travels twice on purpose: in `meta["role"]`, where a renderer can
    read it, and in `authority`, where the contradiction resolver can.  A
    consumer that only looked at one of the two would either print an
    anti-pattern as advice or let it outrank a validated one."""
    out: List[ContextCandidate] = []
    for hit in hits or ():
        try:
            data = hit.to_dict() if isinstance(hit, Experience) else dict(hit)
            exp = Experience.parse(data)
            role = _text(data.get("role"), limit=32) or exp.role()
            if role not in ROLES:
                role = exp.role()
            scores = {str(name): float(value)
                      for name, value in dict(data.get("scores") or {}).items()
                      if isinstance(value, (int, float)) and not isinstance(value, bool)}
            if isinstance(data.get("score"), (int, float)) and not isinstance(data.get("score"), bool):
                scores["total"] = float(data["score"])
            out.append(ContextCandidate(
                candidate_id=new_id("ctxcand"),
                source_type="experience",
                source_ref=f"experience:{exp.id}",
                title=(f"{role}: {exp.intent or 'experience'} — {exp.problem}")[:512],
                body=_render(exp, role),
                section="past_experiences",
                lanes=("lexical",),
                scores=scores,
                trust_class=_TRUST_FOR_VERDICT.get(exp.verdict, "agent_assertion"),
                authority=_AUTHORITY_FOR_ROLE.get(role, "agent_claim"),
                source_revision=exp.source_revision,
                observed_at=exp.updated_at or exp.created_at,
                owner=exp.owner,
                project_id=exp.project_id,
                degraded=exp.stale(),
                meta={
                    "role": role, "verdict": exp.verdict, "result": exp.result,
                    "intent": exp.intent, "technologies": list(exp.technologies),
                    "concepts": list(exp.concepts),
                    "touched_symbols": list(exp.touched_symbols[:32]),
                    "verification_refs": list(exp.verification_refs),
                    "helpful": exp.helpful, "harmful": exp.harmful,
                    "source_run": exp.source_run, "stale": exp.stale(),
                },
            ))
        except Exception as exc:  # noqa: BLE001 - one bad hit costs one candidate
            logger.warning("experiences.as_candidates skipped a hit: %s", exc)
    return out


# ── feedback (§9.5) ────────────────────────────────────────────────────────

def feedback(exp_id: str, kind: str, *, ref: str = "",
             weight: float = 1.0) -> Optional[Experience]:
    """Record that an experience helped or hurt, with what says so.

    The counters are what :meth:`Experience.usefulness` reads; the event row is
    what makes the counter answerable ("helpful according to whom?").  `ref`
    should point at something durable — a packet id, a changeset id, a proof
    identity.  Returns the updated experience, or None if there is nothing to
    update; never raises, because this is called from the tail of a turn where
    an exception would cost the answer that was already produced."""
    key = _text(exp_id, limit=128)
    event = _text(kind, limit=32).lower()
    if not key or event not in FEEDBACK_KINDS:
        return None
    try:
        amount = max(0.0, float(weight))
    except (TypeError, ValueError):
        amount = 1.0
    if amount <= 0.0:
        return None
    column = "helpful" if event == "helpful" else "harmful"
    stamp = store.now_iso()
    try:
        with store.db() as conn:
            cursor = conn.execute(
                f"UPDATE experiences SET {column} = {column} + ?, updated_at = ? "
                "WHERE id = ?",
                (int(round(amount)) or 1, stamp, key))
            if not cursor.rowcount:
                return None
            conn.execute(
                "INSERT INTO experience_feedback "
                "(experience_id, kind, weight, ref, created_at) VALUES (?, ?, ?, ?, ?)",
                (key, event, amount, _text(ref, limit=_MAX_ITEM), stamp))
            row = conn.execute(
                "SELECT * FROM experiences WHERE id = ?", (key,)).fetchone()
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("experiences.feedback(%s, %s) failed: %s", key, event, exc)
        return None
    return _from_row(row) if row else None


# ── going stale (§10.4 seen from this side) ────────────────────────────────

def degrade_for_revision(project_id: str, *, changed_symbols: Sequence[str] = (),
                         changed_files: Sequence[str] = ()) -> int:
    """Mark the experiences whose code has moved under them.

    Deliberately *not* a delete.  An experience that touched a file somebody
    has since rewritten is still the record of how a problem was approached and
    why; what it has stopped being is a description of the current repository.
    So `source_revision` is prefixed with :data:`STALE_REVISION_PREFIX`, which
    costs the experience its freshness term in :func:`search` and makes every
    candidate it produces `degraded=True` — visible, ranked lower, and honest
    about why.  Returns how many were newly marked; already-stale rows are not
    counted again, so calling this on every commit is idempotent."""
    changed = [_text(value, limit=_MAX_ITEM)
               for value in list(changed_symbols or ()) + list(changed_files or ())]
    changed = [value for value in changed if value]
    if not changed:
        return 0
    scope = _text(project_id, limit=128)
    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(
                "SELECT id, touched_symbols, source_revision FROM experiences "
                "WHERE project_id = ?", (scope,)))
            stamp = store.now_iso()
            marked = 0
            for row in rows:
                revision = _text(row.get("source_revision"), limit=256)
                if revision.startswith(STALE_REVISION_PREFIX):
                    continue
                touched = [str(value) for value in store.loads_list(row.get("touched_symbols"))]
                if not any(_same_path(one, other) for one in touched for other in changed):
                    continue
                conn.execute(
                    "UPDATE experiences SET source_revision = ?, updated_at = ? "
                    "WHERE id = ?",
                    (f"{STALE_REVISION_PREFIX}{revision}", stamp, row.get("id")))
                marked += 1
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("experiences.degrade_for_revision(%s) failed: %s", scope, exc)
        return 0
    return marked


# ── diagnostics ────────────────────────────────────────────────────────────

def stats(*, owner: str = "", project_id: str = "") -> Dict[str, Any]:
    """What the store holds, in the terms §9.3 cares about.

    `unproved` and `stale` are reported rather than hidden: the interesting
    number in a healthy install is how many runs could not be proved, because
    that is the one that says whether the verification path is working."""
    empty: Dict[str, Any] = {
        "total": 0, "by_verdict": {v: 0 for v in VERDICTS},
        "by_result": {r: 0 for r in RESULTS},
        "by_role": {role: 0 for role in ROLES},
        "recommendable": 0, "stale": 0, "helpful": 0, "harmful": 0,
    }
    where, params = store.scope_clause(owner, project_id)
    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(
                "SELECT verdict, result, source_revision, helpful, harmful "
                "FROM experiences WHERE " + where, params))
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("experiences.stats failed: %s", exc)
        return empty
    out = dict(empty)
    out["by_verdict"] = dict(empty["by_verdict"])
    out["by_result"] = dict(empty["by_result"])
    out["by_role"] = dict(empty["by_role"])
    for row in rows:
        exp = Experience.parse(row)
        out["total"] += 1
        if exp.verdict in out["by_verdict"]:
            out["by_verdict"][exp.verdict] += 1
        if exp.result in out["by_result"]:
            out["by_result"][exp.result] += 1
        out["by_role"][exp.role()] += 1
        if exp.verdict != "unproved":
            out["recommendable"] += 1
        if exp.stale():
            out["stale"] += 1
        out["helpful"] += exp.helpful
        out["harmful"] += exp.harmful
    return out


__all__ = [
    "RESULTS", "VERDICTS", "FEEDBACK_KINDS", "ROLES", "RESULT_FOR_VERDICT",
    "VERDICT_QUALITY", "STALE_REVISION_PREFIX", "HARM_WEIGHT",
    "MAX_PATTERNS", "MAX_ANTI_PATTERNS", "FRESHNESS_HALF_LIFE_S",
    "W_PROBLEM", "W_TECHNOLOGY", "W_SYMBOLS", "W_VERDICT", "W_FRESHNESS",
    "W_USEFULNESS",
    "Experience", "ExperienceRejected",
    "admit", "from_changeset", "get", "search", "as_candidates", "feedback",
    "degrade_for_revision", "stats", "delete",
]
