from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
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
