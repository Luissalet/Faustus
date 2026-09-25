"""src/engines.py — a model's own vision projector (mmproj): found in an
Ollama blob store or beside the GGUF, passed as --mmproj, round-tripped
through the profile, kept on update and switched off on request."""
from __future__ import annotations

import json
import stat

import pytest

import src.engines as engines
import src.launch_profiles as launch_profiles


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_profiles, "DATA_DIR", str(tmp_path / "data"))
    launch_profiles._own_launches.clear()
    launch_profiles._profile_locks.clear()
    yield
    launch_profiles._own_launches.clear()
    launch_profiles._profile_locks.clear()


@pytest.fixture
def executable(tmp_path):
    path = tmp_path / "llama-server"
    path.write_text("#!/bin/sh\necho hi\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _gguf(path):
    path.write_bytes(b"GGUF" + b"\x03\x00\x00\x00" + b"\x00" * 16)
    return str(path)


@pytest.fixture
def ollama_store(tmp_path):
    """An Ollama store: weights and projector blobs, one manifest naming both."""
    root = tmp_path / "ollama" / "models"
    blobs = root / "blobs"
    blobs.mkdir(parents=True)
    weights = "a" * 64
    proj = "b" * 64
    _gguf(blobs / f"sha256-{weights}")
    _gguf(blobs / f"sha256-{proj}")
    manifest = root / "manifests" / "registry.example" / "library" / "model"
    manifest.mkdir(parents=True)
    (manifest / "27b").write_text(json.dumps({"layers": [
        {"mediaType": "application/vnd.ollama.image.model", "digest": f"sha256:{weights}"},
        {"mediaType": "application/vnd.ollama.image.projector", "digest": f"sha256:{proj}"},
    ]}))
    return str(blobs / f"sha256-{weights}"), str(blobs / f"sha256-{proj}")


def test_find_mmproj_in_ollama_store(ollama_store):
    weights, proj = ollama_store
    assert engines.find_mmproj(weights) == proj


def test_find_mmproj_ollama_blob_without_projector(tmp_path):
    blobs = tmp_path / "models" / "blobs"
    blobs.mkdir(parents=True)
    weights = _gguf(blobs / ("sha256-" + "c" * 64))
    assert engines.find_mmproj(weights) is None


def test_find_mmproj_sibling_file(tmp_path):
    model = _gguf(tmp_path / "vis-model-Q8_0.gguf")
    proj = _gguf(tmp_path / "mmproj-vis-model-f16.gguf")
    assert engines.find_mmproj(model) == proj


def test_find_mmproj_ambiguous_siblings_is_none(tmp_path):
    model = _gguf(tmp_path / "text.gguf")
    _gguf(tmp_path / "mmproj-one.gguf")
    _gguf(tmp_path / "mmproj-two.gguf")
    assert engines.find_mmproj(model) is None


def test_find_mmproj_missing_model():
    assert engines.find_mmproj("") is None
    assert engines.find_mmproj("/nope/model.gguf") is None


def test_create_uses_the_shipped_projector(executable, ollama_store):
    weights, proj = ollama_store
    engine = engines.create_engine(owner="t", name="E", executable=executable,
                                   model_path=weights, port=18081)
    argv = engine["argv"]
    assert argv[argv.index("--mmproj") + 1] == proj
    assert engine["vision"] is True and engine["mmproj_path"] == proj
    assert engine["mmproj_available"] == proj
    assert "--mmproj" not in engine["extra_args"]



def test_create_vision_off(executable, ollama_store):
    weights, _ = ollama_store
    engine = engines.create_engine(owner="t", name="E", executable=executable,
                                   model_path=weights, port=18082, vision=False)
    assert "--mmproj" not in engine["argv"] and engine["vision"] is False


def test_create_vision_on_without_projector_is_refused(executable, tmp_path):
    model = _gguf(tmp_path / "text-only.gguf")
    with pytest.raises(engines.EngineValidationError, match="projector"):
        engines.create_engine(owner="t", name="E", executable=executable,
                              model_path=model, port=18083, vision=True)


def test_create_bad_explicit_projector_is_refused(executable, tmp_path):
    model = _gguf(tmp_path / "m.gguf")
    with pytest.raises(engines.EngineValidationError, match="projector not found"):
        engines.create_engine(owner="t", name="E", executable=executable, model_path=model,
                              port=18084, mmproj_path=str(tmp_path / "missing.gguf"))


def test_update_keeps_projector_and_can_turn_it_off(executable, ollama_store):
    weights, proj = ollama_store
    engine = engines.create_engine(owner="t", name="E", executable=executable,
                                   model_path=weights, port=18085)
    same = engines.update_engine(engine["id"], ctx_size=8192)
    assert same["mmproj_path"] == proj and same["ctx_size"] == 8192
    off = engines.update_engine(engine["id"], vision=False)
    assert off["vision"] is False and "--mmproj" not in off["argv"]
    # Off stays off on an unrelated edit.
    still = engines.update_engine(engine["id"], ctx_size=4096)
    assert still["vision"] is False
    on = engines.update_engine(engine["id"], vision=True)
    assert on["mmproj_path"] == proj


def test_legacy_profile_with_mmproj_in_argv_parses(executable, ollama_store):
    weights, proj = ollama_store
    profile = launch_profiles.create_profile(
        owner="t", name="Old", kind="process", executable=executable,
        argv=["-m", weights, "-c", "4096", "--port", "18086", "--host", "127.0.0.1",
              "-ngl", "99", "--mmproj", proj],
        cwd=str(__import__("os").path.dirname(executable)), readiness={"url": "http://127.0.0.1:18086/health", "timeout_s": 30},
        description="x", desktop=False)
    engine = engines.get_engine(profile["id"])
    assert engine["mmproj_path"] == proj
    assert engine["extra_args"] == ["-ngl", "99"]
    launch_profiles.update_profile(profile["id"], readiness={
        "url": "http://127.0.0.1:18086/health", "timeout_s": 180})
    updated = engines.update_engine(profile["id"], ctx_size=8192)
    assert updated["argv"].count("--mmproj") == 1
    # A longer wait set by hand survives an edit.
    assert updated["readiness"]["timeout_s"] == 180
