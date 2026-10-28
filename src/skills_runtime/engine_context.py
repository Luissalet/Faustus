"""skills_runtime/engine_context.py - which game engine the workspace uses.

The hybrid skill selector ranks skills by what the user wrote. "Add a
Rigidbody" names its engine; "make the player collide with the walls" does
not, and a library that holds both a Unity and a Godot physics skill then
offers whichever happens to score higher. This module answers one cheap
question - does the workspace hold a Unity, Godot or Unreal project - so the
selector can prefer skills tagged with that engine and de-prioritise the
other engines' skills. It reads directory names and file names only.

Skills opt in through their tags: a skill tagged `unity`, `godot` or `unreal`
is an engine skill; every other skill is untouched. With no engine detected
the selector behaves exactly as before.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

ENGINE_TAGS: Tuple[str, ...] = ("unity", "godot", "unreal")

#: Multipliers the selector applies to an engine skill's score.
MATCH_BOOST = 1.5
MISMATCH_FACTOR = 0.25

_SKIP_DIRS = {"library", "temp", "obj", "logs", "build", "builds", "node_modules",
              ".git", "usersettings", ".godot", ".import", "binaries", "intermediate",
              "saved", "deriveddatacache"}
_MAX_DEPTH = 3
_MAX_DIRS = 400
_TTL_SECONDS = 120.0

_LOCK = threading.Lock()
_CACHE: Dict[str, Tuple[float, Tuple[str, ...]]] = {}


def _engines_in(folder: str, names: Sequence[str]) -> List[str]:
    found: List[str] = []
    if "Assets" in names and "ProjectSettings" in names:
        found.append("unity")
    if "project.godot" in names:
        found.append("godot")
    if any(n.lower().endswith(".uproject") for n in names):
        found.append("unreal")
    return found


def detect_engines(workspace: Optional[str], *, use_cache: bool = True) -> List[str]:
    """Engines of the projects at or under `workspace` (shallow, bounded)."""
    try:
        root = os.path.realpath(os.path.expanduser(workspace or ""))
    except Exception:  # noqa: BLE001
        return []
    if not root or not os.path.isdir(root):
        return []
    now = time.monotonic()
    if use_cache:
        with _LOCK:
            hit = _CACHE.get(root)
        if hit and now - hit[0] < _TTL_SECONDS:
            return list(hit[1])

    found: List[str] = []
    frontier: List[Tuple[str, int]] = [(root, 0)]
    visited = 0
    while frontier and visited < _MAX_DIRS:
        folder, depth = frontier.pop(0)
        visited += 1
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        here = _engines_in(folder, names)
        for engine in here:
            if engine not in found:
                found.append(engine)
        if here or depth >= _MAX_DEPTH:
            continue  # a project folder is a leaf: do not walk into its assets
        for name in sorted(names):
            if name.lower() in _SKIP_DIRS or name.startswith("."):
                continue
            child = os.path.join(folder, name)
            if os.path.isdir(child):
                frontier.append((child, depth + 1))
    with _LOCK:
        _CACHE[root] = (now, tuple(found))
    return found


def skill_engine(skill: Dict) -> str:
    """The engine a skill is tagged for, or "" for an engine-neutral skill."""
    tags = {str(t).strip().lower() for t in (skill.get("tags") or [])}
    for engine in ENGINE_TAGS:
        if engine in tags:
            return engine
    return ""


def engine_factor(skill: Dict, engines: Optional[Sequence[str]]) -> float:
    """Score multiplier for `skill` given the workspace's engines."""
    if not engines:
        return 1.0
    engine = skill_engine(skill)
    if not engine:
        return 1.0
    return MATCH_BOOST if engine in engines else MISMATCH_FACTOR


def reset_cache() -> None:
    with _LOCK:
        _CACHE.clear()
