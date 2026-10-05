"""Atlas shared filesystem client. All applications get the same live paths.

No copying into an application cache and no inferred sharing of existing data.
Configure ``family`` with the caller's own token before use.
"""
from __future__ import annotations

from ._famsvc import call_tool

OWNER = "atlas"


def call(tool, arguments=None, *, timeout_s=120):
    response = call_tool(OWNER, "atlas_" + tool, arguments or {}, timeout_s=timeout_s)
    if not response.get("ok"):
        return response
    return {**response["data"], "via": OWNER}


def projects(*, sphere=None):
    return call("projects", {"sphere": sphere} if sphere else {})


def project(project_id):
    return call("project", {"project_id": project_id})


def create(name, *, members, owner=None, sphere="personal", goal="", request_id=None):
    args = {"name": name, "members": list(members), "sphere": sphere, "goal": goal}
    if owner:
        args["owner"] = owner
    if request_id:
        args["request_id"] = request_id
    return call("project_create", args)


def location(project_id, *, area="shared", app=None):
    args = {"project_id": project_id, "area": area}
    if app:
        args["app"] = app
    return call("location", args)


def register(project_id, relative_path, *, title="", request_id=None):
    args = {"project_id": project_id, "relative_path": relative_path, "title": title}
    if request_id:
        args["request_id"] = request_id
    return call("file_register", args)


def resolve(file_id):
    return call("file_resolve", {"file_id": file_id})


def lookup(project_id, source_ids, recipe):
    return call("derived_lookup", {"project_id": project_id, "source_ids": list(source_ids), "recipe": recipe})


def publish(project_id, source_ids, recipe, output_id, *, source_revisions, request_id=None):
    args = {"project_id": project_id, "source_ids": list(source_ids), "recipe": recipe, "output_id": output_id, "source_revisions": dict(source_revisions)}
    if request_id:
        args["request_id"] = request_id
    return call("derived_publish", args)


def context(project_id, file_ids=()):
    return call("context", {"project_id": project_id, "file_ids": list(file_ids)})
