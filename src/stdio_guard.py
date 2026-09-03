"""stdio_guard.py — keep a stray print() out of an MCP stdio stream.

An MCP server that speaks stdio owns ``stdout``: every byte on it must be one
line of JSON-RPC. The built-in servers (``mcp_servers/*.py``) share their
process with app code — ``src.memory``, ``src.rag_manager``, the email stack —
and ONE ``print()`` anywhere in that code, or in a third-party library it
imports, corrupts the protocol stream: the client sees a parse error and the
server dies mid-session, with nothing in the logs that points at the print.

The guard swaps ``sys.stdout`` for a proxy that writes to ``sys.stderr`` while a
stdio session is active, and puts the real object back afterwards. The server's
own transport is unaffected: ``mcp.server.stdio.stdio_server()`` wraps
``sys.stdout.buffer`` when it is ENTERED, so a guard activated after that keeps
writing the protocol to the real handle while everything else lands on stderr
(where the parent process already collects it).

    async with stdio_server() as (read, write):
        with stdio_guard.guard():
            await server.run(read, write, ...)

Reentrant (nested activations count, only the outermost restores),
thread-safe (an RLock around the swap and the counter), a no-op when nothing is
active, and safe on any interpreter where ``sys.stdout``/``sys.stderr`` are
None (pythonw). ``guard()`` honours the ``agent_mcp_stdio_guard`` setting;
with it off nothing is swapped and a print goes exactly where it went before.
"""
from __future__ import annotations

import logging
import sys
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional, TextIO

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_depth = 0
_saved: Optional[Any] = None
_proxy: Optional[Any] = None


def enabled() -> bool:
    """Setting ``agent_mcp_stdio_guard``. Off = stdout is left alone."""
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_mcp_stdio_guard", True))
    except Exception:  # noqa: BLE001 - a server must start even without settings
        return True


class _StdoutToStderr:
    """A stdout stand-in that forwards everything to the CURRENT ``sys.stderr``.

    Resolved on every call rather than bound once, so a program that swaps its
    own stderr (a logging handler, a test capture) keeps working. Every method
    is defensive: a write that cannot land must not raise inside whatever code
    was merely printing.
    """

    __slots__ = ("_fallback",)

    def __init__(self, fallback: Optional[TextIO] = None) -> None:
        self._fallback = fallback

    # -- the target -------------------------------------------------------
    def _target(self) -> Optional[Any]:
        target = getattr(sys, "stderr", None)
        if target is None or target is self:
            return self._fallback
        return target

    # -- the file protocol ------------------------------------------------
    def write(self, data: Any) -> int:
        target = self._target()
        if target is None:
            return 0
        try:
            return int(target.write(data) or 0)
        except Exception:  # noqa: BLE001 - printing must never raise
            return 0

    def writelines(self, lines: Iterable[Any]) -> None:
        for line in lines or ():
            self.write(line)

    def flush(self) -> None:
        target = self._target()
        if target is None:
            return
        try:
            target.flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self) -> bool:
        target = self._target()
        try:
            return bool(target.isatty()) if target is not None else False
        except Exception:  # noqa: BLE001
            return False

    def fileno(self) -> int:
        target = self._target()
        if target is None:
            raise OSError("stdio guard: no stderr to borrow a descriptor from")
        return int(target.fileno())

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def close(self) -> None:
        """Never closes the real stderr: a library that closes "stdout" while
        the guard is up must not take the process's error stream with it."""
        return None

    @property
    def closed(self) -> bool:
        target = self._target()
        return bool(getattr(target, "closed", False)) if target is not None else True

    @property
    def encoding(self) -> str:
        target = self._target()
        return str(getattr(target, "encoding", "utf-8") or "utf-8")

    @property
    def errors(self) -> Optional[str]:
        target = self._target()
        return getattr(target, "errors", None) if target is not None else None

    @property
    def buffer(self) -> Any:
        target = self._target()
        return getattr(target, "buffer", None)

    def __getattr__(self, name: str) -> Any:      # anything else: ask stderr
        target = self._target()
        if target is None:
            raise AttributeError(name)
        return getattr(target, name)


def active() -> bool:
    """True while stdout is being held away from the protocol stream."""
    with _lock:
        return _depth > 0


def depth() -> int:
    """How many activations are currently nested."""
    with _lock:
        return _depth


def original_stdout() -> Any:
    """The real stdout the guard is holding (``sys.stdout`` when inactive) —
    for the rare caller that must write to the protocol handle on purpose."""
    with _lock:
        return _saved if _depth > 0 else getattr(sys, "stdout", None)


def activate(*, force: bool = False) -> int:
    """Redirect ``sys.stdout`` to stderr and return the new nesting depth.

    A no-op returning 0 when the setting is off (unless `force`). Every
    activate() must be paired with a deactivate(); prefer `guard()`, which
    pairs them for you even when the body raises.
    """
    global _depth, _saved, _proxy
    if not force and not enabled():
        return 0
    with _lock:
        if _depth == 0:
            _saved = getattr(sys, "stdout", None)
            _proxy = _StdoutToStderr(fallback=_saved)
            try:
                sys.stdout = _proxy
            except Exception as e:  # noqa: BLE001 - pragma: no cover
                logger.debug("stdio guard: could not redirect stdout: %s", e)
                _saved = _proxy = None
                return 0
        _depth += 1
        return _depth


def deactivate() -> int:
    """Undo one activation; the outermost one restores the saved stdout."""
    global _depth, _saved, _proxy
    with _lock:
        if _depth <= 0:
            return 0
        _depth -= 1
        if _depth == 0:
            try:
                sys.stdout = _saved
            except Exception as e:  # noqa: BLE001 - pragma: no cover
                logger.debug("stdio guard: could not restore stdout: %s", e)
            _saved = _proxy = None
        return _depth


@contextmanager
def guard(*, force: bool = False) -> Iterator[bool]:
    """Hold stdout away from the protocol stream for the length of the block.

    Yields True when the guard is actually up. Restores on the way out however
    the block ends — return, exception or cancellation.
    """
    started = activate(force=force) > 0
    try:
        yield started
    finally:
        if started:
            deactivate()


def reset_for_tests() -> None:
    """Drop any state and put the saved stdout back (test teardown only)."""
    global _depth, _saved, _proxy
    with _lock:
        if _depth > 0 and _saved is not None:
            try:
                sys.stdout = _saved
            except Exception:  # noqa: BLE001 - pragma: no cover
                pass
        _depth = 0
        _saved = _proxy = None
