"""The per-model `extra` block: more Ollama request options, validated by name.

Luis asked for a place to add "additional commands" for Ollama. What Ollama
takes per request is a fixed list of `options`; llama-server flags are not
among them, and the error must say so instead of silently dropping them.
"""
import json

import pytest

from src import model_load_options as mlo


def test_known_options_are_typed():
    out = mlo.sanitize_extra({"num_batch": "512", "min_p": "0.05", "use_mmap": "false", "stop": "###"})
    assert out == {"num_batch": 512, "min_p": 0.05, "use_mmap": False, "stop": ["###"]}


def test_llama_server_flags_are_named_in_the_error():
    with pytest.raises(ValueError) as e:
        mlo.sanitize_extra({"-jinja": True})
    msg = str(e.value)
    assert "'-jinja' is not an Ollama request option" in msg
    assert "llama-server flags" in msg


def test_form_sends_json_text():
    out = mlo.sanitize_options({"num_ctx": "8192", "extra": json.dumps({"num_batch": 256, "seed": 7})})
    assert out == {"num_ctx": 8192, "extra": {"num_batch": 256, "seed": 7}}


def test_bad_json_is_a_clear_error():
    with pytest.raises(ValueError) as e:
        mlo.sanitize_options({"extra": "{num_batch: 512}"})
    assert "valid JSON" in str(e.value)


def test_empty_extra_unsets_the_block():
    assert "extra" not in mlo.sanitize_options({"extra": ""})
    assert "extra" not in mlo.sanitize_options({"extra": {}})


def test_wrong_types_are_rejected():
    with pytest.raises(ValueError):
        mlo.sanitize_extra({"num_batch": "many"})
    with pytest.raises(ValueError):
        mlo.sanitize_extra({"temperature": True})
    with pytest.raises(ValueError):
        mlo.sanitize_extra({"use_mlock": "maybe"})
