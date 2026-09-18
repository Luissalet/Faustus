"""tests for src/project_conventions.py — the shared convention-folder resolver."""

import os

from src.project_conventions import CONVENTION_DIRNAME, LEGACY_CONVENTION_DIRNAME, convention_dir


def test_empty_root_returns_empty_string():
    assert convention_dir("") == ""
    assert convention_dir("", create=True) == ""


def test_a_project_with_only_a_legacy_odysseus_folder_is_still_discovered(tmp_path):
    legacy = tmp_path / LEGACY_CONVENTION_DIRNAME
    legacy.mkdir()
    assert convention_dir(str(tmp_path)) == str(legacy)


def test_a_fresh_project_resolves_to_the_faustus_folder(tmp_path):
    # Neither folder exists yet — the fresh project's convention folder is
    # `.faustus/`, not `.odysseus/`.
    resolved = convention_dir(str(tmp_path))
    assert resolved == str(tmp_path / CONVENTION_DIRNAME)
    assert not os.path.isdir(resolved)  # convention_dir never creates it itself


def test_faustus_wins_when_both_folders_exist(tmp_path):
    (tmp_path / LEGACY_CONVENTION_DIRNAME).mkdir()
    faustus = tmp_path / CONVENTION_DIRNAME
    faustus.mkdir()
    assert convention_dir(str(tmp_path)) == str(faustus)


def test_create_always_resolves_to_faustus_even_with_a_legacy_folder(tmp_path):
    (tmp_path / LEGACY_CONVENTION_DIRNAME).mkdir()
    assert convention_dir(str(tmp_path), create=True) == str(tmp_path / CONVENTION_DIRNAME)
