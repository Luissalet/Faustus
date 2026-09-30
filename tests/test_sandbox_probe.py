"""H02: the sandbox probe tries real allowed and forbidden accesses.

The container-backed checks need a reachable Docker daemon and an image with
python (``container_test_image``); without one they skip, and a skip is not
evidence. The fixture, judging and "a hole is reported" logic run anywhere.
"""
from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from src import sandbox_probe as probe
from src.execution_backends import DockerWorkspaceBackend


def run(coro):
    return asyncio.run(coro)


# ── no Docker needed ───────────────────────────────────────────────────────

def test_fixture_builds_a_sibling_sharing_the_root_prefix_and_links_that_point_out():
    fx = probe.Fixture()
    try:
        assert os.path.basename(fx.sibling) == "root2" and os.path.basename(fx.root) == "root"
        assert open(fx.decoy, encoding="utf-8").read() == fx.tokens["out"]
        if fx.links_created:
            assert os.path.realpath(fx.link_abs) == os.path.realpath(fx.decoy)
            assert os.path.realpath(os.path.join(fx.link_rel)) == os.path.realpath(fx.decoy)
            assert os.path.realpath(fx.link_dir) == os.path.realpath(fx.outside)
        assert fx.env_name in os.environ
    finally:
        fx.close()
    assert fx.env_name not in os.environ and not os.path.exists(fx.base)


def test_judge_reports_a_hole_a_pass_and_an_inconclusive_check():
    fx = probe.Fixture()
    try:
        defs = probe._check_defs(fx, workspace="/workspace", network=False, workspace_writable=True,
                                 inside_readable=True, host="127.0.0.1")
        names = {d["name"] for d in defs}
        assert {"read_outside_absolute_host_path", "read_sibling_relative", "symlink_absolute_does_not_extend_access",
                "directory_link_does_not_extend_access", "network_to_local_server",
                "child_process_read_outside", "host_environment_not_inherited"} <= names
        seen = {
            "read_inside": {"rc": 0, "out": fx.tokens["in"]},
            "read_outside_absolute_host_path": {"rc": 0, "out": fx.tokens["out"]},        # a leak
            "read_sibling_relative": {"rc": 1, "out": "cat: No such file or directory"},   # fine
            "network_to_local_server": {"rc": 1, "out": "something unrelated"},            # unknown why
        }
        rows = {r["check"]: r for r in probe._judge(defs, seen)}
        assert rows["read_inside"]["ok"] is True
        leak = rows["read_outside_absolute_host_path"]
        assert leak["observed"] == "allowed" and leak["ok"] is False
        assert fx.tokens["out"] not in leak["evidence"], "the marker is never echoed back"
        assert rows["read_sibling_relative"]["ok"] is True
        assert rows["network_to_local_server"]["ok"] is None, "denied for no recognisable reason is not a pass"
        assert rows["write_inside"]["ok"] is None and "no result" in rows["write_inside"]["evidence"]
        assert probe._status(list(rows.values())) == "failed"
    finally:
        fx.close()


def test_status_needs_every_check_to_be_a_real_pass():
    ok = {"ok": True}
    assert probe._status([ok, ok]) == "verified"
    assert probe._status([ok, {"ok": None}]) == "verified_with_gaps"
    assert probe._status([{"ok": None}]) == "inconclusive"
    assert probe._status([ok, {"ok": False}]) == "failed"


def test_expectations_follow_the_grant():
    fx = probe.Fixture()
    try:
        ro = {d["name"]: d["expected"] for d in probe._check_defs(
            fx, workspace="/workspace", network=False, workspace_writable=False, inside_readable=True, host="h")}
        assert ro["write_inside"] == "denied" and ro["network_to_local_server"] == "denied"
        rw = {d["name"]: d["expected"] for d in probe._check_defs(
            fx, workspace="/workspace", network=True, workspace_writable=True, inside_readable=True, host="h")}
        assert rw["write_inside"] == "allowed" and rw["child_process_network"] == "allowed"
        none = {d["name"]: d["expected"] for d in probe._check_defs(
            fx, workspace="/workspace", network=False, workspace_writable=False, inside_readable=False, host="h")}
        assert none["read_inside"] == "denied"
    finally:
        fx.close()


def test_windows_coverage_is_stated_per_operation():
    for op in probe.OPERATIONS:
        assert probe.COVERAGE[op]["windows"] and probe.COVERAGE[op]["posix"]
    text = " ".join(probe.COVERAGE[op]["windows"] for op in probe.OPERATIONS)
    assert "powershell" in text and "required" in text and "Job Object" in text
    assert set(probe.coverage_for_host()) == set(probe.OPERATIONS)


def test_file_policy_probe_passes_on_the_real_policy():
    fx = probe.Fixture()
    try:
        result = probe._probe_files(fx)
    finally:
        fx.close()
    assert result["status"] in ("verified", "verified_with_gaps"), result
    assert result["container"] is False and result["enforcement"] == "host_path_policy"
    assert all(row["ok"] is not False for row in result["checks"])
    assert {"sibling_sharing_the_root_prefix", "outside_absolute_path"} <= {r["check"] for r in result["checks"]}


def test_file_policy_probe_reports_a_policy_that_allows_everything(monkeypatch):
    import src.tool_execution as te
    monkeypatch.setattr(te, "_resolve_tool_path_in_roots", lambda roots, raw, primary=None: raw)
    fx = probe.Fixture()
    try:
        result = probe._probe_files(fx)
    finally:
        fx.close()
    assert result["status"] == "failed"
    assert {r["check"] for r in result["checks"] if r["ok"] is False} >= {
        "outside_absolute_path", "sibling_sharing_the_root_prefix", "outside_relative_path"}


def test_unreachable_backend_is_unavailable_never_verified(monkeypatch):
    monkeypatch.setattr(DockerWorkspaceBackend, "probe", lambda self: {
        "ok": False, "reason": "backend_unavailable", "detail": "the docker daemon did not answer"})
    from src.code_mode import confined
    monkeypatch.setattr(confined, "probe", lambda image=None, docker="docker": {
        "ok": False, "reason": "backend_unavailable", "detail": "the docker daemon did not answer"})
    report = run(probe.run_sandbox_probe(["shell", "python", "code_mode", "files"]))
    assert report["operations"]["shell"]["status"] == "unavailable"
    assert "daemon did not answer" in report["operations"]["python"]["reason"]
    assert report["operations"]["code_mode"]["status"] == "unavailable"
    assert report["operations"]["files"]["status"].startswith("verified")
    assert report["ok"] is False and report["unavailable"] == ["code_mode", "python", "shell"]
    text = probe.render_text(report)
    assert "NOT all verified" in text and "Coverage on this host" in text


def test_unknown_operations_are_reported_not_ignored():
    report = run(probe.run_sandbox_probe(["files", "telepathy"]))
    assert report["unknown_operations"] == ["telepathy"] and list(report["operations"]) == ["files"]


def test_real_calls_route_is_reported_next_to_the_verification(monkeypatch):
    values = {"agent_sandbox_execution": False}
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))
    report = run(probe.run_sandbox_probe(["files"]))
    assert report["operations"]["files"]["route_for_real_calls"] == "host_path_policy"
    assert report["configured"]["sandbox"]["enabled"] is False


# ── routes and tool ────────────────────────────────────────────────────────

def test_routes_and_tool_expose_the_probe(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.execution_routes as sr
    monkeypatch.setattr(sr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(sr.setup_execution_routes())
    client = TestClient(app)
    policy = client.get("/api/sandbox").json()
    assert set(policy["coverage"]) == set(probe.OPERATIONS) and "sandbox" in policy
    body = client.post("/api/sandbox/probe", json={"operations": ["files"]}).json()
    assert body["operations"]["files"]["status"].startswith("verified") and "rendered" in body
    assert client.get("/api/process-handles").json()["count"] >= 0

    from src.agent_tools import TOOL_HANDLERS
    result = run(TOOL_HANDLERS["sandbox_probe"]({"operations": ["files"]}, {}))
    assert result["exit_code"] == 0 and "files: verified" in result["output"]


def test_the_execution_mcp_server_lists_the_tools():
    import importlib
    server = importlib.import_module("mcp_servers.execution_server")
    tools = run(server.list_tools())
    assert {t.name for t in tools} == {"process_start", "process_read", "process_write_stdin",
                                       "process_stop", "process_list", "sandbox_probe"}
    out = run(server.call_tool("sandbox_probe", {"operations": ["files"]}))
    assert "verified" in out[0].text


# ── real containers ────────────────────────────────────────────────────────

@pytest.fixture
def image(container_test_image):
    return container_test_image


def test_probe_verifies_a_real_container_backend(image):
    report = run(probe.run_sandbox_probe(image=image))
    failing = {op: [(c["check"], c["observed"]) for c in b["checks"] if c["ok"] is False]
               for op, b in report["operations"].items()}
    assert report["ok"], (report["summary"], failing)
    for op in ("shell", "python", "code_mode"):
        assert len(report["operations"][op]["checks"]) == 13
    assert report["operations"]["descendants"]["checks"][0]["evidence"].endswith("0")


def test_probe_detects_a_widened_mount(image, monkeypatch):
    """A backend that also mounts the host folder holding the decoy (at the same
    path) lets the forbidden read succeed: the probe must say so."""
    real = DockerWorkspaceBackend.docker_args

    windows = os.name == "nt"

    def widened(self, spec, name, **kw):
        args = real(self, spec, name, **kw)
        parent = os.path.dirname(spec.workspace)
        if windows:
            # A Windows host path is never a path inside the Linux container, so
            # the widening that matters there is the one next to /workspace.
            mount = []
            for sub in ("outside", "root2"):
                mount += ["--mount", f"type=bind,source={os.path.join(parent, sub)},target=/{sub},readonly"]
        else:
            mount = ["--mount", f"type=bind,source={parent},target={parent},readonly"]
        i = args.index(self.image)
        return args[:i] + mount + args[i:]

    monkeypatch.setattr(DockerWorkspaceBackend, "docker_args", widened)
    report = run(probe.run_sandbox_probe(["shell", "python"], image=image))
    expected_bad = ({"read_outside_relative", "read_sibling_relative"} if windows
                    else {"read_outside_absolute_host_path", "read_sibling_absolute_host_path"})
    for op in ("shell", "python"):
        body = report["operations"][op]
        assert body["status"] == "failed", body["status"]
        bad = {c["check"] for c in body["checks"] if c["ok"] is False}
        assert expected_bad <= bad, bad
    assert report["ok"] is False and report["failed"] == ["python", "shell"]


def test_probe_detects_an_open_network(image, monkeypatch):
    real = DockerWorkspaceBackend.docker_args

    def networked(self, spec, name, **kw):
        args = real(self, spec, name, **kw)
        args[args.index("--network") + 1] = "bridge"
        return args

    monkeypatch.setattr(DockerWorkspaceBackend, "docker_args", networked)
    report = run(probe.run_sandbox_probe(["shell"], image=image))
    bad = {c["check"] for c in report["operations"]["shell"]["checks"] if c["ok"] is False}
    assert {"network_to_local_server", "child_process_network"} <= bad


def test_probe_leaves_no_containers_or_files_behind(image):
    import tempfile
    tmp = tempfile.gettempdir()
    before = {n for n in os.listdir(tmp) if n.startswith("faustus_sbx_probe_")}
    run(probe.run_sandbox_probe(["shell", "code_mode", "descendants"], image=image))
    left = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"],
                          capture_output=True, text=True, timeout=30).stdout.split()
    assert not [n for n in left if n.startswith(("faustus-codemode-", "faustus-sbx-probe"))]
    assert {n for n in os.listdir(tmp) if n.startswith("faustus_sbx_probe_")} == before
