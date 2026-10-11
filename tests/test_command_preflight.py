"""src/command_preflight.py: wrong-directory preflight for shell commands."""
from __future__ import annotations

import json

import pytest

from src import command_preflight as cp


def _touch(root, rel, text=""):
    p = root.joinpath(*rel.split("/"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def tree(tmp_path):
    _touch(tmp_path, "backend/server.py", "print('ok')")
    _touch(tmp_path, "README.md")
    _touch(tmp_path, "node_modules/x/server.py")
    _touch(tmp_path, ".git/hooks/server.py")
    return tmp_path


def test_single_candidate_is_rewritten(tree):
    r = cp.check("python server.py", str(tree))
    assert r.action == "rewrite"
    assert r.command == "cd backend && python server.py"
    assert r.candidates == ["backend"]
    assert "backend/" in r.note and "server.py" in r.note


def test_flags_and_args_are_kept(tree):
    r = cp.check("python3 -u server.py --port 80 > out.log 2>&1", str(tree))
    assert r.action == "rewrite"
    assert r.command.startswith("cd backend && python3 -u server.py --port 80")


def test_already_correct_runs_unchanged(tree):
    _touch(tree, "server.py")
    r = cp.check("python server.py", str(tree))
    assert r.action == "none" and r.command == "python server.py"


def test_none_found_fails_open(tree):
    r = cp.check("python missing.py", str(tree))
    assert r.action == "none"


def test_ambiguous_refuses_and_lists_candidates(tree):
    _touch(tree, "api/server.py")
    r = cp.check("python server.py", str(tree))
    assert r.action == "refuse"
    assert set(r.candidates) == {"api", "backend"}
    assert "api" in r.error and "backend" in r.error and "Nothing was run" in r.error
    assert cp.refusal(r)["exit_code"] == 2


def test_candidates_capped_at_eight(tmp_path):
    for i in range(12):
        _touch(tmp_path, f"svc{i:02d}/server.py")
    r = cp.check("python server.py", str(tmp_path))
    assert r.action == "refuse"
    assert len(r.candidates) == cp.MAX_CANDIDATES
    assert "and more" in r.error


def test_skipped_dirs_are_not_candidates(tree):
    r = cp.check("python server.py", str(tree))
    assert r.candidates == ["backend"]


def test_windows_paths_and_cd_prefix(tree):
    # a path with backslashes that exists only below backend/
    _touch(tree, "backend/app/main.py")
    r = cp.check("python app\\main.py", str(tree))
    assert r.action == "rewrite" and r.candidates == ["backend"]
    r2 = cp.check("python D:\\somewhere\\else.py", str(tree))
    assert r2.action == "none"


def test_cd_then_command_inserts_relative_cd(tmp_path):
    tree = tmp_path
    _touch(tree, "proj/backend/server.py")
    (tree / "proj" / "other").mkdir()
    r = cp.check("cd proj && python server.py", str(tree))
    assert r.action == "rewrite"
    assert r.command == "cd proj && cd backend && python server.py"


def test_cd_to_correct_dir_is_left_alone(tree):
    r = cp.check("cd backend && python server.py", str(tree))
    assert r.action == "none"


def test_cd_to_unknown_dir_is_doubt(tree):
    r = cp.check("cd nowhere && python server.py", str(tree))
    assert r.action == "none"


def test_powershell_uses_set_location(tree):
    r = cp.check("python server.py", str(tree), shell="powershell")
    assert r.command == "Set-Location -LiteralPath 'backend'; python server.py"


def test_python_module(tmp_path):
    _touch(tmp_path, "svc/pkg/__init__.py")
    _touch(tmp_path, "svc/pkg/tool.py")
    r = cp.check("python -m pkg.tool --x", str(tmp_path))
    assert r.action == "rewrite" and r.candidates == ["svc"]
    assert cp.check("python -m pip install x", str(tmp_path)).action == "none"
    assert cp.check("python -m json.tool a.json", str(tmp_path)).action == "none"


def test_npm_run_needs_script_in_package_json(tmp_path):
    _touch(tmp_path, "web/package.json", json.dumps({"scripts": {"dev": "vite"}}))
    r = cp.check("npm run dev", str(tmp_path))
    assert r.action == "rewrite" and r.command == "cd web && npm run dev"
    assert cp.check("npm run build", str(tmp_path)).action == "none"
    assert cp.check("npm run dev --prefix web", str(tmp_path)).action == "none"
    _touch(tmp_path, "package.json", "{}")
    assert cp.check("npm run dev", str(tmp_path)).action == "none"   # root has one


def test_pnpm_yarn_and_npm_start(tmp_path):
    _touch(tmp_path, "ui/package.json", json.dumps({"scripts": {"start": "x", "test": "y", "dev": "z"}}))
    assert cp.check("npm start", str(tmp_path)).command == "cd ui && npm start"
    assert cp.check("npm test", str(tmp_path)).command == "cd ui && npm test"
    assert cp.check("pnpm run dev", str(tmp_path)).command == "cd ui && pnpm run dev"
    assert cp.check("yarn dev", str(tmp_path)).command == "cd ui && yarn dev"
    assert cp.check("npm install", str(tmp_path)).action == "none"


def test_node_file(tmp_path):
    _touch(tmp_path, "server/app.js")
    r = cp.check("node app.js", str(tmp_path))
    assert r.command == "cd server && node app.js"
    assert cp.check("node -e \"1\"", str(tmp_path)).action == "none"


def test_pytest_paths(tmp_path):
    _touch(tmp_path, "backend/tests/test_x.py")
    r = cp.check("pytest tests/test_x.py -k foo::bar -q", str(tmp_path))
    assert r.action == "rewrite" and r.command.startswith("cd backend && pytest tests/test_x.py")
    r2 = cp.check("python -m pytest tests/test_x.py::test_a", str(tmp_path))
    assert r2.action == "rewrite" and r2.candidates == ["backend"]
    assert cp.check("pytest", str(tmp_path)).action == "none"
    assert cp.check("pytest --rootdir x tests/test_x.py", str(tmp_path)).action == "none"


def test_docker_compose(tmp_path):
    _touch(tmp_path, "deploy/docker-compose.yml")
    r = cp.check("docker compose up -d", str(tmp_path))
    assert r.command == "cd deploy && docker compose up -d"
    assert cp.check("docker-compose build", str(tmp_path)).command == "cd deploy && docker-compose build"
    assert cp.check("docker compose -f deploy/docker-compose.yml up", str(tmp_path)).action == "none"
    assert cp.check("docker compose version", str(tmp_path)).action == "none"


def test_uvicorn(tmp_path):
    _touch(tmp_path, "backend/main.py")
    r = cp.check("uvicorn main:app --reload --port 8000", str(tmp_path))
    assert r.command.startswith("cd backend && uvicorn main:app")
    assert cp.check("uvicorn main:app --app-dir backend", str(tmp_path)).action == "none"


def test_scripts(tmp_path):
    _touch(tmp_path, "tools/run.sh")
    _touch(tmp_path, "tools/go.ps1")
    assert cp.check("./run.sh --fast", str(tmp_path)).command == "cd tools && ./run.sh --fast"
    assert cp.check("bash run.sh", str(tmp_path)).command == "cd tools && bash run.sh"
    assert cp.check(".\\go.ps1", str(tmp_path), shell="powershell").command.startswith("Set-Location")


def test_non_matching_shapes_untouched(tree):
    for cmd in ("ls -la", "git status", "echo python server.py", "python -c \"print(1)\"",
                "false || python server.py", "cat x | python server.py",
                "python $(pwd)/server.py", "python << EOF\nprint(1)\nEOF", "python 'unterminated"):
        assert cp.check(cmd, str(tree)).action == "none", cmd


def test_chained_command_only_target_segment_gets_cd(tree):
    r = cp.check("echo hi; python server.py && echo done", str(tree))
    assert r.command == "echo hi; cd backend && python server.py && echo done"


def test_env_prefix_does_not_hide_target(tree):
    r = cp.check("PORT=80 python server.py", str(tree))
    assert r.command == "cd backend && PORT=80 python server.py"


def test_missing_or_bad_cwd_fails_open(tmp_path):
    assert cp.check("python server.py", str(tmp_path / "nope")).action == "none"
    assert cp.check("", str(tmp_path)).action == "none"


def test_add_note_prefixes_output():
    out = cp.add_note({"output": "ok", "exit_code": 0}, "[preflight] x")
    assert out["output"] == "[preflight] x\nok"
    err = cp.add_note({"error": "boom", "exit_code": 1}, "[preflight] x")
    assert err["error"].startswith("[preflight] x\n")


def test_setting_off_disables(tree, monkeypatch):
    import src.settings as st
    monkeypatch.setattr(st, "get_setting", lambda k, d=None: False if k == "shell_preflight_enabled" else d)
    assert cp.apply("python server.py", str(tree)).action == "none"
    monkeypatch.setattr(st, "get_setting", lambda k, d=None: d)
    assert cp.apply("python server.py", str(tree)).action == "rewrite"

# ---- tool glue ------------------------------------------------------------

def test_bash_tool_runs_rewritten_command_and_notes_it(tree, monkeypatch):
    import asyncio
    from src import tool_execution
    from src.agent_tools import subprocess_tools as st

    monkeypatch.setattr(tool_execution, "agent_cwd", lambda: str(tree))
    seen = {}

    async def fake_inner(self, content, ctx):
        seen["cmd"] = content
        return {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(st.BashTool, "_execute_inner", fake_inner)
    out = asyncio.run(st.BashTool().execute("python server.py", {}))
    assert seen["cmd"].endswith("python server.py") and "backend" in seen["cmd"]
    assert out["output"].startswith("[preflight] ran from backend/")
    assert out["output"].endswith("ok")


def test_bash_tool_refuses_ambiguous_without_running(tree, monkeypatch):
    import asyncio
    from src import tool_execution
    from src.agent_tools import subprocess_tools as st

    _touch(tree, "api/server.py")
    monkeypatch.setattr(tool_execution, "agent_cwd", lambda: str(tree))

    async def boom(self, content, ctx):
        raise AssertionError("must not run")

    monkeypatch.setattr(st.BashTool, "_execute_inner", boom)
    out = asyncio.run(st.BashTool().execute({"command": "python server.py"}, {}))
    assert out["exit_code"] == 2 and "api" in out["error"] and "backend" in out["error"]


def test_powershell_tool_uses_set_location(tree, monkeypatch):
    import asyncio
    from src import tool_execution
    from src.agent_tools import subprocess_tools as st

    monkeypatch.setattr(tool_execution, "agent_cwd", lambda: str(tree))
    monkeypatch.setattr(st.sandbox_exec, "refuse_host_only_tool", lambda name: None)
    seen = {}

    async def fake_inner(self, content, ctx):
        seen["cmd"] = content
        return {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(st.PowerShellTool, "_execute_inner", fake_inner)
    out = asyncio.run(st.PowerShellTool().execute("python server.py", {}))
    assert seen["cmd"] == "Set-Location -LiteralPath 'backend'; python server.py"
    assert "[preflight]" in out["output"]


# ---- python tool (OBJ-53) -------------------------------------------------

def test_infer_subprocess_list_and_shell_string(tree):
    code = 'import subprocess\nsubprocess.run(["python", "server.py"])\n'
    assert cp.inferred_shell_commands(code) == ["python server.py"]
    code_exe = 'import subprocess, sys\nsubprocess.run([sys.executable, "server.py"])\n'
    assert cp.inferred_shell_commands(code_exe) == ["python server.py"]
    code2 = 'import subprocess\nsubprocess.call("python server.py", shell=True)\n'
    assert cp.inferred_shell_commands(code2) == ["python server.py"]
    code3 = 'import runpy\nrunpy.run_path("server.py")\n'
    assert cp.inferred_shell_commands(code3) == ["python server.py"]
    code4 = 'import os\nos.system("pytest tests/test_x.py")\n'
    assert cp.inferred_shell_commands(code4) == ["pytest tests/test_x.py"]
    assert cp.inferred_shell_commands("print(1)") == []
    assert cp.inferred_shell_commands("def oops(") == []  # syntax error -> fail open


def test_python_code_single_candidate_inserts_cwd(tree):
    code = 'import subprocess\nsubprocess.run(["python", "server.py"])\n'
    r = cp.check_python_code(code, str(tree))
    assert r.action == "rewrite"
    assert r.candidates == ["backend"]
    assert "chdir" not in r.command
    assert 'cwd=\'backend\'' in r.command or 'cwd="backend"' in r.command
    assert r.command.startswith("import subprocess\n")
    assert "backend/" in r.note


def test_python_code_skips_call_that_already_sets_cwd(tree):
    """P1: explicit cwd= must not be double-rewritten to backend/backend."""
    code = (
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, 'server.py'], cwd='backend')\n"
    )
    r = cp.check_python_code(code, str(tree))
    assert r.action == "none"
    assert r.command == code


def test_python_code_preserves_other_relative_paths(tree):
    """P2: rewriting the launch must not break open('notes.txt') at workspace root."""
    _touch(tree, "notes.txt", "root-notes\n")
    code = (
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, 'server.py'], capture_output=True, text=True)\n"
        "print(open('notes.txt', encoding='utf-8').read().strip())\n"
    )
    r = cp.check_python_code(code, str(tree))
    assert r.action == "rewrite"
    assert "chdir" not in r.command
    assert "open('notes.txt'" in r.command
    # Execute rewritten source from workspace root (same as python -c cwd).
    import subprocess as sp
    import sys
    proc = sp.run([sys.executable, "-c", r.command], cwd=str(tree), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "root-notes" in proc.stdout


def test_python_code_keeps_future_import_first(tree):
    """P2: never inject a prelude before from __future__."""
    code = (
        "from __future__ import annotations\n"
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, 'server.py'])\n"
    )
    r = cp.check_python_code(code, str(tree))
    assert r.action == "rewrite"
    assert r.command.startswith("from __future__ import annotations\n")
    assert "chdir" not in r.command
    compile(r.command, "<preflight>", "exec")  # must stay valid


def test_python_code_runpy_rewrites_path_literal(tree):
    code = 'import runpy\nrunpy.run_path("server.py")\n'
    r = cp.check_python_code(code, str(tree))
    assert r.action == "rewrite"
    assert 'run_path("backend/server.py")' in r.command or "run_path('backend/server.py')" in r.command


def test_python_code_ambiguous_refuses(tree):
    _touch(tree, "api/server.py")
    code = 'import subprocess\nsubprocess.run(["python", "server.py"])\n'
    r = cp.check_python_code(code, str(tree))
    assert r.action == "refuse"
    assert set(r.candidates) == {"api", "backend"}
    assert "Nothing was run" in r.error


def test_python_code_no_launch_fails_open(tree):
    r = cp.check_python_code("print('hello')", str(tree))
    assert r.action == "none" and r.command == "print('hello')"


def test_python_code_dynamic_argv_fails_open(tree):
    code = 'import subprocess\nsubprocess.run(["python", name])\n'
    r = cp.check_python_code(code, str(tree))
    assert r.action == "none"


def test_apply_python_respects_setting(tree, monkeypatch):
    import src.settings as st
    code = 'import subprocess\nsubprocess.run(["python", "server.py"])\n'
    monkeypatch.setattr(st, "get_setting", lambda k, d=None: False if k == "shell_preflight_enabled" else d)
    assert cp.apply_python(code, str(tree)).action == "none"
    monkeypatch.setattr(st, "get_setting", lambda k, d=None: d)
    assert cp.apply_python(code, str(tree)).action == "rewrite"


def test_python_tool_inserts_cwd_and_notes(tree, monkeypatch):
    import asyncio
    from src import tool_execution
    from src.agent_tools import subprocess_tools as st

    monkeypatch.setattr(tool_execution, "agent_cwd", lambda: str(tree))
    seen = {}

    async def fake_run(tool, command, ctx=None):
        seen["code"] = command
        return {"output": "ok", "exit_code": 0, "sandboxed": True}

    monkeypatch.setattr(st.sandbox_exec, "run", fake_run)
    code = 'import subprocess\nsubprocess.run(["python", "server.py"])\n'
    out = asyncio.run(st.PythonTool().execute(code, {}))
    assert "chdir" not in seen["code"]
    assert "cwd=" in seen["code"] and "backend" in seen["code"]
    assert out["output"].startswith("[preflight] ran from backend/")
    assert out["output"].endswith("ok")


def test_python_tool_refuses_ambiguous_without_running(tree, monkeypatch):
    import asyncio
    from src import tool_execution
    from src.agent_tools import subprocess_tools as st

    _touch(tree, "api/server.py")
    monkeypatch.setattr(tool_execution, "agent_cwd", lambda: str(tree))

    async def boom(tool, command, ctx=None):
        raise AssertionError("must not run")

    monkeypatch.setattr(st.sandbox_exec, "run", boom)
    code = 'import subprocess\nsubprocess.run(["python", "server.py"])\n'
    out = asyncio.run(st.PythonTool().execute({"code": code}, {}))
    assert out["exit_code"] == 2 and "api" in out["error"] and "backend" in out["error"]


def test_python_tool_real_subprocess_on_windows(tree):
    """Host path: cwd= on the call makes nested python see backend/server.py."""
    import asyncio
    from src import tool_execution
    from src.agent_tools import subprocess_tools as st

    async def no_sandbox(tool, command, ctx=None):
        return None

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(tool_execution, "agent_cwd", lambda: str(tree))
    monkeypatch.setattr(st.sandbox_exec, "run", no_sandbox)
    monkeypatch.setattr(st.sandbox_exec, "consume_skip_reason", lambda: "")
    try:
        code = (
            "import subprocess, sys\n"
            "r = subprocess.run([sys.executable, 'server.py'], capture_output=True, text=True)\n"
            "print(r.stdout.strip())\n"
            "raise SystemExit(r.returncode)\n"
        )
        out = asyncio.run(st.PythonTool().execute(code, {}))
        assert out["exit_code"] == 0
        assert "[preflight] ran from backend/" in out["output"]
        assert "ok" in out["output"]
    finally:
        monkeypatch.undo()


def test_python_explicit_cwd_still_works_when_executed(tree):
    """Regression of Claude's P1 repro: cwd='backend' must stay working."""
    import subprocess as sp
    import sys
    code = (
        "import subprocess, sys\n"
        "r = subprocess.run([sys.executable, 'server.py'], cwd='backend', "
        "capture_output=True, text=True)\n"
        "print(r.stdout.strip())\n"
        "raise SystemExit(r.returncode)\n"
    )
    r = cp.check_python_code(code, str(tree))
    assert r.action == "none"
    proc = sp.run([sys.executable, "-c", code], cwd=str(tree), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout



def test_python_trailing_comma_multiline_call(tree):
    """Claude r2 CHANGES: black/ruff trailing comma must not yield SyntaxError."""
    import subprocess as sp
    import sys

    code = (
        "import subprocess, sys\n"
        "r = subprocess.run([sys.executable, 'server.py'],\n"
        ")\n"
        "print('done', r.returncode)\n"
    )
    r = cp.check_python_code(code, str(tree))
    assert r.action == "rewrite"
    assert "cwd=" in r.command and "backend" in r.command
    assert ",\n," not in r.command and ", ," not in r.command
    compile(r.command, "<rewritten>", "exec")
    proc = sp.run([sys.executable, "-c", r.command], cwd=str(tree),
                  capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "done" in proc.stdout


def test_python_trailing_comma_with_kwarg(tree):
    import subprocess as sp
    import sys

    code = (
        "import subprocess, sys\n"
        "r = subprocess.run(\n"
        "    [sys.executable, 'server.py'],\n"
        "    capture_output=True,\n"
        ")\n"
        "print(r.stdout.decode() if isinstance(r.stdout, bytes) else (r.stdout or ''))\n"
        "raise SystemExit(r.returncode)\n"
    )
    r = cp.check_python_code(code, str(tree))
    assert r.action == "rewrite"
    compile(r.command, "<rewritten>", "exec")
    proc = sp.run([sys.executable, "-c", r.command], cwd=str(tree),
                  capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "ok" in proc.stdout


def test_python_nonascii_prefix_same_line_rewrites(tree):
    """col_offset is UTF-8 bytes; rewrite must still land on the call."""
    code = (
        "import subprocess, sys\n"
        "print('ñandú'); subprocess.run([sys.executable, 'server.py'])\n"
    )
    r = cp.check_python_code(code, str(tree))
    assert r.action == "rewrite"
    assert "cwd=" in r.command
    compile(r.command, "<rewritten>", "exec")

