"""Best-effort GPU free-memory query via ``nvidia-smi``.

Used before handing a GPU job (ComfyUI) to a resolved server so an app can
show "not enough VRAM free" instead of a confusing timeout. Never raises:
no ``nvidia-smi`` (no NVIDIA GPU, or not on PATH) simply yields ``[]``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GpuProbe:
    """A completed empty query is different from a query that failed."""
    gpus: tuple["GpuMemory", ...] = ()
    error: str = ""
    error_type: str = ""

    @property
    def status(self) -> str:
        return "error" if self.error else "ready" if self.gpus else "empty"


@dataclass(frozen=True)
class GpuMemory:
    index: int
    total_mb: int
    used_mb: int

    @property
    def free_mb(self) -> int:
        return self.total_mb - self.used_mb


def _nvidia_smi() -> str:
    """Locate nvidia-smi; older Windows drivers install it outside PATH."""
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if sys.platform == "win32":
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        if system.is_file():
            return str(system)
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        legacy = Path(program_files) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"
        if legacy.is_file():
            return str(legacy)
    return "nvidia-smi"


def probe_gpu_memory(timeout_s: float = 2.0) -> GpuProbe:
    """Blocking query with diagnostics; from async code use asyncio.to_thread."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [
                _nvidia_smi(),
                "--query-gpu=index,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout_s,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        return GpuProbe(error=f"nvidia-smi timed out after {timeout_s:g}s", error_type="timeout")
    except FileNotFoundError:
        return GpuProbe(error="nvidia-smi executable not found", error_type="not_found")
    except (OSError, subprocess.SubprocessError) as exc:
        return GpuProbe(error=f"nvidia-smi could not run: {str(exc)[:300]}", error_type="unavailable")
    if proc.returncode != 0:
        detail = str(proc.stderr or proc.stdout or "").strip()[:300]
        return GpuProbe(error=f"nvidia-smi exited {proc.returncode}" + (f": {detail}" if detail else ""), error_type="exit")
    out: list[GpuMemory] = []
    invalid = 0
    for line in (proc.stdout or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            invalid += 1
            continue
        try:
            g = GpuMemory(index=int(parts[0]), total_mb=int(parts[1]), used_mb=int(parts[2]))
            if g.index < 0 or g.total_mb <= 0 or g.used_mb < 0 or g.used_mb > g.total_mb or any(p.index == g.index for p in out):
                invalid += 1
                continue
            out.append(g)
        except ValueError:
            invalid += 1
            continue
    if invalid:
        return GpuProbe(tuple(out), error=f"nvidia-smi returned {invalid} unusable GPU memory row(s)", error_type="invalid_output")
    return GpuProbe(tuple(out))


def gpu_free_mb(timeout_s: float = 2.0) -> list[GpuMemory]:
    """Legacy best-effort view. Admission decisions must use probe_gpu_memory."""
    return list(probe_gpu_memory(timeout_s).gpus)
