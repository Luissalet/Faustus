"""The visual embedder in a separate process, under another Python interpreter (``visual_index_python``).

Faustus' own environment may carry a CPU-only ``torch`` (the speech, transcription and gallery code asks
``torch.cuda.is_available()`` and would start using GPU 0 if that changed). When ``visual_index_python`` names the
``python.exe`` of another environment with a CUDA ``torch`` and ``transformers>=4.57`` (plus Pillow and numpy), the index
embeds there instead: :class:`SubprocessEmbedder` starts ``src/visual_index/worker.py`` with that interpreter and talks to
it in JSON lines (see the worker's docstring). Same interface as :class:`embedder.Qwen3VLEmbedder`:

* the card is chosen by :func:`embedder.choose_device` (never GPU 0 or 1; a lease from the arbiter; no free card means the
  CPU inside the worker), using the interpreter's own count of cards -- the Faustus process need not have torch at all;
* the worker starts with ``CUDA_DEVICE_ORDER=PCI_BUS_ID`` and ``CUDA_VISIBLE_DEVICES=<n>`` (empty for the CPU), so inside it
  the chosen card is ``cuda:0`` and no other card exists for it;
* images travel as temporary PNG files; the worker is restarted once if it dies in the middle of a request; ``close()``
  (the pool calls it after ``visual_index_idle_unload_seconds`` without use) ends the worker and gives the lease back.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence

import numpy as np
from PIL import Image

from .embedder import (DEFAULT_MODEL, DeviceChoice, EmbedderUnavailable, _setting, allowed_gpus, choose_device, model_downloaded,
                       models_dir, release_choice)

logger = logging.getLogger(__name__)

WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")
MIN_TRANSFORMERS = "4.57"
START_TIMEOUT_S = 60.0           # until the worker prints its "ready" line
LOAD_TIMEOUT_S = 3600.0          # the first load may download about 4.5 GB
PROBE_TIMEOUT_S = 180.0
DEFAULT_REQUEST_TIMEOUT_S = 900.0
CLOSE_GRACE_S = 5.0
EXPECTED_LIBS = 'torch (a CUDA build to use a GPU), "transformers>=4.57", pillow and numpy'

_PROBES: Dict[Any, Dict[str, Any]] = {}
_PROBES_LOCK = threading.Lock()


def resolve_python(raw: Any) -> str:
    """The interpreter named by ``visual_index_python``: quotes stripped, ``~`` expanded; a folder (a virtual environment)
    resolves to the interpreter inside it. ``""`` when unset."""
    text = str(raw or "").strip().strip('"').strip("'").strip()
    if not text:
        return ""
    text = os.path.expanduser(os.path.expandvars(text))
    if os.path.isdir(text):
        for rel in (("Scripts", "python.exe"), ("python.exe"), ("bin", "python"), ("bin", "python3")):
            cand = os.path.join(text, *([rel] if isinstance(rel, str) else rel))
            if os.path.isfile(cand):
                return cand
    return text


def configured_python() -> str:
    return resolve_python(_setting("visual_index_python", ""))


def _install_line(python: str) -> str:
    return f'"{python}" -m pip install torch "transformers>={MIN_TRANSFORMERS}" pillow numpy'


def worker_env(gpu: Optional[int], extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment of the worker: the other interpreter must not inherit the Faustus environment's Python variables, and it
    sees ONLY the chosen card (``gpu`` is the host index in PCI bus order; ``None`` means no card at all)."""
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    env["CUDA_VISIBLE_DEVICES"] = "" if gpu is None else str(int(gpu))
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    if os.name == "nt":
        env.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    env.update(extra or {})
    return env


def _creationflags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def probe_python(python: str, *, script: str = WORKER_SCRIPT, timeout: float = PROBE_TIMEOUT_S, force: bool = False,
                 extra_env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Ask ``python`` what it has (torch, CUDA, how many cards, transformers). Cached per interpreter file; raises
    :class:`EmbedderUnavailable` with a precise message when it cannot run or lacks the packages. No card is opened."""
    if not python:
        raise EmbedderUnavailable("visual_index_python is empty", code="python_not_found")
    if not os.path.isfile(python):
        raise EmbedderUnavailable(f"visual_index_python points to {python!r}, which is not a file. Set it to the python.exe of an "
                                  f"environment with {EXPECTED_LIBS}.", code="python_not_found")
    try:
        key = (os.path.abspath(python), os.path.getmtime(python), os.path.abspath(script))
    except OSError:
        key = (python, 0.0, script)
    with _PROBES_LOCK:
        if not force and key in _PROBES:
            return _PROBES[key]
    env = worker_env(None, extra_env)                       # a probe opens no card: nothing visible
    env.pop("CUDA_VISIBLE_DEVICES", None)                   # ...but it must COUNT them all
    try:
        done = subprocess.run([python, "-u", script, "--probe"], capture_output=True, timeout=timeout, env=env,
                              creationflags=_creationflags())
    except FileNotFoundError as exc:
        raise EmbedderUnavailable(f"visual_index_python {python!r} cannot be started ({exc})", code="python_not_found") from exc
    except PermissionError as exc:
        raise EmbedderUnavailable(f"visual_index_python {python!r} cannot be run ({exc})", code="python_not_found") from exc
    except subprocess.TimeoutExpired as exc:
        raise EmbedderUnavailable(f"{python!r} did not answer the probe in {timeout:.0f} s (importing torch hangs?)",
                                  code="python_unusable") from exc
    except OSError as exc:
        raise EmbedderUnavailable(f"visual_index_python {python!r} cannot be started ({exc})", code="python_not_found") from exc
    stdout = done.stdout.decode("utf-8", "replace")
    info: Optional[Dict[str, Any]] = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                info = json.loads(line)
            except ValueError:
                continue
            break
    if not isinstance(info, dict):
        tail = done.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise EmbedderUnavailable(f"{python!r} ran but did not answer the probe (exit {done.returncode}): {' | '.join(tail) or 'no output'}",
                                  code="python_unusable")
    missing = [str(m) for m in (info.get("missing") or [])]
    if missing:
        raise EmbedderUnavailable(f"The Python at {python!r} is missing {', '.join(missing)}. Install them there: {_install_line(python)}",
                                  code="dependencies_missing")
    if info.get("transformers_ok") is False:
        raise EmbedderUnavailable(f"The Python at {python!r} has transformers {info.get('transformers')}, which is older than "
                                  f"{MIN_TRANSFORMERS} (no Qwen3-VL). Upgrade it: {_install_line(python)}", code="dependencies_broken")
    info["python_path"] = python
    with _PROBES_LOCK:
        _PROBES[key] = info
    return info


def clear_probe_cache() -> None:
    with _PROBES_LOCK:
        _PROBES.clear()


class WorkerDied(Exception):
    """The worker exited (or its pipes broke) in the middle of a request."""


class _Proc:
    """One worker process with a thread reading its stdout lines and another keeping the tail of its stderr."""

    def __init__(self, popen: subprocess.Popen):
        self.popen = popen
        self.lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self.stderr_tail: Deque[str] = deque(maxlen=60)
        self.loaded = False
        self.info: Dict[str, Any] = {}
        threading.Thread(target=self._pump_out, daemon=True, name="visual-worker-out").start()
        threading.Thread(target=self._pump_err, daemon=True, name="visual-worker-err").start()

    def _pump_out(self) -> None:
        try:
            for raw in self.popen.stdout:                       # type: ignore[union-attr]
                self.lines.put(raw.decode("utf-8", "replace").strip())
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.lines.put(None)

    def _pump_err(self) -> None:
        try:
            for raw in self.popen.stderr:                       # type: ignore[union-attr]
                text = raw.decode("utf-8", "replace").rstrip()
                if text:
                    self.stderr_tail.append(text)
        except Exception:  # noqa: BLE001
            pass

    def alive(self) -> bool:
        return self.popen.poll() is None

    def tail(self, n: int = 6) -> str:
        return " | ".join(list(self.stderr_tail)[-n:])

    def kill(self) -> None:
        try:
            if self.popen.poll() is None:
                self.popen.kill()
            self.popen.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        for stream in (self.popen.stdin, self.popen.stdout, self.popen.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass


class SubprocessEmbedder:
    """:class:`embedder.VisualEmbedder` backed by ``worker.py`` run with another Python (see the module docstring)."""

    mode = "subprocess"

    def __init__(self, python: Optional[str] = None, *, model_id: str = DEFAULT_MODEL, directory: Optional[str] = None,
                 script: Optional[str] = None, batch_size: Optional[int] = None, request_timeout: Optional[float] = None,
                 lease_factory: Optional[Callable[..., Any]] = None, extra_env: Optional[Dict[str, str]] = None,
                 dim: int = 2048):
        self.name = model_id
        self.dim = dim
        self.python = resolve_python(python) if python is not None else configured_python()
        self.directory = directory or models_dir()
        self.script = script or WORKER_SCRIPT
        self._batch = batch_size
        self._timeout_override = request_timeout
        self._lease_factory = lease_factory
        self._extra_env = dict(extra_env or {})
        self._lock = threading.RLock()
        self._proc: Optional[_Proc] = None
        self._choice: Optional[DeviceChoice] = None
        self._tmp: Optional[str] = None
        self._seq = 0
        self.restarts = 0
        self.device_reason = ""

    # ---- status
    def request_timeout(self) -> float:
        if self._timeout_override is not None:
            return float(self._timeout_override)
        try:
            return max(5.0, float(_setting("visual_index_worker_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_S) or DEFAULT_REQUEST_TIMEOUT_S))
        except (TypeError, ValueError):
            return DEFAULT_REQUEST_TIMEOUT_S

    def _cached_probe(self) -> Optional[Dict[str, Any]]:
        try:
            key = (os.path.abspath(self.python), os.path.getmtime(self.python), os.path.abspath(self.script))
        except OSError:
            return None
        with _PROBES_LOCK:
            return _PROBES.get(key)

    def status(self, *, probe: bool = False) -> Dict[str, Any]:
        """What is known without starting anything (``probe=True`` runs the interpreter check, which imports torch there)."""
        exists = bool(self.python) and os.path.isfile(self.python)
        problem = ""
        info = self._cached_probe()
        if probe and exists:
            try:
                info = probe_python(self.python, script=self.script, extra_env=self._extra_env)
            except EmbedderUnavailable as exc:
                problem = str(exc)
        if not problem and not exists:
            problem = (f"visual_index_python {self.python!r} is not a file" if self.python else "visual_index_python is empty")
        proc = self._proc
        alive = proc is not None and proc.alive()
        choice = self._choice
        return {"embedder": self.name, "mode": "subprocess", "dim": self.dim, "python": self.python, "python_exists": exists,
                "python_info": ({k: info.get(k) for k in ("python", "torch", "cuda", "cuda_available", "device_count", "transformers",
                                                          "transformers_ok")} if info else None),
                "problem": problem, "dependencies_missing": [], "install_hint": _install_line(self.python or "python") if problem else "",
                "model_downloaded": model_downloaded(self.name, self.directory), "models_dir": self.directory,
                "loaded": bool(alive and proc and proc.loaded), "worker_alive": alive, "worker_pid": proc.popen.pid if alive and proc else None,
                "restarts": self.restarts, "device": (choice.device if choice else ""), "gpu": (choice.gpu if choice else None),
                "worker_device": ((proc.info.get("device") if proc else "") or ""), "device_reason": self.device_reason,
                "allowed_gpus": allowed_gpus()}

    # ---- process life
    def _spawn(self) -> _Proc:
        if self._choice is None:
            info = probe_python(self.python, script=self.script, extra_env=self._extra_env)
            count = int(info.get("device_count") or 0)
            self._choice = choose_device(lease_factory=self._lease_factory, gpu_count=lambda: count)
            self.device_reason = self._choice.reason
        choice = self._choice
        device = "cuda:0" if choice.gpu is not None else "cpu"
        os.makedirs(self.directory, exist_ok=True)
        cmd = [self.python, "-u", self.script, "--model", self.name, "--cache-dir", self.directory, "--device", device]
        if self._batch:
            cmd += ["--batch", str(int(self._batch))]
        logger.info("Starting the visual embedding worker: %s (host device %s, %s)", self.python, choice.device, choice.reason or "ok")
        try:
            popen = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=worker_env(choice.gpu, self._extra_env), creationflags=_creationflags())
        except OSError as exc:
            raise EmbedderUnavailable(f"the visual embedding worker could not be started with {self.python!r} ({exc})",
                                      code="python_not_found") from exc
        proc = _Proc(popen)
        try:
            ready = self._read(proc, START_TIMEOUT_S, want_id=None)
        except (WorkerDied, EmbedderUnavailable) as exc:
            tail = proc.tail()
            proc.kill()
            raise EmbedderUnavailable(f"the visual embedding worker did not start ({exc}); its log: {tail or 'empty'}",
                                      code="worker_failed") from exc
        proc.info.update({k: v for k, v in ready.items() if k not in ("ok", "event")})
        self._proc = proc
        try:
            loaded = self._roundtrip(proc, {"op": "load"}, LOAD_TIMEOUT_S)
        except WorkerDied as exc:
            tail = proc.tail()
            self._drop_proc()
            raise EmbedderUnavailable(f"the visual embedding worker died while loading the model ({exc}); its log: {tail or 'empty'}",
                                      code="worker_failed") from exc
        except Exception:
            self._drop_proc()
            raise
        proc.info.update({k: v for k, v in loaded.items() if k not in ("ok", "seconds", "id")})
        proc.loaded = True
        self.dim = int(loaded.get("dim") or self.dim)
        return proc

    def _drop_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.kill()

    def _ensure(self) -> _Proc:
        proc = self._proc
        if proc is not None and proc.alive():
            return proc
        if proc is not None:
            self.restarts += 1
            logger.warning("The visual embedding worker died (%s); starting another", proc.tail(3))
            self._drop_proc()
        try:
            return self._spawn()
        except EmbedderUnavailable:
            release_choice(self._choice)
            self._choice = None
            raise

    # ---- protocol
    def _read(self, proc: _Proc, timeout: float, want_id: Optional[int]) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise EmbedderUnavailable(f"the visual embedding worker did not answer in {timeout:.0f} s", code="worker_timeout")
            try:
                line = proc.lines.get(timeout=min(left, 1.0))
            except queue.Empty:
                if not proc.alive() and proc.lines.empty():
                    raise WorkerDied(f"it exited with code {proc.popen.poll()}")
                continue
            if line is None:
                raise WorkerDied(f"it exited with code {proc.popen.poll()}")
            if not line.startswith("{"):
                logger.debug("visual worker stdout (ignored): %s", line[:200])
                continue
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if not isinstance(reply, dict):
                continue
            if want_id is not None and reply.get("id") != want_id:
                continue
            return reply

    def _roundtrip(self, proc: _Proc, request: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        self._seq += 1
        request = {**request, "id": self._seq}
        try:
            proc.popen.stdin.write((json.dumps(request) + "\n").encode("utf-8"))      # type: ignore[union-attr]
            proc.popen.stdin.flush()                                                   # type: ignore[union-attr]
        except (OSError, ValueError) as exc:
            raise WorkerDied(f"its input closed ({exc})") from exc
        try:
            reply = self._read(proc, timeout, self._seq)
        except EmbedderUnavailable as exc:
            if exc.code == "worker_timeout":
                tail = proc.tail()
                self._drop_proc()
                raise EmbedderUnavailable(f"{exc}; the worker was stopped and starts again on the next call. Its log: {tail or 'empty'}",
                                          code="worker_timeout") from exc
            raise
        if not reply.get("ok"):
            raise EmbedderUnavailable(str(reply.get("error") or "the visual embedding worker failed"),
                                      code=str(reply.get("code") or "worker_error"))
        return reply

    def _call(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """One request; if the worker dies under it, start another and ask again (once)."""
        last: Optional[WorkerDied] = None
        tail = ""
        for _ in range(2):
            proc = self._ensure()
            try:
                return self._roundtrip(proc, request, self.request_timeout())
            except WorkerDied as exc:
                last = exc
                tail = proc.tail()
                logger.warning("The visual embedding worker died during %s: %s (%s)", request.get("op"), exc, tail)
                self._drop_proc()
                self.restarts += 1
        raise EmbedderUnavailable(f"the visual embedding worker died twice in a row ({last}); its log: {tail or 'empty'}",
                                  code="worker_died")

    # ---- VisualEmbedder
    def ensure_loaded(self) -> None:
        with self._lock:
            self._ensure()

    @staticmethod
    def _decode(reply: Dict[str, Any], key: str, shape_tail: Sequence[int]) -> np.ndarray:
        shape = [int(x) for x in reply.get("shape") or []]
        if reply.get(key + "_b64") is not None:
            raw = base64.b64decode(reply[key + "_b64"])
            arr = np.frombuffer(raw, dtype="<f4").astype(np.float32)
            arr = arr.reshape(shape) if shape else arr
        elif key in reply:
            arr = np.asarray(reply[key], dtype=np.float32)
        else:
            raise EmbedderUnavailable("the visual embedding worker answered without vectors", code="worker_error")
        if not np.all(np.isfinite(arr)):
            raise EmbedderUnavailable("the visual embedding worker returned non-finite numbers", code="worker_error")
        return arr

    def embed_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        with self._lock:
            self._ensure()
            if not images:
                return np.zeros((0, self.dim), dtype=np.float32)
            folder = tempfile.mkdtemp(prefix="faustus-visual-")
            try:
                paths = []
                for i, im in enumerate(images):
                    path = os.path.join(folder, f"{i:04d}.png")
                    im.convert("RGB").save(path, format="PNG", compress_level=1)
                    paths.append(path)
                reply = self._call({"op": "embed_images", "paths": paths})
            finally:
                shutil.rmtree(folder, ignore_errors=True)
            arr = self._decode(reply, "vectors", ())
            self.dim = int(reply.get("dim") or self.dim)
            if arr.ndim != 2 or arr.shape[0] != len(images):
                raise EmbedderUnavailable(f"the visual embedding worker returned {tuple(arr.shape)} for {len(images)} images",
                                          code="worker_error")
            return arr

    def embed_query(self, text: str) -> np.ndarray:
        with self._lock:
            reply = self._call({"op": "embed_query", "text": str(text)})
            arr = self._decode(reply, "vector", ())
            self.dim = int(reply.get("dim") or self.dim)
            if arr.ndim != 1:
                raise EmbedderUnavailable(f"the visual embedding worker returned {tuple(arr.shape)} for one query", code="worker_error")
            return arr

    def worker_info(self) -> Dict[str, Any]:
        with self._lock:
            reply = self._call({"op": "info"})
            return {k: v for k, v in reply.items() if k not in ("ok", "id")}

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            if proc is not None:
                if proc.alive():
                    try:
                        proc.popen.stdin.write(b'{"op":"close"}\n')                 # type: ignore[union-attr]
                        proc.popen.stdin.flush()                                    # type: ignore[union-attr]
                        proc.popen.wait(timeout=CLOSE_GRACE_S)
                    except Exception:  # noqa: BLE001
                        pass
                proc.kill()
            release_choice(self._choice)
            self._choice = None
