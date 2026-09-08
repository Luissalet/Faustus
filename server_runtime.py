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


def stop(expected_token=""):
    with launch_lock():
        record=read_record()
        if expected_token and record.get("token")!=expected_token:
            return {"stopped":False,"reason":"not_owned_by_this_window"}
        process=owned_process(record)
        if not process:
            return {"stopped":False,"reason":"no_managed_server"}
        STOP.write_text(json.dumps({"token":record["token"]}),encoding="utf-8")
        try:process.wait(timeout=30)
        except psutil.TimeoutExpired:
            process=owned_process(record)
            if process:terminate_tree(process)
        if read_record().get("token")==record["token"]:RECORD.unlink(missing_ok=True)
        STOP.unlink(missing_ok=True)
        return {"stopped":True,"port":record["port"]}


def start(port=7000,owner="web"):
    with launch_lock():
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
        # These are this server's descendants, not all Python/venv processes.
        for child in psutil.Process().children(recursive=True):
            try:child.terminate()
            except psutil.Error:pass
        if read_record().get("token")==token:RECORD.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["start","stop","status","serve"])
    parser.add_argument("--port",type=int,default=7000)
    parser.add_argument("--owner",choices=["web","desktop"],default="web")
    parser.add_argument("--token",default="")
    args=parser.parse_args()
    if not 1024<=args.port<=65535:parser.error("Use a port between 1024 and 65535")
    try:
        if args.action=="serve":serve(args.port,args.token);return
        if args.action=="start":result=start(args.port,args.owner)
        elif args.action=="stop":result=stop(args.token)
        else:
            record=read_record();result={"managed":bool(owned_process(record)),"port":record.get("port"),"owner":record.get("owner")}
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({"error":str(exc)}));raise SystemExit(1)


if __name__=="__main__":main()
