"""The four places a harness proposal may point at, and how each is read,
validated, written and restored.

======================  ==========================  =============================
axis                    target id                   what "content" is
======================  ==========================  =============================
``prompt_layer``        ``project:<project id>``    the project's standing
                                                    instructions (the layer
                                                    AROUND the base prompt; the
                                                    base prompt itself is not a
                                                    target of anything here)
``skill``               ``skill:<name>``            the skill's ``SKILL.md``
``memory``              ``memory:<entry id>``       the memory entry's text
``subagent_spec``       ``agent:<slug>``            the user's ``AGENT.md``
======================  ==========================  =============================

Nothing else is a valid target. That is the structural half of "the base
prompt is immutable": the proposer may only choose among targets this module
lists as candidates, and :func:`parse_target` refuses every other string, so a
model that names ``system_prompt`` (or a path) gets ``harness.base_prompt_immutable``
or ``harness.bad_target`` and no record at all.

Every write goes through the store that already owns the thing, never around
it: projects through ``ProjectStore.update``, memories through
``MemoryManager``, skills through the existing skill proposal store and its
governance gate, agent definitions through ``agent_defs``' own parser first.
"""
from __future__ import annotations

import copy
import logging
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from core.atomic_io import atomic_write_text
from src import security_scan
from src.constants import DATA_DIR
from src.harness_refinement.store import AXES, HarnessError, changed_lines

logger = logging.getLogger(__name__)

#: Added + removed lines allowed for one edit. "The smallest edit" is enforced,
#: not requested: a model that rewrites a whole file is refused.
MAX_CHANGED_LINES = {"prompt_layer": 30, "memory": 6, "subagent_spec": 30, "skill": 60}
MAX_MEMORY_CHARS = 600
MAX_PROMPT_GROWTH_CHARS = 2000

SUPPORTED_OPS = {
    "prompt_layer": ("create", "update", "delete"),
    "skill": ("update",),
    "memory": ("create", "update", "delete"),
    "subagent_spec": ("create", "update", "delete"),
}

_TARGET_RE = {
    "prompt_layer": re.compile(r"^project:([A-Za-z0-9_-]{1,64})$"),
    "skill": re.compile(r"^skill:([a-z0-9][a-z0-9._-]{0,100})$"),
    "memory": re.compile(r"^memory:([A-Za-z0-9_-]{8,64})$"),
    "subagent_spec": re.compile(r"^agent:([a-z0-9][a-z0-9_-]{0,63})$"),
}

#: What a model might name when it is reaching for the base prompt itself.
_BASE_PROMPT_RE = re.compile(
    r"(system[\s_-]*prompt|base[\s_-]*prompt|core[\s_-]*prompt|prompt[\s_-]*base|agent_loop|\.py\b|"
    r"AGENTS?\.md|FAUSTUS\.md|CLAUDE\.md|^/|^[A-Za-z]:[\\/]|\.\.)", re.I)

# Late-bound so a deployment (or a test) can hand in the live vector index.
_memory_vector: Any = None


def configure(*, memory_vector: Any = None) -> None:
    global _memory_vector
    _memory_vector = memory_vector


# -- parsing ---------------------------------------------------------------

def parse_target(axis: str, target: str) -> str:
    """The identifier inside ``target``, or a :class:`HarnessError`.

    A string aimed at the base prompt (or at a file path) gets its own class so
    the log says what was attempted instead of just "bad target".
    """
    if axis not in AXES:
        if _BASE_PROMPT_RE.search(str(axis or "")) or _BASE_PROMPT_RE.search(str(target or "")):
            raise HarnessError("harness.base_prompt_immutable",
                               "the base system prompt is immutable; only the layer around it can be proposed")
        raise HarnessError("harness.bad_axis", f"unknown axis {axis!r}")
    text = str(target or "").strip()
    match = _TARGET_RE[axis].match(text)
    if not match:
        if _BASE_PROMPT_RE.search(text):
            raise HarnessError("harness.base_prompt_immutable",
                               "the base system prompt and source files are immutable; "
                               "only project instructions, skills, memories and sub-agent specs can be proposed")
        raise HarnessError("harness.bad_target", f"{text!r} is not a valid {axis} target")
    return match.group(1)


def same_content(a: Optional[str], b: Optional[str]) -> bool:
    return (a or "") == (b or "")


def _scan(text: str, kind: str) -> None:
    result = security_scan.scan_text(text, kind=kind, filename="harness-proposal")
    if result.risk_level == "critical":
        raise HarnessError("harness.security_scan_critical", "the proposed text failed the security pre-scan")


# -- prompt_layer: project instructions -----------------------------------

def _project_store():
    from services.projects import get_store
    return get_store()


def _project(ident: str, owner: str) -> Dict[str, Any]:
    row = _project_store().get(ident, owner or None)
    if not row:
        raise HarnessError("harness.target_missing", f"project {ident!r} does not exist")
    return row


# -- skill -----------------------------------------------------------------

def _skills_manager():
    from services.memory.skills import SkillsManager
    return SkillsManager(DATA_DIR)


def _skill_path(name: str, owner: str) -> Optional[str]:
    sm = _skills_manager()
    for path in sm._iter_skill_files():
        sk = sm._read_skill(path)
        if sk and sk.name == name and (sk.owner or "") == (owner or ""):
            return path
    return None


# -- memory ----------------------------------------------------------------

def _memory_manager():
    from src.memory import MemoryManager
    return MemoryManager(DATA_DIR)


def _visible(entry: Mapping[str, Any], owner: str) -> bool:
    return not owner or not entry.get("owner") or entry.get("owner") == owner


def _memory_entries_for_update() -> List[Dict[str, Any]]:
    from src.memory import MemoryStoreUnreadable
    try:
        return _memory_manager().load_all_for_update()
    except MemoryStoreUnreadable as exc:
        raise HarnessError("harness.memory_unreadable", str(exc)) from exc


def _vector_add(entry_id: str, text: str) -> None:
    vec = _memory_vector
    try:
        if vec is not None and getattr(vec, "healthy", False):
            vec.add(entry_id, text)
    except Exception:  # noqa: BLE001 - the JSON store is the source of truth
        logger.debug("memory vector add failed", exc_info=True)


def _vector_remove(entry_id: str) -> None:
    vec = _memory_vector
    try:
        if vec is not None and getattr(vec, "healthy", False):
            vec.remove(entry_id)
    except Exception:  # noqa: BLE001
        logger.debug("memory vector remove failed", exc_info=True)


# -- subagent_spec ---------------------------------------------------------

def _agent_path(slug: str) -> str:
    from src import agent_defs
    if agent_defs.clean_slug(slug) != slug:
        raise HarnessError("harness.bad_target", f"{slug!r} is not a plain agent slug")
    return agent_defs.def_path(slug)


def _parse_agent(slug: str, text: str):
    from src import agent_defs
    try:
        return agent_defs.parse(text, slug=slug, source=agent_defs.SOURCE_USER)
    except agent_defs.AgentDefError as exc:
        raise HarnessError("harness.invalid_agent_spec", f"the sub-agent spec does not load: {exc}") from exc


def _permission_widening(old_rules: List[str], new_rules: List[str]) -> List[str]:
    """Permission rules are ORDERED and the LAST match wins (``subagent_permissions.decide``),
    so a set comparison is blind to a reordering that flips a verdict. The only
    edits that provably cannot make the agent more permissive are:

    * the same rules, in the same order, with some ``allow`` rules dropped, and
    * ``deny`` rules appended at the END (nothing after them can override them).

    Anything else (a reorder, a dropped ``deny``, an inserted rule, an added
    ``allow``) is reported, even when it happens to be harmless: telling a
    harmless reorder from a harmful one is a decision for a person."""
    if list(old_rules) == list(new_rules):
        return []
    j = 0
    for rule in old_rules:
        if j < len(new_rules) and new_rules[j] == rule:
            j += 1
        elif rule.startswith("allow "):
            continue                      # an allow that was dropped can only narrow
        else:
            return ["removes or moves a permission deny rule"]
    tail = new_rules[j:]
    if all(rule.startswith("deny ") for rule in tail):
        return []
    if any(rule.startswith("allow ") for rule in tail):
        return ["adds, reorders or inserts a permission allow rule (the last matching rule wins)"]
    return ["changes the order of the permission rules (the last matching rule wins)"]


def authority_widening(old, new) -> List[str]:
    """What the new spec may do that the old one could not. Empty means the edit
    only keeps or narrows authority. A refinement that needs MORE authority is a
    decision to make by hand, not a thing to propose after a task."""
    reasons: List[str] = []
    if old is None:
        return reasons
    if (old.tools and not new.tools) or (old.tools and set(new.tools) - set(old.tools)):
        reasons.append("adds tools")
    if set(old.deny) - set(new.deny):
        reasons.append("removes a deny entry")
    reasons.extend(_permission_widening([r.as_text() for r in old.permission],
                                        [r.as_text() for r in new.permission]))
    if old.mode != "coordinator" and new.mode == "coordinator":
        reasons.append("lets the agent delegate")
    if old.files and set(new.files) - set(old.files):
        reasons.append("adds file claims")
    for field in ("max_rounds", "timeout_s"):
        a, b = getattr(old, field), getattr(new, field)
        if a is not None and (b is None or b > a):
            reasons.append(f"raises {field}")
    return reasons


# -- reading ---------------------------------------------------------------

def read_current(axis: str, target: str, owner: str = "") -> Optional[str]:
    """The target's content right now, or None when it holds none / does not
    exist yet (a not-yet-created memory, an agent without a file, a project with
    empty instructions). A project that does not exist raises."""
    ident = parse_target(axis, target)
    if axis == "prompt_layer":
        text = str(_project(ident, owner).get("instructions") or "")
        return text or None
    if axis == "skill":
        return _skills_manager().read_skill_md(ident, owner=owner or None)
    if axis == "memory":
        for entry in _memory_manager().load_all():
            if entry.get("id") == ident and _visible(entry, owner):
                return str(entry.get("text") or "")
        return None
    if axis == "subagent_spec":
        path = _agent_path(ident)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise HarnessError("harness.target_unreadable", str(exc)) from exc
    raise HarnessError("harness.bad_axis", f"unknown axis {axis!r}")


# -- validating a proposed edit -------------------------------------------

def validate_edit(axis: str, target: str, op: str, before: Optional[str], after: Optional[str],
                  owner: str = "") -> Tuple[Optional[str], List[str]]:
    """Check one proposed edit against every deterministic rule. Returns the
    (possibly normalised) ``after`` and the risk flags; raises
    :class:`HarnessError` with a stable class for anything that must not be
    stored. Nothing here calls a model."""
    ident = parse_target(axis, target)
    if op not in SUPPORTED_OPS[axis]:
        raise HarnessError("harness.unsupported_op", f"{axis} supports {', '.join(SUPPORTED_OPS[axis])}, not {op!r}")
    existing = before is not None and before != ""
    if op == "create" and existing:
        raise HarnessError("harness.target_exists", "cannot create a target that already has content")
    if op in ("update", "delete") and not existing:
        raise HarnessError("harness.target_missing", f"cannot {op} a target that has no content")
    flags: List[str] = []

    if op == "delete":
        if axis == "memory" or axis == "prompt_layer" or axis == "subagent_spec":
            return None, flags
    if after is None or not str(after).strip():
        raise HarnessError("harness.empty_edit", "the proposed content is empty")
    after = str(after)

    if axis == "prompt_layer":
        after = after.strip()
        from services.projects import MAX_INSTRUCTIONS
        if len(after) > MAX_INSTRUCTIONS:
            raise HarnessError("harness.too_large", f"instructions exceed {MAX_INSTRUCTIONS} characters")
        if op == "update" and len(after) - len(before or "") > MAX_PROMPT_GROWTH_CHARS:
            raise HarnessError("harness.not_minimal", f"the edit adds more than {MAX_PROMPT_GROWTH_CHARS} characters")
        _scan(after, "markdown")
    elif axis == "memory":
        after = after.strip()
        if len(after) > MAX_MEMORY_CHARS:
            raise HarnessError("harness.not_minimal", f"a memory entry is at most {MAX_MEMORY_CHARS} characters")
        _scan(after, "markdown")
        if op == "create":
            from src.memory import get_text_similarity
            for entry in _memory_manager().load_all():
                if _visible(entry, owner) and get_text_similarity(after, str(entry.get("text") or "")) >= 0.8:
                    raise HarnessError("harness.duplicate_memory", "a near-identical memory already exists")
    elif axis == "skill":
        from src.skills_runtime import sleep_optimize as so
        try:
            after = so._with_original_frontmatter(before or "", after)
            so._validate_revision(original_md=before or "", revised_md=after)
        except so.SleepPassError as exc:
            raise HarnessError("harness.skill_" + exc.error_class.split(".")[-1], exc.message) from exc
    elif axis == "subagent_spec":
        new = _parse_agent(ident, after)
        old = _parse_agent(ident, before) if before else None
        widened = authority_widening(old, new)
        if widened:
            raise HarnessError("harness.widens_authority",
                               "the sub-agent spec edit would widen what the agent may do (" + ", ".join(widened) + ")")
        if op == "create":
            flags.append("new_agent")
        _scan(after, "skill_file")

    if op == "update" and same_content(before, after):
        raise HarnessError("harness.no_change", "the proposed content equals the current content")
    if changed_lines(before, after) > MAX_CHANGED_LINES[axis]:
        raise HarnessError("harness.not_minimal",
                           f"the edit changes more than {MAX_CHANGED_LINES[axis]} lines; propose a smaller one")
    return after, flags


# -- writing ---------------------------------------------------------------

def apply_write(record: Dict[str, Any], owner: str = "") -> None:
    """Write ``record['after']`` to the target. Raises :class:`HarnessError`.
    May add keys to ``record['meta']`` that :func:`apply_restore` needs."""
    axis, target, op = record["axis"], record["target"], record["op"]
    ident = parse_target(axis, target)
    after = record.get("after")
    meta = record.setdefault("meta", {})
    expect = (record.get("before"), _CHANGED_BEFORE_APPLY)

    if axis == "prompt_layer":
        _project(ident, owner)
        value = "" if op == "delete" else str(after or "").strip()
        _set_instructions(ident, value, owner, guard=(axis, target, expect))
    elif axis == "skill":
        delegate = meta.get("delegate") or {}
        if not delegate.get("id"):
            raise HarnessError("harness.skill_no_delegate", "this skill proposal has no entry in the skill proposal store")
        from src.skills_runtime import sleep_optimize as so
        _remember_raw(meta, _skill_path(ident, owner))
        try:
            so.approve_proposal(str(delegate["id"]), by="harness-refinement")
        except so.SleepPassError as exc:
            raise HarnessError("harness.skill_" + exc.error_class.split(".")[-1], exc.message) from exc
    elif axis == "memory":
        entries = _memory_entries_for_update()
        index = next((i for i, e in enumerate(entries) if e.get("id") == ident and _visible(e, owner)), -1)
        _guard(axis, target, owner, *expect)
        if op == "create":
            if index >= 0:
                raise HarnessError("harness.target_exists", "a memory with this id already exists")
            entry = _memory_manager().add_entry(str(after or ""), source="harness_refinement",
                                                category="fact", owner=owner or None)
            entry["id"] = ident
            entries.append(entry)
            _memory_manager().save(entries)
            _vector_add(ident, entry["text"])
            return
        if index < 0:
            raise HarnessError("harness.target_missing", "the memory entry no longer exists")
        meta.setdefault("before_entry", copy.deepcopy(entries[index]))
        meta.setdefault("before_index", index)
        if op == "delete":
            entries.pop(index)
            _memory_manager().save(entries)
            _vector_remove(ident)
        else:
            entries[index]["text"] = str(after or "").strip()
            _memory_manager().save(entries)
            _vector_remove(ident)
            _vector_add(ident, entries[index]["text"])
    elif axis == "subagent_spec":
        path = _agent_path(ident)
        _guard(axis, target, owner, *expect)
        _remember_raw(meta, path)
        if op == "delete":
            _remove_agent_file(path)
        else:
            _parse_agent(ident, str(after or ""))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            atomic_write_text(path, str(after), newline="")
    else:
        raise HarnessError("harness.bad_axis", f"unknown axis {axis!r}")


def apply_restore(record: Dict[str, Any], owner: str = "") -> None:
    """Put the target back to ``record['before']`` (the inverse of the write
    :func:`apply_write` made for this record)."""
    axis, target, op = record["axis"], record["target"], record["op"]
    ident = parse_target(axis, target)
    before = record.get("before")
    meta = record.get("meta") or {}
    expect = (record.get("applied_content"), _CHANGED_SINCE_APPLY)

    if axis == "prompt_layer":
        _project(ident, owner)
        _set_instructions(ident, before or "", owner, guard=(axis, target, expect))
    elif axis == "skill":
        path = _skill_path(ident, owner)
        if not path:
            raise HarnessError("harness.target_missing", f"skill {ident!r} no longer exists")
        _guard(axis, target, owner, *expect)
        # Raw bytes: the skill store re-serialises what it is handed (and Windows
        # turns LF into CRLF), and the point of undo is the exact previous bytes.
        _restore_raw(meta, path, before)
    elif axis == "memory":
        entries = _memory_entries_for_update()
        index = next((i for i, e in enumerate(entries) if e.get("id") == ident), -1)
        _guard(axis, target, owner, *expect)
        if op == "create":
            if index >= 0:
                entries.pop(index)
                _memory_manager().save(entries)
            _vector_remove(ident)
        elif op == "update":
            if index < 0:
                raise HarnessError("harness.target_missing", "the memory entry no longer exists")
            original = meta.get("before_entry")
            entries[index] = copy.deepcopy(original) if isinstance(original, dict) else dict(entries[index], text=before or "")
            _memory_manager().save(entries)
            _vector_remove(ident)
            _vector_add(ident, str(before or ""))
        else:  # delete -> put the exact original entry back where it was
            original = meta.get("before_entry")
            if index >= 0:
                return
            entry = copy.deepcopy(original) if isinstance(original, dict) else {
                "id": ident, "text": before or "", "source": "harness_refinement", "category": "fact"}
            at = meta.get("before_index")
            entries.insert(min(int(at), len(entries)) if isinstance(at, int) else len(entries), entry)
            _memory_manager().save(entries)
            _vector_add(ident, str(entry.get("text") or ""))
    elif axis == "subagent_spec":
        _guard(axis, target, owner, *expect)
        _restore_raw(meta, _agent_path(ident), before)
    else:
        raise HarnessError("harness.bad_axis", f"unknown axis {axis!r}")


def _read_bytes(path: Optional[str]) -> Optional[bytes]:
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _write_bytes(path: str, data: bytes) -> None:
    """Atomic, byte-exact write (no newline translation: undo is about bytes)."""
    import tempfile
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".harness-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _remember_raw(meta: Dict[str, Any], path: Optional[str]) -> None:
    """Keep the file's exact bytes before an edit, once, for :func:`apply_restore`."""
    import base64
    if "before_raw_b64" in meta:
        return
    raw = _read_bytes(path)
    meta["before_raw_b64"] = base64.b64encode(raw).decode("ascii") if raw is not None else None


def _restore_raw(meta: Mapping[str, Any], path: str, fallback_text: Optional[str]) -> None:
    """Write back the bytes :func:`_remember_raw` kept (or, for a record from
    before that existed, the text as UTF-8). A file that did not exist is removed."""
    import base64
    if "before_raw_b64" in meta:
        raw = meta.get("before_raw_b64")
        data = base64.b64decode(raw) if raw is not None else None
    else:
        data = fallback_text.encode("utf-8") if fallback_text else None
    if data is None:
        _remove_agent_file(path)
    else:
        _write_bytes(path, data)


_CHANGED_BEFORE_APPLY = ("harness.target_changed",
                         "The target changed after this proposal was made, so applying it now would overwrite that "
                         "change. Reject this proposal and run a new one.")
_CHANGED_SINCE_APPLY = ("harness.target_changed_since_apply",
                        "The target was edited after this proposal was applied. Undoing now would discard that later "
                        "change, so nothing was touched. Edit it by hand if you still want the old text back.")


def _guard(axis: str, target: str, owner: str, expected: Optional[str], error: Tuple[str, str]) -> None:
    """Compare the target with what the edit was made against, RIGHT before the
    write. :func:`store.approve` / :func:`store.undo` check once up front; this
    is the check that sits next to the write (and, for projects, inside the
    store's own lock), so a change made by a person in between is not overwritten."""
    if not same_content(read_current(axis, target, owner), expected):
        raise HarnessError(*error)


def _set_instructions(project_id: str, value: str, owner: str, guard: Optional[Tuple[str, str, Tuple[Any, Tuple[str, str]]]] = None) -> None:
    """Write the project's instructions. With ``guard`` the compare-and-write runs
    under the project store's own mutation lock (the one ``ProjectStore.update``
    takes), so an edit made through that store cannot land between the two."""
    import contextlib
    from services.projects import ProjectError
    store = _project_store()
    lock = getattr(store, "_lock", None)
    try:
        with (lock if lock is not None else contextlib.nullcontext()):
            if guard is not None:
                axis, target, (expected, error) = guard
                _guard(axis, target, owner, expected, error)
            updated = store.update(project_id, {"instructions": value}, owner=owner or None)
    except ProjectError as exc:
        raise HarnessError("harness.apply_failed", str(exc)) from exc
    if not updated:
        raise HarnessError("harness.target_missing", f"project {project_id!r} does not exist")


def _remove_agent_file(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    try:
        os.rmdir(os.path.dirname(path))
    except OSError:
        pass


# -- skills: the existing proposal store ----------------------------------

def create_skill_delegate(*, proposal_id: str, skill_name: str, owner: str, current_md: str, revised_md: str,
                          rationale: str, evidence_ids: List[str], model: str) -> Dict[str, Any]:
    """Register the same proposal in the skills sleep pass's store, under the
    same id, so it shows up (with its diff and Approve / Reject buttons) in
    Skills > Proposals exactly like a sleep-pass proposal. Nothing is applied."""
    import hashlib
    import time as _time
    from src.skills_runtime import sleep_optimize as so
    from src.harness_refinement.store import make_diff

    record = {
        "id": proposal_id, "skill_id": skill_name, "owner": owner or "", "status": "pending",
        "created_at": _time.time(), "model": model, "rationale": rationale,
        "evidence_ids_used": list(evidence_ids)[:50], "evidence_count": len(evidence_ids),
        "diff": make_diff(current_md, revised_md, f"{skill_name}"),
        "original_content_hash": hashlib.sha256(current_md.encode("utf-8")).hexdigest()[:16],
        "source": "harness_refinement", "harness_proposal_id": proposal_id,
    }
    so._save_snapshot(proposal_id, "current", current_md)
    so._save_snapshot(proposal_id, "proposed", revised_md)
    so.save_proposal(record)
    return {"store": "skill_proposals", "id": proposal_id}


def delegate_status(record: Mapping[str, Any]) -> Optional[str]:
    """``approved`` / ``rejected`` when the skill proposal store says so, else None."""
    delegate = (record.get("meta") or {}).get("delegate") or {}
    if not delegate.get("id"):
        return None
    try:
        from src.skills_runtime import sleep_optimize as so
        other = so.get_proposal(str(delegate["id"]))
    except Exception:  # noqa: BLE001
        return None
    status = (other or {}).get("status")
    return status if status in ("approved", "rejected") else None


def reject_delegate(record: Mapping[str, Any], *, by: str, reason: str = "") -> None:
    delegate = (record.get("meta") or {}).get("delegate") or {}
    if not delegate.get("id"):
        return
    from src.skills_runtime import sleep_optimize as so
    try:
        so.reject_proposal(str(delegate["id"]), by=by, reason=reason)
    except so.SleepPassError:
        pass
