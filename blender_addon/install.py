"""Install the shipped add-on through Blender's own installer, preserving preferences."""
from pathlib import Path
import sys
import bpy

source = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
official = Path(sys.argv[sys.argv.index("--") + 2]).resolve()
if source.name != "faustus_bridge.py" or not source.is_file():
    raise ValueError("Expected the shipped Faustus add-on file")
if bpy.app.version < (5, 1, 0):
    raise RuntimeError("The official MCP add-on requires Blender 5.1 or later")
# Install through Blender's own extension manager, into its user repository.
# All other repositories, extensions and user preferences remain in place.
bpy.ops.extensions.package_install_files(filepath=str(official), repo="user_default", enable_on_install=True)
bpy.ops.preferences.addon_install(overwrite=True, filepath=str(source))
bpy.ops.preferences.addon_enable(module="faustus_bridge")
bpy.ops.wm.save_userpref()
print("FAUSTUS_BLENDER_INSTALLED", bpy.app.version_string, bpy.utils.user_resource("SCRIPTS", path="addons"))
