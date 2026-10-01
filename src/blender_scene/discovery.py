"""Find a Blender executable and ask it for its version.

Order: the ``blender_path`` setting, the ``BLENDER_PATH`` environment
variable, ``PATH``, the Windows install folders (newest version first), then
the usual Linux and macOS locations. Everything the search touches (platform,
environment, file system, ``which``) can be injected, so the Windows rules are
testable on any machine.
"""
from __future__ import annotations

import ntpath
import os
import posixpath
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from . import scene_common as common

#: Versions the project knows how to look for when a folder cannot be listed.
KNOWN_WINDOWS_VERSIONS = ("5.0", "4.5", "4.3", "4.0", "3.6")

NOT_FOUND_MESSAGE = (
    "Blender was not found. Install it from blender.org, or point Faustus to it with the "
    "'blender_path' setting or the BLENDER_PATH environment variable (the blender executable, "
    "or the folder that contains it).")


@dataclass(frozen=True)
class Candidate:
    source: str      # setting | env | path | windows | common
    path: str


def _version_key(name: str) -> Tuple[int, int, int]:
    return common.parse_version(name) or (0, 0, 0)


def _exe_names(pathmod, platform: str) -> Tuple[str, ...]:
    if platform == "win32":
        return ("blender.exe",)
    if platform == "darwin":
        return ("Blender", "blender")
    return ("blender",)


def _expand(raw: str, source: str, pathmod, platform: str, isdir, isfile) -> List[Candidate]:
    """A configured value may be the executable, its folder, or a macOS .app."""
    raw = (raw or "").strip().strip('"')
    if not raw:
        return []
    out = [Candidate(source, raw)]
    if isdir(raw):
        for exe in _exe_names(pathmod, platform):
            out.append(Candidate(source, pathmod.join(raw, exe)))
        out.append(Candidate(source, pathmod.join(raw, "Contents", "MacOS", "Blender")))
    return out


def candidates(
    setting: str = "",
    env: Optional[Mapping[str, str]] = None,
    platform: Optional[str] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    listdir: Callable[[str], List[str]] = os.listdir,
    isdir: Callable[[str], bool] = os.path.isdir,
    isfile: Callable[[str], bool] = os.path.isfile,
    home: Optional[str] = None,
) -> List[Candidate]:
    """Every place Blender might be, in the order they are tried."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    win = platform == "win32"
    pathmod = ntpath if win else posixpath
    out: List[Candidate] = []

    out += _expand(setting, "setting", pathmod, platform, isdir, isfile)
    out += _expand(env.get("BLENDER_PATH", ""), "env", pathmod, platform, isdir, isfile)
    for name in ("blender", "blender.exe") if win else ("blender",):
        found = which(name)
        if found:
            out.append(Candidate("path", found))
            break

    if win:
        bases: List[str] = []
        for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            value = env.get(key)
            if value:
                bases.append(value)
        bases.append(r"C:\Program Files")
        local = env.get("LOCALAPPDATA")
        if local:
            bases.append(ntpath.join(local, "Programs"))
        versions: List[Tuple[Tuple[int, int, int], str]] = []
        listed_any = False
        for base in dict.fromkeys(bases):
            folder = ntpath.join(base, "Blender Foundation")
            try:
                entries = listdir(folder)
            except OSError:
                entries = []
            for entry in entries:
                if entry.lower().startswith("blender"):
                    listed_any = True
                    versions.append((_version_key(entry), ntpath.join(folder, entry, "blender.exe")))
        if not listed_any:
            for ver in KNOWN_WINDOWS_VERSIONS:
                versions.append((_version_key(ver), ntpath.join(
                    r"C:\Program Files\Blender Foundation", f"Blender {ver}", "blender.exe")))
        for _, path in sorted(versions, key=lambda t: t[0], reverse=True):
            out.append(Candidate("windows", path))
    else:
        for path in ("/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender",
                     "/var/lib/flatpak/exports/bin/org.blender.Blender",
                     "/Applications/Blender.app/Contents/MacOS/Blender"):
            out.append(Candidate("common", path))
        scan: List[Tuple[Tuple[int, int, int], str]] = []
        roots = ["/opt", "/usr/local", "/Applications"]
        if home:
            roots += [home, posixpath.join(home, "Applications"), posixpath.join(home, "opt")]
        for root in roots:
            try:
                entries = listdir(root)
            except OSError:
                continue
            for entry in entries:
                low = entry.lower()
                if not low.startswith("blender"):
                    continue
                full = posixpath.join(root, entry)
                if low.endswith(".app"):
                    scan.append((_version_key(entry), posixpath.join(full, "Contents", "MacOS", "Blender")))
                else:
                    scan.append((_version_key(entry), posixpath.join(full, "blender")))
        for _, path in sorted(scan, key=lambda t: t[0], reverse=True):
            out.append(Candidate("common", path))

    unique: List[Candidate] = []
    seen = set()
    for cand in out:
        key = pathmod.normcase(cand.path)
        if key not in seen:
            seen.add(key)
            unique.append(cand)
    return unique


def configured_path() -> str:
    try:
        from src.settings import get_setting
        return str(get_setting("blender_path", "") or "")
    except Exception:  # noqa: BLE001 - discovery must work without the settings store
        return ""


def find_blender(
    setting: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    platform: Optional[str] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    listdir: Callable[[str], List[str]] = os.listdir,
    isdir: Callable[[str], bool] = os.path.isdir,
    isfile: Callable[[str], bool] = os.path.isfile,
    home: Optional[str] = None,
) -> Tuple[Optional[Candidate], List[Candidate]]:
    """``(first candidate that is a file or None, all candidates tried)``."""
    if setting is None:
        setting = configured_path()
    if home is None:
        home = os.path.expanduser("~")
    cands = candidates(setting, env, platform, which, listdir, isdir, isfile, home)
    for cand in cands:
        if isfile(cand.path):
            return cand, cands
    return None, cands


_VERSION_CACHE: Dict[Tuple[str, float], Optional[str]] = {}


def read_version(path: str, timeout: float = 30.0,
                 runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run) -> Optional[str]:
    """Run ``blender --version`` and return e.g. ``"4.3.2"`` (cached per file)."""
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        stamp = 0.0
    key = (path, stamp)
    if runner is subprocess.run and key in _VERSION_CACHE:
        return _VERSION_CACHE[key]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = runner([path, "--version"], capture_output=True, text=True, timeout=timeout,
                      creationflags=flags, stdin=subprocess.DEVNULL)
        text = (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"Blender\s+(\d+\.\d+(?:\.\d+)?)", text)
    version = match.group(1) if match else None
    if runner is subprocess.run:
        _VERSION_CACHE[key] = version
    return version


def probe(**kwargs) -> dict:
    """Where Blender is and which version; never raises."""
    runner = kwargs.pop("runner", subprocess.run)
    found, tried = find_blender(**kwargs)
    notes: List[str] = []
    setting = kwargs.get("setting")
    if setting is None:
        setting = configured_path()
    if (setting or "").strip() and not (found and found.source == "setting"):
        notes.append(f"the blender_path setting ({setting!r}) does not point to a Blender executable")
    env = os.environ if kwargs.get("env") is None else kwargs["env"]
    if (env.get("BLENDER_PATH") or "").strip() and not (found and found.source == "env"):
        notes.append("the BLENDER_PATH environment variable does not point to a Blender executable")
    if found is None:
        return {"found": False, "message": NOT_FOUND_MESSAGE, "notes": notes,
                "tried": [c.path for c in tried][:20]}
    version = read_version(found.path, runner=runner)
    if version is None:
        return {"found": False, "path": found.path, "source": found.source,
                "message": f"{found.path} exists but did not answer 'blender --version'; "
                           "it may be broken or not a Blender executable.",
                "notes": notes, "tried": [c.path for c in tried][:20]}
    vt = common.parse_version(version)
    return {
        "found": True, "path": found.path, "source": found.source,
        "version": version, "version_tuple": list(vt) if vt else None,
        "engines": {e: common.engine_name(e, vt) for e in ("eevee", "cycles", "workbench")},
        "message": f"Blender {version} at {found.path} (found via {found.source})",
        "notes": notes,
    }
