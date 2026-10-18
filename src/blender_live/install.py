"""Install verified official release packages into the selected Faustus runtime."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

PACKAGES = {
    "mcp-1.0.3.zip": "a7a9da816192502e5a0a202a396444e266b47d8fc4f74ad4698048bd43040707",
    "blender-1.0.3.mcpb": "d6fe04dd17767f7c7fb452142f1f0e6787db575db49f3976a166142683b5b97e",
}
SOURCE = "https://projects.blender.org/lab/blender_mcp/releases/download/v1.0.3/"


def package(name, directory):
    path = directory / name
    if not path.is_file():
        directory.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(SOURCE + name, path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != PACKAGES[name]:
        raise ValueError("El paquete no coincide con la distribución oficial comprobada: " + str(path))
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--packages", required=True, type=Path)
    parser.add_argument("--blender", required=True)
    args = parser.parse_args()
    addon = package("mcp-1.0.3.zip", args.packages)
    server = package("blender-1.0.3.mcpb", args.packages)
    # A temporary build tree is consumed by pip; only the installed dependency
    # survives. No second source checkout, symlinks or linked dependencies.
    with tempfile.TemporaryDirectory(prefix="faustus-blender-install-") as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(server) as archive:
            for entry in archive.infolist():
                target = (root / entry.filename).resolve()
                if not target.is_relative_to(root.resolve()):
                    raise ValueError("Invalid package member")
            archive.extractall(root)
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-input", str(root)], check=True)
    source = Path(__file__).resolve().parents[2] / "blender_addon"
    subprocess.run([args.blender, "--background", "--python-exit-code", "3", "--python",
                    str(source / "install.py"), "--", str(source / "faustus_bridge.py"), str(addon)], check=True)


if __name__ == "__main__":
    main()
