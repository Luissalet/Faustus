"""One request per operation; an uncertain mutation is never replayed."""
import json
import re
from pathlib import Path

import httpx

OPERATIONS = ("status", "scene", "object", "material", "select", "create_object", "add_component", "set_properties",
              "create_material", "modify_material", "assign_material", "create_prefab", "instantiate_prefab",
              "new_scene", "load_scene", "save_scene", "active_scene", "unload_scene", "undo", "redo",
              "playmode", "run_tests", "tests_job")
READ_OPERATIONS = frozenset({"status", "scene", "object", "material", "tests_job"})


class UnityClient:
    def __init__(self, project, port=None, timeout=30):
        self.project = Path(project).expanduser().resolve()
        if not (self.project / "ProjectSettings/ProjectVersion.txt").is_file():
            raise ValueError("Select a Unity project containing ProjectSettings/ProjectVersion.txt")
        self.port = int(port) if port is not None else int((self.project / "Logs/bridge_port.txt").read_text(encoding="utf-8-sig").strip())
        if not 1 <= self.port <= 65535:
            raise ValueError("Invalid local Unity bridge port")
        self.timeout = timeout

    def _request(self, method, route, payload=None):
        with httpx.Client(timeout=self.timeout, trust_env=False) as client:
            response = client.request(method, f"http://127.0.0.1:{self.port}/{route}", json=payload)
            response.raise_for_status()
            return response.text

    def operation(self, operation, arguments=None):
        if operation not in OPERATIONS:
            raise ValueError("Unknown Unity operation: " + str(operation))
        if arguments is not None and not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        # Identity is checked before a mutation: a port may have been reused by another editor.
        sent = False
        try:
            if operation != "status":
                status = self._identity()
                if not status.get("ok"):
                    return status
                if status.get("state") == "pending":
                    return {**status, "operation": "status", "requested_operation": operation,
                            "requested_operation_sent": False}
            sent = True
            return self._decode(self._request("POST", "api", {"operation": operation, "arguments": arguments or {}}))
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "operation": operation, "requested_operation_sent": sent,
                    "outcome_unknown": sent and operation not in READ_OPERATIONS,
                    "error": str(exc), "recovery": "Read the scene or job before deciding whether to repeat the operation."}

    def _identity(self):
        value = self._decode(self._request("POST", "api", {"operation": "status", "arguments": {}}))
        if value.get("ok") and value.get("state") != "pending" and not value.get("project"):
            raise ValueError("Unity bridge did not identify its project")
        return value

    def _decode(self, text):
        match = re.fullmatch(r"PENDING id=(\d+)", text.strip())
        if match:
            return {"ok": True, "state": "pending", "job_id": int(match.group(1)), "port": self.port}
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("Unity returned a non-object response")
        if value.get("ok") and "project" in value and Path(value["project"]).resolve() != self.project:
            raise ValueError("Unity bridge belongs to a different project")
        return value

    def job(self, job_id):
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1:
            raise ValueError("Expected the pending numeric job_id")
        try:
            identity = self._identity()
            if not identity.get("ok") or identity.get("state") == "pending":
                return {**identity, "requested_job_id": job_id, "requested_job_polled": False}
            return self._decode(self._request("GET", "result?id=" + str(job_id)))
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "outcome_unknown": True, "job_id": job_id, "error": str(exc),
                    "recovery": "A domain reload may have ended the connection. Inspect the scene before repeating."}

    def diagnostics(self, since=0):
        since = int(since)
        identity = self._identity()
        if not identity.get("ok") or identity.get("state") == "pending":
            return identity
        return {"status": self._request("GET", "status"), "console": self._request("GET", "log?since=" + str(since)),
                "project": str(self.project), "port": self.port}
