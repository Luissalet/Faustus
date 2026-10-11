"""Start the shared local backends without Faustus.

Every Hoard app can run on its own. The models live in shared local servers
(ComfyUI for images, video and music; Ollama; a llama.cpp server...). When
Faustus is not there to have started them, an app starts them itself with
this module: it finds the install, starts it detached on loopback, waits
until it answers, and can stop it again.

Machine-wide and shared by the whole family:

* ``~/.hoard/backends.json`` (``HOARD_HOME`` moves ``~/.hoard``): where
  things are installed. Any app's settings can write it::

    {
      "comfyui": {"dir": "D:/ComfyUI", "python": null, "gpu": "auto", "args": []},
      "ollama": {"exe": null},
      "commands": [
        {"id": "llamacpp", "label": "llama.cpp",
         "argv": ["powershell", "-NoProfile", "-File", "D:/LocalAI/Start-LlamaServer.ps1"],
         "stop_argv": ["powershell", "-NoProfile", "-File", "D:/LocalAI/Stop-LlamaServer.ps1"],
         "cwd": "D:/LocalAI", "health": "http://127.0.0.1:8081/health",
         "capabilities": ["llm", "vision"]}
      ]
    }

  Nothing has to be written for a standard install: ComfyUI is looked for
  in the usual folders (and ``COMFYUI_DIR``), Ollama on ``PATH`` and in its
  default install folder.

* ``~/.hoard/backends/state.json``: the processes the family started (pid
  and creation time, so a recycled pid is never mistaken for ours). Prospero
  can stop a ComfyUI the Hub started, and a restarted app still knows what
  it owns. Only those processes are ever stopped: a server somebody else
  started is reported as running and left alone, unless its command in
  backends.json declares its own ``stop_argv`` (a stop script is the owner
  saying how it may be stopped).

ComfyUI instances are addressed by port (``comfyui@8188``). The default
port uses ComfyUI's own output/user folders; any other port gets its own
output, temp, user and database folders under ``~/.hoard/backends/`` so two
instances on two GPUs never race on file names or the asset database.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

__all__ = ["Launcher", "Service", "hoard_home", "comfy_port_from_url", "list_gpus", "memory", "host_stats",
           "process_created", "process_alive", "spawn_orphan"]

DEFAULT_COMFY_PORT = 8188
OLLAMA_URL = "http://127.0.0.1:11434"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
IS_WIN = sys.platform.startswith("win")
COMFY_CAPABILITIES = ["image", "video", "music"]
OLLAMA_CAPABILITIES = ["llm", "vision", "embeddings"]
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,40}$")


def hoard_home() -> Path:
    env = os.environ.get("HOARD_HOME")
    return Path(env).expanduser() if env else Path.home() / ".hoard"


def comfy_port_from_url(url: Optional[str]) -> Optional[int]:
    """The port of a loopback ComfyUI URL (8188 when none is given); None
    for a remote one, which no local launcher can start."""
    if not url:
        return DEFAULT_COMFY_PORT
    parts = urlsplit(url.strip())
    if (parts.hostname or "") not in LOOPBACK:
        return None
    return parts.port or (443 if parts.scheme == "https" else 80)


@dataclass
class Service:
    id: str                          # "comfyui@8188", "ollama", "cmd:llamacpp"
    kind: str                        # comfyui | ollama | command
    label: str
    capabilities: list[str]
    url: str
    health: str
    argv: Optional[list[str]] = None
    cwd: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)
    problem: Optional[str] = None    # why it cannot be started on this machine
    install: Optional[str] = None    # where it was found
    port: Optional[int] = None
    stop_argv: Optional[list[str]] = None  # a command's own stop script (stops it even when started elsewhere)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "capabilities": list(self.capabilities),
                "url": self.url, "install": self.install, "problem": self.problem,
                "command": " ".join(self.argv) if self.argv else None}


# ---------------------------------------------------------------- probes --

def _port_open(url: str, timeout: float = 0.35) -> bool:
    """A quick TCP check first: on Windows an HTTP call to a closed loopback
    port takes about 1.5 s to fail, a status page listing five services
    would take seconds."""
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_status(url: str, timeout: float = 2.0) -> Optional[int]:
    if not _port_open(url):
        return None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:  # noqa: S310 - loopback
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def list_gpus() -> list[dict[str, Any]]:
    """NVIDIA GPUs by PCI bus order (the order ``CUDA_DEVICE_ORDER=PCI_BUS_ID``
    gives ComfyUI), with free and total memory. Empty without nvidia-smi."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    kwargs: dict[str, Any] = {}
    if IS_WIN:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run([exe, "--query-gpu=index,name,memory.free,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL, **kwargs).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            try:
                gpus.append({"index": int(parts[0]), "name": parts[1], "free_mb": int(float(parts[2])),
                             "total_mb": int(float(parts[3]))})
            except ValueError:
                continue
    return gpus


# ------------------------------------------------------------ processes --

def process_created(pid: int) -> Optional[float]:
    """Epoch seconds the process was created, None when it is not alive."""
    if pid <= 0:
        return None
    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return None
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if not k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return None
            ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime
            return ticks / 10_000_000 - 11_644_473_600
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return None
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
        btime = next(int(l.split()[1]) for l in Path("/proc/stat").read_text().splitlines() if l.startswith("btime"))
        return btime + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return 0.0  # alive, creation time unknown (macOS): pid check only


def process_alive(pid: int, created: Optional[float]) -> bool:
    now_created = process_created(pid)
    if now_created is None:
        return False
    if created and now_created and abs(now_created - created) > 2.0:
        return False  # the pid was recycled by another process
    return True


# Preserve the old private spellings for callers that reached into the launcher module.
_creation_time = process_created
_alive = process_alive


# The server must not be a child of the app that starts it: stopping an app
# (the hub, a launcher, Task Manager's "end process tree") terminates its
# descendants, and a ComfyUI that dies with Prospero is not "shared". A
# short-lived Python in between starts it and exits at once, so the server
# is orphaned from the start and no process tree reaches it.
_SPAWNER = r"""
import json, os, subprocess, sys
spec = json.loads(sys.stdin.read())
log = open(spec["log"], "ab")
kw = dict(cwd=spec["cwd"] or None, env=spec["env"], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
          close_fds=True)
if os.name == "nt":
    flags = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    try:
        p = subprocess.Popen(spec["argv"], creationflags=flags | 0x01000000, **kw)  # + CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        p = subprocess.Popen(spec["argv"], creationflags=flags, **kw)
else:
    p = subprocess.Popen(spec["argv"], start_new_session=True, **kw)
sys.stdout.write(str(p.pid))
sys.stdout.flush()
"""


def spawn_orphan(argv: list[str], cwd: Optional[str], env: dict[str, str], log_path: Path) -> int:
    """Start ``argv`` detached, as an orphan; returns its pid."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0
    spec = json.dumps({"argv": argv, "cwd": cwd, "env": env, "log": str(log_path)})
    out = subprocess.run([sys.executable, "-c", _SPAWNER], input=spec, capture_output=True, text=True, timeout=60,
                         env=env, creationflags=flags)
    if out.returncode != 0 or not out.stdout.strip().isdigit():
        raise OSError((out.stderr or out.stdout or "the spawner failed").strip()[-600:])
    return int(out.stdout.strip())


_spawn_orphan = spawn_orphan


def _kill_tree(pid: int, grace_s: float = 6.0) -> None:
    if IS_WIN:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, creationflags=flags, timeout=20)
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline and process_created(pid) is not None:
            time.sleep(0.3)
        if process_created(pid) is not None:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=flags, timeout=20)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and process_created(pid) is not None:
        time.sleep(0.2)
    if process_created(pid) is not None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# --------------------------------------------------------------- launcher --

class Launcher:
    """Finds, starts, watches and stops the shared local backends."""

    def __init__(self, home: Path | str | None = None, *, app: str = "hoard"):
        self.home = Path(home) if home else hoard_home()
        self.app = app
        self._lock = threading.RLock()

    # -- files ---------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.home / "backends.json"

    @property
    def state_path(self) -> Path:
        return self.home / "backends" / "state.json"

    @property
    def logs_dir(self) -> Path:
        return self.home / "backends" / "logs"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError, UnicodeDecodeError):
            return {}

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def config(self) -> dict[str, Any]:
        return self._read_json(self.config_path)

    def set_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge ``patch`` into ``backends.json``: ``comfyui``/``ollama`` are
        merged key by key (None or "" removes a key), ``commands`` replaces
        the list (each one needs ``id``, ``argv`` or ``cmd``, ``health``)."""
        with self._lock:
            cfg = self.config()
            for section in ("comfyui", "ollama"):
                if section in patch and patch[section] is not None:
                    if not isinstance(patch[section], dict):
                        raise ValueError(f"{section} must be an object")
                    cur = dict(cfg.get(section) or {})
                    for key, value in patch[section].items():
                        if value is None or value == "":
                            cur.pop(key, None)
                        else:
                            cur[key] = value
                    if "gpu" in cur and cur["gpu"] != "auto":
                        try:
                            cur["gpu"] = int(cur["gpu"])
                        except (TypeError, ValueError):
                            raise ValueError("comfyui.gpu must be \"auto\" or a GPU index") from None
                    if cur:
                        cfg[section] = cur
                    else:
                        cfg.pop(section, None)
            if "commands" in patch and patch["commands"] is not None:
                cmds = []
                for c in patch["commands"]:
                    if not isinstance(c, dict) or not _ID_RE.match(str(c.get("id") or "")):
                        raise ValueError("each command needs an id of lowercase letters, digits, '-', '_' or '.'")
                    if not (c.get("argv") or c.get("cmd")) or not c.get("health"):
                        raise ValueError(f"command {c['id']!r} needs argv (or cmd) and a health URL")
                    if (urlsplit(str(c["health"])).hostname or "") not in LOOPBACK:
                        raise ValueError(f"command {c['id']!r}: the health URL must be on loopback")
                    cmds.append(c)
                cfg["commands"] = cmds
            self._write_json(self.config_path, cfg)
            return cfg

    def _state(self) -> dict[str, dict[str, Any]]:
        raw = self._read_json(self.state_path)
        return {k: v for k, v in raw.items() if isinstance(v, dict)}

    def _save_state(self, state: dict[str, dict[str, Any]]) -> None:
        self._write_json(self.state_path, state)

    # -- discovery -------------------------------------------------------
    def comfy_candidates(self) -> list[Path]:
        cfg_dir = (self.config().get("comfyui") or {}).get("dir")
        out: list[Path] = []
        for d in (cfg_dir, os.environ.get("COMFYUI_DIR")):
            if d:
                out.append(Path(d).expanduser())
        home = Path.home()
        out += [home / "ComfyUI", home / "Documents" / "ComfyUI", home / "Desktop" / "ComfyUI",
                home / "AI" / "ComfyUI"]
        if IS_WIN:
            for drive in "CDEFGH":
                root = Path(f"{drive}:/")
                out += [root / "ComfyUI", root / "LocalAI" / "ComfyUI", root / "AI" / "ComfyUI",
                        root / "ComfyUI_windows_portable" / "ComfyUI"]
        else:
            out += [Path("/opt/ComfyUI"), Path("/srv/ComfyUI")]
        seen, uniq = set(), []
        for p in out:
            key = str(p).lower() if IS_WIN else str(p)
            if key not in seen:
                seen.add(key)
                uniq.append(p)
        return uniq

    def comfy_install(self) -> tuple[Optional[Path], Optional[Path], Optional[str]]:
        """(ComfyUI folder, its Python, problem)."""
        cfg = self.config().get("comfyui") or {}
        folder = None
        for cand in self.comfy_candidates():
            try:
                if (cand / "main.py").is_file() and (cand / "comfy").is_dir():
                    folder = cand
                    break
            except OSError:
                continue
        if folder is None:
            if cfg.get("dir"):
                return None, None, f"no ComfyUI install at {cfg['dir']} (main.py not found)"
            return None, None, "ComfyUI was not found: set its folder (backends.json comfyui.dir or COMFYUI_DIR)"
        pythons: list[Path] = []
        if cfg.get("python"):
            pythons.append(Path(cfg["python"]).expanduser())
        for venv in ("venv", ".venv", "env"):
            pythons += [folder / venv / "Scripts" / "python.exe", folder / venv / "bin" / "python"]
        pythons.append(folder.parent / "python_embeded" / "python.exe")  # the portable build (sic)
        for py in pythons:
            if py.is_file():
                return folder, py, None
        return folder, None, (f"ComfyUI found at {folder} but no Python environment next to it "
                              "(venv, .venv, python_embeded): set comfyui.python")

    def ollama_exe(self) -> Optional[str]:
        cfg = self.config().get("ollama") or {}
        cands = [cfg.get("exe"), shutil.which("ollama")]
        if IS_WIN:
            local = os.environ.get("LOCALAPPDATA")
            if local:
                cands.append(str(Path(local) / "Programs" / "Ollama" / "ollama.exe"))
        else:
            cands += ["/usr/local/bin/ollama", "/usr/bin/ollama"]
        for c in cands:
            if c and Path(c).is_file():
                return str(c)
        return None

    @staticmethod
    def _comfy_flags(folder: Path) -> str:
        try:
            return (folder / "comfy" / "cli_args.py").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def comfy_service(self, port: int = DEFAULT_COMFY_PORT, gpu: Any = None) -> Service:
        url = f"http://127.0.0.1:{port}"
        svc = Service(id=f"comfyui@{port}", kind="comfyui", label=f"ComfyUI :{port}",
                      capabilities=list(COMFY_CAPABILITIES), url=url, health=url + "/system_stats", port=port)
        folder, python, problem = self.comfy_install()
        svc.problem = problem
        if folder is None or python is None:
            svc.install = str(folder) if folder else None
            return svc
        cfg = self.config().get("comfyui") or {}
        flags = self._comfy_flags(folder)
        argv = [str(python), "main.py", "--listen", "127.0.0.1", "--port", str(port)]
        if "--disable-auto-launch" in flags:
            argv.append("--disable-auto-launch")
        if "--preview-method" in flags:
            argv += ["--preview-method", "none"]
        if gpu is not None and "--cuda-device" in flags:
            argv += ["--cuda-device", str(gpu)]
        if port != DEFAULT_COMFY_PORT:
            own = self.home / "backends" / f"comfyui-{port}"
            for flag, sub in (("--output-directory", "output"), ("--temp-directory", "temp"), ("--user-directory", "user")):
                if flag in flags:
                    argv += [flag, str(own / sub)]
            if "--database-url" in flags:
                argv += ["--database-url", "sqlite:///" + (own / "comfyui.db").as_posix()]
        extra = cfg.get("args") or []
        if isinstance(extra, str):
            extra = shlex.split(extra, posix=not IS_WIN)
        # With "fast disk" a model bigger than the free VRAM is read back from
        # the disk on every step (a 15 GB video model then takes minutes per
        # step); offloading over RAM is several times faster. Opt back in with
        # "--fast-disk" in comfyui.args.
        if ("--disable-fast-disk" in flags and "--fast-disk" not in extra
                and "--disable-fast-disk" not in extra):
            argv.append("--disable-fast-disk")
        argv += [str(a) for a in extra]
        svc.argv, svc.cwd, svc.install = argv, str(folder), str(folder)
        svc.env = {"CUDA_DEVICE_ORDER": "PCI_BUS_ID"}
        return svc

    def ollama_service(self) -> Service:
        svc = Service(id="ollama", kind="ollama", label="Ollama", capabilities=list(OLLAMA_CAPABILITIES),
                      url=OLLAMA_URL, health=OLLAMA_URL + "/api/version")
        exe = self.ollama_exe()
        if exe is None:
            svc.problem = "Ollama is not installed (or set ollama.exe in backends.json)"
        else:
            svc.argv, svc.install = [exe, "serve"], exe
        return svc

    def command_services(self) -> list[Service]:
        out = []
        for c in self.config().get("commands") or []:
            if not isinstance(c, dict) or not _ID_RE.match(str(c.get("id") or "")) or not c.get("health"):
                continue
            argv = c.get("argv")
            if not argv and c.get("cmd"):
                argv = shlex.split(str(c["cmd"]), posix=not IS_WIN)
            health = str(c["health"])
            parts = urlsplit(health)
            stop_argv = c.get("stop_argv")
            if not stop_argv and c.get("stop_cmd"):
                stop_argv = shlex.split(str(c["stop_cmd"]), posix=not IS_WIN)
            svc = Service(id=f"cmd:{c['id']}", kind="command", label=str(c.get("label") or c["id"]),
                          capabilities=[str(x) for x in (c.get("capabilities") or [])],
                          url=f"{parts.scheme}://{parts.netloc}", health=health,
                          argv=[str(a) for a in argv] if argv else None, cwd=c.get("cwd"),
                          env={str(k): str(v) for k, v in (c.get("env") or {}).items()}, install=c.get("cwd"),
                          stop_argv=[str(a) for a in stop_argv] if stop_argv else None,
                          port=parts.port)
            if not svc.argv:
                svc.problem = "no argv/cmd configured"
            elif svc.cwd and not Path(svc.cwd).is_dir():
                svc.problem = f"working folder not found: {svc.cwd}"
            out.append(svc)
        return out

    def services(self, comfy_ports: Optional[list[int]] = None) -> list[Service]:
        ports = [DEFAULT_COMFY_PORT] if comfy_ports is None else list(dict.fromkeys(comfy_ports))
        for sid in self._state():  # anything the family started stays visible
            if sid.startswith("comfyui@"):
                try:
                    p = int(sid.split("@", 1)[1])
                except ValueError:
                    continue
                if p not in ports:
                    ports.append(p)
        return [*(self.comfy_service(p) for p in ports), self.ollama_service(), *self.command_services()]

    def get(self, service_id: str, gpu: Any = None) -> Service:
        if service_id.startswith("comfyui@"):
            try:
                port = int(service_id.split("@", 1)[1])
            except ValueError:
                raise KeyError(service_id) from None
            if not 1 <= port <= 65535:
                raise KeyError(service_id)
            return self.comfy_service(port, gpu=gpu)
        if service_id == "comfyui":
            return self.comfy_service(DEFAULT_COMFY_PORT, gpu=gpu)
        if service_id == "ollama":
            return self.ollama_service()
        for svc in self.command_services():
            if svc.id == service_id or svc.id == f"cmd:{service_id}":
                return svc
        raise KeyError(service_id)

    # -- runtime ---------------------------------------------------------
    def _owned(self, service_id: str) -> Optional[dict[str, Any]]:
        st = self._state().get(service_id)
        if st and st.get("pid") and process_alive(int(st["pid"]), st.get("created")):
            return st
        return None

    def status(self, svc: Service) -> dict[str, Any]:
        code = _http_status(svc.health)
        own = self._owned(svc.id)
        # a server busy with a render (ComfyUI loading a 20 GB model, a long
        # sampler step) can leave its health page unanswered for seconds:
        # the port still listening means it is up, not down
        busy = code is None and _port_open(svc.health)
        if (code is not None and code < 500) or busy:
            state = "running"
        elif own is not None:
            state = "starting"
        elif svc.problem:
            state = "unavailable"
        else:
            state = "down"
        d = svc.public()
        d.update({"state": state, "busy": busy, "pid": own.get("pid") if own else None,
                  "started_by": own.get("by") if own else None,
                  "stoppable": own is not None or (state == "running" and svc.stop_argv is not None),
                  "gpu": own.get("gpu") if own else None,
                  "log": str(self.log_path(svc.id)) if (own or self.log_path(svc.id).is_file()) else None,
                  "startable": state in ("down",) and svc.argv is not None})
        return d

    def statuses(self, comfy_ports: Optional[list[int]] = None) -> list[dict[str, Any]]:
        services = self.services(comfy_ports)
        results: list[Optional[dict[str, Any]]] = [None] * len(services)

        def one(i: int, s: Service) -> None:
            results[i] = self.status(s)

        threads = [threading.Thread(target=one, args=(i, s), daemon=True) for i, s in enumerate(services)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        return [r for r in results if r is not None]

    def log_path(self, service_id: str) -> Path:
        return self.logs_dir / (re.sub(r"[^A-Za-z0-9_.-]+", "_", service_id) + ".log")

    def log_tail(self, service_id: str, chars: int = 1500) -> str:
        try:
            data = self.log_path(service_id).read_bytes()[-chars * 2:]
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace")[-chars:]

    def pick_gpu(self, exclude: Optional[set[int]] = None) -> Optional[int]:
        """The GPU with the most free memory, skipping ones another ComfyUI
        the family started already uses (when there is any other left)."""
        gpus = list_gpus()
        if not gpus:
            return None
        busy = set(exclude or ())
        for sid, st in self._state().items():
            if sid.startswith("comfyui@") and st.get("gpu") is not None and process_alive(int(st.get("pid") or 0), st.get("created")):
                busy.add(int(st["gpu"]))
        pool = [g for g in gpus if g["index"] not in busy] or gpus
        return max(pool, key=lambda g: g["free_mb"])["index"]

    def start(self, service_id: str, *, gpu: Any = None, wait_s: float = 0.0) -> dict[str, Any]:
        """Start a service detached. ``gpu``: an index, "auto" (the most free
        memory) or None (ComfyUI's setting in backends.json, else "auto").
        ``wait_s`` > 0 waits that long for it to answer."""
        with self._lock:
            chosen_gpu: Optional[int] = None
            if service_id.startswith("comfyui"):
                want = gpu if gpu is not None else (self.config().get("comfyui") or {}).get("gpu", "auto")
                if want in (None, "auto", ""):
                    chosen_gpu = self.pick_gpu()
                else:
                    try:
                        chosen_gpu = int(want)
                    except (TypeError, ValueError):
                        return {"ok": False, "service": service_id, "error": f"gpu must be \"auto\" or an index, got {want!r}"}
            try:
                svc = self.get(service_id, gpu=chosen_gpu)
            except KeyError:
                return {"ok": False, "service": service_id, "error": f"unknown service {service_id!r}"}
            current = self.status(svc)
            if current["state"] in ("running", "starting"):
                out = {"ok": True, "service": svc.id, "already": True, "state": current["state"], "url": svc.url}
                if wait_s > 0 and current["state"] == "starting":
                    out.update(self._wait(svc, wait_s))
                return out
            if svc.problem or not svc.argv:
                return {"ok": False, "service": svc.id, "error": svc.problem or "nothing to start"}
            log_path = self.log_path(svc.id)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if svc.kind == "comfyui" and svc.port != DEFAULT_COMFY_PORT:
                for sub in ("output", "temp", "user"):
                    (self.home / "backends" / f"comfyui-{svc.port}" / sub).mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            env.update(svc.env)
            env.setdefault("PYTHONUNBUFFERED", "1")
            try:
                with open(log_path, "ab") as log:
                    log.write(f"\n--- started by {self.app} {time.strftime('%Y-%m-%d %H:%M:%S')}: "
                              f"{' '.join(svc.argv)}\n".encode("utf-8"))
                pid = spawn_orphan(svc.argv, svc.cwd, env, log_path)
            except (OSError, subprocess.SubprocessError) as exc:
                return {"ok": False, "service": svc.id, "error": f"could not start: {exc}", "log": str(log_path)}
            state = self._state()
            state[svc.id] = {"pid": pid, "created": process_created(pid), "started_at": time.time(),
                             "by": self.app, "gpu": chosen_gpu if svc.kind == "comfyui" else None,
                             "command": " ".join(svc.argv)}
            self._save_state(state)
            out = {"ok": True, "service": svc.id, "pid": pid, "url": svc.url, "log": str(log_path),
                   "gpu": chosen_gpu if svc.kind == "comfyui" else None}
        if wait_s > 0:
            out.update(self._wait(svc, wait_s))
        return out

    def _wait(self, svc: Service, wait_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            code = _http_status(svc.health)
            if code is not None and code < 500:
                return {"ready": True, "state": "running"}
            if svc.id in self._state() and self._owned(svc.id) is None:
                log_path = self.log_path(svc.id)
                return {"ok": False, "ready": False, "state": "exited", "log": str(log_path),
                        "error": f"{svc.label} exited while starting; see {log_path} (exit code unavailable); "
                                 f"last log lines:\n{self.log_tail(svc.id, 800)}"}
            time.sleep(1.0)
        return {"ready": False, "state": "starting", "detail": f"not answering after {int(wait_s)} s; still starting"}

    def wait_ready(self, service_id: str, timeout_s: float) -> bool:
        try:
            svc = self.get(service_id)
        except KeyError:
            return False
        return bool(self._wait(svc, timeout_s).get("ready"))

    def _run_stop_script(self, svc: Service) -> dict[str, Any]:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0
        try:
            out = subprocess.run(svc.stop_argv or [], cwd=svc.cwd or None, capture_output=True, text=True, timeout=120,
                                 stdin=subprocess.DEVNULL, creationflags=flags)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "service": svc.id, "error": f"its stop command failed: {exc}"}
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and _port_open(svc.health):
            time.sleep(0.5)
        gone = not _port_open(svc.health)
        tail = ((out.stdout or "") + (out.stderr or "")).strip()[-400:]
        ok = gone and out.returncode == 0
        return {"ok": ok, "service": svc.id, "via": "stop command", "output": tail,
                **({} if ok else {"error": "its stop command failed" if out.returncode else "it still answers after its stop command"})}

    def stop(self, service_id: str) -> dict[str, Any]:
        """Stop a service the family started. One started elsewhere is
        refused: stop it where it runs."""
        with self._lock:
            if service_id == "comfyui":
                service_id = f"comfyui@{DEFAULT_COMFY_PORT}"
            if service_id != "ollama" and not service_id.startswith(("comfyui@", "cmd:")):
                service_id = f"cmd:{service_id}"
            own = self._owned(service_id)
            state = self._state()
            try:
                svc = self.get(service_id)
            except KeyError:
                svc = None
            # A configured stop command owns the supervisor as well as the listener.
            # Killing only the listener (or only our recorded launcher) can respawn it.
            if svc is not None and svc.stop_argv:
                result = self._run_stop_script(svc)
                if result.get("ok") and own is not None and self._owned(service_id) is not None:
                    _kill_tree(int(own["pid"]))
                    if self._owned(service_id) is not None:
                        return {"ok": False, "service": service_id, "error": "the service supervisor is still alive"}
                if result.get("ok"):
                    state.pop(service_id, None)
                    self._save_state(state)
                return result
            if own is None:
                state.pop(service_id, None)
                self._save_state(state)
                try:
                    svc = self.get(service_id)
                    running = _port_open(svc.health)
                except KeyError:
                    running = False
                if running and svc.stop_argv:
                    return self._run_stop_script(svc)
                if running:
                    return {"ok": False, "service": service_id,
                            "error": "it is running but was not started from the Hoard family; stop it where it runs"}
                return {"ok": True, "service": service_id, "detail": "not running"}
            _kill_tree(int(own["pid"]))
            gone = process_created(int(own["pid"])) is None
            if gone:
                state.pop(service_id, None)
                self._save_state(state)
            return {"ok": gone, "service": service_id, "pid": own["pid"],
                    **({} if gone else {"error": "the process is still alive after the stop request"})}


# ------------------------------------------------------------- memory --

def _listeners() -> dict[int, int]:
    """Loopback/any listening TCP port -> owning pid (best effort)."""
    out: dict[int, int] = {}
    try:
        if IS_WIN:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            text = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=10,
                                  creationflags=flags).stdout
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                    port = parts[1].rsplit(":", 1)[-1]
                    if port.isdigit() and parts[4].isdigit():
                        out.setdefault(int(port), int(parts[4]))
        elif shutil.which("ss"):
            text = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True, timeout=10).stdout
            for line in text.splitlines():
                m = re.search(r":(\d+)\s.*pid=(\d+)", line)
                if m:
                    out.setdefault(int(m.group(1)), int(m.group(2)))
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def _gpu_processes() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(gpus with bus ids, compute processes with the gpu index they use)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return [], []
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0

    def q(args: list[str]) -> list[list[str]]:
        try:
            text = subprocess.run([exe, *args, "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                  timeout=10, stdin=subprocess.DEVNULL, creationflags=flags).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [[p.strip() for p in line.split(",")] for line in text.splitlines() if line.strip()]

    gpus, by_bus = [], {}
    def num(v: str) -> Optional[float]:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None  # "[N/A]" on cards that do not report it

    for row in q(["--query-gpu=index,name,pci.bus_id,memory.used,memory.free,memory.total,utilization.gpu,"
                  "temperature.gpu,power.draw,power.limit"]):
        if len(row) < 6:
            continue
        try:
            g = {"index": int(row[0]), "name": row[1], "used_mb": int(float(row[3])), "free_mb": int(float(row[4])),
                 "total_mb": int(float(row[5]))}
        except ValueError:
            continue
        extra = row[6:10] + [""] * (4 - len(row[6:10]))
        g.update({"util": num(extra[0]), "temp": num(extra[1]), "power": num(extra[2]), "power_limit": num(extra[3])})
        gpus.append(g)
        by_bus[row[2].lower()] = g["index"]
    procs = []
    for row in q(["--query-compute-apps=pid,process_name,gpu_bus_id,used_memory"]):
        if len(row) < 4 or not row[0].isdigit():
            continue
        try:
            used = int(float(row[3]))
        except ValueError:
            used = None  # Windows (WDDM) does not report per-process memory
        procs.append({"pid": int(row[0]), "name": os.path.basename(row[1].replace("\\", "/")) if row[1] else "",
                      "gpu": by_bus.get(row[2].lower()), "used_mb": used})
    return gpus, procs


_CPU_LAST: dict[str, tuple[float, float]] = {}


def _cpu_times() -> Optional[tuple[float, float]]:
    """(idle, total) CPU time since boot, any unit."""
    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        val = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime  # noqa: E731
        return float(val(idle)), float(val(kernel) + val(user))  # kernel time includes idle
    try:
        parts = [float(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        return parts[3] + (parts[4] if len(parts) > 4 else 0.0), sum(parts)
    except (OSError, ValueError, IndexError):
        return None


def host_stats() -> dict[str, Any]:
    """System RAM and CPU use (stdlib only). CPU % is measured since the
    previous call (the first call samples 200 ms)."""
    out: dict[str, Any] = {"cpu_count": os.cpu_count()}
    try:
        if IS_WIN:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                total, avail = st.ullTotalPhys, st.ullAvailPhys
                out["ram"] = {"total_mb": total // 1048576, "used_mb": (total - avail) // 1048576,
                              "free_mb": avail // 1048576}
                out["commit"] = {"total_mb": st.ullTotalPageFile // 1048576,
                                 "used_mb": (st.ullTotalPageFile - st.ullAvailPageFile) // 1048576}
        else:
            info = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0]) // 1024
            total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
            out["ram"] = {"total_mb": total, "used_mb": total - avail, "free_mb": avail}
            if info.get("SwapTotal"):
                out["swap"] = {"total_mb": info["SwapTotal"], "used_mb": info["SwapTotal"] - info.get("SwapFree", 0)}
    except (OSError, ValueError, AttributeError):
        pass
    now = _cpu_times()
    prev = _CPU_LAST.get("t")
    if now and not prev:
        time.sleep(0.2)
        prev, now = now, _cpu_times()
    if now and prev and now[1] > prev[1]:
        busy = 1.0 - (now[0] - prev[0]) / (now[1] - prev[1])
        _CPU_LAST["pct"] = (round(max(0.0, min(1.0, busy)) * 100, 1), 0.0)
        _CPU_LAST["t"] = now
    elif now and not prev:
        _CPU_LAST["t"] = now
    if "pct" in _CPU_LAST:  # two calls within the clock's resolution: the last reading
        out["cpu_pct"] = _CPU_LAST["pct"][0]
    return out


def _json(url: str, timeout: float = 3.0) -> Any:
    if not _port_open(url):
        return None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - loopback
            return json.loads(resp.read() or b"null")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _what_is_loaded(item: dict[str, Any]) -> dict[str, Any]:
    """Models a running server holds, as far as its API tells."""
    url = item["url"]
    if item["kind"] == "comfyui":
        stats = _json(url + "/system_stats") or {}
        devs = stats.get("devices") or []
        held = sum(int(d.get("torch_vram_total") or 0) for d in devs) // (1024 * 1024)
        return {"held_mb": held, "models": [], "note": "ComfyUI keeps the last models it used until it is asked to free them"}
    if item["kind"] == "ollama":
        ps = _json(url + "/api/ps") or {}
        models = [{"name": m.get("name"), "vram_mb": int(m.get("size_vram") or 0) // (1024 * 1024)}
                  for m in ps.get("models") or []]
        return {"held_mb": sum(m["vram_mb"] for m in models), "models": models}
    models = _json(url + "/v1/models") or {}
    names = [{"name": m.get("id")} for m in (models.get("data") or []) if isinstance(m, dict) and m.get("id")]
    return {"held_mb": None, "models": names}


# names of processes that serve models (a game launcher also listens on a
# port and draws on the GPU: it is counted, not listed)
_MODEL_SERVER = re.compile(r"llama|ollama|python|kobold|lm ?studio|lms|vllm|comfy|tabby|exllama|whisper|piper|"
                           r"text-generation|sglang|mlc|jan|localai", re.I)


def memory(launcher: "Launcher", comfy_ports: Optional[list[int]] = None) -> dict[str, Any]:
    """What is loaded on each GPU and which family service holds it:
    ``gpus[{index, name, used_mb, free_mb, total_mb, services[], others}]``
    and ``services[{id, label, state, gpus[], models[], held_mb, stoppable}]``."""
    gpus, procs = _gpu_processes()
    listeners = _listeners()
    items = [i for i in launcher.statuses(comfy_ports) if i["state"] in ("running", "starting")]
    pid_to_service: dict[int, str] = {}
    for item in items:
        port = urlsplit(item["url"]).port
        pid = listeners.get(port) if port else None
        if pid:
            pid_to_service[pid] = item["id"]
        if item.get("pid"):
            pid_to_service.setdefault(int(item["pid"]), item["id"])
    known_pids = set(pid_to_service)
    services = []
    for item in items:
        pids = {p for p, sid in pid_to_service.items() if sid == item["id"]}
        on = sorted({p["gpu"] for p in procs if p["pid"] in pids and p["gpu"] is not None})
        services.append({"id": item["id"], "label": item["label"], "kind": item["kind"], "state": item["state"],
                         "url": item["url"], "gpus": on, "stoppable": item["stoppable"], "started_by": item["started_by"],
                         **_what_is_loaded(item)})
    # GPU processes that listen on a port but are no configured service
    # (a llama-server started by hand...): still say what they are
    port_of = {pid: port for port, pid in listeners.items()}
    unknown: dict[int, dict[str, Any]] = {}
    for p in procs:
        if p["pid"] in known_pids or p["pid"] not in port_of or not _MODEL_SERVER.search(p["name"] or ""):
            continue
        u = unknown.setdefault(p["pid"], {"id": f"pid:{p['pid']}", "label": f"{p['name'] or 'process'} :{port_of[p['pid']]}",
                                          "kind": "process", "state": "running",
                                          "url": f"http://127.0.0.1:{port_of[p['pid']]}", "gpus": [],
                                          "stoppable": False, "started_by": None, "held_mb": None, "models": []})
        if p["gpu"] is not None and p["gpu"] not in u["gpus"]:
            u["gpus"].append(p["gpu"])
    services += list(unknown.values())
    for g in gpus:
        g["services"] = [s["id"] for s in services if g["index"] in s["gpus"]]
        g["others"] = len({p["pid"] for p in procs if p["gpu"] == g["index"] and p["pid"] not in known_pids
                           and p["pid"] not in unknown})
    return {"gpus": gpus, "services": services, "host": host_stats()}
