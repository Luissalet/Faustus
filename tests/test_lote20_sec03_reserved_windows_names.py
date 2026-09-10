"""L20 (integrates L16, SEC-03): Windows reserved device names (CON, PRN,
AUX, NUL, COM1-9, LPT1-9) were not rejected by `src.tool_execution`'s path
resolvers. On Windows these name a device rather than a regular file or
directory for ANY path component, with or without an extension — a
model-controlled `write_file`/`read_file` path that hits one is not the
"just another file" the resolver's allowlist/deny-list logic assumes.

Mirrors tests/test_tool_path_confinement.py's style (unit tests on
`_is_sensitive_path` plus the public resolver), which is not owned by this
lote and is left untouched.
"""
import os

import pytest


def test_is_sensitive_path_flags_bare_reserved_names():
    from src.tool_execution import _is_sensitive_path
    assert _is_sensitive_path("/tmp/CON")
    assert _is_sensitive_path("/tmp/PRN")
    assert _is_sensitive_path("/tmp/AUX")
    assert _is_sensitive_path("/tmp/NUL")


def test_is_sensitive_path_flags_com_and_lpt_ports():
    from src.tool_execution import _is_sensitive_path
    for n in range(1, 10):
        assert _is_sensitive_path(f"/tmp/COM{n}")
        assert _is_sensitive_path(f"/tmp/LPT{n}")
    # COM0/LPT0/COM10 are not reserved devices.
    assert not _is_sensitive_path("/tmp/COM0")
    assert not _is_sensitive_path("/tmp/LPT0")
    assert not _is_sensitive_path("/tmp/COM10")


def test_is_sensitive_path_flags_reserved_name_with_extension():
    """Windows treats CON.txt as the CON device too — the extension does
    not make it a regular file."""
    from src.tool_execution import _is_sensitive_path
    assert _is_sensitive_path("/tmp/CON.txt")
    assert _is_sensitive_path(os.path.join("logs", "nul.log"))


def test_is_sensitive_path_flags_reserved_name_case_insensitively():
    from src.tool_execution import _is_sensitive_path
    assert _is_sensitive_path("/tmp/con")
    assert _is_sensitive_path("/tmp/Con")
    assert _is_sensitive_path("/tmp/CoM3")


def test_is_sensitive_path_flags_reserved_name_in_a_middle_component():
    """CON as a DIRECTORY component, not just the final segment: Windows
    refuses the whole device name at any depth."""
    from src.tool_execution import _is_sensitive_path
    assert _is_sensitive_path(os.path.join("workspace", "CON", "out.log"))


def test_is_sensitive_path_does_not_flag_ordinary_names_that_merely_contain_one():
    """'console.log', 'company', 'nullable.py': a reserved name is a whole
    path COMPONENT stem, never a substring — no false positives on ordinary
    files that happen to start with the same letters."""
    from src.tool_execution import _is_sensitive_path
    assert not _is_sensitive_path("/tmp/console.log")
    assert not _is_sensitive_path("/tmp/company")
    assert not _is_sensitive_path("/tmp/nullable.py")
    assert not _is_sensitive_path("/tmp/lpt.py")  # no trailing digit: not a port name


def test_resolve_tool_path_rejects_reserved_name_under_an_allowed_root():
    """/tmp is an allowed root, but the reserved-name deny list fires first
    (same ordering as .ssh/.env), so containment never gets a chance to
    approve it."""
    from src.tool_execution import _resolve_tool_path
    with pytest.raises(ValueError, match="sensitive directory"):
        _resolve_tool_path("/tmp/CON")
    with pytest.raises(ValueError, match="sensitive directory"):
        _resolve_tool_path("/tmp/com3.dat")
