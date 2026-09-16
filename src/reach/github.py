"""`github` channel: (1) public REST API (token optional, higher rate limit
when set), (2) `gh` CLI when installed, (3) Jina Reader on the page."""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from src.reach import credentials
from src.reach.base import Availability, Backend, Channel, ReachBackendError, ReachResult
from src.reach.http_client import make_client
from src.reach.web import JinaReaderBackend, _normalize_url

_REPO_RE = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$")
_ISSUE_RE = re.compile(r"github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?:issues|pull)/(?P<num>\d+)")


def _parse_target(url_or_id: str) -> dict[str, Any]:
    raw = (url_or_id or "").strip()
    if not raw:
        raise ReachBackendError("empty url/id")
    m = _ISSUE_RE.search(raw)
    if m:
        return {"kind": "issue_or_pr", "owner": m["owner"], "repo": m["repo"], "num": m["num"], "is_pr": "/pull/" in raw}
    stripped = re.sub(r"^https?://", "", raw)
    stripped = re.sub(r"^github\.com/", "", stripped)
    m = _REPO_RE.match(stripped)
    if m:
        return {"kind": "repo", "owner": m["owner"], "repo": m["repo"]}
    raise ReachBackendError(f"not a recognizable GitHub repo/issue/PR: {raw!r}")


class GithubApiBackend(Backend):
    name = "github_api"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = credentials.get_token("github")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _probe(self, live: bool) -> Availability:
        if not live:
            return Availability(status="ready", reason="public GitHub REST API, no auth required for basic use", checked_live=live)
        try:
            async with make_client(headers=self._headers()) as client:
                resp = await client.get("https://api.github.com/rate_limit")
            if resp.status_code == 200:
                data = resp.json()
                remaining = data.get("rate", {}).get("remaining")
                return Availability(status="ready", reason=f"{remaining} requests remaining", checked_live=True)
            return Availability(status="unavailable", reason=f"HTTP {resp.status_code}", checked_live=True)
        except Exception as exc:  # noqa: BLE001
            return Availability(status="unavailable", reason=str(exc), checked_live=True)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        target = _parse_target(url_or_id)
        async with make_client(headers=self._headers()) as client:
            if target["kind"] == "repo":
                resp = await client.get(f"https://api.github.com/repos/{target['owner']}/{target['repo']}")
                if resp.status_code >= 400:
                    raise ReachBackendError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                repo = resp.json()
                readme_text = ""
                try:
                    r_resp = await client.get(
                        f"https://api.github.com/repos/{target['owner']}/{target['repo']}/readme",
                        headers={"Accept": "application/vnd.github.raw+json"},
                    )
                    if r_resp.status_code == 200:
                        readme_text = r_resp.text
                except httpx.HTTPError:
                    pass
                text = f"{repo.get('description') or ''}\n\n{readme_text}".strip()
                return ReachResult(
                    channel="github", url=repo.get("html_url", url_or_id), title=repo.get("full_name", ""),
                    author=(repo.get("owner") or {}).get("login", ""), text=text or (repo.get("description") or ""),
                    published_at=repo.get("created_at", ""), source_trust="public_api",
                )
            # issue or PR
            kind_path = "pulls" if target["is_pr"] else "issues"
            resp = await client.get(
                f"https://api.github.com/repos/{target['owner']}/{target['repo']}/issues/{target['num']}"
            )
            if resp.status_code >= 400:
                raise ReachBackendError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            issue = resp.json()
            comments_items: list[dict[str, Any]] = []
            comments_url = issue.get("comments_url")
            if comments_url:
                try:
                    c_resp = await client.get(comments_url, params={"per_page": 30})
                    if c_resp.status_code == 200:
                        for c in c_resp.json():
                            comments_items.append({
                                "author": (c.get("user") or {}).get("login", ""),
                                "text": c.get("body") or "",
                                "score": c.get("reactions", {}).get("total_count", 0),
                                "at": c.get("created_at", ""),
                            })
                except httpx.HTTPError:
                    pass
            return ReachResult(
                channel="github", url=issue.get("html_url", url_or_id), title=issue.get("title", ""),
                author=(issue.get("user") or {}).get("login", ""), text=issue.get("body") or "",
                items=comments_items, published_at=issue.get("created_at", ""), source_trust="public_api",
            )

    async def search(self, query: str, **kwargs: Any) -> list[ReachResult]:
        kind = kwargs.get("kind", "repositories")
        async with make_client(headers=self._headers()) as client:
            resp = await client.get(f"https://api.github.com/search/{kind}", params={"q": query, "per_page": 10})
        if resp.status_code >= 400:
            raise ReachBackendError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        out = []
        for item in data.get("items", []):
            if kind == "repositories":
                out.append(ReachResult(
                    channel="github", url=item.get("html_url", ""), title=item.get("full_name", ""),
                    author=(item.get("owner") or {}).get("login", ""), text=item.get("description") or "",
                    published_at=item.get("created_at", ""), source_trust="public_api",
                ))
            else:
                out.append(ReachResult(
                    channel="github", url=item.get("html_url", ""), title=item.get("name") or item.get("path", ""),
                    author=item.get("repository", {}).get("full_name", ""), text=item.get("path", ""),
                    source_trust="public_api",
                ))
        return out


class GhCliBackend(Backend):
    name = "gh_cli"

    async def _probe(self, live: bool) -> Availability:
        path = shutil.which("gh")
        if not path:
            return Availability(status="needs_config", reason="gh CLI not found on PATH", checked_live=live)
        if not live:
            return Availability(status="ready", reason=path, checked_live=live)
        try:
            proc = await asyncio.to_thread(
                subprocess.run, [path, "auth", "status"], capture_output=True, timeout=5, text=True,
            )
            status = "ready" if proc.returncode == 0 else "needs_config"
            reason = (proc.stdout or proc.stderr or "").strip()[:200]
            return Availability(status=status, reason=reason, checked_live=True)
        except Exception as exc:  # noqa: BLE001
            return Availability(status="unavailable", reason=str(exc), checked_live=True)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        path = shutil.which("gh")
        if not path:
            raise ReachBackendError("gh CLI not found on PATH")
        target = _parse_target(url_or_id)
        if target["kind"] != "repo":
            raise ReachBackendError("gh CLI backend only handles repo reads")
        cmd = [path, "repo", "view", f"{target['owner']}/{target['repo']}", "--json",
               "name,description,url,owner,createdAt"]
        proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=15, text=True)
        if proc.returncode != 0:
            raise ReachBackendError((proc.stderr or "gh repo view failed")[:300])
        import json as _json
        data = _json.loads(proc.stdout or "{}")
        return ReachResult(
            channel="github", url=data.get("url", url_or_id), title=data.get("name", ""),
            author=(data.get("owner") or {}).get("login", ""), text=data.get("description") or "",
            published_at=data.get("createdAt", ""), source_trust="scrape",
        )


class GithubJinaBackend(Backend):
    name = "jina_reader"

    async def _probe(self, live: bool) -> Availability:
        return await JinaReaderBackend()._probe(live)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        url = url_or_id if url_or_id.startswith("http") else f"https://github.com/{url_or_id}"
        result = await JinaReaderBackend().read(url)
        result.channel = "github"
        return result


class GithubChannel(Channel):
    name = "github"
    backends = [GithubApiBackend(), GhCliBackend(), GithubJinaBackend()]
