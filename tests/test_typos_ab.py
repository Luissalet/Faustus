import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("typos_ab", Path(__file__).resolve().parents[1] / "scripts" / "typos_ab.py")
typos_ab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(typos_ab)

KNOWN = {"los", "vecinos", "desarrollaron", "una", "idea", "hola", "bien", "mediodía", "sol", "luz", "fotovoltaica"}


def known(word):
    return word in KNOWN


def test_names_code_urls_and_short_words_are_not_checked():
    text = "Los vecinos de Valladolid desarollaron una idea. Hola, `enteriormente` y https://ex.am/ple bien."
    words = typos_ab.words_to_check(text)
    assert "valladolid" not in words          # a name mid-sentence
    assert "los" in words and "hola" in words  # sentence openers are checked
    assert "enteriormente" not in words and "ple" not in words
    assert "de" not in words


def test_unknown_words_counts_invented_forms_only():
    n, bad = typos_ab.unknown_words("Los vecinos desarollaron una idea.", known)
    assert n == 5 and bad == ["desarollaron"]
    _, bad = typos_ab.unknown_words("Luz fotovoltaica-sol a mediodía.", known)
    assert bad == []                            # every part of the compound is known


def test_summary_rate_and_ranking():
    runs = [{"words": 500, "unknown": ["x", "y"], "seconds": 10, "completion_tokens": 100},
            {"words": 500, "unknown": ["x"], "seconds": 10, "completion_tokens": 100}]
    s = typos_ab.summarise(runs)
    assert s["per_1000"] == 3.0 and s["top_unknown"][0] == ("x", 2) and s["tokens_per_s"] == 10.0


def test_preset_argument():
    assert typos_ab.parse_preset('warm={"temperature":0.8}') == ("warm", {"temperature": 0.8})
    with pytest.raises(Exception):
        typos_ab.parse_preset("nobody")
