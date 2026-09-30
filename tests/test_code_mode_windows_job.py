import asyncio
import ctypes
import gc
import json
import os
from ctypes import wintypes
from pathlib import Path

import pytest

from src.code_mode import runner, bridge, windows_job_bootstrap as jobs

pytestmark = [pytest.mark.skipif(os.name != "nt", reason="Windows JobObject native fixture"),
 pytest.mark.usefixtures("code_mode_host_runtime")]


def kernel():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in (
        ("OpenProcess", [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD], wintypes.HANDLE),
        ("WaitForSingleObject", [wintypes.HANDLE,wintypes.DWORD], wintypes.DWORD),
        ("TerminateProcess", [wintypes.HANDLE,wintypes.UINT], wintypes.BOOL),
        ("GetHandleInformation", [wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ):
        fn = getattr(api,name); fn.argtypes, fn.restype = args,result
    return api


@pytest.fixture
def owned(monkeypatch):
    made = []
    factory = jobs.create_owned_job
    def create():
        job = factory(); made.append(job); return job
    monkeypatch.setattr(jobs,"create_owned_job",create)
    monkeypatch.setattr(runner,"_limits",lambda: {"timeout_seconds":1,"max_calls":3,"max_output_bytes":1000,"max_memory_bytes":0})
    processes = []
    spawn = asyncio.create_subprocess_exec
    async def tracked(*a, **k):
        process = await spawn(*a, **k); processes.append(process); return process
    monkeypatch.setattr(runner.asyncio,"create_subprocess_exec",tracked)
    return made, processes


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["normal","timeout","cancel"])
async def test_owned_job_terminates_real_descendant_handles(owned, monkeypatch, finish):
    api=kernel(); handles=[]; ready=asyncio.Event()
    async def dispatch(name,args,**kwargs):
        handle=api.OpenProcess(0x00100000|1,False,args["pid"])
        assert handle
        handles.append(handle); ready.set()
        return {"output":"ready","exit_code":0}
    monkeypatch.setattr(bridge,"dispatch_call",dispatch)
    code="import sys,subprocess,time\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)\ntools.call('fixture',{'pid':p.pid})\n"
    code += "print('finished')" if finish=="normal" else "time.sleep(30)"
    before=asyncio.all_tasks(); task=asyncio.create_task(runner.run_code_mode(code))
    try:
        await asyncio.wait_for(ready.wait(),10)
        if finish=="cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            result=await asyncio.wait_for(task,10)
            assert result["process_containment"]=={"mechanism":"windows_job_object","assigned":True}
            if finish=="normal":assert result["exit_code"]==0 and result["output"].strip()=="finished"
            else:assert result["receipt"]["terminated_by"]=="timeout"
        assert owned[0][0].handle is None
        assert owned[1][0].returncode is not None
        assert api.WaitForSingleObject(handles[0],5000)==0
        assert not [t for t in asyncio.all_tasks()-before if not t.done()]
        gc.collect()
    finally:
        for handle in handles:
            api.TerminateProcess(handle,1);api.WaitForSingleObject(handle,5000);api.CloseHandle(handle)
        if not task.done():task.cancel()
        await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["ack","drain"])
async def test_early_cancel_before_pump_reaps_child_and_closes_job(owned,monkeypatch,tmp_path,point):
    ready=asyncio.Event(); marker=tmp_path/"user-ran"
    if point=="ack":
        async def read(stream):
            ready.set(); await asyncio.Future()
        monkeypatch.setattr(runner,"_read_line",read)
    else:
        spawn=asyncio.create_subprocess_exec
        async def tracked(*a,**k):
            process=await spawn(*a,**k)
            async def drain():ready.set();await asyncio.Future()
            process.stdin.drain=drain
            return process
        monkeypatch.setattr(runner.asyncio,"create_subprocess_exec",tracked)
    task=asyncio.create_task(runner.run_code_mode("from pathlib import Path; import time; Path("+repr(str(marker))+").write_text('ran');time.sleep(30)"))
    await asyncio.wait_for(ready.wait(),10);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert owned[0][0].handle is None and owned[1][0].returncode is not None
    if point=="ack":assert not marker.exists()


def test_native_parent_handle_noninheritable_and_closed():
    api=kernel();job=jobs.create_owned_job();handle=job.handle;flags=wintypes.DWORD()
    assert api.GetHandleInformation(handle,ctypes.byref(flags))
    assert not flags.value&1
    job.close();job.close()
    assert not api.GetHandleInformation(handle,ctypes.byref(flags))


@pytest.mark.asyncio
async def test_creation_failure_never_spawns(monkeypatch,tmp_path):
    def fail():raise jobs.WindowsJobError("windows_job_create_failed",5)
    async def forbidden(*a,**k):pytest.fail("no child after job creation failure")
    monkeypatch.setattr(jobs,"create_owned_job",fail)
    monkeypatch.setattr(runner.asyncio,"create_subprocess_exec",forbidden)
    result=await runner.run_code_mode("raise AssertionError('must not run')")
    assert result["error_code"]=="windows_job_create_failed"
    assert "process_containment" not in result


@pytest.mark.asyncio
async def test_closed_job_open_failure_never_runs_user_code(monkeypatch,tmp_path):
    factory=jobs.create_owned_job
    def closed():job=factory();job.close();return job
    monkeypatch.setattr(jobs,"create_owned_job",closed)
    marker=tmp_path/"user-ran"
    result=await runner.run_code_mode("open("+repr(str(marker))+",'w').write('ran')")
    assert result["error_code"]=="windows_job_open_failed"
    assert "process_containment" not in result and not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["assignment","bootstrap_close","invalid_ack"])
async def test_bootstrap_native_failure_or_invalid_ack_blocks_guest(owned,monkeypatch,tmp_path,failure):
    proxy=tmp_path/"bootstrap.py";real=str(Path(jobs.__file__).resolve());marker=tmp_path/"user-ran"
    code="import runpy,sys\nns=runpy.run_path("+repr(real)+",run_name='trusted_fixture')\n"
    if failure=="invalid_ack":
        code += "print('{\"type\":\"containment_ready\",\"protocol\":true,\"mechanism\":\"windows_job_object\"}',flush=True)\nimport time;time.sleep(30)\n"
    else:
        code += "k=ns['_kernel']()\n"
        if failure=="assignment":
            code += "outer=k.CreateJobObjectW(None,None)\nassert k.AssignProcessToJobObject(outer,k.GetCurrentProcess())\nk.CloseHandle(outer)\noriginal=k.OpenJobObjectW\nk.OpenJobObjectW=lambda access,inherit,name: original(4,inherit,name)\n"
        else:
            code += "k.CloseHandle=lambda h: False\n"
        code += "ns['_assign_current'].__globals__['_kernel']=lambda:k\nsys.exit(ns['main']())\n"
    proxy.write_text(code,encoding="utf-8")
    monkeypatch.setattr(runner,"_WINDOWS_BOOTSTRAP_PATH",str(proxy))
    result=await runner.run_code_mode("open("+repr(str(marker))+",'w').write('ran')")
    expected={"assignment":"windows_job_assign_failed","bootstrap_close":"windows_job_bootstrap_close_failed","invalid_ack":"windows_job_protocol_failed"}[failure]
    assert result["error_code"]==expected
    assert "process_containment" not in result and not marker.exists()
    assert owned[0][0].handle is None and owned[1][0].returncode is not None


@pytest.mark.asyncio
async def test_spawn_failure_closes_parent_job(owned,monkeypatch):
    async def fail(*a,**k):raise OSError("synthetic spawn failure")
    monkeypatch.setattr(runner.asyncio,"create_subprocess_exec",fail)
    result=await runner.run_code_mode("raise AssertionError('must not run')")
    assert result["exit_code"]==1 and owned[0][0].handle is None
    assert "process_containment" not in result


def test_configuration_failure_closes_native_handle(monkeypatch):
    closed=[]
    class Fake:
        def CreateJobObjectW(self,*a):return 123
        def SetHandleInformation(self,*a):ctypes.set_last_error(5);return False
        def CloseHandle(self,handle):closed.append(handle);return True
    monkeypatch.setattr(jobs,"_kernel",lambda:Fake())
    ctypes.set_last_error(0)
    with pytest.raises(jobs.WindowsJobError) as error:jobs.create_owned_job()
    assert error.value.code=="windows_job_configure_failed" and closed==[123]
