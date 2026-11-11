"""Materialize tab screencast frames as a timestamped WebM, never inline in MCP."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import math
from pathlib import Path
import shutil
import tempfile

import anyio


def encoder() -> str:
    from src.hoard_link.media.bins import find
    result = find("ffmpeg")
    if not result:
        raise ValueError("Video recording requires ffmpeg on this computer")
    return result.path


async def save_video(video: dict, filename: str) -> dict:
    """Validate before creating the destination, and never replace existing bytes."""
    path = Path(filename)
    if not path.is_absolute() or not path.parent.is_dir() or path.exists():
        raise ValueError("Video requires a new absolute output file in an existing directory")
    if not isinstance(video, dict):
        raise ValueError("Invalid video metadata")
    frames = video.get("frames")
    if video.get("mime") != "video/webm" or not isinstance(frames, list) or not frames:
        raise ValueError("Browser returned no video frames")
    executable = encoder()
    with tempfile.TemporaryDirectory(prefix="faustus-tab-video-") as folder:
        root = Path(folder)
        timestamps, lines = [], []
        for index, frame in enumerate(frames):
            if not isinstance(frame, dict):
                raise ValueError("Invalid video frame")
            timestamp = frame.get("timestamp")
            if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp)
                    or timestamps and timestamp < timestamps[-1]):
                raise ValueError("Invalid video frame timestamp")
            try:
                data = base64.b64decode(frame["data"], validate=True)
            except (KeyError, TypeError, ValueError, binascii.Error) as exc:
                raise ValueError("Invalid video frame bytes") from exc
            if not data.startswith(b"\xff\xd8\xff"):
                raise ValueError("Video frame is not JPEG")
            (root / f"{index}.jpg").write_bytes(data)
            timestamps.append(timestamp)
        for index, timestamp in enumerate(timestamps):
            duration = max(.001, timestamps[index + 1] - timestamp) if index + 1 < len(timestamps) else .2
            lines.extend([f"file '{index}.jpg'", f"duration {duration:.6f}"])
        lines.append(f"file '{len(timestamps) - 1}.jpg'")
        (root / "frames.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        # Encode to a private temp file, then publish exclusively. Failed encodes
        # leave no misleading final video; another writer's destination is retained.
        process = await asyncio.create_subprocess_exec(executable, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "concat", "-safe", "1", "-i", str(root / "frames.txt"), "-an", "-vf",
            "scale=1280:720:force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-fps_mode", "vfr", "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "6", "-f", "webm", str(root / "encoded.webm"),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, stderr = await process.communicate()
        except BaseException:
            with anyio.CancelScope(shield=True):
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            raise
        if process.returncode:
            raise ValueError("Video encoding failed: " + stderr.decode("utf-8", errors="replace")[-1200:])
        with (root / "encoded.webm").open("rb") as source, path.open("xb") as destination:
            shutil.copyfileobj(source, destination)
    digest = hashlib.sha256()
    with path.open("rb") as result:
        for chunk in iter(lambda: result.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path), "name": path.name, "mime": "video/webm", "bytes": path.stat().st_size,
            "sha256": digest.hexdigest(), "complete": True, "frames": len(frames),
            "duration_seconds": timestamps[-1] - timestamps[0] + .2, "cursor": video.get("cursor", False),
            "last_minute": video.get("last_minute", False)}
