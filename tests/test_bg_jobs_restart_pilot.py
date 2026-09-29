"""A detached synthetic CLI survives its launching interpreter on Windows."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from src import bg_jobs


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell process pilot")
@pytest.mark.parametrize("shell_name", ["pwsh", "powershell"])
def test_detached_cli_result_survives_launcher_exit(tmp_path, monkeypatch, shell_name):
    shell = shutil.which(shell_name)
    if not shell:
        pytest.skip(f"{shell_name} unavailable")
    jobs = tmp_path / "jobs"
    store = tmp_path / "jobs.json"
    launcher = '''
import json, sys
from pathlib import Path
from src import bg_jobs
bg_jobs._JOBS_DIR = Path(sys.argv[1])
bg_jobs._STORE = Path(sys.argv[2])
bg_jobs.find_powershell = lambda: sys.argv[3]
bg_jobs._is_paused_for_resource_pressure = lambda: False
bg_jobs._max_concurrent_jobs = lambda: 1
record = bg_jobs.launch("Start-Sleep -Milliseconds 600; Write-Output 'pilot complete'; exit 0",
                        "restart-pilot", shell="powershell", max_runtime_s=15)
print(json.dumps({"id": record["id"]}))
'''
    launched = subprocess.run(
        [sys.executable, "-c", launcher, str(jobs), str(store), shell],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert launched.returncode == 0, launched.stderr
    job_id = json.loads(launched.stdout)["id"]
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs)
    monkeypatch.setattr(bg_jobs, "_STORE", store)
    monkeypatch.setattr(bg_jobs, "_is_paused_for_resource_pressure", lambda: False)
    deadline = time.monotonic() + 15
    record = bg_jobs.get(job_id)
    while record["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
        record = bg_jobs.get(job_id)
    assert record["status"] == "done", record
    assert record["exit_code"] == 0
    assert "pilot complete" in bg_jobs.result_text(record)
    assert any(r["id"] == job_id for r in bg_jobs.pending_followups())
    bg_jobs.mark_followed_up(job_id)
    assert all(r["id"] != job_id for r in bg_jobs.pending_followups())
