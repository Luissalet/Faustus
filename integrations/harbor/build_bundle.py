"""Build the two parts of the Linux agent bundle the Harbor adapter installs.

    python integrations/harbor/build_bundle.py src  --out C:/hb/faustus
    python integrations/harbor/build_bundle.py venv --out C:/hb/faustus   (needs Docker)
    python integrations/harbor/build_bundle.py all  --out C:/hb/faustus

``src`` packs the files git tracks in this checkout, and the new ones it does
not ignore, as they are on disk now (uncommitted edits included, deleted files
left out), without the desktop
shell, the web front-end sources or test data, plus ``.bundle-version`` with
the commit and a hash of the packed bytes. ``venv`` builds, in an Ubuntu 20.04
container, a relocatable CPython 3.12 with ``requirements.txt`` installed under
``/opt/faustus`` and copies the tarball out.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP_PREFIXES = ("desktop/", "studio/", "website/", "swift/", "node_modules/", "tests/", "data/", "logs/", "venv/",
                 "_wt/", "_scratch/", "backups/")
DOCKERFILE = """FROM ubuntu:20.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
ENV UV_PYTHON_INSTALL_DIR=/opt/faustus/py UV_LINK_MODE=copy
RUN uv python install 3.12 && uv venv --python 3.12 /opt/faustus/venv
COPY requirements.txt /tmp/requirements.txt
RUN uv pip install --python /opt/faustus/venv/bin/python -r /tmp/requirements.txt
RUN cd /opt && tar -czf /faustus-venv.tgz faustus
"""


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, check=True).stdout
    return sorted({p for p in out.decode("utf-8").split("\0") if p})


def build_src(out_dir: Path) -> Path:
    files = [p for p in _tracked() if not p.startswith(SKIP_PREFIXES) and (ROOT / p).is_file()]
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    digest = hashlib.sha256()
    target = out_dir / "faustus-src.tgz"
    tmp = target.with_suffix(".tmp")
    with tarfile.open(tmp, "w:gz", compresslevel=6) as tar:
        for rel in sorted(files):
            data = (ROOT / rel).read_bytes()
            digest.update(rel.encode() + b"\0" + data)
            info = tarfile.TarInfo(rel)
            info.size, info.mode, info.mtime = len(data), 0o755 if rel.endswith(".sh") else 0o644, int(time.time())
            tar.addfile(info, io.BytesIO(data))
        version = f"{head}+{digest.hexdigest()[:12]}\n".encode()
        info = tarfile.TarInfo(".bundle-version")
        info.size, info.mode = len(version), 0o644
        tar.addfile(info, io.BytesIO(version))
    tmp.replace(target)
    print(f"src: {len(files)} files, version {version.decode().strip()} -> {target}")
    return target


def build_venv(out_dir: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix="faustus-venv-") as tmp:
        ctx = Path(tmp)
        (ctx / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
        (ctx / "requirements.txt").write_bytes((ROOT / "requirements.txt").read_bytes())
        subprocess.run(["docker", "build", "-t", "faustus-agent-venv", str(ctx)], check=True)
    name = "faustus-agent-venv-copy"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "create", "--name", name, "faustus-agent-venv"], check=True, capture_output=True)
    try:
        target = out_dir / "faustus-venv.tgz"
        subprocess.run(["docker", "cp", f"{name}:/faustus-venv.tgz", str(target)], check=True)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    print(f"venv -> {target} ({target.stat().st_size // 1_000_000} MB)")
    return target


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("part", choices=("src", "venv", "all"))
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "bundle"))
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.part in ("venv", "all"):
        build_venv(out)
    if args.part in ("src", "all"):
        build_src(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
