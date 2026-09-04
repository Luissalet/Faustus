"""
contracts/base.py — the three rules every Faustus contract obeys.

1. A rejection names the field and what was seen.  "invalid manifest" is not
   an error message; ``permissions.network: expected bool, got 'yes' (str)``
   is.  A contract that cannot say why it said no is a contract nobody can
   fix their manifest against.

2. An unknown key is an error, never a default.  A manifest that spells
   ``permisions:`` has a typo, and answering that typo with the deny-by-default
   permission set would hide it behind a run that looks plausible.

3. Nothing is coerced across a type boundary.  Stripping the blanks around a
   string is a normalization; reading ``1`` as ``True`` or ``"3"`` as ``3`` is
   a guess about what someone meant.  We do the first and refuse the second.

`fingerprint()` is the same length-prefixed SHA-256 rule as
`src.prove.identity_of`: a variable-length field is never concatenated without
its own length in front of it, so two different contracts cannot collide by
shifting a delimiter across a field boundary.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

_UNSET = object()


class ContractError(ValueError):
    """A rejection that can be acted on: which field, what was expected, and
    what was actually there.  `got` is the value itself when it is small
    enough to quote and a type name when it is not — a 4 MB blob in an error
    message helps nobody."""

    def __init__(self, path: str, message: str, *, got: Any = _UNSET) -> None:
        self.path = path or "<root>"
        self.message = message
        self.got = None if got is _UNSET else got
        self.has_got = got is not _UNSET
        super().__init__(str(self))

    def __str__(self) -> str:
        if not self.has_got:
            return f"{self.path}: {self.message}"
        return f"{self.path}: {self.message}, got {_show(self.got)}"


def _show(value: Any, limit: int = 60) -> str:
    """Quote a value for an error message without pasting a whole payload."""
    name = type(value).__name__
    if value is None:
        return "None"
    if isinstance(value, (str, bytes)):
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        if len(text) > limit:
            return f"{text[:limit]!r}… ({len(text)} chars, {name})"
        return f"{text!r} ({name})"
    if isinstance(value, (int, float, bool)):
        return f"{value!r} ({name})"
    if isinstance(value, (list, tuple, set, frozenset)):
        return f"{name} of {len(value)}"
    if isinstance(value, Mapping):
        return f"{name} with keys {sorted(str(k) for k in value)[:6]}"
    return f"a {name}"


# ── the shape of a payload ──────────────────────────────────────────────────

def as_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(path, "expected an object", got=value)
    bad = [k for k in value if not isinstance(k, str)]
    if bad:
        raise ContractError(path, f"keys must be strings; {bad[:3]} are not")
    return value


def reject_unknown(data: Mapping[str, Any], allowed: Iterable[str], path: str) -> None:
    """Rule 2.  The message lists the nearest allowed key when there is an
    obvious one, because 90% of unknown keys are a typo one letter away."""
    allowed = set(allowed)
    unknown = sorted(k for k in data if k not in allowed)
    if not unknown:
        return
    hints = []
    for key in unknown:
        near = [a for a in sorted(allowed) if _close(key, a)]
        hints.append(f"{key!r}" + (f" (did you mean {near[0]!r}?)" if near else ""))
    raise ContractError(
        path,
        "unknown key" + ("s" if len(unknown) > 1 else "") + ": " + ", ".join(hints)
        + f"; allowed: {sorted(allowed)}",
    )


def _close(a: str, b: str) -> bool:
    """One edit apart, cheaply — enough to catch `permisions`/`permissions`
    and `backend`/`backends` without pulling in a distance library."""
    if a == b:
        return False
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    long, short = (a, b) if len(a) > len(b) else (b, a)
    for i in range(len(long)):
        if long[:i] + long[i + 1:] == short:
            return True
    return False


# ── typed readers.  None of them cross a type boundary (rule 3) ─────────────

def text(data: Mapping[str, Any], key: str, path: str, *,
         required: bool = True, default: str = "",
         max_len: int = 4096, allow_blank: bool = False) -> str:
    raw = data.get(key, _UNSET)
    if raw is _UNSET or raw is None:
        if required:
            raise ContractError(f"{path}.{key}", "is required")
        return default
    if not isinstance(raw, str):
        raise ContractError(f"{path}.{key}", "expected a string", got=raw)
    value = raw.strip()
    if not value and not allow_blank:
        if required:
            raise ContractError(f"{path}.{key}", "is required but blank")
        return default
    if len(value) > max_len:
        raise ContractError(f"{path}.{key}", f"is longer than {max_len} chars", got=value)
    return value


def flag(data: Mapping[str, Any], key: str, path: str, *, default: bool) -> bool:
    raw = data.get(key, _UNSET)
    if raw is _UNSET or raw is None:
        return default
    if not isinstance(raw, bool):
        raise ContractError(
            f"{path}.{key}",
            "expected true or false (a permission is never inferred from a truthy value)",
            got=raw,
        )
    return raw


def whole(data: Mapping[str, Any], key: str, path: str, *,
          required: bool = False, default: Optional[int] = None,
          minimum: Optional[int] = None, maximum: Optional[int] = None) -> Optional[int]:
    raw = data.get(key, _UNSET)
    if raw is _UNSET or raw is None:
        if required:
            raise ContractError(f"{path}.{key}", "is required")
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ContractError(f"{path}.{key}", "expected a whole number", got=raw)
    if minimum is not None and raw < minimum:
        raise ContractError(f"{path}.{key}", f"must be >= {minimum}", got=raw)
    if maximum is not None and raw > maximum:
        raise ContractError(f"{path}.{key}", f"must be <= {maximum}", got=raw)
    return raw


def one_of(data: Mapping[str, Any], key: str, path: str, *,
           choices: Sequence[str], required: bool = True,
           default: Optional[str] = None) -> Optional[str]:
    raw = data.get(key, _UNSET)
    if raw is _UNSET or raw is None:
        if required:
            raise ContractError(f"{path}.{key}", f"is required; one of {list(choices)}")
        return default
    if not isinstance(raw, str):
        raise ContractError(f"{path}.{key}", f"expected one of {list(choices)}", got=raw)
    value = raw.strip()
    if value not in choices:
        near = [c for c in choices if _close(value, c)]
        hint = f" (did you mean {near[0]!r}?)" if near else ""
        raise ContractError(f"{path}.{key}", f"must be one of {list(choices)}{hint}", got=raw)
    return value


def text_list(data: Mapping[str, Any], key: str, path: str, *,
              default: Optional[Sequence[str]] = None,
              max_items: int = 256, max_len: int = 512,
              unique: bool = True, choices: Optional[Sequence[str]] = None) -> Tuple[str, ...]:
    raw = data.get(key, _UNSET)
    if raw is _UNSET or raw is None:
        return tuple(default or ())
    if isinstance(raw, str):
        raise ContractError(
            f"{path}.{key}",
            "expected a list; a bare string is not a one-item list here "
            "(a comma inside it would silently become one permission or two)",
            got=raw,
        )
    if not isinstance(raw, (list, tuple)):
        raise ContractError(f"{path}.{key}", "expected a list of strings", got=raw)
    if len(raw) > max_items:
        raise ContractError(f"{path}.{key}", f"has more than {max_items} items", got=raw)
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            raise ContractError(f"{path}.{key}[{i}]", "expected a string", got=item)
        value = item.strip()
        if not value:
            raise ContractError(f"{path}.{key}[{i}]", "is blank")
        if len(value) > max_len:
            raise ContractError(f"{path}.{key}[{i}]", f"is longer than {max_len} chars", got=value)
        if choices is not None and value not in choices:
            raise ContractError(f"{path}.{key}[{i}]", f"must be one of {list(choices)}", got=value)
        if unique and value in out:
            raise ContractError(f"{path}.{key}[{i}]", "is a duplicate", got=value)
        out.append(value)
    return tuple(out)


# ── names that other systems will have to route on ─────────────────────────

_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
                        r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def ident(data: Mapping[str, Any], key: str, path: str, *,
          required: bool = True, default: str = "") -> str:
    """A lowercase dotted id: `media.video.short-form`.  Deliberately narrow —
    ids end up in filesystem paths, URLs and event names, and a manifest that
    picks `../../etc` for its id should be rejected by the contract, not by
    whichever of those three notices first."""
    value = text(data, key, path, required=required, default=default, max_len=128)
    if not value:
        return value
    if not _ID_RE.fullmatch(value):
        raise ContractError(
            f"{path}.{key}",
            "must be lowercase a-z0-9 separated by . _ or - (it becomes a path, "
            "a URL and an event name)",
            got=value,
        )
    return value


def semver(data: Mapping[str, Any], key: str, path: str, *,
           required: bool = True, default: str = "") -> str:
    value = text(data, key, path, required=required, default=default, max_len=64)
    if not value:
        return value
    if not _SEMVER_RE.fullmatch(value):
        raise ContractError(f"{path}.{key}", "must be a semantic version like 1.0.0", got=value)
    return value


def sha256_hex(data: Mapping[str, Any], key: str, path: str, *,
               required: bool = True, default: str = "") -> str:
    value = text(data, key, path, required=required, default=default, max_len=64)
    if not value:
        return value
    if not _HEX64_RE.fullmatch(value.lower()):
        raise ContractError(f"{path}.{key}", "must be 64 lowercase hex chars (a SHA-256)", got=value)
    return value.lower()


# ── time.  An unreadable timestamp stays None; it never becomes "now" ──────

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp(data: Mapping[str, Any], key: str, path: str, *,
              required: bool = False, default: Optional[str] = None) -> Optional[str]:
    """ISO-8601 UTC.  The rule the history importer learned the hard way: a
    date we cannot read is `None`, not the moment we happened to read it."""
    raw = data.get(key, _UNSET)
    if raw is _UNSET or raw is None:
        if required:
            raise ContractError(f"{path}.{key}", "is required (an ISO-8601 UTC timestamp)")
        return default
    if not isinstance(raw, str):
        raise ContractError(f"{path}.{key}", "expected an ISO-8601 string", got=raw)
    value = raw.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContractError(f"{path}.{key}", "is not an ISO-8601 timestamp", got=raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── the identity hash (length-prefixed, order-free) ────────────────────────

def _lp(raw: bytes) -> bytes:
    """``<len>:<bytes>``.  A variable-length field is never concatenated
    without its own length in front of it."""
    return str(len(raw)).encode("ascii") + b":" + raw


def _encode(value: Any) -> bytes:
    if value is None:
        return b"~"
    if isinstance(value, bool):
        return b"1" if value else b"0"
    if isinstance(value, Mapping):
        items = sorted((str(k), value[k]) for k in value)
        return _lp(str(len(items)).encode("ascii")) + b"".join(
            _lp(k.encode("utf-8", "replace")) + _lp(_encode(v)) for k, v in items
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(_encode(v) for v in value)
        return _lp(str(len(items)).encode("ascii")) + b"".join(_lp(i) for i in items)
    return str(value).encode("utf-8", "replace")


def fingerprint(parts: Sequence[Tuple[str, Any]]) -> str:
    """SHA-256 over ``(name, value)`` pairs, each half length-prefixed."""
    h = hashlib.sha256()
    for name, value in parts:
        h.update(_lp(str(name).encode("utf-8", "replace")))
        h.update(_lp(_encode(value)))
    return h.hexdigest()
