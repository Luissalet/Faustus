"""routes/delivery_routes.py -- HTTP surface for reproducible delivery packages
(Hangar E33). Thin wrappers over `src/delivery_package.run`, the dispatcher the
chat tool and the MCP server also use. `require_admin` on every endpoint: the
admin may name any local file except the sensitive ones read_file refuses
(the chat tool stays confined to the active workspace); packages are scoped to
the signed-in owner.

POST /api/delivery-packages                  {name, items, instructions?, limitations?, software_root?,
                                              out_dir?, lang?}  build and verify
GET  /api/delivery-packages                  the owner's packages, newest first
GET  /api/delivery-packages/readers          formats with a reader, and the roles
GET  /api/delivery-packages/{id}             the manifest (items, checks, gaps, reimport)
POST /api/delivery-packages/{id}/reverify    extract the stored zip again and reopen every item
GET  /api/delivery-packages/{id}/download    the zip
GET  /api/delivery-packages/{id}/document    ENTREGA.md as text

Scope comparator (Hangar E30, src/scope_compare.py):
POST /api/scope-comparisons                  {agreement, new_request, initial?, graph?, effort_hours?, mode?, lang?, title?}
GET  /api/scope-comparisons                  the owner's comparisons, newest first
GET  /api/scope-comparisons/{id}             one comparison

Commission office (Hangar E29, src/commission_office.py):
GET  /api/commissions                        the owner's commissions
POST /api/commissions                        {title, client?, stage?, summary?, request?, budget?}
GET  /api/commissions/{id}                   the record with its computed status
POST /api/commissions/{id}/{action}          update | add_scope | add_milestone | update_milestone | link_package |
                                             link_review {milestone, review_id} | add_contact | add_file | add_decision |
                                             add_payment | proposal
GET  /api/commissions/{id}/proposals/{name}.{docx|md}   a drafted proposal (Hangar E31)
GET  /api/commissions/people-lookup?name=    People's Hoard candidates through the Hub (read-only)
POST /api/commissions/{id}/link_person | reconcile_payments | confirm_payment {label, tx_id | tx_ids}   (Ledger read-only)
GET  /api/commissions/meetings              recent Faustus meetings to import (Hangar E27)
POST /api/commissions/{id}/import_meeting | promote_meeting_item   meeting -> requirements and open questions

Review desk (Hangar E32, src/review_desk.py):
GET  /api/review-desks                       the owner's review desks
POST /api/review-desks                       {title, path?, commission?, milestone?}
GET  /api/review-desks/{id}                  versions, comments and status
GET  /api/review-desks/{id}/compare?a=&b=    reader facts and text diff between two versions
GET  /api/review-desks/{id}/versions/{n}     the snapshot of version n
GET  /api/review-desks/{id}/view?version=&anchor=|comment=|ref=&image=   that version opened at the anchor (Hangar E22)
GET  /api/review-desks/view?ref=review:<id>@vN#anchor               the same from a shared reference
POST /api/review-desks/{id}/{action}         add_version | comment | import_feedback | set_status | deliver

Delete (Faustus' own records only; source files are never touched):
DELETE /api/delivery-packages/{id}           folder, zip and manifest of the package
DELETE /api/scope-comparisons/{id}           one saved comparison
DELETE /api/review-desks/{id}                the desk record and its version snapshots
DELETE /api/commissions/{id}[?with_linked=true]  the record, its proposals and its scope comparisons; with_linked also its
                                             delivery packages (unless another commission links them) and review desks
DELETE /api/commissions/meetings/{id}        the notes of a Faustus meeting (recording kept)
"""
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from core.middleware import require_admin
from src import delivery_package
from src.auth_helpers import effective_user


def _reply(result: Dict[str, Any]):
    if result.get("error_class"):
        status = 404 if str(result["error_class"]).endswith("not_found") else 400
        return JSONResponse(status_code=status, content=result)
    result.pop("exit_code", None)
    return result


def setup_delivery_routes() -> APIRouter:
    router = APIRouter(tags=["delivery"])

    def owner_of(request: Request) -> str:
        require_admin(request)
        return effective_user(request) or ""

    @router.post("/api/delivery-packages")
    async def build_package(request: Request):
        owner = owner_of(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}
        import asyncio
        return _reply(await asyncio.to_thread(delivery_package.run, owner, {**body, "action": "build"}, confine=False))

    @router.get("/api/delivery-packages")
    async def list_packages(request: Request):
        return _reply(delivery_package.run(owner_of(request), {"action": "list"}))

    @router.get("/api/delivery-packages/readers")
    async def readers(request: Request):
        return _reply(delivery_package.run(owner_of(request), {"action": "readers"}))

    @router.get("/api/delivery-packages/{pkg_id}")
    async def show(pkg_id: str, request: Request):
        return _reply(delivery_package.run(owner_of(request), {"action": "show", "id": pkg_id}))

    @router.post("/api/delivery-packages/{pkg_id}/reverify")
    async def reverify(pkg_id: str, request: Request):
        import asyncio
        return _reply(await asyncio.to_thread(delivery_package.run, owner_of(request), {"action": "reverify", "id": pkg_id}))

    @router.delete("/api/delivery-packages/{pkg_id}")
    async def delete_package(pkg_id: str, request: Request):
        return _reply(delivery_package.run(owner_of(request), {"action": "delete", "id": pkg_id}))

    @router.get("/api/delivery-packages/{pkg_id}/download")
    async def download(pkg_id: str, request: Request):
        owner = owner_of(request)
        try:
            path = delivery_package.zip_path(owner, pkg_id)
        except delivery_package.PackageError as exc:
            return JSONResponse(status_code=404, content={"error": exc.detail, "error_class": exc.error_class})
        return FileResponse(str(path), media_type="application/zip", filename=path.name)

    @router.get("/api/delivery-packages/{pkg_id}/document")
    async def document(pkg_id: str, request: Request):
        owner = owner_of(request)
        try:
            path = delivery_package.zip_path(owner, pkg_id)
        except delivery_package.PackageError as exc:
            return JSONResponse(status_code=404, content={"error": exc.detail, "error_class": exc.error_class})
        folder = path.with_suffix("")
        doc = folder / "ENTREGA.md"
        return PlainTextResponse(doc.read_text(encoding="utf-8") if doc.is_file() else "", media_type="text/markdown")

    @router.post("/api/scope-comparisons")
    async def compare_scope(request: Request):
        from src import scope_compare
        owner = owner_of(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}
        import asyncio
        return _reply(await asyncio.to_thread(scope_compare.run, owner, {**body, "action": "compare"}))

    @router.get("/api/scope-comparisons")
    async def list_scope(request: Request):
        from src import scope_compare
        return _reply(scope_compare.run(owner_of(request), {"action": "list"}))

    @router.get("/api/scope-comparisons/{cid}")
    async def show_scope(cid: str, request: Request):
        from src import scope_compare
        return _reply(scope_compare.run(owner_of(request), {"action": "show", "id": cid}))

    @router.delete("/api/scope-comparisons/{cid}")
    async def delete_scope(cid: str, request: Request):
        from src import scope_compare
        return _reply(scope_compare.run(owner_of(request), {"action": "delete", "id": cid}))

    _OFFICE_POST = ("update", "add_scope", "add_milestone", "update_milestone", "link_package", "link_review", "add_contact",
                    "add_file", "add_decision", "add_payment", "proposal", "link_person", "reconcile_payments",
                    "confirm_payment", "import_meeting", "promote_meeting_item")

    async def _json(request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        return body if isinstance(body, dict) else {}

    @router.get("/api/commissions")
    async def list_commissions(request: Request):
        from src import commission_office
        return _reply(commission_office.run(owner_of(request), {"action": "list"}))

    @router.post("/api/commissions")
    async def create_commission(request: Request):
        from src import commission_office
        owner = owner_of(request)
        return _reply(commission_office.run(owner, {**(await _json(request)), "action": "create"}))

    @router.get("/api/commissions/people-lookup")
    async def people_lookup(request: Request, name: str = ""):
        from src import commission_office
        owner = owner_of(request)
        import asyncio
        return _reply(await asyncio.to_thread(commission_office.run, owner, {"action": "people_lookup", "name": name}))

    @router.get("/api/commissions/meetings")
    async def commission_meetings(request: Request):
        from src import commission_office
        return _reply(commission_office.run(owner_of(request), {"action": "list_meetings"}))

    @router.delete("/api/commissions/meetings/{meeting_id}")
    async def delete_commission_meeting(meeting_id: str, request: Request):
        from src import commission_office
        return _reply(commission_office.run(owner_of(request), {"action": "delete_meeting", "meeting_id": meeting_id}))

    @router.delete("/api/commissions/{cid}")
    async def delete_commission(cid: str, request: Request, with_linked: bool = False):
        from src import commission_office
        import asyncio
        return _reply(await asyncio.to_thread(commission_office.run, owner_of(request),
                                              {"action": "delete", "id": cid, "with_linked": with_linked}))

    @router.get("/api/commissions/{cid}")
    async def show_commission(cid: str, request: Request):
        from src import commission_office
        return _reply(commission_office.run(owner_of(request), {"action": "show", "id": cid}))

    @router.get("/api/commissions/{cid}/proposals/{name}.{kind}")
    async def proposal_download(cid: str, name: str, kind: str, request: Request):
        from src import proposal_builder
        owner = owner_of(request)
        try:
            path = proposal_builder.proposal_file(owner, cid, name, kind)
        except (FileNotFoundError, ValueError):
            return JSONResponse(status_code=404, content={"error": "no such proposal", "error_class": "office.not_found"})
        media = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document" if kind == "docx"
                 else "text/markdown")
        return FileResponse(str(path), media_type=media, filename=path.name)

    @router.post("/api/commissions/{cid}/{action}")
    async def commission_action(cid: str, action: str, request: Request):
        from src import commission_office
        owner = owner_of(request)
        if action not in _OFFICE_POST:
            return JSONResponse(status_code=400, content={"error": f"action must be one of {', '.join(_OFFICE_POST)}",
                                                          "error_class": "office.bad_args"})
        import asyncio
        body = await _json(request)
        return _reply(await asyncio.to_thread(commission_office.run, owner, {**body, "action": action, "id": cid}))

    _DESK_POST = ("add_version", "comment", "import_feedback", "set_status", "deliver")

    @router.get("/api/review-desks")
    async def list_desks(request: Request):
        from src import review_desk
        return _reply(review_desk.run(owner_of(request), {"action": "list"}))

    @router.post("/api/review-desks")
    async def create_desk(request: Request):
        from src import review_desk
        import asyncio
        owner = owner_of(request)
        body = await _json(request)
        return _reply(await asyncio.to_thread(review_desk.run, owner, {**body, "action": "create"}, confine=False))

    @router.get("/api/review-desks/view")
    async def view_ref(request: Request, ref: str = "", image: bool = False):
        from src import review_desk
        import asyncio
        owner = owner_of(request)
        return _reply(await asyncio.to_thread(review_desk.run, owner, {"action": "view", "ref": ref, "image": image}))

    @router.get("/api/review-desks/{did}")
    async def show_desk(did: str, request: Request):
        from src import review_desk
        import asyncio
        owner = owner_of(request)
        return _reply(await asyncio.to_thread(review_desk.run, owner, {"action": "show", "id": did}))

    @router.get("/api/review-desks/{did}/compare")
    async def compare_desk(did: str, request: Request, a: str = "", b: str = ""):
        from src import review_desk
        import asyncio
        owner = owner_of(request)
        return _reply(await asyncio.to_thread(review_desk.run, owner, {"action": "compare", "id": did, "a": a, "b": b}))

    @router.get("/api/review-desks/{did}/view")
    async def view_desk(did: str, request: Request, version: str = "", anchor: str = "", comment: str = "", ref: str = "",
                        image: bool = False):
        from src import review_desk
        import asyncio
        owner = owner_of(request)
        args = {"action": "view", "id": did, "version": version or None, "anchor": anchor, "comment": comment, "ref": ref, "image": image}
        return _reply(await asyncio.to_thread(review_desk.run, owner, args))

    @router.get("/api/review-desks/{did}/versions/{n}")
    async def desk_version(did: str, n: int, request: Request):
        from src import review_desk
        owner = owner_of(request)
        try:
            path = review_desk.snapshot_path(owner, did, n)
        except review_desk.DeskError as exc:
            return JSONResponse(status_code=404, content={"error": exc.detail, "error_class": exc.error_class})
        return FileResponse(str(path), filename=path.name)

    @router.delete("/api/review-desks/{did}")
    async def delete_desk(did: str, request: Request):
        from src import review_desk
        return _reply(review_desk.run(owner_of(request), {"action": "delete", "id": did}))

    @router.post("/api/review-desks/{did}/{action}")
    async def desk_action(did: str, action: str, request: Request):
        from src import review_desk
        import asyncio
        owner = owner_of(request)
        if action not in _DESK_POST:
            return JSONResponse(status_code=400, content={"error": f"action must be one of {', '.join(_DESK_POST)}",
                                                          "error_class": "desk.bad_args"})
        body = await _json(request)
        return _reply(await asyncio.to_thread(review_desk.run, owner, {**body, "action": action, "id": did}, confine=False))

    return router
