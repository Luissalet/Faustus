"""Credential locations the agent may never read (Agora #207).

One central list, enforced in two places that cannot disagree because both
call :func:`read_denial`:

* ``ToolRunSecurityContext.decision_for`` (src/tool_capabilities.py) refuses
  the call outright -- no approval card, because an approval would only teach
  people to click "yes" on "read my private key".
* ``execute_tool_block`` (src/tool_execution.py) refuses it again right before
  dispatch, whatever security context the caller passed (including
  ``NO_TOOL_SECURITY_CONTEXT``) and whatever ``tool_approval_mode`` says
  (full access included).

Two tiers:

``credential store``  ~/.ssh, ~/.aws, ~/.gnupg, ~/.config/gcloud, ~/.azure,
                      browser password/cookie databases, the Windows
                      credential / DPAPI / vault folders, ``.netrc`` and
                      friends, and Faustus's own secret files under DATA_DIR.
                      Refused ALWAYS, even when the active workspace happens
                      to contain them (a workspace bound to the home folder
                      must not turn ~/.ssh into "project files").
``key / env file``    ``*.pem``, ``*.key``, ``id_rsa*`` (not ``*.pub``),
                      ``.env`` / ``.env.*`` (not ``.env.example``) ...
                      Refused OUTSIDE the active workspace. Inside it they are
                      the project's own files: the read works and the result
                      is marked ``sensitive`` so the egress guard
                      (src/egress_guard.py) knows those values are secrets.

Beneath both, the file tools keep their own resolver deny list
(``src.tool_execution._is_sensitive_path``), which also refuses ``.env``,
``id_rsa`` and shell rc files INSIDE the workspace; that list now consults
:func:`is_credential_location` too, so ~/.aws, browser databases, DPAPI and
Faustus's own secrets are refused by every resolver-based tool as well.

Shell commands are out of scope here: ``bash``/``python`` are gated after
outside content and governed by the sandbox / command guard.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Sequence

# Tools whose job is to read file contents (or list files by pattern). The
# check also applies to an MCP-qualified tool whose last segment is one of
# these (``mcp__filesystem__read_text_file``).
SENSITIVE_READ_TOOLS = frozenset({
    "read_file", "read_text_file", "read_multiple_files", "read_media_file",
    "grep", "glob",
    "inspect_image", "inspect_media", "inspect_deliverable", "extract_to_schema",
})
_PATH_KEYS = ("path", "paths", "file", "files", "file_path", "filepath", "filename",
              "image", "image_path", "source", "directory", "dir", "root")

# Consecutive path components (casefolded) that mark a credential store.
CREDENTIAL_DIRS: tuple = (
    (".ssh",), (".aws",), (".gnupg",), (".azure",), (".password-store",),
    (".config", "gcloud"), (".local", "share", "keyrings"), ("library", "keychains"),
    ("appdata", "roaming", "microsoft", "credentials"),
    ("appdata", "local", "microsoft", "credentials"),
    ("appdata", "roaming", "microsoft", "protect"),
    ("appdata", "local", "microsoft", "protect"),
    ("appdata", "roaming", "microsoft", "vault"),
    ("appdata", "local", "microsoft", "vault"),
    ("windows", "system32", "config"),
)
# File names that are credentials wherever they live.
CREDENTIAL_FILES = frozenset({".netrc", "_netrc", ".git-credentials", ".pgpass", ".pypirc"})
# Browser password / cookie / key databases, when under a browser profile.
BROWSER_SECRET_FILES = frozenset({
    "login data", "login data for account", "cookies", "web data", "local state",
    "key3.db", "key4.db", "logins.json", "cookies.sqlite", "signons.sqlite", "cert9.db",
})
BROWSER_MARKERS = frozenset({
    "chrome", "google-chrome", "chromium", "edge", "bravesoftware", "brave-browser",
    "firefox", "mozilla", "opera software", "vivaldi",
})
# Key material and .env files: refused outside the workspace, flagged inside.
KEY_FILE_GLOBS = ("*.pem", "*.key", "*.p12", "*.pfx", "*.ppk", "*.jks", "*.keystore", "*.kdbx",
                  "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc")
ENV_FILE_GLOBS = (".env", ".env.*", "*.env")
ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")
# Faustus's own secrets under DATA_DIR (src/backup_service.py SECRET_FILES /
# SECRET_DIRS plus the API keys file of src/api_key_manager.py).
FAUSTUS_SECRET_FILES = frozenset({".app_key", "auth.json", "sessions.json", "vault.json",
                                  "integrations.json", "workflow_credentials.json", "api_keys.json"})
FAUSTUS_SECRET_DIRS = frozenset({"mcp_oauth"})

_WIN_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//[^/])")


@dataclass(frozen=True)
class PathVerdict:
    tier: str        # "credential_store" | "key_file" | ""
    label: str = ""  # human words for the message
    inside_workspace: bool = False

    @property
    def refused(self) -> bool:
        return self.tier == "credential_store" or (self.tier == "key_file" and not self.inside_workspace)

    @property
    def sensitive(self) -> bool:
        return bool(self.tier)


def tool_reads_files(tool_name: Any) -> bool:
    if not isinstance(tool_name, str) or not tool_name:
        return False
    return tool_name.rsplit("__", 1)[-1] in SENSITIVE_READ_TOOLS


def _parse(content: Any) -> Any:
    if isinstance(content, Mapping):
        return content
    raw = content if isinstance(content, str) else ("" if content is None else str(content))
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        return data if isinstance(data, Mapping) else raw
    return raw


def read_targets(tool_name: Any, content: Any) -> List[str]:
    """The paths a file-reading call names (empty when it names none)."""
    if not tool_reads_files(tool_name):
        return []
    base = str(tool_name).rsplit("__", 1)[-1]
    data = _parse(content)
    out: List[str] = []
    if isinstance(data, Mapping):
        keys = _PATH_KEYS + (("pattern",) if base == "glob" else ())
        for key in keys:
            value = data.get(key)
            values = value if isinstance(value, (list, tuple)) else [value]
            for v in values:
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
        if base == "glob" and out and isinstance(data.get("path"), str) and isinstance(data.get("pattern"), str):
            pat = data["pattern"].strip()
            if pat and not _looks_absolute(_expand(pat)):
                out.append(os.path.join(data["path"].strip(), pat))
    elif isinstance(data, str) and data and base in {"read_file", "glob"}:
        out.append(data.split("\n", 1)[0].strip())
    return [p for p in out if p]


def _expand(path: str) -> str:
    p = str(path).strip().strip("\"'")
    if p.lower().startswith("file://"):
        p = p[7:]
    try:
        p = os.path.expandvars(os.path.expanduser(p))
    except Exception:  # noqa: BLE001
        pass
    return p


def _looks_absolute(path: str) -> bool:
    return os.path.isabs(path) or bool(_WIN_ABS_RE.match(path))


def _resolve(path: str, roots: Sequence[str]) -> str:
    p = _expand(path)
    if not _looks_absolute(p):
        base = next((r for r in roots if r), "") or os.getcwd()
        p = os.path.join(base, p)
    if _WIN_ABS_RE.match(p) and os.name != "nt":
        # A Windows path checked on another OS: normalise without realpath,
        # which would glue it under the current directory.
        return p.replace("\\", "/")
    try:
        return os.path.realpath(p)
    except (OSError, ValueError):
        return os.path.normpath(p)


def _components(path: str) -> List[str]:
    return [c.casefold() for c in re.split(r"[\\/]+", path) if c and c not in (".",)]


def _has_sequence(parts: Sequence[str], seq: Sequence[str]) -> bool:
    n = len(seq)
    return any(tuple(parts[i:i + n]) == tuple(seq) for i in range(len(parts) - n + 1))


def _inside(root: str, resolved: str) -> bool:
    if not root:
        return False
    root_r = _resolve(root, ())
    a, b = resolved.replace("\\", "/").rstrip("/"), root_r.replace("\\", "/").rstrip("/")
    if os.name == "nt" or _WIN_ABS_RE.match(b):
        a, b = a.casefold(), b.casefold()
    return a == b or a.startswith(b + "/")


def _data_dir() -> str:
    try:
        from src.constants import DATA_DIR
        return str(DATA_DIR or "")
    except Exception:  # noqa: BLE001
        return ""


def _is_env_file(name: str) -> bool:
    if not any(fnmatch.fnmatch(name, g) for g in ENV_FILE_GLOBS):
        return False
    return not name.endswith(ENV_TEMPLATE_SUFFIXES)


def _is_key_file(name: str) -> bool:
    if name.endswith(".pub"):
        return False
    return any(fnmatch.fnmatch(name, g) for g in KEY_FILE_GLOBS)


def classify_path(path: str, workspace_roots: Iterable[str] = (), data_dir: Optional[str] = None) -> PathVerdict:
    """Where ``path`` (absolute, ~-relative or workspace-relative) falls."""
    roots = [str(r) for r in (workspace_roots or ()) if r]
    resolved = _resolve(path, roots)
    # Judge both what was written and where it really points (a symlink in
    # the workspace to ~/.ssh resolves outside it).
    candidates = {resolved}
    raw = _expand(path)
    if _looks_absolute(raw):
        candidates.add(raw)
    for cand in candidates:
        parts = _components(cand)
        if not parts:
            continue
        for seq in CREDENTIAL_DIRS:
            if _has_sequence(parts, seq):
                return PathVerdict("credential_store", "the credential store " + "/".join(seq))
        name = parts[-1]
        if name in CREDENTIAL_FILES:
            return PathVerdict("credential_store", f"the credential file {name}")
        if name in BROWSER_SECRET_FILES and any(m in parts for m in BROWSER_MARKERS):
            return PathVerdict("credential_store", "a browser password/cookie database")
    ddir = data_dir if data_dir is not None else _data_dir()
    if ddir and _inside(ddir, resolved):
        rel = _components(resolved)[len(_components(_resolve(ddir, ()))):]
        if rel and (rel[0] in FAUSTUS_SECRET_DIRS or (len(rel) == 1 and rel[0] in FAUSTUS_SECRET_FILES)):
            return PathVerdict("credential_store", "Faustus's own secrets (" + "/".join(rel) + ")")
    # (A glob pattern's last segment, "*.pem", is matched as text too.)
    name = (_components(resolved) or [""])[-1]
    if _is_env_file(name) or _is_key_file(name):
        inside = any(_inside(r, resolved) for r in roots)
        label = "an .env file" if _is_env_file(name) else "a private key / key store file"
        return PathVerdict("key_file", label, inside_workspace=inside)
    return PathVerdict("")


def is_credential_location(resolved: str) -> bool:
    """True for a credential store (the always-refused tier). The file tools'
    own deny list (``src.tool_execution._is_sensitive_path``: .ssh, .gnupg,
    .env, id_rsa, shell rc files...) consults this too, so both layers share
    one list of credential locations.

    Hot path (the resolver runs it per file a glob or grep walks), so no
    realpath here: ``resolved`` is already resolved by the caller."""
    try:
        parts = _components(str(resolved))
        if not parts:
            return False
        if any(_has_sequence(parts, seq) for seq in CREDENTIAL_DIRS):
            return True
        name = parts[-1]
        if name in CREDENTIAL_FILES:
            return True
        if name in BROWSER_SECRET_FILES and any(m in parts for m in BROWSER_MARKERS):
            return True
        ddir = _data_dir()
        if ddir:
            dparts = _DATA_PARTS.get(ddir)
            if dparts is None:
                dparts = _DATA_PARTS[ddir] = _components(_resolve(ddir, ()))
            if dparts and parts[:len(dparts)] == dparts:
                rel = parts[len(dparts):]
                return bool(rel) and (rel[0] in FAUSTUS_SECRET_DIRS
                                      or (len(rel) == 1 and rel[0] in FAUSTUS_SECRET_FILES))
        return False
    except Exception:  # noqa: BLE001 - the gate/executor check (read_denial) still applies
        return False


_DATA_PARTS: dict = {}


def read_verdicts(tool_name: Any, content: Any, workspace_roots: Iterable[str] = ()) -> List[tuple]:
    roots = [r for r in (workspace_roots or ()) if r]
    return [(p, classify_path(p, roots)) for p in read_targets(tool_name, content)]


def read_denial(tool_name: Any, content: Any, workspace_roots: Iterable[str] = ()) -> Optional[str]:
    """A refusal message when the call would read a credential location."""
    for path, verdict in read_verdicts(tool_name, content, workspace_roots):
        if not verdict.refused:
            continue
        if verdict.tier == "credential_store":
            return (f"Refused: '{tool_name}' would read {verdict.label} ({path}). Credential locations "
                    "are never readable by the agent, in any approval mode. If the user really wants to "
                    "share something from there, they can copy it into the workspace themselves.")
        return (f"Refused: '{tool_name}' would read {verdict.label} outside the active workspace ({path}). "
                "Key files and .env files are readable only inside the folder this turn is bound to.")
    return None


def reads_sensitive_inside_workspace(tool_name: Any, content: Any, workspace_roots: Iterable[str] = ()) -> bool:
    return any(v.sensitive and not v.refused for _, v in read_verdicts(tool_name, content, workspace_roots))


_ENV_LINE = re.compile(r"^\s*(?:\d+[\s:|]+)?(?:export\s+)?[A-Za-z_][A-Za-z0-9_.-]*\s*[=:]\s*['\"]?([^'\"\s#]{8,})",
                       re.MULTILINE)


def secret_values_from_text(text: str, limit: int = 200) -> List[str]:
    """Values of KEY=VALUE lines (a .env read) and PEM bodies, for the egress guard."""
    out: List[str] = []
    for m in _ENV_LINE.finditer(text or ""):
        out.append(m.group(1))
        if len(out) >= limit:
            break
    for m in re.finditer(r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s*([A-Za-z0-9+/=\s]{40,})", text or ""):
        body = "".join(m.group(1).split())
        out.extend(body[i:i + 32] for i in range(0, min(len(body), 320), 32) if len(body[i:i + 32]) == 32)
    return out
