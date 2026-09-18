"""
skills_runtime/discovery.py — finding skills next to the work, without letting
where they were found mean anything.

The masterplan asks for explicit discovery of local skill folders —
`.odysseus/skills`, `.agents/skills`, `.claude/skills` — walking up from the
workspace to the repository root, loading instructions on demand and **never
elevating permissions because of where a skill came from**.

That last clause is the whole design. It is easy to write a loader that trusts
`.odysseus/` more than `.claude/` because one of them is "ours", and the
moment it does, the way to get a permission is to move a file. So discovery
here returns *where* a skill was found as a fact for the audit, and the bridge
that turns it into a manifest never reads that field.

The other rule is where the walk stops, and the first version got it wrong in
a way worth recording. It climbed to the filesystem root, which on a developer
machine means `C:\\Users\\<you>` — so a scratch folder inherited the user's
personal `.claude/skills`, and a test in a temp directory found a skill from
their home. The masterplan says *up to the repository root*, and it means it:
the walk now stops at the directory holding `.git`, and when there is no
repository it does not climb at all. Anything further is an explicit
`extra_roots`, because reaching into someone's home directory is a decision,
not a default.

It will also not follow symlinks out of the tree, and will not read a file
bigger than a document ought to be.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: In priority order — nearer the work wins over further away, and within one
#: directory this is the tie-break. It is an ordering, not a trust level.
SKILL_DIR_NAMES = (
    os.path.join(".odysseus", "skills"),
    os.path.join(".agents", "skills"),
    os.path.join(".claude", "skills"),
)

#: A SKILL.md is a document. Anything past this is not one, and reading it
#: into the prompt budget would be the bug rather than the feature.
MAX_SKILL_BYTES = 512 * 1024

MAX_DEPTH = 24


@dataclass(frozen=True)
class DiscoveredSkill:
    """Where a skill was found, and how far up. `origin` is for the audit and
    for the UI; nothing in `bridge.py` reads it, on purpose."""

    name: str
    path: str                 # the SKILL.md itself
    origin: str               # which of SKILL_DIR_NAMES it came from
    root: str                 # the directory that folder hung off
    distance: int             # 0 = the workspace itself, 1 = its parent, …
    bytes: int = 0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "path": self.path, "origin": self.origin,
                "root": self.root, "distance": self.distance,
                "bytes": self.bytes, "error": self.error}


def roots_for(start: str, max_depth: int = MAX_DEPTH) -> Tuple[List[str], str]:
    """The directories to look in, and why the walk stopped there.

    Returns `(roots, reason)` so a UI can say "stopped at the repository root"
    or "this is not a repository, so only the workspace itself was searched" —
    both of which explain a missing skill better than an empty list does.
    """
    start = os.path.abspath(start)
    chain: List[str] = []
    current = start
    for _ in range(max_depth):
        chain.append(current)
        if os.path.exists(os.path.join(current, ".git")):
            return chain, "repository root"
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        current = parent
    # No repository above the workspace. Climbing on would walk into the
    # user's home directory and quietly adopt whatever skills live there.
    return [start], "not a repository — only the workspace itself"


def _skill_files(folder: str) -> Iterable[Tuple[str, str]]:
    """`(name, path)` for each `SKILL.md` directly inside `folder`, one level
    of subdirectories deep — the layout `data/skills/<category>/<name>/` and
    the flatter `<folder>/<name>/SKILL.md` both land here."""
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return
    for entry in entries:
        sub = os.path.join(folder, entry)
        if not os.path.isdir(sub) or os.path.islink(sub):
            continue
        direct = os.path.join(sub, "SKILL.md")
        if os.path.isfile(direct):
            yield entry, direct
            continue
        try:
            inner = sorted(os.listdir(sub))
        except OSError:
            continue
        for name in inner:
            nested = os.path.join(sub, name, "SKILL.md")
            if os.path.isfile(nested) and not os.path.islink(os.path.join(sub, name)):
                yield name, nested


def _contained(path: str, root: str) -> bool:
    try:
        resolved_root = os.path.normcase(os.path.realpath(root))
        resolved_path = os.path.normcase(os.path.realpath(path))
        return os.path.commonpath([resolved_root, resolved_path]) == resolved_root
    except (ValueError, OSError):
        return False


def discover(workspace: str, *, extra_roots: Optional[Iterable[str]] = None,
             max_depth: int = MAX_DEPTH) -> List[DiscoveredSkill]:
    """Every skill visible from `workspace`, nearest first.

    A name found closer to the work shadows the same name further up — that is
    an ordering, not a permission. Both are returned; the caller decides, and
    the audit can see there were two.
    """
    found: List[DiscoveredSkill] = []
    roots, _reason = roots_for(workspace, max_depth)
    for extra in (extra_roots or ()):
        if extra and os.path.isdir(extra) and os.path.abspath(extra) not in roots:
            roots.append(os.path.abspath(extra))

    for distance, root in enumerate(roots):
        for origin in SKILL_DIR_NAMES:
            folder = os.path.join(root, origin)
            if not os.path.isdir(folder) or not _contained(folder, root):
                continue
            for name, path in _skill_files(folder):
                if not _contained(path, root):
                    found.append(DiscoveredSkill(name, path, origin, root, distance,
                                                 error="skill source is outside its discovery root"))
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError as e:
                    found.append(DiscoveredSkill(name, path, origin, root, distance,
                                                 error=f"unreadable: {e}"))
                    continue
                if size > MAX_SKILL_BYTES:
                    found.append(DiscoveredSkill(
                        name, path, origin, root, distance, bytes=size,
                        error=f"larger than {MAX_SKILL_BYTES} bytes; not loaded"))
                    continue
                found.append(DiscoveredSkill(name, path, origin, root, distance, bytes=size))
    return found


def shadowed(found: Iterable[DiscoveredSkill]) -> Dict[str, List[DiscoveredSkill]]:
    """Names that appear more than once, with every copy. Worth surfacing:
    "the skill I edited did nothing" is usually this, and the answer is a list
    of paths rather than a guess."""
    by_name: Dict[str, List[DiscoveredSkill]] = {}
    for item in found:
        by_name.setdefault(item.name, []).append(item)
    return {name: items for name, items in by_name.items() if len(items) > 1}


def read_markdown(found: DiscoveredSkill) -> str:
    """Revalidate the source at use time and bound the actual read, not a hint.

    Discovery metadata may be old. Opening a resolved regular file and comparing
    its identity also rejects a replacement between validation and opening.
    """
    if found.error:
        raise ValueError(found.error)
    if not _contained(found.path, found.root):
        raise ValueError("skill source is outside its discovery root")
    resolved = os.path.realpath(found.path)
    before = os.stat(resolved)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("skill source is not a regular file")
    if before.st_size > MAX_SKILL_BYTES:
        raise ValueError(f"skill is larger than {MAX_SKILL_BYTES} bytes; not loaded")
    with open(resolved, "rb") as fh:
        opened = os.fstat(fh.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("skill source changed while opening")
        data = fh.read(MAX_SKILL_BYTES + 1)
    if len(data) > MAX_SKILL_BYTES:
        raise ValueError(f"skill is larger than {MAX_SKILL_BYTES} bytes; not loaded")
    return data.decode("utf-8", "replace")


#: ADP-25: an approval pins a skill to exact bytes, so the digest that gets
#: approved must cover more than the SKILL.md text — a script the manifest
#: references, sitting in the same folder, is part of what was reviewed.
#: Bounded the same way `read_markdown` bounds a single file, so a folder
#: dressed up as a skill cannot make an approval check read arbitrarily much
#: off disk.
MAX_DIGEST_FILES = 512
MAX_DIGEST_BYTES = 16 * 1024 * 1024


def skill_digest(found: DiscoveredSkill) -> str:
    """sha256 over every regular file in the skill's own folder (the
    directory holding its `SKILL.md`), keyed by relative path so moving a
    file within the folder changes the digest even if no byte did.

    Symlinks and anything the walk cannot stat are skipped rather than
    followed — the same reasoning as `_skill_files`: a link out of the
    folder must not let an approval silently cover bytes that live
    elsewhere. Exceeding the file/byte bound is reported as an `error:`
    digest instead of raising, so a caller building a review page gets a
    stable (if uninformative) string rather than a crash on an oversized
    folder.
    """
    folder = os.path.dirname(found.path)
    try:
        root = os.path.realpath(folder)
    except OSError:
        return "error:unreadable-folder"
    entries: List[Tuple[str, str]] = []
    total = 0
    count = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(base, d)))
        for name in sorted(files):
            path = os.path.join(base, name)
            if os.path.islink(path):
                continue
            try:
                resolved = os.path.realpath(path)
                if os.path.commonpath([root, resolved]) != root:
                    continue
                st = os.stat(path)
                if not stat.S_ISREG(st.st_mode):
                    continue
                count += 1
                if count > MAX_DIGEST_FILES:
                    return "error:too-many-files"
                with open(path, "rb") as fh:
                    data = fh.read(MAX_DIGEST_BYTES - total + 1)
            except (OSError, ValueError):
                continue
            total += len(data)
            if total > MAX_DIGEST_BYTES:
                return "error:too-large"
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            entries.append((rel, hashlib.sha256(data).hexdigest()))
    digest = hashlib.sha256()
    for rel, filehash in sorted(entries):
        digest.update(f"{rel}:{filehash}\n".encode("utf-8"))
    return digest.hexdigest()


def load(found: DiscoveredSkill):
    """Read one discovered skill into a `Skill`. Kept separate from `discover`
    because listing what exists and paying to parse it are different costs,
    and the masterplan asks for instructions loaded on demand."""
    from services.memory.skill_format import Skill

    return Skill.from_markdown(read_markdown(found), path=found.path)
