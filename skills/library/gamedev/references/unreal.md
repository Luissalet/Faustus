# Unreal

Read EngineAssociation, enabled plugins, modules and Build.cs before choosing
Blueprint, C++ or Python editor APIs. An association can be a GUID pointing to
a custom build, so resolve the installed engine instead of assuming a release.

Edit assets through the installed editor APIs and retain package/object paths.
UObject properties need Unreal reflection declarations when they must be exposed
to the editor or saved. Runtime actors and editor-only utilities belong in their
corresponding modules; editor dependencies in a runtime module break packaging.

Compile the relevant target and validate the changed asset in the editor. Check
PIE behavior and a packaged run when changing startup, asset loading or networking.
Hot reload success alone does not show a clean build works. No native Unreal
live bridge is implied by Faustus's project metadata reader.
