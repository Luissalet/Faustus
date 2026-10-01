"""Standard-library helpers shared by the validator (plain Python) and the
runner (executed inside Blender's own interpreter).

This file must stay free of third-party and project imports: Blender loads it
by adding this folder to ``sys.path`` and importing it as a top-level module.
It holds the three things that both sides have to agree on exactly:

* the ``${PROJECT_ROOT}`` substitution and the rule that every path ends up
  inside the project folder (:func:`confine_path`),
* the version-dependent render engine name (:func:`engine_name`),
* parsing a Blender version string (:func:`parse_version`).
"""
from __future__ import annotations

import ntpath
import os
import posixpath
import re
from typing import Any, Optional, Tuple

ROOT_TOKEN = "${PROJECT_ROOT}"
REPORT_PREFIX = "BLENDER_SCENE_REPORT "

#: Op fields whose string value is a file path. The validator and the runner
#: both confine every one of these to the project folder.
PATH_FIELDS = frozenset({"path", "hdri"})

_WIN_DRIVE = re.compile(r"^[A-Za-z]:")
_UNKNOWN_VAR = re.compile(r"\$\{[^}]*\}")


# --------------------------------------------------------------------------
# Version and engine names
# --------------------------------------------------------------------------

def parse_version(text: Any) -> Optional[Tuple[int, int, int]]:
    """``"Blender 4.3.2"`` / ``"4.3"`` / ``(4, 3, 2)`` -> ``(4, 3, 2)``."""
    if isinstance(text, (tuple, list)):
        nums = [int(x) for x in list(text)[:3] if isinstance(x, (int, float))]
        while len(nums) < 3:
            nums.append(0)
        return (nums[0], nums[1], nums[2]) if nums else None
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(text or ""))
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def engine_name(engine: str, version: Any) -> str:
    """The ``scene.render.engine`` identifier of a logical engine.

    ``eevee`` is ``BLENDER_EEVEE`` before 4.2, ``BLENDER_EEVEE_NEXT`` from 4.2
    to 4.5 and ``BLENDER_EEVEE`` again from 5.0. ``cycles`` and ``workbench``
    never changed. Unknown engines raise ``ValueError``.
    """
    key = str(engine or "").strip().lower()
    ver = parse_version(version) or (4, 3, 0)
    if key == "eevee":
        return "BLENDER_EEVEE_NEXT" if (4, 2) <= ver[:2] < (5, 0) else "BLENDER_EEVEE"
    if key == "cycles":
        return "CYCLES"
    if key == "workbench":
        return "BLENDER_WORKBENCH"
    raise ValueError(f"unknown render engine {engine!r} (expected eevee, cycles or workbench)")


def logical_engine(internal: str) -> str:
    """The inverse of :func:`engine_name`: any internal id -> eevee/cycles/workbench."""
    up = str(internal or "").upper()
    if up.startswith("BLENDER_EEVEE"):
        return "eevee"
    if up == "CYCLES":
        return "cycles"
    if up == "BLENDER_WORKBENCH":
        return "workbench"
    return up.lower()


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def _flavor(root: str):
    """``ntpath`` for a Windows-looking root, ``posixpath`` otherwise.

    Picked from the root string, not from the platform, so the Windows rules
    can be exercised on any machine.
    """
    if _WIN_DRIVE.match(root or "") or (root or "").startswith("\\\\"):
        return ntpath
    return posixpath


def _real(pathmod, path: str) -> str:
    # Symlinks only mean something when the path flavour is the native one.
    if pathmod is os.path:
        return os.path.realpath(path)
    return pathmod.abspath(path)


def _within(pathmod, root: str, path: str) -> bool:
    r = pathmod.normcase(pathmod.normpath(root))
    p = pathmod.normcase(pathmod.normpath(path))
    if r == p:
        return False
    try:
        return pathmod.commonpath([r, p]) == r
    except ValueError:  # different drives
        return False


def substitute(text: str, root: str) -> str:
    """Replace ``${PROJECT_ROOT}`` everywhere in a string."""
    return text.replace(ROOT_TOKEN, root)


def confine_path(raw: Any, root: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve ``raw`` against ``root``; return ``(absolute_path, None)`` or
    ``(None, reason)``.

    A path is accepted only when, after ``${PROJECT_ROOT}`` substitution, it
    names something *inside* ``root``: no ``..`` segment at all, no absolute
    path outside the folder, no drive letter or UNC share when the folder is
    not a Windows one, no alternate data stream, and the real location (after
    following symbolic links that exist) is still inside the real folder.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "expected a non-empty path"
    if "\x00" in raw:
        return None, "path contains a NUL character"
    if not isinstance(root, str) or not root:
        return None, "no project root to resolve the path against"
    pathmod = _flavor(root)
    root_abs = pathmod.normpath(pathmod.abspath(root)) if pathmod is os.path else pathmod.normpath(root)

    text = raw.strip()
    if ROOT_TOKEN in text:
        if not text.startswith(ROOT_TOKEN):
            return None, f"{ROOT_TOKEN} is only allowed at the very start of a path"
        text = root_abs + text[len(ROOT_TOKEN):]
    unknown = _UNKNOWN_VAR.search(text)
    if unknown:
        return None, f"unknown variable {unknown.group(0)} (only {ROOT_TOKEN} is substituted)"

    if ".." in re.split(r"[\\/]+", text):
        return None, "'..' segments are not allowed in paths"

    absolute = bool(pathmod.isabs(text) or text.startswith(("/", "\\")) or _WIN_DRIVE.match(text))
    if pathmod is posixpath and (_WIN_DRIVE.match(text) or text.startswith("\\\\")):
        return None, f"absolute path outside the project folder: {raw}"
    if pathmod is ntpath:
        rest = text[2:] if _WIN_DRIVE.match(text) else text
        if ":" in rest:
            return None, "alternate data streams / extra ':' are not allowed in paths"

    candidate = text if absolute else pathmod.join(root_abs, text)
    candidate = pathmod.normpath(candidate)
    if not _within(pathmod, root_abs, candidate):
        if pathmod.normcase(candidate) == pathmod.normcase(root_abs):
            return None, "path must name a file inside the project folder, not the folder itself"
        return None, f"path is outside the project folder: {raw}"

    real_root = _real(pathmod, root_abs)
    real_candidate = _real(pathmod, candidate)
    if not _within(pathmod, real_root, real_candidate):
        return None, f"path leaves the project folder through a symbolic link: {raw}"
    return candidate, None
