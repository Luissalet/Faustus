"""skill_library.py — the repo-bundled skill library (lot C).

`skills/library/<slug>/SKILL.md` ships inside the repository itself: a
curated set of procedures (TDD, verification loops, code review, per-
language patterns, research habits, …) any user can browse and install into
their own skill store (`services/memory/skills.py`) without writing one from
scratch. Installing copies the content — nothing here runs a skill or grants
it a tool; it only ever produces a normal, user-owned `SKILL.md` that then
goes through the exact same retrieval and prompt-injection path as a skill
the user wrote by hand.

Layout::

    skills/library/README.md          what this folder is
    skills/library/<slug>/SKILL.md     one library skill, this app's own
                                       SKILL.md dialect (services/memory/
                                       skill_format.py)

Installing runs the same SEC-09 static pre-scan (`src.skill_import_review.
scan_skill_folder`) any other imported skill folder gets, and refuses a
CRITICAL finding outright — a bundled skill earns no exemption from the scan
just because it shipped with the app.

Idempotency: an installed copy is tagged `library:<slug>` (a normal skill
tag, nothing new on disk). `installed()` reads that tag back rather than
comparing file contents, so re-running install with `replace=False` is a
no-op for a slug already present, and `uninstall()` finds exactly the copies
it created.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Set

from services.memory.skill_format import Skill, slugify

logger = logging.getLogger(__name__)

try:  # pragma: no cover - runtime_paths always imports in the app
    from src.runtime_paths import get_app_root
    _APP_ROOT = get_app_root()
except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
    _APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:  # pragma: no cover - constants always import in the app
    from src.constants import DATA_DIR
except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
    DATA_DIR = os.path.join(_APP_ROOT, "data")

#: Repo-bundled, read-only source. Reassigned by tests that want a disposable
#: fixture folder instead of the real `skills/library`.
LIBRARY_DIR = os.path.join(_APP_ROOT, "skills", "library")

#: The tag every install writes, so a re-scan can tell "a copy of this
#: library slug" apart from a skill the user wrote with a similar name.
_LIBRARY_TAG_PREFIX = "library:"

_MAX_FILE_BYTES = 400_000


def _library_tag(slug: str) -> str:
    return f"{_LIBRARY_TAG_PREFIX}{slug}"


def _slug_from_tag(tag: str) -> Optional[str]:
    if isinstance(tag, str) and tag.startswith(_LIBRARY_TAG_PREFIX):
        return tag[len(_LIBRARY_TAG_PREFIX):]
    return None


def _skills_manager(data_dir: Optional[str] = None):
    from services.memory.skills import SkillsManager
    return SkillsManager(data_dir or DATA_DIR)


def _extract_trigger(description: str) -> str:
    """A `## When to Use` fallback, derived from the description's "Use
    when …" clause. Every file this module ships has its own section, so
    this only matters for a hand-added or third-party file dropped into the
    same folder without one — never raises, returns "" when there is
    nothing to derive."""
    text = str(description or "")
    lowered = text.lower()
    idx = lowered.rfind("use when")
    if idx < 0:
        return ""
    clause = text[idx:].strip()
    return clause[:400]


def _library_slugs() -> List[str]:
    try:
        names = sorted(os.listdir(LIBRARY_DIR))
    except OSError:
        return []
    out = []
    for name in names:
        if name.startswith("."):
            continue
        path = os.path.join(LIBRARY_DIR, name, "SKILL.md")
        if os.path.isfile(path):
            out.append(name)
    return out


def _read_library_skill(slug: str) -> Optional[Skill]:
    path = os.path.join(LIBRARY_DIR, slug, "SKILL.md")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(_MAX_FILE_BYTES)
    except OSError:
        return None
    try:
        sk = Skill.from_markdown(text, path=path)
    except Exception as exc:  # noqa: BLE001 - a bad file is data, not a crash
        logger.warning("skill_library: %s does not parse: %s", path, exc)
        return None
    if not sk.when_to_use:
        sk.when_to_use = _extract_trigger(sk.description)
    return sk


def list_library(owner: Optional[str] = None) -> List[Dict]:
    """Every bundled skill, parsed. A file that fails to parse is reported
    with an `error` field rather than dropped silently or raising — one bad
    file must not take out the listing."""
    out: List[Dict] = []
    already = installed(owner)
    for slug in _library_slugs():
        path = os.path.join(LIBRARY_DIR, slug, "SKILL.md")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(_MAX_FILE_BYTES)
            sk = Skill.from_markdown(text, path=path)
        except Exception as exc:  # noqa: BLE001
            out.append({"slug": slug, "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        when_to_use = sk.when_to_use or _extract_trigger(sk.description)
        words = len((sk.when_to_use or "") .split()) + sum(
            len(x.split()) for x in (sk.procedure + sk.pitfalls + sk.verification)
        ) + len((sk.body_extra or "").split())
        row = {
            "slug": slug,
            "name": sk.name or slug,
            "description": sk.description,
            "category": sk.category,
            "tags": list(sk.tags),
            "when_to_use": when_to_use,
            "words": words,
        }
        # An ownerless caller (auth off, loopback bypass) installs ownerless
        # skills, so its flag is just as real as a named owner's.
        row["installed_for"] = slug in already
        out.append(row)
    out.sort(key=lambda r: (r.get("category") or "", r.get("slug") or ""))
    return out


def installed(owner: Optional[str] = None) -> Set[str]:
    """Library slugs already installed for `owner` (or ownerless, when
    `owner` is None), read back from the `library:<slug>` tag."""
    mgr = _skills_manager()
    out: Set[str] = set()
    for sk in mgr.load(owner=owner):
        for tag in sk.get("tags") or []:
            slug = _slug_from_tag(tag)
            if slug:
                out.add(slug)
    return out


def install(owner: Optional[str], slugs: List[str], *, replace: bool = False) -> Dict:
    """Install one or more library slugs into `owner`'s skill store.

    Never raises for a single bad slug: each one gets its own result row
    (`installed` / `skipped` / `refused` / `not_found`), and the caller sees
    the whole batch's outcome rather than losing the rest of it to the first
    failure.
    """
    from src import skill_import_review

    mgr = _skills_manager()
    already = installed(owner)
    results: List[Dict] = []
    for raw_slug in slugs or []:
        slug = slugify(str(raw_slug or ""), fallback="")
        if not slug or slug not in _library_slugs():
            results.append({"slug": raw_slug, "status": "not_found"})
            continue
        if not replace and slug in already:
            results.append({"slug": slug, "status": "skipped", "reason": "already installed"})
            continue
        skill_dir = os.path.join(LIBRARY_DIR, slug)
        scan = skill_import_review.scan_skill_folder(skill_dir)
        if scan.get("risk_level") == "critical":
            results.append({
                "slug": slug, "status": "refused",
                "reason": "security pre-scan found critical issues in this skill's folder",
            })
            continue
        sk = _read_library_skill(slug)
        if sk is None:
            results.append({"slug": slug, "status": "not_found", "reason": "could not be read"})
            continue
        files = {"SKILL.md": sk.to_markdown()}
        try:
            record = mgr.import_bundle_from_files(
                files, owner=owner, category=sk.category or "imported")
        except Exception as exc:  # noqa: BLE001
            results.append({"slug": slug, "status": "error", "reason": str(exc)[:300]})
            continue
        new_tags = sorted(set(record.get("tags") or []) | {_library_tag(slug)})
        mgr.update_skill(record["name"], {"tags": new_tags}, owner=owner)
        results.append({"slug": slug, "status": "installed", "name": record["name"]})
        already.add(slug)
    return {"results": results}


def uninstall(owner: Optional[str], slugs: List[str]) -> Dict:
    """Remove every installed copy tagged `library:<slug>` for `owner`, for
    each requested slug."""
    mgr = _skills_manager()
    wanted = {slugify(str(s or ""), fallback="") for s in (slugs or [])}
    wanted.discard("")
    results: List[Dict] = []
    for sk in mgr.load(owner=owner):
        tags = sk.get("tags") or []
        hit = next((t for t in tags if _slug_from_tag(t) in wanted), None)
        if not hit:
            continue
        slug = _slug_from_tag(hit)
        ok = mgr.delete_skill(sk["name"], owner=owner)
        results.append({"slug": slug, "name": sk["name"], "status": "removed" if ok else "error"})
    found = {r["slug"] for r in results}
    for slug in sorted(wanted - found):
        results.append({"slug": slug, "status": "not_installed"})
    return {"results": results}


__all__ = ["LIBRARY_DIR", "list_library", "install", "uninstall", "installed"]
