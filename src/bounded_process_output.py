"""Drain subprocess pipes continuously while retaining only a fixed tail."""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Capture:
    stdout: bytes
    stderr: bytes
    truncated: bool
    timed_out: bool
    cancelled: bool = False


def capture(proc, *, timeout, stop, limit=64_000, cancel_requested=None):
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("output tail limit must be a positive integer")
    buffers = [bytearray(), bytearray()]
    counts = [0, 0]
    lock = threading.Lock()
    failed = threading.Event()
    finished = [threading.Event(), threading.Event()]

    def drain(index, stream):
        try:
            if stream is not None:
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        break
                    with lock:
                        counts[index] += len(chunk)
                        buffers[index].extend(chunk)
                        if len(buffers[index]) > limit:
                            del buffers[index][:-limit]
        except Exception:
            failed.set()
        finally:
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                failed.set()
            finally:
                finished[index].set()

    readers = [threading.Thread(target=drain, args=(i, stream),
                                name=f"faustus-output-{i}", daemon=True)
               for i, stream in enumerate((proc.stdout, proc.stderr))]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + max(0.01, float(timeout))
    timed_out = False
    cancelled = False
    try:
        while proc.poll() is None:
            if cancel_requested is not None and cancel_requested():
                cancelled = True
                stop()
                break
            if failed.is_set() or time.monotonic() >= deadline:
                timed_out = not failed.is_set()
                stop()
                break
            try:
                proc.wait(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()  # only this still-owned Popen handle, never a bare persisted PID
            proc.wait(timeout=5)
    except BaseException:
        try:
            if proc.poll() is None:
                stop()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        raise
    finally:
        join_deadline = time.monotonic() + 5
        for reader in readers:
            reader.join(timeout=max(0, join_deadline - time.monotonic()))
    if failed.is_set() or not all(event.is_set() for event in finished):
        raise RuntimeError("subprocess output could not be collected completely")
    with lock:
        return Capture(bytes(buffers[0]), bytes(buffers[1]), any(n > limit for n in counts), timed_out, cancelled)
