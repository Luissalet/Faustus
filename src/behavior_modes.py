"""src/behavior_modes.py — behaviour modes ("contra quién hablas"), CONTRATO_MODOS.

A behaviour mode is a named conversational stance the user picks — it
changes HOW Faustus talks and argues, never WHAT it is allowed to do. It is
orthogonal to the Chat/Agent mode (`Session.mode`), the task presets
(`src/preset_manager.py`), the model, the project and the skills. A mode
never suppresses or reorders `UNTRUSTED_CONTEXT_POLICY`, the agent's rules,
permissions, or the user's own explicit instructions — those always win, the
mode's own prompt text says so, and `chat_processor.build_context_preface`
places the mode block strictly before that policy so the ordering is
enforced structurally, not just asked for nicely.

Two sources, similar to `src/recipes.py`'s built-in/draft split but flatter:

* Built-ins — `config/behavior_modes/*.json`, read-only, shipped with the
  repo (`default`, `adversarial`, `socratic`, `terse`, `mentor`, `red_team`,
  `observer`, `editor`). A JSON that fails validation is skipped with a
  warning — never fails startup.
* User modes — one flat file, `DATA_DIR/behavior_modes.json`, as
  `{id: mode_dict}` for every user's custom modes together (each entry
  carries its own `owner`). `list_modes` and `get_mode` filter it by owner;
  nothing here promotes a user mode to built-in status or lets one user's
  mode collide with a built-in id or with another user's id.

`check_response` is a heuristic DETECTOR, not a judge: it never modifies the
reply, and "the model didn't follow the mode" is a fact about that turn, not
a bug in this module.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from src import constants as _constants

logger = logging.getLogger(__name__)

MODE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

#: Prompts are capped for user-submitted modes (built-ins are trusted repo
#: content and are not re-checked against this at load time).
MAX_PROMPT_CHARS = 4000

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUILTIN_DIR = os.path.join(_REPO_ROOT, "config", "behavior_modes")

_ALLOWED_CHECK_KEYS = frozenset({
    "confidence_tags", "first_sentence", "forbidden_phrases",
    "ends_with_question", "max_questions", "max_words",
})


class ModeError(Exception):
    """A save/delete request the route layer should answer as 4xx.

    ``str(exc)`` is one of ``"invalid"``, ``"builtin"`` or ``"not_found"`` —
    the short code `routes/behavior_mode_routes.py` maps to
    ``modes.<code>``.
    """


@dataclass
class Mode:
    """One behaviour mode, built-in or user-owned."""
    id: str
    builtin: bool
    version: int
    name: Dict[str, str]
    description: Dict[str, str]
    prompt: str
    checks: Dict[str, Any] = field(default_factory=dict)
    owner: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "builtin": self.builtin,
            "version": self.version,
            "name": dict(self.name),
            "description": dict(self.description),
            "prompt": self.prompt,
            "checks": dict(self.checks),
        }
        if not self.builtin:
            out["owner"] = self.owner
            out["updated_at"] = self.updated_at
        return out


# ── Validation ────────────────────────────────────────────────────────── #

def _validate_checks(checks: Any) -> Dict[str, Any]:
    """Validate and normalize a `checks` object. Raises ValueError on any
    unknown key or wrong-typed value — callers decide how to surface that
    (a warning for built-ins, a `ModeError("invalid")` for user saves)."""
    if checks in (None, {}):
        return {}
    if not isinstance(checks, dict):
        raise ValueError("checks must be an object")
    out: Dict[str, Any] = {}
    for key, value in checks.items():
        if key not in _ALLOWED_CHECK_KEYS:
            raise ValueError(f"unknown check {key!r}")
        if key in ("confidence_tags", "ends_with_question"):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a bool")
            out[key] = value
        elif key == "first_sentence":
            if value != "challenge":
                raise ValueError("first_sentence must be 'challenge'")
            out[key] = value
        elif key == "forbidden_phrases":
            if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
                raise ValueError("forbidden_phrases must be a list of strings")
            out[key] = [str(p) for p in value]
        elif key in ("max_questions", "max_words"):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{key} must be a non-negative int")
            out[key] = value
    return out


def _mode_from_dict(data: Any, *, builtin: bool) -> Mode:
    if not isinstance(data, dict):
        raise ValueError("mode must be an object")
    mode_id = str(data.get("id") or "")
    if not MODE_ID_RE.match(mode_id):
        raise ValueError(f"bad id {mode_id!r}")
    name = data.get("name")
    if not isinstance(name, dict) or not name.get("en") or not name.get("es"):
        raise ValueError("name.en and name.es are required")
    description = data.get("description") or {}
    if not isinstance(description, dict):
        raise ValueError("description must be an object")
    prompt = data.get("prompt", "")
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string")
    checks = _validate_checks(data.get("checks"))
    version = data.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool):
        version = 1
    is_builtin = bool(data.get("builtin", builtin))
    return Mode(
        id=mode_id,
        builtin=is_builtin,
        version=version,
        name={"en": str(name.get("en")), "es": str(name.get("es"))},
        description={
            "en": str(description.get("en") or ""),
            "es": str(description.get("es") or ""),
        },
        prompt=prompt,
        checks=checks,
        owner=(data.get("owner") if not is_builtin else None),
        updated_at=(data.get("updated_at") if not is_builtin else None),
    )


# ── Built-ins ─────────────────────────────────────────────────────────── #

def load_builtin() -> Dict[str, Mode]:
    """Load `config/behavior_modes/*.json`. A broken file is skipped with a
    warning — never raises, never blocks startup."""
    out: Dict[str, Mode] = {}
    try:
        paths = sorted(glob.glob(os.path.join(_BUILTIN_DIR, "*.json")))
    except OSError:
        return out
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            mode = _mode_from_dict(raw, builtin=True)
        except (OSError, ValueError) as exc:
            logger.warning("[behavior_modes] skipping invalid built-in %s: %s", path, exc)
            continue
        out[mode.id] = mode
    return out


# Hard fallback used only if `default.json` itself is missing or broken —
# `resolve()` must never come back empty-handed.
_HARDCODED_DEFAULT = Mode(
    id="default", builtin=True, version=1,
    name={"en": "Default", "es": "Por defecto"},
    description={
        "en": "Faustus as it is: no extra stance.",
        "es": "Faustus tal cual: sin postura añadida.",
    },
    prompt="", checks={},
)


# ── User modes (DATA_DIR/behavior_modes.json, {id: mode}) ───────────────── #

def _user_modes_path() -> str:
    return os.path.join(_constants.DATA_DIR, "behavior_modes.json")


def _load_user_modes() -> Dict[str, Any]:
    path = _user_modes_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_user_modes(data: Dict[str, Any]) -> None:
    atomic_write_json(_user_modes_path(), data, indent=2)


def _user_mode_objects(owner: Optional[str] = None) -> List[Mode]:
    out: List[Mode] = []
    for mode_id, raw in _load_user_modes().items():
        try:
            mode = _mode_from_dict(raw, builtin=False)
        except ValueError:
            logger.debug("[behavior_modes] skipping invalid user mode %r", mode_id, exc_info=True)
            continue
        if owner is not None and mode.owner != owner:
            continue
        out.append(mode)
    return out


# ── Public catalog API ───────────────────────────────────────────────── #

def list_modes(owner: Optional[str] = None) -> List[Mode]:
    """Default first, then built-ins by name, then this owner's own modes.

    `owner=None` returns only the built-ins — user modes are private to
    their owner (mirrors `src.recipes.list_recipes`).
    """
    builtins = load_builtin()
    out: List[Mode] = []
    default_mode = builtins.pop("default", None) or _HARDCODED_DEFAULT
    out.append(default_mode)
    out.extend(sorted(builtins.values(), key=lambda m: (m.name.get("en") or m.id).casefold()))
    if owner:
        user_modes = _user_mode_objects(owner)
        user_modes.sort(key=lambda m: (m.name.get("en") or m.id).casefold())
        out.extend(user_modes)
    return out


def get_mode(mode_id: Optional[str], owner: Optional[str] = None) -> Optional[Mode]:
    """Look up one mode by id — built-ins first, then this owner's own.

    `owner=None` skips ownership scoping entirely (single-user callers, or
    an admin validating a global default against the built-in catalog).
    """
    mode_id = str(mode_id or "").strip()
    if not mode_id:
        return None
    builtins = load_builtin()
    if mode_id in builtins:
        return builtins[mode_id]
    if mode_id == "default":
        return _HARDCODED_DEFAULT
    raw = _load_user_modes().get(mode_id)
    if not raw:
        return None
    try:
        mode = _mode_from_dict(raw, builtin=False)
    except ValueError:
        return None
    if owner is not None and mode.owner != owner:
        return None
    return mode


def save_user_mode(owner: str, data: Dict[str, Any]) -> Mode:
    """Create or update one of `owner`'s custom modes.

    Raises `ModeError("invalid")` for a bad slug, a missing/malformed
    name/description/checks, or a prompt over `MAX_PROMPT_CHARS`;
    `ModeError("builtin")` when `id` collides with a built-in mode id (an
    id already owned by a DIFFERENT user is also rejected as "invalid" —
    the flat store has no per-owner namespace, so ids are global).
    """
    mode_id = str((data or {}).get("id") or "").strip()
    if not MODE_ID_RE.match(mode_id):
        raise ModeError("invalid")
    if mode_id in load_builtin():
        raise ModeError("builtin")

    name = (data or {}).get("name") or {}
    description = (data or {}).get("description") or {}
    prompt = (data or {}).get("prompt", "")
    if not isinstance(name, dict) or not name.get("en") or not name.get("es"):
        raise ModeError("invalid")
    if not isinstance(description, dict):
        raise ModeError("invalid")
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
        raise ModeError("invalid")
    try:
        checks = _validate_checks((data or {}).get("checks"))
    except ValueError:
        raise ModeError("invalid")

    all_modes = _load_user_modes()
    existing = all_modes.get(mode_id)
    version = 1
    if isinstance(existing, dict):
        if existing.get("owner") != owner:
            raise ModeError("invalid")
        version = int(existing.get("version") or 0) + 1

    mode = Mode(
        id=mode_id, builtin=False, version=version,
        name={"en": str(name.get("en")), "es": str(name.get("es"))},
        description={
            "en": str(description.get("en") or ""),
            "es": str(description.get("es") or ""),
        },
        prompt=prompt, checks=checks, owner=owner,
        updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    all_modes[mode_id] = mode.to_dict()
    _save_user_modes(all_modes)
    return mode


def delete_user_mode(owner: str, mode_id: str) -> None:
    """Raises `ModeError("builtin")` for a built-in id, `ModeError("not_found")`
    when `mode_id` is not one of `owner`'s own modes."""
    if mode_id in load_builtin():
        raise ModeError("builtin")
    all_modes = _load_user_modes()
    existing = all_modes.get(mode_id)
    if not isinstance(existing, dict) or existing.get("owner") != owner:
        raise ModeError("not_found")
    del all_modes[mode_id]
    _save_user_modes(all_modes)


def resolve(
    *,
    requested: Optional[str] = None,
    session_mode: Optional[str] = None,
    default_setting: Optional[str] = None,
    owner: Optional[str] = None,
) -> Mode:
    """Resolve the mode for one turn: request > session > global default >
    `"default"`. An id that does not resolve (typo, deleted user mode,
    stale client) is logged and skipped — this NEVER raises and never
    breaks a chat turn; the worst case is silently falling through to the
    next candidate, and ultimately to `default`."""
    for candidate in (requested, session_mode, default_setting):
        candidate_id = str(candidate).strip() if candidate else ""
        if not candidate_id:
            continue
        mode = get_mode(candidate_id, owner)
        if mode is not None:
            return mode
        logger.warning("behavior_modes.resolve: unknown mode %r, trying next", candidate_id)
    return get_mode("default", owner) or _HARDCODED_DEFAULT


def system_block(mode: Optional[Mode]) -> Optional[str]:
    """The system-role text for `mode`, or `None` for `default` / an empty
    prompt (no block is injected at all in that case). The leading
    `Behaviour mode "<name>":` line lets the context compactor and the
    prompt inspector recognize the block for what it is."""
    if mode is None or mode.id == "default":
        return None
    prompt = (mode.prompt or "").strip()
    if not prompt:
        return None
    label = mode.name.get("en") or mode.id
    return f'Behaviour mode "{label}":\n{prompt}'


# ── check_response — heuristic detector, never a judge ──────────────── #

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_CONFIDENCE_TAG_RE = re.compile(
    r"\[(certain|likely|guessing|seguro|probable|suposici[oó]n)\]", re.IGNORECASE,
)
_AGREEMENT_OPENING_RE = re.compile(
    r"^(yes|s[ií]|correct|exactly|right|true|great|good|indeed|"
    r"tienes\s+razón|tienes\s+razon|you're\s+right|you\s+are\s+right|"
    r"de\s+acuerdo|cierto|exacto)\b",
    re.IGNORECASE,
)


def _first_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    m = re.search(r"[.!?\n]", text)
    return text[:m.start()] if m else text


def _last_paragraph(text: str) -> str:
    parts = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return parts[-1] if parts else text.strip()


def check_response(mode: Optional[Mode], text: str) -> Dict[str, Any]:
    """Heuristic, honest checks against `mode.checks` — a DETECTOR, never a
    judge: it reports what it found and never rewrites or blocks `text`.
    Empty text runs no checks at all (there is nothing to judge)."""
    result: Dict[str, Any] = {"checked": [], "violations": []}
    if mode is None or not (text or "").strip():
        return result

    checks = mode.checks or {}
    stripped = _CODE_BLOCK_RE.sub("", text)

    if checks.get("forbidden_phrases"):
        result["checked"].append("forbidden_phrases")
        low = stripped.casefold()
        for phrase in checks["forbidden_phrases"]:
            if phrase and phrase.casefold() in low:
                result["violations"].append({
                    "rule": "forbidden_phrases",
                    "detail": f'used a banned phrase: "{phrase}"',
                })

    if checks.get("confidence_tags"):
        result["checked"].append("confidence_tags")
        if not _CONFIDENCE_TAG_RE.search(stripped):
            result["violations"].append({
                "rule": "confidence_tags",
                "detail": "no confidence tags ([Certain]/[Likely]/[Guessing])",
            })

    if checks.get("first_sentence") == "challenge":
        result["checked"].append("first_sentence")
        # A leading `[Certain]`-style confidence tag is metadata, not the
        # sentence's own opening words — strip it before testing so a mode
        # that also requires `confidence_tags` doesn't get every reply
        # flagged here just for tagging its first claim.
        _first = _first_sentence(stripped).strip()
        _first = re.sub(r"^\[[^\[\]]{1,20}\]\s*", "", _first)
        if _AGREEMENT_OPENING_RE.match(_first):
            result["violations"].append({
                "rule": "first_sentence",
                "detail": "first sentence opens with agreement",
            })

    if checks.get("ends_with_question"):
        result["checked"].append("ends_with_question")
        if "?" not in _last_paragraph(stripped):
            result["violations"].append({
                "rule": "ends_with_question",
                "detail": "no question in the final paragraph",
            })

    if "max_questions" in checks:
        result["checked"].append("max_questions")
        n = stripped.count("?")
        limit = checks["max_questions"]
        if n > limit:
            result["violations"].append({
                "rule": "max_questions",
                "detail": f"{n} question marks (max {limit})",
            })

    if "max_words" in checks:
        result["checked"].append("max_words")
        n = len(stripped.split())
        limit = checks["max_words"]
        if n > limit:
            result["violations"].append({
                "rule": "max_words",
                "detail": f"{n} words (max {limit})",
            })

    return result
