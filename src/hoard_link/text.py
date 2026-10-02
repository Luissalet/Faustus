"""Small text helpers every app re-wrote: accent folding, slugs, safe file names, hashes, emails and phones.

Standard library only. The Node twin is ``js/hoard-commons/text.js`` (same names in camelCase) and both
are checked against ``tests/vectors/text.json``.

* :func:`fold` — lowercase without accents, **one character per character** (the default), so an offset in
  the folded text points at the same character of the original (search highlights, FTS snippets). ``ß``,
  ``ﬁ`` and other characters whose lowercase form is longer keep their own single character. With
  ``keep_length=False`` it is the plain ``NFKD`` + ``casefold`` comparison key (``"Straße"`` -> ``"strasse"``).
* :func:`slugify`, :func:`safe_filename`, :func:`clamp_text`, :func:`sha256_file`, :func:`now_iso`.
* :func:`normalize_email`, :func:`normalize_phone`, :func:`name_similarity` — the forms People, the mail
  gateway and the merchant matching compare.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
import unicodedata
from typing import Any, Optional

__all__ = [
    "fold", "fold_char", "slugify", "safe_filename", "clamp_text", "sha256_file", "sha256_text", "now_iso",
    "normalize_email", "normalize_phone", "name_similarity", "tokens", "domain_of",
]


def fold_char(ch: str) -> str:
    """One character, lowercase and without its accent; characters that would grow keep their own form."""
    base = unicodedata.normalize("NFD", ch)[:1] or ch
    if unicodedata.category(base) == "Mn":          # a lone combining mark stays as it is
        base = ch
    low = base.lower()
    return low if len(low) == 1 else base


def fold(text: Any, *, keep_length: bool = True) -> str:
    """Lowercase and accent-free. ``keep_length=True`` (default) maps one character to one character so offsets
    in the folded text point into the original; ``False`` gives the full compatibility fold (``ß`` -> ``ss``,
    ``ﬁ`` -> ``fi``), for keys that are only compared."""
    s = "" if text is None else str(text)
    if keep_length:
        return "".join(fold_char(c) for c in s)
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: Any, *, max_len: int = 80, sep: str = "-", fallback: str = "") -> str:
    """ASCII slug: folded, non-alphanumerics collapsed to ``sep``, trimmed, at most ``max_len`` characters
    (cut at a separator when possible). ``fallback`` when nothing is left."""
    s = fold(text, keep_length=False)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = _NON_SLUG.sub(sep, s).strip(sep)
    if max_len and len(s) > max_len:
        cut = s[:max_len]
        if sep in cut and not s[max_len:max_len + 1] == sep:
            head = cut.rsplit(sep, 1)[0]
            cut = head if len(head) >= max_len // 2 else cut
        s = cut.strip(sep)
    return s or fallback


_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_BAD_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def safe_filename(name: Any, *, max_len: int = 120, fallback: str = "file", keep_extension: bool = True) -> str:
    """A file name Windows, macOS and Linux all accept: no reserved characters or device names, no trailing dots
    or spaces, at most ``max_len`` characters (the extension is kept when it is short). Accents are kept."""
    s = unicodedata.normalize("NFC", "" if name is None else str(name))
    s = _BAD_FILE_CHARS.sub("_", s)
    s = re.sub(r"\s+", " ", s).strip().strip(".").strip()
    stem, ext = (os.path.splitext(s) if keep_extension else (s, ""))
    if len(ext) > 12 or not re.fullmatch(r"\.[A-Za-z0-9]{1,11}", ext or ".x"):
        stem, ext = s, ""
    if stem.split(".")[0].lower() in _RESERVED:
        stem = "_" + stem
    room = max(1, max_len - len(ext))
    stem = stem[:room].rstrip(" .")
    out = (stem or fallback) + ext
    return out if out.strip(". ") else fallback


def clamp_text(text: Any, limit: int, *, ellipsis: str = "…") -> str:
    """At most ``limit`` characters, cut at a word boundary when one is close, with ``ellipsis`` when cut."""
    s = re.sub(r"\s+", " ", "" if text is None else str(text)).strip()
    if limit <= 0 or len(s) <= limit:
        return s
    cut = s[: max(0, limit - len(ellipsis))]
    space = cut.rfind(" ")
    if space >= len(cut) * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + ellipsis


def sha256_file(path: Any, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def sha256_text(text: Any) -> str:
    return hashlib.sha256(("" if text is None else str(text)).encode("utf-8")).hexdigest()


def now_iso(*, seconds: bool = True, utc: bool = False) -> str:
    """The current time as ISO 8601 with offset (local time unless ``utc``)."""
    now = _dt.datetime.now(_dt.timezone.utc) if utc else _dt.datetime.now().astimezone()
    return now.isoformat(timespec="seconds" if seconds else "milliseconds")


# ---- people and addresses ---------------------------------------------------------------------

_GMAIL = {"gmail.com", "googlemail.com"}


def normalize_email(addr: Any, *, strip_tag: bool = True) -> str:
    """Lowercase address; ``Name <a@b>`` unwrapped; ``+tag`` dropped (``strip_tag``); Gmail dots ignored and
    ``googlemail.com`` -> ``gmail.com``. Empty string when it is not an address."""
    s = "" if addr is None else str(addr).strip()
    m = re.search(r"<([^<>]+)>", s)
    if m:
        s = m.group(1)
    s = s.strip().strip("<>").strip().lower()
    if s.startswith("mailto:"):
        s = s[7:]
    if s.count("@") != 1:
        return ""
    local, domain = s.split("@")
    domain = domain.strip(".")
    if not local or "." not in domain:
        return ""
    if strip_tag and "+" in local:
        local = local.split("+", 1)[0]
    if domain in _GMAIL:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}" if local else ""


_COUNTRY_CODES = {"ES": "34", "FR": "33", "PT": "351", "IT": "39", "DE": "49", "GB": "44", "UK": "44", "US": "1",
                  "MX": "52", "AR": "54", "NL": "31", "BE": "32", "IE": "353", "CH": "41"}


def normalize_phone(num: Any, region: str = "ES") -> str:
    """E.164-like digits with ``+``: spaces, dots, dashes and brackets dropped, ``00`` -> ``+``, a national
    number gets the ``region``'s code (Spain: 9 digits starting 6-9). Empty string when no number is left."""
    s = "" if num is None else str(num).strip()
    if not s:
        return ""
    plus = s.startswith("+") or s.startswith("00")
    digits = re.sub(r"\D", "", s)
    if s.startswith("00"):
        digits = digits[2:]
    if not digits or len(digits) < 6:
        return ""
    if plus:
        return "+" + digits
    cc = _COUNTRY_CODES.get((region or "").upper(), "")
    if cc == "34" and len(digits) == 11 and digits.startswith("34"):
        return "+" + digits
    if cc:
        return "+" + cc + digits.lstrip("0") if cc != "39" else "+" + cc + digits
    return digits


_WORD = re.compile(r"[a-z0-9]+")


def tokens(text: Any) -> list[str]:
    """Folded alphanumeric words."""
    return _WORD.findall(fold(text, keep_length=False))


def name_similarity(a: Any, b: Any) -> float:
    """0..1 similarity of two person or company names: folded word overlap (Jaccard), with initials matching a
    whole word and word order ignored. ``1.0`` for the same words."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    if ta == tb or sorted(ta) == sorted(tb):
        return 1.0
    sa, sb = set(ta), set(tb)
    hits = len(sa & sb)
    for x in sa - sb:                       # "j" matches "juan"
        if len(x) == 1 and any(y.startswith(x) for y in sb - sa):
            hits += 0.5
    union = len(sa | sb)
    return round(min(1.0, hits / union), 4) if union else 0.0


def domain_of(value: Any) -> str:
    """The host of a URL or the domain of an e-mail address, lowercase, without ``www.``."""
    s = "" if value is None else str(value).strip().lower()
    if not s:
        return ""
    if "@" in s and "://" not in s:
        s = s.rsplit("@", 1)[1]
    else:
        s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
        s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        s = s.rsplit("@", 1)[-1]
        if s.startswith("["):
            return s.split("]", 1)[0].strip("[")
        s = s.split(":", 1)[0]
    s = s.strip(".>").strip()
    return s[4:] if s.startswith("www.") else s

