"""Reading settings from the environment and the data-folder layout every app shares.

Standard library only. The Node twin is ``envStr`` / ``envInt`` / ``envFlag`` in ``js/hoard-commons/server.js``.

Replaces ``_env`` / ``_int`` / ``_float`` (copied in ~15 apps), ``load_dotenv`` (five identical copies), the
``"0/false/no/off"`` flag parsing and the ``db_path`` / ``token_path`` / ``url_path`` / ``logs_dir`` /
``backend_json_path`` properties of 17 ``config.py`` files. An app keeps its own ``Config`` dataclass for its own
fields and gets the layout from :class:`AppPaths` (inherit from it, or hold one).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

__all__ = ["env_str", "env_int", "env_float", "env_flag", "load_dotenv", "AppPaths", "resolve_paths", "FALSE_WORDS", "TRUE_WORDS"]

FALSE_WORDS = frozenset({"0", "false", "no", "off"})
TRUE_WORDS = frozenset({"1", "true", "yes", "on"})


def env_str(*names: str, default: Optional[str] = None) -> Optional[str]:
    """The first of ``names`` that is set to a non-blank value (stripped), else ``default``."""
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def env_int(*names: str, default: Optional[int] = None, minimum: Optional[int] = None, maximum: Optional[int] = None) -> Optional[int]:
    """The first of ``names`` that parses as an integer, clamped to ``minimum`` / ``maximum``; ``default`` when
    none does (an unparsable value is skipped, not an error)."""
    for name in names:
        raw = env_str(name)
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError:
            try:
                number = float(raw)
            except ValueError:
                continue
            if number != int(number):
                continue
            value = int(number)
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value
    return default


def env_float(*names: str, default: Optional[float] = None, minimum: Optional[float] = None, maximum: Optional[float] = None) -> Optional[float]:
    """Like :func:`env_int` for floats."""
    for name in names:
        raw = env_str(name)
        if raw is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value != value:      # NaN
            continue
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value
    return default


def env_flag(name: str, default: bool = False) -> bool:
    """``0/false/no/off`` are False, ``1/true/yes/on`` are True (any case); unset, blank or anything else gives
    ``default``."""
    raw = env_str(name)
    if raw is None:
        return default
    word = raw.lower()
    if word in FALSE_WORDS:
        return False
    if word in TRUE_WORDS:
        return True
    return default


def load_dotenv(path: Union[str, "os.PathLike[str]", None] = None) -> dict[str, str]:
    """Read a ``.env`` file (``KEY=VALUE``, ``export KEY=VALUE``, ``#`` comments, single or double quotes, a trailing
    `` # comment`` after an unquoted value) and put each key into ``os.environ`` **unless it is already set**: the
    real environment always wins. ``path`` defaults to ``.env`` in the current folder. Returns every pair parsed
    (set or not); a missing or unreadable file gives ``{}``."""
    target = Path(path) if path is not None else Path(".env")
    try:
        lines = target.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return {}
    values: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


@dataclass
class AppPaths:
    """The folder layout of an app: ``<data_dir>/<app>.db``, ``mcp-token``, ``url``, ``logs/``, ``backend.json``,
    ``cache/``. ``configured`` is True when the data folder came from the environment (the apps show a different
    first-run screen then)."""

    app: str
    root: Path
    data_dir: Path
    configured: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / f"{self.app}.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "mcp-token"

    @property
    def url_path(self) -> Path:
        return self.data_dir / "url"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backend_json_path(self) -> Path:
        return self.data_dir / "backend.json"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def ensure(self) -> "AppPaths":
        """Create the data, logs and cache folders (never the files)."""
        for folder in (self.data_dir, self.logs_dir, self.cache_dir):
            folder.mkdir(parents=True, exist_ok=True)
        return self


def resolve_paths(app: str, repo_root: Union[str, "os.PathLike[str]"], *, env_prefix: str) -> AppPaths:
    """The layout for ``app``: the data folder is ``$<env_prefix>_DATA_DIR`` (``~`` expanded, made absolute) or
    ``<repo_root>/data``. ``env_prefix`` is the upper-case app prefix (``KAFKA`` reads ``KAFKA_DATA_DIR``)."""
    root = Path(repo_root)
    raw = env_str(f"{env_prefix.upper()}_DATA_DIR")
    if raw:
        return AppPaths(app, root, Path(raw).expanduser().absolute(), configured=True)
    return AppPaths(app, root, root / "data", configured=False)
