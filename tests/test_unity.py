"""Exercise the real HTTP boundary: a timed-out edit must not be submitted twice."""
import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.unity.client import UnityClient
from src.unity.tool import UnityTool, execute


@pytest.fixture
def bridge(tmp_path):
    (tmp_path / "ProjectSettings").mkdir()
    (tmp_path / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.5f1")
    state = {"project": str(tmp_path), "requests": [], "mode": "normal", "edits": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(body["operation"])
            if body["operation"] != "status":
                state["edits"] += 1
                if state["mode"] == "disconnect":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
            reply = {"ok": True, "project": state["project"], "result": body}
            if state["mode"] == "missing_identity":
                reply.pop("project")
            text = "PENDING id=7" if state["mode"] == "pending" else json.dumps(reply)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(text.encode())

        def do_GET(self):
            state["requests"].append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "project": state["project"], "result": "finished"}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (tmp_path / "Logs").mkdir()
    (tmp_path / "Logs/bridge_port.txt").write_text(str(server.server_port), encoding="utf-8-sig")
    state["client"] = UnityClient(tmp_path, timeout=2)
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


def test_reused_port_never_edits_another_project(bridge, tmp_path):
    bridge["project"] = str(tmp_path / "another-project")
    result = bridge["client"].operation("create_object", {"name": "Own object"})
    assert result["ok"] is False
    assert result["requested_operation_sent"] is False
    assert result["outcome_unknown"] is False
    assert bridge["edits"] == 0


def test_missing_identity_does_not_apply_an_edit(bridge):
    bridge["mode"] = "missing_identity"
    result = bridge["client"].operation("create_object")
    assert not result["ok"]
    assert bridge["edits"] == 0


def test_disconnect_after_edit_reports_uncertain_without_replay(bridge):
    bridge["mode"] = "disconnect"
    result = bridge["client"].operation("create_object", {"name": "Once"})
    assert not result["ok"] and result["outcome_unknown"]
    assert result["requested_operation_sent"]
    assert bridge["edits"] == 1
    assert bridge["requests"] == ["status", "create_object"]


def test_pending_identity_preserves_requested_edit_unsent(bridge):
    bridge["mode"] = "pending"
    result = bridge["client"].operation("create_object")
    assert result["state"] == "pending" and result["job_id"] == 7
    assert not result["requested_operation_sent"]
    assert bridge["edits"] == 0


def test_job_poll_checks_project_before_consuming_result(bridge):
    bridge["project"] += "-reused"
    result = bridge["client"].job(7)
    assert not result["ok"]
    assert bridge["requests"] == ["status"]


def test_native_tool_uses_discovered_project_port(bridge):
    result = asyncio.run(UnityTool().execute({"action": "operation", "project": bridge["project"],
                                             "operation": "create_object", "arguments": {"name": "New"}}))
    assert result["exit_code"] == 0
    assert result["structuredContent"]["result"]["arguments"] == {"name": "New"}
    assert bridge["edits"] == 1


def test_schema_discovery_does_not_start_editor(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Discovery connected to an editor")
    monkeypatch.setattr(UnityClient, "__init__", unexpected)
    schema = execute({"action": "schema"})
    assert {"scene", "create_material", "run_tests", "tests_job"} <= set(schema["operations"])


def test_numeric_and_test_job_ids_are_distinct(bridge):
    with pytest.raises(ValueError, match="numeric"):
        bridge["client"].job("a" * 32)


def test_profile_distinguishes_declared_and_locked_web_versions(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"three": "^0.180.0", "@react-three/fiber": "^9.0.0"}}))
    (tmp_path / "package-lock.json").write_text(json.dumps({"packages": {"node_modules/three": {"version": "0.180.2"}}}))
    result = execute({"action": "project_profile", "project": str(tmp_path)})
    engine = result["engines"][0]
    assert engine["declared_packages"]["three"] == "^0.180.0"
    assert engine["locked_packages"]["three"] == "0.180.2"
    assert "@react-three/fiber" not in engine["locked_packages"]


def test_godot_feature_is_not_claimed_as_installed_version(tmp_path):
    (tmp_path / "project.godot").write_text('config_version=5\n[application]\nconfig/features=PackedStringArray("4.5", "Forward Plus")\n')
    result = execute({"action": "project_profile", "project": str(tmp_path)})
    engine = result["engines"][0]
    assert engine["engine"] == "godot" and engine["version"] is None
    assert engine["required_features"] == ["4.5", "Forward Plus"]


def test_profile_unity_is_available_without_running_bridge(bridge):
    result = execute({"action": "project_profile", "project": bridge["project"]})
    assert result["engines"][0]["version"] == "6000.6.5f1"
    assert bridge["requests"] == []
