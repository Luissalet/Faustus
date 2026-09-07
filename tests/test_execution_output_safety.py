import sys

import pytest

from src.contracts import ExecutionSpec
from src.execution_backends import DockerWorkspaceBackend, LocalAttendedBackend, OUTPUT_TAIL_BYTES


@pytest.mark.parametrize("value", ["line1\nINJECTED=x", "line1\rline2", "x\x00y", None])
def test_secret_cannot_add_an_undeclared_environment_line(value):
    with pytest.raises(ValueError, match="single-line"):
        DockerWorkspaceBackend._write_env_file({"DECLARED": value})


@pytest.mark.parametrize("name", ["BAD=KEY", "\nINJECTED", "1INVALID"])
def test_invalid_environment_name_is_rejected_before_creating_file(name):
    with pytest.raises(ValueError, match="identifiers"):
        DockerWorkspaceBackend._write_env_file({name: "fixture"})


def test_local_backend_keeps_only_bounded_output(tmp_path):
    spec = ExecutionSpec.parse({"backend": "local", "isolation": "none", "attended_ack": True,
                                "workspace": str(tmp_path), "limits": {"seconds": 10}})
    result = LocalAttendedBackend().run(spec, [sys.executable, "-c",
        "import sys; sys.stdout.write('x'*2000000+'END'); sys.stderr.write('y'*2000000+'ERR')"],
        run_id="bounded-fixture")
    assert result.status == "completed"
    assert result.output_truncated
    assert len(result.stdout_tail) <= OUTPUT_TAIL_BYTES and len(result.stderr_tail) <= OUTPUT_TAIL_BYTES
    assert result.stdout_tail.endswith("END") and result.stderr_tail.endswith("ERR")
