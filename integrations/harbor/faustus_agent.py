"""Faustus as a Harbor installed agent (``--agent integrations.harbor.faustus_agent:Faustus``).

The task container gets a self-contained runtime under ``/opt/faustus``:

* ``faustus-venv.tgz``: a relocatable CPython 3.12 and the server's
  dependencies, built once by ``build_bundle.py venv`` on an old glibc so it
  runs in any x86_64 Linux image;
* ``faustus-src.tgz``: this source tree (``build_bundle.py src``), rebuilt
  whenever the code changes.

Both are read from ``FAUSTUS_HARBOR_BUNDLE`` (a folder on the host). ``run``
starts ``serve_headless.py`` in the container, waits for ``/api/health`` and
drives one agent turn with ``scripts/faustus_run.py --approve --json`` in the
task's working directory, against the model the job names (``-m
openai/<model>`` with ``OPENAI_BASE_URL``). Everything the turn did is kept in
``/logs/agent`` (server log, NDJSON events, summary).
"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent, NonZeroAgentExitCodeError, with_prompt_template
from harbor.agents.model_connection import ModelConnectionSpec
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

REMOTE_ROOT = "/opt/faustus"
REMOTE_APP = f"{REMOTE_ROOT}/app"
REMOTE_PY = f"{REMOTE_ROOT}/venv/bin/python"
OUTPUT = "faustus.ndjson"
SERVER_LOG = "faustus-server.log"


def _bundle_dir() -> Path:
    return Path(os.environ.get("FAUSTUS_HARBOR_BUNDLE") or Path(__file__).resolve().parent / "bundle")


class Faustus(BaseInstalledAgent):
    MODEL_CONNECTION = ModelConnectionSpec(default_provider="openai", passthrough=True)
    # A task's MCP servers are registered in the headless server before the
    # turn (``faustus_run --mcp-servers``) and offered to the agent as tools.
    capabilities = AgentCapabilities(mcp_servers=True)

    def __init__(self, *args, effort: str | None = None, think_mode: str | None = None,
                 settings: dict[str, Any] | str | None = None, run_timeout: float | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._effort = effort
        self._think_mode = think_mode
        if isinstance(settings, str) and settings.strip():
            settings = json.loads(settings)
        self._settings = settings or {}
        self._run_timeout = float(run_timeout or 7200)

    @staticmethod
    def name() -> str:
        return "faustus"

    def get_version_command(self) -> str | None:
        return f"cat {REMOTE_APP}/.bundle-version 2>/dev/null || echo unknown"

    async def install(self, environment: BaseEnvironment) -> None:
        bundle = _bundle_dir()
        venv, src = bundle / "faustus-venv.tgz", bundle / "faustus-src.tgz"
        for path in (venv, src):
            if not path.is_file():
                raise FileNotFoundError(f"missing Faustus bundle part {path} (run build_bundle.py)")
        await self.exec_as_root(environment, command=f"mkdir -p {REMOTE_ROOT} /tmp/faustus-bundle")
        await environment.upload_file(venv, "/tmp/faustus-bundle/faustus-venv.tgz")
        await environment.upload_file(src, "/tmp/faustus-bundle/faustus-src.tgz")
        await self.exec_as_root(
            environment,
            command=(
                "set -e; tar -xzf /tmp/faustus-bundle/faustus-venv.tgz -C /opt; "
                f"mkdir -p {REMOTE_APP}; tar -xzf /tmp/faustus-bundle/faustus-src.tgz -C {REMOTE_APP}; "
                f"rm -rf /tmp/faustus-bundle; chmod -R a+rX {REMOTE_ROOT}; "
                f"{REMOTE_PY} -c 'import fastapi, uvicorn, sqlalchemy; print(\"faustus runtime ok\")'"
            ),
        )

    def _model_id(self) -> str:
        if not self.model_name:
            raise ValueError("a model is required: -m openai/<model id>")
        return self.model_name.split("/", 1)[-1] if "/" in self.model_name else self.model_name

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        access = self.model_connection
        env = dict(access.env)
        base_url = access.configured_base_url or env.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or ""
        if not base_url:
            raise ValueError("set OPENAI_BASE_URL to the model server")
        model = self._model_id()
        logs = self.environment_logs_dir.as_posix()
        prompt_path = f"{logs}/instruction.md"
        await self._upload_config_text(environment, content=instruction, remote_path=prompt_path, filename="instruction.md")
        env.update({
            "FAUSTUS_ENDPOINT_URL": base_url, "FAUSTUS_MODEL": model, "FAUSTUS_DATA_DIR": "/tmp/faustus-data",
            "FAUSTUS_APP_ROOT": REMOTE_APP, "FAUSTUS_SETTINGS_JSON": json.dumps(self._settings),
            "PYTHONDONTWRITEBYTECODE": "1",
            # A model server shared by many trials can take longer than the
            # default 10 s to accept a connection.
            "LLM_CONNECT_TIMEOUT": os.environ.get("FAUSTUS_LLM_CONNECT_TIMEOUT", "30"),
        })
        if os.environ.get("FAUSTUS_HARBOR_PROMPT_DUMP"):
            # Each round's exact messages and tool schemas, next to the trial logs.
            env["FAUSTUS_PROMPT_DUMP"] = f"{logs}/prompt"
        run_flags = ["--approve", "--json", "--timeout", str(int(self._run_timeout)), "--max-legs", "200"]
        if self.mcp_servers:
            mcp_path = f"{logs}/mcp-servers.json"
            servers = [s.model_dump(mode="json") if hasattr(s, "model_dump") else dict(s) for s in self.mcp_servers]
            await self._upload_config_text(environment, content=json.dumps(servers), remote_path=mcp_path,
                                           filename="mcp-servers.json")
            run_flags += ["--mcp-servers", "@" + mcp_path]
        if self._effort:
            run_flags += ["--effort", self._effort]
        if self._think_mode:
            run_flags += ["--think-mode", self._think_mode]
        health = (f"{REMOTE_PY} -c \"import urllib.request,sys; "
                  f"urllib.request.urlopen('http://127.0.0.1:7000/api/health', timeout=2)\"")
        script = (
            f"WS=\"$(pwd)\"; mkdir -p {shlex.quote(logs)}; "
            f"(cd {REMOTE_APP} && nohup {REMOTE_PY} integrations/harbor/serve_headless.py --port 7000 "
            f"> {shlex.quote(logs + '/' + SERVER_LOG)} 2>&1 &); "
            f"for i in $(seq 1 240); do {health} 2>/dev/null && break; sleep 1; done; "
            f"{health} || {{ echo 'faustus server did not start'; tail -n 40 {shlex.quote(logs + '/' + SERVER_LOG)}; exit 1; }}; "
            # Nobody answers during a trial. Older bundles lack the flag, so
            # it is passed only when this bundle's runner knows it.
            f"U=; grep -q -- '--unattended' {REMOTE_APP}/scripts/faustus_run.py && U=--unattended; "
            f"{REMOTE_PY} {REMOTE_APP}/scripts/faustus_run.py -p @{shlex.quote(prompt_path)} "
            f"--url http://127.0.0.1:7000 --workspace \"$WS\" --model {shlex.quote(model)} "
            f"--endpoint-url {shlex.quote(base_url)} $U {' '.join(shlex.quote(f) for f in run_flags)} "
            f"> {shlex.quote(logs + '/' + OUTPUT)} 2> {shlex.quote(logs + '/faustus-run.err')}; "
            f"rc=$?; tail -c 4000 {shlex.quote(logs + '/faustus-run.err')}; exit $rc"
        )
        result = await self.exec_as_agent(environment, command=script, env=env, timeout_sec=int(self._run_timeout) + 600)
        if getattr(result, "return_code", 0) not in (0, None, 2, 3):
            raise NonZeroAgentExitCodeError(f"faustus_run exited {result.return_code}: {(result.stdout or '')[-800:]}")

    def _summary(self) -> dict[str, Any]:
        path = self.logs_dir / OUTPUT
        if not path.exists():
            return {}
        summary: dict[str, Any] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "run_summary":
                summary = ev
        return summary

    def populate_context_post_run(self, context: AgentContext) -> None:
        summary = self._summary()
        usage = summary.get("usage") or {}
        context.n_input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        context.n_output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        context.n_cache_tokens = int(usage.get("cached_tokens") or usage.get("cache_read_tokens") or 0)
        context.cost_usd = None
        meta = {k: summary.get(k) for k in ("rounds", "stop", "tools", "error", "session") if k in summary}
        try:
            (self.logs_dir / "faustus-summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
