#!/usr/bin/env python
"""Refresh the vendored modern web guidance (skills/guidance/modern-web-guidance).

    python scripts/update_web_guidance.py                  # latest published package
    python scripts/update_web_guidance.py --version 1.0.1  # a given one
    python scripts/update_web_guidance.py --tarball f.tgz  # from a file already downloaded (offline)
    python scripts/update_web_guidance.py --check          # say whether a newer one exists; change nothing

The publisher ships the guides as an npm package. This reads the package's tarball (no npm, no npx), keeps
only the guide Markdown, the publisher's search index, the licence and the skill version, rebuilds
`index.json` and `SOURCE.json`, and swaps the tree in one step. At turn time nothing reaches the network.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import web_guidance  # noqa: E402

SKILL_INDEX = ROOT / "skills" / "library" / "modern-web-guidance" / "references" / "guide-index.md"
PACKAGE = "modern-web-guidance"
REGISTRY = "https://registry.npmjs.org/" + PACKAGE
REPOSITORY = "https://github.com/GoogleChrome/modern-web-guidance"
PREFIX = "package/skills/modern-web-guidance/"
MAX_TARBALL = 30_000_000
_GUIDE_RE = re.compile(r"^" + re.escape(PREFIX) + r"guides/([a-z0-9-]+)/([a-z0-9-]+)\.md$")
_SKILL_VERSION_RE = re.compile(r"--skill-version\s+([0-9A-Za-z_.-]+)")


def _get(url: str, limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "faustus-web-guidance-update"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https registry URL
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise SystemExit(f"{url}: larger than {limit} bytes")
    return data


def latest_version() -> str:
    return json.loads(_get(REGISTRY + "/latest", 1_000_000))["version"]


def fetch_tarball(version: str) -> tuple[bytes, str]:
    meta = json.loads(_get(f"{REGISTRY}/{version}", 1_000_000))
    url = meta["dist"]["tarball"]
    if not url.startswith("https://registry.npmjs.org/"):
        raise SystemExit(f"unexpected tarball location {url}")
    return _get(url, MAX_TARBALL), meta["version"]


def unpack(data: bytes) -> dict:
    """The parts of the tarball worth keeping, by name; anything else in it is ignored."""
    keep = {"guides": {}, "index": None, "license": None, "skill": ""}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile() or member.size > 2_000_000:
                continue
            name = member.name
            body = tar.extractfile(member).read()
            m = _GUIDE_RE.match(name)
            if m:
                keep["guides"][f"{m.group(1)}/{m.group(2)}.md"] = body
            elif name == PREFIX + "gen/guides.json":
                keep["index"] = json.loads(body.decode("utf-8"))
            elif name == "package/LICENSE":
                keep["license"] = body
            elif name == "package/package.json":
                keep["version"] = str(json.loads(body.decode("utf-8")).get("version") or "")
            elif name == PREFIX + "SKILL.md":
                keep["skill"] = body.decode("utf-8")
    if not keep["guides"] or keep["index"] is None or keep["license"] is None:
        raise SystemExit("the tarball is missing guides, the publisher's index or the licence; nothing written")
    return keep


def write_tree(dest: Path, keep: dict, version: str, tarball_sha256: str) -> dict:
    """Build the new tree next to `dest`, then replace `dest` with it."""
    staging = Path(tempfile.mkdtemp(prefix=".web-guidance-", dir=dest.parent))
    try:
        for rel, body in sorted(keep["guides"].items()):
            target = staging / "guides" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body.replace(b"\r\n", b"\n"))
        (staging / "LICENSE").write_bytes(keep["license"])
        index = web_guidance.build_index(staging / "guides", keep["index"])
        (staging / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        found = _SKILL_VERSION_RE.search(keep["skill"])
        source = {
            "package": PACKAGE, "version": version, "skill_version": found.group(1) if found else None,
            "repository": REPOSITORY, "license": "Apache-2.0", "guides": len(index),
            "tarball_sha256": tarball_sha256,
        }
        (staging / "SOURCE.json").write_text(json.dumps(source, indent=1) + "\n", encoding="utf-8")
        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)
        # The library skill carries the id list for hosts that cannot call the tool.
        SKILL_INDEX.parent.mkdir(parents=True, exist_ok=True)
        SKILL_INDEX.write_text(web_guidance.guide_index_markdown(index), encoding="utf-8")
        return source
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", default="latest")
    ap.add_argument("--tarball", help="a package tarball already on disk")
    ap.add_argument("--check", action="store_true", help="report whether a newer version exists; write nothing")
    args = ap.parse_args(argv)
    dest = Path(web_guidance.GUIDANCE_DIR)
    current = web_guidance.source().get("version")
    if args.check:
        newest = latest_version()
        print(f"vendored {current or 'none'}; published {newest}" + ("" if newest == current else "  -> update available"))
        return 0
    if args.tarball:
        data = Path(args.tarball).read_bytes()
        keep = unpack(data)
        version = keep.get("version") or args.version
    else:
        version = latest_version() if args.version == "latest" else args.version
        data, version = fetch_tarball(version)
        keep = unpack(data)
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = write_tree(dest, keep, version, hashlib.sha256(data).hexdigest())
    print(f"vendored {PACKAGE} {source['version']} (skill {source['skill_version']}): {source['guides']} guides -> {dest}"
          + (f"  [was {current}]" if current else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
