"""«Hoard Hub» is named by its name, not by the family word every app shares.

With the Hub's manifest among the plugins, «Arranca Jobhunter's Hoard» named
the Hub too: the first word of «Hoard Hub» is «hoard».
"""
from types import SimpleNamespace

from src import plugins


def _names(name, pid):
    return plugins.names_for(SimpleNamespace(name=name, id=pid))


def test_the_family_word_alone_does_not_name_the_hub():
    names = _names("Hoard Hub", "hoardhub")
    assert "hoard" not in names
    assert "hoard hub" in names and "hoardhub" in names


def test_other_apps_keep_their_first_word():
    assert "disk" in _names("Disk Hoard", "diskhoard")
    assert "jobhunter" in _names("Jobhunter's Hoard", "jobhunter")
