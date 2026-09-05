"""Helpers for keeping sensitive data out of logs.

Endpoint URLs configured by admins can embed credentials in the userinfo
(``https://user:pass@host``) or query string (``?api_key=...``). Logging them
raw leaks those secrets, so route/diagnostic logs run URLs through
``redact_url`` first. Reconstructing the URL without userinfo/query/fragment
also doubles as a sanitizer barrier for CodeQL's clear-text-logging query.
"""

import logging
import re
from urllib.parse import urlparse, urlunparse


def redact_url(url: str) -> str:
    """Return a URL safe for logs by removing userinfo and query/fragment.

    Keeps scheme, host, port and path so logs stay useful for debugging.
    """
    try:
        parsed = urlparse(url or "")
        host = parsed.hostname or ""
        if ":" in host:  # IPv6 literal — re-bracket so host:port stays unambiguous
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunparse((parsed.scheme, host, parsed.path, "", "", ""))
    except Exception:
        return "<endpoint>"


# ── Global secret redaction (SEC-1 / B-009) ─────────────────────────────────
# An OAuth payload reached `data/logs/app.log` through a single f-string in a
# route. Route-by-route discipline cannot hold that line: one careless
# interpolation anywhere is a permanent plaintext leak into a rotating file
# nobody re-reads. So redaction lives on the log *handlers*, applies to every
# record whoever emitted it, and is verified by tests with sentinel secrets.


REDACTED = "***"

#: Key names whose value is never safe to print. Deliberately narrow: a broad
#: `token` would eat `token_count=812` and make the logs useless.
SECRET_KEY_NAMES = (
    "client_secret",
    "client-secret",
    "clientsecret",
    "access_token",
    "refresh_token",
    "id_token",
    "session_token",
    "api_key",
    "apikey",
    "api-key",
    "password",
    "passwd",
    "private_key",
    "secret_key",
    "totp_secret",
    "recovery_code",
    "bw_session",
    "hf_token",
    "internal_token",
)

# `Authorization: Bearer x` is handled first and eats the rest of the line:
# the generic key/value rule stops at whitespace and would leave the token.
_AUTH_HEADER_PATTERN = re.compile(
    r"(?i)(?P<key>['\"]?\bauthorization\b['\"]?\s*(?::|=>|=)\s*)"
    r"(?P<quote>['\"]?)(?P<value>[^'\"\r\n]{1,4096}?)(?P=quote)(?=[,;}\]\r\n]|$)"
)
_BEARER_PATTERN = re.compile(r"(?i)\b(?P<scheme>bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}")
_KV_PATTERN = re.compile(
    r"(?i)(?P<key>['\"]?\b(?:"
    + "|".join(re.escape(name) for name in SECRET_KEY_NAMES)
    + r")\b['\"]?\s*(?::|=>|=)\s*)"
    r"(?P<quote>['\"]?)(?P<value>[^\s,;'\"}\])&]+)(?P=quote)"
)


def _sub_kv(match: "re.Match") -> str:
    quote = match.group("quote")
    return f"{match.group('key')}{quote}{REDACTED}{quote}"


def redact_secrets(text) -> str:
    """Replace secret-looking values in ``text`` with ``***``.

    Handles the three shapes secrets actually take in this codebase: JSON and
    dict reprs (``"client_secret": "…"``, ``'api_key': '…'``), query strings
    and env assignments (``?api_key=…&``), and auth headers.
    """
    if text is None:
        return ""
    out = str(text)
    if not out:
        return out
    out = _AUTH_HEADER_PATTERN.sub(_sub_kv, out)
    out = _BEARER_PATTERN.sub(lambda m: f"{m.group('scheme')} {REDACTED}", out)
    return _KV_PATTERN.sub(_sub_kv, out)


class SecretRedactingFilter(logging.Filter):
    """Rewrites a record's message in place before any handler formats it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        if getattr(record, "exc_text", None):
            record.exc_text = redact_secrets(record.exc_text)
        return True


class SecretRedactingFormatter(logging.Formatter):
    """Wraps another formatter and redacts its output.

    The filter above cannot see a traceback: ``exc_text`` is rendered by the
    formatter, after filters have run. Wrapping the formatter closes that gap,
    because an exception message frequently carries the very value we are
    trying to keep out of the file.
    """

    def __init__(self, inner: logging.Formatter):
        super().__init__()
        self.inner = inner

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(self.inner.format(record))


#: Loggers that install their own handlers instead of propagating to root.
_EXTRA_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore")


def harden_handler(handler: logging.Handler) -> None:
    """Attach the redaction filter and formatter to one handler (idempotent)."""
    if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
        handler.addFilter(SecretRedactingFilter())
    if not isinstance(handler.formatter, SecretRedactingFormatter):
        handler.setFormatter(SecretRedactingFormatter(handler.formatter or logging.Formatter()))


def install_secret_redaction(extra_loggers=None) -> int:
    """Harden every handler on the root logger and the named loggers.

    Safe to call more than once: uvicorn installs its handlers after the app
    module is imported, so this runs again on startup. Returns the number of
    handlers hardened, so a caller can log (or test) that it did something.
    """
    count = 0
    names = list(_EXTRA_LOGGERS) + list(extra_loggers or ())
    for handler in list(logging.getLogger().handlers):
        harden_handler(handler)
        count += 1
    for name in names:
        for handler in list(logging.getLogger(name).handlers):
            harden_handler(handler)
            count += 1
    return count
