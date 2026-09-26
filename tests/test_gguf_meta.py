"""src/gguf_meta.py — synthetic-GGUF unit tests. No real model files: each
test builds a minimal, valid-enough GGUF header by hand."""
from __future__ import annotations

import struct

from src import gguf_meta


def _kv_string(key: str, value: str) -> bytes:
    kb = key.encode("utf-8")
    vb = value.encode("utf-8")
    return struct.pack("<Q", len(kb)) + kb + struct.pack("<I", 8) + struct.pack("<Q", len(vb)) + vb


def _kv_u32(key: str, value: int) -> bytes:
    kb = key.encode("utf-8")
    return struct.pack("<Q", len(kb)) + kb + struct.pack("<I", 4) + struct.pack("<I", value)


def _kv_string_array(key: str, values: list) -> bytes:
    kb = key.encode("utf-8")
    out = struct.pack("<Q", len(kb)) + kb + struct.pack("<I", 9)  # array type
    out += struct.pack("<I", 8) + struct.pack("<Q", len(values))  # elem type string, count
    for v in values:
        vb = v.encode("utf-8")
        out += struct.pack("<Q", len(vb)) + vb
    return out


def _write_gguf(path, kvs: list) -> None:
    with open(path, "wb") as f:
        f.write(b"GGUF")
        f.write(struct.pack("<I", 3))  # version
        f.write(struct.pack("<Q", 0))  # tensor_count
        f.write(struct.pack("<Q", len(kvs)))  # kv_count
        for kv in kvs:
            f.write(kv)


def test_reads_wanted_u32_key(tmp_path):
    path = tmp_path / "m.gguf"
    _write_gguf(path, [
        _kv_u32("general.file_type", 7),
        _kv_u32("qwen3.nextn_predict_layers", 3),
    ])
    kv = gguf_meta.read_gguf_kv(str(path), (".nextn_predict_layers",))
    assert kv == {"qwen3.nextn_predict_layers": 3}


def test_skips_huge_string_array_without_building_it(tmp_path):
    path = tmp_path / "m.gguf"
    _write_gguf(path, [
        _kv_string_array("tokenizer.ggml.tokens", ["a", "b", "c", "d", "e"]),
        _kv_u32("qwen3.nextn_predict_layers", 5),
    ])
    kv = gguf_meta.read_gguf_kv(str(path), (".nextn_predict_layers",))
    assert kv == {"qwen3.nextn_predict_layers": 5}


def test_stops_early_once_all_wanted_keys_found(tmp_path):
    path = tmp_path / "m.gguf"
    kvs = [_kv_u32("qwen3.nextn_predict_layers", 2)]
    # Append a KV that, if the reader kept going and mis-parsed it, would
    # raise — proves early-stop by simply not reading it.
    kvs.append(b"\xff" * 4)  # garbage, would break parsing if read
    _write_gguf(path, kvs)
    kv = gguf_meta.read_gguf_kv(str(path), (".nextn_predict_layers",))
    assert kv == {"qwen3.nextn_predict_layers": 2}


def test_mtp_layers_present(tmp_path):
    path = tmp_path / "m.gguf"
    _write_gguf(path, [_kv_u32("qwen3.nextn_predict_layers", 4)])
    assert gguf_meta.mtp_layers(str(path)) == 4


def test_mtp_layers_absent_but_valid_gguf_returns_zero(tmp_path):
    path = tmp_path / "m.gguf"
    _write_gguf(path, [_kv_string("general.architecture", "llama")])
    assert gguf_meta.mtp_layers(str(path)) == 0


def test_mtp_layers_truncated_file_returns_none(tmp_path):
    path = tmp_path / "m.gguf"
    with open(path, "wb") as f:
        f.write(b"GGUF")
        f.write(struct.pack("<I", 3))
        f.write(struct.pack("<Q", 0))
        f.write(struct.pack("<Q", 5))  # claims 5 kv entries, has none
    assert gguf_meta.mtp_layers(str(path)) is None
    assert gguf_meta.read_gguf_kv(str(path), (".nextn_predict_layers",)) == {}


def test_mtp_layers_non_gguf_file_returns_none(tmp_path):
    path = tmp_path / "not_a_model.bin"
    path.write_bytes(b"not a gguf file at all")
    assert gguf_meta.mtp_layers(str(path)) is None


def test_mtp_layers_missing_file_returns_none(tmp_path):
    assert gguf_meta.mtp_layers(str(tmp_path / "nope.gguf")) is None


def test_mtp_layers_empty_path_returns_none():
    assert gguf_meta.mtp_layers("") is None


# ── architecture_kv: src/model_architecture.py's dense/moe/mtp read ─────────

def test_architecture_kv_reads_all_three_keys_when_present(tmp_path):
    path = tmp_path / "m.gguf"
    _write_gguf(path, [
        _kv_string("general.architecture", "qwen3moe"),
        _kv_u32("qwen3moe.expert_count", 128),
        _kv_u32("qwen3moe.nextn_predict_layers", 1),
    ])
    kv = gguf_meta.architecture_kv(str(path))
    assert kv == {
        "general.architecture": "qwen3moe",
        "qwen3moe.expert_count": 128,
        "qwen3moe.nextn_predict_layers": 1,
    }


def test_architecture_kv_omits_absent_keys_rather_than_defaulting_them(tmp_path):
    path = tmp_path / "m.gguf"
    _write_gguf(path, [_kv_string("general.architecture", "llama")])
    kv = gguf_meta.architecture_kv(str(path))
    assert kv == {"general.architecture": "llama"}


def test_architecture_kv_non_gguf_file_returns_empty_dict(tmp_path):
    path = tmp_path / "not_a_model.bin"
    path.write_bytes(b"not a gguf file at all")
    assert gguf_meta.architecture_kv(str(path)) == {}


def test_architecture_kv_empty_path_returns_empty_dict():
    assert gguf_meta.architecture_kv("") == {}
