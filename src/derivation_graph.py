"""derivation_graph.py — selective rebuild of derived results, and the map that
says what a change touches (proposals 35 and 23).

A derived result is the output of an operation on inputs. This module records,
for each operation: its inputs (a file's content hash, an artifact's blob hash
or the output digest of an upstream node), its parameters, the identity and
version of the tool, recipe or workflow that ran, and the hashes of what it
produced. From that record it answers one question exactly: which nodes are
stale now, and why.

    input hash changed | parameter changed | tool/recipe version changed
    output missing or edited | never ran | failed or interrupted last time
    | flagged by the decision map

``rebuild(target)`` walks the ancestors of the target in dependency order and
re-runs only the stale ones. A node that is intact is reused. A node that ran
to completion is never run again unless its record stops matching, which is
also how a rebuild resumes after a failure between two steps: the completed
nodes are intact, the failed one is stale, so only it and what depends on it
run. If a stale node's new output hashes the same as before, what depends on it
is still intact and is not re-run (the check is made node by node, with the
upstream result already in hand).

This module does not execute transformations by itself and is not a second
orchestrator. Execution is handed in:

* a Python caller passes ``runner(node_view) -> {"outputs": [...]}`` and the
  rebuild drives it (this is how a workflow node or a test uses it);
* with no runner (the chat tool, the HTTP routes, the MCP server) the rebuild
  returns the next stale node, marked running with its idempotency key, and the
  caller runs it with its normal, approved tools and then calls
  ``complete_node``. Nothing is executed on the caller's behalf, so no tool
  approval is bypassed.

``idempotency_key`` of a node is a hash of its identity, inputs, parameters and
operation. It is given to the runner so an operation that reaches outside can
deduplicate; a node left ``running`` by a crash is offered again with the same
key and ``interrupted: true`` because only the operation can know whether its
side effect happened.

Derived results are also registered in Atlas's Hoard (revisions and reusable
derived results; it does not execute anything) when a project is configured and
Atlas answers. When it does not, local records are kept and the result says so.

The decision map (``map_*``, ``affected_by``) is explicit relations between
requirements, decisions, units, formats, derivation nodes and deliverables.
``affected_by(change)`` follows only links somebody wrote, plus the derivation
graph's own input edges; it never matches by meaning.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.atomic_io import atomic_write_json
from core.kernel_file_lock import KernelFileLock
from src import constants as _constants

logger = logging.getLogger(__name__)

SCHEMA = 1
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")
MAX_NODES = 2000
ITEM_KINDS = ("requirement", "decision", "unit", "format", "assumption", "deliverable")
_INLINE_VALUE_LIMIT = 4000


class DerivationError(Exception):
    def __init__(self, error_class: str, detail: str):
        super().__init__(detail)
        self.error_class = error_class
        self.detail = detail


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _dir(owner: str) -> str:
    return os.path.join(_constants.DATA_DIR, "derivations", _SAFE.sub("_", str(owner or "_"))[:120] or "_")


def _path(owner: str, graph_id: str) -> str:
    return os.path.join(_dir(owner), (_SAFE.sub("_", str(graph_id or "default"))[:120] or "default") + ".json")


def _blank(owner: str, graph_id: str) -> Dict[str, Any]:
    return {"schema": SCHEMA, "id": graph_id, "owner": owner, "created": time.time(), "updated": time.time(),
            "base_dir": "", "nodes": {}, "map": {"items": {}, "links": []}, "atlas": {}}


def _load(owner: str, graph_id: str) -> Dict[str, Any]:
    try:
        with open(_path(owner, graph_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("nodes"), dict):
            data.setdefault("map", {"items": {}, "links": []})
            data.setdefault("atlas", {})
            data.setdefault("base_dir", "")
            return data
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        logger.warning("[derivations] unreadable graph %s; starting empty", graph_id, exc_info=True)
    return _blank(owner, graph_id)


def _save(graph: Dict[str, Any]) -> None:
    graph["updated"] = time.time()
    os.makedirs(_dir(graph["owner"]), exist_ok=True)
    atomic_write_json(_path(graph["owner"], graph["id"]), graph)


class _Locked:
    """Load-modify-save under the graph's lock file (kept beside the data)."""

    def __init__(self, owner: str, graph_id: str, *, timeout: float = 10.0):
        self.owner, self.graph_id = owner, graph_id
        self._lock = KernelFileLock(_path(owner, graph_id) + ".lock", timeout=timeout)
        self.graph: Dict[str, Any] = {}

    def __enter__(self) -> "_Locked":
        self._lock.acquire()
        self.graph = _load(self.owner, self.graph_id)
        return self

    def save(self) -> None:
        _save(self.graph)

    def __exit__(self, *exc) -> None:
        self._lock.release()


def get_graph(owner: str, graph_id: str = "default") -> Dict[str, Any]:
    return _load(owner, graph_id)


def list_graphs(owner: str) -> List[Dict[str, Any]]:
    out = []
    try:
        names = sorted(n for n in os.listdir(_dir(owner)) if n.endswith(".json"))
    except OSError:
        return out
    for n in names:
        g = _load(owner, n[:-5])
        out.append({"id": g["id"], "nodes": len(g["nodes"]), "items": len(g["map"]["items"]),
                    "updated": g.get("updated"), "atlas_project": (g.get("atlas") or {}).get("project_id")})
    return out


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------

def sha256_file(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def value_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _abs(graph: Mapping[str, Any], node: Mapping[str, Any], path: str) -> str:
    base = node.get("base_dir") or graph.get("base_dir") or ""
    return os.path.normpath(os.path.join(base, path)) if base and not os.path.isabs(path) else os.path.normpath(path)


def _artifact_sha(artifact_id: str) -> Optional[str]:
    try:
        from src import artifact_identity
        occ = artifact_identity.resolve(artifact_id)
        return getattr(occ, "blob_sha256", None) if occ is not None else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------

def _norm_inputs(raw: Optional[Sequence[Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in raw or []:
        if isinstance(item, str):
            spec = {"type": "file", "path": item}
        elif isinstance(item, Mapping):
            if item.get("node"):
                spec = {"type": "node", "id": str(item["node"])}
            elif item.get("artifact_id"):
                spec = {"type": "artifact", "id": str(item["artifact_id"])}
            elif item.get("file") or item.get("path"):
                spec = {"type": "file", "path": str(item.get("file") or item.get("path"))}
            elif item.get("type") in ("file", "node", "artifact"):
                spec = {k: str(v) for k, v in item.items() if k in ("type", "path", "id")}
            else:
                raise DerivationError("derivations.bad_input", f"unrecognised input {dict(item)!r}")
        else:
            raise DerivationError("derivations.bad_input", f"unrecognised input {item!r}")
        if spec not in out:
            out.append(spec)
    return out


def _norm_outputs(raw: Optional[Sequence[Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in raw or []:
        if isinstance(item, str):
            out.append({"path": item})
        elif isinstance(item, Mapping) and item.get("artifact_id"):
            out.append({"artifact_id": str(item["artifact_id"])})
        elif isinstance(item, Mapping) and item.get("path"):
            out.append({"path": str(item["path"])})
        else:
            raise DerivationError("derivations.bad_output", f"unrecognised output {item!r}")
    return out


def _input_key(spec: Mapping[str, str]) -> str:
    return f"{spec['type']}:{spec.get('path') or spec.get('id')}"


def _ancestors(graph: Mapping[str, Any], targets: Iterable[str]) -> List[str]:
    """Targets and everything they depend on, dependencies first."""
    order: List[str] = []
    seen: set = set()
    visiting: set = set()

    def visit(nid: str) -> None:
        if nid in seen:
            return
        if nid in visiting:
            raise DerivationError("derivations.cycle", f"cycle through {nid!r}")
        visiting.add(nid)
        for spec in graph["nodes"][nid]["inputs"]:
            if spec["type"] == "node" and spec["id"] in graph["nodes"]:
                visit(spec["id"])
        visiting.discard(nid)
        seen.add(nid)
        order.append(nid)

    for t in targets:
        visit(t)
    return order


def _downstream(graph: Mapping[str, Any], start: Iterable[str]) -> List[str]:
    out: List[str] = []
    frontier = list(start)
    seen = set(frontier)
    while frontier:
        cur = frontier.pop()
        for nid, node in graph["nodes"].items():
            if nid not in seen and any(s["type"] == "node" and s["id"] == cur for s in node["inputs"]):
                seen.add(nid)
                out.append(nid)
                frontier.append(nid)
    return out


def define_node(owner: str, graph_id: str, node_id: str, *, op: Mapping[str, Any],
                inputs: Optional[Sequence[Any]] = None, params: Optional[Mapping[str, Any]] = None,
                outputs: Optional[Sequence[Any]] = None, base_dir: str = "",
                provenance: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Create or update a node. Changing ``params``, ``op`` or ``inputs`` of an
    existing node does not touch its recorded state: the next plan sees the
    difference and says which one changed."""
    node_id = str(node_id or "").strip()
    if not node_id or not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,120}", node_id):
        raise DerivationError("derivations.bad_id", "node id: 1-120 letters, digits and _ . : -")
    if not isinstance(op, Mapping) or not op.get("name"):
        raise DerivationError("derivations.bad_op", "op needs a name (and a version)")
    spec = {"kind": str(op.get("kind") or "tool"), "name": str(op["name"]), "version": str(op.get("version") or "")}
    with _Locked(owner, graph_id) as lk:
        g = lk.graph
        if node_id not in g["nodes"] and len(g["nodes"]) >= MAX_NODES:
            raise DerivationError("derivations.too_big", f"at most {MAX_NODES} nodes per graph")
        old = g["nodes"].get(node_id)
        node = old or {"id": node_id, "state": {"status": "never_run"}, "forced_stale": []}
        node.update({"op": spec, "inputs": _norm_inputs(inputs) if inputs is not None else node.get("inputs", []),
                     "params": dict(params) if params is not None else node.get("params", {}),
                     "outputs_declared": _norm_outputs(outputs) if outputs is not None else node.get("outputs_declared", []),
                     "base_dir": base_dir or node.get("base_dir", ""),
                     "provenance": dict(provenance) if provenance is not None else node.get("provenance", {})})
        for s in node["inputs"]:
            if s["type"] == "node" and s["id"] == node_id:
                raise DerivationError("derivations.cycle", "a node cannot depend on itself")
        g["nodes"][node_id] = node
        try:
            _ancestors(g, [node_id])
        except DerivationError:
            if old is None:
                g["nodes"].pop(node_id, None)
            raise
        missing = [s["id"] for s in node["inputs"] if s["type"] == "node" and s["id"] not in g["nodes"]]
        if missing:
            if old is None:
                g["nodes"].pop(node_id, None)
            raise DerivationError("derivations.unknown_node", f"inputs name nodes that do not exist: {missing}")
        lk.save()
        return _public(g, node)


def remove_node(owner: str, graph_id: str, node_id: str) -> Dict[str, Any]:
    with _Locked(owner, graph_id) as lk:
        g = lk.graph
        if node_id not in g["nodes"]:
            raise DerivationError("derivations.unknown_node", f"no node {node_id!r}")
        users = _downstream(g, [node_id])
        if users:
            raise DerivationError("derivations.in_use", f"{users} depend on {node_id!r}; remove them first")
        g["nodes"].pop(node_id)
        links = g["map"]["links"]
        g["map"]["links"] = [l for l in links if l["from"] != f"node:{node_id}" and l["to"] != f"node:{node_id}"]
        lk.save()
    return {"removed": node_id}


def _public(graph: Mapping[str, Any], node: Mapping[str, Any]) -> Dict[str, Any]:
    st = node.get("state") or {}
    return {"id": node["id"], "op": node["op"], "inputs": node["inputs"], "params": node.get("params", {}),
            "outputs": st.get("outputs") or node.get("outputs_declared", []), "status": st.get("status", "never_run"),
            "idempotency_key": st.get("idempotency_key", ""), "completed_at": st.get("completed_at"),
            "error": st.get("error", ""), "forced_stale": list(node.get("forced_stale") or []),
            "provenance": node.get("provenance", {}), "atlas": node.get("atlas")}


def _current_op(owner: str, node: Mapping[str, Any]) -> Dict[str, str]:
    op = dict(node["op"])
    if op["kind"] == "recipe":
        try:
            from src import recipes
            from src.recipe_learning import _fingerprint
            r = recipes.get_recipe(op["name"], owner)
            if r is not None:
                op["version"] = f"{r.version or '0'}/{_fingerprint(r)}" if r.step_templates else (r.version or op["version"])
        except Exception:  # noqa: BLE001
            logger.debug("[derivations] recipe version lookup failed", exc_info=True)
    return op


def _input_hashes(graph: Mapping[str, Any], node: Mapping[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for spec in node["inputs"]:
        key = _input_key(spec)
        if spec["type"] == "file":
            out[key] = sha256_file(_abs(graph, node, spec["path"])) or "missing"
        elif spec["type"] == "artifact":
            out[key] = _artifact_sha(spec["id"]) or "missing"
        else:
            up = graph["nodes"].get(spec["id"])
            st = (up or {}).get("state") or {}
            out[key] = st.get("output_digest") if st.get("status") == "done" else "pending"
    return out


def _idem_key(graph_id: str, node: Mapping[str, Any], op: Mapping[str, str], inputs: Mapping[str, str],
              params_hash: str) -> str:
    return value_hash([graph_id, node["id"], op, dict(sorted(inputs.items())), params_hash])[:32]


def _output_state(graph: Mapping[str, Any], node: Mapping[str, Any], outputs: Sequence[Mapping[str, Any]]) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for o in outputs:
        if "path" in o:
            out[o["path"]] = sha256_file(_abs(graph, node, o["path"]))
        elif "artifact_id" in o:
            out["artifact:" + o["artifact_id"]] = _artifact_sha(o["artifact_id"])
        else:
            out["value"] = o.get("value_hash")
    return out


def _evaluate(owner: str, graph: Mapping[str, Any], node: Mapping[str, Any]) -> Dict[str, Any]:
    st = node.get("state") or {}
    op = _current_op(owner, node)
    inputs = _input_hashes(graph, node)
    params_hash = value_hash(node.get("params", {}))
    reasons: List[str] = []
    status = st.get("status", "never_run")
    if status == "never_run":
        reasons.append("never_run")
    elif status == "failed":
        reasons.append("previous_attempt_failed")
    elif status == "running":
        reasons.append("interrupted")
    if status == "done":
        rec = st.get("inputs") or {}
        for k, h in inputs.items():
            if k not in rec:
                reasons.append(f"input_added:{k}")
            elif rec[k] != h:
                reasons.append(f"input_changed:{k}")
        for k in rec:
            if k not in inputs:
                reasons.append(f"input_removed:{k}")
        if st.get("params_hash") != params_hash:
            reasons.append("parameter_changed")
        rop = st.get("op") or {}
        if rop.get("name") != op["name"] or rop.get("kind") != op["kind"]:
            reasons.append(f"tool_changed:{rop.get('name')}->{op['name']}")
        elif rop.get("version") != op["version"]:
            reasons.append(f"tool_version_changed:{rop.get('version')}->{op['version']}")
        now = _output_state(graph, node, st.get("outputs") or [])
        for o in st.get("outputs") or []:
            key = o.get("path") or ("artifact:" + o["artifact_id"] if "artifact_id" in o else "value")
            if "value_hash" in o:
                continue
            if now.get(key) is None:
                reasons.append(f"output_missing:{key}")
            elif now[key] != o.get("hash"):
                reasons.append(f"output_changed:{key}")
    reasons.extend(f"decision_map:{r}" for r in node.get("forced_stale") or [])
    return {"stale": bool(reasons), "reasons": reasons, "inputs": inputs, "params_hash": params_hash, "op": op,
            "key": _idem_key(graph["id"], node, op, inputs, params_hash)}


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def _targets(graph: Mapping[str, Any], target: Any) -> List[str]:
    if target in (None, "", []):
        return list(graph["nodes"])
    items = [target] if isinstance(target, str) else list(target)
    for t in items:
        if t not in graph["nodes"]:
            raise DerivationError("derivations.unknown_node", f"no node {t!r}")
    return items


def plan(owner: str, graph_id: str, target: Any = None) -> Dict[str, Any]:
    """Which nodes needed for ``target`` are stale and why. Nothing runs. A
    node whose only reason is that something upstream is stale is reported as
    ``conditional``: it stays intact if the upstream result comes out the same."""
    g = _load(owner, graph_id)
    order = _ancestors(g, _targets(g, target))
    stale_ids: set = set()
    rows = []
    for nid in order:
        node = g["nodes"][nid]
        ev = _evaluate(owner, g, node)
        reasons = list(ev["reasons"])
        conditional = False
        if not ev["stale"]:
            ups = [s["id"] for s in node["inputs"] if s["type"] == "node" and s["id"] in stale_ids]
            if ups:
                reasons = [f"upstream_stale:{u}" for u in ups]
                conditional = True
        elif "pending" in ev["inputs"].values():
            conditional = False
        if reasons:
            stale_ids.add(nid)
        rows.append({"id": nid, "stale": bool(reasons), "conditional": conditional, "reasons": reasons,
                     "op": ev["op"], "idempotency_key": ev["key"]})
    return {"graph": graph_id, "target": target, "nodes": rows,
            "stale": [r["id"] for r in rows if r["stale"]], "intact": [r["id"] for r in rows if not r["stale"]]}


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------

def _view(owner: str, graph: Mapping[str, Any], node: Mapping[str, Any], ev: Mapping[str, Any]) -> Dict[str, Any]:
    ins = []
    for spec in node["inputs"]:
        item = dict(spec)
        if spec["type"] == "file":
            item["resolved_path"] = _abs(graph, node, spec["path"])
        elif spec["type"] == "node":
            up = graph["nodes"].get(spec["id"]) or {}
            item["outputs"] = (up.get("state") or {}).get("outputs") or []
        ins.append(item)
    return {"id": node["id"], "op": ev["op"], "inputs": ins, "params": node.get("params", {}),
            "declared_outputs": node.get("outputs_declared", []), "idempotency_key": ev["key"],
            "reasons": list(ev["reasons"]), "interrupted": "interrupted" in ev["reasons"],
            "base_dir": node.get("base_dir") or graph.get("base_dir") or ""}


def _begin(lk: _Locked, nid: str, ev: Mapping[str, Any]) -> None:
    node = lk.graph["nodes"][nid]
    st = node.get("state") or {}
    st.update({"status": "running", "attempt": {"inputs": ev["inputs"], "params_hash": ev["params_hash"], "op": ev["op"],
                                                "key": ev["key"], "started": time.time()},
               "idempotency_key": ev["key"], "error": ""})
    node["state"] = st
    lk.save()


def _normalise_result(graph: Mapping[str, Any], node: Mapping[str, Any], result: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Output records (with hashes) from a runner's or caller's answer."""
    raw = result.get("outputs") if isinstance(result, Mapping) else None
    outputs = _norm_outputs(raw) if raw else list(node.get("outputs_declared", []))
    records: List[Dict[str, Any]] = []
    for o in outputs:
        if "path" in o:
            h = sha256_file(_abs(graph, node, o["path"]))
            if h is None:
                return [], f"output not produced: {o['path']}"
            records.append({"path": o["path"], "hash": h})
        else:
            h = _artifact_sha(o["artifact_id"])
            if h is None:
                return [], f"artifact not found: {o['artifact_id']}"
            records.append({"artifact_id": o["artifact_id"], "hash": h})
    if isinstance(result, Mapping) and "value" in result:
        rec = {"value_hash": value_hash(result["value"])}
        text = json.dumps(result["value"], ensure_ascii=False, default=str)
        if len(text) <= _INLINE_VALUE_LIMIT:
            rec["value"] = result["value"]
        records.append(rec)
    if not records:
        return [], "the operation declared no output and returned none"
    return records, None


def _digest(records: Sequence[Mapping[str, Any]]) -> str:
    return value_hash(sorted((r.get("path") or r.get("artifact_id") or "value", r.get("hash") or r.get("value_hash"))
                             for r in records))


def _finish(lk: _Locked, nid: str, result: Any) -> Optional[str]:
    """Record a completed attempt; returns an error text if it cannot be recorded."""
    g = lk.graph
    node = g["nodes"][nid]
    st = node.get("state") or {}
    attempt = st.get("attempt")
    if not attempt:
        raise DerivationError("derivations.not_running", f"{nid!r} has no attempt in progress; call rebuild first")
    records, err = _normalise_result(g, node, result)
    if err:
        st.update({"status": "failed", "error": err})
        node["state"] = st
        lk.save()
        return err
    st.update({"status": "done", "inputs": attempt["inputs"], "params_hash": attempt["params_hash"], "op": attempt["op"],
               "outputs": records, "output_digest": _digest(records), "idempotency_key": attempt["key"],
               "completed_at": time.time(), "error": "", "runs": int(st.get("runs") or 0) + 1})
    st.pop("attempt", None)
    node["state"] = st
    node["forced_stale"] = []
    lk.save()
    return None


def _fail(lk: _Locked, nid: str, error: str) -> None:
    node = lk.graph["nodes"][nid]
    st = node.get("state") or {}
    st.update({"status": "failed", "error": error[:500]})
    st.pop("attempt", None)
    node["state"] = st
    lk.save()


def complete_node(owner: str, graph_id: str, node_id: str, *, outputs: Optional[Sequence[Any]] = None,
                  value: Any = None, error: str = "") -> Dict[str, Any]:
    """Close the attempt ``rebuild`` opened on ``node_id``: record what it
    produced (or that it failed). Hashes are measured here, from the files."""
    with _Locked(owner, graph_id) as lk:
        if node_id not in lk.graph["nodes"]:
            raise DerivationError("derivations.unknown_node", f"no node {node_id!r}")
        if error:
            _fail(lk, node_id, error)
            return {"node": node_id, "status": "failed", "error": error}
        result: Dict[str, Any] = {}
        if outputs:
            result["outputs"] = list(outputs)
        if value is not None:
            result["value"] = value
        err = _finish(lk, node_id, result)
        if err:
            return {"node": node_id, "status": "failed", "error": err}
        return {"node": node_id, "status": "done", "outputs": lk.graph["nodes"][node_id]["state"]["outputs"]}


def rebuild(owner: str, graph_id: str, target: Any = None, *,
            runner: Optional[Callable[[Dict[str, Any]], Any]] = None, atlas: bool = True,
            atlas_client: Any = None) -> Dict[str, Any]:
    """Bring ``target`` (a node id, a list of them, or everything) up to date,
    running only what is stale. See the module docstring for the two modes."""
    try:
        run_lock = KernelFileLock(_path(owner, graph_id) + ".rebuild.lock", timeout=0.0)
        run_lock.acquire()
    except Exception:  # noqa: BLE001
        return {"status": "busy", "detail": "another rebuild of this graph is running"}
    try:
        return _rebuild_locked(owner, graph_id, target, runner, atlas, atlas_client)
    finally:
        run_lock.release()


def _rebuild_locked(owner, graph_id, target, runner, use_atlas, atlas_client) -> Dict[str, Any]:
    g0 = _load(owner, graph_id)
    order = _ancestors(g0, _targets(g0, target))
    rebuilt: List[str] = []
    reused: List[str] = []
    skipped: List[str] = []
    failed: Optional[Dict[str, str]] = None
    awaiting: Optional[Dict[str, Any]] = None
    for nid in order:
        with _Locked(owner, graph_id) as lk:
            g = lk.graph
            node = g["nodes"][nid]
            if any(s["type"] == "node" and (s["id"] in skipped or (failed and s["id"] == failed["node"]))
                   for s in node["inputs"]):
                skipped.append(nid)
                continue
            ev = _evaluate(owner, g, node)
            if not ev["stale"]:
                reused.append(nid)
                continue
            if any(h == "pending" for h in ev["inputs"].values()):
                skipped.append(nid)       # an upstream node is awaiting execution
                continue
            view = _view(owner, g, node, ev)
            _begin(lk, nid, ev)
        if runner is None:
            awaiting = view
            break
        try:
            result = runner(view)
        except Exception as exc:  # noqa: BLE001 - recorded, the rebuild stops here
            with _Locked(owner, graph_id) as lk:
                _fail(lk, nid, f"{type(exc).__name__}: {exc}")
            failed = {"node": nid, "error": f"{type(exc).__name__}: {exc}"}
            continue
        with _Locked(owner, graph_id) as lk:
            err = _finish(lk, nid, result)
        if err:
            failed = {"node": nid, "error": err}
        else:
            rebuilt.append(nid)
    out: Dict[str, Any] = {"graph": graph_id, "rebuilt": rebuilt, "reused": reused, "skipped": skipped}
    if failed:
        out.update(status="failed", failed=failed)
    elif awaiting:
        out.update(status="awaiting_execution", next=awaiting,
                   instructions="Run this node's operation with its params and inputs, then call complete_node with the "
                                "files it produced. The idempotency key identifies this execution.")
    else:
        out["status"] = "up_to_date" if not rebuilt else "rebuilt"
    if use_atlas and rebuilt:
        out["atlas"] = sync_atlas(owner, graph_id, client=atlas_client)
    return out


# ---------------------------------------------------------------------------
# Atlas
# ---------------------------------------------------------------------------

def set_atlas(owner: str, graph_id: str, project_id: str) -> Dict[str, Any]:
    with _Locked(owner, graph_id) as lk:
        lk.graph["atlas"] = {"project_id": str(project_id)} if project_id else {}
        lk.save()
        return dict(lk.graph["atlas"])


def _atlas_client() -> Any:
    from src import family_services
    from src.hoard_link import fam_workspace
    family_services.configure()
    return fam_workspace


def _inside(path: str, root: str) -> bool:
    try:
        a, b = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


def sync_atlas(owner: str, graph_id: str, *, client: Any = None) -> Dict[str, Any]:
    """Register the finished nodes' results in the graph's Atlas project when
    there is one and Atlas answers: files inside the project's shared folder are
    registered, and each node with such sources and one such output is
    published with its operation, version and parameters as the recipe. Never
    raises: the local records are the source of truth and the answer says
    whether Atlas was reached."""
    g = _load(owner, graph_id)
    project_id = (g.get("atlas") or {}).get("project_id")
    if not project_id:
        return {"registered": 0, "local_only": True, "reason": "no Atlas project configured for this graph"}
    try:
        atlas = client or _atlas_client()
        loc = atlas.location(project_id, area="shared")
    except Exception as exc:  # noqa: BLE001
        return {"registered": 0, "local_only": True, "reason": f"Atlas not reachable: {type(exc).__name__}: {exc}"}
    if not isinstance(loc, Mapping) or loc.get("ok") is False or not loc.get("path"):
        why = (loc or {}).get("error") if isinstance(loc, Mapping) else "no answer"
        return {"registered": 0, "local_only": True, "reason": f"Atlas not reachable: {why or 'no folder returned'}"}
    shared = str(loc["path"])
    project_root = os.path.dirname(shared.rstrip("\\/"))
    registered = 0
    skipped: List[Dict[str, str]] = []
    cache: Dict[str, Dict[str, Any]] = {}

    def register(abs_path: str) -> Optional[Dict[str, Any]]:
        if abs_path in cache:
            return cache[abs_path]
        if not _inside(abs_path, shared):
            return None
        rel = os.path.relpath(os.path.realpath(abs_path), os.path.realpath(project_root)).replace("\\", "/")
        res = atlas.register(project_id, rel, title=os.path.basename(abs_path))
        f = res.get("file") if isinstance(res, Mapping) else None
        if not isinstance(f, Mapping) or not f.get("id"):
            return None
        cache[abs_path] = dict(f)
        return cache[abs_path]

    with _Locked(owner, graph_id) as lk:
        graph = lk.graph
        for nid, node in graph["nodes"].items():
            st = node.get("state") or {}
            if st.get("status") != "done" or (node.get("atlas") or {}).get("key") == st.get("idempotency_key"):
                continue
            sources, why = [], ""
            for spec in node["inputs"]:
                paths = []
                if spec["type"] == "file":
                    paths = [_abs(graph, node, spec["path"])]
                elif spec["type"] == "node":
                    up = graph["nodes"].get(spec["id"]) or {}
                    paths = [_abs(graph, up, o["path"]) for o in (up.get("state") or {}).get("outputs", []) if "path" in o]
                for p in paths:
                    f = register(p)
                    if f is None:
                        why = f"{os.path.basename(p)} is outside the Atlas project folder"
                        break
                    sources.append(f)
                if why:
                    break
            outs = [o for o in st.get("outputs", []) if "path" in o]
            if not why and len(outs) != 1:
                why = "Atlas publishes one output file per result"
            out_file = None
            if not why:
                out_file = register(_abs(graph, node, outs[0]["path"]))
                if out_file is None:
                    why = "output is outside the Atlas project folder"
            if not why and not sources:
                why = "no source files to register"
            if why:
                skipped.append({"node": nid, "reason": why})
                continue
            uniq = {f["id"]: f for f in sources}
            recipe = {"operation": f"{node['op']['kind']}:{node['op']['name']}",
                      "version": st["op"].get("version") or "unversioned", "parameters": node.get("params", {}),
                      "node": nid, "idempotency_key": st.get("idempotency_key")}
            res = atlas.publish(project_id, list(uniq), recipe, out_file["id"],
                                source_revisions={i: f.get("revision") for i, f in uniq.items()})
            if isinstance(res, Mapping) and res.get("ok") is not False and res.get("cache_key"):
                node["atlas"] = {"key": st.get("idempotency_key"), "cache_key": res["cache_key"], "project_id": project_id}
                registered += 1
            else:
                skipped.append({"node": nid, "reason": str((res or {}).get("error") or "Atlas refused the result")})
        lk.save()
    return {"registered": registered, "local_only": registered == 0 and bool(skipped), "skipped": skipped,
            "project_id": project_id}


# ---------------------------------------------------------------------------
# decision and effect map
# ---------------------------------------------------------------------------

def _ref_ok(g: Mapping[str, Any], ref: str) -> bool:
    if ref.startswith("node:"):
        return ref[5:] in g["nodes"]
    return ref in g["map"]["items"]


def map_add(owner: str, graph_id: str, item_id: str, kind: str, title: str = "", note: str = "",
            path: str = "") -> Dict[str, Any]:
    if kind not in ITEM_KINDS:
        raise DerivationError("derivations.bad_kind", f"kind must be one of {', '.join(ITEM_KINDS)}")
    if not item_id or item_id.startswith("node:") or len(item_id) > 120:
        raise DerivationError("derivations.bad_id", "item id: up to 120 characters, not starting with 'node:'")
    with _Locked(owner, graph_id) as lk:
        item = {"id": item_id, "kind": kind, "title": title or item_id, "note": note}
        if path:
            item["path"] = path
        lk.graph["map"]["items"][item_id] = item
        lk.save()
        return item


def map_link(owner: str, graph_id: str, source: str, target: str, relation: str = "") -> Dict[str, Any]:
    """An explicit link ``source -> target``: ``source`` is the thing that, when
    it changes, makes ``target`` need another look. Either end may be an item id
    or ``node:<id>``."""
    with _Locked(owner, graph_id) as lk:
        g = lk.graph
        for ref in (source, target):
            if not _ref_ok(g, ref):
                raise DerivationError("derivations.unknown_ref", f"{ref!r} is not an item or node of this graph")
        if source == target:
            raise DerivationError("derivations.bad_link", "a link needs two different ends")
        link = {"from": source, "to": target, "relation": relation}
        if not any(l["from"] == source and l["to"] == target for l in g["map"]["links"]):
            g["map"]["links"].append(link)
            lk.save()
        return link


def map_unlink(owner: str, graph_id: str, source: str, target: str) -> Dict[str, Any]:
    with _Locked(owner, graph_id) as lk:
        before = len(lk.graph["map"]["links"])
        lk.graph["map"]["links"] = [l for l in lk.graph["map"]["links"] if not (l["from"] == source and l["to"] == target)]
        lk.save()
        return {"removed": before - len(lk.graph["map"]["links"])}


def map_remove(owner: str, graph_id: str, item_id: str) -> Dict[str, Any]:
    with _Locked(owner, graph_id) as lk:
        g = lk.graph
        if item_id not in g["map"]["items"]:
            raise DerivationError("derivations.unknown_ref", f"no item {item_id!r}")
        g["map"]["items"].pop(item_id)
        g["map"]["links"] = [l for l in g["map"]["links"] if item_id not in (l["from"], l["to"])]
        lk.save()
    return {"removed": item_id}


def map_show(owner: str, graph_id: str) -> Dict[str, Any]:
    g = _load(owner, graph_id)
    return {"graph": graph_id, "items": list(g["map"]["items"].values()), "links": g["map"]["links"],
            "nodes": [_public(g, n) for n in g["nodes"].values()]}


def _resolve_change(g: Mapping[str, Any], change: str) -> List[str]:
    if _ref_ok(g, change):
        return [change]
    low = change.strip().lower()
    return [i["id"] for i in g["map"]["items"].values() if i["title"].strip().lower() == low]


def affected_by(owner: str, graph_id: str, change: str, *, invalidate: bool = False) -> Dict[str, Any]:
    """What must be recalculated or re-checked when ``change`` (an item id,
    ``node:<id>`` or an item's exact title) changes. Follows the explicit links
    and the derivation inputs; nothing else. With ``invalidate`` the affected
    derivation nodes are flagged so the next rebuild re-runs them."""
    ctx = _Locked(owner, graph_id) if invalidate else None
    if ctx:
        ctx.__enter__()
        g = ctx.graph
    else:
        g = _load(owner, graph_id)
    try:
        starts = _resolve_change(g, change)
        if not starts:
            raise DerivationError("derivations.unknown_ref", f"{change!r} is not an item, a node or an item title in this graph")
        edges: Dict[str, List[Tuple[str, str]]] = {}
        for l in g["map"]["links"]:
            edges.setdefault(l["from"], []).append((l["to"], l.get("relation") or "linked"))
        for nid, node in g["nodes"].items():
            for s in node["inputs"]:
                if s["type"] == "node":
                    edges.setdefault(f"node:{s['id']}", []).append((f"node:{nid}", "input of"))
        via: Dict[str, List[str]] = {s: [s] for s in starts}
        frontier = list(starts)
        while frontier:
            cur = frontier.pop(0)
            for nxt, rel in edges.get(cur, []):
                if nxt not in via:
                    via[nxt] = via[cur] + [f"-[{rel}]->", nxt]
                    frontier.append(nxt)
        affected = []
        for ref, path in via.items():
            if ref in starts:
                continue
            if ref.startswith("node:"):
                affected.append({"id": ref, "kind": "derivation", "title": ref[5:], "via": path})
            else:
                it = g["map"]["items"][ref]
                affected.append({"id": ref, "kind": it["kind"], "title": it["title"], "via": path,
                                 **({"path": it["path"]} if it.get("path") else {})})
        node_ids = [a["id"][5:] for a in affected if a["kind"] == "derivation"]
        ordered = [n for n in _ancestors(g, node_ids) if n in node_ids] if node_ids else []
        if invalidate:
            for nid in ordered:
                reasons = g["nodes"][nid].setdefault("forced_stale", [])
                text = f"{change} changed"
                if text not in reasons:
                    reasons.append(text)
            ctx.save()
        return {"change": change, "resolved": starts, "affected": affected, "recalculate": ordered,
                "deliverables": [a for a in affected if a["kind"] == "deliverable"],
                "invalidated": ordered if invalidate else [],
                "note": "" if affected else "no explicit link leaves this item; nothing is known to depend on it"}
    finally:
        if ctx:
            ctx.__exit__(None, None, None)


def show(owner: str, graph_id: str = "default") -> Dict[str, Any]:
    g = _load(owner, graph_id)
    return {"graph": graph_id, "base_dir": g.get("base_dir", ""), "atlas": g.get("atlas") or {},
            "nodes": [_public(g, n) for n in g["nodes"].values()],
            "map": {"items": len(g["map"]["items"]), "links": len(g["map"]["links"])}}


def set_base_dir(owner: str, graph_id: str, base_dir: str) -> Dict[str, Any]:
    with _Locked(owner, graph_id) as lk:
        lk.graph["base_dir"] = str(base_dir or "")
        lk.save()
        return {"base_dir": lk.graph["base_dir"]}
