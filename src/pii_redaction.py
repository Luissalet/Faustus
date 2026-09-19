"""pii_redaction.py

Pure, table-driven PII sanitising for RAG chunk text (FAUSTUS §146).

``redact_pii(text)`` replaces personally-identifiable substrings with
bracketed placeholders (``[EMAIL]``, ``[PHONE]``, ``[IBAN]``, ``[CARD]``,
``[ID]``, ``[IP]``) and returns ``(sanitized_text, counts)`` where ``counts``
maps each placeholder type to how many instances were replaced.

This module never touches files on disk — callers decide what text to pass
in (e.g. the RAG indexer sanitises the chunk text that gets embedded, while
the original uploaded document is left untouched) and what to do with the
counts (the indexer records them as per-chunk ``redactions`` metadata).

Detectors are deliberately conservative so common non-PII numbers (years,
prices, invoice numbers, page counts, ...) are left alone:

- EMAIL: standard ``local@domain.tld`` shape.
- IP: dotted-quad IPv4 with each octet in 0-255.
- IBAN: ``CC99XXXXXXXXXXX...`` validated with the mod-97 checksum (ISO 7064),
  so a random 2-letters+digits string is not flagged.
- ID: Spanish DNI (8 digits + control letter) and NIE (X/Y/Z + 7 digits +
  control letter), validated against the real control-letter table so a
  plain 8/9-digit number is not flagged.
- CARD: 13-19 digit runs (optionally space/dash separated) that pass the
  Luhn checksum.
- PHONE: Spanish national format (9 digits starting 6/7/8/9, optionally
  prefixed +34/0034) and generic international E.164-ish numbers
  (``+`` followed by 8-15 digits, optionally grouped).

Order matters: more structurally distinctive patterns (email, IBAN, ID) are
redacted before the looser numeric ones (card, phone) so, e.g., a DNI's
digits are never mistaken for a phone number after its control letter is
stripped.
"""

from __future__ import annotations

import re
from typing import Dict, Tuple

__all__ = ["redact_pii"]

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9][a-zA-Z0-9._%+\-]*@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)

# ---------------------------------------------------------------------------
# IPv4
# ---------------------------------------------------------------------------

_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
)

# ---------------------------------------------------------------------------
# IBAN (ISO 13616, mod-97 = 1 checksum)
# ---------------------------------------------------------------------------

# Compact ("ES9121000418...") or printed in groups of four
# ("ES91 2100 0418 4502 0005 1332").
_IBAN_CANDIDATE_RE = re.compile(
    r"\b[A-Za-z]{2}\d{2}(?:[A-Za-z0-9]{9,30}|(?: ?[A-Za-z0-9]{4}){2,7}(?: ?[A-Za-z0-9]{1,3})?)\b"
)


def _iban_valid(candidate: str) -> bool:
    candidate = candidate.replace(" ", "").upper()
    if not (15 <= len(candidate) <= 34):
        return False
    if not re.match(r"^[A-Z]{2}\d{2}[A-Z0-9]+$", candidate):
        return False
    rearranged = candidate[4:] + candidate[:4]
    digits = "".join(
        str(int(ch, 36)) if ch.isalpha() else ch for ch in rearranged
    )
    try:
        return int(digits) % 97 == 1
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Spanish DNI / NIE (control-letter checksum)
# ---------------------------------------------------------------------------

_DNI_NIE_RE = re.compile(r"\b(?:\d{8}|[XYZxyz]\d{7})[A-Za-z]\b")
_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_NIE_PREFIX = {"X": "0", "Y": "1", "Z": "2"}


def _dni_nie_valid(candidate: str) -> bool:
    body, letter = candidate[:-1], candidate[-1].upper()
    first = body[0].upper()
    if first in _NIE_PREFIX:
        body = _NIE_PREFIX[first] + body[1:]
    if not body.isdigit():
        return False
    expected = _DNI_LETTERS[int(body) % 23]
    return expected == letter


# ---------------------------------------------------------------------------
# Credit-card-like numbers (Luhn checksum)
# ---------------------------------------------------------------------------

_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ \-]?){12,18}\d\b")


def _luhn_valid(digits: str) -> bool:
    if not (13 <= len(digits) <= 19) or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ---------------------------------------------------------------------------
# Phone numbers (Spanish + generic international)
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(
    r"(?<![\w@.])"
    r"(?:"
    # Spanish national/mobile, optionally with +34 / 0034
    # (any grouping of the 9 digits: 612 345 678, 91 123 45 67, 612345678)
    r"(?:\+34|0034)?[ \-]?[6789](?:[ \-]?\d){8}"
    r"|"
    # Generic international E.164-ish: + then 8-15 digits, optionally grouped
    r"\+\d{1,3}(?:[ \-]?\d{2,4}){2,4}"
    r")"
    # A sentence-ending period may follow; a decimal/dotted continuation may not.
    r"(?![\w@]|\.\d)"
)


def _redact_pattern(
    text: str,
    pattern: re.Pattern,
    placeholder: str,
    counts: Dict[str, int],
    *,
    validate=None,
) -> str:
    def _sub(match: "re.Match") -> str:
        candidate = match.group(0)
        if validate is not None and not validate(candidate):
            return candidate
        counts[placeholder] = counts.get(placeholder, 0) + 1
        return f"[{placeholder}]"

    return pattern.sub(_sub, text)


def redact_pii(text: str) -> Tuple[str, Dict[str, int]]:
    """Sanitise ``text``, returning ``(sanitized_text, redactions)``.

    ``redactions`` maps placeholder type ("EMAIL", "PHONE", "IBAN", "CARD",
    "ID", "IP") to the number of instances replaced; types with zero matches
    are omitted. Empty/non-string input is returned unchanged with an empty
    counts dict.
    """
    if not text or not isinstance(text, str):
        return text, {}

    counts: Dict[str, int] = {}

    # Order: most structurally distinctive first, so looser numeric patterns
    # (card, phone) never re-match digits already consumed by a more
    # specific, validated pattern.
    text = _redact_pattern(text, _EMAIL_RE, "EMAIL", counts)
    text = _redact_pattern(text, _IPV4_RE, "IP", counts)
    text = _redact_pattern(text, _IBAN_CANDIDATE_RE, "IBAN", counts, validate=_iban_valid)
    text = _redact_pattern(text, _DNI_NIE_RE, "ID", counts, validate=_dni_nie_valid)
    text = _redact_pattern(
        text, _CARD_CANDIDATE_RE, "CARD", counts,
        validate=lambda c: _luhn_valid(c.replace(" ", "").replace("-", "")),
    )
    text = _redact_pattern(text, _PHONE_RE, "PHONE", counts)

    return text, counts
