"""src/gguf_meta.py — minimal, stdlib-only GGUF header reader.

Only reads what `src/engines.py` needs to decide whether a model supports
MTP speculative decoding: a handful of scalar KV metadata keys, picked out
by suffix (the full key is namespaced per architecture, e.g.
`qwen3.nextn_predict_layers`, so callers match on suffix rather than a
fixed key). Never loads tensor data, and skips string arrays (vocab lists
can be 100k+ entries) by reading only their length-prefixes.

GGUF format (little-endian):
  magic:        4 bytes, b"GGUF"
  version:      u32
  tensor_count: u64
  kv_count:     u64
  kv[kv_count]: { key: gguf_string, value_type: u32, value }

gguf_string := u64 length + that many UTF-8 bytes (not NUL-terminated).

Value types (ggml_type is unrelated — this is GGUF's own value-type enum):
  0 u8   1 i8   2 u16   3 i16   4 u32   5 i32   6 f32   7 bool(u8)
  8 string   9 array(elem_type: u32, count: u64, elems)
  10 u64   11 i64   12 f64

Never raises: any parse failure yields an empty dict / None, since this is
advisory metadata (whether to offer MTP in the UI), not something a model
load depends on.
"""
from __future__ import annotations

import struct
from typing import Any, Dict, Iterable, Optional

_MAGIC = b"GGUF"

# Fixed byte-width scalar types: type_id -> (struct fmt, size).
_SCALAR_FMT = {
    0: ("B", 1),   # u8
    1: ("b", 1),   # i8
    2: ("H", 2),   # u16
    3: ("h", 2),   # i16
    4: ("I", 4),   # u32
    5: ("i", 4),   # i32
    6: ("f", 4),   # f32
    7: ("B", 1),   # bool, stored as u8
    10: ("Q", 8),  # u64
    11: ("q", 8),  # i64
    12: ("d", 8),  # f64
}

_TYPE_STRING = 8
_TYPE_ARRAY = 9


class _Reader:
    """Sequential little-endian reader over a buffered file handle."""

    def __init__(self, fh: Any) -> None:
        self._fh = fh

    def _read(self, n: int) -> bytes:
        data = self._fh.read(n)
        if len(data) != n:
            raise EOFError("unexpected end of file")
        return data

    def u32(self) -> int:
        return struct.unpack("<I", self._read(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self._read(8))[0]

    def skip(self, n: int) -> None:
        if n <= 0:
            return
        # seek is fine here (file is opened for random access); avoids
        # buffering potentially large blobs (string arrays) into memory.
        self._fh.seek(n, 1)

    def string(self) -> str:
        length = self.u64()
        return self._read(length).decode("utf-8", errors="replace")

    def skip_string(self) -> None:
        length = self.u64()
        self.skip(length)

    def scalar(self, type_id: int) -> Any:
        fmt, size = _SCALAR_FMT[type_id]
        return struct.unpack("<" + fmt, self._read(size))[0]

    def skip_value(self, type_id: int) -> None:
        """Skip a value of the given type without materializing it (used
        for array elements and for values of keys we don't want)."""
        if type_id == _TYPE_STRING:
            self.skip_string()
        elif type_id == _TYPE_ARRAY:
            elem_type = self.u32()
            count = self.u64()
            if elem_type == _TYPE_STRING:
                for _ in range(count):
                    self.skip_string()
            elif elem_type == _TYPE_ARRAY:
                for _ in range(count):
                    self.skip_value(elem_type)
            else:
                _, size = _SCALAR_FMT[elem_type]
                self.skip(size * count)
        else:
            _, size = _SCALAR_FMT[type_id]
            self.skip(size)

    def read_value(self, type_id: int) -> Any:
        """Materialize a scalar or string value (never called for arrays —
        wanted keys here are always scalars)."""
        if type_id == _TYPE_STRING:
            return self.string()
        return self.scalar(type_id)


def read_gguf_kv(path: str, wanted_suffixes: Iterable[str]) -> Dict[str, Any]:
    """Parse a GGUF file's KV header and return only the keys whose name
    ends with one of `wanted_suffixes`. Arrays (including huge string
    arrays like vocab/token lists) are skipped without being built.
    Returns {} on any error, and stops early once every wanted suffix has
    been matched at least once."""
    return _read_gguf_kv_internal(path, wanted_suffixes)[0]


def _read_gguf_kv_internal(path: str, wanted_suffixes: Iterable[str]):
    """Like `read_gguf_kv`, but also reports whether the file parsed clean
    (valid magic and the header fully readable) — `mtp_layers` needs this
    to tell "valid GGUF, key just absent" (0) from "unreadable/corrupt"
    (None) apart, since both look like an empty result dict."""
    suffixes = tuple(wanted_suffixes)
    out: Dict[str, Any] = {}
    if not suffixes:
        return out, True
    try:
        with open(path, "rb") as fh:
            r = _Reader(fh)
            magic = fh.read(4)
            if magic != _MAGIC:
                return out, False
            _version = r.u32()
            _tensor_count = r.u64()
            kv_count = r.u64()
            remaining = set(suffixes)
            for _ in range(kv_count):
                if not remaining:
                    break
                key = r.string()
                value_type = r.u32()
                matched = next((s for s in remaining if key.endswith(s)), None)
                if matched is not None and value_type != _TYPE_ARRAY:
                    out[key] = r.read_value(value_type)
                    remaining.discard(matched)
                else:
                    r.skip_value(value_type)
    except (OSError, EOFError, struct.error, UnicodeDecodeError, KeyError, ValueError):
        return {}, False
    return out, True


def mtp_layers(path: str) -> Optional[int]:
    """Value of the `*.nextn_predict_layers` KV key (how many MTP/draft
    layers this GGUF ships), or None when the file is unreadable / not a
    GGUF. 0 means the key is present-but-zero or simply absent — either way
    "no MTP support"."""
    if not path:
        return None
    kv, ok = _read_gguf_kv_internal(path, (".nextn_predict_layers",))
    if not ok:
        return None
    for key, value in kv.items():
        if key.endswith(".nextn_predict_layers"):
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0
