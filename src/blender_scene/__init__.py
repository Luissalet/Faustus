"""3D scenes described as typed JSON and rendered headless with Blender.

A scene is ``{"meta": {...}, "ops": [...]}``: a list of typed operations
(add a mesh, a material, a light, a camera, render, save, assert...) that a
fixed runner applies inside ``blender --background``. The document is
validated before Blender starts and there is no op that runs arbitrary code.
See ``docs/api/blender-scenes.md``.
"""
from .discovery import candidates, find_blender, probe
from .run import build_command, default_timeout, parse_report, run_scene
from .scene_common import ROOT_TOKEN, confine_path, engine_name, logical_engine, parse_version
from .smoke import reload_scene, smoke_scene
from .spec import OPS, scene_schema
from .summary import scene_summary
from .validate import Issue, ValidationResult, validate_scene

__all__ = [
    "OPS", "ROOT_TOKEN", "Issue", "ValidationResult", "build_command", "candidates", "confine_path",
    "default_timeout", "engine_name", "find_blender", "logical_engine", "parse_report", "parse_version",
    "probe", "reload_scene", "run_scene", "scene_schema", "scene_summary", "smoke_scene", "validate_scene",
]
