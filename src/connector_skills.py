"""The skills the connected apps carry, and how a turn picks them up.

A family app hands out procedures from the skill library for the work it does:
the tools ``skills_list`` / ``skill_read`` and one prompt ``skill-<slug>`` per
skill. Faustus used to list only the tools. This module keeps one small catalogue
per connector (slug, name, "Use when..." line, version), collected when the
connector connects or its tools are refreshed and never per turn, and answers
three questions with it:

* which connectors are in play for a turn (one of their tools was retrieved, or
  the request names the app),
* which of their skills are worth showing the model (a one-line catalogue; the
  procedure itself is read on demand with the connector's own ``skill_read``),
* which library skills a connector also serves (one entry, marked as served by
  the connector too).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

#: The tools an app adds for its shelf. They are not retrieved by meaning (forty
#: apps carry the same five); a turn that has an app in play gets ``skill_read``.
SKILL_SHELF_TOOLS = frozenset({"skills_list", "skill_read", "skills_refresh", "skills_library", "skills_assign"})

#: How many skills one turn shows per connector in play, and in all.
PER_CONNECTOR = 2
PER_TURN = 4
DESCRIPTION_CHARS = 170

_catalogs: Dict[str, Dict[str, Any]] = {}
_pending: Dict[str, "asyncio.Task[Any]"] = {}


def is_shelf_tool(qualified_name: str) -> bool:
    """``mcp__<server>__skills_list`` and its siblings."""
    name = str(qualified_name or "")
    if not name.startswith("mcp__"):
        return False
    _, _, tool = name[5:].partition("__")
    return tool in SKILL_SHELF_TOOLS


def _split(qualified_name: str):
    parts = str(qualified_name or "").split("__", 2)
    return (parts[1], parts[2]) if len(parts) == 3 and parts[0] == "mcp" else ("", "")


# ── catalogue (per connector, cached) ────────────────────────────────────────

def catalog(server_id: str) -> Optional[Dict[str, Any]]:
    entry = _catalogs.get(server_id)
    return dict(entry) if entry else None


def catalogs() -> Dict[str, Dict[str, Any]]:
    return {sid: dict(e) for sid, e in _catalogs.items()}


def drop(server_id: str) -> None:
    _catalogs.pop(server_id, None)
    task = _pending.pop(server_id, None)
    if task is not None and not task.done():
        task.cancel()


def reset() -> None:
    for sid in list(_catalogs):
        drop(sid)
    _catalogs.clear()


def _trigger(description: str) -> str:
    from src.skills_runtime.selector import extract_trigger
    return extract_trigger(description or "") or ""


def _norm_row(row: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    slug = str(row.get("slug") or "").strip().lower().removeprefix("skill-")
    if not slug:
        return None
    description = " ".join(str(row.get("description") or "").split())
    return {
        "slug": slug,
        "name": str(row.get("name") or slug),
        "description": description,
        "when_to_use": _trigger(description) or description,
        "version": row.get("version"),
        "category": row.get("category") or "",
        "tags": [str(t) for t in (row.get("tags") or []) if t],
        "files": [str(f) for f in (row.get("files") or []) if f],
        "source": source,
    }


def _connector_of(server_id: str) -> Dict[str, Any]:
    try:
        from src import connector_sidecar
        entry = connector_sidecar.get_connector_for_server(server_id) or {}
    except Exception:  # noqa: BLE001 - a missing sidecar only costs the aliases
        entry = {}
    return {"connector_id": entry.get("id"), "preset_id": entry.get("preset_id")}


def _library_slugs() -> Set[str]:
    try:
        from src.skill_library import _library_slugs as slugs
        return set(slugs())
    except Exception:  # noqa: BLE001
        return set()


def _mark_library(rows: List[Dict[str, Any]]) -> None:
    library = _library_slugs()
    for row in rows:
        row["in_library"] = row["slug"] in library


async def app_is_up(meta: Dict[str, Any]) -> bool:
    """True when the connector's app answers its health check (through the Hub's
    relay when Faustus lives elsewhere). A family bridge starts a closed app on
    the first call that reaches it, and some apps open hardware when they start,
    so reading a shelf never goes through to an app that is off. Connectors that
    are not family apps have nothing of ours to start."""
    connector_id, preset_id = meta.get("connector_id"), meta.get("preset_id")
    if not connector_id or not preset_id:
        return True
    try:
        from src import connector_relay, connector_sidecar, connector_status, connectors
        remote = await connector_relay.describe(preset_id, check=True)
        if remote is not None:
            return bool((remote.get("health") or {}).get("reachable"))
        preset = connectors.get_preset(preset_id)
        entry = connector_sidecar.get_connector(connector_id, redact=False) or {}
        resolved = connectors.resolve_preset_values(preset, entry.get("values") or {}) if preset else {}
        if preset is None or not resolved.get("ok"):
            return False
        health = await connector_status._fetch_health(resolved["app_url"], preset.health_path)
        return bool(health.get("reachable"))
    except Exception as exc:  # noqa: BLE001 - not knowing counts as "do not touch it"
        logger.debug("[connector-skills] health of %s unknown: %s", preset_id, exc)
        return False


async def collect(manager: Any, server_id: str, *, timeout: float = 15.0) -> Dict[str, Any]:
    """Read the skills of one connected connector and keep them. Never raises:
    a connector without skills gets an empty catalogue, one that failed keeps
    its error so the screen can say so. A connector whose app is off is not
    asked (see `app_is_up`); its catalogue says so and fills in at the next
    connect or refresh once the app is up."""
    session = manager._sessions.get(server_id)
    names = {t.get("name") for t in manager._tools.get(server_id, [])}
    conn = manager._connections.get(server_id) or {}
    meta = _connector_of(server_id)
    entry: Dict[str, Any] = {
        "server_id": server_id, "server_name": conn.get("name") or server_id, **meta,
        "skills": [], "source": "none", "error": None, "collected_at": time.time(),
    }
    if session is None:
        entry["error"] = "not connected"
        return entry
    if not await app_is_up(meta):
        entry["error"] = "app not running"
        entry["skipped"] = True
        old = _catalogs.get(server_id)
        if old and old.get("skills"):          # what was collected while it was up stays
            entry["skills"], entry["source"] = old["skills"], old.get("source", "none")
        _catalogs[server_id] = entry
        return dict(entry)
    try:
        if "skills_list" in names:
            result = await manager._do_call(session, "skills_list", {}, timeout_s=timeout)
            if result.get("exit_code"):
                raise RuntimeError(str(result.get("stderr") or "skills_list failed")[:300])
            data = json.loads(result.get("stdout") or "{}")
            rows = data.get("skills") if isinstance(data, dict) else data
            entry["skills"] = [r for r in (_norm_row(x, "tool") for x in rows or [] if isinstance(x, dict)) if r]
            entry["source"] = "tool"
        elif meta.get("connector_id"):
            prompts = await asyncio.wait_for(session.list_prompts(), timeout=timeout)
            rows = [{"slug": p.name, "description": getattr(p, "description", "") or ""}
                    for p in getattr(prompts, "prompts", []) or [] if str(p.name).startswith("skill-")]
            entry["skills"] = [r for r in (_norm_row(x, "prompt") for x in rows) if r]
            entry["source"] = "prompt" if entry["skills"] else "none"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - no prompts capability, bridge older than the shelf, a timeout
        entry["error"] = (str(exc) or type(exc).__name__)[:300]
        logger.debug("[connector-skills] %s: %s", server_id, entry["error"])
    _mark_library(entry["skills"])
    _catalogs[server_id] = entry
    return dict(entry)


def schedule(manager: Any, server_id: str) -> None:
    """Collect in the background after a connect or a tools refresh. The result
    replaces the cached catalogue; a turn never waits for it."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    old = _pending.get(server_id)
    if old is not None and not old.done():
        return

    async def run() -> None:
        for _ in range(20):               # the session is stored just after discovery
            if manager._sessions.get(server_id) is not None:
                break
            await asyncio.sleep(0.25)
        try:
            await collect(manager, server_id)
        finally:
            _pending.pop(server_id, None)

    _pending[server_id] = loop.create_task(run(), name=f"connector-skills-{server_id}")


async def ensure(manager: Any, server_id: str) -> Dict[str, Any]:
    entry = _catalogs.get(server_id)
    if entry is None:
        return await collect(manager, server_id)
    return dict(entry)


async def read(manager: Any, server_id: str, slug: str, file: str = "") -> Dict[str, Any]:
    """The text of one skill, from the connector that carries it."""
    session = manager._sessions.get(server_id)
    if session is None:
        return {"ok": False, "error": "connector not connected"}
    slug = str(slug or "").strip().lower().removeprefix("skill-")
    names = {t.get("name") for t in manager._tools.get(server_id, [])}
    try:
        if "skill_read" in names:
            args = {"slug": slug, **({"file": file} if file else {})}
            result = await manager._do_call(session, "skill_read", args, timeout_s=20)
            if result.get("exit_code"):
                return {"ok": False, "error": str(result.get("stderr") or "skill_read failed")[:400]}
            data = json.loads(result.get("stdout") or "{}")
            return {"ok": True, "server_id": server_id, **(data if isinstance(data, dict) else {"text": str(data)})}
        got = await asyncio.wait_for(session.get_prompt("skill-" + slug), timeout=20)
        text = "\n".join(getattr(m.content, "text", "") for m in getattr(got, "messages", []) if hasattr(m, "content"))
        return {"ok": True, "server_id": server_id, "slug": slug, "file": "SKILL.md", "text": text}
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": (str(exc) or type(exc).__name__)[:400]}


def listing(manager: Any = None) -> List[Dict[str, Any]]:
    """One row per connected connector that has a catalogue, for the API."""
    rows = []
    for sid, entry in sorted(_catalogs.items()):
        if manager is not None and manager._sessions.get(sid) is None:
            continue
        rows.append({k: entry.get(k) for k in ("server_id", "server_name", "connector_id", "preset_id", "source", "error", "collected_at")}
                    | {"count": len(entry["skills"]), "skills": entry["skills"]})
    return rows


def served_by() -> Dict[str, List[Dict[str, Any]]]:
    """slug -> the connectors that carry it (for marking library skills)."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for sid, entry in _catalogs.items():
        for row in entry["skills"]:
            out.setdefault(row["slug"], []).append({
                "server_id": sid, "server_name": entry["server_name"], "connector_id": entry.get("connector_id")})
    return out


# ── a turn ───────────────────────────────────────────────────────────────────

_GENERIC = {"hoard", "hub", "link", "built-in", "builtin", "mcp", "server"}


def _aliases(entry: Dict[str, Any]) -> Set[str]:
    out: Set[str] = set()
    name = str(entry.get("server_name") or "").lower()
    name = re.sub(r"^built-in:\s*", "", name)
    base = re.sub(r"(?:'s|’s)?\s+hoard$", "", name).strip()
    for alias in (base, name, str(entry.get("preset_id") or "").lower()):
        alias = alias.strip()
        if len(alias) >= 3 and alias not in _GENERIC:
            out.add(alias)
    return out


def names_app(entry: Dict[str, Any], query: str) -> bool:
    low = str(query or "").lower()
    return any(re.search(r"(?<![\w])" + re.escape(a) + r"(?:'s|’s)?(?![\w])", low) for a in _aliases(entry))


def servers_in_play(relevant_tools: Optional[Iterable[str]], query: str) -> List[str]:
    """Connectors with skills whose tools are in the turn's set or that the request names."""
    in_set: Set[str] = set()
    for name in relevant_tools or ():
        sid, tool = _split(name)
        if sid and tool not in SKILL_SHELF_TOOLS:
            in_set.add(sid)
    out = []
    for sid, entry in _catalogs.items():
        if entry["skills"] and (sid in in_set or names_app(entry, query)):
            out.append(sid)
    return sorted(out)


def _local_by_slug(local_skills: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """library slug (or plain name) -> the local skill's name."""
    out: Dict[str, str] = {}
    for sk in local_skills or ():
        name = str(sk.get("name") or "")
        out.setdefault(name.lower(), name)
        for tag in sk.get("tags") or []:
            if isinstance(tag, str) and tag.startswith("library:"):
                out.setdefault(tag[len("library:"):].lower(), name)
    return out


class TurnPlan:
    """What a turn gets from the connectors in play."""

    def __init__(self) -> None:
        self.servers: List[str] = []
        self.entries: List[Dict[str, Any]] = []
        self.skill_tools: Set[str] = set()
        self.boost: Dict[str, float] = {}
        self.text: str = ""

    def __bool__(self) -> bool:
        return bool(self.servers)


def plan_turn(query: str, relevant_tools: Optional[Iterable[str]], *, owner: Optional[str] = None,
              local_skills: Optional[List[Dict[str, Any]]] = None, sm: Any = None) -> TurnPlan:
    plan = TurnPlan()
    if not _catalogs or not str(query or "").strip():
        return plan
    plan.servers = servers_in_play(relevant_tools, query)
    if not plan.servers:
        return plan
    local = _local_by_slug(local_skills or [])
    chosen: List[Dict[str, Any]] = []
    for sid in plan.servers:
        cat = _catalogs[sid]
        plan.skill_tools.add(f"mcp__{sid}__skill_read")
        mine = cat["skills"]
        # A library skill the connector also serves stays ONE entry, the local one:
        # it is ranked up here and not listed a second time.
        for row in mine:
            if row["slug"] in local:
                plan.boost[local[row["slug"]]] = 1.5
        fresh = [r for r in mine if r["slug"] not in local]
        for row in _rank(sm, owner, query, sid, fresh)[:PER_CONNECTOR]:
            chosen.append({**row, "server_id": sid, "server_name": cat["server_name"]})
    plan.entries = chosen[:PER_TURN]
    plan.text = render(plan.entries, plan.servers)
    return plan


def _rank(sm: Any, owner: Optional[str], query: str, sid: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(rows) <= 1 or sm is None:
        return list(rows)
    pseudo = [{
        "id": f"connector:{sid}:{r['slug']}", "name": r["slug"], "description": r["description"],
        "when_to_use": r["when_to_use"], "tags": r["tags"], "status": "published", "owner": owner,
    } for r in rows]
    try:
        from src.skills_runtime import selector
        ranked = selector.select(sm, owner, query, skills=pseudo, threshold=0.0, max_items=len(pseudo))
        order = {sk["name"]: i for i, sk in enumerate(ranked)}
        return sorted(rows, key=lambda r: order.get(r["slug"], len(order)))
    except Exception:  # noqa: BLE001 - ranking is a nicety; the catalogue order still works
        logger.debug("[connector-skills] ranking failed", exc_info=True)
        return list(rows)


def render(entries: List[Dict[str, Any]], servers: List[str]) -> str:
    if not entries:
        return ""
    lines = ["## Skills carried by the apps in play",
             "These apps bring procedures for the work they do. Before using the app's tools for a task one of them "
             "covers, read it with that app's `skill_read` (slug) and follow it; it is read on demand, not pasted here."]
    for row in entries:
        desc = row["when_to_use"] or row["description"]
        if len(desc) > DESCRIPTION_CHARS:
            desc = desc[:DESCRIPTION_CHARS - 1].rstrip() + "…"
        lines.append(f"- {row['slug']} ({row['server_name']}, `mcp__{row['server_id']}__skill_read`): {desc}")
    return "\n".join(lines)
