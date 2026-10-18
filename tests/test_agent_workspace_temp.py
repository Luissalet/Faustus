"""Run-local native temporary files, without changing host mounts or caches."""
import asyncio
from collections import namedtuple
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from src import run_causality, tool_execution as te, worker_caches
from src.native_env import native_host_environment


class WorkspaceTempTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="faustus-temp-contract-")
        self.addCleanup(self.folder.cleanup)
        self.workspace = Path(self.folder.name) / "project space"
        self.workspace.mkdir()
        token = te._active_workspace.set(str(self.workspace))
        self.addCleanup(te._active_workspace.reset, token)

    def environment(self, run="run-a", session="session-a", call="tool-1", base=None):
        return te._workspace_temp_env(
            dict(os.environ) if base is None else base,
            session, run_causality.CallOrigin(session, run, call),
        )

    def test_same_run_reuses_files_across_tool_calls(self):
        first = self.environment()
        saved = Path(first["TMPDIR"]) / "from-first-command.txt"
        saved.write_text("retained", encoding="utf-8")
        second = self.environment(call="tool-2")
        self.assertEqual(first, second)
        self.assertEqual(saved.read_text(encoding="utf-8"), "retained")
        self.assertTrue(saved.is_relative_to(self.workspace))
        self.assertEqual(first["TMP"], first["TEMP"])
        self.assertEqual(first["TMP"], first["TMPDIR"])

    def test_run_session_and_workspace_separate_paths(self):
        first = self.environment()["TMPDIR"]
        self.assertNotEqual(first, self.environment(run="run-b")["TMPDIR"])
        self.assertNotEqual(first, self.environment(session="session-b")["TMPDIR"])
        other = self.workspace.parent / "other"
        other.mkdir()
        token = te._active_workspace.set(str(other))
        try:
            self.assertNotEqual(first, self.environment()["TMPDIR"])
        finally:
            te._active_workspace.reset(token)

    def test_concurrent_run_contexts_do_not_share_temp(self):
        async def task(run):
            token = run_causality.bind_run("session-a", run)
            try:
                await asyncio.sleep(0)
                return te._workspace_temp_env({}, "session-a")["TMPDIR"]
            finally:
                run_causality.reset_run(token)

        async def main():
            return await asyncio.gather(task("one"), task("two"))

        a, b = asyncio.run(main())
        self.assertNotEqual(a, b)

    def test_no_run_has_stable_session_fallback_without_call_uuid(self):
        a = self.environment(run=None)
        self.assertEqual(a, self.environment(run=None, call="tool-2"))
        self.assertNotEqual(a["TMPDIR"], self.environment(run=None, session="b")["TMPDIR"])

    def test_identity_does_not_become_a_path(self):
        env = self.environment(run="../../foreign/\\:bad", session="../other")
        self.assertTrue(Path(env["TMPDIR"]).resolve().is_relative_to(self.workspace.resolve()))

    def test_native_environment_keeps_options_and_unrelated_caches(self):
        base = {"PATH": os.environ["PATH"], "KEEP_OPTION": "enabled",
                "PIP_CACHE_DIR": "existing-pip", "HF_HOME": "existing-model-cache",
                "SERVICE_TOKEN": "test-only", "HOME": "existing-home", "TMP": "old"}
        if os.name == "nt":
            base.update({"Tmp": "old-case", "temp": "old-case", "tmpdir": "old-case"})
        before = dict(base)
        env = self.environment(base=base)
        self.assertEqual(base, before)
        for key in ("KEEP_OPTION", "PIP_CACHE_DIR", "HF_HOME", "SERVICE_TOKEN", "HOME"):
            self.assertEqual(native_host_environment(env)[key], base[key])
        if os.name == "nt":
            self.assertFalse({"Tmp", "temp", "tmpdir"} & env.keys())

    def test_worker_cache_environment_takes_precedence(self):
        with worker_caches.isolated(str(self.workspace)):
            env = self.environment()
        self.assertEqual(env["TMPDIR"], str(Path(worker_caches.cache_root_for(str(self.workspace))) / "tmp"))
        self.assertEqual(env["PIP_CACHE_DIR"], str(Path(worker_caches.cache_root_for(str(self.workspace))) / "pip"))

    def test_dispatch_hands_the_same_environment_to_native_handler(self):
        async def handler(content, ctx):
            self.assertEqual(content, "unchanged content")
            return {"temp": ctx["subproc_env"]["TMPDIR"]}

        facade = types.ModuleType("src.agent_tools")
        facade.TOOL_HANDLERS = {"python": handler}
        origin = run_causality.CallOrigin("session-a", "run-a", "tool-1")
        with patch.dict(sys.modules, {"src.agent_tools": facade}):
            result = asyncio.run(te._direct_fallback(
                "python", "unchanged content", session_id="session-a", _causal_call=origin))
        self.assertEqual(result, {"temp": self.environment()["TMPDIR"]})

    def test_all_native_command_handlers_receive_run_temp(self):
        async def handler(content, ctx):
            return {"root": ctx["subproc_env"]["TMPDIR"]}

        facade = types.ModuleType("src.agent_tools")
        facade.TOOL_HANDLERS = dict.fromkeys(("bash", "python", "powershell"), handler)
        origin = run_causality.CallOrigin("session-a", "run-a", "tool-1")
        with patch.dict(sys.modules, {"src.agent_tools": facade}):
            for tool in facade.TOOL_HANDLERS:
                with self.subTest(tool=tool):
                    result = asyncio.run(te._direct_fallback(
                        tool, "unchanged", session_id="session-a", _causal_call=origin))
                    self.assertEqual(result, {"root": self.environment()["TMPDIR"]})

    def test_other_tool_does_not_create_a_temp_folder(self):
        async def handler(content, ctx):
            return {"ok": True}

        facade = types.ModuleType("src.agent_tools")
        facade.TOOL_HANDLERS = {"read_file": handler}
        with patch.dict(sys.modules, {"src.agent_tools": facade}):
            result = asyncio.run(te._direct_fallback("read_file", "unchanged"))
        self.assertEqual(result, {"ok": True})
        self.assertFalse((self.workspace / ".faustus-tmp").exists())

    def test_without_workspace_uses_persistent_agent_directory(self):
        token = te._active_workspace.set(None)
        try:
            with patch.object(te, "_AGENT_WORKDIR", str(self.workspace)):
                env = self.environment()
            self.assertTrue(Path(env["TMPDIR"]).is_relative_to(self.workspace))
        finally:
            te._active_workspace.reset(token)

    def test_unavailable_directory_is_reported_without_global_fallback(self):
        facade = types.ModuleType("src.agent_tools")
        facade.TOOL_HANDLERS = {"python": None}
        with patch.dict(sys.modules, {"src.agent_tools": facade}), \
                patch.object(te.os, "makedirs", side_effect=OSError("unwritable temp")):
            result = asyncio.run(te._direct_fallback("python", "unchanged"))
        self.assertEqual(result, {"error": "python: unwritable temp", "exit_code": 1})

    def test_real_python_tempfile_uses_run_folder(self):
        env = native_host_environment(self.environment())
        command = "import json,tempfile; f=tempfile.NamedTemporaryFile(delete=False); f.close(); print(json.dumps({'path':f.name,'root':tempfile.gettempdir()}))"
        result = subprocess.run([sys.executable, "-c", command], env=env, cwd=self.workspace,
                                capture_output=True, text=True, check=True, timeout=15)
        observed = json.loads(result.stdout)
        self.assertEqual(Path(observed["root"]), Path(env["TMPDIR"]))
        self.assertTrue(Path(observed["path"]).is_relative_to(Path(env["TMPDIR"])))

    @unittest.skipUnless(os.name == "nt", "Windows native PowerShell contract")
    def test_real_powershell_tempfile_uses_run_folder(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(shell)
        env = native_host_environment(self.environment())
        command = "@{root=[IO.Path]::GetTempPath();path=[IO.Path]::GetTempFileName()}|ConvertTo-Json -Compress"
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                env=env, cwd=self.workspace, capture_output=True, text=True,
                                check=True, timeout=15)
        observed = json.loads(result.stdout)
        self.assertEqual(Path(observed["root"]), Path(env["TMPDIR"]))
        self.assertTrue(Path(observed["path"]).is_relative_to(Path(env["TMPDIR"])))

    @unittest.skipUnless(os.name == "nt", "Windows Git Bash contract")
    def test_real_git_bash_tmpdir_is_shared_with_native_children(self):
        bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        self.assertTrue(bash.is_file(), "Git Bash must be installed for this contract")
        env = native_host_environment(self.environment())
        child = shlex.quote(sys.executable.replace("\\", "/")) + " -c " + shlex.quote(
            "import tempfile; print(tempfile.gettempdir())")
        result = subprocess.run([bash, "-c", 'mktemp "$TMPDIR/probe.XXXXXX"; cygpath -w "$TMPDIR"; ' + child],
                                env=env, cwd=self.workspace, capture_output=True, text=True,
                                check=True, timeout=15)
        created, root, child_root = result.stdout.strip().splitlines()
        self.assertEqual(Path(root), Path(env["TMPDIR"]))
        self.assertEqual(Path(child_root), Path(env["TMPDIR"]))
        self.assertEqual(len(list(Path(root).glob("probe.*"))), 1)
        again = subprocess.run([bash, "-c", 'test -f "$TMPDIR/"probe.*'], env=env,
                               cwd=self.workspace, capture_output=True, text=True, timeout=15)
        self.assertEqual(again.returncode, 0, again.stderr)

    def _normal_dispatch(self, disconnected):
        """Exercise the public dispatcher; only unused application services stubbed."""
        facade = types.ModuleType("src.agent_tools")
        facade.ToolBlock = namedtuple("ToolBlock", "tool_type content")
        implementations = types.ModuleType("src.tool_implementations")
        async def unused(*args, **kwargs):
            raise AssertionError("Unrelated application service invoked")
        implementations.__getattr__ = lambda name: unused
        seen = []
        async def handler(content, ctx):
            root = Path(ctx["subproc_env"]["TMPDIR"])
            if content == "create temporary marker":
                command = [sys.executable, "-c", "import tempfile;from pathlib import Path;r=Path(tempfile.gettempdir());(r/'dispatch-marker').write_text('same-run');print(r)"]
            elif content == "check PowerShell temporary marker":
                command = [shutil.which("pwsh") or shutil.which("powershell"), "-NoProfile", "-NonInteractive", "-Command",
                           "[IO.Path]::GetTempPath();[IO.File]::ReadAllText((Join-Path $env:TMPDIR 'dispatch-marker'))"]
            else:
                command = ["C:/Program Files/Git/bin/bash.exe", "-c", 'cygpath -w "$TMPDIR"; cat "$TMPDIR/dispatch-marker"']
            child = subprocess.run(command, cwd=self.workspace,
                env=native_host_environment(ctx["subproc_env"]), text=True,
                capture_output=True, timeout=15)
            seen.append({"root": root, "ctx": dict(ctx), "rc": child.returncode, "stdout": child.stdout})
            return {"stdout": child.stdout, "stderr": child.stderr, "exit_code": child.returncode}
        facade.TOOL_HANDLERS = dict.fromkeys(("python", "powershell", "bash"), handler)
        class Disconnected:
            def __init__(self): self.calls = []
            async def call_tool(self, name, args):
                self.calls.append((name, args))
                return {"error": "fixture not connected", "exit_code": 1}
        manager = Disconnected() if disconnected else None
        async def run():
            for run_id in ("normal-run-a", "normal-run-b"):
                token = run_causality.bind_run("normal-session", run_id)
                try:
                    for tool, content in (("python", "create temporary marker"),
                                          ("powershell", "check PowerShell temporary marker"),
                                          ("bash", "check Bash temporary marker")):
                        await te.execute_tool_block(facade.ToolBlock(tool, content),
                            session_id="normal-session", owner="normal-owner",
                            workspace=str(self.workspace), security_context=te.NO_TOOL_SECURITY_CONTEXT)
                finally:
                    run_causality.reset_run(token)
        from src import exec_ledger
        with patch.dict(sys.modules, {"src.agent_tools": facade, "src.tool_implementations": implementations}), \
                patch.object(te, "get_mcp_manager", return_value=manager), \
                patch.object(te, "_owner_is_admin", return_value=True), \
                patch.object(exec_ledger, "enabled", return_value=False):
            asyncio.run(run())
        self.assertEqual(len(seen), 6)
        for index, row in enumerate(seen):
            run_id = "normal-run-a" if index < 3 else "normal-run-b"
            expected = Path(self.environment(run=run_id, session="normal-session")["TMPDIR"])
            with self.subTest(disconnected=disconnected, run=run_id, tool=index % 3):
                self.assertEqual(row["root"], expected)
                self.assertEqual(row["ctx"]["session_id"], "normal-session")
                self.assertEqual(row["ctx"]["owner"], "normal-owner")
                self.assertEqual(row["ctx"]["_causal_call"].run_id, run_id)
                self.assertEqual(row["rc"], 0, row["stdout"])
                if index % 3:
                    self.assertIn("same-run", row["stdout"])
        self.assertNotEqual(seen[0]["root"], seen[3]["root"])
        if manager:
            self.assertEqual(len(manager.calls), 4)  # python/bash only; PowerShell stays direct.

    @unittest.skipUnless(os.name == "nt", "Windows native dispatcher contract")
    def test_normal_dispatch_without_mcp_manager_preserves_run_temp(self):
        self._normal_dispatch(False)

    @unittest.skipUnless(os.name == "nt", "Windows native dispatcher contract")
    def test_normal_dispatch_disconnected_mcp_preserves_run_temp(self):
        self._normal_dispatch(True)

    def test_legacy_fallback_preserves_complete_execution_context(self):
        async def progress(event):
            return None
        context = dict(progress_cb=progress, session_id="session-context", owner="owner-context",
            human_approved=True, disabled_tools={"unused"}, tool_policy=object(),
            security_context=object(), pdf_call_binding=object(),
            _causal_call=run_causality.CallOrigin("session-context", "captured-run", "captured-call"))
        disconnected = types.SimpleNamespace(call_tool=AsyncMock(return_value={
            "error": "fixture not connected", "exit_code": 1}))
        for manager in (None, disconnected):
            with self.subTest(manager=manager is not None):
                fallback = AsyncMock(return_value={"exit_code": 0, "stdout": "preserved"})
                with patch.object(te, "get_mcp_manager", return_value=manager), \
                        patch.object(te, "_direct_fallback", fallback):
                    result = asyncio.run(te._call_mcp_tool("python", "print('same')", **context))
                self.assertEqual(result, {"exit_code": 0, "stdout": "preserved"})
                fallback.assert_awaited_once_with("python", "print('same')", **context)

    def test_connected_legacy_mcp_receives_only_original_tool_arguments(self):
        expected = {"stdout": "remote result", "exit_code": 0}
        manager = types.SimpleNamespace(call_tool=AsyncMock(return_value=expected))
        fallback = AsyncMock(side_effect=AssertionError("Connected MCP must not use direct fallback"))
        with patch.object(te, "get_mcp_manager", return_value=manager), \
                patch.object(te, "_direct_fallback", fallback):
            result = asyncio.run(te._call_mcp_tool("python", "print('same')",
                session_id="private-session", owner="private-owner",
                _causal_call=run_causality.CallOrigin("private-session", "private-run", None)))
        self.assertIs(result, expected)
        manager.call_tool.assert_awaited_once_with("mcp__python__python", {"code": "print('same')"})
        fallback.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
