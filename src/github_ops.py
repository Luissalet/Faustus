"""github_ops.py -- the `/github` composer command and its agent/MCP twins:
labels, release notes and release checks, built on the GitHub CLI (`gh`)
and on plain local git.

Three jobs, one module, no framework:

* ``labels``        list the repository's labels (`gh label list`); ``labels
                    sync <file>`` compares a small YAML/JSON file in the repo
                    with them and plans the creates/updates. The plan is READ
                    ONLY. Applying it is a remote write and needs BOTH the
                    repository policy (``push`` in `src.agent_git_policy`) AND
                    an explicit approval -- see "Approval" below.
* ``notes``         release notes drafted from the first-parent history
                    between two refs (default: the last tag .. HEAD). Merged
                    PRs (title, labels, author) come from `gh pr list` when
                    `gh` can answer; without it the notes still work from
                    local git alone (conventional-commit prefixes group the
                    entries) and say why labels were not used.
* ``release-check`` a checklist: clean tree, branch pushed, tag free / on
                    HEAD, CI status of HEAD, open blocker issues/PRs, version
                    strings consistent. The GitHub items degrade to "skipped:
                    <reason>" when `gh` is missing, unauthenticated or the
                    remote is not GitHub; the local items never need `gh`.

Nothing here pushes, and nothing creates a release or a tag: the only remote
write is ``gh label create|edit``.

Design notes
------------
* ``run_gh`` is the one seam that shells out (a real argv, never a shell,
  ``stdin=DEVNULL``). Tests monkeypatch ``github_ops.run_gh`` with a canned
  double; ``git_panel.run_git`` is the git seam, used for real in tests
  against a throw-away repository.
* Errors are values, not exceptions: every public function returns a dict
  with ``ok`` and, on failure, ``error_class`` + ``error`` -- the vocabulary
  the other git tools use (``gh.missing``, ``gh.unauthenticated``,
  ``gh.no_remote``, ``git.policy_denied`` ...).
* Approval. ``prepare_label_sync`` returns the plan and, only when the
  policy allows remote writes and there is something to do, a single-use
  ``approval_id`` bound to the owner, the repository and a digest of the
  plan. ``apply_label_sync`` re-checks the policy, re-reads the remote
  labels and refuses (``approval.stale``) when the plan is no longer the one
  the human saw.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src import git_panel
from src.git_github import GhNotAvailableError, _gh_env
from src.github_pr import _parse_remote_url

logger = logging.getLogger(__name__)

GH_TIMEOUT = 25.0
APPROVAL_TTL = 600.0
DEFAULT_BLOCKER_LABELS: Tuple[str, ...] = ("blocker", "release-blocker")

SUBCOMMANDS = ("labels", "notes", "release-check")

USAGE = {
    "labels": "/github labels",
    "labels_sync": "/github labels sync <file.yml|file.json>",
    "notes": "/github notes [<from-tag>..<to>]",
    "release_check": "/github release-check [<tag>]",
}


# ---------------------------------------------------------------------------
# i18n -- the markdown is shown to the user, so the headings follow the UI
# ---------------------------------------------------------------------------
_ES: Dict[str, str] = {
    "Release notes": "Notas de la versión",
    "Changes since": "Cambios desde",
    "No tags yet": "Sin etiquetas (tags) todavía",
    "Breaking changes": "Cambios incompatibles",
    "Security": "Seguridad",
    "Features": "Novedades",
    "Fixes": "Correcciones",
    "Performance": "Rendimiento",
    "Documentation": "Documentación",
    "Maintenance": "Mantenimiento",
    "Other": "Otros",
    "Nothing merged in this range.": "No hay cambios en este rango.",
    "Contributors": "Colaboradores",
    "Compare": "Comparar",
    "commits": "commits",
    "pull requests": "pull requests",
    "Labels of": "Etiquetas de",
    "No labels in the repository.": "El repositorio no tiene etiquetas.",
    "Name": "Nombre",
    "Colour": "Color",
    "Description": "Descripción",
    "Label sync plan": "Plan de sincronización de etiquetas",
    "Create": "Crear",
    "Update": "Actualizar",
    "Unchanged": "Sin cambios",
    "Only on GitHub (never deleted)": "Solo en GitHub (no se borran)",
    "Nothing to do: the repository already matches the file.": "Nada que hacer: el repositorio ya coincide con el archivo.",
    "Remote writes are not allowed for this repository (Settings > Git policy > push). Nothing was changed.": "Este repositorio no permite escrituras remotas (Ajustes > Política de Git > push). No se ha cambiado nada.",
    "Applying it needs your explicit approval.": "Aplicarlo necesita tu aprobación explícita.",
    "Applied": "Aplicado",
    "created": "creadas",
    "updated": "actualizadas",
    "failed": "con error",
    "Release check": "Comprobación de versión",
    "Ready to release": "Lista para publicar",
    "Not ready": "No está lista",
    "Working tree is clean": "El árbol de trabajo está limpio",
    "Branch is pushed": "La rama está subida",
    "Tag": "Etiqueta (tag)",
    "CI on HEAD": "CI del commit actual",
    "No open blockers": "Sin bloqueantes abiertos",
    "Version strings agree": "Las versiones coinciden",
    "GitHub": "GitHub",
    "used": "usado",
    "not used": "no usado",
    "gh was not needed (no pull request numbers in the history)": "no hizo falta gh (el historial no tiene números de pull request)",
    "skipped": "omitidos",
}


def tr(lang: str, text: str) -> str:
    if (lang or "").lower().startswith("es"):
        return _ES.get(text, text)
    return text


# ---------------------------------------------------------------------------
# The gh seam
# ---------------------------------------------------------------------------
def run_gh(args: Sequence[str], cwd: Optional[str] = None, *,
           timeout: float = GH_TIMEOUT) -> subprocess.CompletedProcess:
    """The one place that shells out to `gh`. A real argv, never a shell."""
    exe = shutil.which("gh")
    if not exe:
        raise GhNotAvailableError()
    argv = [exe, *args]
    try:
        return subprocess.run(
            argv, cwd=cwd or None, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=_gh_env(), stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise GhNotAvailableError()
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=argv, returncode=124, stdout="",
            stderr=f"gh {args[0] if args else ''} timed out after {timeout:.0f}s",
        )


def classify_gh_error(stderr: str, returncode: Optional[int] = None) -> str:
    s = (stderr or "").lower()
    if returncode == 124 or "timed out" in s:
        return "gh.timeout"
    if ("gh auth login" in s or "not logged in" in s or "authentication required" in s
            or "http 401" in s or "bad credentials" in s or "gh_token" in s):
        return "gh.unauthenticated"
    if ("none of the git remotes" in s or "no git remotes" in s or "not a github" in s
            or "unable to determine base repository" in s or "no default remote" in s):
        return "gh.no_remote"
    if "rate limit" in s:
        return "gh.rate_limited"
    if "http 404" in s or "could not resolve to a" in s or "not found" in s:
        return "gh.not_found"
    return "gh.failed"


_GH_HINT = {
    "gh.missing": "The GitHub CLI (gh) is not installed or not on PATH.",
    "gh.unauthenticated": "gh is not signed in to GitHub (run `gh auth login`).",
    "gh.no_remote": "This repository has no remote that gh recognises as GitHub.",
    "gh.timeout": "gh did not answer in time.",
    "gh.rate_limited": "GitHub rate limit reached; try again later.",
    "gh.not_found": "GitHub does not know this repository (or you cannot see it).",
}


_ES_ERRORS = {
    "gh.missing": "El CLI de GitHub (gh) no está instalado o no está en el PATH.",
    "gh.unauthenticated": "gh no ha iniciado sesión en GitHub (ejecuta `gh auth login`).",
    "gh.timeout": "gh no respondió a tiempo.",
    "gh.rate_limited": "Se alcanzó el límite de peticiones de GitHub; inténtalo más tarde.",
    "gh.not_found": "GitHub no conoce este repositorio (o no tienes acceso).",
    "gh.no_remote.none": "Este repositorio no tiene remoto 'origin': no hay nada en GitHub que consultar.",
    "gh.no_remote.local": "El remoto 'origin' es una ruta local (`{url}`), no un repositorio de GitHub.",
    "gh.no_remote.unparsable": "No se pudo leer owner/repo del remoto {url}.",
    "approval.invalid": "Esa aprobación no existe, ha caducado o ya se usó. Vuelve a ejecutar el comando.",
    "approval.stale": "Las etiquetas de GitHub o el archivo han cambiado desde que viste el plan. Vuelve a ejecutar el comando.",
    "git.policy_denied": "La política del repositorio ya no permite escrituras remotas (push).",
    "git.unknown_ref": "{ref} no es un commit ni una etiqueta de este repositorio.",
    "args.bad_ref": "No es una referencia válida: {ref}",
    "labels.no_file": "{path} no existe en el repositorio.",
    "labels.outside_repo": "{path} no está dentro del repositorio.",
}

_ES_FMT = {
    "{n} file(s) with uncommitted changes": "{n} archivo(s) con cambios sin confirmar",
    "HEAD is detached": "HEAD está desacoplado",
    "{b} has no upstream branch": "{b} no tiene rama remota asociada",
    "{b} is {n} commit(s) behind {u}": "{b} va {n} commit(s) por detrás de {u}",
    "{b} is {n} commit(s) ahead of {u} (unpushed)": "{b} va {n} commit(s) por delante de {u} (sin subir)",
    "{b} is in sync with {u}": "{b} está sincronizada con {u}",
    "free: the tag does not exist yet": "libre: la etiqueta aún no existe",
    " (last tag: {t})": " (última etiqueta: {t})",
    "already points at HEAD": "ya apunta a HEAD",
    "exists and points at {a}, not at HEAD ({h})": "existe y apunta a {a}, no a HEAD ({h})",
    "last tag {t}, {n} commit(s) since": "última etiqueta {t}, {n} commit(s) desde entonces",
    "no tags yet": "aún no hay etiquetas",
    "No workflow runs for this commit (is it pushed?)": "No hay ejecuciones de workflows para este commit (¿está subido?)",
    "Failing: {x}": "Fallan: {x}",
    "Still running: {x}": "En curso: {x}",
    "{n} workflow(s) passed": "{n} workflow(s) correctos",
    "labels checked: {x}": "etiquetas comprobadas: {x}",
    "no static version strings found": "no se han encontrado versiones estáticas",
    "mismatch: {x}": "no coinciden: {x}",
    "files say {v} but the tag is {t} ({l})": "los archivos dicen {v} pero la etiqueta es {t} ({l})",
    "no commits yet": "aún no hay commits",
    "the tag already exists on origin (at {a}) but not locally": "la etiqueta ya existe en origin (en {a}) pero no en local",
    "the tag exists on origin at {r} but locally at {l}": "la etiqueta existe en origin en {r} pero en local en {l}",
    "could not check the remote (`{x}`); the tag does not exist locally": "no se pudo comprobar el remoto (`{x}`); la etiqueta no existe en local",
    "already points at HEAD, but the remote could not be checked (`{x}`)": "ya apunta a HEAD, pero no se pudo comprobar el remoto (`{x}`)",
    "no 'origin' remote": "no hay remoto 'origin'",
    "timed out": "se agotó el tiempo",
    "No failures detected · partial check ({n} unverified)": "No se han detectado fallos · comprobación parcial ({n} sin verificar)",
}


def tf(lang: str, text: str, **kw: Any) -> str:
    """A detail line, formatted, in the UI language."""
    if (lang or "").lower().startswith("es"):
        text = _ES_FMT.get(text, text)
    return text.format(**kw)


def _err(error_class: str, detail: str = "", *, key: Optional[str] = None, **args: Any) -> Dict[str, Any]:
    base = _GH_HINT.get(error_class, "")
    msg = " ".join(x for x in (base, detail.strip()) if x) or error_class
    return {"ok": False, "error_class": error_class, "error": msg, "error_key": key or error_class, "error_args": args}


def loc_error(err: Dict[str, Any], lang: str) -> str:
    """The error's message in the UI language when a Spanish text exists for it,
    else the English one."""
    if (lang or "").lower().startswith("es"):
        tmpl = _ES_ERRORS.get(err.get("error_key") or err.get("error_class") or "")
        if tmpl:
            try:
                return tmpl.format(**(err.get("error_args") or {}))
            except (KeyError, IndexError):
                pass
    return err.get("error", "")


def localize(res: Dict[str, Any], lang: str) -> Dict[str, Any]:
    """Last step of every public entry point: failures get the UI-language
    message and a markdown body."""
    if res.get("ok") is False:
        res["error"] = loc_error(res, lang)
        if "markdown" not in res:
            res["markdown"] = f"**{res.get('error_class', 'error')}**: {res['error']}"
    return res


# ---------------------------------------------------------------------------
# Repository context
# ---------------------------------------------------------------------------
_LOCAL_REMOTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\./|\.\./|file://|\\\\)")


def repo_context(repo_root: str) -> Dict[str, Any]:
    """``{"root", "remote_url", "slug", "github", "reason"}``. ``github`` is
    True only when ``origin`` points somewhere `gh` could talk to; ``reason``
    then says why not (``no_remote`` / ``local_remote`` / ``unparsable``)."""
    ctx: Dict[str, Any] = {"root": repo_root, "remote_url": None, "slug": None, "github": False, "reason": None}
    proc = git_panel.run_git(repo_root, "remote", "get-url", "origin")
    url = (proc.stdout or "").strip() if proc.returncode == 0 else ""
    if not url:
        ctx["reason"] = "no_remote"
        return ctx
    ctx["remote_url"] = url
    if _LOCAL_REMOTE_RE.match(url):
        ctx["reason"] = "local_remote"
        return ctx
    parsed = _parse_remote_url(url)
    if not parsed:
        ctx["reason"] = "unparsable"
        return ctx
    ctx["slug"] = f"{parsed[0]}/{parsed[1]}"
    ctx["github"] = True
    return ctx


def _no_github(ctx: Dict[str, Any]) -> Dict[str, Any]:
    reason = ctx.get("reason")
    url = ctx.get("remote_url")
    if reason == "local_remote":
        return _err("gh.no_remote", f"'origin' is a local path (`{url}`), not a GitHub repository.", key="gh.no_remote.local", url=url)
    if reason == "unparsable":
        return _err("gh.no_remote", f"Could not read owner/repo from the remote {url!r}.", key="gh.no_remote.unparsable", url=repr(url))
    return _err("gh.no_remote", "There is no 'origin' remote.", key="gh.no_remote.none")


def _gh_json(ctx: Dict[str, Any], args: Sequence[str]) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """``(data, None)`` or ``(None, error_dict)``. Adds ``--repo`` from the
    origin remote so a second remote can never make `gh` ask a question."""
    if not ctx.get("github"):
        return None, _no_github(ctx)
    argv = [*args, "--repo", ctx["slug"]]
    try:
        proc = run_gh(argv, ctx["root"])
    except GhNotAvailableError:
        return None, _err("gh.missing")
    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout or "").strip()
        return None, _err(classify_gh_error(text, proc.returncode), text[:300])
    try:
        return json.loads(proc.stdout or "null"), None
    except ValueError as exc:
        return None, _err("gh.failed", f"gh returned invalid JSON: {exc}")


def _gh_exec(ctx: Dict[str, Any], args: Sequence[str]) -> Optional[Dict[str, Any]]:
    """Run a gh command whose output is not JSON; ``None`` on success."""
    if not ctx.get("github"):
        return _no_github(ctx)
    try:
        argv = list(args)
        at = argv.index("--") if "--" in argv else len(argv)
        argv[at:at] = ["--repo", ctx["slug"]]  # options first: after "--" everything is positional
        proc = run_gh(argv, ctx["root"], timeout=30.0)
    except GhNotAvailableError:
        return _err("gh.missing")
    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout or "").strip()
        return _err(classify_gh_error(text, proc.returncode), text[:300])
    return None


# ---------------------------------------------------------------------------
# /github command parsing
# ---------------------------------------------------------------------------
_REF_RE = re.compile(r"^[A-Za-z0-9._/@+\-]{1,120}$")
_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')

_ALIASES = {
    "label": "labels", "labels": "labels", "etiquetas": "labels",
    "notes": "notes", "note": "notes", "changelog": "notes", "release-notes": "notes", "notas": "notes",
    "release-check": "release-check", "releasecheck": "release-check", "release": "release-check",
    "check": "release-check", "ready": "release-check", "comprobar": "release-check",
}


def safe_ref(value: str) -> bool:
    """A ref or tag name that cannot be read as an option and has no shell
    or revision-syntax surprises."""
    v = (value or "").strip()
    return bool(v) and not v.startswith("-") and ".." not in v and bool(_REF_RE.match(v))


def parse_range(spec: str) -> Dict[str, Any]:
    """``a..b`` / ``a...b`` / ``a..`` / ``..b`` / ``a`` (= from a) -> from/to."""
    s = (spec or "").strip()
    if not s:
        return {"ok": True, "from": None, "to": None}
    if ".." in s:
        left, _, right = re.split(r"(\.\.\.?)", s, maxsplit=1)
        a, b = left.strip() or None, right.strip() or None
    else:
        a, b = s, None
    for ref in (a, b):
        if ref and not safe_ref(ref):
            return {"ok": False, "error": f"Not a valid ref or tag: {ref!r}"}
    return {"ok": True, "from": a, "to": b}


def parse_command(line: str) -> Dict[str, Any]:
    """``/github`` arguments -> ``{"ok": True, "action": ..., ...}`` or
    ``{"ok": False, "error", "usage"}``. Pure: nothing is touched."""
    tokens = [t.strip("\"'") for t in _TOKEN_RE.findall(line or "")]
    if not tokens or tokens[0].lower() in ("help", "?", "-h", "--help"):
        return {"ok": True, "action": "help", "usage": dict(USAGE)}
    head = tokens[0].lower()
    action = _ALIASES.get(head)
    rest = tokens[1:]
    if action is None:
        return {"ok": False, "error": f"Unknown /github subcommand: {tokens[0]!r}.", "usage": dict(USAGE)}

    if action == "labels":
        if not rest:
            return {"ok": True, "action": "labels"}
        sub = rest[0].lower()
        if sub in ("sync", "apply", "push"):
            if len(rest) < 2:
                return {"ok": False, "error": "labels sync needs a file.", "usage": {"labels_sync": USAGE["labels_sync"]}}
            if len(rest) > 2:
                return {"ok": False, "error": "labels sync takes exactly one file.", "usage": {"labels_sync": USAGE["labels_sync"]}}
            return {"ok": True, "action": "labels_sync", "file": rest[1]}
        if sub in ("list", "ls"):
            return {"ok": True, "action": "labels"}
        return {"ok": False, "error": f"Unknown labels subcommand: {rest[0]!r}.", "usage": {"labels": USAGE["labels"], "labels_sync": USAGE["labels_sync"]}}

    if action == "notes":
        if len(rest) > 1:
            return {"ok": False, "error": "notes takes one optional range.", "usage": {"notes": USAGE["notes"]}}
        rng = parse_range(rest[0] if rest else "")
        if not rng["ok"]:
            return {"ok": False, "error": rng["error"], "usage": {"notes": USAGE["notes"]}}
        return {"ok": True, "action": "notes", "from": rng["from"], "to": rng["to"]}

    # release-check
    if len(rest) > 1:
        return {"ok": False, "error": "release-check takes one optional tag.", "usage": {"release_check": USAGE["release_check"]}}
    tag = rest[0] if rest else None
    if tag and not safe_ref(tag):
        return {"ok": False, "error": f"Not a valid tag name: {tag!r}", "usage": {"release_check": USAGE["release_check"]}}
    return {"ok": True, "action": "release_check", "tag": tag}


def help_markdown(lang: str = "en") -> str:
    es = (lang or "").lower().startswith("es")
    rows = [
        (USAGE["labels"], "Lista las etiquetas del repositorio." if es else "Lists the repository's labels."),
        (USAGE["labels_sync"], "Crea o actualiza etiquetas desde un archivo del repo; escribir en GitHub pide política y aprobación." if es
         else "Creates or updates labels from a file in the repo; writing to GitHub needs the policy and your approval."),
        (USAGE["notes"], "Borrador de notas de versión desde los PR fusionados y commits." if es
         else "Drafts release notes from merged PRs and commits."),
        (USAGE["release_check"], "Comprueba si una versión está lista: árbol limpio, etiqueta, CI, bloqueantes, versiones." if es
         else "Checks a release is ready: clean tree, tag, CI, blockers, versions."),
    ]
    lines = ["| " + ("Comando" if es else "Command") + " | " + ("Qué hace" if es else "What it does") + " |", "| --- | --- |"]
    for usage, text in rows:
        lines.append(f"| `{usage}` | {text} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
_COLOR_RE = re.compile(r"^[0-9a-fA-F]{6}$")


def _norm_label(entry: Any, fallback_name: str = "") -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    if isinstance(entry, str):
        entry = {"name": entry}
    if not isinstance(entry, dict):
        return None, f"label entry must be an object, got {type(entry).__name__}"
    name = str(entry.get("name") or fallback_name or "").strip()
    if not name:
        return None, "a label has no name"
    if len(name) > 50:
        return None, f"label name {name!r} is longer than 50 characters"
    if name.startswith("-"):
        return None, f"label name {name!r} starts with '-': it would be read as an option by gh"
    color = str(entry.get("color") or "").strip().lstrip("#")
    if color and not _COLOR_RE.match(color):
        return None, f"label {name!r}: colour must be 6 hex digits, got {entry.get('color')!r}"
    desc = str(entry.get("description") or "").strip()
    if len(desc) > 100:
        return None, f"label {name!r}: description is longer than 100 characters"
    return {"name": name, "color": color.lower(), "description": desc}, None


def parse_label_text(text: str, filename: str = "") -> Dict[str, Any]:
    """YAML or JSON text -> ``{"ok", "labels": [{name,color,description}]}``.
    Accepted shapes: a list of labels (strings or objects), ``{"labels": [...]}``
    or a mapping ``name -> {color, description}``."""
    raw = (text or "").strip()
    if not raw:
        return _err("labels.empty_file", "The label file is empty.")
    data: Any = None
    is_json = filename.lower().endswith(".json") or raw[:1] in "[{" and not filename.lower().endswith((".yml", ".yaml"))
    if is_json:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            if filename.lower().endswith(".json"):
                return _err("labels.bad_file", f"Invalid JSON: {exc}")
            is_json = False
    if not is_json:
        try:
            import yaml  # PyYAML ships with the app's environment
        except ImportError:
            return _err("labels.bad_file", "YAML support (PyYAML) is not installed; use a .json file.")
        try:
            data = yaml.safe_load(raw)
        except Exception as exc:  # noqa: BLE001 - any parser error is a bad file
            return _err("labels.bad_file", f"Invalid YAML: {str(exc)[:200]}")
    if isinstance(data, dict) and isinstance(data.get("labels"), (list, dict)):
        data = data["labels"]
    entries: List[Tuple[Any, str]] = []
    if isinstance(data, list):
        entries = [(e, "") for e in data]
    elif isinstance(data, dict):
        for name, value in data.items():
            if isinstance(value, dict):
                entries.append((value, str(name)))
            elif isinstance(value, str):
                entries.append(({"color": value}, str(name)))
            else:
                entries.append(({}, str(name)))
    else:
        return _err("labels.bad_file", "Expected a list of labels or a {labels: [...]} object.")
    labels: List[Dict[str, str]] = []
    seen: Dict[str, str] = {}
    for entry, fallback in entries:
        label, problem = _norm_label(entry, fallback)
        if problem:
            return _err("labels.bad_file", problem)
        key = label["name"].lower()
        if key in seen:
            return _err("labels.bad_file", f"label {label['name']!r} appears twice (GitHub label names are case-insensitive).")
        seen[key] = label["name"]
        labels.append(label)
    if not labels:
        return _err("labels.empty_file", "The label file defines no labels.")
    return {"ok": True, "labels": labels}


def load_label_file(repo_root: str, rel: str) -> Dict[str, Any]:
    safe = git_panel.safe_rel_path(repo_root, rel)
    if not safe:
        return _err("labels.outside_repo", f"{rel!r} is not inside the repository.", path=rel)
    full = os.path.join(repo_root, safe)
    if not os.path.isfile(full):
        return _err("labels.no_file", f"{safe} does not exist in the repository.", path=safe)
    try:
        if os.path.getsize(full) > 256 * 1024:
            return _err("labels.bad_file", "The label file is larger than 256 KB.")
        with open(full, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        return _err("labels.bad_file", f"Could not read {safe}: {exc}")
    parsed = parse_label_text(text, safe)
    if parsed.get("ok"):
        parsed["path"] = safe
    return parsed


def list_labels(repo_root: str, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ctx = ctx or repo_context(repo_root)
    data, err = _gh_json(ctx, ["label", "list", "--json", "name,color,description", "--limit", "1000"])
    if err:
        return err
    labels: List[Dict[str, str]] = []
    for item in data or []:
        if isinstance(item, dict) and item.get("name"):
            labels.append({"name": str(item["name"]), "color": str(item.get("color") or "").lower(),
                           "description": str(item.get("description") or "")})
    labels.sort(key=lambda x: x["name"].lower())
    return {"ok": True, "labels": labels, "slug": ctx.get("slug")}


def plan_label_sync(existing: List[Dict[str, str]], desired: List[Dict[str, str]]) -> Dict[str, Any]:
    """Pure: what a sync would do. A desired label without a colour keeps the
    remote colour on update (and gets a neutral grey on create); a label
    without a description keeps the remote description -- this tool never
    blanks a field and never deletes a label."""
    have = {e["name"].lower(): e for e in existing}
    create: List[Dict[str, str]] = []
    update: List[Dict[str, Any]] = []
    unchanged: List[str] = []
    wanted = set()
    for d in desired:
        key = d["name"].lower()
        wanted.add(key)
        cur = have.get(key)
        if cur is None:
            create.append({"name": d["name"], "color": d.get("color") or "ededed", "description": d.get("description", "")})
            continue
        changes: Dict[str, Any] = {}
        if d.get("color") and d["color"].lower() != (cur.get("color") or "").lower():
            changes["color"] = {"from": cur.get("color", ""), "to": d["color"].lower()}
        if d.get("description") and d["description"] != (cur.get("description") or ""):
            changes["description"] = {"from": cur.get("description", ""), "to": d["description"]}
        if d["name"] != cur["name"]:
            changes["name"] = {"from": cur["name"], "to": d["name"]}
        if changes:
            update.append({"name": cur["name"], "changes": changes,
                           "color": d.get("color") or cur.get("color", ""),
                           "description": d.get("description") or cur.get("description", ""),
                           "new_name": d["name"] if d["name"] != cur["name"] else ""})
        else:
            unchanged.append(cur["name"])
    extra = sorted((e["name"] for e in existing if e["name"].lower() not in wanted), key=str.lower)
    return {"create": create, "update": update, "unchanged": unchanged, "only_remote": extra,
            "changes": len(create) + len(update)}


def plan_digest(slug: str, plan: Dict[str, Any]) -> str:
    blob = json.dumps({"slug": slug, "create": plan["create"],
                       "update": [{k: u[k] for k in ("name", "color", "description", "new_name")} for u in plan["update"]]},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def remote_writes_allowed(owner: Optional[str], repo_root: str) -> bool:
    """The repository policy's ``push`` field -- the one switch that stands
    for "the agent may write to the remote" everywhere in the git tools."""
    from src import agent_git_policy
    repo_id = git_panel.compute_repo_id(repo_root)
    return bool(agent_git_policy.effective_policy(owner, repo_id)["effective"].get("push"))


def sync_plan_markdown(plan: Dict[str, Any], slug: str, lang: str = "en") -> str:
    T = lambda s: tr(lang, s)  # noqa: E731
    lines = [f"### {T('Label sync plan')} — `{slug}`", ""]

    def swatch(c: str) -> str:
        return f"`#{c}`" if c else ""

    if plan["create"]:
        lines += [f"**{T('Create')} ({len(plan['create'])})**", "",
                  f"| {T('Name')} | {T('Colour')} | {T('Description')} |", "| --- | --- | --- |"]
        lines += [f"| `{c['name']}` | {swatch(c['color'])} | {c['description']} |" for c in plan["create"]]
        lines.append("")
    if plan["update"]:
        lines += [f"**{T('Update')} ({len(plan['update'])})**", "",
                  f"| {T('Name')} | " + ("Cambios" if lang.startswith("es") else "Changes") + " |", "| --- | --- |"]
        for u in plan["update"]:
            parts = []
            for field, ch in u["changes"].items():
                parts.append(f"{field}: `{ch['from']}` → `{ch['to']}`")
            lines.append(f"| `{u['name']}` | {'; '.join(parts)} |")
        lines.append("")
    if plan["unchanged"]:
        lines += [f"**{T('Unchanged')}**: {len(plan['unchanged'])}", ""]
    if plan["only_remote"]:
        names = ", ".join(f"`{n}`" for n in plan["only_remote"][:30])
        more = f" (+{len(plan['only_remote']) - 30})" if len(plan["only_remote"]) > 30 else ""
        lines += [f"**{T('Only on GitHub (never deleted)')}**: {names}{more}", ""]
    if not plan["changes"]:
        lines.append(T("Nothing to do: the repository already matches the file."))
    return "\n".join(lines).rstrip() + "\n"


_PENDING: Dict[str, Dict[str, Any]] = {}
_PENDING_LOCK = threading.Lock()


def _purge_pending(now: float) -> None:
    for key in [k for k, v in _PENDING.items() if v["expires"] < now]:
        _PENDING.pop(key, None)


def _label_sync_state(repo_root: str, file: str) -> Dict[str, Any]:
    """Shared by prepare and apply: file + remote -> plan (or an error)."""
    ctx = repo_context(repo_root)
    loaded = load_label_file(repo_root, file)
    if not loaded.get("ok"):
        return loaded
    remote = list_labels(repo_root, ctx)
    if not remote.get("ok"):
        return remote
    plan = plan_label_sync(remote["labels"], loaded["labels"])
    return {"ok": True, "ctx": ctx, "plan": plan, "path": loaded["path"], "slug": ctx["slug"]}


def prepare_label_sync(owner: Optional[str], repo_root: str, file: str, lang: str = "en") -> Dict[str, Any]:
    """Read-only. Returns the plan and, when applying is both allowed by the
    policy and worth doing, a single-use ``approval_id``."""
    state = _label_sync_state(repo_root, file)
    if not state.get("ok"):
        return state
    plan, slug = state["plan"], state["slug"]
    allowed = remote_writes_allowed(owner, repo_root)
    md = sync_plan_markdown(plan, slug, lang)
    out: Dict[str, Any] = {
        "ok": True, "kind": "labels_sync", "applied": False, "slug": slug, "file": state["path"],
        "plan": plan, "policy_allowed": allowed, "approval_id": None,
        "requires_approval": bool(plan["changes"]), "markdown": md,
    }
    if not plan["changes"]:
        out["requires_approval"] = False
        return out
    if not allowed:
        out["policy"] = "git_agent_policy"
        out["git_policy_field"] = "push"
        out["markdown"] = md + "\n> " + tr(lang, "Remote writes are not allowed for this repository (Settings > Git policy > push). Nothing was changed.") + "\n"
        return out
    approval_id = secrets.token_urlsafe(18)
    now = time.time()
    with _PENDING_LOCK:
        _purge_pending(now)
        _PENDING[approval_id] = {"owner": owner or "", "root": os.path.realpath(repo_root), "file": state["path"],
                                 "digest": plan_digest(slug, plan), "expires": now + APPROVAL_TTL, "lang": lang}
    out["approval_id"] = approval_id
    out["approval_expires_in"] = int(APPROVAL_TTL)
    out["markdown"] = md + "\n> " + tr(lang, "Applying it needs your explicit approval.") + "\n"
    return out


def execute_label_plan(ctx: Dict[str, Any], plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The remote write: ``gh label create|edit`` per item. Callers have
    already checked policy and approval."""
    results: List[Dict[str, Any]] = []
    for c in plan["create"]:
        args = ["label", "create", "--color", c["color"] or "ededed"]
        if c.get("description"):
            args += ["--description", c["description"]]
        args += ["--", c["name"]]
        err = _gh_exec(ctx, args)
        results.append({"op": "create", "name": c["name"], "ok": err is None, **({"error": err["error"]} if err else {})})
    for u in plan["update"]:
        args = ["label", "edit"]
        if u.get("color"):
            args += ["--color", u["color"]]
        if u.get("description"):
            args += ["--description", u["description"]]
        if u.get("new_name"):
            args += ["--name", u["new_name"]]
        args += ["--", u["name"]]
        err = _gh_exec(ctx, args)
        results.append({"op": "update", "name": u["name"], "ok": err is None, **({"error": err["error"]} if err else {})})
    return results


def _apply_markdown(results: List[Dict[str, Any]], slug: str, lang: str) -> str:
    T = lambda s: tr(lang, s)  # noqa: E731
    made = sum(1 for r in results if r["ok"] and r["op"] == "create")
    upd = sum(1 for r in results if r["ok"] and r["op"] == "update")
    bad = [r for r in results if not r["ok"]]
    lines = [f"### {T('Applied')} — `{slug}`", "",
             f"- {made} {T('created')}", f"- {upd} {T('updated')}"]
    if bad:
        lines.append(f"- {len(bad)} {T('failed')}")
        lines += [f"  - `{r['name']}`: {r.get('error', '')}" for r in bad]
    return "\n".join(lines) + "\n"


def apply_label_sync(owner: Optional[str], approval_id: str, lang: str = "") -> Dict[str, Any]:
    """Consumes the approval. Re-checks the policy and that the plan is still
    the one that was approved before touching GitHub."""
    now = time.time()
    with _PENDING_LOCK:
        _purge_pending(now)
        pending = _PENDING.pop(str(approval_id or ""), None)
    if not pending or pending["owner"] != (owner or ""):
        return localize(_err("approval.invalid", "That approval does not exist, has expired or was already used. Run the command again."), lang or "en")
    lang = lang or pending.get("lang", "en")
    root = pending["root"]
    if not remote_writes_allowed(owner, root):
        return localize({**_err("git.policy_denied", "The repository policy no longer allows remote writes (push)."),
                         "policy": "git_agent_policy", "git_policy_field": "push"}, lang)
    state = _label_sync_state(root, pending["file"])
    if not state.get("ok"):
        return localize(state, lang)
    if plan_digest(state["slug"], state["plan"]) != pending["digest"]:
        return localize(_err("approval.stale", "The labels on GitHub or the file changed since you saw the plan. Run the command again."), lang)
    results = execute_label_plan(state["ctx"], state["plan"])
    return {"ok": all(r["ok"] for r in results), "kind": "labels_sync", "applied": True, "slug": state["slug"],
            "results": results, "markdown": _apply_markdown(results, state["slug"], lang)}


def labels_markdown(labels: List[Dict[str, str]], slug: str, lang: str = "en") -> str:
    T = lambda s: tr(lang, s)  # noqa: E731
    if not labels:
        return f"### {T('Labels of')} `{slug}`\n\n{T('No labels in the repository.')}\n"
    lines = [f"### {T('Labels of')} `{slug}` ({len(labels)})", "",
             f"| {T('Name')} | {T('Colour')} | {T('Description')} |", "| --- | --- | --- |"]
    for l in labels:
        lines.append(f"| `{l['name']}` | {('`#' + l['color'] + '`') if l['color'] else ''} | {l['description']} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Release notes
# ---------------------------------------------------------------------------
_SEP = "\x1f"
_REC = "\x1e"
_PR_SQUASH_RE = re.compile(r"\(#(\d+)\)\s*$")
_PR_MERGE_RE = re.compile(r"^Merge pull request #(\d+)\b")
_CONV_RE = re.compile(
    r"^(?P<type>feat|feature|fix|bugfix|docs?|perf|refactor|chore|test|tests|ci|build|style|revert|security|deps)"
    r"(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<rest>.+)$", re.IGNORECASE)

GROUP_ORDER = ("breaking", "security", "features", "fixes", "perf", "docs", "maintenance", "other")
GROUP_TITLES = {"breaking": "Breaking changes", "security": "Security", "features": "Features", "fixes": "Fixes",
                "perf": "Performance", "docs": "Documentation", "maintenance": "Maintenance", "other": "Other"}
_LABEL_GROUPS: Dict[str, Tuple[str, ...]] = {
    "breaking": ("breaking", "breaking-change"),
    "security": ("security", "vulnerability", "cve"),
    "features": ("feature", "feat", "enhancement"),
    "fixes": ("bug", "fix", "bugfix", "regression", "defect"),
    "perf": ("performance", "perf"),
    "docs": ("documentation", "docs", "doc"),
    "maintenance": ("chore", "dependencies", "deps", "dependency", "maintenance", "refactor", "ci", "build", "test", "tests", "tech-debt"),
}
_CONV_GROUPS = {"feat": "features", "feature": "features", "fix": "fixes", "bugfix": "fixes", "doc": "docs", "docs": "docs",
                "perf": "perf", "refactor": "maintenance", "chore": "maintenance", "test": "maintenance", "tests": "maintenance",
                "ci": "maintenance", "build": "maintenance", "style": "maintenance", "revert": "fixes",
                "security": "security", "deps": "maintenance"}


# A breaking change is declared in the commit FOOTER (git trailer form, the
# conventional-commits spec: ``BREAKING CHANGE:`` or its synonym
# ``BREAKING-CHANGE:``, upper case) or by a ``!`` before the colon of the
# subject (``feat!:`` / ``feat(api)!:``).
_BREAKING_RE = re.compile(r"^BREAKING[ -]CHANGE\s*:\s*(.*)$")
_TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*\s*:\s")


def breaking_note(body: str) -> Optional[str]:
    """The explanation of a ``BREAKING CHANGE:`` / ``BREAKING-CHANGE:`` footer
    in a commit body (continuation lines joined), ``""`` when the footer has no
    text, ``None`` when the commit declares none."""
    lines = (body or "").splitlines()
    for i, line in enumerate(lines):
        m = _BREAKING_RE.match(line.strip())
        if not m:
            continue
        parts = [m.group(1).strip()]
        for nxt in lines[i + 1:]:
            if not nxt.strip() or _TRAILER_RE.match(nxt.strip()):
                break
            parts.append(nxt.strip())
        return " ".join(p for p in parts if p)
    return None


def has_bang(title: str) -> bool:
    m = _CONV_RE.match((title or "").strip())
    return bool(m and m.group("bang"))


def classify_entry(title: str, labels: Sequence[str]) -> Tuple[str, str]:
    """``(group, clean_title)``. Labels beat the conventional-commit prefix,
    which beats "other"; a ``!`` after the prefix means breaking."""
    clean = title.strip()
    m = _CONV_RE.match(clean)
    conv_group = None
    if m:
        conv_group = "breaking" if m.group("bang") else _CONV_GROUPS.get(m.group("type").lower())
        clean = m.group("rest").strip()
        clean = clean[:1].upper() + clean[1:] if clean else clean
    tokens = set()
    for label in labels:
        low = str(label).lower().strip()
        tokens.add(low)
        tokens.update(t for t in re.split(r"[^a-z0-9]+", low) if t)
    for group in GROUP_ORDER:
        if group in _LABEL_GROUPS and tokens & set(_LABEL_GROUPS[group]):
            return group, clean
    return (conv_group or "other"), clean


def _rev_ok(repo_root: str, ref: str) -> bool:
    return git_panel.run_git(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def _rev(repo_root: str, ref: str) -> str:
    p = git_panel.run_git(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def last_tag(repo_root: str, to: str = "HEAD") -> Optional[str]:
    """Newest tag reachable from ``to``; if ``to`` itself is tagged with it,
    the one before (so notes for a tagged release cover that release)."""
    p = git_panel.run_git(repo_root, "describe", "--tags", "--abbrev=0", to)
    if p.returncode != 0:
        return None
    tag = (p.stdout or "").strip()
    if tag and _rev(repo_root, tag) == _rev(repo_root, to):
        p2 = git_panel.run_git(repo_root, "describe", "--tags", "--abbrev=0", f"{to}^")
        return (p2.stdout or "").strip() or None if p2.returncode == 0 else None
    return tag or None


def collect_commits(repo_root: str, from_ref: Optional[str], to: str) -> List[Dict[str, Any]]:
    rng = f"{from_ref}..{to}" if from_ref else to
    fmt = f"--format=%H{_SEP}%an{_SEP}%s{_SEP}%P{_SEP}%b{_REC}"
    p = git_panel.run_git(repo_root, "log", "--first-parent", fmt, rng, "--")
    if p.returncode != 0:
        return []
    out: List[Dict[str, Any]] = []
    for rec in (p.stdout or "").split(_REC):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        parts = rec.split(_SEP)
        if len(parts) < 5:
            continue
        sha, author, subject, parents, body = parts[0].strip(), parts[1], parts[2], parts[3], _SEP.join(parts[4:])
        out.append({"sha": sha, "short": sha[:7], "author": author, "subject": subject.strip(),
                    "merge": len(parents.split()) > 1, "body": body.strip()})
    return out


def _pr_number(commit: Dict[str, Any]) -> Optional[int]:
    m = _PR_MERGE_RE.match(commit["subject"]) or _PR_SQUASH_RE.search(commit["subject"])
    return int(m.group(1)) if m else None


def fetch_merged_prs(ctx: Dict[str, Any], numbers: Sequence[int]) -> Tuple[Dict[int, Dict[str, Any]], Optional[Dict[str, Any]]]:
    data, err = _gh_json(ctx, ["pr", "list", "--state", "merged", "--limit", "300",
                               "--json", "number,title,labels,author,url,mergedAt"])
    if err:
        return {}, err
    prs: Dict[int, Dict[str, Any]] = {}
    for item in data or []:
        if isinstance(item, dict) and item.get("number") in set(numbers):
            prs[int(item["number"])] = {
                "title": str(item.get("title") or ""), "url": str(item.get("url") or ""),
                "labels": [str(l.get("name")) for l in (item.get("labels") or []) if isinstance(l, dict) and l.get("name")],
                "author": str((item.get("author") or {}).get("login") or ""),
            }
    return prs, None


def build_notes(commits: List[Dict[str, Any]], prs: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for c in commits:
        number = _pr_number(c)
        pr = prs.get(number) if number else None
        if pr:
            title, labels, author, url = pr["title"], pr["labels"], pr["author"] or c["author"], pr["url"]
        else:
            title = c["subject"]
            if _PR_MERGE_RE.match(title):  # merge commit: the PR title is the first body line
                title = (c["body"].splitlines() or [title])[0].strip() or title
            title = _PR_SQUASH_RE.sub("", title).strip()
            labels, author, url = [], c["author"], ""
        group, clean = classify_entry(title, labels)
        note = breaking_note(c.get("body") or "")
        # A breaking change goes ONLY under "Breaking changes" (not repeated
        # under its type group), with the footer's explanation under it.
        breaking = group == "breaking" or has_bang(title) or note is not None
        type_group = group
        if breaking:
            group = "breaking"
        entries.append({"sha": c["short"], "title": clean, "pr": number, "labels": labels, "author": author,
                        "url": url, "group": group, "type_group": type_group, "breaking": breaking,
                        "breaking_note": note or "", "from_gh": bool(pr)})
    return entries


def notes_markdown(entries: List[Dict[str, Any]], from_ref: Optional[str], to: str, slug: Optional[str],
                   lang: str, gh_line: str) -> str:
    T = lambda s: tr(lang, s)  # noqa: E731
    title = f"## {T('Release notes')} — {to}"
    if from_ref:
        sub = f"{T('Changes since')} `{from_ref}`"
    else:
        sub = T("No tags yet")
    pr_count = sum(1 for e in entries if e["pr"])
    lines = [title, "", f"{sub} · {len(entries)} {T('commits')} · {pr_count} {T('pull requests')}", ""]
    if not entries:
        lines.append(T("Nothing merged in this range."))
    for group in GROUP_ORDER:
        items = [e for e in entries if e["group"] == group]
        if not items:
            continue
        lines += [f"### {T(GROUP_TITLES[group])}", ""]
        for e in items:
            ref = f"#{e['pr']}" if e["pr"] else e["sha"]
            who = f", @{e['author']}" if e["from_gh"] and e["author"] else (f", {e['author']}" if e["author"] else "")
            lines.append(f"- {e['title']} ({ref}{who})")
            if e.get("breaking_note"):
                lines.append(f"  - {e['breaking_note']}")
        lines.append("")
    people = sorted({e["author"] for e in entries if e["author"]}, key=str.lower)
    if people:
        lines += [f"**{T('Contributors')}**: " + ", ".join(people), ""]
    if slug and from_ref:
        lines += [f"{T('Compare')}: https://github.com/{slug}/compare/{from_ref}...{to}", ""]
    lines += [f"{T('GitHub')}: {gh_line}", ""]
    return "\n".join(lines)


def release_notes(repo_root: str, from_ref: Optional[str] = None, to: Optional[str] = None, lang: str = "en") -> Dict[str, Any]:
    """Read-only. Works from local git; adds PR titles/labels/authors when
    `gh` can supply them and says so when it cannot."""
    to = (to or "HEAD").strip()
    if not safe_ref(to) and to != "HEAD":
        return localize(_err("args.bad_ref", f"Not a valid ref: {to!r}", ref=repr(to)), lang)
    if from_ref and not safe_ref(from_ref):
        return localize(_err("args.bad_ref", f"Not a valid ref: {from_ref!r}", ref=repr(from_ref)), lang)
    if not _rev_ok(repo_root, to):
        return localize(_err("git.unknown_ref", f"{to!r} is not a commit in this repository.", ref=repr(to)), lang)
    if from_ref and not _rev_ok(repo_root, from_ref):
        return localize(_err("git.unknown_ref", f"{from_ref!r} is not a commit or tag in this repository.", ref=repr(from_ref)), lang)
    if not from_ref:
        from_ref = last_tag(repo_root, to)
    commits = collect_commits(repo_root, from_ref, to)
    ctx = repo_context(repo_root)
    numbers = [n for n in (_pr_number(c) for c in commits) if n]
    prs: Dict[int, Dict[str, Any]] = {}
    gh_note: Dict[str, Any] = {"used": False, "reason": None}
    if not numbers:
        gh_note["reason"] = "no_pr_numbers"
    else:
        prs, err = fetch_merged_prs(ctx, numbers)
        if err:
            gh_note["reason"] = err["error_class"]
            gh_note["detail"] = loc_error(err, lang)
        else:
            gh_note["used"] = True
    entries = build_notes(commits, prs)
    if gh_note["used"]:
        gh_line = f"{tr(lang, 'used')} ({len(prs)}/{len(numbers)} PR)"
    elif gh_note["reason"] == "no_pr_numbers":
        gh_line = tr(lang, "gh was not needed (no pull request numbers in the history)")
    else:
        gh_line = f"{tr(lang, 'not used')} — {gh_note.get('detail') or gh_note['reason']}"
    md = notes_markdown(entries, from_ref, to, ctx.get("slug") if ctx.get("github") else None, lang, gh_line)
    return {"ok": True, "kind": "notes", "from": from_ref, "to": to, "commits": len(commits),
            "pull_requests": len(numbers), "entries": entries, "gh": gh_note, "markdown": md,
            "groups": {g: [e for e in entries if e["group"] == g] for g in GROUP_ORDER if any(e["group"] == g for e in entries)}}


# ---------------------------------------------------------------------------
# Release check
# ---------------------------------------------------------------------------
def _item(item_id: str, label: str, status: str, detail: str = "") -> Dict[str, str]:
    return {"id": item_id, "label": label, "status": status, "detail": detail}


def _norm_version(v: str) -> str:
    return (v or "").strip().lstrip("vV")


def _toml_table_version(text: str, tables: Sequence[str]) -> Optional[str]:
    current = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            current = s.strip("[]").strip()
            continue
        if current in tables:
            m = re.match(r"""^version\s*=\s*["']([^"']+)["']""", s)
            if m:
                return m.group(1)
    return None


def find_versions(repo_root: str) -> List[Dict[str, str]]:
    """Version strings declared in the usual places (static ones only)."""
    found: List[Dict[str, str]] = []

    def read(rel: str) -> Optional[str]:
        path = os.path.join(repo_root, rel)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                return fh.read(200_000)
        except OSError:
            return None

    for rel in ("package.json", "studio/package.json", "frontend/package.json", "web/package.json", "ui/package.json", "client/package.json"):
        text = read(rel)
        if text:
            try:
                v = json.loads(text).get("version")
            except (ValueError, AttributeError):
                v = None
            if isinstance(v, str) and v.strip():
                found.append({"file": rel, "version": v.strip()})
    for rel, tables in (("pyproject.toml", ("project", "tool.poetry")), ("Cargo.toml", ("package",)),
                        ("studio/Cargo.toml", ("package",)), ("src-tauri/Cargo.toml", ("package",))):
        text = read(rel)
        if text:
            v = _toml_table_version(text, tables)
            if v:
                found.append({"file": rel, "version": v})
    for rel in ("VERSION", "version.txt"):
        text = read(rel)
        if text and text.strip():
            first = text.strip().splitlines()[0].strip()
            if re.match(r"^[vV]?\d+(\.\d+){1,3}([-+.\w]*)$", first):
                found.append({"file": rel, "version": first})
    for rel in ("__init__.py", "src/__init__.py", "version.py", "src/version.py", "_version.py", "src/_version.py", "app/version.py"):
        text = read(rel)
        if text:
            m = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", text, re.MULTILINE)
            if m:
                found.append({"file": rel, "version": m.group(1)})
    return found


REMOTE_TAG_TIMEOUT = 20.0


def _ls_remote_tag(repo_root: str, tag: str, timeout: float = REMOTE_TAG_TIMEOUT) -> Dict[str, Any]:
    """Does ``origin`` have this tag? Read-only (``git ls-remote``), works for a
    local bare remote and for GitHub alike, never prompts, bounded by a timeout.

    ``{"state": "found", "sha": <commit the tag points at>}``, ``{"state":
    "absent"}`` or ``{"state": "unknown", "reason": ...}`` (no origin,
    offline, timeout, no access) -- the caller must never read "unknown" as
    "free"."""
    try:
        url = git_panel.run_git(repo_root, "remote", "get-url", "origin")
        if url.returncode != 0 or not (url.stdout or "").strip():
            return {"state": "unknown", "reason": "no 'origin' remote"}
        p = git_panel.run_git(repo_root, "ls-remote", "--tags", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}",
                              timeout=timeout)
    except git_panel.GitNotFoundError:
        return {"state": "unknown", "reason": "git is not installed"}
    if p.returncode == 124:
        return {"state": "unknown", "reason": "timed out"}
    if p.returncode != 0:
        lines = [ln.strip() for ln in (p.stderr or "").splitlines() if ln.strip()]
        why = re.sub(r"^(fatal|error):\s*", "", lines[0])[:140] if lines else "git ls-remote failed"
        return {"state": "unknown", "reason": why}
    sha = peeled = ""
    for line in (p.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[1] == f"refs/tags/{tag}^{{}}":
            peeled = parts[0]
        elif parts[1] == f"refs/tags/{tag}":
            sha = parts[0]
    if not (sha or peeled):
        return {"state": "absent"}
    return {"state": "found", "sha": peeled or sha}


def _reason(lang: str, remote: Dict[str, Any]) -> str:
    """The "why unknown" text; it may come from git's stderr, so it is never
    used as a format template."""
    text = str(remote.get("reason") or "unknown")
    return _ES_FMT.get(text, text) if (lang or "").lower().startswith("es") else text


def _tag_item(repo_root: str, tag: str, head: str, latest: Optional[str], lang: str) -> Dict[str, str]:
    label = f"Tag {tag}"
    existing = _rev(repo_root, f"refs/tags/{tag}")
    remote = _ls_remote_tag(repo_root, tag)
    state, rsha = remote["state"], remote.get("sha") or ""
    if state == "found" and not existing:
        return _item("tag", label, "fail", tf(lang, "the tag already exists on origin (at {a}) but not locally", a=rsha[:7]))
    if state == "found" and rsha and rsha != existing:
        return _item("tag", label, "fail", tf(lang, "the tag exists on origin at {r} but locally at {l}", r=rsha[:7], l=existing[:7]))
    if not existing:
        if state == "unknown":
            why = _reason(lang, remote)
            return _item("tag", label, "skip", tf(lang, "could not check the remote (`{x}`); the tag does not exist locally", x=why))
        return _item("tag", label, "ok", tf(lang, "free: the tag does not exist yet") + (tf(lang, " (last tag: {t})", t=latest) if latest else ""))
    if existing != head:
        return _item("tag", label, "fail", tf(lang, "exists and points at {a}, not at HEAD ({h})", a=existing[:7], h=head[:7]))
    if state == "unknown":
        return _item("tag", label, "warn", tf(lang, "already points at HEAD, but the remote could not be checked (`{x}`)", x=_reason(lang, remote)))
    return _item("tag", label, "ok", tf(lang, "already points at HEAD"))


def _ci_item(ctx: Dict[str, Any], sha: str, lang: str = "en") -> Dict[str, str]:
    label = "CI on HEAD"
    data, err = _gh_json(ctx, ["run", "list", "--commit", sha, "--limit", "30",
                               "--json", "name,workflowName,status,conclusion,url"])
    if err:
        return _item("ci", label, "skip", loc_error(err, lang))
    runs = [r for r in (data or []) if isinstance(r, dict)]
    if not runs:
        return _item("ci", label, "warn", tf(lang, "No workflow runs for this commit (is it pushed?)"))
    latest: Dict[str, Dict[str, Any]] = {}
    for r in runs:  # newest first: keep one run per workflow
        key = str(r.get("workflowName") or r.get("name") or "?")
        latest.setdefault(key, r)
    bad, pending, good = [], [], []
    for name, r in latest.items():
        status, conclusion = str(r.get("status") or ""), str(r.get("conclusion") or "")
        if status != "completed":
            pending.append(name)
        elif conclusion in ("success", "skipped", "neutral"):
            good.append(name)
        else:
            bad.append(f"{name} ({conclusion or 'unknown'})")
    if bad:
        return _item("ci", label, "fail", tf(lang, "Failing: {x}", x=", ".join(bad)))
    if pending:
        return _item("ci", label, "warn", tf(lang, "Still running: {x}", x=", ".join(pending)))
    return _item("ci", label, "ok", tf(lang, "{n} workflow(s) passed", n=len(good)))


def _blockers_item(ctx: Dict[str, Any], labels: Sequence[str], lang: str = "en") -> Dict[str, str]:
    label = "No open blockers"
    found: List[str] = []
    for name in labels:
        for kind, noun in (("issue", "issue"), ("pr", "PR")):
            data, err = _gh_json(ctx, [kind, "list", "--label", name, "--state", "open", "--limit", "50",
                                       "--json", "number,title,url"])
            if err:
                return _item("blockers", label, "skip", loc_error(err, lang))
            for it in data or []:
                if isinstance(it, dict):
                    found.append(f"{noun} #{it.get('number')} {str(it.get('title') or '')[:70]}")
    if found:
        return _item("blockers", label, "fail", "; ".join(found[:10]) + (f" (+{len(found) - 10})" if len(found) > 10 else ""))
    return _item("blockers", label, "ok", tf(lang, "labels checked: {x}", x=", ".join(labels)))


def release_check(repo_root: str, tag: Optional[str] = None, blocker_labels: Optional[Sequence[str]] = None,
                  lang: str = "en") -> Dict[str, Any]:
    """Read-only checklist. ``ready`` is true when nothing FAILS; warnings and
    skipped GitHub items do not block it (but are listed)."""
    if tag and not safe_ref(tag):
        return localize(_err("args.bad_ref", f"Not a valid tag name: {tag!r}", ref=repr(tag)), lang)
    blocker_labels = [str(b).strip() for b in (blocker_labels or DEFAULT_BLOCKER_LABELS)
                      if str(b).strip() and not str(b).strip().startswith("-") and len(str(b).strip()) <= 50
                      and not re.search(r"[\x00-\x1f]", str(b))] or list(DEFAULT_BLOCKER_LABELS)
    ctx = repo_context(repo_root)
    head = _rev(repo_root, "HEAD")
    items: List[Dict[str, str]] = []

    status = git_panel.repo_status(repo_root)
    dirty = len(status.get("staged", [])) + len(status.get("unstaged", [])) + len(status.get("untracked", [])) + len(status.get("conflicts", []))
    items.append(_item("clean", "Working tree is clean", "ok" if not dirty else "fail",
                       "" if not dirty else tf(lang, "{n} file(s) with uncommitted changes", n=dirty)))

    branch = status.get("branch")
    if status.get("detached"):
        items.append(_item("pushed", "Branch is pushed", "warn", tf(lang, "HEAD is detached")))
    elif not status.get("upstream"):
        items.append(_item("pushed", "Branch is pushed", "warn", tf(lang, "{b} has no upstream branch", b=branch or "?")))
    elif status.get("behind"):
        items.append(_item("pushed", "Branch is pushed", "fail", tf(lang, "{b} is {n} commit(s) behind {u}", b=branch, n=status["behind"], u=status["upstream"])))
    elif status.get("ahead"):
        items.append(_item("pushed", "Branch is pushed", "fail", tf(lang, "{b} is {n} commit(s) ahead of {u} (unpushed)", b=branch, n=status["ahead"], u=status["upstream"])))
    else:
        items.append(_item("pushed", "Branch is pushed", "ok", tf(lang, "{b} is in sync with {u}", b=branch, u=status["upstream"])))

    latest = last_tag(repo_root, "HEAD") if head else None
    if tag:
        items.append(_tag_item(repo_root, tag, head, latest, lang))
    else:
        since = 0
        if latest:
            p = git_panel.run_git(repo_root, "rev-list", "--first-parent", "--count", f"{latest}..HEAD")
            since = int((p.stdout or "0").strip() or 0) if p.returncode == 0 else 0
        items.append(_item("tag", "Tag", "ok", tf(lang, "last tag {t}, {n} commit(s) since", t=latest, n=since) if latest else tf(lang, "no tags yet")))

    if head and ctx.get("github"):
        items.append(_ci_item(ctx, head, lang))
        items.append(_blockers_item(ctx, blocker_labels, lang))
    else:
        reason = loc_error(_no_github(ctx), lang) if not ctx.get("github") else tf(lang, "no commits yet")
        items.append(_item("ci", "CI on HEAD", "skip", reason))
        items.append(_item("blockers", "No open blockers", "skip", reason))

    versions = find_versions(repo_root)
    if not versions:
        items.append(_item("versions", "Version strings agree", "skip", tf(lang, "no static version strings found")))
    else:
        distinct = sorted({_norm_version(v["version"]) for v in versions})
        listing = ", ".join(f"{v['file']}={v['version']}" for v in versions)
        if len(distinct) > 1:
            items.append(_item("versions", "Version strings agree", "fail", tf(lang, "mismatch: {x}", x=listing)))
        elif tag and _norm_version(tag) != distinct[0]:
            items.append(_item("versions", "Version strings agree", "fail", tf(lang, "files say {v} but the tag is {t} ({l})", v=distinct[0], t=tag, l=listing)))
        else:
            items.append(_item("versions", "Version strings agree", "ok", listing))

    fails = [i for i in items if i["status"] == "fail"]
    ready = not fails
    # What could not be verified: CI (skipped, still running or without runs),
    # blockers or the remote tag (skipped / remote unreachable), or a branch
    # with no upstream / a detached HEAD. "ready" stays "nothing
    # failed" (nothing here blocks); "complete" says every relevant check ran.
    unverified = [i["id"] for i in items
                  if (i["id"] in ("ci", "blockers", "tag") and i["status"] in ("skip", "warn"))
                  or (i["id"] == "pushed" and i["status"] == "warn")]
    result = {"ok": True, "kind": "release_check", "tag": tag, "head": head[:7] if head else "", "ready": ready,
              "complete": not unverified, "unverified": unverified,
              "items": items, "blocking": [i["id"] for i in fails],
              "counts": {s: sum(1 for i in items if i["status"] == s) for s in ("ok", "warn", "fail", "skip")},
              "github": {"slug": ctx.get("slug"), "usable": bool(ctx.get("github"))}}
    result["markdown"] = release_check_markdown(result, lang)
    return result


def release_check_markdown(result: Dict[str, Any], lang: str = "en") -> str:
    T = lambda s: tr(lang, s)  # noqa: E731
    es = lang.startswith("es")
    head = f"## {T('Release check')}" + (f" — {result['tag']}" if result.get("tag") else "") + (f" (`{result['head']}`)" if result.get("head") else "")
    unverified = result.get("unverified") or []
    if not result["ready"]:
        verdict = T("Not ready")
    elif unverified:
        verdict = tf(lang, "No failures detected · partial check ({n} unverified)", n=len(unverified))
    else:
        verdict = T("Ready to release")
    c = result["counts"]
    lines = [head, "", f"**{verdict}** · {c['ok']} ok · {c['warn']} " + ("avisos" if es else "warnings")
             + f" · {c['fail']} " + ("fallos" if es else "failures") + f" · {c['skip']} {T('skipped')}", ""]
    for i in result["items"]:
        label = i["label"]
        if label.startswith("Tag "):
            label = f"{T('Tag')} {label[4:]}"
        else:
            label = T(label)
        mark = {"ok": "[x]", "fail": "[ ]", "warn": "[ ]", "skip": "[ ]"}[i["status"]]
        tag = {"ok": "", "fail": " **" + ("FALLO" if es else "FAIL") + "**", "warn": " *(" + ("aviso" if es else "warning") + ")*",
               "skip": " *(" + T("skipped") + ")*"}[i["status"]]
        detail = f" — {i['detail']}" if i["detail"] else ""
        lines.append(f"- {mark} {label}{tag}{detail}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# One entry point for the composer command
# ---------------------------------------------------------------------------
def run_command(owner: Optional[str], repo_root: str, line: str, lang: str = "en") -> Dict[str, Any]:
    """``/github <line>`` -> a result dict that always carries ``markdown``
    (or ``error``). Read-only except through the approval the result asks for."""
    cmd = parse_command(line)
    if not cmd.get("ok"):
        usage = "\n".join(f"- `{u}`" for u in (cmd.get("usage") or {}).values())
        return {"ok": False, "error_class": "args.bad_command", "error": cmd["error"],
                "markdown": f"{cmd['error']}\n\n{usage}".strip()}
    action = cmd["action"]
    if action == "help":
        return {"ok": True, "kind": "help", "markdown": help_markdown(lang)}
    if action == "labels":
        res = list_labels(repo_root)
        if res.get("ok"):
            res.update(kind="labels", markdown=labels_markdown(res["labels"], res.get("slug") or "", lang))
        return localize(res, lang)
    if action == "labels_sync":
        return localize(prepare_label_sync(owner, repo_root, cmd["file"], lang), lang)
    if action == "notes":
        return localize(release_notes(repo_root, cmd.get("from"), cmd.get("to"), lang), lang)
    return localize(release_check(repo_root, cmd.get("tag"), lang=lang), lang)
