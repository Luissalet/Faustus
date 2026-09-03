"""native_env.py — the environment a foreign child process should be handed.

Faustus runs inside its own virtualenv, so its process environment carries
``VIRTUAL_ENV``, a ``PYTHONPATH``, and a ``PATH`` whose first entry is the
venv's ``bin``/``Scripts``. Any subprocess that inherits that environment
resolves ``python``, ``pip`` and its imports against *our* interpreter and
*our* site-packages instead of its own. A user's project test run, an external
agent runner, a python-based CLI agent: each silently borrows our environment,
and the symptom is the worst kind — it works on the developer's machine and
imports the wrong package on the user's.

``native_host_environment()`` is for children that are **not ours**. Faustus
spawning its *own* python (the built-in MCP servers, anything built on
``builtin_python_env``) must keep the venv: that inheritance is the point, not
a leak.

Stdlib only.
"""
from __future__ import annotations

import os
import sys
from typing import Dict, Iterable, Mapping, Optional, Tuple

# Variables that exist only because an activated environment put them there.
# Dropping them is what makes a child resolve its own interpreter.
VENV_MARKERS: Tuple[str, ...] = (
    "VIRTUAL_ENV",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONNOUSERSITE",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
)

# The markers that name a directory, so a PATH entry can be tested against it.
_ROOT_MARKERS: Tuple[str, ...] = ("VIRTUAL_ENV", "CONDA_PREFIX")


def _norm(path: str) -> str:
    """A path in the form paths are compared in on this platform.

    ``normcase`` is the whole point: on Windows it case-folds and turns ``/``
    into ``\\`` so ``C:/Proj/venv/Scripts`` and ``c:\\proj\\venv\\scripts`` are
    one directory; elsewhere it is the identity, because they are not.
    """
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(path))))
    except (OSError, ValueError):        # embedded NUL, absurd length
        return os.path.normcase(path.strip())


def _dedup(paths: Iterable[str]) -> Tuple[str, ...]:
    seen: Dict[str, None] = {}
    for path in paths:
        if not path:
            continue
        normalised = _norm(path)
        if normalised and normalised not in seen:
            seen[normalised] = None
    return tuple(seen)


def detected_venv_roots() -> Tuple[str, ...]:
    """Every directory *this process* considers "the environment we run in".

    ``sys.prefix != sys.base_prefix`` is the reliable signal (it holds for venv
    and virtualenv whether or not anyone ran ``activate``); the two variables
    cover an environment activated around us that we did not create.
    """
    roots = []
    if getattr(sys, "prefix", None) and getattr(sys, "base_prefix", None) and sys.prefix != sys.base_prefix:
        roots.append(sys.prefix)
    roots.extend(os.environ.get(var) or "" for var in _ROOT_MARKERS)
    return _dedup(roots)


def _roots_for(env: Mapping[str, str]) -> Tuple[str, ...]:
    """The roots to strip from `env`: this process's, plus the ones `env` names.

    An environment being cleaned may describe a venv other than the one we are
    running in — a base assembled by a caller, or a child's inherited copy —
    and its PATH must lose that venv's entries too.
    """
    return _dedup(list(detected_venv_roots()) + [env.get(var) or "" for var in _ROOT_MARKERS])


def _is_under(path: str, roots: Iterable[str]) -> bool:
    if not path:
        return False
    candidate = _norm(path)
    if not candidate:
        return False
    for root in roots:
        if candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def is_venv_path(path: str) -> bool:
    """Is `path` this process's own environment directory, or inside it?"""
    return _is_under(path, detected_venv_roots())


def _strip_venv_from_path(raw: str, roots: Iterable[str]) -> str:
    """`raw` with its venv entries removed, order and separator preserved.

    Returns `raw` unchanged when nothing usable would be left: a child with no
    PATH cannot exec anything at all, so a leaked venv beats a broken spawn.
    """
    roots = tuple(roots)
    if not roots:
        return raw
    kept = [entry for entry in raw.split(os.pathsep) if not _is_under(entry, roots)]
    if not any(entry.strip() for entry in kept):
        return raw
    return os.pathsep.join(kept)


def native_host_environment(base: Optional[Mapping[str, str]] = None, *,
                            extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment a FOREIGN child should get: ours, minus the marks our
    own virtualenv left on it.

    `base` defaults to this process's environment and is never mutated.
    `extra` is layered on top and is never filtered — a caller that asks for a
    variable by name gets it, venv marker or not.
    """
    source: Mapping[str, str] = os.environ if base is None else base
    roots = _roots_for(source)
    out: Dict[str, str] = {}
    for key, value in source.items():
        if key is None or key in VENV_MARKERS:
            continue
        out[str(key)] = "" if value is None else str(value)
    path = out.get("PATH")
    if path:
        out["PATH"] = _strip_venv_from_path(path, roots)
    for key, value in (extra or {}).items():
        if key is None:
            continue
        out[str(key)] = "" if value is None else str(value)
    return out
