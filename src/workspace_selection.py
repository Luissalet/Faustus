"""Select a task workspace without widening a project's execution root."""

import os


def select_project_workspace(requested: str, project_root: str, rejected: str = "") -> tuple[str, str]:
    """Choose between already-vetted paths; this function grants no access.

    An explicit invalid selection stays rejected instead of falling back to
    the larger project. Attached tool-access roots are intentionally absent.
    """
    if not project_root:
        return requested, rejected
    if rejected:
        return "", rejected
    if not requested:
        return project_root, ""
    try:
        root = os.path.normcase(os.path.realpath(project_root))
        candidate = os.path.normcase(os.path.realpath(requested))
        if os.path.commonpath((root, candidate)) == root:
            return requested, ""
    except (OSError, ValueError):
        pass
    return "", requested
