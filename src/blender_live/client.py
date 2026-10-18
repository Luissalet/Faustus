"""Native asset jobs executed through Blender Lab's official TCP protocol."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

OPERATIONS = ("scene", "execute", "api", "nodes", "animation", "asset_check", "views", "save", "load", "export",
              "recipe_save", "recipe_list", "recipe_run", "snapshot", "diff")


def connection_directory():
    explicit = os.getenv("FAUSTUS_BLENDER_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(os.getenv("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "Faustus/blender_bridge"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


class BridgeClient:
    def __init__(self, port=None, directory=None):
        self.directory = Path(directory or connection_directory()).resolve()
        connection = self.directory / "connection.json"
        configured = json.loads(connection.read_text(encoding="utf-8")) if connection.exists() else {}
        configured_port = configured.get("port") if configured.get("service") == "blender-lab-mcp" else None
        self.port = int(port or os.getenv("BLENDER_MCP_PORT") or os.getenv("FAUSTUS_BLENDER_PORT") or configured_port or 9876)

    def execute(self, code, timeout=30):
        # Blender Lab protocol: one connection per request, NUL-delimited UTF-8
        # JSON. The official add-on executes bpy on Blender's main thread.
        request = json.dumps({"type": "execute", "code": code, "strict_json": True}).encode() + b"\0"
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as sock:
                sock.settimeout(timeout)
                sock.sendall(request)
                response = bytearray()
                while b"\0" not in response:
                    chunk = sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("Blender cerró la conexión antes de completar la respuesta.")
                    response.extend(chunk)
            result = json.loads(response.partition(b"\0")[0].decode("utf-8"))
        except OSError as exc:
            raise RuntimeError("No se pudo conectar con el servidor MCP oficial de Blender. Abre Blender 5.1+ y comprueba el complemento MCP.") from exc
        if result.get("status") != "ok":
            raise RuntimeError(result.get("message", "Error de Blender"))
        return result

    def probe(self):
        try:
            value = self.execute("import bpy\nresult = {'version': bpy.app.version_string, 'version_tuple': list(bpy.app.version), 'background': bpy.app.background, 'file': bpy.data.filepath}", timeout=5)["result"]
            return {"connected": True, "service": "blender-lab-mcp", "port": self.port,
                    "supported": tuple(value["version_tuple"]) >= (5, 1, 0), **value, "operations": list(OPERATIONS)}
        except (RuntimeError, ValueError) as exc:
            return {"connected": False, "message": str(exc), "port": self.port, "operations": list(OPERATIONS)}

    def submit(self, operation, arguments=None):
        if operation not in OPERATIONS:
            raise ValueError("Unknown live scene operation: " + str(operation))
        job = {"id": uuid.uuid4().hex, "action": operation, "arguments": arguments or {},
               "port": self.port, "state": "queued", "created_at": time.time(), "updated_at": time.time(), "result": {}}
        path = self.directory / "jobs" / (job["id"] + ".json")
        write_json(path, job)
        env = dict(os.environ, PYTHONUTF8="1", FAUSTUS_BLENDER_DATA_DIR=str(self.directory))
        root = Path(__file__).resolve().parents[2]
        try:
            with path.with_suffix(".log").open("wb") as log:
                process = subprocess.Popen([sys.executable, "-m", "src.blender_live.worker", str(path)],
                    cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt")
            write_json(path.with_suffix(".pid"), {"pid": process.pid, "job": str(path)})
        except OSError as exc:
            job.update(state="failed", error=str(exc), updated_at=time.time())
            write_json(path, job)
            raise
        return job

    def job(self, job_id):
        path = self.directory / "jobs" / (self.job_id(job_id) + ".json")
        job = json.loads(path.read_text(encoding="utf-8"))
        if job["state"] in ("queued", "running") and time.time() - job["created_at"] > 10:
            pid_file = path.with_suffix(".pid")
            if pid_file.exists():
                import psutil
                pid = json.loads(pid_file.read_text(encoding="utf-8"))["pid"]
                try:
                    alive = str(path) in psutil.Process(pid).cmdline()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    alive = False
                if not alive:
                    current = json.loads(path.read_text(encoding="utf-8"))
                    if current["state"] in ("queued", "running"):
                        job = {**current, "state": "interrupted", "updated_at": time.time(),
                               "error": "La conexión terminó antes de confirmar el resultado. Comprueba la escena antes de repetir."}
                        write_json(path, job)
        return job

    def jobs(self):
        return {"jobs": sorted((self.job(path.stem) for path in (self.directory / "jobs").glob("*.json")), key=lambda job: job["created_at"])}

    def cancel(self, job_id):
        job = self.job(job_id)
        marker = self.directory / "jobs" / (job_id + ".cancel")
        if job["state"] in ("queued", "running"):
            marker.touch()
        return {**job, "cancel_requested": marker.exists()}

    @staticmethod
    def job_id(value):
        if len(str(value)) != 32 or any(char not in "0123456789abcdef" for char in str(value)):
            raise ValueError("Expected a live job ID")
        return str(value)
