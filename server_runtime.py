"""Owned local server lifecycle shared by the desktop and web launchers.

Never stops a process merely because it listens on a port or uses our venv.
The PID, creation time, command and launch token must all match our record.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import psutil

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "data" / "runtime"
RECORD = RUNTIME / "server.json"
STOP = RUNTIME / "stop.json"


def read_record():
    try:
        value=json.loads(RECORD.read_text(encoding="utf-8"))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):
        return {}


def owned_process(record):
    try:
        process=psutil.Process(int(record["pid"]))
        command=process.cmdline()
        if abs(process.create_time()-float(record["created"]))>.01:
            return None
        if str(ROOT / "server_runtime.py") not in command or "serve" not in command or record["token"] not in command:
            return None
        if Path(process.cwd()).resolve()!=ROOT:
            return None
        return process
    except (KeyError,ValueError,TypeError,psutil.Error):
        return None


@contextmanager
def launch_lock():
    RUNTIME.mkdir(parents=True,exist_ok=True)
    with (RUNTIME / "launch.lock").open("a+b") as handle:
        handle.seek(0);handle.write(b"0");handle.flush();handle.seek(0)
        if os.name=="nt":
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
        try:yield
        finally:
            handle.seek(0)
            if os.name=="nt":msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


def listening(port):
    with socket.socket() as connection:
        connection.settimeout(.5)
        return connection.connect_ex(("127.0.0.1",port))==0


def healthy(port):
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/health",timeout=2) as response:
            return json.loads(response.read(4096)).get("status")=="healthy"
    except Exception:return False


def terminate_tree(process):
    try:children=process.children(recursive=True)
    except psutil.Error:children=[]
    for item in [*reversed(children),process]:
        try:item.terminate()
        except psutil.Error:pass
    _,alive=psutil.wait_procs([*children,process],timeout=3)
    for item in alive:
        try:item.kill()
        except psutil.Error:pass
    if alive:psutil.wait_procs(alive,timeout=5)


def _normal_path(value):
    """Comparable path text without requiring the process path to exist."""
    if value is None or str(value).strip()=="":return ""
    try:return os.path.normcase(os.path.abspath(str(value))).replace("\\", "/")
    except (OSError,TypeError,ValueError):return ""


def _inside(path, parent):
    path,parent=_normal_path(path),_normal_path(parent)
    return bool(path and parent and (path==parent or path.startswith(parent.rstrip("/")+"/")))


def _process_details(process):
    """Read what Windows permits; an elevated process may deny some fields."""
    details={"pid":process.pid,"ppid":0,"name":"","exe":"","cwd":"","cmdline":[]}
    for key,reader in (
        ("ppid",process.ppid),("name",process.name),("exe",process.exe),
        ("cwd",process.cwd),("cmdline",process.cmdline),
    ):
        try:details[key]=reader()
        except (psutil.Error,OSError):pass
    return details


def _is_faustus_process(details):
    """Strong identity test used by the emergency all-instance stopper.

    Merely listening on port 7000, being named python.exe, or using a Python
    from the virtualenv is intentionally insufficient. This keeps unrelated
    local services and one-off developer commands safe.
    """
    root=_normal_path(ROOT)
    venv=_normal_path(ROOT / "venv")
    electron=_normal_path(ROOT / "desktop" / "node_modules" / "electron")
    exe=_normal_path(details.get("exe"))
    cwd=_normal_path(details.get("cwd"))
    argv=[str(value) for value in (details.get("cmdline") or [])]
    command=" ".join(argv).replace("\\", "/").lower()
    markers=(
        "server_runtime.py", "launch-windows.ps1", "start-faustus.ps1",
        "start-faustus-desktop.ps1", "app:app", "mcp_servers/",
        "/desktop/main.cjs", "/desktop\"", "/desktop'",
    )
    marked=any(marker in command for marker in markers)

    # Every Electron executable below this checkout is part of Faustus. This
    # includes renderer/GPU helpers whose commands omit the desktop argument.
    if _inside(exe,electron):return True
    # Managed/manual Python servers and orphan MCP children from this venv,
    # when they run THIS checkout. The venv is shared: a second instance
    # started from another worktree of the repository (its own cwd, its own
    # app and mcp_servers paths) uses the same interpreter, and stopping the
    # main instance once killed such a test instance three hours into a
    # run (26-09-2026).
    # (argv[0] is the shared interpreter itself, so it cannot say which
    # checkout the process runs.)
    root_in_args = root.lower() in " ".join(argv[1:]).replace("\\", "/").lower()
    other_checkout = bool(cwd) and not _inside(cwd, root) and not root_in_args
    if _inside(exe,venv) and marked and not other_checkout:return True
    # System Python or PowerShell launchers require both checkout and entrypoint.
    if root_in_args and marked:return True
    # Manual ``python -m uvicorn app:app`` can expose only its working directory.
    if cwd==root and ("app:app" in command or "server_runtime.py" in command):return True
    return False


def _ancestor_pids(pid):
    result=set()
    try:process=psutil.Process(pid)
    except psutil.Error:return result
    while True:
        try:process=process.parent()
        except psutil.Error:break
        if not process:break
        result.add(process.pid)
    return result


def _basic_process_snapshot():
    """Fast (pid, parent pid, image name) snapshot.

    psutil's Windows process_iter asks the kernel about every process one by
    one; on a busy desktop that can take 15-30 seconds. Toolhelp provides the
    same routing data in a single snapshot, which keeps the stopper immediate.
    """
    if os.name!="nt":
        return [(p.pid,p.info.get("ppid") or 0,p.info.get("name") or "")
                for p in psutil.process_iter(["pid","ppid","name"],ad_value="")]
    import ctypes
    from ctypes import wintypes
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_=[
            ("dwSize",wintypes.DWORD),("cntUsage",wintypes.DWORD),
            ("th32ProcessID",wintypes.DWORD),("th32DefaultHeapID",ctypes.c_size_t),
            ("th32ModuleID",wintypes.DWORD),("cntThreads",wintypes.DWORD),
            ("th32ParentProcessID",wintypes.DWORD),("pcPriClassBase",wintypes.LONG),
            ("dwFlags",wintypes.DWORD),("szExeFile",wintypes.WCHAR*260),
        ]
    kernel32=ctypes.WinDLL("kernel32",use_last_error=True)
    snapshot=kernel32.CreateToolhelp32Snapshot(0x00000002,0)
    invalid=ctypes.c_void_p(-1).value
    if snapshot==invalid:raise ctypes.WinError(ctypes.get_last_error())
    entry=PROCESSENTRY32W();entry.dwSize=ctypes.sizeof(entry)
    result=[]
    try:
        ok=kernel32.Process32FirstW(snapshot,ctypes.byref(entry))
        while ok:
            result.append((int(entry.th32ProcessID),int(entry.th32ParentProcessID),entry.szExeFile))
            ok=kernel32.Process32NextW(snapshot,ctypes.byref(entry))
    finally:kernel32.CloseHandle(snapshot)
    return result


def discover_faustus_processes():
    """Return Faustus desktop/server roots and all of their descendants."""
    excluded={os.getpid(),*_ancestor_pids(os.getpid())}
    processes={}
    details={}
    # Reading cwd/cmdline/exe for every process is surprisingly expensive (and
    # can make a stopper look hung), so inspect only plausible entrypoints.
    entrypoint_names={
        "python", "python.exe", "pythonw", "pythonw.exe", "electron",
        "electron.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
        "uvicorn", "uvicorn.exe",
    }
    for pid,ppid,name in _basic_process_snapshot():
        if pid in excluded:continue
        try:process=psutil.Process(pid)
        except psutil.Error:continue
        item={"pid":pid,"ppid":ppid,"name":name or "","exe":"","cwd":"","cmdline":[]}
        if item["name"].lower() in entrypoint_names:item=_process_details(process)
        processes[pid]=process
        details[pid]=item
    selected={pid for pid,item in details.items() if _is_faustus_process(item)}
    # Include opaque Electron helpers and MCP children by ancestry.
    changed=True
    while changed:
        changed=False
        for pid,item in details.items():
            if pid not in selected and item.get("ppid") in selected:
                selected.add(pid);changed=True
    # Launch profiles (including app-shell windows), bridges and external
    # model helpers are deliberately detached. They may retain Faustus as
    # their Windows parent, but the ledger proves they are meant to outlive it.
    keep=set()
    try:
        ledger=json.loads((RUNTIME / "detached.json").read_text(encoding="utf-8"))
        for raw_pid,record in (ledger or {}).items():
            try:
                pid=int(raw_pid)
                process=processes.get(pid) or psutil.Process(pid)
                if abs(process.create_time()-float(record.get("created") or 0))<=.01:keep.add(pid)
            except (psutil.Error,ValueError,TypeError):pass
    except (OSError,ValueError):pass
    changed=True
    while changed:
        changed=False
        for pid,item in details.items():
            if pid not in keep and item.get("ppid") in keep:
                keep.add(pid);changed=True
    selected.difference_update(keep)
    return [processes[pid] for pid in selected]


def _terminate_processes(processes,timeout=5):
    unique={process.pid:process for process in processes if process.pid!=os.getpid()}
    if not unique:return [],[]
    for process in unique.values():
        try:process.terminate()
        except psutil.Error:pass
    _,alive=psutil.wait_procs(list(unique.values()),timeout=timeout)
    for process in alive:
        try:process.kill()
        except psutil.Error:pass
    if alive:_,alive=psutil.wait_procs(alive,timeout=5)
    remaining=[]
    for process in alive:
        try:
            if process.is_running():remaining.append(process.pid)
        except psutil.Error:pass
    return sorted(set(unique)-set(remaining)),sorted(remaining)


def stop(expected_token="",wait_seconds=30):
    with launch_lock():
        record=read_record()
        if expected_token and record.get("token")!=expected_token:
            return {"stopped":False,"reason":"not_owned_by_this_window"}
        process=owned_process(record)
        if not process:
            return {"stopped":False,"reason":"no_managed_server"}
        STOP.write_text(json.dumps({"token":record["token"]}),encoding="utf-8")
        try:process.wait(timeout=wait_seconds)
        except psutil.TimeoutExpired:
            process=owned_process(record)
            if process:terminate_tree(process)
        if read_record().get("token")==record["token"]:RECORD.unlink(missing_ok=True)
        STOP.unlink(missing_ok=True)
        return {"stopped":True,"port":record["port"]}


def stop_all():
    """Stop all Faustus instances from this checkout, including tray apps."""
    managed=stop(wait_seconds=5)
    stopped,remaining=_terminate_processes(discover_faustus_processes())
    record=read_record()
    if not owned_process(record):
        RECORD.unlink(missing_ok=True)
        STOP.unlink(missing_ok=True)
    return {
        "stopped":bool(managed.get("stopped") or stopped),
        "managed":managed,
        "pids":stopped,
        "remaining":remaining,
        "preserved":["ollama","llama-server","unrelated-python"],
    }


def start(port=7000,owner="web"):
    with launch_lock():
        record=read_record()
        if owned_process(record):
            if record.get("port")!=port:raise ValueError("This checkout already has a managed server on another port")
            # Another window already owns the server. Wait for it to become
            # healthy — or to finish dying — instead of failing the splash
            # with "shared server is not ready" during shutdown/startup races.
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                process=owned_process(record)
                if not process:break
                if healthy(port):
                    return {"started":False,"port":port,"owner":record.get("owner"),"healthy":True}
                time.sleep(.4)
            if owned_process(record) and not healthy(port):
                raise ValueError("The shared Faustus server is not ready. Check logs/.")
            record=read_record()
            if owned_process(record):
                if record.get("port")!=port:raise ValueError("This checkout already has a managed server on another port")
                return {"started":False,"port":port,"owner":record.get("owner"),"healthy":healthy(port)}
        if listening(port):
            if not healthy(port):raise ValueError("The port is occupied by another service. Nothing was stopped.")
            return {"started":False,"port":port,"owner":"external","healthy":True}
        token=secrets.token_hex(24)
        logs=ROOT / "logs";logs.mkdir(exist_ok=True)
        log=logs / f"faustus-{owner}-{datetime.now():%Y%m%d-%H%M%S}.log"
        with log.open("ab") as output:
            process=subprocess.Popen([sys.executable,str(ROOT / "server_runtime.py"),"serve","--port",str(port),"--token",token],
                cwd=str(ROOT),stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0,start_new_session=os.name!="nt")
        record={"pid":process.pid,"created":psutil.Process(process.pid).create_time(),"token":token,"port":port,"owner":owner}
        RECORD.write_text(json.dumps(record),encoding="utf-8")
        # Keep the lock until ready: simultaneous launchers cannot race ownership.
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            if process.poll() is not None:break
            if healthy(port):return {"started":True,"port":port,"token":token,"owner":owner,"healthy":True}
            time.sleep(.4)
        current=owned_process(record)
        if current:terminate_tree(current)
        if read_record().get("token")==token:RECORD.unlink(missing_ok=True)
        raise ValueError(f"Faustus did not start. Check {log}")


def serve(port,token):
    os.chdir(ROOT)
    os.environ["APP_PORT"]=str(port)
    import uvicorn
    server=uvicorn.Server(uvicorn.Config("app:app",host="127.0.0.1",port=port,timeout_graceful_shutdown=20))
    finished=threading.Event()
    def watch():
        while not finished.wait(.3):
            try:
                if json.loads(STOP.read_text(encoding="utf-8")).get("token")==token:
                    server.should_exit=True
                    return
            except (OSError,ValueError):pass
    threading.Thread(target=watch,daemon=True).start()
    try:server.run()
    finally:
        finished.set()
        # These are this server's descendants, not all Python/venv processes —
        # minus the ones launched to outlive us (the WhatsApp bridge, launch
        # profiles): src.process_launch keeps their pids in data/runtime/detached.json.
        for child in _children_to_terminate():
            try:child.terminate()
            except psutil.Error:pass
        if read_record().get("token")==token:RECORD.unlink(missing_ok=True)


def _children_to_terminate():
    keep=set()
    try:
        ledger=json.loads((RUNTIME / "detached.json").read_text(encoding="utf-8"))
        for pid,rec in (ledger or {}).items():
            try:
                proc=psutil.Process(int(pid))
                if abs(proc.create_time()-float(rec.get("created") or 0))<=.01:
                    keep.add(proc.pid)
                    keep.update(c.pid for c in proc.children(recursive=True))
            except (psutil.Error,ValueError,TypeError):pass
    except (OSError,ValueError):pass
    try:children=psutil.Process().children(recursive=True)
    except psutil.Error:children=[]
    return [c for c in children if c.pid not in keep]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["start","stop","stop-all","status","serve"])
    parser.add_argument("--port",type=int,default=7000)
    parser.add_argument("--owner",choices=["web","desktop"],default="web")
    parser.add_argument("--token",default="")
    args=parser.parse_args()
    if not 1024<=args.port<=65535:parser.error("Use a port between 1024 and 65535")
    try:
        if args.action=="serve":serve(args.port,args.token);return
        if args.action=="start":result=start(args.port,args.owner)
        elif args.action=="stop":result=stop(args.token)
        elif args.action=="stop-all":result=stop_all()
        else:
            record=read_record();result={"managed":bool(owned_process(record)),"port":record.get("port"),"owner":record.get("owner")}
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({"error":str(exc)}));raise SystemExit(1)


if __name__=="__main__":main()
