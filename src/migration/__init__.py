"""src/migration — import a project from the documented interchange format
(A34, docs/spec/paridad/MIGRACION.md) into Faustus.

Two calls only:

    report = preview(path)                       # never writes anything
    result = apply(path, report, decisions={})    # imports what preview allowed

``preview`` classifies every resource it finds under ``path`` as
``preserved`` (copied as-is, already Faustus's own shape), ``transformed``
(mapped into a Faustus-compatible shape), ``unsupported`` (no mapping —
listed with a reason, never silently dropped) or
``requires_reauthorization`` (a connector: Faustus never imports a
credential value, ever — see ``ConnectorImporter`` below).

``apply`` imports only ``preserved``/``transformed`` entries (or ones
``decisions`` explicitly forces to ``"import"``) into
``DATA_DIR/migrations/<import_id>/``, and always writes ``losses.md``
naming every entry it did NOT import and why — nothing found by ``preview``
is ever left out of the final record, even the parts correctly excluded
from the import itself.
"""
from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.constants import DATA_DIR

STATUS_VALUES = ("preserved", "transformed", "unsupported", "requires_reauthorization")

# Faustus's own /api/app-connectors two-step connect flow — the real place
# a re-authorized credential belongs, so the report can point at it instead
# of just saying "no".
CONNECTOR_REAUTH_HINT = (
    "POST /api/app-connectors then POST /api/app-connectors/{connector_id}/connect "
    "(routes/connector_routes.py) — re-enter the credential there by hand"
)

_SUPPORTED_MODEL_PROVIDERS = frozenset({"openai", "anthropic", "ollama", "local"})
_SUPPORTED_SCHEDULE_ACTION_TYPES = frozenset({"prompt", "workflow"})


@dataclass
class ResourceEntry:
    kind: str  # "agent" | "model" | "connector" | "skill" | "session" | "file" | "schedule"
    source_path: str
    status: str
    reason: str
    target_hint: str = ""
    # Extra payload the importer needs at apply() time (never credentials).
    _payload: Dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("_payload", None)
        return d


@dataclass
class Report:
    project_path: str
    entries: List[ResourceEntry] = field(default_factory=list)

    def by_status(self, status: str) -> List[ResourceEntry]:
        return [e for e in self.entries if e.status == status]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_path": self.project_path,
            "entries": [e.to_dict() for e in self.entries],
        }


@dataclass
class ApplyResult:
    import_id: str
    output_dir: str
    imported: List[ResourceEntry] = field(default_factory=list)
    excluded: List[ResourceEntry] = field(default_factory=list)
    losses_path: str = ""


def _safe_json_load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_yaml_or_json(path: Path) -> Any:
    if path.suffix.lower() == ".json":
        return _safe_json_load(path)
    try:
        import yaml  # type: ignore

        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# --------------------------------------------------------------------- #
# preview()
# --------------------------------------------------------------------- #


def _scan_agents(project: Path, entries: List[ResourceEntry]) -> None:
    agents_dir = project / "agents"
    if not agents_dir.is_dir():
        return
    for f in sorted(agents_dir.iterdir()):
        if f.suffix.lower() not in (".json", ".yaml", ".yml"):
            entries.append(ResourceEntry(
                kind="agent", source_path=str(f), status="unsupported",
                reason=f"unrecognised agent file extension {f.suffix!r}",
            ))
            continue
        data = _load_yaml_or_json(f)
        if not isinstance(data, dict) or not data.get("name"):
            entries.append(ResourceEntry(
                kind="agent", source_path=str(f), status="unsupported",
                reason="not a valid agent definition (missing 'name' or unparsable)",
            ))
            continue
        slug = str(data["name"]).strip().lower().replace(" ", "-")
        entries.append(ResourceEntry(
            kind="agent", source_path=str(f), status="transformed",
            reason="mapped to Faustus agent definition (AGENT.md frontmatter + body)",
            target_hint=f"migrations/<import_id>/agents/{slug}/AGENT.md",
            _payload={"data": data, "slug": slug},
        ))


def _scan_models(project: Path, entries: List[ResourceEntry]) -> None:
    models_file = project / "models.json"
    if not models_file.is_file():
        return
    data = _safe_json_load(models_file)
    models = data if isinstance(data, list) else []
    for i, m in enumerate(models):
        if not isinstance(m, dict):
            entries.append(ResourceEntry(
                kind="model", source_path=f"{models_file}[{i}]", status="unsupported",
                reason="not an object",
            ))
            continue
        provider = str(m.get("provider", "")).strip().lower()
        model_id = m.get("id", f"model_{i}")
        if provider in _SUPPORTED_MODEL_PROVIDERS:
            entries.append(ResourceEntry(
                kind="model", source_path=f"{models_file}[{i}]", status="transformed",
                reason=f"provider {provider!r} recognised by Faustus's model router",
                target_hint=f"migrations/<import_id>/models.json#{model_id}",
                _payload={"data": m},
            ))
        else:
            entries.append(ResourceEntry(
                kind="model", source_path=f"{models_file}[{i}]", status="unsupported",
                reason=f"provider {provider!r} has no Faustus client mapping",
            ))


def _scan_connectors(project: Path, entries: List[ResourceEntry]) -> None:
    connectors_file = project / "connectors.json"
    if not connectors_file.is_file():
        return
    data = _safe_json_load(connectors_file)
    connectors = data if isinstance(data, list) else []
    for i, c in enumerate(connectors):
        if not isinstance(c, dict):
            continue
        cid = c.get("id", f"connector_{i}")
        ctype = c.get("type", "unknown")
        had_credentials = bool(c.get("credentials"))
        reason = (
            f"connector {cid!r} (type={ctype!r}) always requires re-authorization — "
            f"credentials are never imported. {CONNECTOR_REAUTH_HINT}"
        )
        entries.append(ResourceEntry(
            kind="connector", source_path=f"{connectors_file}[{i}]",
            status="requires_reauthorization", reason=reason,
            target_hint="/api/app-connectors",
            _payload={"id": cid, "type": ctype, "had_credentials": had_credentials},
        ))


def _scan_skills(project: Path, entries: List[ResourceEntry]) -> None:
    skills_dir = project / "skills"
    if not skills_dir.is_dir():
        return
    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if skill_md.is_file():
            entries.append(ResourceEntry(
                kind="skill", source_path=str(skill_md), status="preserved",
                reason="already Faustus's own skill-pack shape (frontmatter + Markdown)",
                target_hint=f"migrations/<import_id>/skills/{skill_dir.name}/SKILL.md",
                _payload={"skill_dir": str(skill_dir)},
            ))
        else:
            entries.append(ResourceEntry(
                kind="skill", source_path=str(skill_dir), status="unsupported",
                reason="no SKILL.md in this skill directory",
            ))


def _scan_sessions(project: Path, entries: List[ResourceEntry]) -> None:
    sessions_dir = project / "sessions"
    if not sessions_dir.is_dir():
        return
    for f in sorted(sessions_dir.glob("*.jsonl")):
        lines_ok = 0
        lines_bad = 0
        try:
            for raw in f.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict) and "role" in obj:
                        lines_ok += 1
                    else:
                        lines_bad += 1
                except json.JSONDecodeError:
                    lines_bad += 1
        except OSError:
            entries.append(ResourceEntry(
                kind="session", source_path=str(f), status="unsupported",
                reason="could not read file",
            ))
            continue
        if lines_ok == 0:
            entries.append(ResourceEntry(
                kind="session", source_path=str(f), status="unsupported",
                reason="no valid {role, content} lines found",
            ))
            continue
        reason = "normalised to {role, content, ordinal}"
        if lines_bad:
            reason += f"; {lines_bad} malformed line(s) skipped (not dropped from the report)"
        entries.append(ResourceEntry(
            kind="session", source_path=str(f), status="transformed", reason=reason,
            target_hint=f"migrations/<import_id>/sessions/{f.name}",
            _payload={"file": str(f)},
        ))


def _scan_files(project: Path, entries: List[ResourceEntry]) -> None:
    files_dir = project / "files"
    if not files_dir.is_dir():
        return
    for f in sorted(p for p in files_dir.rglob("*") if p.is_file()):
        rel = f.relative_to(files_dir)
        entries.append(ResourceEntry(
            kind="file", source_path=str(f), status="preserved",
            reason="copied byte for byte",
            target_hint=f"migrations/<import_id>/files/{rel.as_posix()}",
            _payload={"file": str(f), "rel": rel.as_posix()},
        ))


def _scan_schedules(project: Path, entries: List[ResourceEntry]) -> None:
    schedules_file = project / "schedules.json"
    if not schedules_file.is_file():
        return
    data = _safe_json_load(schedules_file)
    schedules = data if isinstance(data, list) else []
    for i, s in enumerate(schedules):
        if not isinstance(s, dict):
            continue
        sid = s.get("id", f"schedule_{i}")
        action = s.get("action") if isinstance(s.get("action"), dict) else {}
        atype = str(action.get("type", "")).strip().lower()
        if atype in _SUPPORTED_SCHEDULE_ACTION_TYPES:
            entries.append(ResourceEntry(
                kind="schedule", source_path=f"{schedules_file}[{i}]", status="transformed",
                reason=f"action type {atype!r} understood by Faustus's scheduler",
                target_hint=f"migrations/<import_id>/schedules.json#{sid}",
                _payload={"data": s},
            ))
        else:
            entries.append(ResourceEntry(
                kind="schedule", source_path=f"{schedules_file}[{i}]", status="unsupported",
                reason=f"action type {atype!r} has no Faustus scheduler mapping",
            ))


def preview(path: str | Path) -> Report:
    """Classify every resource under ``path`` (the documented interchange
    format). Never writes anything — read-only."""
    project = Path(path)
    entries: List[ResourceEntry] = []
    if not project.is_dir():
        entries.append(ResourceEntry(
            kind="project", source_path=str(project), status="unsupported",
            reason="not a directory",
        ))
        return Report(project_path=str(project), entries=entries)

    _scan_agents(project, entries)
    _scan_models(project, entries)
    _scan_connectors(project, entries)
    _scan_skills(project, entries)
    _scan_sessions(project, entries)
    _scan_files(project, entries)
    _scan_schedules(project, entries)
    return Report(project_path=str(project), entries=entries)


# --------------------------------------------------------------------- #
# apply()
# --------------------------------------------------------------------- #


def _write_agent_md(out_dir: Path, slug: str, data: Dict[str, Any]) -> None:
    frontmatter_keys = {k: v for k, v in data.items() if k not in ("instructions",)}
    lines = ["---"]
    for k, v in frontmatter_keys.items():
        lines.append(f"{k}: {json.dumps(v)}")
    lines.append("---")
    lines.append("")
    lines.append(str(data.get("instructions", "")))
    agent_dir = out_dir / "agents" / slug
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "AGENT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _import_entry(entry: ResourceEntry, out_dir: Path) -> None:
    if entry.kind == "agent":
        _write_agent_md(out_dir, entry._payload["slug"], entry._payload["data"])
    elif entry.kind == "model":
        models_out = out_dir / "models.json"
        existing = json.loads(models_out.read_text(encoding="utf-8")) if models_out.exists() else []
        existing.append(entry._payload["data"])
        models_out.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    elif entry.kind == "skill":
        src_dir = Path(entry._payload["skill_dir"])
        dest_dir = out_dir / "skills" / src_dir.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_dir / "SKILL.md", dest_dir / "SKILL.md")
    elif entry.kind == "session":
        src_file = Path(entry._payload["file"])
        dest_file = out_dir / "sessions" / src_file.name
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        normalised: List[Dict[str, Any]] = []
        for ordinal, raw in enumerate(src_file.read_text(encoding="utf-8").splitlines()):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "role" in obj:
                normalised.append({
                    "role": obj.get("role"), "content": obj.get("content", ""),
                    "ts": obj.get("ts"), "ordinal": ordinal,
                })
        with dest_file.open("w", encoding="utf-8") as f:
            for row in normalised:
                f.write(json.dumps(row) + "\n")
    elif entry.kind == "file":
        src_file = Path(entry._payload["file"])
        dest_file = out_dir / "files" / entry._payload["rel"]
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
    elif entry.kind == "schedule":
        schedules_out = out_dir / "schedules.json"
        existing = json.loads(schedules_out.read_text(encoding="utf-8")) if schedules_out.exists() else []
        existing.append(entry._payload["data"])
        schedules_out.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    else:
        raise ValueError(f"no importer for resource kind {entry.kind!r}")


def apply(
    path: str | Path,
    report: Report,
    decisions: Optional[Dict[str, str]] = None,
    *,
    out_root: Optional[str | Path] = None,
) -> ApplyResult:
    """Import ``report``'s ``preserved``/``transformed`` entries (plus any
    ``decisions[source_path] == "import"`` override — never a way to force
    a credential in) into ``DATA_DIR/migrations/<import_id>/``.

    NEVER writes a connector's ``credentials``/token anywhere — connectors
    stay ``requires_reauthorization`` regardless of ``decisions``; forcing
    one to "import" imports its non-secret config only (never attempted
    here: connectors have no importer branch in ``_import_entry`` at all).

    Always writes ``losses.md`` naming every excluded entry with its reason
    — nothing ``preview`` found is left out of the final record.
    """
    decisions = decisions or {}
    import_id = str(uuid.uuid4())
    base = Path(out_root) if out_root is not None else Path(DATA_DIR) / "migrations"
    out_dir = base / import_id
    out_dir.mkdir(parents=True, exist_ok=True)

    imported: List[ResourceEntry] = []
    excluded: List[ResourceEntry] = []

    for entry in report.entries:
        forced = decisions.get(entry.source_path) == "import"
        importable = entry.status in ("preserved", "transformed") and entry.kind != "connector"
        if importable or (forced and entry.kind != "connector" and entry.kind != "project"):
            try:
                _import_entry(entry, out_dir)
                imported.append(entry)
            except Exception as exc:
                entry.reason = f"{entry.reason} (import failed: {exc})"
                excluded.append(entry)
        else:
            excluded.append(entry)

    manifest = {
        "import_id": import_id,
        "project_path": report.project_path,
        "imported": [e.to_dict() for e in imported],
        "excluded": [e.to_dict() for e in excluded],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    losses_lines = [
        "# Migration losses", "",
        f"Import `{import_id}` from `{report.project_path}`.", "",
        f"{len(imported)} resource(s) imported, {len(excluded)} excluded.", "",
    ]
    if excluded:
        losses_lines.append("| kind | source | status | reason |")
        losses_lines.append("|---|---|---|---|")
        for e in excluded:
            losses_lines.append(f"| {e.kind} | {e.source_path} | {e.status} | {e.reason} |")
    else:
        losses_lines.append("Nothing was excluded.")
    losses_path = out_dir / "losses.md"
    losses_path.write_text("\n".join(losses_lines) + "\n", encoding="utf-8")

    return ApplyResult(
        import_id=import_id, output_dir=str(out_dir), imported=imported,
        excluded=excluded, losses_path=str(losses_path),
    )
