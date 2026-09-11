"""Agent profiles over HTTP — /api/agent-profiles/* (src/agent_profiles/).

`routes/agent_def_routes.py` already answers "what may this agent do". These
routes answer the orthogonal half the profiles plan added: how far a mission
pushes, which versioned policies it references, and — the one that matters —
what the EFFECTIVE configuration of a run would be before anything runs.

Three rules run through every handler, each of them a specific failure:

* **`POST /resolve` is a preview and takes no identity from the body.** The
  owner comes from the session and the project from the session's own binding
  (`services/projects.py`), exactly as `POST /api/context/compile` does it. A
  resolver that accepted `owner` from JSON would be a privilege escalation
  wearing a diagnostic's clothes: work roots, project defaults and every
  restriction that hangs off them are decided by that field. Anything the body
  sends under `owner` or `project_id` is ignored and NAMED in the answer, so a
  caller does not spend an afternoon wondering why their override did nothing.

* **A refusal is an answer, not a failure.** An unknown slug, a profile id this
  build does not have: each is a 200 with `{"ok": false, "error": {"path",
  "message"}}`, the convention `routes/contracts_routes.py` and
  `routes/context_engine_routes.py` already keep. The 4xx codes stay reserved
  for a body that is not JSON — a caller that cannot tell "your input was
  refused" from "your request was malformed" retries the wrong one.

* **Nothing here resolves anything itself.** Every answer is
  `src/agent_profiles/resolver.py` speaking; this file reads the session, calls
  it, and serialises. A second resolution path reachable over HTTP would be a
  second answer to "what did this run start from", which is one more than the
  plan can keep honest.

Admin-only, like the definitions API: a resolution says what a worker on this
machine may do, and the repo lane reads files out of the linked folder.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Mapping, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from core.middleware import require_admin
from src.agent_profiles import catalog, completion, resolver
from src.agent_profiles.contracts import (
    CAPABILITIES, COMPLETION_MODES, PRECEDENCE, PROFILE_KINDS, ProfileError,
)

logger = logging.getLogger(__name__)

#: Fields a caller may not set on a preview. They are not rejected — a body
#: that carries them still resolves — but they are ignored and listed back, so
#: the difference between "you cannot set this" and "this quietly did nothing"
#: is visible from the answer alone.
IDENTITY_FIELDS = ("owner", "project_id")


def _owner(request: Request) -> str:
    """Who the caller is, for scoping. `effective_user` rather than
    `get_current_user`, so a paired client resolves as the human who minted its
    token instead of as a separate `api` silo."""
    from src.auth_helpers import effective_user

    return str(effective_user(request) or "").strip()


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return payload


def _refused(path: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "error": {"path": str(path), "message": str(message)}}


def _map(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _project_scope(session_id: str, owner: str) -> Dict[str, Any]:
    """The project this preview resolves inside, from the SESSION.

    Never from the body (§1.6: `project_id`, owner and trust are never accepted
    from outside the runtime). A chat with no project yields an empty
    `project_id` and no work roots, which grants nothing — the honest answer for
    a resolution that has no project to inherit.
    """
    scope: Dict[str, Any] = {"owner": owner, "project_id": "", "session_id": session_id}
    defaults: Dict[str, Any] = {}
    if not session_id:
        return {"scope": scope, "project_defaults": defaults, "workspace": ""}
    try:
        from services.projects import project_context_for_session, work_roots_for_session

        context = project_context_for_session(session_id, owner or None)
        scope["project_id"] = context.project_id
        roots = work_roots_for_session(session_id, owner or None)
        if roots:
            defaults["work_roots"] = list(roots)
        return {"scope": scope, "project_defaults": defaults, "workspace": context.workspace or ""}
    except Exception as exc:  # noqa: BLE001 - no project store is not a failure
        logger.debug("agent-profiles: project lookup failed for %s: %s", session_id, exc)
        return {"scope": scope, "project_defaults": defaults, "workspace": ""}


def _packs() -> Optional[List[Dict[str, Any]]]:
    """Every agent pack this build ships, or `None` when packs are not built yet.

    Probed rather than imported at module level: `packs.py` lands in this
    package on its own schedule, and a route file that will not import because
    a sibling module does not exist yet would take the definitions API down
    with it. `None` and `[]` are different answers and the handler says which.
    """
    try:
        from src.agent_profiles import packs  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    for name in ("list_packs", "all_packs", "as_dicts"):
        reader = getattr(packs, name, None)
        if callable(reader):
            try:
                rows = reader()
                return [dict(r) if isinstance(r, Mapping) else _pack_row(r) for r in rows or ()]
            except Exception:  # noqa: BLE001
                continue
    registry = getattr(packs, "PACKS", None)
    if isinstance(registry, Mapping):
        return [_pack_row(row) for row in registry.values()]
    return []


def _pack_row(pack: Any) -> Dict[str, Any]:
    """One pack as plain data, whatever shape it turned out to have."""
    if isinstance(pack, Mapping):
        return dict(pack)
    reader = getattr(pack, "to_dict", None)
    if callable(reader):
        try:
            return dict(reader())
        except Exception:  # noqa: BLE001
            pass
    from dataclasses import fields, is_dataclass

    if is_dataclass(pack):
        return {f.name: getattr(pack, f.name) for f in fields(pack)}
    return {"id": str(getattr(pack, "id", "") or getattr(pack, "pack", "") or "")}


def _definitions(workspace: Optional[str]) -> Dict[str, Any]:
    """Every definition with the fields the profiles plan added, plus the files
    that would not load. `agent_defs.to_dict()` already carries all of them, so
    nothing is re-derived here."""
    from src import agent_defs

    result = agent_defs.load_all(workspace)
    return {"agents": [d.to_dict() for d in result.agents], "errors": list(result.errors)}


def _one(slug: str, workspace: Optional[str]) -> Dict[str, Any]:
    """One definition, with the CONTENT of every profile it references.

    The ids alone are what the record stores (§4: a reference, never a copy),
    but a page that showed `full_delivery_v1` and nothing else would be asking
    its reader to hold the catalogue in their head. So the ids stay the record
    and the contents ride along, read from the catalogue at request time.
    """
    from src import agent_defs

    result = agent_defs.load_all(workspace)
    definition = result.by_slug().get(agent_defs.clean_slug(slug))
    if definition is None:
        known = ", ".join(sorted(result.by_slug())[:12])
        return _refused("slug", "no agent definition called `{}`{}".format(
            slug, ". Known: " + known if known else ""))
    row = definition.to_dict()
    row["rules"] = agent_defs.explain(definition)
    profiles: Dict[str, Any] = {}
    for kind, key in (("verification", "verification_profile"), ("context", "context_profile"),
                      ("budget", "budget_profile"), ("collaboration", "collaboration_profile"),
                      ("output", "output_contract")):
        wanted = str(row.get(key) or "")
        if not wanted:
            continue
        found = catalog.get(kind, wanted)
        profiles[kind] = ({f: getattr(found, f) for f in found.__dataclass_fields__}
                          if found is not None else
                          {"id": wanted, "missing": True,
                           "reason": "no such {} profile in this build".format(kind)})
    return {"ok": True, "agent": row, "profiles": profiles}


def _resolution_payload(body: Mapping[str, Any], owner: str) -> Dict[str, Any]:
    """The preview itself. Pure apart from reading the definition store."""
    from src import agent_defs

    session_id = str(body.get("session_id") or "").strip()[:120]
    place = _project_scope(session_id, owner)
    index = agent_defs.load_all(place["workspace"] or None).by_slug()
    wanted = agent_defs.clean_slug(body.get("agent"))
    if str(body.get("agent") or "").strip() and wanted not in index:
        # A preview of an agent that does not exist must not quietly become a
        # preview of a different one: the answer names the field, as every
        # other refusal in this repo does.
        known = ", ".join(sorted(index)[:12])
        return _refused("agent", "no agent definition called `{}`{}".format(
            str(body.get("agent")).strip()[:80], ". Known: " + known if known else ""))
    ignored = [f for f in IDENTITY_FIELDS if f in body]
    defaults = dict(place["project_defaults"])
    for key, value in _map(body.get("project_defaults")).items():
        # A caller may state the project's DEFAULTS (a completion mode, a
        # budget profile) but never its identity or its roots: those two decide
        # what a run may touch and they come from the binding, not from JSON.
        if key in ("work_roots", "project_id", "owner"):
            ignored.append("project_defaults." + key)
            continue
        defaults[key] = value
    resolution = resolver.resolve(
        agent=wanted,
        defs=index,
        task=_map(body.get("task")),
        activity=_map(body.get("activity")),
        scope=place["scope"],
        project_defaults=defaults,
        owner_policy=_map(body.get("owner_policy")),
        available_models=[str(m) for m in (body.get("available_models") or ())],
        available_runners=[str(r) for r in (body.get("available_runners") or ())],
        unhealthy=[str(x) for x in (body.get("unhealthy") or ())],
        quarantined=[str(x) for x in (body.get("quarantined") or ())],
        instruction=str(body.get("instruction") or "")[:8000],
        workspace=place["workspace"] or None,
    )
    return {
        "ok": True,
        "resolution": resolution.to_dict(),
        "identity": resolution.identity(),
        "explain": resolver.explain(resolution),
        "rows": resolver.explain_rows(resolution),
        # Named, not silently dropped: "this endpoint does not take your owner"
        # is a different sentence from "your override did nothing".
        "ignored_fields": sorted(set(ignored)),
        "preview": True,
    }


# ── the router ─────────────────────────────────────────────────────────────

def setup_agent_profiles_routes() -> APIRouter:
    router = APIRouter(prefix="/api/agent-profiles", tags=["agent-profiles"])

    # The literal paths are declared BEFORE `/{slug}`: FastAPI matches in
    # declaration order, and a catch-all above them would serve `/catalog` as a
    # definition called "catalog" and answer `ok: false` to a working route.

    @router.get("/catalog")
    async def read_catalog(request: Request) -> Dict[str, Any]:
        """The five catalogues (§9-§13), as data.

        Ids are versioned and the meaning of one belongs to whoever owns it —
        this is the list, not a second definition of it."""
        require_admin(request)
        return {"ok": True, "kinds": list(PROFILE_KINDS),
                "profiles": {kind: catalog.list_profiles(kind) for kind in PROFILE_KINDS},
                "write_policies": list(catalog.WRITE_POLICIES),
                "speak_policies": list(catalog.SPEAK_POLICIES),
                "capabilities": list(CAPABILITIES)}

    @router.get("/completion-modes")
    async def read_modes(request: Request) -> Dict[str, Any]:
        """The four modes, their versioned policies, and the ladder that picks
        one. `precedence` is `§1.6`; `mode_precedence` is the six levels a MODE
        may come from — the three permission levels are absent on purpose,
        because a mode is depth and never authority (§3.3)."""
        require_admin(request)
        return {
            "ok": True,
            "modes": [{field: getattr(policy, field) for field in policy.__dataclass_fields__}
                      for policy in (completion.POLICIES[name] for name in completion.MODE_ORDER)],
            "order": list(completion.MODE_ORDER),
            "known": list(COMPLETION_MODES),
            "mode_precedence": list(completion.MODE_PRECEDENCE),
            "precedence": list(PRECEDENCE),
            "grants_nothing": "A completion mode is depth, never authority: it names no tool, no "
                              "path, no effect and no work root, and cannot.",
        }

    @router.get("/packs")
    async def read_packs(request: Request) -> Dict[str, Any]:
        """Agent packs (§17). A pack groups definitions; it never raises the
        permissions of its members (§23)."""
        require_admin(request)
        rows = _packs()
        if rows is None:
            return {"ok": True, "packs": [],
                    "degraded": ["agent_packs: not in this build yet"]}
        return {"ok": True, "packs": rows, "degraded": []}

    @router.get("/lint")
    async def lint_catalog(request: Request) -> Dict[str, Any]:
        """Lote A4: every profile in the catalogue, linted (§9-§13).

        These are legal-but-suspicious configurations, not schema errors —
        a schema error is already refused at `catalog.register` time and
        never reaches this list."""
        require_admin(request)
        from src.agent_profile_lint import lint_all
        return {"ok": True, "findings": [f.to_dict() for f in lint_all()]}

    @router.get("/lint/{kind}/{profile_id}")
    async def lint_one(request: Request, kind: str, profile_id: str) -> Dict[str, Any]:
        """Lote A4: one profile, linted."""
        require_admin(request)
        from src.agent_profile_lint import lint_profile
        try:
            profile = catalog.get(kind, profile_id)
        except ProfileError as exc:
            return _refused(exc.path, exc.message)
        if profile is None:
            return _refused(kind, "no {} profile called `{}`".format(kind, profile_id))
        return {"ok": True, "findings": [f.to_dict() for f in lint_profile(kind, profile)]}

    @router.post("/resolve")
    async def preview(request: Request) -> Dict[str, Any]:
        """What this configuration would resolve to. Runs nothing.

        The owner is the session's and the project is the session's binding;
        `owner` and `project_id` in the body are ignored and listed back in
        `ignored_fields`."""
        require_admin(request)
        body = await _json_body(request)
        owner = _owner(request)
        try:
            return await asyncio.to_thread(_resolution_payload, body, owner)
        except ProfileError as exc:
            return _refused(exc.path, exc.message)

    @router.post("/select")
    async def select_agent(request: Request) -> Dict[str, Any]:
        """Which agent this build would choose for a task, and why (§14).

        The same resolver the runtimes use, so the answer here and the answer a
        run gives cannot drift. When there is no selector in this build the
        trace says which fallback rule chose and `degraded_integrations` names
        the gap — an agent chosen for a reason nobody wrote down is one the
        selector proposes again next turn."""
        require_admin(request)
        body = await _json_body(request)
        owner = _owner(request)
        payload = dict(body)
        payload["agent"] = ""      # asking WHO is the whole question
        try:
            answer = await asyncio.to_thread(_resolution_payload, payload, owner)
        except ProfileError as exc:
            return _refused(exc.path, exc.message)
        if not answer.get("ok"):
            return answer
        resolution = answer["resolution"]
        return {"ok": True, "selection": resolution["selection"],
                "agent": resolution["agent"],
                "degraded_integrations": resolution["degraded_integrations"],
                "caveats": resolution["caveats"],
                "ignored_fields": answer["ignored_fields"]}

    @router.get("")
    async def list_profiles(request: Request,
                            workspace: str = Query(default="")) -> Dict[str, Any]:
        """Every definition with the fields the profiles plan added, and the
        files that would not load, each with its reason."""
        require_admin(request)
        payload = await asyncio.to_thread(_definitions, workspace or None)
        return {"ok": True, **payload, "precedence": list(PRECEDENCE),
                "completion_modes": list(COMPLETION_MODES)}

    @router.get("/{slug}")
    async def read_one(request: Request, slug: str,
                       workspace: str = Query(default="")) -> Dict[str, Any]:
        """One definition and the content of every profile it references."""
        require_admin(request)
        return await asyncio.to_thread(_one, slug, workspace or None)

    return router
