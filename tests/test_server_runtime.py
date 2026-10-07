from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import json
import subprocess
import sys
import time
import urllib.request
import psutil
import pytest
import server_runtime as runtime


def record():return {'pid':123,'created':10.0,'token':'owned-token','port':7000,'owner':'desktop'}


def test_pid_reuse_is_not_ownership(monkeypatch):
    process=Mock();process.create_time.return_value=11.0
    monkeypatch.setattr(runtime.psutil,'Process',lambda _:process)
    assert runtime.owned_process(record()) is None
    process.terminate.assert_not_called()


def test_command_and_workspace_must_match(monkeypatch,tmp_path):
    process=Mock();process.create_time.return_value=10.0
    process.cmdline.return_value=['python','unrelated.py','serve','owned-token']
    monkeypatch.setattr(runtime.psutil,'Process',lambda _:process)
    assert runtime.owned_process(record()) is None
    process.cmdline.return_value=['python',str(runtime.ROOT/'server_runtime.py'),'serve','owned-token']
    process.cwd.return_value=str(tmp_path)
    assert runtime.owned_process(record()) is None
    process.cwd.return_value=str(runtime.ROOT)
    assert runtime.owned_process(record()) is process


def test_window_cannot_stop_another_launch(monkeypatch):
    monkeypatch.setattr(runtime,'launch_lock',nullcontext)
    monkeypatch.setattr(runtime,'read_record',record)
    stop=Mock();monkeypatch.setattr(runtime,'owned_process',stop)
    assert runtime.stop('different-token')['stopped'] is False
    stop.assert_not_called()


def test_external_server_is_reused_not_adopted(monkeypatch):
    monkeypatch.setattr(runtime,'launch_lock',nullcontext)
    monkeypatch.setattr(runtime,'read_record',lambda:{})
    monkeypatch.setattr(runtime,'owned_process',lambda _:None)
    monkeypatch.setattr(runtime,'listening',lambda _:True)
    monkeypatch.setattr(runtime,'healthy',lambda _:True)
    result=runtime.start()
    assert result['started'] is False and result['owner']=='external'
    assert 'token' not in result


def test_emergency_stopper_recognizes_checkout_entrypoints_only():
    root=str(runtime.ROOT)
    venv=str(runtime.ROOT/'venv'/'Scripts'/'python.exe')
    electron=str(runtime.ROOT/'desktop'/'node_modules'/'electron'/'dist'/'electron.exe')
    assert runtime._is_faustus_process({
        'exe':venv,'cwd':root,'cmdline':[venv,'-m','uvicorn','app:app'],'pid':10,'ppid':1,
    })
    assert runtime._is_faustus_process({
        'exe':electron,'cwd':root,'cmdline':[electron,'--type=renderer'],'pid':11,'ppid':10,
    })
    assert runtime._is_faustus_process({
        'exe':r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
        'cwd':r'C:\Windows',
        'cmdline':['powershell.exe','-File',str(runtime.ROOT/'launch-windows.ps1')],
        'pid':12,'ppid':1,
    })


def test_emergency_stopper_does_not_claim_unrelated_python_or_port_service():
    # The same interpreter can be used for tests or maintenance. A venv path,
    # generic python name, or conventional port alone is not ownership.
    venv=str(runtime.ROOT/'venv'/'Scripts'/'python.exe')
    assert not runtime._is_faustus_process({
        'exe':venv,'cwd':str(runtime.ROOT),'cmdline':[venv,'-m','pytest','tests'],
        'pid':20,'ppid':1,
    })
    assert not runtime._is_faustus_process({
        'exe':r'C:\Python313\python.exe','cwd':r'D:\somewhere-else',
        'cmdline':['python','unrelated_server.py','--port','7000'],'pid':21,'ppid':1,
    })


def test_stop_all_preserves_model_servers_and_reports_managed_stop(monkeypatch,tmp_path):
    monkeypatch.setattr(runtime,'RUNTIME',tmp_path)
    monkeypatch.setattr(runtime,'RECORD',tmp_path/'server.json')
    monkeypatch.setattr(runtime,'STOP',tmp_path/'stop.json')
    monkeypatch.setattr(runtime,'stop',lambda wait_seconds=30:{'stopped':True,'port':7000})
    monkeypatch.setattr(runtime,'discover_faustus_processes',lambda:[])
    monkeypatch.setattr(runtime,'owned_process',lambda record:None)
    result=runtime.stop_all()
    assert result['stopped'] is True
    assert result['remaining']==[]
    assert result['preserved']==['ollama','llama-server','unrelated-python']


def test_shutdown_spares_children_launched_to_outlive_the_server(monkeypatch, tmp_path):
    """The WhatsApp bridge and launch profiles are detached children; on
    Windows they still list the server as parent, so the shutdown sweep must
    read data/runtime/detached.json and leave them (and their subtrees)."""
    import json
    monkeypatch.setattr(runtime, "RUNTIME", tmp_path)
    bridge = SimpleNamespace(pid=501, create_time=lambda: 1000.0, children=lambda recursive=True: [SimpleNamespace(pid=502)])
    stale = SimpleNamespace(pid=601, create_time=lambda: 999.0, children=lambda recursive=True: [])   # pid recycled
    (tmp_path / "detached.json").write_text(json.dumps({"501": {"created": 1000.0}, "601": {"created": 5.0}}), encoding="utf-8")
    procs = {501: bridge, 601: stale}
    monkeypatch.setattr(runtime.psutil, "Process", lambda pid=None: procs[pid] if pid is not None else SimpleNamespace(
        children=lambda recursive=True: [SimpleNamespace(pid=501), SimpleNamespace(pid=502), SimpleNamespace(pid=601), SimpleNamespace(pid=700)]))
    doomed = sorted(c.pid for c in runtime._children_to_terminate())
    assert doomed == [601, 700]


def test_spawn_detached_records_the_child_in_the_ledger(monkeypatch, tmp_path):
    import json
    from src import process_launch, process_ownership
    monkeypatch.setattr(process_launch, "detached_ledger_path", lambda: str(tmp_path / "detached.json"))
    monkeypatch.setattr(process_ownership, "creation_time", lambda pid: 42.0 if pid == 77 else 0.0)
    process_launch._note_detached(77, 42.0, "node server.mjs")
    ledger = json.loads((tmp_path / "detached.json").read_text())
    assert ledger == {"77": {"created": 42.0, "command": "node server.mjs"}}
    # a recycled pid is pruned on the next write
    monkeypatch.setattr(process_ownership, "creation_time", lambda pid: 43.0 if pid == 77 else 9.0)
    process_launch._note_detached(78, 9.0, "x")
    ledger = json.loads((tmp_path / "detached.json").read_text())
    assert set(ledger) == {"78"}


@pytest.mark.parametrize("stale_ledger", [False, True], ids=["verified-detached-app", "stale-create-time"])
def test_stop_timeout_preserves_only_verified_detached_app_tree(monkeypatch, tmp_path, stale_ledger):
    """Exercise the real timeout fallback with a parent, stdio helper and app tree."""
    app_ready = tmp_path / "app-ready.json"
    fixture_state = tmp_path / "fixture-state.json"
    app_code = r'''import http.server, json, subprocess, sys
from pathlib import Path
flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"healthy")
    def log_message(self, *args): pass
server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
Path(sys.argv[1]).write_text(json.dumps({"port": server.server_port, "worker_pid": worker.pid}), encoding="utf-8")
server.serve_forever()
'''
    parent_code = r'''import json, subprocess, sys, time
from pathlib import Path
flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
app = subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
ready = Path(sys.argv[2])
deadline = time.monotonic() + 10
while not ready.exists() and time.monotonic() < deadline: time.sleep(.02)
if not ready.exists(): raise SystemExit("fixture app did not start")
Path(sys.argv[3]).write_text(json.dumps({"helper_pid": helper.pid, "app_pid": app.pid}), encoding="utf-8")
time.sleep(120)
'''
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, app_code, str(app_ready), str(fixture_state)],
        cwd=str(tmp_path), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=flags,
    )
    app_pid = worker_pid = helper_pid = None
    process_identities = {}

    def alive(pid):
        try:
            process = psutil.Process(pid)
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def wait_dead(pid, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not alive(pid): return
            time.sleep(.03)
        assert not alive(pid), f"process {pid} remained alive"

    try:
        deadline = time.monotonic() + 12
        while not fixture_state.exists() and time.monotonic() < deadline: time.sleep(.03)
        assert fixture_state.exists(), "fixture parent did not report child processes"
        fixture = json.loads(fixture_state.read_text(encoding="utf-8"))
        app_pid, helper_pid = int(fixture["app_pid"]), int(fixture["helper_pid"])
        app_info = json.loads(app_ready.read_text(encoding="utf-8"))
        worker_pid, port = int(app_info["worker_pid"]), int(app_info["port"])
        app_process = psutil.Process(app_pid)
        app_created = app_process.create_time()
        process_identities = {
            parent.pid: psutil.Process(parent.pid).create_time(),
            helper_pid: psutil.Process(helper_pid).create_time(),
            app_pid: app_created,
            worker_pid: psutil.Process(worker_pid).create_time(),
        }

        monkeypatch.setattr(runtime, "RUNTIME", tmp_path)
        monkeypatch.setattr(runtime, "RECORD", tmp_path / "server.json")
        monkeypatch.setattr(runtime, "STOP", tmp_path / "stop.json")
        monkeypatch.setattr(runtime, "launch_lock", nullcontext)
        record_value = {"pid": parent.pid, "created": psutil.Process(parent.pid).create_time(), "token": "fixture-token", "port": 7000}
        monkeypatch.setattr(runtime, "read_record", lambda: record_value)
        monkeypatch.setattr(runtime, "owned_process", lambda _record: psutil.Process(parent.pid) if alive(parent.pid) else None)
        ledger_created = app_created - 2 if stale_ledger else app_created
        (tmp_path / "detached.json").write_text(json.dumps({str(app_pid): {"created": ledger_created}}), encoding="utf-8")

        result = runtime.stop("fixture-token", wait_seconds=.05)
        assert result == {"stopped": True, "port": 7000}
        wait_dead(parent.pid)
        wait_dead(helper_pid)
        if stale_ledger:
            wait_dead(app_pid)
            wait_dead(worker_pid)
        else:
            assert alive(app_pid) and alive(worker_pid)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                assert response.status == 200 and response.read() == b"healthy"
    finally:
        # Isolated test cleanup owns every fixture PID, including a preserved app.
        for pid in (parent.pid, helper_pid, app_pid, worker_pid):
            if pid and pid in process_identities:
                try:
                    process = psutil.Process(pid)
                    if abs(process.create_time()-process_identities[pid])<=.01:
                        runtime.terminate_tree(process)
                except psutil.Error:pass
        try:parent.wait(timeout=2)
        except subprocess.TimeoutExpired:parent.kill();parent.wait(timeout=2)


def test_emergency_stopper_leaves_an_instance_of_another_checkout_alone():
    """Same venv, another worktree: a test instance on another port and its
    MCP children are not this checkout's processes."""
    venv=str(runtime.ROOT/'venv'/'Scripts'/'python.exe')
    other=str(runtime.ROOT.parent/'_claude_tmp'/'brain_wt')
    assert not runtime._is_faustus_process({
        'exe':venv,'cwd':other,
        'cmdline':[venv,'-m','uvicorn','app:app','--port','7006'],'pid':30,'ppid':1,
    })
    assert not runtime._is_faustus_process({
        'exe':venv,'cwd':other,
        'cmdline':[venv,other+'/mcp_servers/files_server.py'],'pid':31,'ppid':30,
    })
    # This checkout's own MCP child still counts, wherever its cwd is.
    assert runtime._is_faustus_process({
        'exe':venv,'cwd':r'C:\Windows',
        'cmdline':[venv,str(runtime.ROOT/'mcp_servers'/'files_server.py')],'pid':32,'ppid':1,
    })
