"""src/effective_config.py — one explainable view of what actually governs a turn.

ARCH-03 (docs/spec/v2/MAPA_REUTILIZACION.md): the codebase already computes a
turn's configuration by walking several existing stores — global settings
(``src/settings.py``), a project's own knobs and its AGENTS.md/CLAUDE.md
(``services/projects.py``, ``src/project_instructions.py``), an agent preset
(``src/agent_defs.py``), per-model load options (``src/model_load_options.py``)
and the turn's own overrides — but nothing compiled them into one place a
person (or this module's own callers) could point at and ask "why is this the
effective value, and what did the level below it want instead?".
``_AGENT_RULES`` being assigned twice in ``src/agent_loop.py``, the second
assignment silently winning, was exactly this failure mode with no compiler to
catch it: two opinions at the same precedence level, one discarded without a
trace (fixed in this same lote; :func:`agent_rules_duplication_conflicts`
keeps watching for it, and for its two siblings that are still duplicated).

This module is that compiler, for the layers that already exist. It resolves

    global -> project -> role/preset -> model -> turn      (weakest to strongest)

for a curated set of fields whose sources are real, existing authorities —
nothing here introduces a second store (rule 4 of ``lotes/COMUN.md``). Every
effective value remembers which level supplied it and what every OTHER level
that had an opinion wanted instead (:attr:`EffectiveValue.overridden`); two
sources that disagree at the SAME level — more than one candidate instructions
file on disk, or a rule block assigned twice in one module — are reported in
``conflicts`` rather than resolved in silence. Secrets are masked with
``core.log_safety.redact_secrets`` before anything leaves this module; the
identity hash follows ``src.contracts.base.fingerprint`` (length-prefixed,
insensitive to the order fields happen to be listed in).

Never raises: a source this module cannot read degrades to "no opinion from
that level" — the same promise ``src/agent_profiles/resolver.py`` makes for
its own, larger ladder — because a page whose whole job is explaining
configuration must not be the one thing a bad setting breaks.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as _dc_field
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "EffectiveValue", "EffectiveConfig", "compile_effective", "prompt_blocks_for_session",
    "LEVELS_WEAKEST_FIRST", "LEVELS_STRONGEST_FIRST", "agent_rules_duplication_conflicts",
]

#: The precedence this module resolves, strongest first — the order the ladder
#: walk in :func:`_pick` actually iterates. ``LEVELS_WEAKEST_FIRST`` is the same
#: order the lote's own prose states it in ("global -> proyecto -> rol/preset ->
#: modelo -> turno") and is kept for callers that want to print it that way.
LEVELS_STRONGEST_FIRST: Tuple[str, ...] = ("turn", "model", "role_preset", "project", "global")
LEVELS_WEAKEST_FIRST: Tuple[str, ...] = tuple(reversed(LEVELS_STRONGEST_FIRST))

#: Every field this module knows how to resolve, even when no level currently
#: has an opinion about it — so the table always shows the same columns rather
#: than a set that shrinks and grows with what happens to be configured.
_KNOWN_FIELDS: Tuple[str, ...] = (
    "model", "max_rounds", "max_tool_calls", "stream_timeout_seconds",
    "harness_checks", "checkpoints", "run_tests",
    "project_instructions_enabled", "project_instructions_max_chars",
    "project_instructions_text", "num_ctx", "keep_alive", "temperature",
)

#: "this level stated nothing" — an object identity, never confused with a
#: real effective value of ``False``, ``0`` or ``""``, all of which are
#: meaningful opinions for several of the fields above (`checkpoints=False`,
#: `max_tool_calls=0` meaning unlimited).
_ABSENT = object()

#: field -> the DEFAULT_SETTINGS / agent_settings_schema key that states it at
#: the global level. Kept as one small table so the settings' own key names —
#: not a second guess at them — are what a "settings:<key>" source path names.
_GLOBAL_FIELD_SETTINGS: Dict[str, str] = {
    "max_rounds": "agent_max_rounds",
    "max_tool_calls": "agent_max_tool_calls",
    "stream_timeout_seconds": "agent_stream_timeout_seconds",
    "harness_checks": "agent_harness_checks",
    "checkpoints": "agent_checkpoints",
    "run_tests": "agent_project_tests",
    "project_instructions_enabled": "agent_project_instructions",
    "project_instructions_max_chars": "agent_project_instructions_max_chars",
}

#: Module-level rule blocks whose duplication is exactly the ARCH-03 bug
#: pattern, generalised: an assignment repeated at the same (module/global)
#: level, the last one winning without a trace. `_AGENT_RULES` is fixed by
#: this lote (see tests/test_effective_config.py); the other two are still
#: duplicated as dead first assignments and are reported, not hidden — see
#: "Limitaciones" in the final report.
_DUP_RULE_NAMES: Tuple[str, ...] = ("_AGENT_PREAMBLE", "_AGENT_RULES", "_API_AGENT_RULES")


def _mask_text(text: str) -> str:
    """A string, with anything that looks like a secret replaced by ``***``.

    Reuses the same redaction the log handlers apply
    (``core.log_safety.redact_secrets``) rather than a second pattern list —
    rule 4. Never raises: an import or regex failure returns the text
    unmasked rather than dropping it, on the theory that this module's own
    callers (the route, the Studio inspector) are the same trust boundary the
    settings store already sits behind; the masking is defence in depth for a
    value that should not have been a secret this far downstream in the
    first place.
    """
    try:
        from core.log_safety import redact_secrets
        return redact_secrets(text)
    except Exception as exc:  # noqa: BLE001 - never let masking break the caller
        logger.debug("effective_config: secret redaction unavailable: %s", exc)
        return text


def _mask_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _mask_text(value)
    if isinstance(value, (list, tuple)):
        return [_mask_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _mask_value(v) for k, v in value.items()}
    return _mask_text(str(value))


# ── the value, and what it beat ─────────────────────────────────────────────

@dataclass
class EffectiveValue:
    """One field's resolved value, where it came from, and what lost.

    ``source`` is ``None`` only when no level in the ladder had an opinion at
    all — an honest "unset" rather than a guessed default standing in for it.
    """

    field: str
    value: Any
    source: Optional[Dict[str, Any]]
    overridden: List[Dict[str, Any]] = _dc_field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "source": self.source,
            "overridden": list(self.overridden),
        }


@dataclass
class EffectiveConfig:
    """The compiled answer: every known field, its winner, and any conflicts."""

    owner: str
    session_id: str
    project_id: str
    model: str
    values: Dict[str, EffectiveValue]
    conflicts: List[Dict[str, Any]]
    hash: str
    computed_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "owner": self.owner,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "model": self.model,
            "computed_at": self.computed_at,
            "hash": self.hash,
            "values": {k: v.to_dict() for k, v in sorted(self.values.items())},
            "conflicts": list(self.conflicts),
        }


# ── the ladder walk ──────────────────────────────────────────────────────

#: One level's offering for a field: (value, source_path, source_line|None).
_Offer = Tuple[Any, str, Optional[int]]


def _pick(field_name: str, levels: Mapping[str, Mapping[str, _Offer]]) -> EffectiveValue:
    """Walk :data:`LEVELS_STRONGEST_FIRST` and take the first level with an
    opinion about ``field_name``. Every WEAKER level that also had one is
    recorded in ``overridden`` — not just the ones that lost to a level right
    above them, the same "outranked, not refused" distinction
    ``agent_profiles/resolver.py`` draws for its own ladder.
    """
    winner_level = ""
    winner_value: Any = None
    winner_source: Optional[Dict[str, Any]] = None
    overridden: List[Dict[str, Any]] = []
    for level in LEVELS_STRONGEST_FIRST:
        offer = levels.get(level, {}).get(field_name, _ABSENT)
        if offer is _ABSENT:
            continue
        value, path, line = offer
        if not winner_level:
            winner_level, winner_value = level, value
            winner_source = {"layer": level, "path": path, "line": line}
            continue
        overridden.append({
            "layer": level, "value": _mask_value(value), "path": path, "line": line,
        })
    return EffectiveValue(
        field=field_name, value=_mask_value(winner_value), source=winner_source,
        overridden=overridden,
    )


# ── layer sources ────────────────────────────────────────────────────────

def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception as exc:  # noqa: BLE001
        logger.debug("effective_config: get_setting(%s) failed: %s", key, exc)
        return default


def _global_offers() -> Dict[str, _Offer]:
    """The global level: ``src/settings.py``, described by
    ``src/agent_settings_schema.py`` (the schema names every ``agent_*`` key
    this build has — reused here only for the DEFAULT_SETTINGS fallback, not
    re-declared)."""
    offers: Dict[str, _Offer] = {}
    try:
        from src.settings import DEFAULT_SETTINGS
    except Exception as exc:  # noqa: BLE001
        logger.debug("effective_config: DEFAULT_SETTINGS unavailable: %s", exc)
        DEFAULT_SETTINGS = {}
    for field_name, setting_key in _GLOBAL_FIELD_SETTINGS.items():
        default = DEFAULT_SETTINGS.get(setting_key)
        value = _setting(setting_key, default)
        offers[field_name] = (value, f"settings:{setting_key}", None)
    default_model = _setting("default_model", "")
    if default_model:
        offers["model"] = (default_model, "settings:default_model", None)
    return offers


def _project_and_workspace(
    owner: Optional[str], session_id: str, project_id: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """The project row (if any) and its workspace, reusing
    ``services/projects.py`` exactly as the chat routes do."""
    project: Optional[Dict[str, Any]] = None
    workspace = ""
    try:
        from services import projects as _proj
        if project_id:
            project = _proj.get_store().get(project_id, owner)
        elif session_id:
            project = _proj.project_for_session(session_id, owner)
        if project:
            workspace = str(project.get("workspace") or "")
        elif session_id:
            workspace = _proj.workspace_for_session(session_id, owner)
    except Exception as exc:  # noqa: BLE001
        logger.debug("effective_config: project/workspace lookup failed: %s", exc)
    return project, workspace


def _project_offers(
    project: Optional[Dict[str, Any]], workspace: str,
) -> Tuple[Dict[str, _Offer], List[Dict[str, Any]]]:
    """The project level: the project's own agent knobs
    (``services.projects.agent_options``) plus its workspace's AGENTS.md /
    CLAUDE.md text (``src/project_instructions.py`` — this is also where the
    ``#`` composer shortcut's per-session rules land, since
    ``project_instructions.remember`` appends to that same file).

    A second candidate instructions file (AGENTS.md AND CLAUDE.md both
    present) is a same-level conflict: `project_instructions.find_file` picks
    the first one silently, so it is reported here rather than left for
    someone to discover the hard way."""
    offers: Dict[str, _Offer] = {}
    conflicts: List[Dict[str, Any]] = []
    if project:
        try:
            from services import projects as _proj
            opts = _proj.agent_options(project)
            path = f"project:{project.get('id') or ''}"
            if "checkpoints" in opts:
                offers["checkpoints"] = (bool(opts["checkpoints"]), path, None)
            if "run_tests" in opts:
                offers["run_tests"] = (bool(opts["run_tests"]), path, None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("effective_config: project agent_options failed: %s", exc)
    if workspace:
        try:
            from src import project_instructions as _pinstr
            found = _pinstr.found_files(workspace)
            if len(found) > 1:
                conflicts.append({
                    "field": "project_instructions_text",
                    "layer": "project",
                    "reason": (
                        "more than one instructions file exists in this workspace; only the "
                        "first (by lookup order) is injected into the prompt"
                    ),
                    "candidates": [{"path": p} for p in found],
                    "winner": found[0],
                })
            info = _pinstr.read(workspace)
            if info.get("text"):
                offers["project_instructions_text"] = (
                    info["text"], info.get("path") or workspace, None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("effective_config: project_instructions read failed: %s", exc)
    return offers, conflicts


def _session_field(session_id: str, column: str) -> str:
    """One column off the `sessions` row, or "" — never raises, never opens a
    second session store."""
    if not session_id:
        return ""
    try:
        from core.database import Session as DbSession, SessionLocal
        db = SessionLocal()
        try:
            row = db.query(getattr(DbSession, column)).filter(DbSession.id == session_id).first()
        finally:
            db.close()
        value = getattr(row, column, None) if row is not None else None
        return str(value) if value else ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("effective_config: session.%s lookup failed: %s", column, exc)
        return ""


def _role_preset_offers(
    session_id: str, workspace: str, turn_overrides: Mapping[str, Any],
) -> Dict[str, _Offer]:
    """The role/preset level: an agent definition (``src/agent_defs.py``,
    the store ``src/agent_profiles`` resolves against), named explicitly by
    the turn (``turn_overrides["agent"]``) or by the session's own ``mode``
    column. Empty when neither names a definition this build knows — most
    chat sessions do not, since `agent_defs` is primarily the sub-agent/
    dispatch store rather than the main chat loop's own configuration, and an
    empty level here is exactly "no opinion", not a bug."""
    slug = str((turn_overrides or {}).get("agent") or "").strip()
    if not slug:
        slug = _session_field(session_id, "mode")
    if not slug:
        return {}
    try:
        from src import agent_defs
        defn = agent_defs.load_all(workspace or None).by_slug().get(slug)
    except Exception as exc:  # noqa: BLE001
        logger.debug("effective_config: agent_defs lookup failed: %s", exc)
        return {}
    if defn is None:
        return {}
    path = f"agent_defs:{slug}"
    offers: Dict[str, _Offer] = {}
    if getattr(defn, "model", ""):
        offers["model"] = (defn.model, path, None)
    if getattr(defn, "max_rounds", None) is not None:
        offers["max_rounds"] = (int(defn.max_rounds), path, None)
    if getattr(defn, "timeout_s", None) is not None:
        offers["stream_timeout_seconds"] = (int(defn.timeout_s), path, None)
    return offers


def _model_layer_offers(model: str) -> Dict[str, _Offer]:
    """The model level: per-model load defaults
    (``src/model_load_options.py``), matched by model name only — this
    module has no endpoint URL to key the saved entry's host against, so the
    FIRST saved entry whose model name matches wins, best-effort. Under
    ``/ctx``-style explicit turn overrides these values are already meant to
    lose (see that module's own docstring), which is exactly this ladder's
    model < turn precedence."""
    if not model:
        return {}
    try:
        from src import model_load_options as mlo
        table = mlo.all_options()
    except Exception as exc:  # noqa: BLE001
        logger.debug("effective_config: model_load_options unavailable: %s", exc)
        return {}
    for key, opts in table.items():
        try:
            _endpoint_id, saved_model = mlo.split_key(key)
        except Exception:
            continue
        try:
            matches = mlo._model_matches(saved_model, model)  # reuse the real matcher
        except Exception:
            matches = saved_model == model
        if not matches:
            continue
        path = f"model_load_options:{key}"
        offers: Dict[str, _Offer] = {}
        if opts.get("num_ctx") is not None:
            offers["num_ctx"] = (opts["num_ctx"], path, None)
        if opts.get("keep_alive"):
            offers["keep_alive"] = (opts["keep_alive"], path, None)
        extra = opts.get("extra") or {}
        if isinstance(extra, dict) and extra.get("temperature") is not None:
            offers["temperature"] = (extra["temperature"], path, None)
        return offers
    return {}


_TURN_DIRECT_FIELDS: Tuple[str, ...] = (
    "model", "max_rounds", "max_tool_calls", "stream_timeout_seconds",
    "harness_checks", "checkpoints", "run_tests",
)
_TURN_GEN_FIELDS: Tuple[str, ...] = ("num_ctx", "keep_alive", "temperature")


def _turn_offers(turn_overrides: Mapping[str, Any]) -> Dict[str, _Offer]:
    """The turn level: ``turn_overrides`` as the caller passed it — the
    strongest level by construction, since it is literally what this one
    turn asked for. ``gen_overrides`` (top_p, think, num_ctx, ...) is read the
    same way ``src/llm_core.py`` reads it: an explicit per-request override
    that sits above the model's own saved defaults."""
    ov = dict(turn_overrides or {})
    path = "turn_overrides"
    offers: Dict[str, _Offer] = {}
    for key in _TURN_DIRECT_FIELDS:
        if key in ov and ov[key] is not None:
            offers[key] = (ov[key], path, None)
    gen = ov.get("gen_overrides")
    if isinstance(gen, dict):
        for key in _TURN_GEN_FIELDS:
            if key in gen and gen[key] is not None:
                offers[key] = (gen[key], f"{path}.gen_overrides", None)
    return offers


# ── the _AGENT_RULES-shaped conflict, generalised ───────────────────────

def agent_rules_duplication_conflicts() -> List[Dict[str, Any]]:
    """Same-level conflicts still present among ``src/agent_loop.py``'s own
    module-level rule blocks — the exact shape of the ARCH-03 bug this module
    exists to catch, applied to itself. A name assigned more than once at
    module scope is two opinions at the same (global) precedence level, the
    LAST one winning without a trace; this reports every one still standing
    instead of only the one this lote was asked to fix.

    Never raises: a source read failure degrades to "nothing to report",
    consistent with every other layer in this module."""
    conflicts: List[Dict[str, Any]] = []
    try:
        import inspect
        from src import agent_loop
        source = inspect.getsource(agent_loop)
    except Exception as exc:  # noqa: BLE001
        logger.debug("effective_config: could not read agent_loop source: %s", exc)
        return conflicts
    lines = source.splitlines()
    for name in _DUP_RULE_NAMES:
        pattern = re.compile(rf"^{re.escape(name)}\s*=")
        hits = [i + 1 for i, line in enumerate(lines) if pattern.match(line)]
        if len(hits) > 1:
            conflicts.append({
                "field": name,
                "layer": "global",
                "reason": (
                    f"{name} is assigned {len(hits)} times at module scope in "
                    "src/agent_loop.py; the last assignment wins in silence"
                ),
                "candidates": [{"path": "src/agent_loop.py", "line": ln} for ln in hits],
                "winner_line": hits[-1],
            })
    return conflicts


# ── the hash ─────────────────────────────────────────────────────────────

def _compute_hash(values: Mapping[str, EffectiveValue]) -> str:
    """A stable fingerprint of the resolved values (not their provenance):
    reordering the fields does not change it (the parts are sorted by field
    name before hashing), but changing any one value does. Follows
    ``src.contracts.base.fingerprint`` — length-prefixed, the same identity
    rule the rest of the contracts layer uses."""
    from src.contracts.base import fingerprint
    parts = sorted(((k, v.value) for k, v in values.items()), key=lambda p: p[0])
    return fingerprint(parts)


# ── the one entry point ──────────────────────────────────────────────────

def compile_effective(
    owner: str,
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
    model: Optional[str] = None,
    turn_overrides: Optional[Mapping[str, Any]] = None,
) -> EffectiveConfig:
    """The effective configuration for one (owner, session, project, model,
    turn) — every known field resolved global -> project -> role/preset ->
    model -> turn, with provenance and conflicts. Never raises."""
    from src.contracts.base import now_iso

    owner = owner or ""
    session_id = str(session_id or "")
    turn_overrides = dict(turn_overrides or {})

    project, workspace = _project_and_workspace(owner, session_id, str(project_id or ""))
    resolved_project_id = str((project or {}).get("id") or project_id or "")
    resolved_model = str(
        model or turn_overrides.get("model") or _session_field(session_id, "model") or ""
    )

    project_offers, conflicts = _project_offers(project, workspace)
    turn_offers = _turn_offers(turn_overrides)
    if model and "model" not in turn_offers:
        # The explicit `model` argument is itself a turn-level opinion — this
        # IS "what model this turn uses" — distinct from anything named inside
        # `turn_overrides`. Without this, `cfg.model` (used to key the model
        # layer below) and `values["model"]` could disagree about why.
        turn_offers["model"] = (str(model), "compile_effective(model=...)", None)
    levels: Dict[str, Dict[str, _Offer]] = {
        "global": _global_offers(),
        "project": project_offers,
        "role_preset": _role_preset_offers(session_id, workspace, turn_overrides),
        "model": _model_layer_offers(resolved_model),
        "turn": turn_offers,
    }

    fields = sorted(set(_KNOWN_FIELDS) | {f for level in levels.values() for f in level})
    values: Dict[str, EffectiveValue] = {f: _pick(f, levels) for f in fields}

    conflicts = list(conflicts) + agent_rules_duplication_conflicts()

    cfg = EffectiveConfig(
        owner=owner, session_id=session_id, project_id=resolved_project_id,
        model=resolved_model, values=values, conflicts=conflicts, hash="",
        computed_at=now_iso(),
    )
    cfg.hash = _compute_hash(values)
    return cfg


# ── the prompt-blocks preview ────────────────────────────────────────────

def prompt_blocks_for_session(
    owner: str, session_id: Optional[str] = None, needs_admin: bool = False,
) -> Dict[str, Any]:
    """The tool-section blocks that would enter a turn's system prompt, with
    each block's origin — built by calling
    ``src.agent_loop.base_prompt_with_origin`` (the real assembly path every
    turn uses), never a copy of its logic.

    Scope: without the turn's own message there is no RAG-narrowed tool
    selection to reproduce (that lives in ``src/tool_index.py`` and
    ``routes/chat_routes.py``, outside this lote's assigned files), so this
    reports the BASE block set for the owner — every tool section not
    disabled for them — which is the same set a real turn falls back to
    whenever retrieval is unavailable, not a synthetic one.
    """
    from src import agent_loop

    owner = owner or ""
    disabled: set = set()
    try:
        from src.tool_security import blocked_tools_for_owner
        disabled = set(blocked_tools_for_owner(owner or None))
    except Exception as exc:  # noqa: BLE001
        logger.debug("effective_config: blocked_tools_for_owner failed: %s", exc)

    prompt, blocks = agent_loop.base_prompt_with_origin(
        disabled_tools=disabled, needs_admin=bool(needs_admin), relevant_tools=None,
        compact=False, owner=owner or None,
    )
    masked_blocks = [
        {
            "kind": b.get("kind"), "name": b.get("name"), "chars": b.get("chars"),
            "text": _mask_text(b.get("text") or ""),
        }
        for b in blocks
    ]
    return {
        "owner": owner,
        "session_id": str(session_id or ""),
        "prompt_chars": len(prompt),
        "prompt": _mask_text(prompt),
        "blocks": masked_blocks,
        "note": (
            "base block set for this owner: the turn's own message (needed for the "
            "RAG-narrowed tool selection a live turn may use instead) was not available here"
        ),
    }
