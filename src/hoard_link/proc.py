"""Subprocess helpers every app re-wrote six times: no console window, UTF-8 output, killing a whole process tree,
finding an executable on a Windows machine, and showing a file in the file manager.

Standard library only (``psutil`` is used for the process tree when it is installed, never required).

* :func:`run` — a bounded ``subprocess.run``: argument list only, ``stdin`` closed unless ``input`` is given, output decoded
  as UTF-8 with ``errors="replace"``, the **tree** killed on timeout or when ``cancel`` (a ``threading.Event``) is set.
* :func:`run_streaming` — feed every output line to a callback while the command runs (ffmpeg ``-progress``, yt-dlp, pip…).
* :func:`popen` — ``subprocess.Popen`` with the same defaults; ``detached=True`` for a server that must outlive the app,
  ``low_priority=True`` for background work.
* :func:`kill_tree`, :func:`request_stop` — stop a process and everything it started (``taskkill /T`` on Windows, the process
  group on POSIX).
* :func:`find_exe`, :func:`exe_candidates` — where a program lives: explicit path, environment variables, PATH
  (``.exe``/``.com`` only on Windows), WinGet links, WindowsApps, Program Files, ``$HOARD_HOME/bin``.
* :func:`reveal_in_file_manager` — ``explorer /select,`` as an argument **list** (never a string with quotes in it).
* :func:`build_env` — the environment child processes get (UTF-8 for Python children).

Every child is started with ``CREATE_NO_WINDOW`` on Windows (a console flashing for each call was the bug in 13 call sites) and
in its own process group / session, so :func:`kill_tree` reaches the grandchildren (yt-dlp -> ffmpeg).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from .errors import HoardLinkError
from .launch import hoard_home

__all__ = [
    "Cancelled", "IS_WIN", "no_window_kwargs", "build_env", "popen", "run", "run_streaming", "kill_tree", "request_stop",
    "pid_alive", "exe_candidates", "find_exe", "which", "resolve_value", "exe_in_dir", "reveal_command", "reveal_in_file_manager", "tail_lines",
]

log = logging.getLogger("hoard_link.proc")

IS_WIN = sys.platform.startswith("win")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
_BREAKAWAY = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
_POLL_S = 0.1


class Cancelled(HoardLinkError):
    """The caller's ``cancel`` event was set; the process tree has been killed. ``partial`` may carry what had been produced."""

    def __init__(self, message: str = "Cancelled.", partial: Any = None):
        super().__init__(message)
        self.partial = partial


# ---------------------------------------------------------------- basics --

def no_window_kwargs() -> dict[str, Any]:
    """``{"creationflags": CREATE_NO_WINDOW}`` on Windows, ``{}`` elsewhere."""
    return {"creationflags": _CREATE_NO_WINDOW} if IS_WIN else {}


def _argv(args: Sequence[Any]) -> list[str]:
    if isinstance(args, (str, bytes)):
        raise TypeError("pass the command as a list of arguments, never as one string (no shell is involved)")
    return [os.fspath(a) if isinstance(a, os.PathLike) else str(a) for a in args]


def build_env(base: Optional[dict[str, str]] = None, **extra: Any) -> dict[str, str]:
    """``base`` (default: a copy of ``os.environ``) plus ``extra`` (``None`` removes a key), with UTF-8 forced for Python children
    (``PYTHONUTF8=1``, ``PYTHONIOENCODING=utf-8``) so a non-ASCII file name never raises ``UnicodeDecodeError`` under cp1252."""
    env = dict(os.environ if base is None else base)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    for key, value in extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


def tail_lines(text: str, n: int = 12, limit: int = 1200) -> str:
    """The last ``n`` non-empty lines of ``text`` (at most ``limit`` characters): the useful end of an error log."""
    lines = [ln.rstrip() for ln in str(text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])[-limit:]


# ---------------------------------------------------------------- popen --

def popen(args: Sequence[Any], *, detached: bool = False, low_priority: bool = False, **kw: Any) -> subprocess.Popen:
    """``subprocess.Popen`` with the family's defaults.

    * ``stdin`` is closed unless you pass one; text mode (``text=True`` / ``encoding=``) decodes as UTF-8 with ``errors="replace"``.
    * Windows: ``CREATE_NO_WINDOW`` always and ``CREATE_NEW_PROCESS_GROUP`` (so Ctrl+Break and ``taskkill /T`` can address the
      child). POSIX: a new session (the child leads its own process group, :func:`kill_tree` kills the group).
    * ``detached=True``: the child must outlive this process — stdio goes to the null device, ``close_fds``, and on Windows
      ``CREATE_BREAKAWAY_FROM_JOB`` is tried first (a Task Manager "end tree" then does not take it down).
    * ``low_priority=True``: below-normal priority (``BELOW_NORMAL_PRIORITY_CLASS`` / ``nice 10``).
    """
    argv = _argv(args)
    kw = dict(kw)
    kw.setdefault("stdin", subprocess.DEVNULL)
    if kw.get("text") or kw.get("universal_newlines") or kw.get("encoding"):
        kw.setdefault("encoding", "utf-8")
        kw.setdefault("errors", "replace")
    if detached:
        kw.setdefault("stdout", subprocess.DEVNULL)
        kw.setdefault("stderr", subprocess.DEVNULL)
        kw.setdefault("close_fds", True)
    if not IS_WIN:
        kw.setdefault("start_new_session", True)
        proc = subprocess.Popen(argv, **kw)
        if low_priority:
            try:
                os.setpriority(os.PRIO_PROCESS, proc.pid, 10)
            except (OSError, AttributeError):
                pass
        return proc
    flags = int(kw.pop("creationflags", 0)) | _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    if low_priority:
        flags |= _BELOW_NORMAL
    if detached:
        try:
            return subprocess.Popen(argv, creationflags=flags | _BREAKAWAY, **kw)
        except OSError:
            pass  # the parent's job does not allow breakaway
    return subprocess.Popen(argv, creationflags=flags, **kw)


# ---------------------------------------------------------------- killing --

def pid_alive(pid: int) -> bool:
    """True while ``pid`` is a running process (a zombie awaiting its parent counts as gone)."""
    if pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return False
    except (OSError, IndexError):
        pass
    return True


def _taskkill(pid: int, force: bool) -> None:
    cmd = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
    try:
        subprocess.run(cmd, capture_output=True, timeout=20, stdin=subprocess.DEVNULL, **no_window_kwargs())
    except (OSError, subprocess.SubprocessError):
        pass


def _children_pids(pid: int) -> list[int]:
    """Descendants of ``pid`` (psutil when installed, else /proc on Linux, else none)."""
    try:
        import psutil  # type: ignore

        return [c.pid for c in psutil.Process(pid).children(recursive=True)]
    except Exception:  # noqa: BLE001 - not installed, no such process, access denied
        pass
    parents: dict[int, list[int]] = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                stat = Path(f"/proc/{entry}/stat").read_text()
                ppid = int(stat.rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            parents.setdefault(ppid, []).append(int(entry))
    except OSError:
        return []
    out: list[int] = []
    queue = [pid]
    while queue:
        for child in parents.get(queue.pop(), []):
            out.append(child)
            queue.append(child)
    return out


def _signal_tree(pid: int, sig: int) -> None:
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        pgid = -1
    if pgid == pid and pgid != os.getpgrp():  # the child leads its own group: the signal reaches every descendant in it
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError):
            pass
    victims = _children_pids(pid) + [pid]
    for victim in reversed(victims):
        try:
            os.kill(victim, sig)
        except (ProcessLookupError, PermissionError):
            pass


def kill_tree(target: "int | subprocess.Popen", *, grace_s: float = 5.0, force: bool = True) -> bool:
    """Stop a process and everything it started; returns True when it is gone.

    Windows: ``taskkill /T`` (polite), then ``/F`` after ``grace_s`` when ``force``. POSIX: ``SIGTERM`` to the process group
    (or to the process and its descendants when it does not lead one), then ``SIGKILL``. ``grace_s=0`` goes straight to the
    forced kill. Pass the ``Popen`` when you have it (its exit is then reaped and a zombie is never mistaken for a live process).
    """
    proc = target if hasattr(target, "pid") else None
    pid = int(target.pid if proc is not None else target)  # type: ignore[union-attr, arg-type]

    def alive() -> bool:
        return proc.poll() is None if proc is not None else pid_alive(pid)

    def wait(seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, seconds)
        while alive():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    if not alive():
        return True
    polite = grace_s > 0
    if IS_WIN:
        _taskkill(pid, force=not polite)
    else:
        _signal_tree(pid, signal.SIGTERM if polite else signal.SIGKILL)
    if wait(grace_s if polite else 2.0):
        return True
    if force and polite:
        if IS_WIN:
            _taskkill(pid, force=True)
        else:
            _signal_tree(pid, signal.SIGKILL)
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
        return wait(3.0)
    return False


def request_stop(proc: subprocess.Popen) -> None:
    """Ask politely so the program can save its work: ``CTRL_BREAK_EVENT`` on Windows (the child was started by :func:`popen`,
    so it has its own process group), ``SIGTERM`` to the group on POSIX."""
    try:
        if IS_WIN:
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            _signal_tree(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError, ValueError):
        pass


# ---------------------------------------------------------------- run --

def _drain(proc: subprocess.Popen) -> tuple[Any, Any]:
    try:
        return proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:  # a grandchild still holds the pipes
        kill_tree(proc, grace_s=0)
        try:
            return proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            return ("" if getattr(proc, "text_mode", True) else b""), ""


def run(args: Sequence[Any], *, timeout: Optional[float] = None, cwd: Any = None, env: Optional[dict[str, str]] = None,
        input: Any = None, check: bool = False, text: bool = True, cancel: Optional[threading.Event] = None,
        grace_s: float = 2.0) -> subprocess.CompletedProcess:
    """Run a command to the end and capture its output.

    ``text=True`` (default) decodes stdout/stderr as UTF-8 with ``errors="replace"``; ``text=False`` returns bytes. ``stdin`` is
    closed unless ``input`` is given. On ``timeout`` the process **tree** is killed and :class:`subprocess.TimeoutExpired` is
    raised (with the output so far); when ``cancel`` is set it is killed and :class:`Cancelled` is raised. A command that cannot
    be started raises ``OSError`` (``FileNotFoundError``); ``check=True`` raises ``CalledProcessError`` on a non-zero exit.
    """
    argv = _argv(args)
    kw: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "cwd": os.fspath(cwd) if cwd else None, "env": env}
    if text:
        kw["text"] = True
    if input is not None:
        kw["stdin"] = subprocess.PIPE
    proc = popen(argv, **kw)
    deadline = time.monotonic() + timeout if timeout else None
    pending = input
    try:
        while True:
            slice_s: Optional[float] = None
            if cancel is not None:
                slice_s = _POLL_S
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout or 0)
                slice_s = left if slice_s is None else min(slice_s, left)
            try:
                out, err = proc.communicate(pending, timeout=slice_s)
                break
            except subprocess.TimeoutExpired:
                pending = None
                if cancel is not None and cancel.is_set():
                    raise Cancelled() from None
                if deadline is not None and time.monotonic() >= deadline:
                    raise
    except (subprocess.TimeoutExpired, Cancelled) as stop:
        kill_tree(proc, grace_s=grace_s)
        out, err = _drain(proc)
        if isinstance(stop, subprocess.TimeoutExpired):
            raise subprocess.TimeoutExpired(argv, timeout or 0, output=out, stderr=err) from None
        stop.partial = out
        raise
    except BaseException:
        kill_tree(proc, grace_s=0)
        raise
    done = subprocess.CompletedProcess(argv, proc.returncode, out, err)
    if check and done.returncode != 0:
        raise subprocess.CalledProcessError(done.returncode, argv, output=out, stderr=err)
    return done


def run_streaming(args: Sequence[Any], on_line: Callable[[str], None], *, stderr_line: Optional[Callable[[str], None]] = None,
                  timeout: Optional[float] = None, cancel: Optional[threading.Event] = None, cwd: Any = None,
                  env: Optional[dict[str, str]] = None, grace_s: float = 2.0, low_priority: bool = False) -> int:
    """Run a command and hand every stdout line to ``on_line`` (a ``\\r`` also ends a line, so progress meters stream).

    ``stderr_line`` gets the stderr lines; without it stderr is merged into stdout. Returns the exit code. Callback errors are
    swallowed (a broken parser must not stop the reader). On ``timeout`` the tree is killed and ``TimeoutExpired`` is raised; when
    ``cancel`` is set it is killed and :class:`Cancelled` is raised. The reader threads are always joined before returning.
    """
    argv = _argv(args)
    kw: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE if stderr_line else subprocess.STDOUT,
                          "cwd": os.fspath(cwd) if cwd else None, "env": env, "text": True, "bufsize": 1}
    proc = popen(argv, low_priority=low_priority, **kw)

    def reader(stream: Any, callback: Callable[[str], None]) -> None:
        try:
            for raw in iter(stream.readline, ""):
                line = raw.rstrip("\r\n")
                try:
                    callback(line)
                except Exception:  # noqa: BLE001
                    log.debug("a line callback raised", exc_info=True)
        except (OSError, ValueError):
            pass

    threads = [threading.Thread(target=reader, args=(proc.stdout, on_line), name="hoard-proc-out", daemon=True)]
    if stderr_line:
        threads.append(threading.Thread(target=reader, args=(proc.stderr, stderr_line), name="hoard-proc-err", daemon=True))
    for t in threads:
        t.start()
    started = time.monotonic()
    stop: Optional[str] = None
    try:
        while True:
            try:
                proc.wait(timeout=_POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel is not None and cancel.is_set():
                stop = "cancel"
                break
            if timeout is not None and time.monotonic() - started > timeout:
                stop = "timeout"
                break
    except BaseException:
        kill_tree(proc, grace_s=0)
        raise
    finally:
        if proc.poll() is None:
            kill_tree(proc, grace_s=grace_s)
        for t in threads:
            t.join(timeout=5)
        if any(t.is_alive() for t in threads):  # a grandchild holds the pipe open
            kill_tree(proc, grace_s=0)
            for t in threads:
                t.join(timeout=2)
    if stop == "cancel":
        raise Cancelled()
    if stop == "timeout":
        raise subprocess.TimeoutExpired(argv, timeout or 0)
    return int(proc.returncode)


# ---------------------------------------------------------------- finding programs --

def _clean_spec(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def _exe_names(name: str, allow_scripts: bool) -> list[str]:
    if not IS_WIN:
        return [name]
    low = name.lower()
    if low.endswith((".exe", ".com")):
        return [name]
    names = [name + ".exe", name + ".com"]
    if allow_scripts:
        names += [name + ".cmd", name + ".bat"]
    return names


def _is_runnable(path: Path) -> bool:
    try:
        return path.is_file() and (IS_WIN or os.access(path, os.X_OK))
    except OSError:
        return False


def exe_in_dir(directory: "str | Path", name: str, allow_scripts: bool) -> Optional[str]:
    for candidate in _exe_names(name, allow_scripts):
        p = Path(directory) / candidate
        if _is_runnable(p):
            return str(p)
    return None


def _path_dirs() -> list[str]:
    raw = os.environ.get("PATH") or os.environ.get("Path") or ""
    return [d.strip().strip('"') for d in raw.split(os.pathsep) if d.strip()]


def resolve_value(value: Any, name: str, allow_scripts: bool) -> Optional[str]:
    """An explicit path / environment value: a file, a folder that holds ``name``, or a bare command found on PATH."""
    text = _clean_spec(value)
    if not text:
        return None
    text = os.path.expanduser(text)
    p = Path(text)
    if p.is_dir():
        return exe_in_dir(p, name, allow_scripts)
    if os.sep in text or "/" in text or (IS_WIN and "\\" in text):
        return str(p) if p.is_file() else None
    for d in _path_dirs():
        found = exe_in_dir(d, text, allow_scripts)
        if found:
            return found
    return None


def _well_known_dirs(name: str) -> list[tuple[str, str]]:
    """(directory, how) places a program of that name is commonly installed."""
    out: list[tuple[str, str]] = []
    env = os.environ
    if IS_WIN:
        local = env.get("LOCALAPPDATA", "")
        if local:
            out.append((str(Path(local) / "Microsoft" / "WinGet" / "Links"), "winget"))
            out.append((str(Path(local) / "Microsoft" / "WindowsApps"), "system"))
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            base = env.get(var)
            if base:
                for sub in (name, f"{name}/bin", "nodejs" if name == "node" else name):
                    out.append((str(Path(base) / sub), "system"))
        choco = env.get("ChocolateyInstall")
        if choco:
            out.append((str(Path(choco) / "bin"), "system"))
        profile = env.get("USERPROFILE")
        if profile:
            out.append((str(Path(profile) / "scoop" / "shims"), "system"))
        out.append((f"C:/{name}/bin", "system"))
    else:
        for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", str(Path.home() / ".local" / "bin")):
            out.append((d, "system"))
    return out


def exe_candidates(name: str, *, explicit: Optional[str] = None, env_vars: Iterable[str] = (), extra_dirs: Iterable[Any] = (),
                   allow_scripts: bool = False) -> list[tuple[str, str]]:
    """Every place ``name`` exists, in lookup order, as ``(path, how)``; ``how`` is ``explicit``, ``env``, ``extra``, ``path``,
    ``winget``, ``system`` or ``hoard-bin``. Nothing is run; a file that exists is a candidate (verify it with the caller's own check).

    Order: ``explicit`` > ``env_vars`` (each a path, a folder or a command name) > ``extra_dirs`` > PATH (``.exe``/``.com`` only on
    Windows — ``.cmd``/``.bat`` shims cannot be spawned safely, unless ``allow_scripts``) > WinGet links > WindowsApps >
    Program Files and other usual folders > ``$HOARD_HOME/bin``.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(path: Optional[str], how: str) -> None:
        if not path:
            return
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            found.append((path, how))

    if explicit:
        add(resolve_value(explicit, name, allow_scripts=True), "explicit")
    for var in env_vars:
        add(resolve_value(os.environ.get(var), name, allow_scripts=True), "env")
    for d in extra_dirs:
        if d:
            add(exe_in_dir(d, name, allow_scripts), "extra")
    for d in _path_dirs():
        add(exe_in_dir(d, name, allow_scripts), "path")
    for d, how in _well_known_dirs(name):
        add(exe_in_dir(d, name, allow_scripts), how)
    add(exe_in_dir(hoard_home() / "bin", name, allow_scripts), "hoard-bin")
    return found


def _passes(path: str, verify_args: Sequence[str]) -> bool:
    try:
        done = run([path, *verify_args], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def find_exe(name: str, *, explicit: Optional[str] = None, env_vars: Iterable[str] = (), extra_dirs: Iterable[Any] = (),
             verify_args: Optional[Sequence[str]] = None, allow_scripts: bool = False) -> Optional[str]:
    """The first working path of ``name`` (see :func:`exe_candidates` for the order), or ``None``.

    With ``verify_args`` (``["-version"]``) a candidate counts only when running it with them exits 0 (a broken shim or a
    quarantined download is skipped and the next candidate tried); without it the file only has to exist and be executable.
    """
    for path, _how in exe_candidates(name, explicit=explicit, env_vars=env_vars, extra_dirs=extra_dirs, allow_scripts=allow_scripts):
        if verify_args is None or _passes(path, verify_args):
            return path
    return None


def which(name: str) -> Optional[str]:
    """``find_exe(name)`` without verification (the one place an app asks where a program is)."""
    return find_exe(name)


# ---------------------------------------------------------------- file manager --

def reveal_command(path: "str | Path", platform: Optional[str] = None) -> Optional[list[str]]:
    """The argument list that shows ``path`` in the file manager, or ``None`` when nothing at that path or above it exists.

    Windows: ``["explorer.exe", "/select,", path]`` — the path is its own argument, so a quote or a space in it can never
    change the command (the old ``explorer /select,"<path>"`` string could). macOS: ``open -R``. Elsewhere: ``xdg-open`` on the
    folder.
    """
    plat = platform or sys.platform
    p = Path(os.fspath(path))
    target = p
    if not target.exists():
        target = p.parent
        if not target.exists():
            return None
    if plat.startswith("win"):
        return ["explorer.exe", "/select,", str(p)] if p.exists() else ["explorer.exe", str(target)]
    if plat == "darwin":
        return ["open", "-R", str(p)] if p.exists() else ["open", str(target)]
    return ["xdg-open", str(target if target.is_dir() else target.parent)]


def reveal_in_file_manager(path: "str | Path") -> bool:
    """Show a file (selected) or a folder in the system file manager. True when the command was started."""
    cmd = reveal_command(path)
    if not cmd:
        return False
    try:
        popen(cmd, detached=True)
    except OSError:
        return False
    return True
