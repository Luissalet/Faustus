"""native_env.py — the environment a foreign child process should be handed.

Faustus runs inside its own virtualenv, so its process environment carries
``VIRTUAL_ENV``, a ``PYTHONPATH``, and a ``PATH`` whose first entry is the
venv's ``bin``/``Scripts``. Any subprocess that inherits that environment
resolves ``python``, ``pip`` and its imports against *our* interpreter and
*our* site-packages instead of its own. A user's project test run, an external
agent runner, a python-based CLI agent: each silently borrows our environment,
and the symptom is the worst kind — it works on the developer's machine and
imports the wrong package on the user's.

SEC-1 (B-008): stripping venv markers is not the same as being safe. For a
child with no business seeing the operator's credentials use
``profile_environment(profile, ...)`` — an allowlist — and pass what the child
does need through ``extra``. ``native_host_environment`` is the wide door: it
hands over everything except the venv marks and Faustus's private variables.

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
    return scrub_private(out)


# ── Allowlist profiles (SEC-1 / B-008) ──────────────────────────────────────
# `native_host_environment` answers "what did our virtualenv add?". It does not
# answer "what does this child actually need?", and the difference is the bug:
# a foreign child was handed every provider key, cloud credential and
# repository token the operator had exported, because they happened to be in
# our process.
#
# A profile is an ALLOWLIST: the child gets what makes a program run at all
# (where to find binaries, where to write temp files, which locale, which CA
# bundle), plus what its own job needs, plus what the caller names explicitly.
# Everything else stays here.

#: Never handed to any child, in any profile, inherit-all included. The
#: internal token authenticates the in-process tool loopback: a child holding
#: it can call privileged routes as Faustus itself.
FAUSTUS_PRIVATE_NAMES: Tuple[str, ...] = (
    "ODYSSEUS_INTERNAL_TOKEN",
    "FAUSTUS_INTERNAL_TOKEN",
)
FAUSTUS_PRIVATE_PREFIXES: Tuple[str, ...] = (
    "ODYSSEUS_INTERNAL",
    "FAUSTUS_INTERNAL",
)

#: What any process needs to start, find its tools and write a temp file.
#: Structural, not secret — without these a child fails in ways that look like
#: a Faustus bug ("python: command not found", "no such locale").
STRUCTURAL_NAMES: Tuple[str, ...] = (
    # POSIX
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE", "DISPLAY", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    # Windows
    "ALLUSERSPROFILE", "APPDATA", "COMMONPROGRAMFILES", "COMPUTERNAME", "COMSPEC",
    "DRIVERDATA", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "NUMBER_OF_PROCESSORS",
    "OS", "PATHEXT", "PROCESSOR_ARCHITECTURE", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "PROGRAMW6432", "PSMODULEPATH", "PUBLIC", "SESSIONNAME",
    "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "USERDOMAIN", "USERNAME",
    "USERPROFILE", "WINDIR",
    # TLS trust and proxy: without them a child reaches nothing, and on a
    # corporate machine the proxy variables are how it reaches anything.
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
)

#: Toolchain caches and roots. A build that cannot find its cache re-downloads
#: the world; none of these is a credential.
BUILD_NAMES: Tuple[str, ...] = (
    "CC", "CXX", "CFLAGS", "CXXFLAGS", "LDFLAGS", "MAKEFLAGS", "MAKELEVEL",
    "CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOCACHE", "GOMODCACHE", "GOROOT",
    "JAVA_HOME", "GRADLE_USER_HOME", "MAVEN_OPTS", "M2_HOME",
    "NODE_OPTIONS", "NPM_CONFIG_CACHE", "npm_config_cache", "PNPM_HOME",
    "NVM_DIR", "COREPACK_HOME", "YARN_CACHE_FOLDER",
    "PIP_CACHE_DIR", "PIPX_HOME", "POETRY_CACHE_DIR", "UV_CACHE_DIR",
    "DOTNET_ROOT", "DOTNET_CLI_TELEMETRY_OPTOUT", "NUGET_PACKAGES",
    "CI", "CUDA_VISIBLE_DEVICES", "CUDA_PATH",
)

#: Git's own knobs. `SSH_AUTH_SOCK` is deliberately here and nowhere else: it
#: is a handle to the user's ssh-agent — what makes `git push` work, and
#: exactly what a foreign agent should not be holding.
GIT_NAMES: Tuple[str, ...] = (
    "GIT_ASKPASS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_TERMINAL_PROMPT", "GIT_EDITOR",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    "SSH_AUTH_SOCK", "SSH_AGENT_PID",
)

PROFILE_SYSTEM = "system"
PROFILE_BUILD = "build"
PROFILE_GIT = "git"
PROFILE_AGENT = "agent"
PROFILE_MCP = "mcp"

#: profile -> the names it allows *beyond* the structural set.
PROFILES: Dict[str, Tuple[str, ...]] = {
    PROFILE_SYSTEM: (),
    PROFILE_BUILD: BUILD_NAMES,
    PROFILE_GIT: GIT_NAMES,
    # An external agent CLI gets nothing of ours: its endpoint, its model and
    # its own configuration arrive through `extra`, named one by one by the
    # runner table.
    PROFILE_AGENT: (),
    # Same for an MCP server: what its row declares, and nothing else.
    PROFILE_MCP: (),
}


def is_private_name(name: str) -> bool:
    """Is this one of Faustus's own variables, which no child may ever see?"""
    key = str(name or "").upper()
    if key in FAUSTUS_PRIVATE_NAMES:
        return True
    return any(key.startswith(prefix) for prefix in FAUSTUS_PRIVATE_PREFIXES)


def scrub_private(env: Mapping[str, str]) -> Dict[str, str]:
    """A copy of `env` without Faustus's own privileged variables."""
    return {k: v for k, v in env.items() if not is_private_name(k)}


def profile_allowlist(profile: str) -> Tuple[str, ...]:
    """The variable names `profile` allows. Unknown profile -> structural only."""
    return tuple(STRUCTURAL_NAMES) + tuple(PROFILES.get(str(profile or ""), ()))


def profile_environment(profile: str, *, base: Optional[Mapping[str, str]] = None,
                        extra: Optional[Mapping[str, str]] = None,
                        allow: Optional[Iterable[str]] = None,
                        inherit_all: bool = False) -> Dict[str, str]:
    """The environment a foreign child gets under `profile`.

    `inherit_all=True` is the documented escape hatch for a server or runner
    that genuinely needs the operator's whole environment. It still loses the
    venv markers and Faustus's own private variables — that part is not
    negotiable and not configurable, because the internal token is a key to
    this application, not a setting.

    `allow` names extra variables this particular child may read from the
    operator's environment — the explicit, per-run grant the audit asks for.
    `extra` is layered last and is never filtered: a caller naming a variable
    means it. Names are matched case-insensitively, which is what Windows does
    with its environment anyway.
    """
    source: Mapping[str, str] = os.environ if base is None else base
    if inherit_all:
        out = scrub_private(native_host_environment(source))
    else:
        allowed = {name.upper() for name in profile_allowlist(profile)}
        allowed.update(str(name).upper() for name in (allow or ()) if name)
        roots = _roots_for(source)
        out = {}
        for key, value in source.items():
            if key is None or str(key).upper() not in allowed:
                continue
            if is_private_name(key) or str(key) in VENV_MARKERS:
                continue
            out[str(key)] = "" if value is None else str(value)
        path = out.get("PATH")
        if path:
            out["PATH"] = _strip_venv_from_path(path, roots)
    for key, value in (extra or {}).items():
        if key is None:
            continue
        out[str(key)] = "" if value is None else str(value)
    return scrub_private(out)
