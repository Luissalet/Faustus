#!/usr/bin/env python3
"""HW-07 (LAB) — Spark + PC + eGPU reproducible benchmark.

Opt-in, standalone, outside the application: nothing in src/, routes/ or
services/hwfit/ imports this file, and this file imports nothing from the
running application except read-only helpers it explicitly reuses (see
below) — it never starts the FastAPI app, never touches a database, and
never registers anything persistent.

Two independent modes, run separately so a "capacity" estimate is never
mistaken for a measured number:

  --mode capacity   Pure arithmetic: given a model's size and each side's
                     VRAM (via src.vram_fit, reused not reimplemented), how
                     many layers would plausibly fit on each side. Marked
                     "measured": false in its own output — this is a
                     planning number, not something the script observed.

  --mode link       A REAL round-trip against an already SSH-paired host
                     (src.ssh_trust.is_paired / ssh_argv, reused not
                     reimplemented): latency (min/p50/p99 over N pings) and
                     a payload-transfer bandwidth figure. Marked
                     "measured": true — this really happened.

Requires --i-know-this-is-experimental on every invocation. See
docs/spec/v2/lab/HW-07_spark_pc_egpu.md for what this laboratory does and,
explicitly, does not claim (no RDMA, no cross-node memory pooling, no
transparent failover).

Usage:
    python3 scripts/lab/hw07_spark_pc_egpu_benchmark.py --i-know-this-is-experimental \\
        --mode capacity --model-size-gb 32 --local-vram-gb 12 --remote-vram-gb 24

    python3 scripts/lab/hw07_spark_pc_egpu_benchmark.py --i-know-this-is-experimental \\
        --mode link --host user@spark-box --pings 10 --payload-mb 64
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _capacity_mode(args: argparse.Namespace) -> dict:
    """Arithmetic only — see the module docstring. Reuses
    src.vram_fit.HEADROOM-style thinking (leave room for a KV cache) instead
    of assuming every VRAM byte is available for weights."""
    from src import vram_fit  # reused, not reimplemented

    model_gb = float(args.model_size_gb)
    local_gb = max(0.0, float(args.local_vram_gb) - 1.0)   # 1 GB headroom, same spirit as vram_admission's floor
    remote_gb = max(0.0, float(args.remote_vram_gb) - 1.0)
    total_gb = local_gb + remote_gb
    if total_gb <= 0:
        local_share = remote_share = 0.0
    else:
        local_share = round(model_gb * (local_gb / total_gb), 2)
        remote_share = round(model_gb * (remote_gb / total_gb), 2)
    fits_at_all = total_gb >= model_gb
    return {
        "mode": "capacity",
        "measured": False,
        "note": "arithmetic estimate only -- not a real load, see the design doc",
        "model_size_gb": model_gb,
        "local_usable_vram_gb": round(local_gb, 2),
        "remote_usable_vram_gb": round(remote_gb, 2),
        "estimated_local_share_gb": local_share,
        "estimated_remote_share_gb": remote_share,
        "fits_combined": fits_at_all,
        "kv_rates_known": len(getattr(vram_fit, "KV_RATES", {}) or {}),
    }


def _link_mode(args: argparse.Namespace) -> dict:
    """A real round-trip against an already-paired host. Refuses to run
    against a host ssh_trust does not already trust -- this script pairs
    nothing on its own."""
    from src import ssh_trust  # reused, not reimplemented
    import subprocess

    host = args.host
    ssh_port = args.ssh_port or None
    if not ssh_trust.is_paired(host, ssh_port):
        return {
            "mode": "link", "measured": False, "error":
                f"{host} is not SSH-paired yet -- pair it first (this script pairs nothing itself)",
        }

    latencies_ms = []
    for _ in range(max(1, int(args.pings))):
        argv = ssh_trust.ssh_argv(host, ssh_port, "true", connect_timeout=10)
        t0 = time.monotonic()
        try:
            r = subprocess.run(argv, capture_output=True, timeout=15)
            ok = r.returncode == 0
        except Exception:
            ok = False
        latencies_ms.append(((time.monotonic() - t0) * 1000.0) if ok else None)
    ok_latencies = sorted(l for l in latencies_ms if l is not None)

    bandwidth = None
    if ok_latencies and args.payload_mb > 0:
        payload_bytes = int(args.payload_mb * 1024 * 1024)
        remote_cmd = f"wc -c > /dev/null"
        argv = ssh_trust.ssh_argv(host, ssh_port, remote_cmd, connect_timeout=15)
        chunk = os.urandom(min(payload_bytes, 4 * 1024 * 1024))
        written = 0
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            while written < payload_bytes:
                n = min(len(chunk), payload_bytes - written)
                proc.stdin.write(chunk[:n])
                written += n
            proc.stdin.close()
            proc.wait(timeout=60)
            elapsed = max(1e-6, time.monotonic() - t0)
            bandwidth = {
                "payload_mb": round(written / (1024 * 1024), 2),
                "seconds": round(elapsed, 3),
                "mb_per_second": round((written / (1024 * 1024)) / elapsed, 2),
            }
        except Exception as e:  # noqa: BLE001 - a failed transfer is a result, not a crash
            bandwidth = {"error": str(e)[:200]}

    def _pct(values, p):
        if not values:
            return None
        idx = min(len(values) - 1, int(round(p * (len(values) - 1))))
        return round(values[idx], 2)

    return {
        "mode": "link",
        "measured": True,
        "host": host,
        "pings": len(latencies_ms),
        "pings_ok": len(ok_latencies),
        "latency_ms_min": _pct(ok_latencies, 0.0),
        "latency_ms_p50": _pct(ok_latencies, 0.5),
        "latency_ms_p99": _pct(ok_latencies, 0.99),
        "bandwidth": bandwidth,
        "note": "this is ssh/scp-over-the-existing-link bandwidth, not a claim about "
               "any dedicated interconnect (Thunderbolt/RDMA) -- see the design doc",
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--i-know-this-is-experimental", action="store_true", required=True,
                   help="required acknowledgement -- HW-07 is LAB, opt-in, not a supported feature")
    p.add_argument("--mode", choices=["capacity", "link"], required=True)
    p.add_argument("--model-size-gb", type=float, default=32.0)
    p.add_argument("--local-vram-gb", type=float, default=12.0)
    p.add_argument("--remote-vram-gb", type=float, default=24.0)
    p.add_argument("--host", default="")
    p.add_argument("--ssh-port", default="")
    p.add_argument("--pings", type=int, default=5)
    p.add_argument("--payload-mb", type=float, default=16.0)
    args = p.parse_args()

    if args.mode == "link" and not args.host:
        p.error("--mode link requires --host")

    result = _capacity_mode(args) if args.mode == "capacity" else _link_mode(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
