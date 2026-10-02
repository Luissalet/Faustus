"""Measured routes: which local model a benchmark found best for what.

An app that measures the local models (Galton's Hoard) writes
``~/.hoard/routes.json`` (``HOARD_HOME`` moves ``~/.hoard``;
``HOARD_ROUTES_FILE`` names the file itself)::

    {"schema": 1, "source": "galton", "updated_at": "2026-10-02T10:00:00+02:00",
     "tasks": {"code": {"capability": "llm",
                        "prefer": [{"names": ["qwen3.8:27b-q4_K_M", "qwen3.8-27b-q4-llamacpp"],
                                    "score": 0.81, "ci": [0.74, 0.87], "n": 60,
                                    "tok_s": 34.2, "vram_gb": 17.1}],
                        "explain": "..."}},
     "capabilities": {"llm": {"prefer": [...]}, "vision": {"prefer": [...]}}}

:func:`load_routes` reads it (cached by path, mtime and size) and never
raises: a missing or broken file is an empty :class:`Routes` whose
``problem`` says why. :class:`~hoard_link.link.Link` only uses the names
to *order* candidates it already considers; routes never load a model and
never override an explicit URL or command.

Standard library only.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

__all__ = ["Routes", "Pref", "TaskRoute", "load_routes", "routes_path", "names_match", "name_keys"]

_CACHE: dict[str, tuple[int, int, "Routes"]] = {}
_LOCK = threading.Lock()


def routes_path(path: Optional[str | Path] = None) -> Path:
    """The routes file: ``path``, else ``HOARD_ROUTES_FILE``, else ``~/.hoard/routes.json``."""
    if path:
        return Path(path).expanduser()
    env = (os.environ.get("HOARD_ROUTES_FILE") or "").strip()
    if env:
        return Path(env).expanduser()
    home = (os.environ.get("HOARD_HOME") or "").strip()
    return (Path(home).expanduser() if home else Path.home() / ".hoard") / "routes.json"


def name_keys(name: Any) -> set[str]:
    """Every spelling under which two model names count as the same model:
    case-insensitive, with or without an Ollama ``:latest`` suffix, and (for
    a GGUF file) by file-name stem, so ``D:\\models\\Qwen.gguf`` is ``qwen``."""
    if not isinstance(name, str):
        return set()
    n = name.strip().lower()
    if not n:
        return set()
    keys = {n}
    if n.endswith(":latest"):
        keys.add(n[: -len(":latest")])
    if n.endswith(".gguf"):
        stem = n.replace("\\", "/").rsplit("/", 1)[-1][: -len(".gguf")]
        if stem:
            keys.add(stem)
    return keys


def names_match(a: Any, b: Any) -> bool:
    ka, kb = name_keys(a), name_keys(b)
    return bool(ka and kb and ka & kb)


@dataclass(frozen=True)
class Pref:
    """One measured preference: the names a model goes by, and how it scored."""

    names: tuple[str, ...]
    score: Optional[float] = None
    ci: Optional[tuple[float, float]] = None
    n: Optional[int] = None
    tok_s: Optional[float] = None
    vram_gb: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {"names": list(self.names), "score": self.score, "ci": list(self.ci) if self.ci else None,
                "n": self.n, "tok_s": self.tok_s, "vram_gb": self.vram_gb}


@dataclass(frozen=True)
class TaskRoute:
    capability: Optional[str]
    prefer: tuple[Pref, ...] = ()
    explain: str = ""


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _parse_prefs(raw: Any) -> tuple[Pref, ...]:
    out: list[Pref] = []
    if not isinstance(raw, list):
        return ()
    for item in raw:
        if not isinstance(item, dict):
            continue
        names = item.get("names")
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list):
            continue
        names = tuple(n.strip() for n in names if isinstance(n, str) and n.strip())
        if not names:
            continue
        ci = item.get("ci")
        ci_t = (float(ci[0]), float(ci[1])) if (isinstance(ci, list) and len(ci) == 2
                                                 and all(_num(x) is not None for x in ci)) else None
        n = item.get("n")
        out.append(Pref(names=names, score=_num(item.get("score")), ci=ci_t,
                        n=int(n) if isinstance(n, int) and not isinstance(n, bool) else None,
                        tok_s=_num(item.get("tok_s")), vram_gb=_num(item.get("vram_gb"))))
    return tuple(out)


def _flatten(prefs: tuple[Pref, ...]) -> list[str]:
    return [name for p in prefs for name in p.names]


@dataclass(frozen=True)
class Routes:
    path: str = ""
    source: Optional[str] = None
    updated_at: Optional[str] = None
    tasks: dict[str, TaskRoute] = field(default_factory=dict)
    capabilities: dict[str, tuple[Pref, ...]] = field(default_factory=dict)
    problem: Optional[str] = None

    @property
    def empty(self) -> bool:
        return not self.tasks and not any(self.capabilities.values())

    def preferences(self, capability: str, task: Optional[str] = None) -> list[str]:
        """Model names to prefer for ``capability``: the task's own list first
        (only when that task is about this capability), then the capability's,
        de-duplicated with the order kept."""
        names: list[str] = []
        if task:
            route = self.tasks.get(task)
            if route is not None and route.capability in (None, capability):
                names.extend(_flatten(route.prefer))
        names.extend(_flatten(self.capabilities.get(capability, ())))
        seen: set[str] = set()
        out: list[str] = []
        for name in names:
            key = name.strip().lower()
            if key not in seen:
                seen.add(key)
                out.append(name)
        return out

    def summary(self) -> dict[str, Any]:
        """The ``routes`` block of :meth:`Link.status`."""
        return {"file": self.path, "updated_at": self.updated_at, "source": self.source,
                "tasks": sorted(self.tasks), "problem": self.problem}


def _parse(path: str, raw: Any) -> Routes:
    if not isinstance(raw, dict):
        return Routes(path=path, problem="not a JSON object")
    tasks: dict[str, TaskRoute] = {}
    raw_tasks = raw.get("tasks")
    if isinstance(raw_tasks, dict):
        for name, t in raw_tasks.items():
            if not isinstance(t, dict):
                continue
            cap = t.get("capability")
            tasks[str(name)] = TaskRoute(capability=cap if isinstance(cap, str) and cap else None,
                                         prefer=_parse_prefs(t.get("prefer")),
                                         explain=str(t.get("explain") or ""))
    caps: dict[str, tuple[Pref, ...]] = {}
    raw_caps = raw.get("capabilities")
    if isinstance(raw_caps, dict):
        for name, c in raw_caps.items():
            if isinstance(c, dict):
                caps[str(name)] = _parse_prefs(c.get("prefer"))
    source = raw.get("source")
    updated = raw.get("updated_at")
    return Routes(path=path, source=source if isinstance(source, str) else None,
                  updated_at=updated if isinstance(updated, str) else None, tasks=tasks, capabilities=caps)


def load_routes(path: Optional[str | Path] = None) -> Routes:
    """The measured routes, cached by (path, mtime, size). Never raises."""
    p = routes_path(path)
    key = str(p)
    try:
        st = p.stat()
    except OSError:
        return Routes(path=key, problem="no routes file")
    stamp = (st.st_mtime_ns, st.st_size)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and (hit[0], hit[1]) == stamp:
            return hit[2]
    try:
        # utf-8-sig: Windows Notepad saves UTF-8 with a BOM.
        routes = _parse(key, json.loads(p.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as exc:
        routes = Routes(path=key, problem=f"unreadable routes file: {type(exc).__name__}: {exc}"[:200])
    except Exception as exc:  # noqa: BLE001  (a surprising shape must not break resolution)
        routes = Routes(path=key, problem=f"invalid routes file: {type(exc).__name__}: {exc}"[:200])
    with _LOCK:
        _CACHE[key] = (stamp[0], stamp[1], routes)
    return routes
