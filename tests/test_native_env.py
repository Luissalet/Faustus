"""A foreign child must not inherit the marks of the host's virtualenv.

Faustus runs inside a venv, so its environment names that venv in VIRTUAL_ENV
and puts its bin/ first on PATH. A child that inherits it resolves `python`,
`pip` and its imports against our interpreter and our site-packages instead of
its own — the failure that works on the developer's machine and imports the
wrong package on the user's.
"""
import os
import sys

import pytest

from src import native_env
from src.native_env import (
    VENV_MARKERS,
    detected_venv_roots,
    is_venv_path,
    native_host_environment,
)

SEP = os.pathsep


def joined(*parts):
    return SEP.join(parts)


# ── the markers go ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("marker", VENV_MARKERS)
def test_every_venv_marker_is_dropped(marker):
    out = native_host_environment({marker: "whatever", "HOME": "/home/u"})
    assert marker not in out
    assert out["HOME"] == "/home/u"


def test_unrelated_variables_are_kept_verbatim():
    base = {"HOME": "/home/u", "LANG": "en_GB.UTF-8", "MY_TOKEN": "abc", "EMPTY": ""}
    assert native_host_environment(base) == base


def test_absent_markers_are_not_invented():
    out = native_host_environment({"HOME": "/home/u"})
    assert set(out) == {"HOME"}


# ── PATH loses the venv, keeps everything else ─────────────────────────────

def test_venv_bin_is_removed_from_path():
    base = {"VIRTUAL_ENV": "/srv/app/venv",
            "PATH": joined("/srv/app/venv/bin", "/usr/local/bin", "/usr/bin")}
    assert native_host_environment(base)["PATH"] == joined("/usr/local/bin", "/usr/bin")


def test_path_order_and_separator_are_preserved():
    base = {"VIRTUAL_ENV": "/srv/app/venv",
            "PATH": joined("/a", "/srv/app/venv/bin", "/b", "/c")}
    assert native_host_environment(base)["PATH"] == joined("/a", "/b", "/c")


def test_conda_prefix_entries_are_removed_too():
    base = {"CONDA_PREFIX": "/opt/conda/envs/x",
            "PATH": joined("/opt/conda/envs/x/bin", "/usr/bin")}
    assert native_host_environment(base)["PATH"] == "/usr/bin"


def test_nested_venv_subdirectory_is_removed():
    base = {"VIRTUAL_ENV": "/srv/app/venv",
            "PATH": joined("/srv/app/venv/lib/node_modules/.bin", "/usr/bin")}
    assert native_host_environment(base)["PATH"] == "/usr/bin"


def test_a_sibling_that_only_shares_a_prefix_is_kept():
    base = {"VIRTUAL_ENV": "/srv/app/venv",
            "PATH": joined("/srv/app/venv-tools/bin", "/usr/bin")}
    assert native_host_environment(base)["PATH"] == joined("/srv/app/venv-tools/bin", "/usr/bin")


def test_a_path_of_only_venv_entries_is_left_alone():
    # A child with no PATH cannot exec anything: a leaked venv beats that.
    base = {"VIRTUAL_ENV": "/srv/app/venv", "PATH": "/srv/app/venv/bin"}
    assert native_host_environment(base)["PATH"] == "/srv/app/venv/bin"


def test_environment_without_path_is_handled():
    assert "PATH" not in native_host_environment({"VIRTUAL_ENV": "/srv/app/venv"})


def test_empty_path_is_left_empty():
    assert native_host_environment({"VIRTUAL_ENV": "/v", "PATH": ""})["PATH"] == ""


@pytest.mark.skipif(os.name != "nt", reason="only Windows compares paths case-insensitively")
def test_windows_matches_paths_case_insensitively():
    base = {"VIRTUAL_ENV": r"C:\Proj\venv",
            "PATH": joined(r"c:\proj\VENV\Scripts", r"C:\Windows\System32")}
    assert native_host_environment(base)["PATH"] == r"C:\Windows\System32"


@pytest.mark.skipif(os.name == "nt", reason="POSIX compares paths case-sensitively")
def test_posix_matches_paths_case_sensitively():
    base = {"VIRTUAL_ENV": "/srv/app/venv", "PATH": joined("/srv/app/VENV/bin", "/usr/bin")}
    assert native_host_environment(base)["PATH"] == joined("/srv/app/VENV/bin", "/usr/bin")


# ── extra, purity, defaults ────────────────────────────────────────────────

def test_extra_is_layered_on_top():
    out = native_host_environment({"HOME": "/home/u"}, extra={"GIT_TERMINAL_PROMPT": "0"})
    assert out["GIT_TERMINAL_PROMPT"] == "0" and out["HOME"] == "/home/u"


def test_extra_overrides_a_base_value():
    out = native_host_environment({"LANG": "C"}, extra={"LANG": "C.UTF-8"})
    assert out["LANG"] == "C.UTF-8"


def test_extra_is_never_filtered():
    # A caller that names a marker explicitly means it.
    out = native_host_environment({"PYTHONPATH": "/srv/app/venv/lib"},
                                  extra={"PYTHONPATH": "/user/project"})
    assert out["PYTHONPATH"] == "/user/project"


def test_the_input_mapping_is_not_mutated():
    base = {"VIRTUAL_ENV": "/v", "PATH": joined("/v/bin", "/usr/bin")}
    snapshot = dict(base)
    native_host_environment(base, extra={"X": "1"})
    assert base == snapshot


def test_the_result_is_a_plain_mutable_dict():
    out = native_host_environment({"HOME": "/home/u"})
    out["ADDED"] = "1"
    assert isinstance(out, dict) and out["ADDED"] == "1"


def test_default_base_is_the_process_environment(monkeypatch):
    monkeypatch.setenv("FAUSTUS_NATIVE_ENV_PROBE", "present")
    monkeypatch.setenv("VIRTUAL_ENV", "/srv/app/venv")
    out = native_host_environment()
    assert out["FAUSTUS_NATIVE_ENV_PROBE"] == "present"
    assert "VIRTUAL_ENV" not in out


def test_default_base_strips_the_live_venv_from_path(monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/srv/app/venv")
    monkeypatch.setenv("PATH", joined("/srv/app/venv/bin", "/usr/bin"))
    assert native_host_environment()["PATH"] == "/usr/bin"


# ── the process-level predicates ───────────────────────────────────────────

def test_detected_roots_include_sys_prefix_when_it_is_a_venv(monkeypatch):
    monkeypatch.setattr(sys, "prefix", "/srv/app/venv", raising=False)
    monkeypatch.setattr(sys, "base_prefix", "/usr", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    assert detected_venv_roots() == (native_env._norm("/srv/app/venv"),)


def test_no_roots_are_detected_outside_a_venv(monkeypatch):
    monkeypatch.setattr(sys, "prefix", "/usr", raising=False)
    monkeypatch.setattr(sys, "base_prefix", "/usr", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    assert detected_venv_roots() == ()


def test_is_venv_path_follows_the_detected_root(monkeypatch):
    monkeypatch.setattr(sys, "prefix", "/srv/app/venv", raising=False)
    monkeypatch.setattr(sys, "base_prefix", "/usr", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    assert is_venv_path("/srv/app/venv/bin")
    assert is_venv_path("/srv/app/venv")
    assert not is_venv_path("/usr/bin")
    assert not is_venv_path("/srv/app/venv-tools/bin")
    assert not is_venv_path("")


def test_sys_prefix_venv_is_stripped_even_with_no_marker_variables(monkeypatch):
    # An unactivated venv sets no VIRTUAL_ENV; sys.prefix is the only signal.
    monkeypatch.setattr(sys, "prefix", "/srv/app/venv", raising=False)
    monkeypatch.setattr(sys, "base_prefix", "/usr", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    base = {"PATH": joined("/srv/app/venv/bin", "/usr/bin")}
    assert native_host_environment(base)["PATH"] == "/usr/bin"
