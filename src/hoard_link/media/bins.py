"""Find, verify, report and update the external programs the family shells out to — and build yt-dlp command lines.

Six tools, one lookup (the old code found ffmpeg six different ways)::

    from hoard_link.media import bins

    ff = bins.find("ffmpeg")                  # Tool(found=True, path=..., argv=[...], version="6.1", how="path", ...)
    if ff:
        subprocess.run([*ff.argv, "-version"])
    bins.status()                             # for a settings page: what was found, how, and what was tried
    bins.update("ytdlp")                      # self-update / pip / download into $HOARD_HOME/bin

Lookup order for ``find(name)`` (each candidate is **run** with ``-version`` / ``--version`` and skipped when it does not exit 0):

1. ``HOARD_<NAME>`` (``HOARD_FFMPEG``, ``HOARD_YTDLP`` …), then the legacy per-app variables (``LINKS_FFMPEG``,
   ``LUMIERE_FFMPEG``, ``COOKHOARD_FFMPEG``, ``LINKS_YTDLP``, ``LINKS_GALLERYDL`` …) and the ``legacy_env`` the caller passes. A value
   is a path, a folder, a bare command name, ``"python -m yt_dlp"`` or a ``.py`` script (run with this interpreter).
2. For ``ffprobe``: the folder of the ``ffmpeg`` that was found (a pair from one build, not whichever ffprobe is first on ``PATH``).
3. ``$HOARD_HOME/bin`` (what :func:`update` downloads) and the caller's ``extra_dirs``.
4. ``PATH`` (``.exe``/``.com`` only on Windows), the WinGet links folder, WindowsApps, Program Files and the usual folders.
5. ``python -m yt_dlp`` / ``python -m gallery_dl`` with the current interpreter (when the module is installed).
6. ``imageio_ffmpeg``'s bundled binary (ffmpeg only).

Results are cached 30 s when found and 4 s when missing (``refresh=True`` skips the cache), so a settings page can poll.

The second half of the module is the pure, Node-twinned part (``js/hoard-commons/media.js``, same vectors in
``tests/vectors/media_ytdlp.json``): yt-dlp / gallery-dl argument builders, output parsing, failure classification
(:func:`classify_failure`), platform detection and :func:`normalize_media_url` with its SSRF check.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import math
import os
import platform as _platform
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence
from urllib.parse import quote, urlsplit

from .. import proc
from ..errors import Unavailable
from ..launch import hoard_home

__all__ = [
    "TOOL_NAMES", "Tool", "find", "status", "bin_dir", "update", "reset_cache", "install_hint", "tool_missing", "parse_command_spec",
    "ytdlp_base_args", "ytdlp_age_days", "ytdlp_stale", "OUTPUT_TEMPLATE", "COOKIE_BROWSERS", "video_selector", "cookie_args",
    "cookie_attempts", "build_ytdlp_args", "build_gallery_args", "build_probe_args", "parse_ytdlp_line", "classify_failure",
    "detect_platform", "OTHER_PLATFORM", "file_kind", "normalize_media_url", "MediaUrlError", "format_speed", "format_eta",
    "JS_RUNTIME_MIN_VERSION",
]

TTL_FOUND_S = 30.0
TTL_MISSING_S = 4.0
VERIFY_TIMEOUT_S = 10.0
INSTALL_COMMAND = "python -m pip install -U yt-dlp gallery-dl"
#: yt-dlp releases from this one on need a JavaScript runtime for YouTube (``--js-runtimes``); older ones reject the option.
JS_RUNTIME_MIN_VERSION = "2025.11.12"

_DEFS: dict[str, dict[str, Any]] = {
    "ffmpeg": {"label": "ffmpeg", "bin": "ffmpeg", "verify": ("-version",), "module": None, "pip": None,
               "legacy": ("LINKS_FFMPEG", "LUMIERE_FFMPEG", "COOKHOARD_FFMPEG", "PROSPERO_FFMPEG", "IMAGEIO_FFMPEG_EXE")},
    "ffprobe": {"label": "ffprobe", "bin": "ffprobe", "verify": ("-version",), "module": None, "pip": None,
                "legacy": ("LUMIERE_FFPROBE", "COOKHOARD_FFPROBE", "PROSPERO_FFPROBE")},
    "ytdlp": {"label": "yt-dlp", "bin": "yt-dlp", "verify": ("--version",), "module": "yt_dlp", "pip": "yt-dlp",
              "legacy": ("LINKS_YTDLP", "COOKHOARD_YTDLP")},
    "gallerydl": {"label": "gallery-dl", "bin": "gallery-dl", "verify": ("--version",), "module": "gallery_dl", "pip": "gallery-dl",
                  "legacy": ("LINKS_GALLERYDL",)},
    "piper": {"label": "piper", "bin": "piper", "verify": ("--help",), "any_exit": True, "module": None, "pip": None,
              "legacy": ("PROSPERO_PIPER",)},
    "node": {"label": "Node.js", "bin": "node", "verify": ("--version",), "module": None, "pip": None, "legacy": ()},
}
TOOL_NAMES = tuple(_DEFS)

Runner = Callable[[list[str], float], "tuple[Optional[int], str, str]"]


# ---------------------------------------------------------------- the Tool record --

@dataclass
class Tool:
    """What :func:`find` found (or did not): ``argv`` is the command prefix (``[path]`` or ``[python, "-m", "yt_dlp"]``)."""

    name: str
    path: Optional[str] = None
    argv: list[str] = field(default_factory=list)
    version: str = ""
    how: str = ""          # env | hoard-bin | extra | path | winget | system | sibling | python-module | imageio
    tried: list[dict[str, str]] = field(default_factory=list)
    source: str = ""       # the environment variable or folder it came from
    hint: str = ""         # what to do when it is missing

    @property
    def found(self) -> bool:
        return bool(self.argv)

    def __bool__(self) -> bool:
        return self.found

    def command(self, *args: Any) -> list[str]:
        """``argv`` plus ``args`` — ready for :func:`hoard_link.proc.run`."""
        return [*self.argv, *(str(a) for a in args)]

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"found": self.found, "path": self.path, "version": self.version or None, "how": self.how or None}
        if self.source:
            out["source"] = self.source
        if not self.found and self.hint:
            out["hint"] = self.hint
        if self.tried:
            out["tried"] = list(self.tried)
        return out


def tool_missing(name: str) -> Unavailable:
    """The :class:`~hoard_link.errors.Unavailable` to raise when a required program is not installed."""
    label = _DEFS.get(name, {}).get("label", name)
    return Unavailable(label, [f"{label} was not found. {install_hint(name)}"])


def install_hint(name: str, plat: Optional[str] = None) -> str:
    plat = plat or sys.platform
    env = f"HOARD_{name.upper()}"
    if name in ("ffmpeg", "ffprobe"):
        how = "winget install Gyan.FFmpeg" if plat.startswith("win") else "brew install ffmpeg" if plat == "darwin" else "sudo apt install ffmpeg"
        return f"Install ffmpeg ({how}) or set {env} to its path."
    if name == "node":
        how = "winget install OpenJS.NodeJS.LTS" if plat.startswith("win") else "brew install node" if plat == "darwin" else "sudo apt install nodejs"
        return f"Install Node.js ({how}) or set {env} to its path."
    if name == "piper":
        return f"Download Piper (https://github.com/rhasspy/piper/releases) and set {env} to piper.exe, or put it in {bin_dir()}."
    label = _DEFS.get(name, {}).get("label", name)
    return f"Install {label} with: {INSTALL_COMMAND} (or set {env} to its path)."


# ---------------------------------------------------------------- finding --

def bin_dir(create: bool = False) -> Path:
    """``$HOARD_HOME/bin`` — where :func:`update` puts downloaded programs and where every app looks first."""
    d = hoard_home() / "bin"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def parse_command_spec(spec: Any, bin_name: str = "") -> Optional[list[str]]:
    """An environment value as an argv prefix: ``"python -m yt_dlp"``, a ``.py`` script (run with this interpreter), a path, a
    folder holding ``bin_name`` or a bare command name found on PATH. ``None`` when it cannot be resolved."""
    text = str(spec or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if not text:
        return None
    mod = re.match(r"^(\S+)\s+-m\s+([\w.]+)$", text)
    if mod:
        return [mod.group(1), "-m", mod.group(2)]
    if text.lower().endswith(".py"):
        return [sys.executable, text] if Path(text).is_file() else None
    found = proc.resolve_value(text, bin_name, allow_scripts=True)
    return [found] if found else None


def _default_runner(argv: list[str], timeout: float) -> "tuple[Optional[int], str, str]":
    try:
        done = proc.run(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout:g} s"
    except (OSError, ValueError) as exc:
        return None, "", str(exc)
    return done.returncode, done.stdout or "", done.stderr or ""


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")


def _parse_version(name: str, out: str, err: str) -> str:
    text = out or err
    line = _first_line(text)
    if name in ("ffmpeg", "ffprobe"):
        m = re.search(r"(?:ffmpeg|ffprobe) version (\S+)", text, re.I)
        return m.group(1) if m else line
    return re.sub(r"^Python\s+", "", line, flags=re.I)


_CACHE: dict[tuple, tuple[float, Tool]] = {}
_CACHE_LOCK = threading.Lock()


def reset_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _env_names(name: str, legacy_env: Iterable[str]) -> list[str]:
    names: list[str] = [f"HOARD_{name.upper()}"]
    for n in (*legacy_env, *_DEFS[name]["legacy"]):
        if n not in names:
            names.append(n)
    return names


def _fingerprint(names: list[str]) -> tuple:
    env = os.environ
    return (tuple(env.get(n) for n in names), env.get("PATH") or env.get("Path"), env.get("HOARD_HOME"), env.get("LOCALAPPDATA"))


def find(name: str, *, refresh: bool = False, legacy_env: Iterable[str] = (), extra_dirs: Iterable[Any] = (),
         runner: Optional[Runner] = None, clock: Optional[Callable[[], float]] = None) -> Tool:
    """Find one tool (``ffmpeg``, ``ffprobe``, ``ytdlp``, ``gallerydl``, ``piper``, ``node``); see the module docstring for the order.

    Never raises for a missing program: the returned :class:`Tool` is falsy, carries ``tried`` (what was attempted and why it
    failed) and ``hint``. ``runner(argv, timeout) -> (returncode | None, stdout, stderr)`` and ``clock`` are injectable for tests.
    """
    if name not in _DEFS:
        raise ValueError(f"unknown tool {name!r}; expected one of {', '.join(TOOL_NAMES)}")
    legacy = tuple(legacy_env)
    extras = tuple(str(d) for d in extra_dirs if d)
    names = _env_names(name, legacy)
    key = (name, legacy, extras, _fingerprint(names))
    now = (clock or time.monotonic)()
    if not refresh:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit and now - hit[0] < (TTL_FOUND_S if hit[1].found else TTL_MISSING_S):
            return hit[1]
    tool = _discover(name, names, extras, runner or _default_runner, clock)
    with _CACHE_LOCK:
        _CACHE[key] = (now, tool)
    return tool


def _candidates(name: str, names: list[str], extras: tuple[str, ...], tried: list[dict[str, str]],
                runner: Runner, clock: Optional[Callable[[], float]]) -> "Iterable[tuple[list[str], str, str, str]]":
    """(argv, display, how, source) in lookup order."""
    d = _DEFS[name]
    seen: set[tuple] = set()

    def emit(argv: list[str], display: str, how: str, source: str):
        k = tuple(os.path.normcase(a) for a in argv)
        if k in seen:
            return None
        seen.add(k)
        return argv, display, how, source

    for var in names:
        value = os.environ.get(var)
        if not value or not value.strip():
            continue
        argv = parse_command_spec(value, d["bin"])
        if not argv:
            tried.append({"how": "env", "path": value.strip(), "error": f"{var} does not point to anything that can run"})
            continue
        item = emit(argv, value.strip(), "env", var)
        if item:
            yield item
    if name == "ffprobe":
        ffm = find("ffmpeg", runner=runner, clock=clock)
        if ffm.found and ffm.path and len(ffm.argv) == 1:
            side = proc.exe_in_dir(Path(ffm.path).parent, "ffprobe", False)
            if side:
                item = emit([side], side, "sibling", str(Path(side).parent))
                if item:
                    yield item
    home_bin = bin_dir()
    for path, how in proc.exe_candidates(d["bin"], extra_dirs=[home_bin, *extras]):
        if how == "extra" and Path(path).parent == home_bin:
            how = "hoard-bin"
        item = emit([path], path, how, str(Path(path).parent) if how in ("hoard-bin", "extra") else "")
        if item:
            yield item
    module = d.get("module")
    if module and not getattr(sys, "frozen", False) and importlib.util.find_spec(module) is not None:
        item = emit([sys.executable, "-m", module], f"{sys.executable} -m {module}", "python-module", "")
        if item:
            yield item
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg  # type: ignore

            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # noqa: BLE001 - not installed, or no bundled binary
            exe = ""
        if exe:
            item = emit([exe], exe, "imageio", "")
            if item:
                yield item


def _discover(name: str, names: list[str], extras: tuple[str, ...], runner: Runner, clock: Optional[Callable[[], float]]) -> Tool:
    d = _DEFS[name]
    tried: list[dict[str, str]] = []
    for argv, display, how, source in _candidates(name, names, extras, tried, runner, clock):
        rc, out, err = runner([*argv, *d["verify"]], VERIFY_TIMEOUT_S)
        ok = rc is not None and (rc == 0 or d.get("any_exit"))
        if ok:
            return Tool(name, display, argv, _parse_version(name, out, err), how, tried, source)
        tried.append({"how": how, "path": display, "error": _first_line(err) or (f"exited with code {rc}" if rc is not None else "could not start")})
    return Tool(name, None, [], "", "", tried, "", install_hint(name))


def status(*, refresh: bool = False) -> dict[str, Any]:
    """Everything a settings page needs: one entry per tool (found, path, version, how, tried, hint), Python, the bin folder."""
    out: dict[str, Any] = {name: find(name, refresh=refresh).public() for name in TOOL_NAMES}
    out["python"] = {"found": True, "path": sys.executable, "version": _platform.python_version()}
    out["bin_dir"] = str(bin_dir())
    out["install_command"] = INSTALL_COMMAND
    out["platform"] = sys.platform
    return out


# ---------------------------------------------------------------- updating --

_RELEASES: dict[str, dict[str, Any]] = {
    "ytdlp": {
        "base": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/",
        "asset": {"win": "yt-dlp.exe", "darwin": "yt-dlp_macos", "linux": "yt-dlp_linux"},
        "sums": "SHA2-256SUMS",
    },
    "gallerydl": {
        "base": "https://github.com/mikf/gallery-dl/releases/latest/download/",
        "asset": {"win": "gallery-dl.exe", "linux": "gallery-dl.bin"},
        "sums": None,
    },
}


def _asset_for(name: str) -> Optional[str]:
    assets = _RELEASES.get(name, {}).get("asset", {})
    key = "win" if sys.platform.startswith("win") else "darwin" if sys.platform == "darwin" else "linux"
    asset = assets.get(key)
    if asset and key == "linux" and name == "ytdlp" and _platform.machine().lower() in ("aarch64", "arm64"):
        asset = "yt-dlp_linux_aarch64"
    return asset


def _default_fetch(url: str, timeout: float, max_bytes: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "hoard-link/0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https URL fixed in _RELEASES
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("the download is larger than expected")
    return data


def _replace_file(src: Path, dst: Path) -> None:
    """``os.replace`` that survives Windows briefly locking the target (antivirus, a still-exiting process)."""
    for attempt in range(6):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.4)


def _tail(text: str, lines: int = 6) -> str:
    return "\n".join(str(text or "").strip().splitlines()[-lines:])[-600:]


def _pip_available() -> bool:
    return not getattr(sys, "frozen", False) and importlib.util.find_spec("pip") is not None


def _download_release(name: str, fetch: Callable[[str, float, int], bytes], verify: bool) -> "tuple[bool, str, str]":
    """Download the standalone release into ``$HOARD_HOME/bin``. Returns (ok, method, output)."""
    rel = _RELEASES.get(name)
    asset = _asset_for(name)
    if not rel or not asset:
        return False, "download", f"there is no standalone release of {_DEFS[name]['label']} for this platform"
    method = f"download {asset}"
    try:
        data = fetch(rel["base"] + asset, 180.0, 120 * 1024 * 1024)
    except Exception as exc:  # noqa: BLE001 - network errors of every kind
        return False, method, f"download failed: {exc}"
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    if verify and rel.get("sums"):
        try:
            sums = fetch(rel["base"] + rel["sums"], 30.0, 1024 * 1024).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            return False, method, f"could not fetch the checksum list ({exc}); pass verify_sha=False to skip the check"
        expected = {parts[-1].lstrip("*"): parts[0].lower() for parts in (ln.split() for ln in sums.splitlines()) if len(parts) >= 2}
        if expected.get(asset) != digest:
            return False, method, f"checksum mismatch for {asset}: expected {expected.get(asset)!r}, got {digest}"
    target = bin_dir(create=True) / (_DEFS[name]["bin"] + (".exe" if sys.platform.startswith("win") else ""))
    tmp = target.with_name(target.name + f".{os.getpid()}.part")
    try:
        tmp.write_bytes(data)
        if not sys.platform.startswith("win"):
            tmp.chmod(0o755)
        _replace_file(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False, method, f"could not write {target}: {exc}"
    return True, method, f"saved {target} (sha256 {digest[:12]}…)"


def update(name: str = "ytdlp", *, method: str = "auto", runner: Optional[Runner] = None,
           fetch: Optional[Callable[[str, float, int], bytes]] = None, verify_sha: bool = True) -> dict[str, Any]:
    """Update ``ytdlp`` or ``gallerydl``. Returns ``{tool, ok, method, before, after, updated, output, error?}``.

    ``method="auto"``: a standalone binary updates itself (``-U``); when that says it came from a package manager, or the tool runs
    as a Python module or is missing, ``pip install -U`` (when pip exists), else the release is downloaded into
    ``$HOARD_HOME/bin`` (checked against the published SHA-256 list unless ``verify_sha=False``). ``"self"``, ``"pip"`` and
    ``"download"`` force one way. ``runner`` and ``fetch(url, timeout, max_bytes) -> bytes`` are injectable for tests.
    """
    if name not in ("ytdlp", "gallerydl"):
        return {"tool": name, "ok": False, "updated": False, "error": f"{name} is not updated from here"}
    d = _DEFS[name]
    run_ = runner or _default_runner
    before = find(name, refresh=True, runner=run_)
    how = ""
    output = ""
    ok = False

    def pip() -> "tuple[bool, str, str]":
        if not _pip_available():
            return False, "pip", "pip is not available in this Python"
        rc, out, err = run_([sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check", d["pip"]], 300.0)
        return rc == 0, f"pip install -U {d['pip']}", _tail(out + "\n" + err)

    choice = method
    if choice == "auto":
        if before.found and before.how != "python-module" and name == "ytdlp":
            choice = "self"
        elif _pip_available():
            choice = "pip"
        else:
            choice = "download"
    if choice == "self":
        if not before.found:
            ok, how, output = False, "self-update (-U)", f"{d['label']} is not installed"
        else:
            rc, out, err = run_([*before.argv, "-U"], 180.0)
            ok, how, output = rc == 0, "self-update (-U)", _tail(out + "\n" + err)
            if not ok and re.search(r"pip|package manager|installed (?:by|via|from|with)", output, re.I):
                ok, how, output = pip()
                if not ok and method == "auto":
                    ok, how, output = _download_release(name, fetch or _default_fetch, verify_sha)
    elif choice == "pip":
        ok, how, output = pip()
        if not ok and method == "auto":
            ok, how, output = _download_release(name, fetch or _default_fetch, verify_sha)
    elif choice == "download":
        ok, how, output = _download_release(name, fetch or _default_fetch, verify_sha)
    else:
        raise ValueError(f"unknown update method {method!r}")
    reset_cache()
    after = find(name, refresh=True, runner=run_)
    result: dict[str, Any] = {
        "tool": name, "ok": ok, "method": how, "before": before.version or None, "after": after.version or None,
        "updated": bool(after.version) and after.version != before.version, "output": output,
    }
    if not ok:
        result["error"] = f"{d['label']} could not be updated. {output}".strip()
    return result


# ---------------------------------------------------------------- yt-dlp helpers --

def _version_tuple(version: Any) -> Optional[tuple[int, ...]]:
    m = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})(?:\.(\d+))?", str(version or ""))
    return tuple(int(g or 0) for g in m.groups()) if m else None


def ytdlp_age_days(version: Any, today: Any = None) -> Optional[int]:
    """Age in days of a yt-dlp version string (``YYYY.MM.DD[.N]``), or ``None`` when it is not a date. ``today``: ``YYYY-MM-DD``,
    a ``date`` or ``datetime`` (default: now, UTC)."""
    import datetime as dt

    m = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", str(version or ""))
    if not m:
        return None
    built = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if today is None:
        now = dt.datetime.now(dt.timezone.utc).date()
    elif isinstance(today, dt.datetime):
        now = today.date()
    elif isinstance(today, dt.date):
        now = today
    else:
        now = dt.date.fromisoformat(str(today)[:10])
    return (now - built).days


def ytdlp_stale(version: Any, days: int = 45, today: Any = None) -> bool:
    """True when the build is older than ``days`` (a stale yt-dlp is refreshed before downloading)."""
    age = ytdlp_age_days(version, today)
    return age is not None and age > days


def ytdlp_base_args(*, node: "Optional[str | Tool]" = None, version: Any = None, ignore_config: bool = True) -> list[str]:
    """The options every yt-dlp call should start with: ``--ignore-config`` (a user's own config file must not change what the app
    asks for) and, when a Node.js binary is found, ``--js-runtimes node:<path> --remote-components ejs:github`` (recent yt-dlp needs a
    JavaScript runtime to solve YouTube's player; it is skipped for builds older than :data:`JS_RUNTIME_MIN_VERSION`, which would
    reject the option, and when no node is found)."""
    args: list[str] = ["--ignore-config"] if ignore_config else []
    path = node.path if isinstance(node, Tool) else node
    if path is None and not isinstance(node, Tool):
        found = find("node")
        path = found.path if found else None
    vt = _version_tuple(version)
    if path and (vt is None or vt >= _version_tuple(JS_RUNTIME_MIN_VERSION)):  # type: ignore[operator]
        args += ["--js-runtimes", f"node:{path}", "--remote-components", "ejs:github"]
    return args


OUTPUT_TEMPLATE = "%(title).120s [%(id)s].%(ext)s"
COOKIE_BROWSERS = ("firefox", "chrome", "edge", "brave", "chromium", "vivaldi", "opera")
DEFAULT_MAX_ITEMS = 50


def video_selector(quality: Any = "best", has_ffmpeg: bool = True) -> str:
    """The yt-dlp ``-f`` expression: H.264 + AAC first (plays everywhere), then anything."""
    h = f"[height<={quality}]" if re.fullmatch(r"\d+", str(quality)) else ""
    if not has_ffmpeg:
        return f"b[ext=mp4]{h}/b{h}/b"
    return "/".join([f"bv*[vcodec^=avc1]{h}+ba[ext=m4a]", f"b[ext=mp4]{h}", f"bv*{h}+ba", f"b{h}", "b"])


def cookie_args(attempt: Optional[dict[str, Any]]) -> list[str]:
    """One cookie attempt (see :func:`cookie_attempts`) as yt-dlp arguments."""
    if not attempt or attempt.get("type") == "none":
        return []
    if attempt.get("type") == "file":
        return ["--cookies", attempt["path"]]
    return ["--cookies-from-browser", attempt["name"]]


def cookie_attempts(request: str = "auto", *, cookies_file: str = "", browsers: Sequence[str] = COOKIE_BROWSERS) -> list[dict[str, str]]:
    """The ordered cookie attempts for a request (``auto`` | ``none`` | a browser name): no cookies first, then each browser,
    until one works; a cookies file wins over ``auto``."""
    r = str(request or "auto").strip().lower()
    if r == "none":
        return [{"type": "none"}]
    if r and r != "auto":
        return [{"type": "browser", "name": r}]
    if cookies_file:
        return [{"type": "file", "path": cookies_file}]
    return [{"type": "none"}, *({"type": "browser", "name": b} for b in browsers)]


def build_ytdlp_args(*, url: str, format: str = "video", quality: Any = "best", dir: str, playlist: bool = False,
                     max_items: int = DEFAULT_MAX_ITEMS, cookie: Optional[dict[str, Any]] = None, has_ffmpeg: bool = True,
                     ffmpeg_path: Optional[str] = None, extra: Sequence[str] = (), node_path: Optional[str] = None,
                     ytdlp_version: Any = None) -> list[str]:
    """The complete argument list of a yt-dlp download (``--ignore-config``, retries, output folder and template, format
    selection, cookies, machine-readable progress lines, ``--`` and the URL). ``node_path`` adds the JavaScript runtime options
    (see :func:`ytdlp_base_args`). Progress lines start with ``LHP|``, ``LHPP|``, ``LHSEL|``, ``LHMETA|`` (:func:`parse_ytdlp_line`)."""
    head = ytdlp_base_args(node=node_path, version=ytdlp_version) if node_path else ["--ignore-config"]
    args = [*head, "--newline", "--no-colors", "--no-warnings", "--progress", "--windows-filenames", "--no-mtime",
            "--retries", "10", "--fragment-retries", "10", "--concurrent-fragments", "4",
            *(["--yes-playlist", "--playlist-end", str(max_items)] if playlist else ["--no-playlist"]),
            "-P", dir, "-o", OUTPUT_TEMPLATE]
    if ffmpeg_path:
        args += ["--ffmpeg-location", ffmpeg_path]
    if format == "audio":
        args += ["-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "0", "--embed-metadata"]
    else:
        args += ["-f", video_selector(quality, has_ffmpeg)]
        if has_ffmpeg:
            args += ["--merge-output-format", "mp4"]
    args += cookie_args(cookie)
    args += [
        "--progress-template", "download:LHP|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(progress.status)s",
        "--progress-template", "postprocess:LHPP|%(progress.postprocessor)s|%(progress.status)s",
        "--print", "before_dl:LHSEL|%(format_id)s|%(playlist_index)s|%(n_entries)s|%(filename)s",
        "--print", "after_move:LHMETA|%(.{id,title,uploader,channel,upload_date,description,duration,playlist_title,filepath})j",
        *extra, "--", url,
    ]
    return args


def build_gallery_args(*, url: str, dir: str, cookie: Optional[dict[str, Any]] = None, max_items: int = DEFAULT_MAX_ITEMS) -> list[str]:
    return [*cookie_args(cookie), "--write-metadata", "--no-mtime", "--range", f"1-{max_items}", "-D", dir, "--", url]


def build_probe_args(*, url: str, playlist: bool = False, cookie: Optional[dict[str, Any]] = None, node_path: Optional[str] = None,
                     ytdlp_version: Any = None) -> list[str]:
    base = ytdlp_base_args(node=node_path, version=ytdlp_version) if node_path else ["--ignore-config"]
    return [*base, "--dump-single-json", "--no-warnings", "--skip-download", *(["--flat-playlist"] if playlist else ["--no-playlist"]),
            *cookie_args(cookie), "--", url]


def _num(v: Any) -> "int | float | None":
    s = str(v if v is not None else "").strip()
    if s in ("", "NA"):
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    if n != n or n in (float("inf"), float("-inf")):
        return None
    return int(n) if n == int(n) else n


def parse_ytdlp_line(line: Any) -> Optional[dict[str, Any]]:
    """One stdout line of a :func:`build_ytdlp_args` run as an event (``progress``, ``pp``, ``sel``, ``meta``, ``already``), or ``None``."""
    text = str(line)
    if text.startswith("LHP|"):
        p = text.split("|")
        g = lambda i: p[i] if i < len(p) else ""  # noqa: E731
        return {"type": "progress", "downloaded": _num(g(1)), "total": _num(g(2)), "estimate": _num(g(3)), "speed": _num(g(4)),
                "eta": _num(g(5)), "status": g(6) or "downloading"}
    if text.startswith("LHPP|"):
        p = text.split("|")
        return {"type": "pp", "name": p[1] if len(p) > 1 else "", "status": p[2] if len(p) > 2 else ""}
    if text.startswith("LHSEL|"):
        p = text.split("|")
        g = lambda i: p[i] if i < len(p) else ""  # noqa: E731
        return {"type": "sel", "formatId": g(1), "index": _num(g(2)), "count": _num(g(3)), "filename": "|".join(p[4:])}
    if text.startswith("LHMETA|"):
        try:
            return {"type": "meta", "data": json.loads(text[7:])}
        except ValueError:
            return None
    m = re.match(r"^\[download\]\s+(.+?)\s+has already been downloaded", text)
    if m:
        return {"type": "already", "path": m.group(1)}
    return None


def format_speed(bytes_per_second: Any) -> str:
    try:
        v = float(bytes_per_second)
    except (TypeError, ValueError):
        return ""
    if not v > 0 or v == float("inf"):
        return ""
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    if v >= 100 or i == 0:
        return f"{math.floor(v + 0.5)} {units[i]}"
    tenths = math.floor(v * 10 + 0.5)  # round half up, like the Node twin (Python's round() and format() round half to even)
    return f"{tenths // 10}.{tenths % 10} {units[i]}"


def format_eta(seconds: Any) -> str:
    try:
        v = float(seconds)
    except (TypeError, ValueError):
        return ""
    if v != v or v < 0 or v == float("inf"):
        return ""
    s = int(v + 0.5)
    h, m = s // 3600, (s % 3600) // 60
    return f"{h}:{m:02d}:{s % 60:02d}" if h else f"{m:02d}:{s % 60:02d}"


# -- failures ---------------------------------------------------------------

_I = re.IGNORECASE
_RX = {
    "ffmpeg": re.compile(r"ffmpeg.*(?:not found|not installed|could not be found)|ffprobe and ffmpeg not found|requires ffmpeg|ffmpeg is required|ffmpeg or avconv", _I),
    "no_video": re.compile(r"there is no video in this post|no video could be found|no video formats? found|does not contain (?:a )?video|no video in this (?:post|tweet)", _I),
    "unsupported": re.compile(r"unsupported url|no suitable extractor|no extractor found", _I),
    # connection-level failures: never a sign-in problem (the old Cook regex matched the bare word "age" inside "webpage")
    "network": re.compile(r"getaddrinfo|name or service not known|nodename nor servname|temporary failure in name resolution|no address associated|failed to resolve|name resolution|network is unreachable|no route to host|connection (?:reset|refused|aborted|closed)|timed out|\btimeout\b|urlopen error|\bssl\b|certificate verify|remote end closed", _I),
    "strong_login": re.compile(r"\bsign ?in\b|\blog ?in\b|\blogged in\b|\bprivate\b|\bage[- ]restricted|confirm your age|members[- ]only|not a bot|\bcookies\b|\bauthenticat", _I),
    "login": re.compile(r"\blog ?in\b|\bsign ?in\b|\blogged in\b|\bcookies\b|\bauthenticat|\bprivate (?:video|account|post|tweet)|not a bot|rate[- ]limit|empty media response|restricted video|\bage[- ]restricted|confirm your age|members[- ]only|requires? (?:an )?account|\bnsfw\b|protected tweet|tweet is protected|\b40[13]\b|\bforbidden\b|autherror|authrequired|too many requests|\b429\b", _I),
    # YouTube and others answer 403 to an old yt-dlp's media requests: an update fixes it far more often than cookies
    "blocked": re.compile(r"unable to download video data:? http error 403|requested format is not available|\bsabr\b|po token|signature (?:extraction|decipher)|http error 403", _I),
    "outdated": re.compile(r"no such option|unrecognized arguments|invalid (?:output )?template|unknown (?:output )?template|unsupported field|nsig extraction failed|unable to extract (?:uploader|video data|\w+ (?:data|info|player))", _I),
    "unavailable": re.compile(r"video unavailable|this video is (?:not available|unavailable|private)|has been removed|no longer available|been deleted|does not exist|http error 404|\bnot ?found\b|geo[- ]restrict|not available in your country", _I),
    "weak_network": re.compile(r"unable to download (?:webpage|json)", _I),
}


def classify_failure(tool: str, stderr: Any, code: Optional[int] = None) -> str:
    """What went wrong in a failed yt-dlp / gallery-dl run, from its stderr: ``no_ffmpeg``, ``no_video`` (a photo post: try
    gallery-dl), ``unsupported``, ``network``, ``forbidden`` (403: usually an outdated yt-dlp), ``login`` (cookies needed),
    ``outdated``, ``unavailable`` or ``unknown``. ``code`` is the exit code (gallery-dl bit 16 means authentication)."""
    raw = str(stderr or "")
    if _RX["ffmpeg"].search(raw):
        return "no_ffmpeg"
    if _RX["no_video"].search(raw):
        return "no_video"
    if _RX["unsupported"].search(raw):
        return "unsupported"
    if _RX["network"].search(raw):
        return "network"
    if tool == "yt-dlp" and _RX["blocked"].search(raw) and not _RX["strong_login"].search(raw):
        return "forbidden"
    if _RX["login"].search(raw) or (tool == "gallery-dl" and code and (code & 16)):
        return "login"
    if _RX["outdated"].search(raw):
        return "outdated"
    if _RX["unavailable"].search(raw):
        return "unavailable"
    if _RX["weak_network"].search(raw):
        return "network"
    return "unknown"


# -- platforms, kinds, URLs -----------------------------------------------------

_PLATFORMS = (
    ("YouTube", ("youtube.com", "youtu.be", "youtube-nocookie.com")),
    ("X (Twitter)", ("twitter.com", "x.com", "t.co", "fxtwitter.com", "vxtwitter.com", "fixupx.com")),
    ("Instagram", ("instagram.com", "instagr.am")),
    ("TikTok", ("tiktok.com",)),
    ("Audiomack", ("audiomack.com",)),
    ("SoundCloud", ("soundcloud.com", "snd.sc")),
    ("Vimeo", ("vimeo.com",)),
    ("Twitch", ("twitch.tv",)),
    ("Reddit", ("reddit.com", "redd.it")),
    ("Facebook", ("facebook.com", "fb.watch", "fb.com")),
    ("Bilibili", ("bilibili.com", "b23.tv")),
    ("Dailymotion", ("dailymotion.com", "dai.ly")),
    ("Bandcamp", ("bandcamp.com",)),
    ("Pinterest", ("pinterest.com", "pin.it")),
    ("Threads", ("threads.net",)),
)
OTHER_PLATFORM = "Other (yt-dlp)"
PLATFORM_NAMES = tuple(label for label, _ in _PLATFORMS)
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


def detect_platform(url: Any, other: str = OTHER_PLATFORM) -> str:
    """Label of the site a URL belongs to (the 15 platforms yt-dlp handles best); unknown sites are still attempted."""
    text = str(url).strip()
    try:
        host = (urlsplit(text if _SCHEME.match(text) else f"https://{text}").hostname or "").lower()
    except ValueError:
        return other
    for label, domains in _PLATFORMS:
        if any(host == d or host.endswith("." + d) for d in domains):
            return label
    return other


_EXT = {
    "video": {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".ts"},
    "audio": {".mp3", ".m4a", ".opus", ".ogg", ".oga", ".flac", ".wav", ".aac", ".wma"},
    "image": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif", ".bmp", ".tiff"},
}


def file_kind(name: Any) -> str:
    """``video`` | ``audio`` | ``image`` | ``other`` from a file name's extension."""
    ext = os.path.splitext(str(name))[1].lower()
    for kind, exts in _EXT.items():
        if ext in exts:
            return kind
    return "other"


class MediaUrlError(ValueError):
    """The URL is not one the media downloader may fetch (not http(s), no host, or it points at this machine / a private network)."""


_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".lan", ".home.arpa")
_NUMERIC_HOST = re.compile(r"^(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}$", re.I)
# (network, prefix length) of IPv4 ranges that are not the public internet
_V4_BLOCKED = ((0, 8), (0x0A000000, 8), (0x64400000, 10), (0x7F000000, 8), (0xA9FE0000, 16), (0xAC100000, 12), (0xC0000000, 24),
               (0xC0A80000, 16), (0xC6120000, 15), (0xE0000000, 4), (0xF0000000, 4))


def _v4_blocked(n: int) -> bool:
    return any((n >> (32 - bits)) == (net >> (32 - bits)) for net, bits in _V4_BLOCKED)


def _v6_blocked(groups: Sequence[int]) -> bool:
    g = list(groups)
    if all(x == 0 for x in g[:7]) and g[7] in (0, 1):                      # :: and ::1
        return True
    if all(x == 0 for x in g[:5]) and g[5] == 0xFFFF:                      # ::ffff:a.b.c.d (IPv4-mapped)
        return _v4_blocked((g[6] << 16) | g[7])
    if g[:6] == [0x64, 0xFF9B, 0, 0, 0, 0]:                                # 64:ff9b::/96 (NAT64)
        return _v4_blocked((g[6] << 16) | g[7])
    return (g[0] & 0xFE00) == 0xFC00 or (g[0] & 0xFFC0) in (0xFE80, 0xFEC0) or (g[0] & 0xFF00) == 0xFF00


def _host_is_private(host: str) -> bool:
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return True
    if ":" in host:
        try:
            packed = ipaddress.IPv6Address(host).packed
        except ValueError:
            return True  # not a valid IPv6 literal: refuse rather than guess
        return _v6_blocked([(packed[i] << 8) | packed[i + 1] for i in range(0, 16, 2)])
    if _NUMERIC_HOST.match(host):
        try:
            return _v4_blocked(int.from_bytes(socket.inet_aton(host), "big"))
        except OSError:
            return True
    return False


def normalize_media_url(url: Any) -> str:
    """A clean ``http(s)`` URL for a download, or :class:`MediaUrlError`.

    A missing scheme becomes ``https://``. Refused: other schemes, hosts without a dot (except none), ``localhost`` and ``*.local`` /
    ``*.internal`` names, and IP literals — in any spelling (``2130706433``, ``0x7f.1``, ``[::ffff:127.0.0.1]``) — that are loopback,
    private (10/8, 172.16/12, 192.168/16), link-local, CGNAT (100.64/10), multicast or otherwise not the public internet.
    No DNS lookup is made, so a public name that resolves to a private address is not caught here (use the family fetcher's guard).
    """
    text = str(url or "").strip()
    if not text:
        raise MediaUrlError("The URL is missing.")
    if not _SCHEME.match(text):
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise MediaUrlError(f"{url!r} is not a valid URL.") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise MediaUrlError(f"{url!r} is not a valid http(s) URL.")
    if not host or not ("." in host or ":" in host):
        raise MediaUrlError(f"{url!r} is not a valid http(s) URL.")
    if _host_is_private(host):
        raise MediaUrlError(f"{url!r} points at this machine or a private network; only public pages can be downloaded.")
    scheme = parts.scheme.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += f":{port}"
    userinfo = ""
    if parts.username is not None:
        userinfo = quote(parts.username, safe="%") + (":" + quote(parts.password, safe="%") if parts.password is not None else "") + "@"
    path = quote(parts.path or "/", safe="/:@!$&'()*+,;=-._~%")
    query = ("?" + quote(parts.query, safe="=&/:@!$'()*+,;-._~%?")) if parts.query else ""
    frag = ("#" + quote(parts.fragment, safe="=&/:@!$'()*+,;-._~%?#")) if parts.fragment else ""
    return f"{scheme}://{userinfo}{netloc}{path}{query}{frag}"
