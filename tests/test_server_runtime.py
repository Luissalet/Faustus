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
