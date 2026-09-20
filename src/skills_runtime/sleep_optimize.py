"""src/skills_runtime/sleep_optimize.py — the skill sleep pass.

Offline, on demand or scheduled: mine recent sessions for evidence that a
skill worked or did not, hand that evidence plus the skill's current
SKILL.md to a local model, and get back ONE proposed revision. The proposal
is never applied automatically — it is stored as `pending` and must go
through the same governance gate every other skill promotion uses
(`src.skill_governance.validate_promotion`) before its text reaches disk.

Where "the skill was active" comes from
----------------------------------------
Skill activation is not recorded as its own event anywhere in this repo.
What IS recorded, in every session's persisted messages
(`core.database.ChatMessage`), is the tool call that reads or edits a
skill: the model calls `manage_skills(action="view"|"edit"|"patch",
name=...)` (`src/tools/system.py::do_manage_skills`), and that call — tool
name, its JSON arguments, and its result text — is written into the
assistant message's `metadata.tool_events` list
(`routes/chat_routes.py`/`src/agent_loop.py`, shape `{"round", "tool",
"command", "output", "exit_code"}`). A `view`/`patch` on a given skill name
IS the activation signal this module mines: it is the one place "this skill
was consulted in this turn" is durably recorded, so `collect_evidence`
scans for it rather than inventing a second, unrecorded signal.

Three separate stores stay separate, on purpose
------------------------------------------------
* Evidence is read straight out of `core.database` (`Session`,
  `ChatMessage`) — no new table, no sidecar cache that could drift from the
  real conversation history.
* A proposal is its own small JSON store under `DATA_DIR/skill_proposals/`
  (`skill_sources.py`'s git-backed versioning only applies to a skill that
  was `install()`-ed from a git remote through that module; most skills on
  a personal box never were, so this module cannot honestly lean on it for
  every skill). `record_version`/`is_obsolete` from `src.skill_governance`
  are still the gate a proposal's *approval* must pass, and this module
  keeps its own lightweight version history + content snapshots so an
  approved proposal can be rolled back byte-for-byte — the same idea
  `skill_import_review.py`'s snapshot directory uses, at a smaller scope.
* Applying an approved proposal writes through
  `services.memory.skills.SkillsManager.update_skill`, the exact same path
  `do_manage_skills(action="edit")` uses — never a raw file write that
  could skip frontmatter/rename handling.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from difflib import unified_diff
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.atomic_io import atomic_write_json, atomic_write_text
from src import security_scan
from src import skill_governance
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

PROPOSALS_ROOT = os.path.join(DATA_DIR, "skill_proposals")
PROPOSALS_FILE = os.path.join(PROPOSALS_ROOT, "proposals.json")
SNAPSHOT_DIR = os.path.join(PROPOSALS_ROOT, "snapshots")
VERSIONS_FILE = os.path.join(PROPOSALS_ROOT, "versions.json")

#: A revised SKILL.md this far over the ORIGINAL's size is refused outright
#: — a local model padding/duplicating content rather than editing it is a
#: model failure, not a real improvement, and this catches it before a
#: human even sees the diff.
MAX_GROWTH_RATIO = 2.5
# Small skills need room: a few added steps can easily triple a 400-byte file.
MIN_GROWTH_ALLOWANCE_BYTES = 2000
#: Absolute ceiling regardless of the original's size.
MAX_SKILL_MD_BYTES = 60_000

__all__ = [
    "PROPOSALS_ROOT", "PROPOSALS_FILE", "SNAPSHOT_DIR", "VERSIONS_FILE",
    "EvidenceItem", "Proposal", "SleepPassError",
    "classify_reaction", "collect_evidence", "propose",
    "load_proposals", "get_proposal", "save_proposal",
    "list_proposals_for", "approve_proposal", "reject_proposal",
    "rollback_proposal",
]


class SleepPassError(Exception):
    """Carries a stable `error_class`, same shape as `SkillReviewError`."""

    def __init__(self, error_class: str, message: str):
        super().__init__(message)
        self.error_class = error_class
        self.message = message


# ── reaction classification (deterministic, no model call) ─────────────────

#: Phrases whose presence in the user's NEXT message after a skill was
#: consulted is a strong signal the skill's output was rejected, undone, or
#: retried — i.e. it did not work. Matched as case-insensitive substrings
#: against the lower-cased message, deliberately loose (a false positive
#: here only adds one extra evidence item a human proposal-reviewer can
#: discount; a false negative silently hides real failure evidence, the
#: worse of the two).
_NEGATIVE_PHRASES_EN = (
    "doesn't work", "does not work", "that's wrong", "that is wrong",
    "still broken", "still not working", "not working", "didn't work",
    "did not work", "no it isn't", "wrong again", "that broke",
    "broke it", "undo that", "undo this", "revert that", "revert this",
    "try again", "not right", "incorrect", "bad output", "worse now",
    "it's worse", "made it worse", "this is worse",
)
_NEGATIVE_PHRASES_ES = (
    "no funciona", "sigue roto", "sigue mal", "otra vez mal", "no sirve",
    "esta mal", "está mal", "sigue sin funcionar", "no funcionó",
    "no funciono", "deshaz eso", "deshaz esto", "revierte eso",
    "revierte esto", "intenta otra vez", "intentalo de nuevo",
    "inténtalo de nuevo", "sigue fallando", "no anda", "peor que antes",
    "lo empeoraste", "quedó peor", "quedo peor",
)
#: Phrases signalling the skill's output was accepted / worked.
_POSITIVE_PHRASES_EN = (
    "works now", "that works", "that worked", "works great", "perfect",
    "great, thanks", "great thanks", "exactly right", "nailed it",
    "that fixed it", "fixed it", "all good now", "looks good", "thank you",
    "thanks, that", "yes that's it", "yes that is it",
)
_POSITIVE_PHRASES_ES = (
    "funciona", "perfecto", "gracias", "genial gracias", "ya funciona",
    "eso era", "asi es", "así es", "quedo bien", "quedó bien",
    "se arreglo", "se arregló", "excelente", "eso lo arregla",
    "eso lo soluciona",
)


def classify_reaction(text: str) -> str:
    """`"negative"` | `"positive"` | `"neutral"` for one user message,
    against the deterministic en/es phrase tables above. Negative is
    checked first: a message that both thanks and then corrects
    ("gracias pero sigue mal") should read as evidence of failure, not
    success — the correction is the substantive signal."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return "neutral"
    for phrase in _NEGATIVE_PHRASES_EN + _NEGATIVE_PHRASES_ES:
        if phrase in lowered:
            return "negative"
    for phrase in _POSITIVE_PHRASES_EN + _POSITIVE_PHRASES_ES:
        if phrase in lowered:
            return "positive"
    return "neutral"


# ── evidence collection ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class EvidenceItem:
    session_id: str
    turn_index: int                 # position of the activation message within the session
    message_id: str
    timestamp: str
    activation_excerpt: str         # what manage_skills was called with
    activation_action: str          # view | edit | patch
    user_reaction: str              # negative | positive | neutral | none
    user_reaction_excerpt: str
    tool_errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id, "turn_index": self.turn_index,
            "message_id": self.message_id, "timestamp": self.timestamp,
            "activation_excerpt": self.activation_excerpt,
            "activation_action": self.activation_action,
            "user_reaction": self.user_reaction,
            "user_reaction_excerpt": self.user_reaction_excerpt,
            "tool_errors": list(self.tool_errors),
            "id": self.evidence_id(),
        }

    def evidence_id(self) -> str:
        return f"{self.session_id}:{self.message_id}"


def _load_meta(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_event_name_and_args(event: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    tool = str(event.get("tool") or "")
    command = event.get("command")
    args: Dict[str, Any] = {}
    if isinstance(command, str) and command.strip().startswith("{"):
        try:
            parsed = json.loads(command)
            if isinstance(parsed, dict):
                args = parsed
        except (TypeError, ValueError):
            pass
    elif isinstance(command, dict):
        args = command
    return tool, args


def _tool_event_is_error(event: Mapping[str, Any]) -> Optional[str]:
    exit_code = event.get("exit_code")
    output = str(event.get("output") or "")
    if exit_code not in (0, None) or "error" in output.lower()[:2000]:
        return output[:300] or f"{event.get('tool')} exited {exit_code}"
    return None


def collect_evidence(skill_id: str, *, since_days: int = 30, limit: int = 20,
                      owner: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every turn in the last `since_days` where `skill_id` was consulted
    via `manage_skills`, with the user's very next message classified and
    any tool errors seen in the activation turn attached. Newest first,
    capped at `limit`. Never raises for "no evidence" — an empty list is a
    normal, valid answer.
    """
    from core.database import SessionLocal, ChatMessage, Session as DbSession
    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(days=max(1, int(since_days)))
    since_naive = since.replace(tzinfo=None)

    db = SessionLocal()
    try:
        query = (
            db.query(ChatMessage)
            .filter(ChatMessage.role == "assistant")
            .filter(ChatMessage.timestamp >= since_naive)
            .filter(ChatMessage.meta_data.like(f"%manage_skills%"))
            .filter(ChatMessage.meta_data.like(f"%{skill_id}%"))
            .order_by(ChatMessage.session_id, ChatMessage.timestamp.asc())
        )
        if owner:
            owned_ids = {
                row.id for row in db.query(DbSession.id).filter(DbSession.owner == owner).all()
            }
            candidates = [m for m in query.all() if m.session_id in owned_ids]
        else:
            candidates = query.all()

        items: List[EvidenceItem] = []
        # Group messages per session so "the next message" can be found.
        by_session: Dict[str, List[ChatMessage]] = {}
        for m in candidates:
            by_session.setdefault(m.session_id, []).append(m)

        for session_id, msgs in by_session.items():
            all_msgs = (
                db.query(ChatMessage)
                .filter(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.timestamp.asc())
                .all()
            )
            index_of = {m.id: i for i, m in enumerate(all_msgs)}
            for m in msgs:
                meta = _load_meta(m.meta_data)
                events = meta.get("tool_events")
                if not isinstance(events, list):
                    continue
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    tool, args = _tool_event_name_and_args(event)
                    if tool != "manage_skills":
                        continue
                    action = str(args.get("action") or "").strip().lower()
                    name = str(args.get("name") or args.get("skill_id") or "").strip()
                    if action not in ("view", "view_ref", "edit", "patch"):
                        continue
                    if name != skill_id:
                        continue

                    idx = index_of.get(m.id, -1)
                    tool_errors: List[str] = []
                    for ev in events:
                        if isinstance(ev, dict):
                            err = _tool_event_is_error(ev)
                            if err:
                                tool_errors.append(err)
                    # The next assistant message in the same turn (if any,
                    # e.g. a further tool round before handing back to the
                    # user) can also carry errors from applying what the
                    # skill said to do.
                    if idx >= 0 and idx + 1 < len(all_msgs) and all_msgs[idx + 1].role == "assistant":
                        next_meta = _load_meta(all_msgs[idx + 1].meta_data)
                        for ev in (next_meta.get("tool_events") or []):
                            if isinstance(ev, dict):
                                err = _tool_event_is_error(ev)
                                if err:
                                    tool_errors.append(err)

                    reaction, excerpt = "none", ""
                    if idx >= 0:
                        for later in all_msgs[idx + 1:]:
                            if later.role == "user":
                                excerpt = str(later.content or "")[:400]
                                reaction = classify_reaction(excerpt)
                                break

                    items.append(EvidenceItem(
                        session_id=session_id, turn_index=idx,
                        message_id=str(m.id),
                        timestamp=(m.timestamp.isoformat() if m.timestamp else ""),
                        activation_excerpt=json.dumps(args)[:300],
                        activation_action=action,
                        user_reaction=reaction,
                        user_reaction_excerpt=excerpt,
                        tool_errors=tool_errors[:5],
                    ))
        items.sort(key=lambda e: e.timestamp, reverse=True)
        return [e.to_dict() for e in items[:max(0, int(limit))]]
    finally:
        db.close()


# ── proposal generation ─────────────────────────────────────────────────────

_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "revised_skill_md": {"type": "string"},
        "rationale": {"type": "string"},
        "evidence_ids_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["revised_skill_md", "rationale"],
}


def _build_prompt(current_md: str, evidence: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "You are improving a SKILL.md procedure file based on real usage evidence.",
        "Rules:",
        "- Keep the YAML frontmatter's `name:` field EXACTLY unchanged.",
        "- Keep all frontmatter keys present; you may only change `description`, "
        "`version` (bump it), and the body sections.",
        "- Never remove the Pitfalls or Verification sections if they exist.",
        "- Only propose a change that is justified by the evidence below.",
        "- Return the full, complete revised SKILL.md text, not a diff.",
        "",
        "=== CURRENT SKILL.md ===",
        current_md,
        "",
        "=== EVIDENCE FROM RECENT SESSIONS ===",
    ]
    if not evidence:
        lines.append("(no usage evidence found in the lookback window)")
    for ev in evidence:
        lines.append(
            f"- [{ev.get('id')}] action={ev.get('activation_action')} "
            f"reaction={ev.get('user_reaction')!r} "
            f"user_said={ev.get('user_reaction_excerpt')!r} "
            f"tool_errors={ev.get('tool_errors')}"
        )
    lines.append("")
    lines.append(
        "Respond as JSON: {\"revised_skill_md\": \"...\", \"rationale\": \"...\", "
        "\"evidence_ids_used\": [\"...\"]}"
    )
    return "\n".join(lines)


def _frontmatter_of(text: str) -> Dict[str, Any]:
    from services.memory.skill_format import parse_frontmatter
    try:
        fm, _body = parse_frontmatter(text or "")
    except Exception:
        return {}
    return fm if isinstance(fm, dict) else {}


def _parse_model_json(text: str) -> Optional[Dict[str, Any]]:
    """Tolerant JSON read of a model reply: code fences, prose around the
    object and raw newlines inside strings (common when a model embeds a
    whole Markdown file) are all accepted. None when nothing parses."""
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        for strict in (True, False):
            try:
                data = json.JSONDecoder(strict=strict).decode(cand)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return None


def _validate_revision(*, original_md: str, revised_md: str) -> None:
    """Raises `SleepPassError` naming exactly why a proposal is refused
    before it is ever stored. Never silently "fixes" the model's output."""
    if not revised_md or not revised_md.strip():
        raise SleepPassError("sleep_pass.empty", "model returned an empty revision")
    if len(revised_md.encode("utf-8")) > MAX_SKILL_MD_BYTES:
        raise SleepPassError("sleep_pass.too_large",
                             f"revision exceeds the {MAX_SKILL_MD_BYTES}-byte limit")
    orig_len = max(1, len(original_md.encode("utf-8")))
    if len(revised_md.encode("utf-8")) > max(orig_len * MAX_GROWTH_RATIO,
                                             orig_len + MIN_GROWTH_ALLOWANCE_BYTES):
        raise SleepPassError("sleep_pass.grew_too_much",
                             "revision is more than "
                             f"{MAX_GROWTH_RATIO}x the original's size")

    orig_fm = _frontmatter_of(original_md)
    new_fm = _frontmatter_of(revised_md)
    if not new_fm:
        raise SleepPassError("sleep_pass.no_frontmatter", "revision has no parseable frontmatter")
    if str(orig_fm.get("name") or "") != str(new_fm.get("name") or ""):
        raise SleepPassError("sleep_pass.name_changed",
                             "revision changed the skill's `name` — refused")
    for required_key in ("description", "version", "category"):
        if not str(new_fm.get(required_key) or "").strip():
            raise SleepPassError("sleep_pass.missing_frontmatter",
                                 f"revision is missing frontmatter key {required_key!r}")

    from services.memory.skill_format import parse_body
    try:
        _fm, orig_body = _split_frontmatter_body(original_md)
        _fm2, new_body = _split_frontmatter_body(revised_md)
        orig_sections = parse_body(orig_body)
        new_sections = parse_body(new_body)
    except Exception as e:  # noqa: BLE001
        raise SleepPassError("sleep_pass.unparseable", f"revision body did not parse: {e}") from e
    for safety_key in ("pitfalls", "verification"):
        if orig_sections.get(safety_key) and not new_sections.get(safety_key):
            raise SleepPassError(
                "sleep_pass.removed_safety_section",
                f"revision removed the non-empty {safety_key!r} section — refused")

    scan = security_scan.scan_text(revised_md, kind="skill_file", filename="SKILL.md")
    if scan.risk_level == "critical":
        raise SleepPassError("sleep_pass.security_scan_critical",
                             "revised SKILL.md failed the security pre-scan (critical)")


def _split_frontmatter_body(text: str) -> Tuple[Dict[str, Any], str]:
    from services.memory.skill_format import parse_frontmatter
    return parse_frontmatter(text or "")


async def propose(skill_id: str, evidence: Sequence[Mapping[str, Any]], *,
                   url: Optional[str] = None, model: Optional[str] = None,
                   owner: Optional[str] = None) -> Dict[str, Any]:
    """One local-model call proposing a revised SKILL.md for `skill_id`,
    given `evidence` (as `collect_evidence` returns it). Returns the STORED
    proposal record (status `pending`). Raises `SleepPassError` for any
    reason the model's answer cannot be honestly stored — a model failure
    or a validation failure never produces a proposal, silently or
    otherwise."""
    from services.memory.skills import SkillsManager

    sm = SkillsManager(DATA_DIR)
    skills = sm.load(owner=owner)
    match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
    if not match:
        raise SleepPassError("sleep_pass.skill_not_found", f"skill {skill_id!r} not found")
    current_md = sm.read_skill_md(match.get("name"), owner=owner)
    if current_md is None:
        raise SleepPassError("sleep_pass.no_source", "skill has no readable SKILL.md source")

    resolved_url, resolved_model = url, model
    if not resolved_url or not resolved_model:
        try:
            from src.endpoint_resolver import resolve_endpoint
            resolved_url, resolved_model, _headers = resolve_endpoint(
                "skills_sleep_pass", fallback_url=url, fallback_model=model, owner=owner,
            )
        except Exception:  # noqa: BLE001 - never let settings trouble block a raise below
            logger.debug("[sleep_optimize] endpoint resolution failed", exc_info=True)
    if not resolved_url or not resolved_model:
        raise SleepPassError("sleep_pass.no_endpoint", "no local model endpoint configured")

    prompt = _build_prompt(current_md, evidence)
    try:
        from src.llm_core import llm_call_async
        raw = await llm_call_async(
            url=resolved_url, model=resolved_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=4000, timeout=120, max_retries=1,
            workload="background", response_schema=_PROPOSAL_SCHEMA,
        )
    except Exception as e:  # noqa: BLE001 - a model/network failure is not a bug here
        raise SleepPassError("sleep_pass.model_call_failed", f"{type(e).__name__}: {e}"[:300]) from e

    if isinstance(raw, tuple):
        raw = raw[0]
    data = _parse_model_json(raw if isinstance(raw, str) else "")
    if data is None:
        snippet = (raw if isinstance(raw, str) else repr(raw))[:160].replace("\n", " ")
        raise SleepPassError("sleep_pass.unparseable_response",
                             f"model response could not be parsed as JSON: {snippet!r}")
    revised_md = str(data.get("revised_skill_md") or "").strip()
    rationale = str(data.get("rationale") or "").strip()[:2000]
    evidence_ids_used = [str(x) for x in (data.get("evidence_ids_used") or [])
                         if str(x).strip()][:50]

    _validate_revision(original_md=current_md, revised_md=revised_md)

    diff_text = "".join(unified_diff(
        current_md.splitlines(keepends=True), revised_md.splitlines(keepends=True),
        fromfile=f"{skill_id}@current", tofile=f"{skill_id}@proposed",
    ))

    record = {
        "id": uuid.uuid4().hex,
        "skill_id": skill_id,
        "owner": owner or "",
        "status": "pending",
        "created_at": time.time(),
        "model": resolved_model,
        "rationale": rationale,
        "evidence_ids_used": evidence_ids_used,
        "evidence_count": len(evidence),
        "diff": diff_text,
        "original_content_hash": hashlib.sha256(current_md.encode("utf-8")).hexdigest()[:16],
    }
    _save_snapshot(record["id"], "current", current_md)
    _save_snapshot(record["id"], "proposed", revised_md)
    save_proposal(record)
    return record


# ── proposal store (JSON under DATA_DIR/skill_proposals/) ──────────────────

def _ensure_dirs() -> None:
    os.makedirs(PROPOSALS_ROOT, exist_ok=True)
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)


def _snapshot_path(proposal_id: str, which: str) -> str:
    return os.path.join(SNAPSHOT_DIR, f"{proposal_id}.{which}.md")


def _save_snapshot(proposal_id: str, which: str, text: str) -> None:
    _ensure_dirs()
    atomic_write_text(_snapshot_path(proposal_id, which), text or "")


def _read_snapshot(proposal_id: str, which: str) -> Optional[str]:
    path = _snapshot_path(proposal_id, which)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def load_proposals() -> Dict[str, Dict[str, Any]]:
    try:
        with open(PROPOSALS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_proposal(record: Mapping[str, Any]) -> None:
    _ensure_dirs()
    all_proposals = load_proposals()
    all_proposals[str(record["id"])] = dict(record)
    atomic_write_json(PROPOSALS_FILE, all_proposals, indent=2)


def get_proposal(proposal_id: str) -> Optional[Dict[str, Any]]:
    return load_proposals().get(proposal_id)


def list_proposals_for(skill_id: Optional[str] = None, *, owner: Optional[str] = None,
                        status: Optional[str] = None) -> List[Dict[str, Any]]:
    out = []
    for record in load_proposals().values():
        if skill_id and record.get("skill_id") != skill_id:
            continue
        if owner is not None and record.get("owner") != owner:
            continue
        if status and record.get("status") != status:
            continue
        out.append(record)
    out.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return out


def _load_version_history() -> Dict[str, List[Dict[str, Any]]]:
    try:
        with open(VERSIONS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_version_history(history: Mapping[str, List[Dict[str, Any]]]) -> None:
    _ensure_dirs()
    atomic_write_json(VERSIONS_FILE, dict(history), indent=2)


def approve_proposal(proposal_id: str, *, by: str, owner: Optional[str] = None,
                      reviewed: bool = True) -> Dict[str, Any]:
    """Apply an approved proposal to the live skill. Goes through
    `skill_governance.validate_promotion` (this module's origin is always
    treated as requiring the same explicit review a Teach-Mode promotion
    would — a sleep-pass proposal is machine-authored content nobody has
    read yet, exactly the case that gate exists for) and
    `skill_governance.record_version` for a version history entry that
    `rollback_proposal` can undo.
    """
    record = get_proposal(proposal_id)
    if record is None:
        raise SleepPassError("sleep_pass.not_found", f"no proposal {proposal_id!r}")
    if record.get("status") != "pending":
        raise SleepPassError("sleep_pass.not_pending",
                             f"proposal is {record.get('status')!r}, not pending")

    verdict = skill_governance.validate_promotion(
        {"source": "sleep_pass", "status": "draft"},
        target_status="published", reviewed=reviewed,
    )
    if not verdict.get("ok"):
        raise SleepPassError("sleep_pass.governance_refused", verdict.get("reason") or "refused")

    revised_md = _read_snapshot(proposal_id, "proposed")
    current_md = _read_snapshot(proposal_id, "current")
    if revised_md is None:
        raise SleepPassError("sleep_pass.snapshot_missing", "proposed SKILL.md snapshot is missing")

    skill_id = record["skill_id"]
    history = _load_version_history()
    skill_history = history.get(skill_id, [])
    new_entry = skill_governance.record_version(
        skill_history, name=skill_id, content=revised_md,
        status="published", origin="sleep_pass", reviewed=reviewed,
    )
    new_entry["proposal_id"] = proposal_id
    new_entry["approved_by"] = by or "unknown"
    new_entry["approved_at"] = time.time()
    new_entry["previous_content_snapshot"] = current_md or ""
    skill_history.append(new_entry)
    history[skill_id] = skill_history
    _save_version_history(history)

    from services.memory.skill_format import Skill, slugify
    from services.memory.skills import SkillsManager
    from src.tools.system import _skill_dump

    sk_new = Skill.from_markdown(revised_md)
    sk_new.name = slugify(sk_new.name or skill_id)
    sm = SkillsManager(DATA_DIR)
    ok = sm.update_skill(skill_id, _skill_dump(sk_new), owner=record.get("owner") or None)
    if not ok:
        raise SleepPassError("sleep_pass.apply_failed",
                             f"could not write revised SKILL.md for {skill_id!r}")

    record["status"] = "approved"
    record["approved_by"] = by or "unknown"
    record["approved_at"] = time.time()
    save_proposal(record)
    return record


def reject_proposal(proposal_id: str, *, by: str, reason: str = "") -> Dict[str, Any]:
    record = get_proposal(proposal_id)
    if record is None:
        raise SleepPassError("sleep_pass.not_found", f"no proposal {proposal_id!r}")
    if record.get("status") != "pending":
        raise SleepPassError("sleep_pass.not_pending",
                             f"proposal is {record.get('status')!r}, not pending")
    record["status"] = "rejected"
    record["rejected_by"] = by or "unknown"
    record["rejected_at"] = time.time()
    record["reject_reason"] = str(reason or "")[:500]
    save_proposal(record)
    return record


def rollback_proposal(skill_id: str, *, owner: Optional[str] = None) -> Dict[str, Any]:
    """Restore `skill_id` to the content it had immediately before the most
    recent `approve_proposal` call for it, using this module's own version
    history (independent of `skill_sources.py`'s git-backed rollback, which
    only applies to skills installed from a git remote)."""
    history = _load_version_history()
    skill_history = history.get(skill_id) or []
    if not skill_history:
        raise SleepPassError("sleep_pass.no_history", f"no sleep-pass version history for {skill_id!r}")
    last = skill_history[-1]
    previous_content = last.get("previous_content_snapshot")
    if not previous_content:
        raise SleepPassError("sleep_pass.no_previous", "no previous content recorded to roll back to")

    from services.memory.skill_format import Skill, slugify
    from services.memory.skills import SkillsManager
    from src.tools.system import _skill_dump

    sk_prev = Skill.from_markdown(previous_content)
    sk_prev.name = slugify(sk_prev.name or skill_id)
    sm = SkillsManager(DATA_DIR)
    ok = sm.update_skill(skill_id, _skill_dump(sk_prev), owner=owner)
    if not ok:
        raise SleepPassError("sleep_pass.rollback_failed", f"could not restore {skill_id!r}")

    rollback_entry = dict(last)
    rollback_entry["rolled_back_at"] = time.time()
    skill_history.append({**rollback_entry, "version": f"{last.get('version')}-rollback"})
    history[skill_id] = skill_history
    _save_version_history(history)
    return {"skill_id": skill_id, "restored_to": last.get("supersedes") or "previous", "ok": True}
