"""S1.3: `scripts/export_openapi.py` produces the real app's OpenAPI
document offline, without touching the real `data/` this repo runs from.

Subprocess, not an in-process import: `export_openapi.py`'s whole point is
`import app` — the same heavy, side-effecting module-level init
`tests/conftest.py` already isolates the rest of the suite from (see its own
comment on why `DATABASE_URL` defaults to sqlite in-memory for collection).
Importing it a second time, for real, inside this test process would leak
`app`'s module-level state (auth manager, session manager, background
task registrations) into every test that runs after this one in the same
session — exactly the failure mode `tests/test_route_factory_isolation.py`
exists to catch for routers. A subprocess keeps the two worlds apart, the
same reason `tests/test_auth_root_path.py::test_real_auth_middleware_uses_application_relative_path`
already runs its own `import app` probe as a subprocess.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from src import api_version

ROOT = Path(__file__).resolve().parents[1]


def test_export_openapi_writes_the_real_apps_document(tmp_path):
    out_path = tmp_path / "openapi.json"
    real_data_dir = ROOT / "data"
    before = set(real_data_dir.iterdir()) if real_data_dir.is_dir() else set()

    env = os.environ.copy()
    env.pop("FAUSTUS_DATA_DIR", None)
    env.pop("DATABASE_URL", None)

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "export_openapi.py"), "--out", str(out_path)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert out_path.is_file()

    spec = json.loads(out_path.read_text(encoding="utf-8"))
    assert spec["info"]["x-api-version"] == api_version.API_VERSION
    assert "/api/chat_stream" in spec["paths"]
    assert "/api/session" in spec["paths"]

    # No new file in the real data/ dir — the script pointed FAUSTUS_DATA_DIR
    # / DATABASE_URL at a throwaway temp location instead of the repo's own.
    after = set(real_data_dir.iterdir()) if real_data_dir.is_dir() else set()
    assert after == before, f"export_openapi.py wrote into the real data/ dir: {after - before}"


def test_export_openapi_reports_the_output_path_and_summary(tmp_path):
    out_path = tmp_path / "nested" / "openapi.json"
    env = os.environ.copy()
    env.pop("FAUSTUS_DATA_DIR", None)
    env.pop("DATABASE_URL", None)

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "export_openapi.py"), "--out", str(out_path)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert str(out_path) in result.stdout
    assert "x-api-version" in result.stdout
    assert out_path.is_file()  # --out creates missing parent dirs
