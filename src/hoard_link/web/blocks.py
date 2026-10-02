"""Anti-bot / login-wall detection over a fetched document.

The Node twin is ``detectBlock`` in ``js/hoard-commons/web.js`` (same rules, checked against
``tests/vectors/web_blocks.json``).

The scripts of perfectly normal pages mention "captcha", "akamai" or "datadome", so bare keywords are never
matched. Every rule needs a structural signal (a header, a title, a widget element, a challenge URL) and, for
markup-based rules, a *small* page: real challenge pages are a few kilobytes, real product pages are not.

``detect_block`` returns one of: cloudflare, akamai, datadome, perimeterx, captcha, login, http_403,
http_429, http_5xx — or ``""`` when the document looks fine.
"""

from __future__ import annotations

import html as htmllib
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

CLOUDFLARE = "cloudflare"
AKAMAI = "akamai"
DATADOME = "datadome"
PERIMETERX = "perimeterx"
CAPTCHA = "captcha"
LOGIN = "login"
HTTP_403 = "http_403"
HTTP_429 = "http_429"
HTTP_5XX = "http_5xx"

__all__ = ["detect_block", "block_hint", "apply_block", "BLOCK_REASONS", "BROWSER_RETRY_REASONS", "CLOUDFLARE", "AKAMAI", "DATADOME",
           "PERIMETERX", "CAPTCHA", "LOGIN", "HTTP_403", "HTTP_429", "HTTP_5XX"]

BLOCK_REASONS = (CLOUDFLARE, AKAMAI, DATADOME, PERIMETERX, CAPTCHA, LOGIN, HTTP_403, HTTP_429, HTTP_5XX)
# Reasons for which a real browser might get through (a rate limit or an outage would only get worse).
BROWSER_RETRY_REASONS = frozenset({CLOUDFLARE, AKAMAI, DATADOME, PERIMETERX, CAPTCHA, LOGIN, HTTP_403})

SMALL_PAGE = 60_000       # a challenge / error page is far smaller than this
TINY_PAGE = 12_000
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_LOGIN_PATH = re.compile(
    r"/(?:login|log-in|signin|sign-in|sign_in|iniciar-?sesion|acceso|ap/signin|account/login|customer/account/login)(?:[/?#.]|$)", re.I)
_LOGIN_TITLE = re.compile(
    r"^\s*(?:inicia(?:r)?\s+sesi[oó]n|acceso\s+de\s+clientes|identif[ií]cate|sign\s*in|log\s*in|login|iniciar sesi[oó]n en .*)\s*(?:[|\-–—:].*)?$", re.I)
_CF_TITLE = re.compile(r"just a moment|un momento|attention required!? \| cloudflare|checking your browser|verificando", re.I)
_CF_MARKERS = ("challenges.cloudflare.com", "challenge-platform", "cf-chl", "cf-turnstile", "__cf_chl", "cdn-cgi/styles/cf.errors")
_AMAZON_CAPTCHA = ("validatecaptcha", "robot check", "introduce los caracteres que ves", "type the characters you see",
                   "enter the characters you see below", "introduce los caracteres que aparecen")


def _norm_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def _title(text: str) -> str:
    match = _TITLE.search(text[:40_000])
    return htmllib.unescape(_TAGS.sub("", match.group(1))).strip() if match else ""


def _visible_len(text: str) -> int:
    """Rough length of the visible text (scripts / styles / tags removed) — cheap, for small pages only."""
    stripped = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.I | re.S)
    return len(re.sub(r"\s+", " ", _TAGS.sub(" ", stripped)).strip())


def detect_block(status: int, text: str, headers: Mapping[str, str] | None, url: str) -> str:
    """Classify a response. See the module docstring for the vocabulary."""
    hdr = _norm_headers(headers)
    text = text or ""
    size = len(text)
    small = size < SMALL_PAGE
    body = htmllib.unescape(text[:SMALL_PAGE]).lower() if small else ""
    title = _title(text) if small else ""
    title_l = title.lower()
    server = hdr.get("server", "").lower()
    cookies = (hdr.get("set-cookie", "") + " " + hdr.get("cookie", "")).lower()

    # --- Cloudflare: header, or challenge title/markers on a small page
    if hdr.get("cf-mitigated", "").lower() == "challenge":
        return CLOUDFLARE
    if small:
        cf_marker = "cloudflare" in body or any(m in body for m in _CF_MARKERS)
        if (_CF_TITLE.search(title) and cf_marker) or (status in (403, 429, 503) and any(m in body for m in _CF_MARKERS)):
            return CLOUDFLARE

    # --- Akamai: "Access Denied" + "Reference #" error page, or the bot-manager interstitial
    if small:
        if ("access denied" in title_l or "<h1>access denied</h1>" in body) and (
                "reference #" in body or "errors.edgesuite.net" in body or "akamai" in body):
            return AKAMAI
        if size < TINY_PAGE and ("bm-verify" in body or "/_sec/verify" in body or "triggerinterstitialchallenge" in body):
            return AKAMAI
        if status in (403, 429) and "akamai" in server and size < TINY_PAGE:
            return AKAMAI

    # --- DataDome: header / cookie plus a small page or an error status, or the captcha-delivery iframe
    if "x-datadome" in hdr or "x-dd-b" in hdr or "datadome=" in cookies:
        if status in (403, 429) or size < TINY_PAGE:
            return DATADOME
    if small and ("captcha-delivery.com" in body or "geo.captcha-delivery" in body):
        return DATADOME

    # --- PerimeterX / HUMAN
    if small and ('id="px-captcha"' in body or "id='px-captcha'" in body or "press & hold" in body
                  or "pulsa y mantén" in body or ("px-cloud.net" in body and status in (403, 429))):
        return PERIMETERX

    # --- Amazon robot check and generic CAPTCHA widgets on a nearly empty page
    if small and any(marker in body for marker in _AMAZON_CAPTCHA):
        return CAPTCHA
    if small and re.search(r'class="[^"]*\b(?:g-recaptcha|h-captcha|cf-turnstile)\b|data-sitekey=', body) and _visible_len(text) < 800:
        return CAPTCHA

    # --- Login wall: the final URL is a login page, or the page title is one
    path = urlsplit(url or "").path
    if _LOGIN_PATH.search(path):
        return LOGIN
    if small and title and _LOGIN_TITLE.match(title):
        return LOGIN
    if status == 401:
        return LOGIN

    # --- Plain HTTP statuses
    if status == 403:
        return HTTP_403
    if status == 429:
        return HTTP_429
    if 500 <= status <= 599:
        return HTTP_5XX
    return ""


def block_hint(reason: str) -> str:
    """Human-readable explanation used in ``FetchResult.error`` and in the UI."""
    return {
        CLOUDFLARE: "Cloudflare challenge page",
        AKAMAI: "Akamai bot-manager page",
        DATADOME: "DataDome challenge",
        PERIMETERX: "PerimeterX / HUMAN challenge",
        CAPTCHA: "CAPTCHA / robot check",
        LOGIN: "login wall",
        HTTP_403: "HTTP 403 forbidden",
        HTTP_429: "HTTP 429 too many requests",
        HTTP_5XX: "server error",
    }.get(reason, reason)


def apply_block(fr: Any, reason: str) -> Any:
    """Fold a ``detect_block`` verdict into a ``FetchResult`` (mutates and returns it).

    A block (anti-bot page, login wall, 403/429) sets ``blocked``; a plain server error only sets
    ``block_reason`` — it is an outage, not somebody refusing us. ``error_kind`` is ``"blocked"``, ``"http"``
    or left alone.
    """
    fr.block_reason = reason
    fr.blocked = bool(reason) and reason != HTTP_5XX
    good_status = 200 <= fr.status < 400
    fr.ok = bool(good_status and not reason and fr.text)
    if reason:
        fr.error = f"blocked: {block_hint(reason)}" if fr.blocked else f"{block_hint(reason)} (HTTP {fr.status})"
        fr.error_kind = "blocked" if fr.blocked else "http"
    elif not good_status:
        fr.error = fr.error or f"HTTP {fr.status}"
        fr.error_kind = fr.error_kind or "http"
    elif not fr.text:
        fr.error = fr.error or "empty response"
    return fr
