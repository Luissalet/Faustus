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
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        legacy = Path(program_files) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"
        if legacy.is_file():
            return str(legacy)
    return "nvidia-smi"


def gpu_free_mb(timeout_s: float = 2.0) -> list[GpuMemory]:
    """Blocking (runs a subprocess): from async code use ``asyncio.to_thread``."""
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
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out: list[GpuMemory] = []
    for line in (proc.stdout or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            out.append(GpuMemory(index=int(parts[0]), total_mb=int(parts[1]), used_mb=int(parts[2])))
        except ValueError:
            continue
    return out
