"""A control-plane refusal must not leave a credential file on disk."""
from pathlib import Path

import pytest

from src.contracts import ExecutionSpec
from src.execution_backends import DockerWorkspaceBackend


def test_a_rejected_start_callback_removes_the_temporary_secret_file(tmp_path, monkeypatch):
    backend = DockerWorkspaceBackend()
    monkeypatch.setattr(backend, "preflight", lambda spec: {"ok": True})
    original = backend._write_env_file
    paths = []

    def write(values):
        path = original(values)
        paths.append(path)
        return path

    monkeypatch.setattr(backend, "_write_env_file", write)
    spec = ExecutionSpec.parse({"backend": "docker_workspace", "isolation": "container",
                                "workspace": str(tmp_path), "secret_names": ["QA_SECRET"]})

    def event(kind, payload):
        raise RuntimeError("workflow cancelled before start")

    try:
        with pytest.raises(RuntimeError, match="cancelled before start"):
            backend.run(spec, ["python", "--version"], run_id="cancel-before-start",
                        secrets={"QA_SECRET": "synthetic-test-only"}, on_event=event)
        assert paths and not Path(paths[0]).exists()
    finally:
        # Clean the synthetic fixture even when reproducing the pre-fix leak.
        for path in paths:
            Path(path).unlink(missing_ok=True)
