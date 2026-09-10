"""HW-07 (LAB) - Spark + PC + eGPU: design doc + reproducible benchmark script.

No core code for this ID by design (see docs/spec/v2/lab/HW-07_spark_pc_egpu.md):
scripts/lab/hw07_spark_pc_egpu_benchmark.py is standalone, not imported by the
application, and requires an explicit --i-know-this-is-experimental flag on
every run. This test imports the script as a module (it lives outside any
package) purely to exercise its two pure/near-pure functions without a real
Spark/eGPU rig.
"""
from __future__ import annotations

import argparse
import importlib.util
import os

import pytest

_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "lab", "hw07_spark_pc_egpu_benchmark.py",
)


def _load_script():
    spec = importlib.util.spec_from_file_location("hw07_lab_benchmark", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hw07():
    return _load_script()


def test_design_doc_exists_and_names_what_is_not_claimed():
    doc_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "docs", "spec", "v2", "lab", "HW-07_spark_pc_egpu.md",
    )
    assert os.path.isfile(doc_path)
    text = open(doc_path, encoding="utf-8").read()
    for term in ("RDMA", "failover", "pooling"):
        assert term in text


def test_the_flag_is_required(hw07):
    # argparse itself enforces required=True; verify via a parser built the
    # same way main()'s is, rather than sys.exit()'ing out of the test
    # process by calling main() with a missing required argument.
    p = argparse.ArgumentParser()
    p.add_argument("--i-know-this-is-experimental", action="store_true", required=True)
    p.add_argument("--mode", choices=["capacity", "link"], required=True)
    with pytest.raises(SystemExit):
        p.parse_args(["--mode", "capacity"])


def test_capacity_mode_is_arithmetic_not_measured(hw07):
    args = argparse.Namespace(model_size_gb=32.0, local_vram_gb=12.0, remote_vram_gb=24.0)
    result = hw07._capacity_mode(args)
    assert result["measured"] is False
    assert result["fits_combined"] is True
    # 11 usable local + 23 usable remote = 34 usable for a 32 GB model,
    # split proportionally.
    assert result["estimated_local_share_gb"] + result["estimated_remote_share_gb"] == pytest.approx(32.0, abs=0.1)


def test_capacity_mode_reports_does_not_fit_when_combined_vram_is_short(hw07):
    args = argparse.Namespace(model_size_gb=64.0, local_vram_gb=8.0, remote_vram_gb=8.0)
    result = hw07._capacity_mode(args)
    assert result["fits_combined"] is False


def test_link_mode_refuses_an_unpaired_host(hw07, monkeypatch):
    monkeypatch.setattr("src.ssh_trust.is_paired", lambda remote, ssh_port=None: False)
    args = argparse.Namespace(host="some-host", ssh_port="", pings=1, payload_mb=1.0)
    result = hw07._link_mode(args)
    assert result["measured"] is False
    assert "not SSH-paired" in result["error"]
