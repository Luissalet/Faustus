"""mcp_config_audit.py - a hygiene audit of the MCP servers configured in the app.

`routes/mcp/mcp_routes.py::scan_mcp_server_config` (SEC-09) looks for hostile
content in one server: a `curl ... | sh` launch line, a proxy variable,
prompt-injection markers in tool descriptions. This module looks at the
*configuration* of every server at once and reports what a careful admin
would change even when nothing is hostile:

* ``plaintext_secret``  - a credential typed straight into ``env``, ``args``
  or the URL instead of a reference to a variable (high);
* ``insecure_url``      - a remote server reached over plain ``http://``
  (high; loopback addresses are fine);
* ``unpinned_package``  - ``npx``/``uvx``/``bunx``/``pnpm dlx``/``pipx run``
  launching a package without a fixed version, or with ``@latest``, so the
  code that runs can change on any restart (medium);
* ``broad_root``        - a whole drive, ``/`` or the user's home handed to
  a server as a path argument (medium);
* ``inherits_env``      - a stdio server that receives the app's whole
  environment, provider keys and the loopback token included (medium);
* ``shell_wrapper``     - the server is started through ``cmd /c``,
  ``powershell -Command`` or ``sh -c``, so its launch line is reinterpreted
  by a shell (low);
* ``duplicate``         - two servers with the same launch line or URL (low).

Pure and offline: no network, no process is started, nothing is changed.
Every snippet that could contain a secret goes through
``security_scan.mask_secrets`` (or is masked here) before it leaves.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlsplit

SEVERITIES = ("high", "medium", "low")
_SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

_SECRET_KEY = re.compile(
    r"(?i)(api[_-]?key|apikey|secret|token|passw(or)?d|pwd|credential|private[_-]?key|access[_-]?key|auth[_-]?token|authorization|bearer|\bpat\b|_pat$)")
# A value that only *points* at a secret (resolved elsewhere) is not one.
_REFERENCE = re.compile(r"^\s*(\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|%[A-Za-z_][A-Za-z0-9_]*%|env:[A-Za-z_]\w*|"
                        r"\{\{.*\}\}|<[^>]+>|op://\S+|vault:\S+|keyring:\S+)\s*$")
_PLACEHOLDER = re.compile(r"(?i)^(x+|\*+|changeme|your[-_ ].*|replace[-_ ]?me|todo|none|null|false|true|0|1)$")
_POINTER_KEY = re.compile(r"(?i)(_|^)(file|path|dir|home|url|endpoint|env|name|id|type|mode|header)$")
_PATH_VALUE = re.compile(r"^([A-Za-z]:[\\/]|\\\\|/|~[\\/]|\./|\.\./)")
_FLAG_WITH_SECRET = re.compile(r"(?i)^--?(api[-_]?key|token|secret|password|passwd|auth(-token)?|access[-_]?key)(=(.+))?$")
_URL_SECRET_PARAM = re.compile(r"(?i)[?&](api[_-]?key|key|token|access[_-]?token|secret|password|auth)=([^&#]+)")

_RUNNERS_NPM = {"npx", "bunx"}
_RUNNERS_PY = {"uvx"}
_SHELLS = {"cmd": ("/c", "/k"), "powershell": ("-command", "-c", "-encodedcommand", "-file"),
           "pwsh": ("-command", "-c", "-encodedcommand", "-file"), "bash": ("-c",), "sh": ("-c",), "zsh": ("-c",)}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}


def _mask(value: str) -> str:
    value = str(value or "")
    if len(value) <= 4:
        return "****"
    return value[:2] + "…" + value[-2:] + f" ({len(value)} chars)"


def _base(command: str) -> str:
    name = ntpath.basename(posixpath.basename(str(command or "").strip().strip('"')))
    name = name.lower()
    for ext in (".cmd", ".exe", ".bat", ".ps1"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return name


def _is_secret_value(key: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v or v.isdigit() or _REFERENCE.match(v) or _PLACEHOLDER.match(v):
        return False
    # FOO_TOKEN_FILE=C:\...\token is the right way to do it: the variable
    # names where the secret lives, it is not the secret (seen live on 11-10
    # with every Hoard server).
    if _POINTER_KEY.search(str(key or "")) or _PATH_VALUE.match(v):
        return False
    return bool(_SECRET_KEY.search(str(key or ""))) and len(v) >= 6


def _finding(code: str, severity: str, message: str, *, server: Mapping[str, Any],
             detail: Optional[str] = None, fix: Optional[str] = None) -> Dict[str, Any]:
    out = {"code": code, "severity": severity, "server_id": server.get("id"),
           "server": server.get("name") or server.get("id"), "message": message}
    if detail:
        out["detail"] = detail
    if fix:
        out["fix"] = fix
    return out


# ── individual checks ───────────────────────────────────────────────────────

def _check_secrets(srv: Mapping[str, Any]) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for key, value in (srv.get("env") or {}).items():
        if _is_secret_value(key, value):
            found.append(_finding(
                "plaintext_secret", "high", f"env {key} holds a credential in plain text",
                server=srv, detail=f"{key}={_mask(value)}",
                fix=f"Store it in the vault or the app environment and reference it as ${{{key}}}."))
    args = [str(a) for a in (srv.get("args") or [])]
    for i, arg in enumerate(args):
        m = _FLAG_WITH_SECRET.match(arg)
        if not m:
            continue
        value = m.group(4) if m.group(4) is not None else (args[i + 1] if i + 1 < len(args) else "")
        if value and not value.startswith("-") and _is_secret_value(m.group(1), value):
            found.append(_finding(
                "plaintext_secret", "high", f"argument {arg.split('=')[0]} passes a credential on the command line",
                server=srv, detail=f"{arg.split('=')[0]} {_mask(value)}",
                fix="Command lines are visible to every process on the machine; pass it through env instead."))
    try:
        from src.security_scan import find_secrets
        launch = " ".join(args)
        for hit in find_secrets(launch)[:3]:
            found.append(_finding(
                "plaintext_secret", "high", f"the launch line contains what looks like a {hit.get('kind', 'secret')}",
                server=srv, detail=str(hit.get("preview") or ""),
                fix="Move it to env as a reference to a stored secret."))
    except Exception:  # noqa: BLE001 - the pattern check above still ran
        pass
    url = str(srv.get("url") or "")
    for m in _URL_SECRET_PARAM.finditer(url):
        found.append(_finding(
            "plaintext_secret", "high", f"the URL carries a credential in its query string ({m.group(1)})",
            server=srv, detail=f"{m.group(1)}={_mask(m.group(2))}",
            fix="Send it as a header from the vault; URLs end up in logs."))
    return found


def _check_url(srv: Mapping[str, Any]) -> List[Dict[str, Any]]:
    url = str(srv.get("url") or "").strip()
    if not url or str(srv.get("transport") or "stdio") == "stdio":
        return []
    try:
        parts = urlsplit(url)
    except ValueError:
        return []
    host = (parts.hostname or "").lower()
    if parts.scheme == "http" and host not in _LOOPBACK_HOSTS and not host.endswith(".localhost"):
        return [_finding("insecure_url", "high", f"remote server reached over plain http ({host})",
                         server=srv, detail=f"{parts.scheme}://{host}{parts.path}",
                         fix="Use https; on plain http the traffic and any token can be read or altered.")]
    return []


def _package_arg(args: List[str], start: int = 0) -> Optional[str]:
    skip_next = {"-p", "--package", "--from", "--with", "--python"}
    i = start
    while i < len(args):
        a = args[i]
        if a in skip_next:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return None


def _npm_pinned(spec: str) -> bool:
    name, sep, version = spec[1:].partition("@") if spec.startswith("@") else spec.partition("@")
    if not sep:
        return False
    return bool(version) and version.lower() not in {"latest", "next", "*", "x"}


def _py_pinned(spec: str) -> bool:
    if "==" in spec or re.search(r"@\s*\d", spec):
        return True
    return False


def _check_pinning(srv: Mapping[str, Any]) -> List[Dict[str, Any]]:
    if str(srv.get("transport") or "stdio") != "stdio":
        return []
    cmd = _base(srv.get("command") or "")
    args = [str(a) for a in (srv.get("args") or [])]
    spec, pinned, runner = None, True, cmd
    if cmd in _RUNNERS_NPM:
        spec = _package_arg(args)
        pinned = spec is None or _looks_local(spec) or _npm_pinned(spec)
    elif cmd == "pnpm" and args[:1] == ["dlx"]:
        spec, runner = _package_arg(args, 1), "pnpm dlx"
        pinned = spec is None or _looks_local(spec) or _npm_pinned(spec)
    elif cmd in _RUNNERS_PY or (cmd == "pipx" and args[:1] == ["run"]):
        runner = "pipx run" if cmd == "pipx" else cmd
        from_idx = args.index("--from") if "--from" in args else -1
        spec = args[from_idx + 1] if 0 <= from_idx < len(args) - 1 else _package_arg(args, 1 if cmd == "pipx" else 0)
        pinned = spec is None or _looks_local(spec) or _py_pinned(spec)
    if pinned:
        return []
    return [_finding("unpinned_package", "medium",
                     f"{runner} runs {spec} without a fixed version: a new release runs on the next restart",
                     server=srv, detail=f"{runner} {spec}",
                     fix=f"Pin it ({spec.split('@')[0] if not spec.startswith('@') else spec}@<version> or ==<version>) and update on purpose.")]


def _looks_local(spec: str) -> bool:
    return spec.startswith((".", "/", "\\", "file:", "git+")) or bool(re.match(r"^[A-Za-z]:[\\/]", spec))


def _norm_path(p: str) -> str:
    p = str(p or "").strip().strip('"')
    if not p:
        return ""
    if re.match(r"^[A-Za-z]:", p) or "\\" in p:
        return ntpath.normcase(ntpath.normpath(p)).rstrip("\\") or "\\"
    return posixpath.normpath(p).rstrip("/") or "/"


def _check_roots(srv: Mapping[str, Any], home: Optional[str]) -> List[Dict[str, Any]]:
    found = []
    homes = {_norm_path(h) for h in (home, os.path.expanduser("~")) if h}
    home_parents = {_norm_path(ntpath.dirname(h) if ("\\" in h or ":" in h) else posixpath.dirname(h))
                    for h in homes if h}
    for arg in (srv.get("args") or []):
        a = str(arg)
        if a.startswith("-") and "=" in a:
            a = a.split("=", 1)[1]
        n = _norm_path(a)
        if not n:
            continue
        why = None
        if re.fullmatch(r"[a-z]:\\?", n) or n in ("/", "\\"):
            why = "a whole drive"
        elif n in homes:
            why = "the whole user profile"
        elif n in home_parents and n not in ("/", "\\"):
            why = "every user profile"
        if why:
            found.append(_finding("broad_root", "medium", f"the server gets {why} ({a})", server=srv,
                                  detail=a, fix="Give it only the project folders it needs."))
    return found


def _check_env_mode(srv: Mapping[str, Any]) -> List[Dict[str, Any]]:
    if str(srv.get("transport") or "stdio") == "stdio" and srv.get("inherit_env") is True:
        return [_finding("inherits_env", "medium",
                         "the server process receives the app's whole environment (provider keys and the loopback token)",
                         server=srv, fix="Switch it to the minimal environment and declare only the variables it reads.")]
    return []


def _check_shell(srv: Mapping[str, Any]) -> List[Dict[str, Any]]:
    cmd = _base(srv.get("command") or "")
    flags = _SHELLS.get(cmd)
    if not flags or str(srv.get("transport") or "stdio") != "stdio":
        return []
    args = [str(a).lower() for a in (srv.get("args") or [])]
    if any(a in flags for a in args):
        return [_finding("shell_wrapper", "low", f"the server is started through {cmd}, which reinterprets its launch line",
                         server=srv, detail=" ".join([cmd, *args])[:200],
                         fix="Point command at the real executable and pass its arguments directly.")]
    return []


def _launch_key(srv: Mapping[str, Any]) -> Optional[str]:
    if str(srv.get("transport") or "stdio") == "stdio":
        if not srv.get("command"):
            return None
        # env is part of the identity: one generic bridge (the REST->MCP
        # adapter) serves different apps through REST_BASE_URL (seen live 11-10).
        env = {str(k): str(v) for k, v in (srv.get("env") or {}).items()}
        return "stdio:" + json.dumps([_base(srv.get("command")), *[str(a) for a in (srv.get("args") or [])],
                                      sorted(env.items())])
    url = str(srv.get("url") or "").strip().rstrip("/").lower()
    return ("url:" + url) if url else None


# ── report ──────────────────────────────────────────────────────────────────

def normalize_server(row: Any) -> Dict[str, Any]:
    """A DB row (`core.database.McpServer`) or a mapping → plain dict with
    `args` as a list and `env` as a dict."""
    get = (lambda k, d=None: row.get(k, d)) if isinstance(row, Mapping) else (lambda k, d=None: getattr(row, k, d))

    def _json(value, fallback):
        if isinstance(value, (list, dict)):
            return value if isinstance(value, type(fallback)) else fallback
        try:
            parsed = json.loads(value) if value else fallback
        except (TypeError, ValueError):
            return fallback
        return parsed if isinstance(parsed, type(fallback)) else fallback

    return {
        "id": get("id"), "name": get("name") or get("id"), "transport": get("transport") or "stdio",
        "command": get("command"), "args": _json(get("args"), []), "env": _json(get("env"), {}),
        "url": get("url"), "is_enabled": bool(get("is_enabled", True)),
        "inherit_env": get("inherit_env"),
    }


def audit_servers(servers: Iterable[Any], *, home: Optional[str] = None,
                  include_disabled: bool = True) -> Dict[str, Any]:
    rows = [normalize_server(s) for s in servers]
    if not include_disabled:
        rows = [r for r in rows if r["is_enabled"]]
    findings: List[Dict[str, Any]] = []
    for srv in rows:
        for check in (_check_secrets, _check_url, _check_pinning, _check_env_mode, _check_shell):
            findings.extend(check(srv))
        findings.extend(_check_roots(srv, home))
    seen: Dict[str, Dict[str, Any]] = {}
    for srv in rows:
        key = _launch_key(srv)
        if not key:
            continue
        if key in seen:
            first = seen[key]
            findings.append(_finding("duplicate", "low",
                                     f"same launch line as {first['name']}: the tools are loaded twice",
                                     server=srv, detail=str(first["id"]),
                                     fix="Keep one of them, or disable the copy."))
        else:
            seen[key] = srv
    for f in findings:
        srv = next((r for r in rows if r["id"] == f["server_id"]), None)
        f["enabled"] = bool(srv and srv["is_enabled"])
    # A shell-wrapper or package detail may repeat a credential that another
    # check identified. Mask every finding field, not only plaintext_secret.
    from src.security_scan import mask_secrets
    secrets = set()
    for srv in rows:
        secrets.update(str(v) for k, v in srv["env"].items() if _is_secret_value(k, v))
        args = [str(a) for a in srv["args"]]
        for i, arg in enumerate(args):
            match = _FLAG_WITH_SECRET.match(arg)
            if match:
                value = match.group(4) if match.group(4) is not None else (args[i + 1] if i + 1 < len(args) else "")
                if _is_secret_value(match.group(1), value):
                    secrets.add(value)
        secrets.update(m.group(2) for m in _URL_SECRET_PARAM.finditer(str(srv["url"] or "")))
    for f in findings:
        for key in ("message", "detail", "fix"):
            if key in f:
                text = str(f[key])
                for value in sorted(secrets, key=len, reverse=True):
                    text = re.sub(re.escape(value), lambda _: _mask(value), text, flags=re.IGNORECASE)
                f[key] = mask_secrets(text)
    findings.sort(key=lambda f: (_SEVERITY_RANK.get(f["severity"], 9), not f["enabled"], str(f["server"]), f["code"]))
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}
    per_server = []
    for srv in rows:
        mine = [f for f in findings if f["server_id"] == srv["id"]]
        worst = min((_SEVERITY_RANK[f["severity"]] for f in mine), default=None)
        per_server.append({"id": srv["id"], "name": srv["name"], "transport": srv["transport"],
                           "enabled": srv["is_enabled"], "findings": len(mine),
                           "worst": SEVERITIES[worst] if worst is not None else "none",
                           "codes": sorted({f["code"] for f in mine})})
    return {"servers": len(rows), "with_findings": sum(1 for p in per_server if p["findings"]),
            "counts": counts, "findings": findings, "per_server": per_server,
            "checks": ["plaintext_secret", "insecure_url", "unpinned_package", "broad_root",
                       "inherits_env", "shell_wrapper", "duplicate"]}


def load_configured_servers() -> List[Dict[str, Any]]:
    """Every MCP server row in the app database, normalized."""
    from core.database import McpServer, SessionLocal

    db = SessionLocal()
    try:
        return [normalize_server(r) for r in db.query(McpServer).all()]
    finally:
        db.close()


def audit_candidate(candidate: Mapping[str, Any], existing: Iterable[Any] = ()) -> Dict[str, Any]:
    """Preview one unsaved configuration against the current list, without writes.

    A supplied id replaces that row for duplicate detection; no raw launch/env
    configuration is included in the receipt. Findings concern the candidate only.
    """
    row = normalize_server(candidate)
    row["id"] = row["id"] or "__draft__"
    row["name"] = row["name"] or row["id"]
    peers = [normalize_server(s) for s in existing]
    peers = [s for s in peers if s["id"] != row["id"]]
    report = audit_servers([*peers, row])
    report["findings"] = [f for f in report["findings"] if f["server_id"] == row["id"]]
    report["per_server"] = [s for s in report["per_server"] if s["id"] == row["id"]]
    report["servers"] = 1
    report["with_findings"] = int(bool(report["findings"]))
    report["counts"] = {s: sum(f["severity"] == s for f in report["findings"]) for s in SEVERITIES}
    report["configuration"] = "candidate"
    return report
