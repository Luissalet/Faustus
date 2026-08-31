"""agent_harness.py — reliability harness for the Agent loop.

The model's prose is a *claim*; the tool trace is the *evidence*. A turn only
counts as verified when the two agree. This module is the single place that
knows how to:

  * keep a per-turn ledger of tool executions (what ran, whether it succeeded,
    which paths it touched, which paths it *observed* in outputs);
  * detect mutation claims ("I've added…", "he modificado…") in a text-only
    round and check them against the ledger;
  * detect paths the model mentions that exist neither on disk nor in any tool
    output this turn (fabricated filesystem);
  * detect "I will now do X" announcements that end the turn without a call
    (multilingual — the built-in supervisor regex only knew English);
  * build the structured error a file tool returns for an unknown path (root +
    real suggestions) so the model recovers by discovering instead of guessing;
  * summarize the *real* working-tree change (git status/diff) at the end;
  * produce the compact tool-use policy injected for local models.

Everything here is dependency-free (stdlib only) and defensive: a harness
failure must never break a chat turn, so callers wrap it in try/except and the
helpers themselves swallow filesystem/git errors.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import subprocess
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------

# Tools whose success IS evidence that files changed on disk.
FILE_MUTATION_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
# Shell tools: only evidence when the command itself looks mutating (see below).
SHELL_TOOLS = frozenset({"bash", "python"})
# Non-file side effects (documents, notes, mail…) — evidence for "I did X" claims
# that are not about repo files.
OTHER_EFFECT_TOOLS = frozenset({
    "create_document", "update_document", "edit_document", "manage_documents",
    "manage_notes", "manage_calendar", "manage_tasks", "manage_memory",
    "send_email", "reply_to_email", "delete_email", "archive_email",
    "mark_email_read", "unsubscribe_email", "bulk_email",
    "mcp__email__send_email", "mcp__email__reply_to_email",
    "mcp__email__delete_email", "mcp__email__archive_email",
    "generate_image", "edit_image", "manage_session", "create_session",
    "manage_endpoints", "manage_mcp", "manage_webhooks", "manage_tokens",
    "manage_settings", "manage_skills", "manage_contact",
})
# Tools whose OUTPUT grounds paths: anything they print exists (or existed).
DISCOVERY_TOOLS = frozenset({
    "ls", "glob", "grep", "read_file", "get_workspace", "project_context",
    "bash", "python",
})

# Shell commands that plausibly change the filesystem/repo.
_MUTATING_SHELL_RE = re.compile(
    r"(?:^|[\s;&|(])(?:rm|mv|cp|mkdir|rmdir|touch|tee|sed\s+-i|perl\s+-p?i|"
    r"git\s+(?:add|commit|checkout|switch|restore|reset|rm|mv|apply|am|merge|rebase|stash|cherry-pick|revert|clean)|"
    r"npm\s+(?:install|i|ci|uninstall|update|run)|pnpm|yarn|pip3?\s+(?:install|uninstall)|"
    r"python3?\s+(?:-m\s+pip|setup\.py)|make\b|cargo\s+(?:build|add|install)|go\s+(?:build|mod|install)|"
    r"chmod|chown|ln\s|patch\b|truncate|dd\s|"
    r"Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Move-Item|Copy-Item|Rename-Item)"
    r"|(?:>>?\s*(?!/dev/null\b)[\w./\\~-]+)",
    re.IGNORECASE,
)


def shell_command_looks_mutating(command: str) -> bool:
    return bool(_MUTATING_SHELL_RE.search(command or ""))


# ---------------------------------------------------------------------------
# Claim detection (Spanish + English)
# ---------------------------------------------------------------------------

_ES_PP = (
    r"(?:cread[oa]s?|añadid[oa]s?|agregad[oa]s?|modificad[oa]s?|actualizad[oa]s?|"
    r"implementad[oa]s?|eliminad[oa]s?|borrad[oa]s?|cambiad[oa]s?|editad[oa]s?|"
    r"escrit[oa]s?|corregid[oa]s?|arreglad[oa]s?|aplicad[oa]s?|movid[oa]s?|"
    r"renombrad[oa]s?|refactorizad[oa]s?|integrad[oa]s?|completad[oa]s?|"
    r"terminad[oa]s?|finalizad[oa]s?|solucionad[oa]s?|reparad[oa]s?|"
    r"configurad[oa]s?|instalad[oa]s?|guardad[oa]s?|list[oa]s?)"
)
_EN_PP = (
    r"(?:created|added|modified|updated|implemented|removed|deleted|changed|"
    r"edited|written|wrote|fixed|applied|moved|renamed|refactored|integrated|"
    r"completed|finished|resolved|repaired|configured|installed|saved|patched|"
    r"inserted|replaced|adjusted|extended|wired(?:\s+up)?|hooked(?:\s+up)?)"
)

# A reply that is nothing but "Done." / "Hecho." — a completion claim with no
# content. Counted as a mutation claim only when the model did *nothing* this
# turn (see TurnLedger.check_completion): after real tool work it is a report.
_BARE_DONE_ES = re.compile(r"^\s*(?:hecho|listo|completado|terminado)[.!]?\s*$", re.IGNORECASE | re.MULTILINE)
_BARE_DONE_EN = re.compile(r"^\s*(?:done|all\s+set|completed|finished)[.!]?\s*$", re.IGNORECASE | re.MULTILINE)
BARE_DONE_PATTERNS = (_BARE_DONE_ES, _BARE_DONE_EN)

MUTATION_CLAIM_PATTERNS: List[re.Pattern] = [
    # Spanish — perfect / preterite / passive / "está listo"
    re.compile(r"\b(?:he|hemos)\s+" + _ES_PP, re.IGNORECASE),
    re.compile(r"\b(?:ya\s+)?(?:está|están|queda|quedan|ha\s+quedado|han\s+quedado)\s+(?:ya\s+)?(?:completamente\s+|totalmente\s+)?" + _ES_PP, re.IGNORECASE),
    re.compile(r"\bse\s+(?:ha|han)\s+" + _ES_PP, re.IGNORECASE),
    re.compile(r"\b(?:creé|añadí|agregué|modifiqué|actualicé|implementé|eliminé|borré|cambié|edité|escribí|corregí|arreglé|apliqué|moví|renombré|refactoricé|integré|completé|terminé|finalicé|solucioné|reparé|configuré|instalé|guardé)\b", re.IGNORECASE),
    re.compile(r"\b(?:los\s+)?cambios\s+(?:han\s+sido\s+|fueron\s+|están\s+)?" + _ES_PP, re.IGNORECASE),
    re.compile(r"\b(?:todo|la\s+funcionalidad|la\s+implementación|el\s+código|la\s+tarea)\s+(?:ya\s+)?(?:está|queda)\s+(?:completamente\s+|totalmente\s+)?(?:list[oa]|implementad[oa]|integrad[oa]|hech[oa]|completad[oa]|terminad[oa])", re.IGNORECASE),
    _BARE_DONE_ES,
    # English
    re.compile(r"\bI(?:'ve|\s+have)\s+(?:now\s+|also\s+|successfully\s+|just\s+)?" + _EN_PP, re.IGNORECASE),
    re.compile(r"\bI\s+(?:then\s+|also\s+|now\s+)?" + r"(?:created|added|modified|updated|implemented|removed|deleted|changed|edited|wrote|fixed|applied|moved|renamed|refactored|integrated|completed|finished|patched|inserted|replaced)\b", re.IGNORECASE),
    re.compile(r"\b(?:has|have)\s+been\s+(?:successfully\s+)?" + _EN_PP, re.IGNORECASE),
    re.compile(r"\b(?:is|are)\s+now\s+(?:fully\s+|completely\s+)?(?:implemented|complete|done|ready|in\s+place|integrated|working|fixed|updated)\b", re.IGNORECASE),
    re.compile(r"\b(?:the\s+)?(?:implementation|feature|changes?|fix|task|work)\s+(?:is|are)\s+(?:now\s+)?(?:complete|done|ready|finished|in\s+place)\b", re.IGNORECASE),
    _BARE_DONE_EN,
    re.compile(r"^\s*(?:[-*•]\s*)?(?:✅|✓|☑|\[x\])\s+\S", re.MULTILINE),
    re.compile(r"^\s*(?:[-*•]\s*)?(?:Added|Created|Modified|Updated|Implemented|Removed|Deleted|Fixed|Wired|Hooked)\b", re.MULTILINE),
    re.compile(r"^\s*(?:[-*•]\s*)?(?:Añadid[oa]|Cread[oa]|Modificad[oa]|Actualizad[oa]|Implementad[oa]|Eliminad[oa]|Corregid[oa]|Añadí|Creé|Modifiqué|Actualicé|Implementé|Eliminé)\b", re.MULTILINE),
]

# Phrases that announce an action the model then never performs. Matched
# against the END of a text-only round (the classic "I'll start by editing
# X:" stall). English + Spanish.
INTENT_PATTERNS: List[re.Pattern] = [
    re.compile(
        r"\b(?:let\s+me|i(?:'ll|\s+will|\s+am\s+going\s+to|'m\s+going\s+to)|"
        r"i\s+(?:need|should|must|have)\s+to|we\s+(?:need|should|must)\s+to|"
        r"(?:first|next|now|then),?\s+i(?:'ll|\s+will)|let'?s\s+(?:start|begin|first|now)|"
        r"going\s+to|i\s+will\s+now)\s+"
        r"(?:start|begin|check|investigate|look|see|read|open|fetch|inspect|verify|"
        r"diagnose|examine|debug|run|call|create|write|edit|modify|update|add|remove|"
        r"delete|implement|fix|change|apply|patch|search|find|list|locate|explore|"
        r"review|test|install|build|refactor|proceed|continue|make|use|generate|"
        r"tail|grab|pull|view|query|try)\b[^\n]{0,200}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:voy\s+a|vamos\s+a|procedo\s+a|paso\s+a|empiezo\s+(?:a|por|con)|"
        r"comienzo\s+(?:a|por|con)|comenzaré|empezaré|comencemos|empecemos|"
        r"(?:primero|ahora|luego|después|a\s+continuación|seguidamente),?\s+"
        r"(?:voy|vamos|procedo|paso|empiezo|comienzo|necesito|debo|tengo\s+que|hay\s+que|"
        r"crearé|modificaré|actualizaré|añadiré|agregaré|revisaré|leeré|buscaré|"
        r"implementaré|editaré|ejecutaré|comprobaré|verificaré)|"
        r"necesito\s+(?:revisar|leer|ver|buscar|comprobar|verificar|modificar|crear|"
        r"editar|actualizar|ejecutar|examinar|inspeccionar|localizar|encontrar|abrir|añadir|agregar|implementar)|"
        r"debo\s+(?:revisar|leer|ver|buscar|comprobar|modificar|crear|editar|actualizar|ejecutar)|"
        r"tengo\s+que\s+(?:revisar|leer|ver|buscar|comprobar|modificar|crear|editar|actualizar|ejecutar)|"
        r"déjame|permíteme|dejadme)\b[^\n]{0,200}",
        re.IGNORECASE,
    ),
]

# Question to the user → the turn legitimately ends without tools.
_QUESTION_TAIL_RE = re.compile(r"[?¿][\s*_`\"')\]]*$")

# Path-like tokens. Requires a known source/config extension so prose like
# "v1.2" or "e.g." is not treated as a file. Directory prefixes optional.
_PATH_EXTS = (
    "vue|jsx?|tsx?|mjs|cjs|py|pyi|css|scss|sass|less|html?|json|jsonc|md|mdx|"
    r"ya?ml|toml|ini|cfg|conf|env|txt|rst|go|rs|java|kt|kts|swift|cs|cpp|cc|c|h|hpp|"
    r"rb|php|sh|bash|zsh|ps1|psm1|bat|cmd|sql|graphql|gql|proto|xml|svg|lock|"
    r"dockerfile|makefile|gradle|properties|csv|ipynb|dart|lua|r|m|mm|ex|exs|erl|"
    r"hs|scala|clj|cljs|elm|vim|tf|tfvars|hcl|nix|zig|v|sol|wasm"
)
PATH_TOKEN_RE = re.compile(
    r"(?<![\w@:/\\.-])"                                   # not glued to a URL/word
    r"((?:\.{0,2}[\w@.-]+[/\\])*"                          # optional dir segments
    r"[\w@.-]*[A-Za-z_][\w@.-]*\.(?:" + _PATH_EXTS + r")(?!\w))"  # basename.ext, whole extension
    r"(?![\w/\\.-]*(?:://|@))",
    re.IGNORECASE,
)
_PATH_STOPWORDS = {
    "e.g.", "i.e.", "etc.", "vs.", "node.js", "next.js", "vue.js", "react.js",
    "express.js", "three.js", "d3.js", "chart.js", "socket.io", "asp.net", "nuxt.js",
}
_IGNORED_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".next", ".nuxt", "target",
    ".idea", ".vscode", "coverage", ".tox", ".cache", "site-packages", ".odysseus",
}
_INDEX_MAX_FILES = 60000
_INDEX_TTL_S = 20.0


def detect_language(text: str) -> str:
    """Very small ES/EN heuristic used only to pick the wording of user-facing
    harness notes. Defaults to English."""
    t = f" {(text or '').lower()} "
    if any(ch in t for ch in ("¿", "¡", "ñ", "á", "é", "í", "ó", "ú")):
        return "es"
    es_hits = sum(t.count(w) for w in (" que ", " los ", " las ", " para ", " pero ", " también ", " está ", " hay ", " con ", " una ", " del ", " por "))
    en_hits = sum(t.count(w) for w in (" the ", " and ", " with ", " that ", " this ", " for ", " but ", " also ", " is ", " are ", " of "))
    return "es" if es_hits > en_hits else "en"


# A mutation claim only counts when it is about something technical: a
# file/path, code, a UI element, a command… "He creado un personaje" in a
# story, or "hemos terminado la reunión", must never trip the harness.
_TECH_CONTEXT_RE = re.compile(
    r"(?:`[^`\n]+`|"
    r"\b(?:file|files|path|folder|directory|repo|code|function|method|class|component|"
    r"button|route|endpoint|api|test|tests|script|module|import|variable|config|schema|"
    r"migration|css|html|js|javascript|typescript|python|json|yaml|markdown|readme|"
    r"stylesheet|handler|listener|hook|template|query|database|table|column|commit|patch|diff|"
    r"implementation|feature|fix|bug|changes?|update|refactor|logic|frontend|backend|server|client|"
    r"page|view|menu|form|layout|style|styles|counter|element|dom|ui|"
    r"fichero|ficheros|archivo|archivos|ruta|rutas|carpeta|directorio|c[oó]digo|funci[oó]n|"
    r"funciones|m[eé]todo|clase|componente|bot[oó]n|botones|tarjeta|tarjetas|endpoint|"
    r"pruebas?|script|m[oó]dulo|variable|configuraci[oó]n|esquema|migraci[oó]n|estilos?|"
    r"plantilla|consulta|base\s+de\s+datos|tabla|columna|parche|cambios?|implementaci[oó]n|"
    r"l[oó]gica|interfaz|frontend|backend|servidor|cliente|p[aá]gina|vista|men[uú]|formulario)\b)",
    re.IGNORECASE,
)


# "I have completed the review / He terminado el análisis": finishing an
# investigation is not a change. Applied to completion-type verbs only.
_COMPLETION_VERB_RE = re.compile(
    r"\b(?:completed|finished|resolved|done|complet[ae]d[oa]s?|terminad[oa]s?|finalizad[oa]s?|"
    r"complet[eé]|termin[eé]|finalic[eé]|list[oa]s?)\b",
    re.IGNORECASE,
)
_INVESTIGATION_OBJECT_RE = re.compile(
    r"\b(?:review(?:ing)?|analy[sz](?:is|ing|e)|investigat(?:ion|ing|e)|explor(?:ation|ing|e)|"
    r"inspect(?:ion|ing)|read(?:ing)?|search(?:ing)?|research|audit(?:ing)?|assessment|diagnos(?:is|ing)|"
    r"revisi[oó]n|revisar|an[aá]lisis|analizar|investigaci[oó]n|investigar|exploraci[oó]n|explorar|"
    r"inspecci[oó]n|inspeccionar|lectura|leer|b[uú]squeda|buscar|auditor[ií]a|auditar|diagn[oó]stico|"
    r"evaluaci[oó]n|evaluar|comprobaci[oó]n|comprobar|verificaci[oó]n|verificar)\b",
    re.IGNORECASE,
)


# "No he modificado nada" / "not modified" — a negated claim is the opposite of
# a claim. Checked on the few words right before a match.
_NEGATION_BEFORE_RE = re.compile(
    r"\b(?:no|nunca|jam[aá]s|tampoco|ni|nada|ning[uú]n[oa]?|nothing|not|never|neither|nor|without|sin|"
    r"haven't|hasn't|hadn't|didn't|don't|doesn't|wasn't|weren't|isn't|aren't)\s+(?:\w+\s+)?$",
    re.IGNORECASE,
)


def _negated_before(text: str, start: int) -> bool:
    return bool(_NEGATION_BEFORE_RE.search(text[max(0, start - 28):start]))


def _finishes_an_investigation(match_text: str, following: str) -> bool:
    """True for "completed the analysis"-style matches: a completion verb whose
    object (next ~50 chars) is an investigation, not a change."""
    if not _COMPLETION_VERB_RE.search(match_text):
        return False
    return bool(_INVESTIGATION_OBJECT_RE.search(following[:50]))


def find_mutation_claims(text: str, limit: int = 4, include_bare_done: bool = True) -> List[str]:
    """Return up to `limit` distinct snippets that read as 'I changed X' *about
    something technical* (see _TECH_CONTEXT_RE). Narrative first person is
    ignored. With include_bare_done=False the content-free "Done." / "Hecho."
    lines are skipped (only claims that describe a change are returned)."""
    out: List[str] = []
    seen: Set[str] = set()
    body = text or ""
    has_paths = bool(PATH_TOKEN_RE.search(body))
    # A bare "Done." / "Hecho." reply is a completion claim by itself.
    terse = len(body.strip()) < 200
    for pat in MUTATION_CLAIM_PATTERNS:
        if not include_bare_done and pat in BARE_DONE_PATTERNS:
            continue
        for m in pat.finditer(body):
            start = max(0, m.start() - 20)
            snippet = body[start:m.end() + 60].replace("\n", " ").strip()
            # Context window: the sentence-ish region around the match.
            ctx_start = max(0, m.start() - 160)
            ctx = body[ctx_start:m.end() + 160]
            standalone = pat.pattern.startswith("^")  # bare "Done." style lines
            if not (has_paths or (terse and standalone) or _TECH_CONTEXT_RE.search(ctx)):
                continue
            if _finishes_an_investigation(m.group(0), body[m.end():m.end() + 50]):
                continue
            if _negated_before(body, m.start()):
                continue
            key = m.group(0).strip().lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(snippet[:140])
            if len(out) >= limit:
                return out
    return out


def find_intent_announcement(text: str, tail_chars: int = 600) -> Optional[str]:
    """If the END of `text` announces an action (EN/ES) — or ends with a colon
    introducing content that never came — return the offending phrase."""
    body = (text or "").rstrip()
    if not body:
        return None
    if _QUESTION_TAIL_RE.search(body):
        return None
    tail = body[-tail_chars:]
    # Only the last paragraph matters: earlier announcements may have been
    # followed by real calls in the same round.
    last_para = re.split(r"\n\s*\n", tail.strip())[-1]
    for pat in INTENT_PATTERNS:
        matches = list(pat.finditer(last_para))
        if matches:
            return matches[-1].group(0).strip()
    stripped = last_para.rstrip(" \t*_`")
    if stripped.endswith(":"):
        return stripped.splitlines()[-1].strip()[-160:]
    return None


_MISSING_ACK_RE = re.compile(
    r"(no existe|no exist[ií]a|no (?:lo |la )?(?:he )?encontr|no se encontr|not exist|doesn'?t exist|does not exist|"
    r"did not exist|not found|couldn'?t find|could not find|no such file|en su lugar|en vez de|instead)",
    re.I,
)


def extract_path_tokens(text: str) -> List[str]:
    """Path-like tokens mentioned in prose (de-duplicated, order kept)."""
    out: List[str] = []
    seen: Set[str] = set()
    for m in PATH_TOKEN_RE.finditer(text or ""):
        tok = m.group(1).strip().strip(".,;:()[]{}<>\"'`")
        # A leading "@" is the composer's file-mention sigil (src/file_mentions.py),
        # not part of the path. Left on, "@src/app.py" never matches the workspace
        # index and every mention would look like a user-named missing file — the
        # exact false positive that fires check_target_substitution.
        if tok.startswith("@") and len(tok) > 1:
            tok = tok.lstrip("@")
        low = tok.lower()
        if not tok or low in _PATH_STOPWORDS or low in seen:
            continue
        # skip bare version-ish or numeric names like 1.0.json? keep — rare.
        if re.fullmatch(r"[\d.]+\.\w+", tok):
            continue
        seen.add(low)
        out.append(tok)
    return out


# ---------------------------------------------------------------------------
# Workspace file index (for path grounding + suggestions)
# ---------------------------------------------------------------------------

_index_cache: Dict[str, Tuple[float, List[str]]] = {}


def workspace_file_index(workspace: str, max_files: int = _INDEX_MAX_FILES) -> List[str]:
    """Relative paths (forward slashes) of files under `workspace`, cached for a
    few seconds. Skips vendored/build dirs. Never raises."""
    if not workspace:
        return []
    try:
        root = os.path.realpath(workspace)
    except OSError:
        return []
    now = time.time()
    cached = _index_cache.get(root)
    if cached and now - cached[0] < _INDEX_TTL_S:
        return cached[1]
    files: List[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS and not d.startswith(".git")]
            rel_dir = os.path.relpath(dirpath, root)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
            for fn in filenames:
                files.append(rel_dir + fn)
                if len(files) >= max_files:
                    raise StopIteration
    except StopIteration:
        pass
    except OSError as e:
        logger.debug("workspace index failed for %s: %s", root, e)
    _index_cache[root] = (now, files)
    return files


def invalidate_index(workspace: Optional[str]) -> None:
    if workspace:
        try:
            _index_cache.pop(os.path.realpath(workspace), None)
        except OSError:
            pass


def _norm(p: str) -> str:
    return p.replace("\\", "/").lower().strip("/ ")


def workspace_relative(workspace: Optional[str], path: str) -> str:
    """`path` relative to the workspace (forward slashes) when it resolves inside
    it; unchanged otherwise. Models sometimes pass absolute paths
    (D:\\proj\\server.py): chips, audit rows and review-mode keys are nicer and
    stable when every consumer sees the same relative form."""
    if not workspace or not path or not os.path.isabs(path):
        return path
    try:
        root = os.path.realpath(workspace)
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return path
    root_cmp, real_cmp = (root.lower(), real.lower()) if os.name == "nt" else (root, real)
    if real_cmp == root_cmp or not real_cmp.startswith(root_cmp.rstrip(os.sep) + os.sep):
        return path
    return os.path.relpath(real, root).replace(os.sep, "/")


def path_exists_in_workspace(workspace: str, token: str) -> bool:
    """True if `token` names an existing file: as given (abs or relative to the
    workspace), or by basename anywhere in the index."""
    if not token:
        return False
    try:
        if os.path.isabs(token) and os.path.exists(token):
            return True
        if workspace and os.path.exists(os.path.join(workspace, token)):
            return True
    except (OSError, ValueError):
        pass
    if not workspace:
        return False
    t = _norm(token)
    base = t.rsplit("/", 1)[-1]
    for rel in workspace_file_index(workspace):
        r = rel.lower()
        if r == t or r.endswith("/" + t) or r.rsplit("/", 1)[-1] == base:
            return True
    return False


def suggest_paths(workspace: str, raw_path: str, limit: int = 5) -> List[str]:
    """Real files in the workspace that look like what the model asked for.
    Basename fuzzy match first, then substring match on the full path."""
    if not workspace or not raw_path:
        return []
    files = workspace_file_index(workspace)
    if not files:
        return []
    want = _norm(raw_path)
    want_base = want.rsplit("/", 1)[-1]
    want_stem = want_base.rsplit(".", 1)[0] if "." in want_base else want_base
    scored: List[Tuple[float, str]] = []
    for rel in files:
        r = rel.lower()
        base = r.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0] if "." in base else base
        score = 0.0
        if base == want_base:
            score = 1.0
        elif want_stem and (want_stem in stem or stem in want_stem) and min(len(stem), len(want_stem)) >= 3:
            score = 0.85
        else:
            ratio = difflib.SequenceMatcher(None, stem, want_stem).ratio() if want_stem else 0
            if ratio >= 0.6:
                score = ratio * 0.8
        if score == 0 and want and want in r:
            score = 0.5
        if score > 0:
            # prefer shallower paths on ties
            scored.append((score - min(r.count("/"), 8) * 0.005, rel))
    scored.sort(reverse=True)
    return [rel for _, rel in scored[:limit]]


def not_found_error(tool: str, raw_path: str, resolved: str, workspace: Optional[str]) -> str:
    """Structured, recovery-oriented error text for a missing path."""
    parts = [f"{tool}: '{raw_path}' does not exist (resolved to {resolved})."]
    if workspace:
        parts.append(f"Workspace root: {workspace}")
        sugg = suggest_paths(workspace, raw_path)
        if sugg:
            parts.append("Did you mean one of these EXISTING files? " + ", ".join(sugg))
        else:
            parts.append("No file with a similar name exists in the workspace.")
    parts.append(
        "Do NOT guess another path. Discover the real one first: use glob "
        "(e.g. pattern \"**/*<name>*\") or grep for a symbol, or ls the folder, "
        "then call the tool again with a path returned by those tools."
    )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Git working-tree verification
# ---------------------------------------------------------------------------

def git_change_summary(workspace: Optional[str], timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    """`git status --porcelain` + `git diff --shortstat` for the workspace, or
    None when it is not a git repo / git is unavailable."""
    if not workspace or not os.path.isdir(workspace):
        return None
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        if probe.returncode != 0 or "true" not in (probe.stdout or ""):
            return None
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        stat = subprocess.run(
            ["git", "diff", "--shortstat"],
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        changed: List[Dict[str, str]] = []
        for line in (status.stdout or "").splitlines():
            if len(line) < 4:
                continue
            code, path = line[:2], line[3:].strip()
            changed.append({"status": code.strip() or "M", "path": path})
        return {
            "changed": changed[:200],
            "changed_count": len(changed),
            "shortstat": (stat.stdout or "").strip(),
        }
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("git summary failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Per-turn ledger
# ---------------------------------------------------------------------------

class TurnLedger:
    """What actually happened this turn, as recorded from tool executions."""

    def __init__(self, workspace: Optional[str] = None, user_text: str = ""):
        self.workspace = workspace
        self.user_text = user_text or ""
        self.language = detect_language(self.user_text)
        self.events: List[Dict[str, Any]] = []
        self.observed_paths: Set[str] = set()
        self.rejections = 0
        self.length_continues = 0
        self.intent_nudges = 0
        self.progress: Optional[List[Dict[str, Any]]] = None
        self.progress_round: int = 0
        self._events_at_last_progress = 0
        self.finish_reasons: List[Optional[str]] = []
        self.stop_reason: str = "complete"
        self.notes: List[str] = []
        self.static_checks: List[Dict[str, Any]] = []
        self.syntax_rejections = 0
        self.target_nudges = 0
        # Functional verification + turn baseline (see project_tests.py,
        # auto_review.py, workspace_checkpoints.py).
        self.checkpoint: Optional[Dict[str, Any]] = None   # {"sha", ...} | {"skipped"|"failed": True}
        self.tests: Optional[Dict[str, Any]] = None        # project_tests.compact()
        self.tests_runs = 0
        self.tests_fix_rounds = 0
        self.review: Optional[Dict[str, Any]] = None       # auto_review.compact()
        self.review_runs = 0
        self.review_fix_rounds = 0
        self.review_mutations_at_fix = -1     # len(mutations) when the review fix round started
        self.asked_user = False
        # The user's own message may name real files; those count as observed.
        for tok in extract_path_tokens(self.user_text):
            self.observed_paths.add(_norm(tok))

    # -- recording ----------------------------------------------------------
    def record(self, tool: str, content: str, result: Dict[str, Any], round_num: int = 0) -> Dict[str, Any]:
        ok = _result_ok(result)
        paths = [workspace_relative(self.workspace, p) for p in _paths_from_args(tool, content)]
        kind = "read"
        if tool in FILE_MUTATION_TOOLS:
            kind = "mutation"
        elif tool in SHELL_TOOLS:
            kind = "mutation" if shell_command_looks_mutating(content) else "shell"
        elif tool in OTHER_EFFECT_TOOLS:
            kind = "effect"
        ev = {
            "round": round_num, "tool": tool, "ok": ok, "kind": kind,
            "paths": paths, "error": (result or {}).get("error") if not ok else None,
        }
        self.events.append(ev)
        if tool == "ask_user":
            self.asked_user = True
        # Delegated workers: their verified mutations are evidence for the
        # coordinator's own report ("the workers changed X").
        if tool == "delegate_agents" and isinstance(result, dict):
            for sub in result.get("subagents") or []:
                if not isinstance(sub, dict):
                    continue
                sub_paths = [workspace_relative(self.workspace, p) for p in (sub.get("mutations") or []) if isinstance(p, str)]
                if sub_paths:
                    self.events.append({
                        "round": round_num, "tool": "delegate_agents:" + str(sub.get("name") or "worker"),
                        "ok": True, "kind": "mutation", "paths": sub_paths, "error": None,
                    })
                    for p in sub_paths:
                        self.observed_paths.add(_norm(p))
            invalidate_index(self.workspace)
        if ok and tool == "write_file" and isinstance(result, dict):
            # Whole-file rewrite of an existing file: real work, but the most
            # common way a local model silently drops code it did not remember.
            d = result.get("diff") if isinstance(result.get("diff"), dict) else None
            if d and not d.get("new_file") and (int(d.get("removed") or 0) >= 5):
                for p in paths[:1]:
                    note = f"whole_file_rewrite:{p}"
                    if note not in self.notes:
                        self.notes.append(note)
        if ok:
            for p in paths:
                self.observed_paths.add(_norm(p))
            if tool in DISCOVERY_TOOLS or kind == "mutation":
                out = _result_text(result)
                for tok in extract_path_tokens(out[:60000]):
                    self.observed_paths.add(_norm(tok))
            if kind == "mutation":
                invalidate_index(self.workspace)
        return ev

    def record_progress(self, todos: List[Dict[str, Any]], round_num: int) -> List[Dict[str, Any]]:
        """Store a todowrite snapshot; mark items newly completed WITHOUT any
        successful tool call since the previous snapshot as unverified."""
        prev = {t.get("content"): t for t in (self.progress or [])}
        evidence_since = any(e["ok"] and e["tool"] != "todowrite" for e in self.events[self._events_at_last_progress:])
        mutation_since = any(e["ok"] and e["kind"] in ("mutation", "effect") for e in self.events[self._events_at_last_progress:])
        annotated: List[Dict[str, Any]] = []
        for t in todos:
            item = dict(t)
            before = prev.get(t.get("content"))
            newly_done = t.get("status") == "completed" and (before is None or before.get("status") != "completed")
            if newly_done:
                item["verified"] = bool(evidence_since)
                item["mutation_backed"] = bool(mutation_since)
            elif before is not None:
                for k in ("verified", "mutation_backed"):
                    if k in before:
                        item[k] = before[k]
            annotated.append(item)
        self.progress = annotated
        self.progress_round = round_num
        self._events_at_last_progress = len(self.events)
        return annotated

    # -- evidence -----------------------------------------------------------
    @property
    def mutations(self) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["ok"] and e["kind"] == "mutation"]

    @property
    def effects(self) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["ok"] and e["kind"] in ("mutation", "effect")]

    @property
    def failed(self) -> List[Dict[str, Any]]:
        return [e for e in self.events if not e["ok"]]

    def mutated_paths(self) -> List[str]:
        out: List[str] = []
        for e in self.mutations:
            for p in e["paths"]:
                if p not in out:
                    out.append(p)
        return out

    def tools_run(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for e in self.events:
            counts[e["tool"]] = counts.get(e["tool"], 0) + 1
        return counts

    # -- checks -------------------------------------------------------------
    def unverified_paths(self, text: str, limit: int = 6) -> List[str]:
        """Paths the text mentions that neither exist nor were observed."""
        out: List[str] = []
        for tok in extract_path_tokens(text):
            n = _norm(tok)
            if n in self.observed_paths or any(o.endswith("/" + n) or n.endswith("/" + o) for o in self.observed_paths):
                continue
            if path_exists_in_workspace(self.workspace or "", tok):
                continue
            out.append(tok)
            if len(out) >= limit:
                break
        return out

    def user_missing_paths(self) -> List[str]:
        """Files the USER named that do not exist in the workspace (and were not
        created this turn). Empty without a workspace."""
        if not self.workspace:
            return []
        created = {_norm(p).rsplit("/", 1)[-1] for p in self.mutated_paths()}
        out: List[str] = []
        for tok in extract_path_tokens(self.user_text):
            if path_exists_in_workspace(self.workspace, tok):
                continue
            if _norm(tok).rsplit("/", 1)[-1] in created:
                continue
            out.append(tok)
        return out[:4]

    def check_target_substitution(self, text: str) -> Optional[Dict[str, Any]]:
        """The user named a file that does not exist; the model changed OTHER
        files and its answer neither says the named file is missing nor asked the
        user. Seen live (t4): 'fix static/js/cards.js' → cards.js does not exist,
        the model guessed a null-guard in projects.js and reported it as the fix.
        Returns {"missing": [...], "changed": [...]} when the answer must
        acknowledge the substitution, else None."""
        if not self.mutations:
            return None
        missing = self.user_missing_paths()
        if not missing:
            return None
        if any(e.get("tool") == "ask_user" for e in self.events):
            return None
        low = (text or "").lower()
        for tok in missing:
            base = _norm(tok).rsplit("/", 1)[-1]
            # Acknowledged: the named file is mentioned together with a "does not
            # exist / not found / instead" statement anywhere in the answer.
            if base in low and _MISSING_ACK_RE.search(low):
                return None
        return {"missing": missing, "changed": self.mutated_paths()[:6]}

    def target_substitution_message(self, check: Dict[str, Any]) -> str:
        missing = ", ".join(f"`{m}`" for m in check.get("missing", []))
        changed = ", ".join(f"`{c}`" for c in check.get("changed", [])) or "other files"
        return (
            "[Harness check — automatic message from the runtime, not from the user] "
            f"The user named {missing}, which does NOT exist in the workspace, and you changed "
            f"{changed} instead without saying so. Do NOT redo or extend the edits. Write the final "
            "answer so that it (a) states explicitly that the named file does not exist, (b) names the "
            "file(s) you changed instead and why you believe that is what the user meant, and (c) if "
            "you found no concrete defect and the change is a guess, say so and offer to revert it — "
            "or call ask_user to confirm which file / behaviour the user meant."
        )

    def check_completion(self, text: str) -> Dict[str, Any]:
        """Judge a text-only (final) round against the evidence.

        Returns {"ok": bool, "reasons": [...], "claims": [...], "bad_paths": [...],
                 "intent": str|None}
        """
        body = text or ""
        claims = find_mutation_claims(body)
        bad_paths = self.unverified_paths(body)
        intent = find_intent_announcement(body)
        reasons: List[str] = []
        is_question = bool(_QUESTION_TAIL_RE.search(body.rstrip()))
        if claims and not self.effects:
            # "Done." after real (read-only / shell) tool work is a report of
            # whatever ran, not a fabricated edit: reject only when the text
            # describes changes, or when nothing at all was executed.
            did_something = any(e["ok"] for e in self.events)
            if not did_something or find_mutation_claims(body, include_bare_done=False):
                reasons.append("claims_without_mutation")
        if bad_paths:
            # Unknown paths are a hallucination when the model presents work as
            # done, or when it reasons about a filesystem it never looked at
            # (no tool ran this turn). After real discovery, a stray unknown
            # name is usually a future/hypothetical file ("we could add
            # utils.js"): record it, do not reject.
            if claims or (not self.events and not is_question):
                reasons.append("fabricated_paths")
            else:
                note = "unverified_mentions:" + ",".join(bad_paths)
                if note not in self.notes:
                    self.notes.append(note)
        if intent and not claims:
            reasons.append("intent_without_action")
        return {
            "ok": not reasons,
            "reasons": reasons,
            "claims": claims,
            "bad_paths": bad_paths,
            "intent": intent,
        }

    # -- messages -----------------------------------------------------------
    def rejection_message(self, check: Dict[str, Any]) -> str:
        """Instruction fed back to the model (English: local models follow
        English instructions more reliably; the user-facing summary is
        localized separately)."""
        lines = [
            "[Harness check — automatic message from the runtime, not from the user]",
            "Your last message is NOT supported by the tool log of this turn:",
        ]
        tools = ", ".join(f"{k}×{v}" for k, v in self.tools_run().items()) or "none"
        if "claims_without_mutation" in check["reasons"]:
            lines.append(
                "- You describe changes as done, but no mutation tool (edit_file / write_file / "
                "apply_patch / a mutating shell command) succeeded this turn. Files actually "
                f"modified this turn: NONE. Tools that ran: {tools}."
            )
            for c in check["claims"][:3]:
                lines.append(f'    claim: "{c}"')
        if "fabricated_paths" in check["reasons"]:
            lines.append(
                "- You mention paths that do not exist in the workspace and were never returned "
                "by any tool: " + ", ".join(check["bad_paths"]) + ". Never reason about files you "
                "have not seen in a tool result."
            )
            if self.workspace:
                hints: List[str] = []
                for bp in check["bad_paths"][:3]:
                    for s in suggest_paths(self.workspace, bp, limit=3):
                        if s not in hints:
                            hints.append(s)
                if hints:
                    lines.append("    Real files with similar names: " + ", ".join(hints[:8]))
        if "intent_without_action" in check["reasons"]:
            lines.append(
                f'- You announced "{check["intent"]}" and then ended the turn without calling '
                "any tool. Announcing is not doing."
            )
        lines.append(
            "Nothing you described has happened. Do not apologize and do not restate the plan. "
            "Either (a) DO the work now — discover real files with glob/grep/ls, read them, then "
            "change them with edit_file / apply_patch / write_file; or (b) if what was asked "
            "ALREADY exists in the code, say exactly that, citing the file and lines you saw in a "
            "tool result — do not describe it as something you did; or (c) if you cannot do it, "
            "reply with ONE sentence saying that no changes were made and why. Never present "
            "unexecuted work as done."
        )
        return "\n".join(lines)

    def user_note(self, check: Dict[str, Any], final: bool) -> str:
        """Localized note appended to the visible answer when the model kept
        claiming unsupported work after the retries were exhausted."""
        es = self.language == "es"
        parts: List[str] = []
        if "claims_without_mutation" in check["reasons"]:
            parts.append(
                "el modelo afirma haber realizado cambios, pero ninguna herramienta de escritura "
                "se ejecutó con éxito en este turno — **archivos modificados realmente: ninguno**"
                if es else
                "the model claims changes, but no write tool succeeded this turn — **files "
                "actually modified: none**"
            )
        if "fabricated_paths" in check["reasons"]:
            parts.append(
                ("menciona rutas que no existen en el workspace: " if es else
                 "it mentions paths that do not exist in the workspace: ")
                + ", ".join(f"`{p}`" for p in check["bad_paths"])
            )
        if "intent_without_action" in check["reasons"]:
            parts.append(
                "anunció una acción y terminó sin ejecutar ninguna herramienta" if es else
                "it announced an action and ended without calling any tool"
            )
        head = "⚠️ **Verificación del harness**: " if es else "⚠️ **Harness check**: "
        tail = (
            " No des por hecho nada de lo anterior." if es else
            " Do not take the text above as done."
        )
        return head + "; ".join(parts) + "." + tail

    def summary(self, git: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "language": self.language,
            "tools_run": self.tools_run(),
            "tool_calls": len(self.events),
            "failed_calls": len(self.failed),
            "mutations": self.mutated_paths(),
            "effects": len(self.effects),
            "rejections": self.rejections,
            "length_continues": self.length_continues,
            "intent_nudges": self.intent_nudges,
            "finish_reasons": self.finish_reasons,
            "stop_reason": self.stop_reason,
            "notes": self.notes,
            "git": git,
            "progress": self.progress,
            "static_checks": self.static_checks,
            "checkpoint": (self.checkpoint or {}).get("sha") if isinstance(self.checkpoint, dict) else None,
            "tests": self.tests,
            "tests_fix_rounds": self.tests_fix_rounds,
            "review": self.review,
            "review_fix_rounds": self.review_fix_rounds,
            "asked_user": self.asked_user,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _result_ok(result: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return False
    if result.get("blocked") or result.get("approval_required"):
        return False
    code = result.get("exit_code")
    if code not in (None, 0):
        return False
    return True


def _result_text(result: Optional[Dict[str, Any]]) -> str:
    if not isinstance(result, dict):
        return ""
    for k in ("output", "stdout", "content", "results", "response"):
        v = result.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _paths_from_args(tool: str, content: str) -> List[str]:
    """Best-effort extraction of the path(s) a tool call targets."""
    raw = (content or "").strip()
    paths: List[str] = []
    if tool in ("read_file", "write_file", "edit_file", "ls", "glob", "grep"):
        if raw.startswith("{"):
            try:
                args = json.loads(raw)
                if isinstance(args, dict):
                    p = args.get("path")
                    if isinstance(p, str) and p.strip():
                        paths.append(p.strip())
            except (json.JSONDecodeError, TypeError):
                pass
        elif tool in ("read_file", "write_file"):
            first = raw.split("\n", 1)[0].strip()
            if first:
                paths.append(first)
    elif tool == "apply_patch":
        for m in re.finditer(r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+)$", raw, re.MULTILINE):
            paths.append(m.group(1).strip())
    elif tool in SHELL_TOOLS:
        for tok in extract_path_tokens(raw[:4000]):
            paths.append(tok)
    return paths


def local_model_policy() -> str:
    """Compact imperative tool-use policy for local/weak models."""
    return (
        "\n\n## Reliability rules (enforced by the runtime — violations are rejected)\n"
        "1. NEVER invent file names or paths. A path you have not seen in a tool result does not "
        "exist for you. Discover first: glob (e.g. \"**/*card*\"), grep for a symbol, or ls; then read_file. "
        "Only call tools by the exact names you were given (files/folders: ls, glob, grep, read_file — "
        "list_models is about AI models, not files).\n"
        "2. Do not announce actions (\"I will now edit X\", \"voy a modificar X\"). Call the tool in the "
        "same turn. Text without a tool call ENDS the turn and is treated as your final answer.\n"
        "3. Never say a file was created/changed/fixed unless edit_file, write_file or apply_patch "
        "returned success in THIS turn. The runtime compares your words with the tool log and "
        "rejects unsupported claims; a shell command counts only if it actually modified files.\n"
        "4. After a tool error: read the error, discover the real path or fix the arguments, retry. "
        "Never pretend it worked and never switch to describing what you 'would' do.\n"
        "5. For any task with 2+ steps, FIRST call todowrite with the objectives (one in_progress), "
        "then call todowrite again each time an objective is verifiably done. The user sees it as a "
        "progress panel.\n"
        "6. Before finishing a coding task, re-read the changed region (read_file with offset/limit) "
        "or run a check (py_compile, node --check, tests) and report only what the tools showed.\n"
        "7. Keep narration minimal: tools first, then a short factual summary in the user's language "
        "listing exactly which files changed (from the tool results, not from memory).\n"
        "8. If the request is ambiguous (which bug? which file? which behaviour?) and the code you "
        "read shows no concrete defect, do NOT guess a fix: call ask_user with the specific question "
        "(or the 2-3 candidate interpretations). Never rewrite code you have not shown to be wrong.\n"
        "9. Change existing files with edit_file (exact old_string → new_string). write_file is for "
        "NEW files or when the user asked for a full rewrite; a whole-file rewrite from memory drops "
        "code you did not remember.\n"
        "10. Never start a server, dev watcher or any command that does not exit on its own "
        "(uvicorn, flask run, npm start/dev, tail -f, …) in the foreground: it blocks the turn and "
        "gets killed. Either put `#!bg` as the first line of the bash block to run it detached, "
        "bound it with `timeout 30 …`, or verify the code without running it (tests, import, "
        "calling the handler directly). Never run interactive programs."
    )


# ---------------------------------------------------------------------------
# Post-mutation static checks
# ---------------------------------------------------------------------------

_CHECK_TIMEOUT = 20.0


def _check_python(path: str) -> Optional[str]:
    import sys
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "py_compile", path],
            capture_output=True, text=True, timeout=_CHECK_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None if isinstance(e, OSError) else f"py_compile timed out"
    if proc.returncode == 0:
        return None
    err = (proc.stderr or proc.stdout or "").strip()
    return err.splitlines()[-1][:300] if err else "py_compile failed"


def _check_javascript(path: str) -> Optional[str]:
    import shutil
    import tempfile
    node = shutil.which("node")
    if not node:
        return None  # cannot check — not an error
    target = path
    tmp = None
    try:
        # ES modules (import/export) fail `node --check` on a .js name because
        # Node parses it as CommonJS; check a .mjs copy instead.
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(200000)
        if re.search(r"^\s*(?:import\s|export\s)", head, re.MULTILINE) and not path.endswith((".mjs", ".cjs")):
            fd, tmp = tempfile.mkstemp(suffix=".mjs")
            with os.fdopen(fd, "w", encoding="utf-8") as tf:
                tf.write(head)
            target = tmp
        proc = subprocess.run([node, "--check", target], capture_output=True, text=True, timeout=_CHECK_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    if proc.returncode == 0:
        return None
    err = (proc.stderr or proc.stdout or "").strip()
    # Keep the location + message lines, drop the stack noise.
    lines = [l for l in err.splitlines() if l.strip() and not l.strip().startswith("at ") and "node:internal" not in l]
    text = " | ".join(lines[:4])[:400]
    return text.replace(target, os.path.basename(path)) if text else "node --check failed"


def _check_json(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
    except json.JSONDecodeError as e:
        return f"invalid JSON: {e}"[:300]
    except OSError:
        return None
    return None


_CHECKERS = {
    ".py": _check_python,
    ".js": _check_javascript, ".mjs": _check_javascript, ".cjs": _check_javascript,
    ".json": _check_json,
}


def static_check_files(paths: Iterable[str], workspace: Optional[str] = None, limit: int = 12) -> List[Dict[str, Any]]:
    """Cheap syntax checks for files the agent just changed. Returns one entry
    per checked file: {"path", "ok", "error"}. Files without a checker, or
    that no longer exist, are skipped. Never raises."""
    results: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for raw in paths:
        if len(results) >= limit:
            break
        if not raw:
            continue
        p = raw
        try:
            if not os.path.isabs(p) and workspace:
                p = os.path.join(workspace, p)
            p = os.path.realpath(p)
        except (OSError, ValueError):
            continue
        key = _norm(p)
        if key in seen or not os.path.isfile(p):
            continue
        seen.add(key)
        ext = os.path.splitext(p)[1].lower()
        checker = _CHECKERS.get(ext)
        if not checker:
            continue
        try:
            err = checker(p)
        except Exception as e:  # never break the turn
            logger.debug("static check failed for %s: %s", p, e)
            continue
        display = os.path.relpath(p, workspace).replace(os.sep, "/") if workspace and _norm(p).startswith(_norm(os.path.realpath(workspace))) else p
        results.append({"path": display, "ok": err is None, "error": err})
    return results
