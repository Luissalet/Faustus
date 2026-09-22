"""src/instincts.py — Instincts: small learned behaviours with confidence,
scoped per project (and, once seen widely enough, promoted to global).

An instinct is an atomic learned behaviour extracted from real sessions in
the background by the cheap utility model: a `trigger` ("when writing new
FastAPI routes"), an `action` ("use the router factory in routes/ and
register in app.py"), a `confidence` in [0.1, 0.95], a `domain`
(code-style|workflow|testing|tooling|communication|debugging|other) and a
`scope` (project|global). They are never a new gate on anything — they only
add context to the prompt (`render_block`) when confident enough, the same
way the repository map and project concepts already do.

Storage
-------
`<DATA_DIR>/instincts/<owner-safe>/instincts.json`, one flat JSON object per
owner, written atomically (temp + os.replace via `core.atomic_io`). The dict
KEY is a storage key derived from `(scope, project, trigger, action)` — see
`instinct_id` — so the SAME underlying idea ("when X, do Y") can exist as
several distinct `project`-scoped records (one per project it was observed
in) plus at most one `scope="global"` record once `promote()` merges them.
This is a deliberate refinement of "id: slug from trigger+action, stable"
(LOT_I's brief): the id is still a stable, deterministic function of trigger
and action, with the scope/project folded in only so two projects observing
the identical pattern do not collide into one record before `promote()` has
had a chance to notice they are the same idea in ≥2 places. `promote()` and
`evolve()` group records by the trigger+action identity alone (see
`_base_slug`), independent of that per-record key.

Three things this module deliberately does NOT do
--------------------------------------------------
* No new approval gate. `render_block` only ever adds a block of text to the
  prompt; nothing here blocks a tool call or requires a click.
* No model call on the read/inject path. `render_block`/`list_instincts`/
  `status` are pure, deterministic reads of the JSON store.
* No cross-owner leakage. Every function takes `owner` explicitly and reads/
  writes only that owner's file; there is no "global" owner sentinel like
  skills' `GLOBAL_SKILL_OWNER` — `scope="global"` on a record is orthogonal
  to WHO owns the file (that owner still owns every one of their records,
  project-scoped or global-scoped).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

__all__ = [
    "DOMAINS", "SCOPES", "STATUSES",
    "instinct_id", "project_key", "detect_project_name",
    "effective_confidence",
    "list_instincts", "get", "upsert", "add",
    "confirm", "contradict", "retire", "delete",
    "promote", "evolve",
    "render_block",
    "should_extract", "looks_like_correction", "extract_from_turn",
    "export_json", "import_json", "status",
]

DOMAINS = frozenset({
    "code-style", "workflow", "testing", "tooling", "communication",
    "debugging", "other",
})
SCOPES = frozenset({"project", "global"})
STATUSES = frozenset({"active", "retired"})

# ── confidence math (unit-tested directly) ─────────────────────────────────

CONFIRM_BUMP = 0.05
CONTRADICT_PENALTY = 0.10
DECAY_PER_WEEK = 0.02
CONFIDENCE_FLOOR = 0.1
CONFIDENCE_CEILING = 0.95
RETIRE_THRESHOLD = 0.15
EVIDENCE_CAP = 12
_WEEK_SECONDS = 7 * 86400


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _initial_confidence(observations: int) -> float:
    """Initial confidence bucket by how many times an instinct has been
    observed so far (own upsert count, not a single extraction batch's raw
    count — the effect is the same: more independent sightings, more
    confidence, applied as a floor so a later re-observation never drags a
    since-confirmed instinct's confidence back down)."""
    n = max(0, int(observations or 0))
    if n <= 2:
        return 0.3
    if n <= 5:
        return 0.5
    if n <= 10:
        return 0.7
    return 0.85


def effective_confidence(item: Mapping[str, Any], now: Optional[float] = None) -> float:
    """Decayed confidence at time `now` (default: right now).

    `-0.02` per week since `last_observed` (falling back to `updated`, then
    `created`), floored at 0.1 and ceilinged at 0.95. Pure function — never
    mutates `item` or touches disk.
    """
    now = now if now is not None else time.time()
    base = _to_float(item.get("confidence"), 0.3)
    last = item.get("last_observed")
    if last is None:
        last = item.get("updated")
    if last is None:
        last = item.get("created")
    last = _to_float(last, now)
    weeks = max(0.0, (now - last) / _WEEK_SECONDS)
    value = base - weeks * DECAY_PER_WEEK
    return max(CONFIDENCE_FLOOR, min(CONFIDENCE_CEILING, value))


# ── identity: id, project key, project name ────────────────────────────────

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _base_slug(trigger: str, action: str) -> str:
    from services.memory.skill_format import slugify
    base = slugify(f"{trigger} {action}", fallback="instinct")[:40]
    digest = hashlib.sha1(f"{_norm(trigger)}|{_norm(action)}".encode("utf-8")).hexdigest()[:10]
    return f"{base}-{digest}"


def _project_tag(project: str) -> str:
    from services.memory.skill_format import slugify
    p = (project or "").strip()
    if not p:
        return "unscoped"
    return slugify(p, fallback="project")[:24]


def instinct_id(trigger: str, action: str, scope: str = "project", project: str = "") -> str:
    """Deterministic storage key for one (trigger, action, scope, project)
    combination. Stable: the same inputs always produce the same id, so
    `upsert` calling this twice for the same instinct always merges."""
    base = _base_slug(trigger, action)
    if (scope or "project") == "global":
        return f"global-{base}"
    return f"proj-{_project_tag(project)}-{base}"


def project_key(workspace: Optional[str], project_id: Optional[str]) -> str:
    """`(owner, project)` scoping rule mirrored from
    `src/context_engine/adapters/memory.py::MemoryEngineSource._scope`: the
    workspace PATH wins when present, `project_id` is only a fallback for a
    request that has no workspace, and both missing means "" (global/
    unscoped) — never `None`."""
    return str(workspace or project_id or "").strip()


def detect_project_name(workspace: Optional[str]) -> str:
    """Cheap, offline project name for a workspace: the git remote's repo
    name when a `.git/config` is readable locally (no subprocess, no
    network — just a file read), else the folder's own basename."""
    ws = str(workspace or "").strip()
    if not ws:
        return ""
    basename = os.path.basename(ws.rstrip("/\\")) or ws
    try:
        cfg_path = os.path.join(ws, ".git", "config")
        if os.path.isfile(cfg_path):
            with open(cfg_path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
            m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', text, re.S)
            if m:
                url = re.sub(r"\.git$", "", m.group(1).strip())
                candidate = url.rstrip("/").rsplit("/", 1)[-1]
                if candidate:
                    return candidate
    except OSError:
        pass
    return basename


# ── owner-scoped store (JSON under DATA_DIR/instincts/<owner-safe>/) ───────

def _owner_safe(owner: Optional[str]) -> str:
    from services.memory.skill_format import slugify
    return slugify(owner or "", fallback="_unowned")


def _owner_dir(owner: Optional[str]) -> str:
    return os.path.join(DATA_DIR, "instincts", _owner_safe(owner))


def _store_path(owner: Optional[str]) -> str:
    return os.path.join(_owner_dir(owner), "instincts.json")


def _load_raw(owner: Optional[str]) -> Dict[str, Dict[str, Any]]:
    try:
        with open(_store_path(owner), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_raw(owner: Optional[str], data: Mapping[str, Dict[str, Any]]) -> None:
    path = _store_path(owner)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    from core.atomic_io import atomic_write_json
    atomic_write_json(path, dict(data), indent=2)


def _load(owner: Optional[str], *, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """Load the store, lazily retiring any active record whose decayed
    (`effective_confidence`) value has fallen to/under `RETIRE_THRESHOLD`,
    and persisting that retirement so it is stable across reads."""
    now = now if now is not None else time.time()
    data = _load_raw(owner)
    changed = False
    for item in data.values():
        if not isinstance(item, dict):
            continue
        if item.get("status") == "active" and effective_confidence(item, now) <= RETIRE_THRESHOLD:
            item["status"] = "retired"
            item["updated"] = now
            changed = True
    if changed:
        _save_raw(owner, data)
    return data


def _clean_domain(value: Any) -> str:
    d = str(value or "").strip().lower()
    return d if d in DOMAINS else "other"


def _clean_scope(value: Any) -> str:
    s = str(value or "project").strip().lower()
    return s if s in SCOPES else "project"


def _new_record(iid: str, *, trigger: str, action: str, domain: str, scope: str,
                project: str, project_name: str, source: str, confidence: float,
                evidence: Sequence[Mapping[str, Any]], now: float) -> Dict[str, Any]:
    return {
        "id": iid,
        "trigger": trigger,
        "action": action,
        "confidence": float(confidence),
        "domain": domain,
        "scope": scope,
        "project": project or "",
        "project_name": project_name or "",
        "source": source,
        "evidence": [dict(e) for e in (evidence or [])][-EVIDENCE_CAP:],
        "observations": 1,
        "confirmations": 0,
        "contradictions": 0,
        "created": now,
        "updated": now,
        "last_observed": now,
        "status": "active",
        "promoted_from": [],
    }


def _evidence_dict(evidence: Any) -> Optional[Dict[str, Any]]:
    """Accept either a mapping (stored as-is) or a plain string (wrapped as
    a short note) — the tool handler and the extraction pipeline both pass
    evidence through here rather than assuming callers always hand a dict."""
    if evidence is None:
        return None
    if isinstance(evidence, Mapping):
        return dict(evidence)
    text = str(evidence).strip()
    return {"note": text[:200]} if text else None


def _with_effective(record: Mapping[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    now = now if now is not None else time.time()
    out = dict(record)
    out["effective_confidence"] = effective_confidence(record, now)
    return out


# ── token overlap (relevance boost) — reuse skills.py's helper if importable ──

def _token_overlap(a: str, b: str) -> float:
    try:
        from services.memory.skills import _jaccard as _jac, _tokenize as _tok
    except Exception:  # noqa: BLE001
        def _tok(text: str) -> set:
            return {w.strip('.,!?";:()[]') for w in (text or "").lower().split() if len(w) > 1}

        def _jac(x: set, y: set) -> float:
            if not x or not y:
                return 0.0
            return len(x & y) / len(x | y)
    return _jac(_tok(a), _tok(b))


# ── stop-word-filtered keywords (evolve's clustering) ───────────────────────

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
    "when", "is", "are", "this", "that", "it", "was", "be", "as", "at",
    "by", "from", "always", "never", "should", "use", "using", "do", "does",
    "de", "la", "el", "en", "y", "o", "para", "con", "que", "es", "un",
    "una", "los", "las", "al", "del", "cuando", "siempre", "nunca", "se",
})


def _keyword_set(text: str) -> set:
    try:
        from services.memory.skills import _tokenize as _tok
        raw = _tok(text)
    except Exception:  # noqa: BLE001
        raw = {w.strip('.,!?";:()[]') for w in (text or "").lower().split() if len(w) > 1}
    return {t for t in raw if t not in _STOPWORDS and len(t) > 2}


# ── CRUD ─────────────────────────────────────────────────────────────────

def upsert(owner: Optional[str], item: Mapping[str, Any]) -> Dict[str, Any]:
    """Create or merge one instinct observation.

    Merge is by `instinct_id(trigger, action, scope, project)`: a second
    upsert for the same (trigger, action, scope, project) bumps
    `observations`, raises `confidence` to at least the observation-count
    bucket (never lowers it — a confirmed instinct does not regress just
    because it was observed again), extends `evidence` (capped at
    `EVIDENCE_CAP`, newest kept), and revives a retired record to active.
    """
    trigger = str(item.get("trigger") or "").strip()
    action = str(item.get("action") or "").strip()
    if not trigger or not action:
        raise ValueError("upsert requires both 'trigger' and 'action'")
    domain = _clean_domain(item.get("domain"))
    scope = _clean_scope(item.get("scope"))
    project = str(item.get("project") or "").strip()
    project_name = str(item.get("project_name") or "").strip()
    source = str(item.get("source") or "session-observation").strip() or "session-observation"
    new_evidence = list(item.get("evidence") or [])
    confidence_hint = item.get("confidence")

    now = time.time()
    iid = instinct_id(trigger, action, scope, project)
    data = _load(owner, now=now)
    existing = data.get(iid)

    if existing is None:
        conf = _to_float(confidence_hint, None) if confidence_hint is not None else None
        if conf is None:
            conf = _initial_confidence(1)
        record = _new_record(
            iid, trigger=trigger, action=action, domain=domain, scope=scope,
            project=project, project_name=project_name, source=source,
            confidence=conf, evidence=new_evidence, now=now,
        )
    else:
        record = dict(existing)
        record["trigger"] = trigger
        record["action"] = action
        record["domain"] = domain
        record["scope"] = scope
        if project:
            record["project"] = project
        if project_name:
            record["project_name"] = project_name
        observations = int(record.get("observations") or 0) + 1
        record["observations"] = observations
        bucket = _initial_confidence(observations)
        record["confidence"] = max(_to_float(record.get("confidence"), 0.3), bucket)
        record["evidence"] = (list(record.get("evidence") or []) + new_evidence)[-EVIDENCE_CAP:]
        record["updated"] = now
        record["last_observed"] = now
        record["status"] = "active"

    data[iid] = record
    _save_raw(owner, data)
    return _with_effective(record, now)


def add(owner: Optional[str], *, trigger: str, action: str, domain: str = "other",
        scope: str = "project", project: str = "", project_name: str = "",
        confidence: float = 0.6, source: str = "manual") -> Dict[str, Any]:
    """Manual add: same merge path as `upsert`, defaulting to
    `source="manual"` and `confidence=0.6` per the manage_instincts `add`
    action's contract."""
    return upsert(owner, {
        "trigger": trigger, "action": action, "domain": domain, "scope": scope,
        "project": project, "project_name": project_name, "source": source,
        "confidence": confidence, "evidence": [],
    })


def get(owner: Optional[str], id: str) -> Optional[Dict[str, Any]]:
    data = _load(owner)
    rec = data.get(id)
    return None if rec is None else _with_effective(rec)


def list_instincts(owner: Optional[str], *, project: Optional[str] = None,
                    include_global: bool = True, min_confidence: float = 0.0) -> List[Dict[str, Any]]:
    """Active instincts for `owner`, best (decayed) confidence first.

    `project=None` returns every active instinct regardless of which
    project it is scoped to (still honouring `include_global`); pass a
    project key to scope to that project's own records plus (optionally)
    every `scope="global"` record.
    """
    now = time.time()
    data = _load(owner, now=now)
    out: List[Dict[str, Any]] = []
    for rec in data.values():
        if not isinstance(rec, dict) or rec.get("status") != "active":
            continue
        scope = rec.get("scope")
        if scope == "global":
            if not include_global:
                continue
        elif project is not None and rec.get("project") != project:
            continue
        eff = effective_confidence(rec, now)
        if eff < min_confidence:
            continue
        item = dict(rec)
        item["effective_confidence"] = eff
        out.append(item)
    out.sort(key=lambda r: r["effective_confidence"], reverse=True)
    return out


def confirm(owner: Optional[str], id: str, evidence: Optional[Any] = None) -> Dict[str, Any]:
    now = time.time()
    data = _load(owner, now=now)
    rec = data.get(id)
    if rec is None:
        raise KeyError(f"no instinct {id!r}")
    rec["confirmations"] = int(rec.get("confirmations") or 0) + 1
    rec["confidence"] = min(CONFIDENCE_CEILING, _to_float(rec.get("confidence"), 0.3) + CONFIRM_BUMP)
    rec["updated"] = now
    rec["last_observed"] = now
    rec["status"] = "active"
    ev = _evidence_dict(evidence)
    if ev:
        rec["evidence"] = (list(rec.get("evidence") or []) + [ev])[-EVIDENCE_CAP:]
    data[id] = rec
    _save_raw(owner, data)
    return _with_effective(rec, now)


def contradict(owner: Optional[str], id: str, evidence: Optional[Any] = None) -> Dict[str, Any]:
    now = time.time()
    data = _load(owner, now=now)
    rec = data.get(id)
    if rec is None:
        raise KeyError(f"no instinct {id!r}")
    rec["contradictions"] = int(rec.get("contradictions") or 0) + 1
    rec["confidence"] = max(CONFIDENCE_FLOOR, _to_float(rec.get("confidence"), 0.3) - CONTRADICT_PENALTY)
    rec["updated"] = now
    rec["last_observed"] = now
    ev = _evidence_dict(evidence)
    if ev:
        rec["evidence"] = (list(rec.get("evidence") or []) + [ev])[-EVIDENCE_CAP:]
    if effective_confidence(rec, now) <= RETIRE_THRESHOLD:
        rec["status"] = "retired"
    data[id] = rec
    _save_raw(owner, data)
    return _with_effective(rec, now)


def retire(owner: Optional[str], id: str) -> Dict[str, Any]:
    data = _load_raw(owner)
    rec = data.get(id)
    if rec is None:
        raise KeyError(f"no instinct {id!r}")
    rec["status"] = "retired"
    rec["updated"] = time.time()
    data[id] = rec
    _save_raw(owner, data)
    return dict(rec)


def delete(owner: Optional[str], id: str) -> bool:
    data = _load_raw(owner)
    if id not in data:
        return False
    del data[id]
    _save_raw(owner, data)
    return True


# ── promotion: project → global once seen widely enough ────────────────────

def _promotion_settings() -> tuple:
    try:
        from src.settings import get_setting
        min_projects = int(get_setting("instincts_promote_min_projects", 2))
    except Exception:  # noqa: BLE001
        min_projects = 2
    try:
        from src.settings import get_setting
        min_confidence = float(get_setting("instincts_promote_min_confidence", 0.8))
    except Exception:  # noqa: BLE001
        min_confidence = 0.8
    return min_projects, min_confidence


def promote(owner: Optional[str], id: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Merge project-scoped instincts that share the same (trigger, action)
    identity across >= `instincts_promote_min_projects` distinct projects,
    with mean effective confidence >= `instincts_promote_min_confidence`,
    into ONE `scope="global"` record. The merged project-scoped copies are
    retired (kept on disk, never deleted) with `promoted_from` recording
    the projects that fed the merge.

    `dry_run=True` never mutates the store; it returns the candidates that
    WOULD promote. `id` (a specific project-scoped record's storage key)
    restricts a real promotion to that one candidate's group.
    """
    min_projects, min_confidence = _promotion_settings()
    now = time.time()
    data = _load(owner, now=now)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for key, rec in data.items():
        if not isinstance(rec, dict) or rec.get("status") != "active" or rec.get("scope") != "project":
            continue
        base = _base_slug(rec.get("trigger", ""), rec.get("action", ""))
        groups.setdefault(base, []).append({**rec, "_key": key})

    candidates = []
    for base, members in groups.items():
        projects = sorted({m.get("project") for m in members if m.get("project")})
        if len(projects) < min_projects:
            continue
        mean_conf = sum(effective_confidence(m, now) for m in members) / len(members)
        if mean_conf < min_confidence:
            continue
        candidates.append({"base": base, "members": members, "projects": projects, "mean_confidence": mean_conf})

    if dry_run:
        return {
            "dry_run": True,
            "candidates": [
                {
                    "base": c["base"], "projects": c["projects"],
                    "mean_confidence": round(c["mean_confidence"], 4),
                    "keys": [m["_key"] for m in c["members"]],
                }
                for c in candidates
            ],
        }

    if id is not None:
        candidates = [c for c in candidates if id in {m["_key"] for m in c["members"]}]

    promoted_ids = []
    for c in candidates:
        members = c["members"]
        best = max(members, key=lambda m: effective_confidence(m, now))
        global_key = instinct_id(best["trigger"], best["action"], "global", "")
        evidence: List[Dict[str, Any]] = []
        for m in members:
            evidence.extend(m.get("evidence") or [])
        global_record = {
            "id": global_key,
            "trigger": best["trigger"],
            "action": best["action"],
            "confidence": min(CONFIDENCE_CEILING, c["mean_confidence"]),
            "domain": best.get("domain", "other"),
            "scope": "global",
            "project": "",
            "project_name": "",
            "source": "promoted",
            "evidence": evidence[-EVIDENCE_CAP:],
            "observations": sum(int(m.get("observations") or 0) for m in members),
            "confirmations": sum(int(m.get("confirmations") or 0) for m in members),
            "contradictions": sum(int(m.get("contradictions") or 0) for m in members),
            "created": min((m.get("created") or now) for m in members),
            "updated": now,
            "last_observed": now,
            "status": "active",
            "promoted_from": c["projects"],
        }
        # An existing global record for this same idea absorbs the new merge
        # rather than being overwritten blind — keep its own accumulated
        # counters and just widen `promoted_from`.
        prior = data.get(global_key)
        if isinstance(prior, dict):
            global_record["observations"] += int(prior.get("observations") or 0)
            global_record["confirmations"] += int(prior.get("confirmations") or 0)
            global_record["contradictions"] += int(prior.get("contradictions") or 0)
            global_record["created"] = min(global_record["created"], prior.get("created") or now)
            global_record["evidence"] = (list(prior.get("evidence") or []) + global_record["evidence"])[-EVIDENCE_CAP:]
            global_record["promoted_from"] = sorted(set(global_record["promoted_from"]) | set(prior.get("promoted_from") or []))

        data[global_key] = global_record
        for m in members:
            k = m["_key"]
            data[k]["status"] = "retired"
            data[k]["updated"] = now
        promoted_ids.append(global_key)

    _save_raw(owner, data)
    return {"dry_run": False, "promoted": promoted_ids}


# ── evolve: cluster instincts into a draft skill/command/agent suggestion ──

def evolve(owner: Optional[str], project: Optional[str] = None, *, min_cluster: int = 2,
           generate: bool = False) -> Dict[str, Any]:
    """Cluster active instincts by trigger-keyword overlap (stop-word
    filtered, overlap coefficient >= 0.5 with >= 2 shared keywords). Each
    cluster of size >= `min_cluster` is returned with a `suggested` kind:
    `"agent"` at size >= 5, `"command"` when the majority domain is
    `"workflow"` and mean confidence >= 0.7, else `"skill"`.

    With `generate=True`, each cluster is also written as a DRAFT skill
    (`services.memory.skills.SkillsManager.add_skill`, `source="learned"`,
    `status="draft"`, `category="instincts"`) — never published.
    """
    now = time.time()
    if project is not None:
        items = list_instincts(owner, project=project, include_global=True, min_confidence=0.0)
    else:
        items = [
            _with_effective(v, now) for v in _load(owner, now=now).values()
            if isinstance(v, dict) and v.get("status") == "active"
        ]

    by_id = {it["id"]: it for it in items}
    kw = {iid: _keyword_set(it.get("trigger", "")) for iid, it in by_id.items()}
    ids = list(by_id.keys())

    parent = {i: i for i in ids}

    def _find(x: str) -> str:
        while parent[x] != x:
            x = parent[x]
        return x

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(ids)):
        a_set = kw[ids[i]]
        if not a_set:
            continue
        for j in range(i + 1, len(ids)):
            b_set = kw[ids[j]]
            if not b_set:
                continue
            shared = a_set & b_set
            if len(shared) < 2:
                continue
            overlap = len(shared) / min(len(a_set), len(b_set))
            if overlap >= 0.5:
                _union(ids[i], ids[j])

    groups: Dict[str, List[str]] = {}
    for i in ids:
        groups.setdefault(_find(i), []).append(i)

    clusters = []
    for members in groups.values():
        if len(members) < min_cluster:
            continue
        member_items = [by_id[m] for m in members]
        keywords: set = set()
        for m in members:
            keywords |= kw[m]
        mean_conf = sum(mi.get("effective_confidence", mi.get("confidence", 0.0)) for mi in member_items) / len(member_items)
        domain_counts = Counter(mi.get("domain", "other") for mi in member_items)
        majority_domain = domain_counts.most_common(1)[0][0] if domain_counts else "other"
        if len(member_items) >= 5:
            suggested = "agent"
        elif majority_domain == "workflow" and mean_conf >= 0.7:
            suggested = "command"
        else:
            suggested = "skill"
        best = max(member_items, key=lambda mi: mi.get("effective_confidence", 0.0))
        clusters.append({
            "keywords": sorted(keywords),
            "instinct_ids": list(members),
            "suggested": suggested,
            "title": (best.get("trigger") or "")[:80],
            "mean_confidence": round(mean_conf, 4),
            "domain": majority_domain,
        })
    clusters.sort(key=lambda c: c["mean_confidence"], reverse=True)

    created_skill_ids: List[str] = []
    if generate and clusters:
        from services.memory.skills import SkillsManager
        sm = SkillsManager(DATA_DIR)
        for c in clusters:
            member_items = [by_id[m] for m in c["instinct_ids"]]
            triggers = [mi.get("trigger", "") for mi in member_items if mi.get("trigger")]
            actions = list(dict.fromkeys(mi.get("action", "") for mi in member_items if mi.get("action")))
            entry = sm.add_skill(
                description=c["title"] or "Learned pattern",
                category="instincts",
                when_to_use="; ".join(triggers),
                procedure=actions,
                tags=list(c["keywords"])[:5],
                status="draft",
                confidence=c["mean_confidence"],
                source="learned",
                owner=owner,
            )
            created_skill_ids.append(entry.get("name") or entry.get("id") or "")

    return {"clusters": clusters, "created_skill_ids": created_skill_ids}


# ── injection: the prompt block ─────────────────────────────────────────────

def _inject_settings(threshold: Optional[float], limit: Optional[int]) -> tuple:
    try:
        from src.settings import get_setting
        thr = float(threshold) if threshold is not None else float(get_setting("instincts_inject_threshold", 0.7))
    except Exception:  # noqa: BLE001
        thr = float(threshold) if threshold is not None else 0.7
    try:
        from src.settings import get_setting
        lim = int(limit) if limit is not None else int(get_setting("instincts_inject_max", 6))
    except Exception:  # noqa: BLE001
        lim = int(limit) if limit is not None else 6
    return thr, lim


def render_block(owner: Optional[str], project: Optional[str], *, threshold: Optional[float] = None,
                  limit: Optional[int] = None, user_message: str = "") -> str:
    """`"Learned instincts (confidence):\\n- [project 85%] when …: do …"`, or
    `""` when nothing qualifies. Project-scoped instincts for `project` plus
    every global one, filtered to `effective_confidence >= threshold`
    (`instincts_inject_threshold` setting, default 0.7), capped at `limit`
    (`instincts_inject_max`, default 6). Ties are broken by a small keyword-
    overlap boost against `user_message`, then project wins over global.
    """
    thr, lim = _inject_settings(threshold, limit)
    items = list_instincts(owner, project=project, include_global=True, min_confidence=thr)
    if not items:
        return ""

    def _score(it: Dict[str, Any]) -> float:
        base = it["effective_confidence"]
        boost = 0.0
        if user_message:
            boost = _token_overlap(user_message, it.get("trigger", "")) * 0.1
        return round(base + boost, 4)

    items = sorted(items, key=lambda it: (-_score(it), 0 if it.get("scope") == "project" else 1))
    items = items[:max(0, lim)]
    if not items:
        return ""

    lines = ["Learned instincts (confidence):"]
    for it in items:
        pct = int(round(it["effective_confidence"] * 100))
        label = "project" if it.get("scope") == "project" else "global"
        lines.append(f"- [{label} {pct}%] {it.get('trigger', '')}: {it.get('action', '')}")
    return "\n".join(lines)


# ── extraction (background, after a turn) ──────────────────────────────────

def looks_like_correction(text: str) -> bool:
    """Negative reaction ⇒ correction, via
    `src.skills_runtime.sleep_optimize.classify_reaction`'s en/es phrase
    tables — reused rather than a second, drifting phrase list."""
    try:
        from src.skills_runtime.sleep_optimize import classify_reaction
    except Exception:  # noqa: BLE001
        return False
    return classify_reaction(text) == "negative"


def should_extract(round_count: int, tool_count: int, user_message: str) -> bool:
    try:
        rc = int(round_count or 0)
    except (TypeError, ValueError):
        rc = 0
    try:
        tc = int(tool_count or 0)
    except (TypeError, ValueError):
        tc = 0
    return rc >= 2 or tc >= 2 or looks_like_correction(user_message)


_INSTINCT_BLOCK_RE = re.compile(r"<instinct>\s*(.*?)\s*</instinct>", re.S | re.I)
_INSTINCT_FIELD_RE = re.compile(r"^\s*(trigger|action|domain|evidence|contradicts)\s*:\s*(.*)$", re.I)


def _parse_instinct_blocks(text: str) -> List[Dict[str, str]]:
    out = []
    for m in _INSTINCT_BLOCK_RE.finditer(text or ""):
        fields: Dict[str, str] = {}
        current_key: Optional[str] = None
        for line in m.group(1).splitlines():
            fm = _INSTINCT_FIELD_RE.match(line)
            if fm:
                current_key = fm.group(1).lower()
                fields[current_key] = fm.group(2).strip().strip('"')
            elif current_key and line.strip():
                fields[current_key] = (fields[current_key] + " " + line.strip()).strip()
        if fields:
            out.append(fields)
    return out


def _valid_trigger(trigger: str) -> bool:
    t = (trigger or "").strip()
    if not t or len(t) > 140:
        return False
    low = t.lower()
    return low.startswith("when") or low.startswith("cuando")


def _build_extract_prompt(conversation_text: str, existing: Sequence[Mapping[str, str]]) -> str:
    lines = [
        "You are studying an AI agent session to spot small, reusable behaviours "
        "it should remember for next time in THIS project.",
        "Propose 0 to 3 'instincts': a TRIGGER (a recurring situation) and an "
        "ACTION (what to do in that situation), grounded ONLY in what actually "
        "happened in the conversation below — never invented.",
        "Rules:",
        "- trigger MUST start with 'when' (English) or 'cuando' (Spanish), max 140 characters.",
        "- action is a concrete instruction, max 240 characters.",
        "- domain is exactly one of: code-style, workflow, testing, tooling, communication, debugging, other.",
        "- evidence MUST be a short quote copied verbatim from the conversation below.",
        "- contradicts: the id of one existing instinct below that this new one "
        "contradicts, or the bare word none.",
        "- Return NOTHING (zero <instinct> blocks) when there is no genuinely "
        "reusable pattern this turn.",
        "",
        "=== EXISTING INSTINCTS FOR THIS PROJECT (id: trigger) ===",
    ]
    if existing:
        for it in existing:
            lines.append(f"- {it.get('id', '')}: {it.get('trigger', '')}")
    else:
        lines.append("(none yet)")
    lines += [
        "",
        "=== CONVERSATION ===",
        conversation_text[:8000],
        "",
        "Respond with zero to three blocks EXACTLY in this tagged format (plain "
        "text, no JSON, no code fences):",
        "<instinct>",
        "trigger: when ...",
        "action: ...",
        "domain: workflow",
        'evidence: "<short quote from the conversation>"',
        "contradicts: <existing id or none>",
        "</instinct>",
    ]
    return "\n".join(lines)


def _conversation_text(messages: Sequence[Mapping[str, Any]]) -> str:
    lines = []
    for m in messages or []:
        role = m.get("role", "?") if isinstance(m, Mapping) else "?"
        content = m.get("content", "") if isinstance(m, Mapping) else ""
        if not isinstance(content, str):
            try:
                content = json.dumps(content, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                content = str(content)
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


async def extract_from_turn(owner: Optional[str], session_id: Optional[str],
                             messages: List[Dict[str, Any]], *, project: str,
                             project_name: str, workspace: str) -> List[Dict[str, Any]]:
    """One background model call proposing 0-3 instincts from `messages`,
    validated and stored. Never raises — any failure (settings, no
    endpoint, model call, unparseable reply) simply means an empty or
    partial result, exactly like `services.memory.skill_extractor`'s own
    defensive contract."""
    if not owner:
        return []
    try:
        from src.settings import get_setting
        if not get_setting("instincts_enabled", True) or not get_setting("instincts_extract_enabled", True):
            return []
    except Exception:  # noqa: BLE001
        return []
    try:
        from src.settings import get_setting
        max_per_turn = max(0, int(get_setting("instincts_extract_max_per_turn", 3)))
    except Exception:  # noqa: BLE001
        max_per_turn = 3

    conversation_text = _conversation_text(messages)
    if not conversation_text.strip():
        return []

    try:
        existing_ctx = [
            {"id": it["id"], "trigger": it.get("trigger", "")}
            for it in list_instincts(owner, project=project, include_global=True, min_confidence=0.0)[:20]
        ]
    except Exception:  # noqa: BLE001
        existing_ctx = []

    prompt = _build_extract_prompt(conversation_text, existing_ctx)

    try:
        from src.endpoint_resolver import resolve_endpoint
        url, model, headers = resolve_endpoint("instincts_extract", owner=owner)
    except Exception:  # noqa: BLE001
        return []
    if not url or not model:
        return []

    try:
        from src.llm_core import llm_call_async
        raw = await llm_call_async(
            url=url, model=model, messages=[{"role": "user", "content": prompt}],
            headers=headers, temperature=0.2, max_tokens=1200, timeout=45,
            max_retries=1, workload="background",
        )
    except Exception:  # noqa: BLE001
        logger.debug("[instincts] extraction model call failed", exc_info=True)
        return []
    if isinstance(raw, tuple):
        raw = raw[0]
    if not isinstance(raw, str) or not raw.strip():
        return []

    normalized_conversation = _norm(conversation_text)
    stored: List[Dict[str, Any]] = []
    for block in _parse_instinct_blocks(raw)[:max_per_turn]:
        trigger = str(block.get("trigger") or "").strip()
        action = str(block.get("action") or "").strip()
        domain = str(block.get("domain") or "").strip().lower()
        evidence_text = str(block.get("evidence") or "").strip()
        contradicts = str(block.get("contradicts") or "").strip()

        if not _valid_trigger(trigger):
            continue
        if not action or len(action) > 240:
            continue
        if domain not in DOMAINS:
            continue
        if not evidence_text or _norm(evidence_text) not in normalized_conversation:
            # Grounding: no invented evidence, ever.
            continue

        if contradicts and contradicts.lower() != "none":
            try:
                contradict(owner, contradicts, evidence={
                    "session_id": session_id or "", "turn_ts": time.time(),
                    "excerpt": evidence_text[:200],
                })
            except Exception:  # noqa: BLE001
                pass

        try:
            record = upsert(owner, {
                "trigger": trigger, "action": action, "domain": domain,
                "scope": "project", "project": project or "", "project_name": project_name or "",
                "source": "session-observation",
                "evidence": [{
                    "session_id": session_id or "", "turn_ts": time.time(),
                    "excerpt": evidence_text[:200],
                }],
            })
        except Exception:  # noqa: BLE001
            continue
        stored.append(record)

    return stored[:max_per_turn]


# ── export / import ────────────────────────────────────────────────────────

def export_json(owner: Optional[str]) -> str:
    data = _load_raw(owner)
    return json.dumps(list(data.values()), indent=2, ensure_ascii=False)


def import_json(owner: Optional[str], text: str, *, scope_override: Optional[str] = None) -> Dict[str, Any]:
    """Validated, dedup-by-id import of `export_json`'s own shape (a JSON
    array of instinct records). Rows that are not a dict, or lack a
    `trigger`/`action`, or whose id already exists are skipped rather than
    raising — a malformed single row must not sink the whole import."""
    try:
        rows = json.loads(text)
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid JSON: {e}") from e
    if not isinstance(rows, list):
        raise ValueError("expected a JSON array of instinct records")

    data = _load_raw(owner)
    now = time.time()
    imported: List[str] = []
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        trigger = str(row.get("trigger") or "").strip()
        action = str(row.get("action") or "").strip()
        if not trigger or not action:
            skipped += 1
            continue
        domain = _clean_domain(row.get("domain"))
        scope = _clean_scope(scope_override if scope_override is not None else row.get("scope"))
        project = str(row.get("project") or "").strip()
        iid = str(row.get("id") or "").strip() or instinct_id(trigger, action, scope, project)
        if iid in data:
            skipped += 1
            continue
        record = _new_record(
            iid, trigger=trigger, action=action, domain=domain, scope=scope,
            project=project, project_name=str(row.get("project_name") or ""),
            source=str(row.get("source") or "import").strip() or "import",
            confidence=_to_float(row.get("confidence"), 0.3),
            evidence=row.get("evidence") or [], now=now,
        )
        for key in ("observations", "confirmations", "contradictions"):
            if isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool):
                record[key] = int(row[key])
        if row.get("status") in STATUSES:
            record["status"] = row["status"]
        if isinstance(row.get("promoted_from"), list):
            record["promoted_from"] = [str(x) for x in row["promoted_from"]]
        data[iid] = record
        imported.append(iid)

    _save_raw(owner, data)
    return {"imported": imported, "skipped": skipped, "count": len(imported)}


# ── status ──────────────────────────────────────────────────────────────

def status(owner: Optional[str], project: Optional[str] = None) -> Dict[str, Any]:
    now = time.time()
    data = _load(owner, now=now)
    active = [v for v in data.values() if isinstance(v, dict) and v.get("status") == "active"]
    by_scope: Dict[str, int] = {}
    by_domain: Dict[str, int] = {}
    for v in active:
        by_scope[v.get("scope", "project")] = by_scope.get(v.get("scope", "project"), 0) + 1
        by_domain[v.get("domain", "other")] = by_domain.get(v.get("domain", "other"), 0) + 1
    top = list_instincts(owner, project=project, include_global=True, min_confidence=0.0)[:5]
    pending = promote(owner, dry_run=True)
    return {
        "total": len(active),
        "by_scope": by_scope,
        "by_domain": by_domain,
        "top": top,
        "pending_promotions": pending.get("candidates", []),
    }
