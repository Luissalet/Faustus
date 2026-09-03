"""The provenance graph — a 2D AUDIT view over DECLARED edges only (FAUSTUS).

Why this shape, and not a 3D knowledge nebula
---------------------------------------------
The source ecosystem this feature was mined from settles the question with its
own numbers. ``eidetic_engine_cli`` ships a full graph stack — PageRank, HITS,
Gomory-Hu — and then weights it **0.10** against 0.45 BM25 + 0.45 semantic in
its own retrieval. The one place a graph genuinely rules, ``beads_viewer``, is
a DAG of **declared** dependencies: edges a human wrote down. A 3D hairball
past two hundred nodes is illegible and improves no query.

So this module is not a knowledge graph. It is an audit view that answers three
questions about the memory and the workspace:

1. **"Why does the agent believe this?"** — :func:`explain` follows a memory
   item back to the chat, the file and the line its evidence span names.
2. **"What is floating, and what is said twice?"** — :func:`orphans`, plus
   ``duplicate_of`` edges from :mod:`src.text_overlap`, which detects
   near-duplicates POSITIONALLY and verifies every span with an exact
   substring compare before it is reported.
3. **"What breaks if I touch this?"** — :func:`impact`, the set reachable by
   reversed ``depends_on`` / ``changed`` edges.

The one discipline that matters
-------------------------------
**NEVER create an edge a model asserted.** Every edge in this graph traces to
something already stored as declared truth:

===================  ==================================================================
edge kind            what it is read from
===================  ==================================================================
``depends_on``       a dependency edge record in ``objectives.jsonl`` — a human or the
                     delta compiler wrote it (services/objectives.py)
``evidence_of``      an evidence span stored on a memory item, or an evidence/delta
                     record in ``objectives_log.jsonl`` (src/memory_engine.py)
``contradicts``      a memory item's stored ``inverted_from``: the Curator inverted
                     that rule after ≥3 harmful events (src/memory_curator.py)
``changed``          a dispatch job's checkpoint diff — what Faustus SAW change on
                     disk, not what a worker claimed (src/dispatch.py mirrors)
``cites``            a stored evidence ref that resolves to a chunk in an expert's
                     own index, with the page that index recorded
                     (services/experts.py — a page is copied, never invented)
``contains``         a corpus file is listed in that expert's chunk index
``duplicate_of``     :func:`src.text_overlap.find_duplicates` — a LITERALLY verified
                     shared span, carrying its measured ratio as the confidence
===================  ==================================================================

Which way an edge points
------------------------
Uniformly: **``X --kind--> Y`` means "Y is the stored record that accounts for
X"**. Follow the arrow and you walk TOWARDS the proof — a memory to the chat
session and the file:line its evidence span names, an objective to the job
whose diff was recorded against it, a checkpoint to the session it was
dispatched from. ``depends_on`` reads the same way (``OBJ-3 --depends_on-->
OBJ-1``: OBJ-1 is what OBJ-3 rests on), which is what makes :func:`impact`
a plain reversed traversal.

Every edge carries ``trust``. Today the only value in the graph is
``"declared"``. The hook for a future model-inferred source is
:data:`TRUST_INFERRED`: such an edge must arrive with a trust below the
declared ones and be filterable out with :func:`filter_trust`. **Nothing
inferred is built here** — the hook exists so that adding one later is a
deliberate, visible act rather than a quiet mixing of asserted edges into an
audit view.

Everything is optional
----------------------
No project, no objectives file, no memory database, no dispatch mirrors, no
experts: each missing source yields a SMALLER graph and a ``sources`` entry
saying so. Nothing here raises — ``build`` is called behind an HTTP read and
may end up behind a hot path, and a broken source must cost a section of the
graph, never the request.

Deterministic: nodes sort by ``(kind, id)``, edges by ``(kind, from, to)``, and
:func:`build` is pure given its inputs — the clock is injected (``now=``) and
the node budget cuts at a fixed, deterministic point.

Pure stdlib.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from src import text_overlap

logger = logging.getLogger(__name__)

try:  # pragma: no cover - constants always import in the app
    from src.constants import DATA_DIR as _DEFAULT_DATA_DIR
except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
    _DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# Module-level so tests can point the mirrors somewhere disposable.
DATA_DIR = _DEFAULT_DATA_DIR
DISPATCH_DIRNAME = "dispatch"

NODE_KINDS: Tuple[str, ...] = (
    "objective", "memory", "chat", "file", "checkpoint", "expert", "corpus",
)
EDGE_KINDS: Tuple[str, ...] = (
    "depends_on", "evidence_of", "contradicts", "changed", "cites", "contains",
    "duplicate_of",
)

#: The only trust an edge in this graph carries today: it was read from a
#: stored record, not asserted by a model.
TRUST_DECLARED = "declared"
#: Documented hook, deliberately unused. A future model-inferred source must
#: stamp its edges with this so :func:`filter_trust` can drop them and the UI
#: can grey them out. Nothing in this module ever emits it.
TRUST_INFERRED = "inferred"
TRUST_ORDER: Dict[str, int] = {TRUST_DECLARED: 2, TRUST_INFERRED: 1}

DEFAULT_LIMIT_NODES = 2000
MAX_LIMIT_NODES = 20_000
DEFAULT_HOPS = 2
MAX_HOPS = 6
MAX_EXPLAIN_STEPS = 60
MAX_EXPLAIN_HOPS = 3
#: The report's cap on what a graph may contribute to a retrieval score.
RANKING_CAP = 0.10

MAX_OBJECTIVE_LOG = 400        # audit records read per project
MAX_DISPATCH_MIRRORS = 60      # newest job mirrors read from disk
MAX_LABEL_CHARS = 160
MAX_WHY_CHARS = 300
MAX_DUPLICATE_ITEMS = 300
DUPLICATE_THRESHOLD = 0.6

_PATH_LINE_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?::\d+)?$")
_CHUNK_ID_RE = re.compile(r"\bc[0-9a-f]{16}\b")
_EXPERT_REF_RE = re.compile(r"\bexpert:(?P<slug>[A-Za-z0-9_-]{1,80})[#:](?P<chunk>c[0-9a-f]{16})\b")
_PATHY_RE = re.compile(r"[\w.-]+\.[A-Za-z0-9]{1,8}$")


# ---------------------------------------------------------------------------
# Small helpers — none of them raise
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any, limit: int = MAX_LABEL_CHARS) -> str:
    try:
        out = " ".join(str(value if value is not None else "").split())
    except Exception:  # noqa: BLE001
        return ""
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _conf(value: Any, default: float = 1.0) -> float:
    return round(max(0.0, min(1.0, _num(value, default))), 4)


def _norm_path(path: Any, workspace: Optional[str] = None) -> str:
    """A workspace-relative, forward-slashed path key.

    An absolute path under the workspace becomes relative to it, so the file a
    memory span names and the file a checkpoint diff names land on ONE node.
    """
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if workspace:
        base = str(workspace).replace("\\", "/").rstrip("/")
        if base and raw.lower().startswith(base.lower() + "/"):
            raw = raw[len(base) + 1:]
    return raw.strip("/")


def _split_line(ref: Any) -> Tuple[str, Optional[int]]:
    """``"src/app.py:120"`` → ``("src/app.py", 120)``; otherwise (ref, None)."""
    raw = str(ref or "").strip()
    match = _PATH_LINE_RE.match(raw)
    if not match:
        return raw, None
    try:
        return match.group("path"), int(match.group("line"))
    except (TypeError, ValueError):  # pragma: no cover - the regex guarantees digits
        return raw, None


def _looks_like_path(ref: Any) -> bool:
    raw = str(ref or "").strip()
    if not raw or len(raw) > 400:
        return False
    if "/" in raw or "\\" in raw:
        return True
    return bool(_PATHY_RE.match(raw))


def _dispatch_dir() -> str:
    return os.path.join(DATA_DIR, DISPATCH_DIRNAME)


# ---------------------------------------------------------------------------
# The builder — the only thing that creates a node or an edge
# ---------------------------------------------------------------------------


class _Builder:
    """Collects nodes and edges under a hard node budget.

    Past the budget no new node is created and ``truncated`` goes true; an edge
    naming a node that was never created is dropped at assembly, so the answer
    is always internally consistent — never an edge pointing into nothing.
    """

    def __init__(self, limit_nodes: int) -> None:
        self.limit = max(1, int(limit_nodes))
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self.truncated = False
        self.sources: Dict[str, Dict[str, Any]] = {}

    # -- nodes ----------------------------------------------------------

    def node(self, kind: str, key: Any, *, label: Any = "", detail: Any = "",
             meta: Optional[Dict[str, Any]] = None) -> str:
        """Create (or enrich) one node and return its id, or "" when the node
        budget is spent."""
        key = str(key or "").strip()
        if not key or kind not in NODE_KINDS:
            return ""
        node_id = f"{kind}:{key}"
        existing = self.nodes.get(node_id)
        if existing is not None:
            if label and not existing.get("label"):
                existing["label"] = _text(label)
            if detail and not existing.get("detail"):
                existing["detail"] = _text(detail, MAX_WHY_CHARS)
            if meta:
                for name, value in meta.items():
                    existing["meta"].setdefault(name, value)
            return node_id
        if len(self.nodes) >= self.limit:
            self.truncated = True
            return ""
        self.nodes[node_id] = {
            "id": node_id,
            "kind": kind,
            "label": _text(label) or key,
            "detail": _text(detail, MAX_WHY_CHARS),
            "meta": dict(meta or {}),
        }
        return node_id

    def has(self, node_id: str) -> bool:
        return node_id in self.nodes

    # -- edges ----------------------------------------------------------

    def edge(self, source: str, target: str, kind: str, *, why: Any,
             confidence: Any = 1.0, trust: str = TRUST_DECLARED,
             meta: Optional[Dict[str, Any]] = None) -> bool:
        """One edge. Both endpoints must exist and the kind must be known; a
        repeat of the same (from, to, kind) keeps the first ``why``."""
        if not source or not target or source == target:
            return False
        if kind not in EDGE_KINDS:
            return False
        key = (source, target, kind)
        if key in self.edges:
            return False
        self.edges[key] = {
            "from": source,
            "to": target,
            "kind": kind,
            "confidence": _conf(confidence),
            "why": _text(why, MAX_WHY_CHARS),
            "trust": trust,
            **({"meta": dict(meta)} if meta else {}),
        }
        return True

    # -- sources --------------------------------------------------------

    def source(self, name: str, *, available: bool, count: int = 0, note: str = "") -> None:
        self.sources[name] = {"available": bool(available), "count": int(count),
                              "note": _text(note, MAX_WHY_CHARS)}

    # -- assembly -------------------------------------------------------

    def finish(self) -> Dict[str, Any]:
        nodes = sorted(self.nodes.values(), key=lambda n: (n["kind"], n["id"]))
        known = set(self.nodes)
        edges = [e for e in self.edges.values() if e["from"] in known and e["to"] in known]
        edges.sort(key=lambda e: (e["kind"], e["from"], e["to"]))
        return {"nodes": nodes, "edges": edges, "sources": dict(sorted(self.sources.items())),
                "truncated": bool(self.truncated)}


# ---------------------------------------------------------------------------
# Source 1 — objectives (services/objectives.py): declared dependencies
# ---------------------------------------------------------------------------


def _collect_objectives(builder: _Builder,
                        project: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Objective nodes + ``depends_on`` edges. Returns ``{OBJ-id: node id}``."""
    by_obj: Dict[str, str] = {}
    if not (project or {}).get("workspace"):
        builder.source("objectives", available=False,
                       note="no project with a bound folder was given")
        return by_obj
    try:
        from services import objectives as objectives_svc
        state = objectives_svc.load_state(project)
    except Exception as exc:  # noqa: BLE001 - an optional source
        logger.debug("provenance: objectives unavailable (%s)", exc)
        builder.source("objectives", available=False, note=f"objectives store unreadable: {exc}")
        return by_obj

    records = state.get("objectives") or {}
    # Dropped objectives are history, not plan: services/objectives.py leaves
    # them out of the prompt block and the impact scores, and so does this.
    live = {oid: rec for oid, rec in records.items()
            if isinstance(rec, dict) and rec.get("status") != "dropped"}
    for oid in sorted(live):
        record = live[oid]
        node_id = builder.node(
            "objective", oid,
            label=record.get("title"),
            detail=f"{record.get('status') or 'open'} · P{record.get('priority', 3)}",
            meta={"objective_id": oid, "status": _text(record.get("status"), 40),
                  "priority": record.get("priority"), "owner": _text(record.get("owner"), 60),
                  "updated_at": _text(record.get("updated_at"), 40),
                  "title": _text(record.get("title"))},
        )
        if node_id:
            by_obj[oid] = node_id

    deps = 0
    for edge in state.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        frm, to = by_obj.get(str(edge.get("from") or "")), by_obj.get(str(edge.get("to") or ""))
        if not frm or not to:
            continue
        if builder.edge(frm, to, "depends_on", confidence=1.0,
                        why=f"{edge.get('from')} declares a dependency on {edge.get('to')} "
                            f"in objectives.jsonl"):
            deps += 1
    builder.source("objectives", available=True, count=len(by_obj),
                   note=f"{len(by_obj)} objective(s), {deps} declared dependency edge(s)")
    return by_obj


def _collect_objective_log(builder: _Builder, project: Optional[Dict[str, Any]],
                           by_obj: Dict[str, str]) -> Set[str]:
    """Chat and checkpoint nodes named by ``objectives_log.jsonl``.

    Two record kinds are read, both written by the app itself:
    ``evidence`` (source + ref + confidence, appended by the dispatch settle
    path) and ``delta`` (which session applied an ADD/EDIT/KILL). Returns the
    dispatch job ids the log referenced, so the mirror reader knows which jobs
    this project actually touched.
    """
    jobs: Set[str] = set()
    if not by_obj or not (project or {}).get("workspace"):
        builder.source("objective_log", available=False, note="no objectives log to read")
        return jobs
    try:
        from services import objectives as objectives_svc
        records = objectives_svc.read_log(project, MAX_OBJECTIVE_LOG)
    except Exception as exc:  # noqa: BLE001
        logger.debug("provenance: objectives log unavailable (%s)", exc)
        builder.source("objective_log", available=False, note=f"log unreadable: {exc}")
        return jobs

    used = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        target = by_obj.get(str(record.get("id") or ""))
        if not target:
            continue
        kind = str(record.get("kind") or "")
        ts = _text(record.get("ts"), 40)
        if kind == "evidence":
            source_name = str(record.get("source") or "")
            ref = str(record.get("ref") or "")
            note = _text(record.get("note"), 200)
            if source_name == "dispatch" and ref:
                jobs.add(ref)
                node_id = builder.node("checkpoint", ref, label=f"dispatch job {ref}",
                                       detail=note, meta={"job_id": ref, "source": "dispatch"})
                why = (f"a dispatched job that named {record.get('id')} left this evidence "
                       f"on {ts}: {note}" if note else
                       f"a dispatched job that named {record.get('id')} left evidence on {ts}")
            elif ref:
                node_id = builder.node("chat", ref, label=f"session {ref[:12]}",
                                       detail=note, meta={"session_id": ref})
                why = (f"{source_name or 'a chat'} left evidence for {record.get('id')} "
                       f"on {ts}: {note}" if note else
                       f"{source_name or 'a chat'} left evidence for {record.get('id')} on {ts}")
            else:
                continue
            if node_id and builder.edge(target, node_id, "evidence_of",
                                        confidence=_conf(record.get("confidence"), 0.5), why=why):
                used += 1
        elif kind == "delta":
            session = str(record.get("session") or "")
            if not session:
                continue
            node_id = builder.node("chat", session, label=f"session {session[:12]}",
                                   meta={"session_id": session})
            op = _text(record.get("op"), 20) or "changed"
            rationale = _text(record.get("rationale"), 160)
            why = (f"{record.get('id')} was {op}ed from this chat session on {ts}"
                   + (f" — {rationale}" if rationale else ""))
            if node_id and builder.edge(target, node_id, "evidence_of", confidence=1.0, why=why):
                used += 1
    builder.source("objective_log", available=True, count=len(records),
                   note=f"{len(records)} audit record(s) read, {used} edge(s) drawn")
    return jobs


# ---------------------------------------------------------------------------
# Source 2 — learned memory (src/memory_engine.py): evidence spans
# ---------------------------------------------------------------------------


def _memory_label(item: Dict[str, Any]) -> str:
    return _text(item.get("text"))


def _collect_memory(builder: _Builder, owner: Optional[str], workspace: Optional[str],
                    now: datetime) -> Tuple[List[Dict[str, Any]], Set[str], Set[str]]:
    """Memory nodes, their ``evidence_of`` edges and the ``contradicts`` pairs.

    Returns ``(items, dispatch refs seen, citation refs seen)`` so the later
    sources know what the stored evidence actually pointed at.
    """
    jobs: Set[str] = set()
    citations: Set[str] = set()
    try:
        from src import memory_engine as engine
    except Exception as exc:  # noqa: BLE001 - the store is optional
        logger.debug("provenance: memory engine unavailable (%s)", exc)
        builder.source("memory", available=False, note=f"memory engine unavailable: {exc}")
        return [], jobs, citations
    try:
        items = engine.scoped_items(owner or None, workspace or None, engine.STATUSES)
    except Exception as exc:  # noqa: BLE001 - a missing/broken db is a smaller graph
        logger.debug("provenance: memory store unreadable (%s)", exc)
        builder.source("memory", available=False, note=f"memory store unreadable: {exc}")
        return [], jobs, citations

    items = [i for i in items if isinstance(i, dict) and i.get("id")]
    items.sort(key=lambda i: str(i.get("id")))
    by_text: Dict[str, str] = {}          # normalized text → memory node id
    node_of: Dict[str, str] = {}
    spans_drawn = 0

    for item in items:
        item_id = str(item.get("id"))
        try:
            score = round(engine.effective_score(item, now), 4)
            harm = round(engine.harmful_ratio(item, now), 4)
        except Exception:  # noqa: BLE001 - scoring is arithmetic, but never fatal
            score, harm = 0.0, 0.0
        node_id = builder.node(
            "memory", item_id,
            label=_memory_label(item),
            detail=f"{item.get('level')} · {item.get('status')} · {item.get('maturity')}",
            meta={"id8": item_id[:8], "level": _text(item.get("level"), 40),
                  "status": _text(item.get("status"), 40),
                  "maturity": _text(item.get("maturity"), 40),
                  "trust_class": _text(item.get("trust_class"), 40),
                  "effective_score": score, "harmful_ratio": harm,
                  "updated_at": _text(item.get("updated_at"), 40),
                  "project": _text(item.get("project"), 300)},
        )
        if not node_id:
            continue
        node_of[item_id] = node_id
        by_text.setdefault(text_overlap.normalize(item.get("text")), node_id)

        for span in item.get("evidence") or []:
            if not isinstance(span, dict):
                continue
            spans_drawn += _memory_evidence_edge(builder, node_id, item, span,
                                                 workspace, jobs, citations)

    # contradicts: the Curator's inversion, which is a STORED field. The target
    # must already be in the graph — a rule that was pruned leaves no phantom.
    inverted = 0
    for item in items:
        if item.get("status") != "anti_pattern":
            continue
        original = str(item.get("inverted_from") or "")
        if not original:
            continue
        source_id = node_of.get(str(item.get("id")))
        target_id = by_text.get(text_overlap.normalize(original))
        if not source_id or not target_id:
            continue
        try:
            harm = round(engine.harmful_ratio(item, now), 4)
        except Exception:  # noqa: BLE001
            harm = 0.0
        # Confidence 1.0 because the EDGE is certain: ``inverted_from`` is a
        # stored field the deterministic Curator wrote. How badly the original
        # rule was doing is a separate, decaying number and rides in meta.
        if builder.edge(source_id, target_id, "contradicts", confidence=1.0,
                        why=("the Curator inverted that rule into this anti-pattern after it "
                             "proved harmful; the original text is stored in inverted_from"),
                        meta={"harmful_ratio": harm}):
            inverted += 1

    builder.source("memory", available=True, count=len(items),
                   note=f"{len(items)} item(s), {spans_drawn} evidence edge(s), "
                        f"{inverted} contradiction(s)")
    return items, jobs, citations


def _memory_evidence_edge(builder: _Builder, memory_node: str, item: Dict[str, Any],
                          span: Dict[str, Any], workspace: Optional[str],
                          jobs: Set[str], citations: Set[str]) -> int:
    """One stored evidence span → one ``evidence_of`` edge. Never invents.

    The span's own ``kind`` decides the target; a ``ref`` that names an expert
    chunk is remembered for the citation pass instead of being guessed at here.
    """
    kind = str(span.get("kind") or "chat")
    ref = str(span.get("ref") or "")
    session = str(span.get("session_id") or "")
    excerpt = _text(span.get("excerpt"), 160)
    trust = _conf(item.get("trust"), 0.5)

    if _EXPERT_REF_RE.search(ref) or _CHUNK_ID_RE.search(ref):
        citations.add(ref)
        return 0

    if kind == "dispatch" and ref:
        jobs.add(ref)
        target = builder.node("checkpoint", ref, label=f"dispatch job {ref}",
                              meta={"job_id": ref, "source": "dispatch"})
        why = (f"this memory's evidence span names dispatch job {ref}"
               + (f": “{excerpt}”" if excerpt else ""))
        return 1 if builder.edge(memory_node, target, "evidence_of",
                                 confidence=trust, why=why) else 0

    if kind == "file" or (kind != "chat" and _looks_like_path(ref)) or \
            (kind == "chat" and not session and _looks_like_path(ref)):
        path, line = _split_line(ref)
        key = _norm_path(path, workspace)
        if not key:
            return 0
        target = builder.node("file", key, label=key.rsplit("/", 1)[-1], detail=key,
                              meta={"path": key})
        if target and line is not None:
            lines = builder.nodes[target]["meta"].setdefault("lines", [])
            if line not in lines:
                lines.append(line)
                lines.sort()
        where = f"{key}:{line}" if line is not None else key
        why = (f"this memory's evidence span points at {where}"
               + (f": “{excerpt}”" if excerpt else ""))
        return 1 if builder.edge(memory_node, target, "evidence_of",
                                 confidence=trust, why=why,
                                 meta=({"line": line} if line is not None else None)) else 0

    chat_key = session or (ref if ref and not _looks_like_path(ref) else "")
    if not chat_key:
        return 0
    target = builder.node("chat", chat_key, label=f"session {chat_key[:12]}",
                          meta={"session_id": chat_key})
    why = (f"this memory was recorded from chat session {chat_key[:12]}"
           + (f": “{excerpt}”" if excerpt else ""))
    return 1 if builder.edge(memory_node, target, "evidence_of",
                             confidence=trust, why=why) else 0


# ---------------------------------------------------------------------------
# Source 3 — dispatch mirrors: checkpoints and what they changed on disk
# ---------------------------------------------------------------------------


def _mirror_paths(mirror: Dict[str, Any]) -> List[Tuple[str, str]]:
    """``[(path, "added"|"modified"|"deleted"|"changed")]`` from a job mirror.

    Read from ``changes`` — what Faustus SAW on disk between its two
    checkpoints — falling back to ``result.files_changed``, which dispatch has
    already replaced with the observed set.
    """
    out: List[Tuple[str, str]] = []
    changes = mirror.get("changes")
    if isinstance(changes, dict):
        for kind in ("added", "modified", "deleted"):
            for path in changes.get(kind) or []:
                out.append((str(path), kind))
    if not out:
        result = mirror.get("result")
        if isinstance(result, dict):
            for path in result.get("files_changed") or []:
                out.append((str(path), "changed"))
    return out


def _read_mirrors(limit: int) -> List[Dict[str, Any]]:
    """The newest job mirrors under ``DATA_DIR/dispatch/``. Never raises."""
    directory = _dispatch_dir()
    try:
        names = [n for n in os.listdir(directory) if n.endswith(".json")]
    except OSError:
        return []
    stamped: List[Tuple[float, str]] = []
    for name in names:
        path = os.path.join(directory, name)
        try:
            stamped.append((os.path.getmtime(path), name))
        except OSError:
            continue
    # Newest first, name as the tiebreak so a same-second pair is deterministic.
    stamped.sort(key=lambda pair: (-pair[0], pair[1]))
    out: List[Dict[str, Any]] = []
    for _, name in stamped[:max(0, int(limit))]:
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as fh:
                mirror = json.load(fh)
        except (OSError, ValueError, TypeError):
            continue                     # a half-written mirror is simply skipped
        if isinstance(mirror, dict) and mirror.get("id"):
            out.append(mirror)
    out.sort(key=lambda m: str(m.get("id")))
    return out


def _collect_checkpoints(builder: _Builder, workspace: Optional[str],
                         wanted_jobs: Set[str]) -> None:
    """Checkpoint nodes + ``changed`` edges to file nodes."""
    mirrors = _read_mirrors(MAX_DISPATCH_MIRRORS)
    if not mirrors:
        builder.source("checkpoints", available=False,
                       note=f"no dispatch mirrors under {_dispatch_dir()}")
        return
    base = _norm_path(workspace, None).lower() if workspace else ""
    used = 0
    changed_edges = 0
    for mirror in mirrors:
        job_id = str(mirror.get("id"))
        in_scope = job_id in wanted_jobs
        if not in_scope and workspace:
            in_scope = _norm_path(mirror.get("workspace"), None).lower() == base
        if not in_scope and not workspace:
            in_scope = True              # no scope asked for: show the newest jobs
        if not in_scope:
            continue
        paths = _mirror_paths(mirror)
        node_id = builder.node(
            "checkpoint", job_id,
            label=_text(mirror.get("title")) or f"dispatch job {job_id}",
            detail=f"{_text(mirror.get('status'), 40)} · {len(paths)} file(s) changed",
            meta={"job_id": job_id, "status": _text(mirror.get("status"), 40),
                  "verdict": _text(mirror.get("verdict"), 200),
                  "workspace": _text(mirror.get("workspace"), 300),
                  "checkpoint": _text(mirror.get("checkpoint"), 60),
                  "session_id": _text(mirror.get("session_id"), 120)},
        )
        if not node_id:
            continue
        used += 1
        for path, how in paths:
            key = _norm_path(path, workspace)
            if not key:
                continue
            target = builder.node("file", key, label=key.rsplit("/", 1)[-1], detail=key,
                                  meta={"path": key})
            if builder.edge(node_id, target, "changed", confidence=1.0,
                            why=(f"the checkpoint diff of job {job_id} shows {key} was {how} "
                                 f"— observed on disk, not claimed by a worker"),
                            meta={"how": how}):
                changed_edges += 1
        session = str(mirror.get("session_id") or "")
        if session:
            chat = builder.node("chat", session, label=f"session {session[:12]}",
                                meta={"session_id": session})
            builder.edge(node_id, chat, "evidence_of", confidence=1.0,
                         why=f"job {job_id} was dispatched from chat session {session[:12]}")
    builder.source("checkpoints", available=True, count=used,
                   note=f"{used} job mirror(s) in scope, {changed_edges} changed-file edge(s)")


# ---------------------------------------------------------------------------
# Source 4 — experts (services/experts.py): citations that resolve to a page
# ---------------------------------------------------------------------------


def _collect_experts(builder: _Builder, citation_refs: Set[str]) -> None:
    """``expert`` / ``corpus`` nodes and ``cites`` edges.

    The spec's first choice for this source is "chunks a STORED REVIEW cited".
    Faustus does not store reviews: ``src/expert_review.py`` parses a model's
    corrections, anchors them and hands the result to the model — nothing is
    written to disk. So the source used here is the other stored citation
    record that DOES exist: an evidence ref (on a memory item or an objective)
    that names an expert chunk id. That ref was written by the app when the
    evidence was recorded, and the page it resolves to comes from
    ``services.experts.citation()``, which copies the page out of the chunk
    index and NEVER invents one.

    With no such ref the source is skipped silently and the graph is smaller.
    """
    if not citation_refs:
        builder.source("experts", available=False,
                       note="no stored evidence ref names an expert chunk "
                            "(Faustus stores no review records)")
        return
    try:
        from services import experts as experts_svc
    except Exception as exc:  # noqa: BLE001
        logger.debug("provenance: experts unavailable (%s)", exc)
        builder.source("experts", available=False, note=f"experts unavailable: {exc}")
        return
    try:
        slugs = list(experts_svc.list_expert_slugs())
    except Exception as exc:  # noqa: BLE001
        logger.debug("provenance: expert listing failed (%s)", exc)
        builder.source("experts", available=False, note=f"expert listing failed: {exc}")
        return
    if not slugs:
        builder.source("experts", available=False, note="no experts on disk")
        return

    # chunk id → (slug, source file), from each expert's own stored index.
    owner_of: Dict[str, Tuple[str, str]] = {}
    for slug in sorted(slugs):
        try:
            index = experts_svc.load_index(slug)
        except Exception as exc:  # noqa: BLE001
            logger.debug("provenance: index of %s unreadable (%s)", slug, exc)
            continue
        for chunk in index:
            if isinstance(chunk, dict) and chunk.get("id"):
                owner_of.setdefault(str(chunk["id"]), (slug, str(chunk.get("source") or "")))

    # chunk id → the nodes that STORED a ref naming it (stamped by
    # _remember_citation_refs, so the citing side is a record, not a guess).
    citing: Dict[str, List[str]] = {}
    for node_id, node in builder.nodes.items():
        for chunk_id in (node.get("meta") or {}).get("cited_chunks") or []:
            citing.setdefault(str(chunk_id), []).append(node_id)

    wanted: List[str] = []
    for ref in sorted(citation_refs):
        explicit = _EXPERT_REF_RE.search(ref)
        for chunk_id in ([explicit.group("chunk")] if explicit else _CHUNK_ID_RE.findall(ref)):
            if chunk_id not in wanted:
                wanted.append(chunk_id)

    resolved = 0
    edges_drawn = 0
    for chunk_id in wanted:
        found = owner_of.get(chunk_id)
        if not found:
            continue                     # a ref naming no stored chunk cites nothing
        slug, source_name = found
        try:
            info = experts_svc.citation(slug, chunk_id) or {}
        except Exception:  # noqa: BLE001
            info = {}
        source_name = str(info.get("source") or source_name)
        if not source_name:
            continue
        expert_node = builder.node("expert", slug, label=slug,
                                   detail="specialist expert with its own corpus",
                                   meta={"slug": slug})
        corpus_node = builder.node(
            "corpus", f"{slug}/{source_name}", label=source_name,
            detail=f"corpus file of the {slug} expert",
            meta={"slug": slug, "source": source_name,
                  "file_url": _text(info.get("file_url"), 300)},
        )
        if not corpus_node:
            continue
        resolved += 1
        if expert_node:
            builder.edge(expert_node, corpus_node, "contains", confidence=1.0,
                         why=f"{source_name} is a file in the {slug} expert's own corpus, "
                             f"and its chunk index records this chunk from it")
        # A page is copied out of the index or not reported at all: an "unknown"
        # page confidence never becomes a page number here either.
        page = info.get("page")
        if isinstance(page, int) and str(info.get("page_confidence") or "") == "exact":
            where, confidence = f"page {page}", 1.0
        else:
            where = f"lines {info.get('start_line', '?')}–{info.get('end_line', '?')}"
            confidence = 0.6
        for node_id in sorted(citing.get(chunk_id, ())):
            if builder.edge(node_id, corpus_node, "cites", confidence=confidence,
                            why=f"stored evidence cites {source_name} at {where} "
                                f"(chunk {chunk_id[:10]})"):
                edges_drawn += 1
    builder.source("experts", available=bool(resolved), count=resolved,
                   note=f"{resolved} stored citation(s) resolved to a chunk in an expert "
                        f"index, {edges_drawn} cites edge(s)")


def _remember_citation_refs(builder: _Builder, items: Sequence[Dict[str, Any]]) -> None:
    """Stamp each memory node's meta with the chunk ids its evidence named, so
    the citation pass can find the node that stored the ref."""
    for item in items:
        node_id = f"memory:{item.get('id')}"
        node = builder.nodes.get(node_id)
        if not node:
            continue
        found: List[str] = []
        for span in item.get("evidence") or []:
            if not isinstance(span, dict):
                continue
            for chunk_id in _CHUNK_ID_RE.findall(str(span.get("ref") or "")):
                if chunk_id not in found:
                    found.append(chunk_id)
        if found:
            node["meta"]["cited_chunks"] = found


# ---------------------------------------------------------------------------
# Source 5 — verified near-duplicates (src/text_overlap.py)
# ---------------------------------------------------------------------------


def _collect_duplicates(builder: _Builder, items: Sequence[Dict[str, Any]],
                        by_obj: Dict[str, str]) -> None:
    """``duplicate_of`` edges over memory texts and objective titles.

    The ratio that becomes the edge's ``confidence`` is measured, not guessed:
    :mod:`src.text_overlap` only reports a span after an exact substring
    comparison confirms it.
    """
    rows: List[Dict[str, Any]] = []
    for item in items:
        rows.append({"id": f"memory:{item.get('id')}", "text": item.get("text")})
    for oid, node_id in by_obj.items():
        node = builder.nodes.get(node_id)
        if node:
            rows.append({"id": node_id, "text": node["meta"].get("title") or node["label"]})
    rows = [r for r in rows if builder.has(r["id"])]
    if len(rows) < 2:
        builder.source("duplicates", available=False,
                       note="fewer than two texts to compare")
        return
    if len(rows) > MAX_DUPLICATE_ITEMS:
        rows = sorted(rows, key=lambda r: r["id"])[:MAX_DUPLICATE_ITEMS]
    try:
        pairs = text_overlap.find_duplicates(rows, DUPLICATE_THRESHOLD,
                                             max_items=MAX_DUPLICATE_ITEMS)
    except Exception as exc:  # noqa: BLE001 - the detector already guards itself
        logger.debug("provenance: duplicate detection failed (%s)", exc)
        builder.source("duplicates", available=False, note=f"detection failed: {exc}")
        return
    by_id = {r["id"]: r["text"] for r in rows}
    drawn = 0
    for pair in pairs:
        spans = pair.get("spans") or []
        excerpt = ""
        if spans:
            (start, end), _ = spans[0]
            excerpt = _text(text_overlap.span_text(by_id.get(pair["a"]), start, end), 120)
        percent = int(round(pair["ratio"] * 100))
        why = (f"{percent}% of the two texts is literally shared — verified by exact substring "
               f"comparison, not by a model" + (f": “{excerpt}”" if excerpt else ""))
        if builder.edge(pair["a"], pair["b"], "duplicate_of",
                        confidence=pair["ratio"], why=why,
                        meta={"spans": [[list(s[0]), list(s[1])] for s in spans[:8]]}):
            drawn += 1
    builder.source("duplicates", available=True, count=drawn,
                   note=f"{len(rows)} text(s) compared, {drawn} verified near-duplicate pair(s)")


# ---------------------------------------------------------------------------
# build()
# ---------------------------------------------------------------------------


def build(
    owner: Optional[str] = None,
    *,
    project: Optional[Dict[str, Any]] = None,
    workspace: Optional[str] = None,
    limit_nodes: int = DEFAULT_LIMIT_NODES,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The graph: ``{"nodes", "edges", "sources", "truncated"}``.

    ``project`` is a project record (the same dict ``services/objectives.py``
    takes — only ``workspace`` is read from it); ``workspace`` defaults to that
    project's folder and is BOTH the memory-engine scope key (which is a
    workspace path, see src/tool_execution.py) and the root file paths are made
    relative to.

    ``owner``/``workspace`` left empty mean "do not filter", so an admin
    auditing the whole store sees the whole store.

    Node shape::

        {"id": "<kind>:<key>", "kind", "label", "detail", "meta": {...}}

    Edge shape::

        {"from", "to", "kind", "confidence": float, "why": str,
         "trust": "declared", "meta"?: {...}}

    ``why`` is a short human sentence naming the record the edge came from,
    because the whole point of this view is that a user can ask "why is this
    here" and get an answer.

    Never raises. Every source is optional: a missing one lands in ``sources``
    as ``available: false`` with a reason, and the graph is smaller.
    """
    try:
        limit = max(1, min(MAX_LIMIT_NODES, int(limit_nodes or DEFAULT_LIMIT_NODES)))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT_NODES
    now = now or _utcnow()
    builder = _Builder(limit)
    project = project if isinstance(project, dict) else None
    if workspace is None and project:
        workspace = project.get("workspace") or None

    try:
        by_obj = _collect_objectives(builder, project)
    except Exception as exc:  # noqa: BLE001
        logger.debug("provenance: objectives pass failed (%s)", exc)
        by_obj = {}
        builder.source("objectives", available=False, note=f"failed: {exc}")

    try:
        log_jobs = _collect_objective_log(builder, project, by_obj)
    except Exception as exc:  # noqa: BLE001
        logger.debug("provenance: objective log pass failed (%s)", exc)
        log_jobs = set()
        builder.source("objective_log", available=False, note=f"failed: {exc}")

    try:
        items, mem_jobs, citation_refs = _collect_memory(builder, owner, workspace, now)
    except Exception as exc:  # noqa: BLE001
        logger.debug("provenance: memory pass failed (%s)", exc)
        items, mem_jobs, citation_refs = [], set(), set()
        builder.source("memory", available=False, note=f"failed: {exc}")

    try:
        _remember_citation_refs(builder, items)
    except Exception as exc:  # noqa: BLE001
        logger.debug("provenance: citation bookkeeping failed (%s)", exc)

    try:
        _collect_checkpoints(builder, workspace, log_jobs | mem_jobs)
    except Exception as exc:  # noqa: BLE001
        logger.debug("provenance: checkpoint pass failed (%s)", exc)
        builder.source("checkpoints", available=False, note=f"failed: {exc}")

    try:
        _collect_experts(builder, citation_refs)
    except Exception as exc:  # noqa: BLE001
        logger.debug("provenance: expert pass failed (%s)", exc)
        builder.source("experts", available=False, note=f"failed: {exc}")

    try:
        _collect_duplicates(builder, items, by_obj)
    except Exception as exc:  # noqa: BLE001
        logger.debug("provenance: duplicate pass failed (%s)", exc)
        builder.source("duplicates", available=False, note=f"failed: {exc}")

    return builder.finish()


# ---------------------------------------------------------------------------
# Reading the graph
# ---------------------------------------------------------------------------


def _nodes_of(graph: Any) -> List[Dict[str, Any]]:
    nodes = (graph or {}).get("nodes") if isinstance(graph, dict) else None
    return [n for n in (nodes or []) if isinstance(n, dict) and n.get("id")]


def _edges_of(graph: Any) -> List[Dict[str, Any]]:
    edges = (graph or {}).get("edges") if isinstance(graph, dict) else None
    return [e for e in (edges or []) if isinstance(e, dict) and e.get("from") and e.get("to")]


def stats(graph: Any) -> Dict[str, Any]:
    """Counts by node kind and edge kind, plus the totals. Never raises."""
    nodes, edges = _nodes_of(graph), _edges_of(graph)
    by_kind: Dict[str, int] = {}
    for node in nodes:
        by_kind[str(node.get("kind"))] = by_kind.get(str(node.get("kind")), 0) + 1
    by_edge: Dict[str, int] = {}
    for edge in edges:
        by_edge[str(edge.get("kind"))] = by_edge.get(str(edge.get("kind")), 0) + 1
    connected = {e["from"] for e in edges} | {e["to"] for e in edges}
    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "node_kinds": dict(sorted(by_kind.items())),
        "edge_kinds": dict(sorted(by_edge.items())),
        "orphans": len([n for n in nodes if n["id"] not in connected]),
        "truncated": bool((graph or {}).get("truncated")) if isinstance(graph, dict) else False,
    }


def filter_kinds(graph: Any, kinds: Optional[Iterable[str]]) -> Dict[str, Any]:
    """The subgraph of the named node kinds; edges touching a dropped node go
    with it. ``None``/empty means "everything". Never raises."""
    base = graph if isinstance(graph, dict) else {}
    wanted = {str(k).strip().lower() for k in (kinds or []) if str(k).strip()}
    if not wanted:
        return dict(base)
    nodes = [n for n in _nodes_of(base) if str(n.get("kind")) in wanted]
    keep = {n["id"] for n in nodes}
    edges = [e for e in _edges_of(base) if e["from"] in keep and e["to"] in keep]
    out = dict(base)
    out["nodes"], out["edges"] = nodes, edges
    return out


def filter_trust(graph: Any, minimum: str = TRUST_DECLARED) -> Dict[str, Any]:
    """Drop every edge below ``minimum`` trust.

    Today this is a no-op — the builder only ever emits ``declared`` edges. It
    exists so that the day a model-inferred source is added, dropping it is one
    call and the audit view can be restored to declared-only.
    """
    base = graph if isinstance(graph, dict) else {}
    floor = TRUST_ORDER.get(str(minimum), TRUST_ORDER[TRUST_DECLARED])
    out = dict(base)
    out["edges"] = [e for e in _edges_of(base)
                    if TRUST_ORDER.get(str(e.get("trust") or TRUST_DECLARED), 0) >= floor]
    return out


def orphans(graph: Any) -> Dict[str, Any]:
    """Nodes no edge touches, grouped by kind — value #2 of the report.

    ``{"by_kind": {kind: [node, ...]}, "ids": [...], "count": int}``, every
    list sorted by id so two calls answer identically.
    """
    nodes, edges = _nodes_of(graph), _edges_of(graph)
    connected = {e["from"] for e in edges} | {e["to"] for e in edges}
    loose = sorted((n for n in nodes if n["id"] not in connected), key=lambda n: n["id"])
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for node in loose:
        by_kind.setdefault(str(node.get("kind")), []).append(node)
    return {"by_kind": dict(sorted(by_kind.items())),
            "ids": [n["id"] for n in loose], "count": len(loose)}


def _adjacency(edges: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for edge in edges:
        out.setdefault(edge["from"], []).append(edge)
        out.setdefault(edge["to"], []).append(edge)
    return out


def neighbors(graph: Any, node_id: Any, hops: int = DEFAULT_HOPS) -> Dict[str, Any]:
    """The subgraph within ``hops`` undirected steps of ``node_id`` — value #3.

    ``{"root", "hops", "nodes", "edges", "missing": bool}``. An unknown node id
    answers with an empty subgraph and ``missing: True``, never an error.
    """
    node_id = str(node_id or "")
    nodes, edges = _nodes_of(graph), _edges_of(graph)
    by_id = {n["id"]: n for n in nodes}
    if node_id not in by_id:
        return {"root": node_id, "hops": 0, "nodes": [], "edges": [], "missing": True}
    try:
        hops = max(0, min(MAX_HOPS, int(hops)))
    except (TypeError, ValueError):
        hops = DEFAULT_HOPS
    adjacency = _adjacency(edges)
    reached = {node_id}
    frontier = [node_id]
    used: List[Dict[str, Any]] = []
    seen_edges: Set[Tuple[str, str, str]] = set()
    for _ in range(hops):
        nxt: List[str] = []
        for current in frontier:
            for edge in adjacency.get(current, ()):
                key = (edge["from"], edge["to"], edge["kind"])
                if key not in seen_edges:
                    seen_edges.add(key)
                    used.append(edge)
                other = edge["to"] if edge["from"] == current else edge["from"]
                if other not in reached:
                    reached.add(other)
                    nxt.append(other)
        frontier = sorted(set(nxt))
        if not frontier:
            break
    sub_nodes = sorted((by_id[i] for i in reached if i in by_id),
                       key=lambda n: (n["kind"], n["id"]))
    sub_edges = sorted((e for e in used if e["from"] in reached and e["to"] in reached),
                       key=lambda e: (e["kind"], e["from"], e["to"]))
    return {"root": node_id, "hops": hops, "nodes": sub_nodes, "edges": sub_edges,
            "missing": False}


#: Edge kinds that make "what breaks if I touch this" true: a dependent breaks
#: when its dependency moves, and a checkpoint's story changes when a file it
#: recorded does.
IMPACT_KINDS: Tuple[str, ...] = ("depends_on", "changed")


def impact(graph: Any, node_id: Any) -> List[str]:
    """Everything reachable by REVERSED ``depends_on`` / ``changed`` edges.

    Literally "what breaks if I touch this": ``A depends_on B`` means touching
    B threatens A, and ``checkpoint changed F`` means touching F changes what
    that checkpoint's diff describes. Returns sorted node ids, never including
    the root. Never raises.
    """
    node_id = str(node_id or "")
    edges = [e for e in _edges_of(graph) if e.get("kind") in IMPACT_KINDS]
    incoming: Dict[str, List[str]] = {}
    for edge in edges:
        incoming.setdefault(edge["to"], []).append(edge["from"])
    seen: Set[str] = set()
    queue = [node_id]
    while queue:
        current = queue.pop()
        for dependent in incoming.get(current, ()):
            if dependent not in seen and dependent != node_id:
                seen.add(dependent)
                queue.append(dependent)
    return sorted(seen)


#: Edges that carry provenance. Followed forwards ("what this rests on") and
#: backwards ("what vouches for this") by :func:`explain`.
_EXPLAIN_FORWARD: Tuple[str, ...] = ("evidence_of", "cites", "contradicts", "changed",
                                     "depends_on", "duplicate_of")
_EXPLAIN_BACKWARD: Tuple[str, ...] = ("evidence_of", "changed", "cites", "duplicate_of",
                                      "contradicts")
#: Steps are ordered by hop, then by how much a kind actually EXPLAINS: the
#: stored evidence first, the "also stored over there" last. Alphabetical order
#: would put duplicate_of above evidence_of, which reads as an odd answer to
#: "why does the agent believe this".
_EXPLAIN_RANK: Dict[str, int] = {"evidence_of": 0, "cites": 1, "changed": 2,
                                 "contradicts": 3, "depends_on": 4, "duplicate_of": 5}


def explain(graph: Any, node_id: Any, hops: int = MAX_EXPLAIN_HOPS) -> Dict[str, Any]:
    """The audit answer for one node — value #1 of the report.

    Returns the incoming evidence chain as ORDERED steps, nearest first::

        {"node": {...}, "steps": [{"order", "hop", "from", "to", "kind",
                                   "confidence", "trust", "why", "direction",
                                   "node": {...}}],
         "summary": str, "missing": bool}

    ``direction`` is ``"rests_on"`` when the step follows an edge out of the
    node (a memory pointing at the chat, file and line its evidence span names)
    and ``"vouches_for"`` when it follows one in (a checkpoint whose diff, or a
    session whose delta record, is evidence ABOUT this node). Every step
    carries the edge's own ``why``, so the chain reads as sentences.

    An unknown id answers with an empty chain and ``missing: True``.
    """
    node_id = str(node_id or "")
    nodes, edges = _nodes_of(graph), _edges_of(graph)
    by_id = {n["id"]: n for n in nodes}
    root = by_id.get(node_id)
    if root is None:
        return {"node": None, "steps": [], "summary": "", "missing": True}
    try:
        hops = max(1, min(MAX_HOPS, int(hops)))
    except (TypeError, ValueError):
        hops = MAX_EXPLAIN_HOPS

    out_edges: Dict[str, List[Dict[str, Any]]] = {}
    in_edges: Dict[str, List[Dict[str, Any]]] = {}
    for edge in edges:
        out_edges.setdefault(edge["from"], []).append(edge)
        in_edges.setdefault(edge["to"], []).append(edge)

    steps: List[Dict[str, Any]] = []
    seen_edges: Set[Tuple[str, str, str]] = set()
    seen_nodes = {node_id}
    frontier = [node_id]
    for hop in range(1, hops + 1):
        batch: List[Tuple[Tuple[str, str, str], Dict[str, Any], str]] = []
        for current in frontier:
            for edge in out_edges.get(current, ()):
                if edge["kind"] in _EXPLAIN_FORWARD:
                    batch.append(((edge["kind"], edge["from"], edge["to"]), edge, "rests_on"))
            for edge in in_edges.get(current, ()):
                if edge["kind"] in _EXPLAIN_BACKWARD:
                    batch.append(((edge["kind"], edge["from"], edge["to"]), edge, "vouches_for"))
        batch.sort(key=lambda row: (_EXPLAIN_RANK.get(row[0][0], 9), row[0][1], row[0][2],
                                    row[2]))
        nxt: List[str] = []
        for key, edge, direction in batch:
            if key in seen_edges:
                continue
            seen_edges.add(key)
            other = edge["to"] if direction == "rests_on" else edge["from"]
            steps.append({
                "order": len(steps) + 1,
                "hop": hop,
                "from": edge["from"],
                "to": edge["to"],
                "kind": edge["kind"],
                "confidence": edge.get("confidence"),
                "trust": edge.get("trust") or TRUST_DECLARED,
                "why": edge.get("why") or "",
                "direction": direction,
                "node": by_id.get(other),
            })
            if len(steps) >= MAX_EXPLAIN_STEPS:
                break
            if other not in seen_nodes:
                seen_nodes.add(other)
                nxt.append(other)
        if len(steps) >= MAX_EXPLAIN_STEPS:
            break
        frontier = sorted(set(nxt))
        if not frontier:
            break

    if steps:
        summary = (f"{root.get('label') or node_id} — {len(steps)} declared record(s) "
                   f"explain this node.")
    else:
        summary = (f"{root.get('label') or node_id} — nothing stored points at this node, "
                   f"so there is no evidence chain to show.")
    return {"node": root, "steps": steps, "summary": summary, "missing": False}


def ranking_signal(graph: Any, node_ids: Optional[Iterable[str]] = None) -> Dict[str, float]:
    """Normalized graph centrality in **[0, 0.10]**, per node id.

    The cap is the point, not a detail. ``eidetic_engine_cli`` — the richest
    graph in the ecosystem this feature was mined from, with PageRank, HITS and
    Gomory-Hu behind it — weights its own graph lane **0.10** against 0.45 BM25
    and 0.45 semantic, and ``src/memory_engine.py`` already retrieves on
    exactly those three weights. A graph is a good tiebreak and a bad ranker:
    connectedness measures how much has been WRITTEN about a thing, not how
    well it answers the question in front of you. So a caller adds this to a
    lexical+semantic score knowing the graph can never dominate it.

    Degree centrality, normalized by the busiest node, times 0.10. Never
    raises; an unknown id scores 0.0.
    """
    edges = _edges_of(graph)
    degree: Dict[str, int] = {}
    for edge in edges:
        degree[edge["from"]] = degree.get(edge["from"], 0) + 1
        degree[edge["to"]] = degree.get(edge["to"], 0) + 1
    wanted = [str(i) for i in node_ids] if node_ids is not None else \
        [n["id"] for n in _nodes_of(graph)]
    top = max(degree.values()) if degree else 0
    if top <= 0:
        return {node_id: 0.0 for node_id in wanted}
    return {node_id: round(RANKING_CAP * degree.get(node_id, 0) / top, 6)
            for node_id in wanted}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def enabled() -> bool:
    """``agent_provenance_graph``. Never raises."""
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_provenance_graph", True))
    except Exception:  # noqa: BLE001
        return True


def max_nodes() -> int:
    """``agent_provenance_max_nodes``, clamped. Never raises."""
    try:
        from src.settings import get_setting
        value = int(get_setting("agent_provenance_max_nodes", DEFAULT_LIMIT_NODES))
    except Exception:  # noqa: BLE001
        value = DEFAULT_LIMIT_NODES
    return max(50, min(MAX_LIMIT_NODES, value))
