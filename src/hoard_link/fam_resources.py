"""Opt-in cooperative admission for CPU/RAM/disk work; GPU uses ``lease``.

Timeout or Hub failure stops admission rather than starting unreserved work.
The application must actually keep its execution within the requested budget.
"""
from contextlib import contextmanager
import secrets
import threading
import time
from . import family


@contextmanager
def claim(*, cpu_slots=1, ram_mb=0, io_slots=0, mode="background", timeout_s=60):
    uid = secrets.token_hex(12)
    args = {"request_id": uid, "cpu_slots": cpu_slots, "ram_mb": ram_mb, "io_slots": io_slots, "mode": mode, "ttl_s": 60}
    stopped = threading.Event()
    lost = threading.Event()
    worker = None
    def request():
        status, body = family._post("/api/resources/request", args, 5)
        if status != 200 or not isinstance(body, dict) or not body.get("ok"):
            raise RuntimeError("resource arbiter unavailable or request refused")
        return body["claim"]
    def renew():
        while not stopped.wait(15):
            try:
                request()
            except Exception:
                lost.set()
                return
    try:
        deadline = time.monotonic() + timeout_s
        while request()["state"] != "granted":
            if time.monotonic() >= deadline:
                raise TimeoutError("resources remain queued")
            stopped.wait(min(0.25, max(0, deadline - time.monotonic())))
        worker = threading.Thread(target=renew, daemon=True, name="hoard-resource-renew")
        worker.start()
        yield lost  # cooperative long-running code checks is_set() between chunks.
        if lost.is_set():
            raise RuntimeError("resource reservation was lost during execution")
    finally:
        stopped.set()
        # A renewal already in flight must finish before release, or it could
        # recreate the claim after its work has ended.
        if worker is not None:
            worker.join(timeout=6)
        family._post("/api/resources/release", {"request_id": uid}, 5)
