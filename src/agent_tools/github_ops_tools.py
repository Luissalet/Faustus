"""agent_tools/github_ops_tools.py -- the agent's twins of `/github` (see
src/github_ops.py): labels, release notes and the release check, built on
`gh` and local git.

    github_labels         read   the repo's labels; with `file`, the dry-run plan of a sync
    github_labels_sync    WRITE  create/update labels from a file (`gh label create|edit`)
    github_notes          read   release notes drafted from merged PRs / commits
    github_release_check  read   checklist: clean tree, tag, CI, blockers, versions

Repository resolution is the one the other git tools use
(`git_tools._resolve_repo_root`: `path`, `repo` name, active workspace, the
project's only repo).

`github_labels_sync` is the only remote write and it is stricter than
`git_push`: the repository policy's ``push`` field must allow remote writes
(the global Full access mode counts as allowing them) AND a human must have
approved this exact call (``ctx["human_approved"]`` from a sealed approval
card, or ``user_confirmed: true`` after asking). The refusal carries the
dry-run plan so the model can show it when it asks. Nothing here pushes, and
nothing creates a release or a tag.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from src import github_ops
from src.agent_tools import git_tools as gt


def _fail(tool: str, res: Dict[str, Any]) -> Dict[str, Any]:
    return {"error": f"{tool}: {res.get('error', 'failed')}", "exit_code": 1,
            "error_class": res.get("error_class", "github.failed")}


async def _in_thread(fn, *a, **kw):
    return await asyncio.to_thread(fn, *a, **kw)


class GithubLabelsTool:
    """`github_labels`: list the labels; with `file`, plan (never apply) a sync."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = gt._args(content)
        root, err = gt._resolve_repo_root("github_labels", args, ctx)
        if err:
            return err
        file = str(args.get("file") or "").strip()
        lang = str(args.get("lang") or "en")
        if file:
            res = await _in_thread(self._plan, gt._owner(ctx), root, file, lang)
            if not res.get("ok"):
                return _fail("github_labels", res)
            # the dry-run never hands the agent an approval id: approval is a human act
            res = {k: v for k, v in res.items() if k != "approval_id"}
            return {"output": res["markdown"], "exit_code": 0, "repo_root": root, **{k: res[k] for k in ("plan", "policy_allowed", "slug", "file") if k in res}}
        res = await _in_thread(github_ops.list_labels, root)
        if not res.get("ok"):
            return _fail("github_labels", res)
        return {"output": github_ops.labels_markdown(res["labels"], res.get("slug") or "", lang), "exit_code": 0,
                "repo_root": root, "labels": res["labels"], "slug": res.get("slug")}

    @staticmethod
    def _plan(owner, root, file, lang):
        res = github_ops.prepare_label_sync(owner, root, file, lang)
        if res.get("approval_id"):  # nothing consumes it here; do not leave it armed
            with github_ops._PENDING_LOCK:
                github_ops._PENDING.pop(res["approval_id"], None)
        return res


class GithubLabelsSyncTool:
    """`github_labels_sync`: the remote write. Policy AND approval, see the module docstring."""

    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_capabilities import tool_approval_mode
        args = gt._args(content)
        file = str(args.get("file") or "").strip()
        if not file:
            return {"error": "github_labels_sync: `file` is required (a YAML/JSON label file in the repo)",
                    "exit_code": 1, "error_class": "args.missing"}
        root, err = gt._resolve_repo_root("github_labels_sync", args, ctx)
        if err:
            return err
        owner = gt._owner(ctx)
        lang = str(args.get("lang") or "en")
        full = tool_approval_mode() == "full"

        state = await _in_thread(github_ops._label_sync_state, root, file)
        if not state.get("ok"):
            return _fail("github_labels_sync", state)
        plan, slug = state["plan"], state["slug"]
        if not plan["changes"]:
            return {"output": github_ops.sync_plan_markdown(plan, slug, lang), "exit_code": 0, "repo_root": root,
                    "applied": False, "plan": plan}

        if not full and not github_ops.remote_writes_allowed(owner, root):
            return {"error": ("github_labels_sync: refused -- this repository's git agent policy does not allow remote writes "
                              "(push=false). The user can allow it from Settings > Git policy (global or per-repo)."),
                    "exit_code": 1, "policy": "git_agent_policy", "git_policy_field": "push",
                    "error_class": "git.policy_denied", "plan": plan}
        if not full and not gt._human_approved(ctx, args):
            return {"error": ("github_labels_sync: needs the user's explicit approval. Show them this plan, ask with ask_user, and "
                              "retry this EXACT call with \"user_confirmed\": true once they say yes.\n\n"
                              + github_ops.sync_plan_markdown(plan, slug, lang)),
                    "exit_code": 1, "error_class": "approval.required", "plan": plan}

        results = await _in_thread(github_ops.execute_label_plan, state["ctx"], plan)
        ok = all(r["ok"] for r in results)
        out = {"output": github_ops._apply_markdown(results, slug, lang), "exit_code": 0 if ok else 1,
               "repo_root": root, "applied": True, "results": results, "slug": slug}
        if not ok:
            out["error"] = "github_labels_sync: some labels failed -- see results"
            out["error_class"] = "github.failed"
        return out


class GithubNotesTool:
    """`github_notes`: release notes from the first-parent history between two refs."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = gt._args(content)
        root, err = gt._resolve_repo_root("github_notes", args, ctx)
        if err:
            return err
        res = await _in_thread(github_ops.release_notes, root, str(args.get("from") or "").strip() or None,
                               str(args.get("to") or "").strip() or None, str(args.get("lang") or "en"))
        if not res.get("ok"):
            return _fail("github_notes", res)
        return {"output": res["markdown"], "exit_code": 0, "repo_root": root, "from": res["from"], "to": res["to"],
                "commits": res["commits"], "pull_requests": res["pull_requests"], "gh": res["gh"],
                "entries": res["entries"]}


class GithubReleaseCheckTool:
    """`github_release_check`: is this release ready? A checklist, never a release."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = gt._args(content)
        root, err = gt._resolve_repo_root("github_release_check", args, ctx)
        if err:
            return err
        labels = args.get("blocker_labels")
        if isinstance(labels, str):
            labels = [x.strip() for x in labels.split(",") if x.strip()]
        res = await _in_thread(github_ops.release_check, root, str(args.get("tag") or "").strip() or None,
                               labels if isinstance(labels, list) else None, str(args.get("lang") or "en"))
        if not res.get("ok"):
            return _fail("github_release_check", res)
        return {"output": res["markdown"], "exit_code": 0, "repo_root": root, "ready": res["ready"],
                "items": res["items"], "blocking": res["blocking"], "counts": res["counts"]}
