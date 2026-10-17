"""Native project management over the same stores used by Projects UI."""
import asyncio
import json

from services import projects
from src import project_workspace as workspace
from src.skill_import_review import SkillReviewError


def perform(project: dict, args: dict, owner=None) -> dict:
    action = args.get("action", "")
    store = projects.get_store()
    if action == "instructions_read":
        return {"instructions": project.get("instructions", "")}
    if action == "instructions_write":
        return {"project": store.update(project["id"], {"instructions": args["content"]}, owner)}
    if action == "memory_list":
        return {"files": store.list_memory_files(project)}
    if action == "memory_read":
        return {"content": store.read_memory_file(project, args["name"])}
    if action == "memory_write":
        store.write_memory_file(project, args["name"], args["content"])
        return {"name": args["name"], "content": store.read_memory_file(project, args["name"])}
    if action == "skills_list":
        return {"skills": workspace.list_skills(project)}
    if action == "skill_read":
        return {"skill": workspace.read_skill(project, args["key"])}
    if action == "skills_catalog":
        return {"skills": workspace.catalog(owner)}
    if action == "skills_repository":
        return {"skills": workspace.repository_skills(args["url"])}
    if action == "skill_install":
        return {"skill": workspace.install_catalog(project, args["kind"], args["name"], owner)}
    if action == "skill_import":
        return {"skill": workspace.install_url(project, args["url"]) if args.get("url") else workspace.install_files(project, args.get("files") or {"SKILL.md": args.get("content", "")})}
    if action == "skill_activate":
        # Chat/MCP can activate a previously reviewed digest, never self-approve.
        return {"skill": workspace.activate(project, args["key"], actor=owner or "agent")}
    if action == "skill_deactivate":
        return {"skill": workspace.deactivate(project, args["key"])}
    raise projects.ProjectError("Unknown project workspace action.")


class ProjectWorkspaceTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content) if isinstance(content, str) else dict(content)
            if not isinstance(args, dict):
                raise ValueError("Arguments must be an object.")
            owner = ctx.get("owner") or None
            # Resolve from the trusted chat context; ignore model-supplied ids.
            project = (projects.get_store().get(ctx["project_id"], owner) if ctx.get("project_id")
                       else projects.project_for_session(ctx.get("session_id") or "", owner))
            if not project:
                raise projects.ProjectError("This chat has no project. Open a project chat first.")
            result = await asyncio.to_thread(perform, project, args, owner)
            return {"exit_code": 0, "output": json.dumps(result, ensure_ascii=False), **result}
        except (ValueError, OSError, KeyError, projects.ProjectError, SkillReviewError) as exc:
            return {"exit_code": 1, "error": str(exc)}
