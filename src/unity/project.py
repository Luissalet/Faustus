"""Inspect engine metadata without launching an editor, installing or fetching packages."""
import json
import re
from pathlib import Path


def profile(project):
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Project directory not found")
    engines = []
    unity = root / "ProjectSettings/ProjectVersion.txt"
    if unity.is_file():
        match = re.search(r"^m_EditorVersion:\s*(.+)$", unity.read_text(encoding="utf-8-sig"), re.M)
        manifest = root / "Packages/manifest.json"
        packages = json.loads(manifest.read_text(encoding="utf-8-sig")).get("dependencies", {}) if manifest.is_file() else {}
        engines.append({"engine": "unity", "version": match.group(1).strip() if match else None,
                        "version_source": str(unity), "packages": packages, "reference": "references/unity.md"})
    godot = root / "project.godot"
    if godot.is_file():
        text = godot.read_text(encoding="utf-8-sig")
        feature = re.search(r'^config/features\s*=\s*PackedStringArray\((.+)\)', text, re.M)
        features = re.findall(r'"([^\"]+)"', feature.group(1)) if feature else []
        engines.append({"engine": "godot", "version": None, "required_features": features,
                        "version_source": "project.godot features are requirements; query the installed editor for its exact version",
                        "reference": "references/godot.md"})
    for descriptor in sorted(root.glob("*.uproject")):
        value = json.loads(descriptor.read_text(encoding="utf-8-sig"))
        engines.append({"engine": "unreal", "version": value.get("EngineAssociation"), "version_source": str(descriptor),
                        "plugins": value.get("Plugins", []), "reference": "references/unreal.md"})
    manifest = root / "package.json"
    if manifest.is_file():
        package = json.loads(manifest.read_text(encoding="utf-8-sig"))
        deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
        wanted = {key: value for key, value in deps.items() if key in {
            "three", "@react-three/fiber", "@react-three/drei", "@react-three/rapier",
            "@react-three/postprocessing", "three.quarks", "yuka", "colyseus", "react"}}
        if wanted:
            locked = {}
            lockfile = root / "package-lock.json"
            if lockfile.is_file():
                lock = json.loads(lockfile.read_text(encoding="utf-8-sig"))
                for key in wanted:
                    item = lock.get("packages", {}).get("node_modules/" + key, lock.get("dependencies", {}).get(key, {}))
                    if item.get("version"):
                        locked[key] = item["version"]
            engines.append({"engine": "threejs", "version": locked.get("three"), "declared_packages": wanted,
                            "locked_packages": locked, "version_source": str(lockfile) if lockfile.is_file() else None,
                            "reference": "references/threejs.md"})
    return {"ok": True, "project": str(root), "engines": engines,
            "skill": "gamedev", "note": "Declared ranges and engine associations do not prove installed versions."}
