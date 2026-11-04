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
                                             add_contact | add_file | add_decision | add_payment | proposal
GET  /api/commissions/{id}/proposals/{name}.{docx|md}   a drafted proposal (Hangar E31)
GET  /api/commissions/people-lookup?name=    People's Hoard candidates through the Hub (read-only)
POST /api/commissions/{id}/link_person | reconcile_payments | confirm_payment   (Ledger read-only)
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

    _OFFICE_POST = ("update", "add_scope", "add_milestone", "update_milestone", "link_package", "add_contact",
                    "add_file", "add_decision", "add_payment", "proposal", "link_person", "reconcile_payments",
                    "confirm_payment")

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

    return router
