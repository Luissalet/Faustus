"""Which folders and files an app may read or write on behalf of the user (or of an assistant), and path hygiene.

Standard library only. Replaces the two forks of ``paths.py`` (Kafka, Pygmalion) and the ``is_dir()``-only checks
of Borges and Vulcan, which accepted ``C:\\``, ``C:\\Windows`` or the whole profile folder (with ``.ssh``) as an
indexing root and did not strip the quotes Explorer's "Copy as path" adds.

* :func:`clean_user_path` — strip spaces and quotes, expand ``~`` and environment variables.
* :func:`unsafe_folder`, :func:`unsafe_file`, :func:`unsafe_output_dir` — ``None`` when the path may be used,
  otherwise the reason (Spanish by default, ``lang="en"`` for English; :func:`reason` gives either by code).
* :func:`is_inside`, :func:`safe_member` (zip-slip), :func:`hidden_parts`.

**Windows paths work on any platform.** A string that looks like a Windows path (``C:\\...``, ``\\\\server\\share``)
is analysed as one even on Linux, purely by its text: case-insensitive, ``..`` collapsed, trailing dots and spaces
ignored (Windows strips them), no filesystem access. In that mode the *policy* checks (drive root, profile root,
system folders, hidden/secret folders, the app's own data folder) still run, and the existence check is skipped.
On Windows itself the real filesystem is used (``resolve()``), so ``PROGRA~1`` and junctions are seen through.

Policy is checked **before** existence, so ``C:\\Windows`` is "a system folder" whether or not it exists here.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import tempfile
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Optional, Sequence, Union

__all__ = [
    "clean_user_path", "unsafe_folder", "unsafe_file", "unsafe_output_dir", "is_inside", "safe_member", "hidden_parts",
    "reason", "REASONS", "SYSTEM_PARTS_WIN", "SYSTEM_PARTS_POSIX", "SECRET_NAMES", "HIDDEN_PARTS",
]

PathLike = Union[str, "os.PathLike[str]"]

SYSTEM_PARTS_WIN = frozenset({"windows", "windows.old", "program files", "program files (x86)", "programdata", "$recycle.bin",
                              "system volume information", "recovery", "$windows.~bt"})
SYSTEM_PARTS_POSIX = frozenset({"etc", "usr", "bin", "sbin", "lib", "lib64", "proc", "sys", "dev", "boot", "root", "var", "run", "opt", "snap"})
SECRET_NAMES = re.compile(r"(^\.env($|\.)|mcp-token|^id_(rsa|ed25519|ecdsa|dsa)|\.pem$|\.key$|\.p12$|\.pfx$|\.kdbx$|credentials|secrets?\.|\.ssh$|\.htpasswd)", re.I)
HIDDEN_PARTS = frozenset({".git", ".ssh", ".gnupg", ".aws", ".config", "appdata", "node_modules", "__pycache__"})
_HOME_PARENTS_POSIX = ("home", "users")
_TEMP_SEQ_WIN = ("appdata", "local", "temp")

REASONS: dict[str, dict[str, str]] = {
    "path_invalid": {"es": "no es una ruta válida", "en": "not a valid path"},
    "path_relative": {"es": "la ruta debe ser absoluta", "en": "the path must be absolute"},
    "path_unresolved": {"es": "no se puede resolver la ruta", "en": "the path cannot be resolved"},
    "folder_missing": {"es": "la carpeta no existe", "en": "the folder does not exist"},
    "file_missing": {"es": "el archivo no existe", "en": "the file does not exist"},
    "path_is_file": {"es": "esa ruta es un archivo, no una carpeta", "en": "that path is a file, not a folder"},
    "path_root": {"es": "la raíz de una unidad es demasiado amplia", "en": "a drive root is too broad"},
    "path_home": {"es": "la carpeta de usuario es demasiado amplia: elige una subcarpeta", "en": "the user profile folder is too broad: pick a subfolder"},
    "path_system": {"es": "no se permiten carpetas del sistema", "en": "system folders are not allowed"},
    "path_own_data_folder": {"es": "la carpeta de datos de la propia app no se puede usar", "en": "the app's own data folder cannot be used"},
    "path_contains_own_data": {"es": "esa carpeta contiene la carpeta de datos de la propia app", "en": "that folder contains the app's own data folder"},
    "path_hidden": {"es": "no se permiten carpetas de configuración u ocultas", "en": "configuration and hidden folders are not allowed"},
    "path_credentials": {"es": "parece un archivo de credenciales", "en": "that looks like a credentials file"},
}


def reason(code: str, lang: str = "es") -> str:
    """The text of a reason code in ``lang`` (``es`` or ``en``; anything else falls back to English)."""
    entry = REASONS.get(code, {})
    return entry.get("es" if str(lang).lower().startswith("es") else "en") or entry.get("en") or code


# ------------------------------------------------------------------ cleaning

_ENV_PCT = re.compile(r"%([A-Za-z_][A-Za-z0-9_()]*)%")
_QUOTES = "\"'\u201c\u201d\u2018\u2019`"


def clean_user_path(raw: Any) -> str:
    """What the user pasted, made usable: surrounding whitespace and quote pairs removed (Explorer's "Copy as
    path" yields ``"C:\\My Folder\\file.txt"``), ``~`` and ``$VAR`` / ``%VAR%`` expanded (an undefined variable is
    left as typed). ``None`` gives ``""``. Interior characters are never touched."""
    if raw is None:
        return ""
    s = str(raw).strip()
    for _ in range(3):
        if len(s) >= 2 and s[0] in _QUOTES and s[-1] in _QUOTES:
            s = s[1:-1].strip()
        else:
            break
    if not s:
        return ""
    s = os.path.expandvars(s)
    if os.name != "nt":   # %VAR% is what a Windows user types; os.path.expandvars only knows it on Windows
        s = _ENV_PCT.sub(lambda m: os.environ.get(m.group(1), m.group(0)), s)
    if s.startswith("~"):
        s = os.path.expanduser(s)
    return s


# ------------------------------------------------------------------ parsing into a flavour-neutral view

class _Parsed:
    """A path reduced to what the policy needs. ``low`` are lowercase components (anchor first); ``real`` is a
    resolved ``Path`` when the filesystem can be consulted (None for a Windows path analysed on another OS)."""

    __slots__ = ("win", "low", "shown", "real", "unc", "name")

    def __init__(self, win: bool, low: tuple[str, ...], shown: tuple[str, ...], real: Optional[Path], unc: bool):
        self.win, self.low, self.shown, self.real, self.unc = win, low, shown, real, unc
        self.name = shown[-1] if shown else ""


def _looks_windows(s: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:([\\/]|$)", s)) or s.startswith("\\\\") or s.startswith("//")


def _win_low(parts: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    shown, low = [], []
    for i, part in enumerate(parts):
        if i > 0:
            stripped = part.rstrip(". ")       # Windows ignores trailing dots and spaces
            part = stripped or part
        shown.append(part)
        low.append(part.lower())
    return tuple(low), tuple(shown)


def _parse(path: Any, *, resolve: bool = True) -> tuple[Optional[_Parsed], Optional[str]]:
    """(parsed, None) or (None, reason code)."""
    try:
        s = clean_user_path(os.fspath(path) if isinstance(path, os.PathLike) else path)
    except (TypeError, ValueError):
        return None, "path_invalid"
    if not s or "\x00" in s:
        return None, "path_invalid"
    if s.startswith("\\\\?\\") and _looks_windows(s[4:]):
        s = s[4:]
    on_nt = os.name == "nt"
    if on_nt and not resolve and s.startswith("/") and not _looks_windows(s):
        parts = PurePosixPath(posixpath.normpath(s)).parts
        return _Parsed(False, tuple(x.lower() for x in parts), parts, None, False), None
    if on_nt or _looks_windows(s):
        if on_nt:
            try:
                p = Path(s).expanduser()
            except (OSError, ValueError, RuntimeError):
                return None, "path_invalid"
            if not p.is_absolute():
                return None, "path_relative"
            if resolve:
                try:
                    p = p.resolve()
                except (OSError, RuntimeError):
                    return None, "path_unresolved"
            text = str(p)
            if text.startswith("\\\\?\\UNC\\"):
                text = "\\\\" + text[8:]
            elif text.startswith("\\\\?\\") and _looks_windows(text[4:]):
                text = text[4:]
            pure = PureWindowsPath(text)
            real: Optional[Path] = p
        else:
            pure = PureWindowsPath(ntpath.normpath(s))
            if not pure.is_absolute():
                return None, "path_relative"
            real = None
        low, shown = _win_low(pure.parts)
        unc = bool(pure.drive.startswith("\\\\"))
        return _Parsed(True, low, shown, real, unc), None
    try:
        p = Path(s).expanduser()
    except (OSError, ValueError, RuntimeError):
        return None, "path_invalid"
    if not p.is_absolute():
        return None, "path_relative"
    if resolve:
        try:
            p = p.resolve()
        except (OSError, RuntimeError):
            return None, "path_unresolved"
    else:
        p = Path(posixpath.normpath(str(p)))
    parts = p.parts
    return _Parsed(False, tuple(x.lower() for x in parts), tuple(parts), p, False), None


def _starts_with(low: Sequence[str], prefix: Sequence[str]) -> bool:
    return len(low) >= len(prefix) and tuple(low[:len(prefix)]) == tuple(prefix)


def _temp_low() -> Optional[tuple[str, ...]]:
    try:
        return tuple(x.lower() for x in Path(tempfile.gettempdir()).resolve().parts)
    except (OSError, RuntimeError):
        return None


def _hidden_in(parsed: _Parsed, *, skip_last: bool = False) -> list[str]:
    """Hidden/configuration-like folder names among the components, in order. Inside the system temp folder (which
    lives under AppData on Windows) only the components below it count, so a document saved to Temp can be used."""
    low = list(parsed.low)
    start = 1                                   # the anchor is never a hidden name
    temp = _temp_low() if parsed.real is not None else None
    if temp and _starts_with(low, temp):
        start = len(temp)
    elif parsed.win:
        for i in range(len(low) - len(_TEMP_SEQ_WIN) + 1):
            if tuple(low[i:i + len(_TEMP_SEQ_WIN)]) == _TEMP_SEQ_WIN:
                start = i + len(_TEMP_SEQ_WIN)
                break
    scan = low[start:-1] if skip_last else low[start:]
    out: list[str] = []
    for part in scan:
        if part in HIDDEN_PARTS and part not in out:
            out.append(part)
    return out


def _is_root(parsed: _Parsed) -> bool:
    return len(parsed.low) <= 1


def _is_home_like(parsed: _Parsed) -> bool:
    low = parsed.low
    if parsed.win:
        if parsed.unc:
            return False
        if len(low) == 2 and low[1] == "users":
            return True
        if len(low) == 3 and low[1] == "users":
            return True
    else:
        if len(low) == 2 and low[1] in _HOME_PARENTS_POSIX:
            return True
        if len(low) == 3 and low[1] in _HOME_PARENTS_POSIX:
            return True
    try:
        home = Path.home().resolve() if parsed.real is not None else None
    except (OSError, RuntimeError):
        home = None
    if home is not None and parsed.real is not None:
        hl = tuple(x.lower() for x in home.parts)
        if tuple(low) == hl:
            return True
    return False


def _in_system(parsed: _Parsed, data_dir: Optional[_Parsed], *, parent_only: bool = False) -> bool:
    low = parsed.low[:-1] if parent_only else parsed.low
    if parsed.win:
        return len(low) >= 2 and low[1] in SYSTEM_PARTS_WIN
    if len(low) >= 2 and low[1] in SYSTEM_PARTS_POSIX:
        # /var/tmp, /var/folders and the like hold temp files; the real system trees do not
        if low[1] == "var" and len(low) >= 3 and low[2] in ("tmp", "folders"):
            return False
        if data_dir is not None and _starts_with(parsed.low, data_dir.low):
            return False                      # the app's own data lives there on purpose
        return True
    return False


def _inside_low(child: Sequence[str], parent: Sequence[str]) -> bool:
    return len(parent) > 0 and _starts_with(child, parent)


def _subdirs(data: Optional[_Parsed], allow: Any) -> list[tuple[str, ...]]:
    """The allowed subfolders of the data folder as lowercase components (relative names resolve against it)."""
    if allow is None or data is None:
        return []
    items: Iterable[Any] = [allow] if isinstance(allow, (str, os.PathLike)) else allow
    out = []
    for item in items:
        if item is None:
            continue
        text = os.fspath(item)
        if _looks_windows(text) or os.path.isabs(text):
            sub, err = _parse(text, resolve=data.real is not None)
            if sub is not None:
                out.append(sub.low)
        else:
            tail = tuple(x.lower() for x in re.split(r"[\\/]+", text.strip("\\/")) if x)
            out.append(data.low + tail)
    return out


def _own_data_code(parsed: _Parsed, data: Optional[_Parsed], allow: list[tuple[str, ...]], *, file: bool) -> Optional[str]:
    if data is None:
        return None
    if _inside_low(parsed.low, data.low):
        if any(_inside_low(parsed.low, a) for a in allow):
            return None
        return "path_own_data_folder"
    if not file and _inside_low(data.low, parsed.low):
        return "path_contains_own_data"
    return None


def _data_parsed(data_dir: Any, like: _Parsed) -> Optional[_Parsed]:
    if data_dir is None or str(data_dir) == "":
        return None
    parsed, err = _parse(data_dir, resolve=like.real is not None)
    return parsed if parsed is not None and parsed.win == like.win else None


# ------------------------------------------------------------------ the three policies

def _folder_code(path: Any, data_dir: Any, allow: Any) -> Optional[str]:
    p, err = _parse(path)
    if p is None:
        return err
    if _is_root(p):
        return "path_root"
    if _is_home_like(p):
        return "path_home"
    data = _data_parsed(data_dir, p)
    if _in_system(p, data):
        return "path_system"
    code = _own_data_code(p, data, _subdirs(data, allow), file=False)
    if code:
        return code
    if _hidden_in(p):
        return "path_hidden"
    if p.real is not None:
        if p.real.is_file():
            return "path_is_file"
        if not p.real.is_dir():
            return "folder_missing"
    return None


def _file_code(path: Any, data_dir: Any, allow: Any) -> Optional[str]:
    p, err = _parse(path)
    if p is None:
        return err
    if SECRET_NAMES.search(p.name):
        return "path_credentials"
    data = _data_parsed(data_dir, p)
    if _in_system(p, data, parent_only=True):
        return "path_system"
    code = _own_data_code(p, data, _subdirs(data, allow), file=True)
    if code:
        return code
    if _hidden_in(p, skip_last=True):
        return "path_hidden"
    if p.real is not None and not p.real.is_file():
        return "file_missing"
    return None


def _output_code(path: Any, data_dir: Any, allow: Any) -> Optional[str]:
    p, err = _parse(path)
    if p is None:
        return err
    if _is_root(p):
        return "path_root"
    data = _data_parsed(data_dir, p)
    if _in_system(p, data):
        return "path_system"
    code = _own_data_code(p, data, _subdirs(data, allow), file=True)
    if code:
        return code
    if _hidden_in(p):
        return "path_hidden"
    if p.real is not None and p.real.exists() and not p.real.is_dir():
        return "path_is_file"
    return None


def unsafe_folder(path: Any, *, data_dir: Any = None, allow_data_subdir: Any = None, lang: str = "es") -> Optional[str]:
    """``None`` when the folder may be read or watched; otherwise the reason it must not.

    Refused: a drive root, the user profile folder (and ``C:\\Users`` / ``/home`` themselves), system folders
    (``C:\\Windows``, ``Program Files``, ``ProgramData``, ``/etc``, ``/usr`` ...), configuration and secret folders
    anywhere in the path (``.ssh``, ``.gnupg``, ``.aws``, ``.git``, ``AppData``, ``node_modules`` ...), the app's own
    ``data_dir`` (except ``allow_data_subdir``: one path or a list, absolute or relative to ``data_dir``) and any
    folder that contains it, a relative path, and a folder that does not exist. The input goes through
    :func:`clean_user_path` first.
    """
    code = _folder_code(path, data_dir, allow_data_subdir)
    return None if code is None else reason(code, lang)


def unsafe_file(path: Any, *, data_dir: Any = None, allow_data_subdir: Any = None, lang: str = "es") -> Optional[str]:
    """``None`` when the file may be read; otherwise the reason (credentials-looking names, system or hidden
    folders above it, the app's own data folder except ``allow_data_subdir``, missing file)."""
    code = _file_code(path, data_dir, allow_data_subdir)
    return None if code is None else reason(code, lang)


def unsafe_output_dir(path: Any, *, data_dir: Any = None, allow_data_subdir: Any = None, lang: str = "es") -> Optional[str]:
    """``None`` when files may be written into this folder (it need not exist yet); otherwise the reason (a drive
    root, system or hidden folders, the app's own data folder except ``allow_data_subdir``, an existing file)."""
    code = _output_code(path, data_dir, allow_data_subdir)
    return None if code is None else reason(code, lang)


# ------------------------------------------------------------------ containment and archives

def is_inside(child: Any, parent: Any) -> bool:
    """True when ``child`` is ``parent`` or below it. Both are resolved (symlinks, ``..``); Windows-looking paths
    are compared case-insensitively. Mixed flavours and unresolvable paths give False."""
    c, _ = _parse(child)
    p, _ = _parse(parent)
    if c is None or p is None or c.win != p.win:
        return False
    return _inside_low(c.low, p.low)


def safe_member(base: PathLike, name: str) -> Path:
    """The destination for archive member ``name`` under ``base``, or ``ValueError`` (zip-slip: absolute names,
    drive letters, ``..`` that escape, NUL, or a symlink that leads outside). Both separators are accepted."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError(f"unsafe archive member name: {name!r}")
    norm = name.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        raise ValueError(f"absolute archive member name: {name!r}")
    parts = [x for x in norm.split("/") if x not in ("", ".")]
    depth = 0
    for part in parts:
        depth += -1 if part == ".." else 1
        if depth < 0:
            raise ValueError(f"archive member escapes its folder: {name!r}")
    if not parts or all(x == ".." for x in parts):
        raise ValueError(f"unsafe archive member name: {name!r}")
    root = Path(base).resolve()
    target = root.joinpath(*parts).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"archive member escapes its folder: {name!r}") from None
    return target


def hidden_parts(path: Any) -> list[str]:
    """The configuration-like folder names (``.git``, ``.ssh``, ``AppData``, ``node_modules`` ...) among the
    components of ``path``, lowercase, in order, without repeats. Pure: it never touches the disk. Components
    inside the system temp folder do not count."""
    if isinstance(path, PurePath) and not isinstance(path, Path):
        text = str(path)
    else:
        text = os.fspath(path) if isinstance(path, os.PathLike) else str(path)
    p, _ = _parse(text, resolve=False)
    if p is None:
        return []
    return _hidden_in(p)
