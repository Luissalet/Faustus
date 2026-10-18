"""One durable native asset job; survives the originating MCP client closing."""
import json
import hashlib
import os
from pathlib import Path
import sys
import threading
import time

import httpx

from .client import BridgeClient, write_json


def hub_request(route, body):
    with httpx.Client(base_url=os.getenv("HOARD_HUB_URL", "http://127.0.0.1:8810"), trust_env=False, timeout=5) as client:
        response = client.post(route, json=body)
        response.raise_for_status()
        return response.json()


def run(path):
    job = json.loads(path.read_text(encoding="utf-8"))
    allocation = None
    stopped = threading.Event()
    try:
        if path.with_suffix(".cancel").exists():
            job.update(state="cancelled", updated_at=time.time())
            write_json(path, job)
            return
        if job["action"] == "views":
            try:
                body = {"owner": "faustus-blender", "purpose": "Scene review " + job["id"],
                        "vram_mb": 2048, "gpu": 0, "priority": 0, "ttl_s": 180, "wait": False, "pid": os.getpid()}
                while True:
                    allocation = hub_request("/api/lease/request", body)
                    if not allocation.get("lease_id"):
                        raise RuntimeError("The Hub did not return a lease")
                    job["resource"] = allocation
                    write_json(path, job)
                    if allocation.get("state") == "granted":
                        break
                    if path.with_suffix(".cancel").exists():
                        job.update(state="cancelled", updated_at=time.time())
                        write_json(path, job)
                        return
                    body = {"lease_id": allocation["lease_id"], "wait": False}
                    stopped.wait(.5)
            except Exception as exc:
                job["resource_warning"] = "Sin reserva compartida: " + str(exc)
                write_json(path, job)

            def renew():
                while not stopped.wait(45):
                    if allocation and allocation.get("lease_id"):
                        try:
                            hub_request("/api/lease/renew", {"lease_id": allocation["lease_id"], "ttl_s": 180})
                        except httpx.HTTPError:
                            pass
            threading.Thread(target=renew, daemon=True).start()
        source = Path(__file__).resolve().parents[2] / "blender_addon" / "faustus_bridge.py"
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        code = ("import importlib.util, sys\n"
                "module = sys.modules.get('_faustus_workflows')\n"
                f"if module is None or getattr(module, '_source_sha', None) != {source_sha!r}:\n"
                f"    spec = importlib.util.spec_from_file_location('_faustus_workflows', {str(source)!r})\n"
                "    module = importlib.util.module_from_spec(spec)\n"
                "    sys.modules[spec.name] = module\n"
                "    spec.loader.exec_module(module)\n"
                f"    module._source_sha = {source_sha!r}\n"
                f"result = module.run_job({str(path)!r})\n")
        BridgeClient(port=job["port"], directory=path.parent.parent).execute(code, timeout=None)
    except Exception as exc:
        current = json.loads(path.read_text(encoding="utf-8"))
        if current["state"] in ("queued", "running"):
            current.update(state="failed", error=str(exc), updated_at=time.time())
            write_json(path, current)
    finally:
        stopped.set()
        if allocation and allocation.get("lease_id"):
            try:
                hub_request("/api/lease/release", {"lease_id": allocation["lease_id"]})
            except httpx.HTTPError:
                pass


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
