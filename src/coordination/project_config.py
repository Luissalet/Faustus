"""Read-only repository choices verified against the existing project work roots."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

from .bindings import CoordinationError


def origin_slug(value):
    value = str(value or "").strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if "github.com" not in value.casefold():
        return value.casefold() if value.count("/") == 1 and ":" not in value else ""
    return value.casefold().split("github.com", 1)[1].lstrip(":/")


def roots(project):
    from services.projects import get_store
    from src.tool_execution import vet_project_root
    paths = [project.get("workspace") or ""] + [item["path"] for item in get_store().normalized_links(project)
        if item["enabled"] and item["access_mode"] == "work_root" and item["kind"] in {"file", "folder"}]
    return list(dict.fromkeys(path for candidate in paths if (path := vet_project_root(candidate))))


def git_info(path):
    directory = path if os.path.isdir(path) else os.path.dirname(path)
    def read(*args, at=None):
        proc = subprocess.run(["git", "-C", at or directory, *args], capture_output=True, text=True,
                              timeout=4, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.stdout.strip() if proc.returncode == 0 else ""
    try:
        top = read("rev-parse", "--show-toplevel")
        if not top:
            return None
        remote = read("config", "--get", "remote.origin.url")
        slug = origin_slug(remote)
        lineage = [top]
        # An isolated clone can point at another local checkout. Follow only
        # existing local Git origins, with a fixed cap; never contact a remote.
        seen = set()
        for _ in range(8):
            if slug or not remote or remote in seen:
                break
            seen.add(remote)
            local = Path(remote.removeprefix("file://"))
            if not local.is_absolute():
                local = Path(top) / local
            if not local.is_dir():
                break
            lineage.append(str(local.resolve()))
            remote = read("config", "--get", "remote.origin.url", at=str(local))
            slug = origin_slug(remote)
        relative = Path(path).resolve().relative_to(Path(top).resolve()).as_posix()
        return {"path": path, "top": top, "relative": "" if relative == "." else relative, "slug": slug, "lineage": lineage}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


async def choices(project, connector_id, manager):
    allowed = project.get("connectors")
    if allowed is not None and (not isinstance(allowed, list) or connector_id not in allowed):
        raise CoordinationError("this project does not allow the coordination connector")
    from .runtime import connector_fingerprint
    connector_fingerprint(connector_id)
    if manager is None or manager.get_server_status(connector_id).get("status") != "connected":
        raise CoordinationError("coordination connector is not connected")
    name = f"mcp__{connector_id}__hub_repos"
    if not any(t.get("qualified_name") == name and not t.get("is_disabled") for t in manager.get_all_tools()):
        raise CoordinationError("this connector does not offer the repository catalogue")
    result = await asyncio.wait_for(manager.call_tool(name, {"filter": "all"}), timeout=10)
    if result.get("exit_code") or result.get("error"):
        raise CoordinationError("repository catalogue is unavailable")
    data = result.get("structured_content")
    if data is None:
        try:
            data = json.loads(result.get("stdout", ""))
        except (ValueError, TypeError) as exc:
            raise CoordinationError("invalid repository catalogue") from exc
    repos = data.get("repos", []) if isinstance(data, dict) else []
    project_roots = await asyncio.to_thread(roots, project)
    local = await asyncio.gather(*(asyncio.to_thread(git_info, path) for path in project_roots[:100]))
    output = []
    verified_paths = {}
    for item in local:
        if not item:
            continue
        for repo in repos:
            if not repo.get("name"):
                continue
            name = repo["name"]
            matched = bool(item["slug"] and origin_slug(repo.get("github")) == item["slug"])
            # SSH aliases and repositories without a GitHub origin are matched
            # against the connector's actual registered checkout path. A folder
            # basename only narrows candidates; it never establishes authority.
            if not matched and name.casefold() in {Path(p).name.casefold() for p in item["lineage"]}:
                if name not in verified_paths:
                    tool = f"mcp__{connector_id}__hub_repo"
                    if not any(t.get("qualified_name") == tool and not t.get("is_disabled") for t in manager.get_all_tools()):
                        continue
                    full = await asyncio.wait_for(manager.call_tool(tool, {"name": name}), timeout=10)
                    detail = full.get("structured_content")
                    if detail is None:
                        try:
                            detail = json.loads(full.get("stdout", ""))
                        except (ValueError, TypeError):
                            detail = {}
                    registered = detail.get("path") if isinstance(detail, dict) and detail.get("ok") is True else None
                    verified_paths[name] = os.path.normcase(os.path.realpath(registered)) if registered else None
                matched = verified_paths[name] in {os.path.normcase(os.path.realpath(p)) for p in item["lineage"]}
            if not matched:
                continue
            suffix = "/" + item["relative"] if item["relative"] else ""
            output.append({"label": name, "repo": name, "resources": [f"path:{name}{suffix}"], "path": item["path"]})
    return {"choices": output, "unavailable": None if output else "No project work folder matches a repository known to this connector"}
