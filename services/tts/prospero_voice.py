"""Prospero's Hoard as a speech provider (`tts_provider = "prospero"`).

Prospero's Hoard keeps a registry of local voice engines (KittenTTS on the CPU,
Piper, and the optional cloning engines). This provider sends the text to its
`POST /api/voice/speak` and returns the WAV it answers with, so Faustus reads
aloud with whatever voice engine that Hoard has installed, without a second
copy of the engine inside Faustus.

Loopback only: the address is read from `tts_prospero_url` and anything that is
not the local machine is refused, so this can never be pointed at a stranger's
server with the user's text.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8815"
DEFAULT_ENGINE = "kittentts"
_TIMEOUT_S = 60.0


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def base_url(value: Optional[str] = None) -> str:
    """The Hoard's base URL, or "" when it is not a loopback http(s) address."""
    raw = (value or DEFAULT_URL).strip().rstrip("/") or DEFAULT_URL
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname or not _loopback(parts.hostname):
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _get(url: str, timeout: float = 5.0) -> Optional[Any]:
    import httpx
    try:
        r = httpx.get(url, timeout=timeout, trust_env=False)
        if r.status_code == 200:
            return r.json()
    except Exception as exc:  # noqa: BLE001 - an unreachable Hoard is a state, not an error
        logger.info("Prospero voice: %s unreachable (%s)", url, exc)
    return None


def engines(url: Optional[str] = None) -> Dict[str, Any]:
    """Voice engines the Hoard reports, `{"reachable", "engines": [{id, name, ready}]}`. Never raises."""
    base = base_url(url)
    if not base:
        return {"reachable": False, "engines": [], "reason": "tts_prospero_url must be a loopback address"}
    data = _get(f"{base}/api/voice/engines")
    if data is None:
        return {"reachable": False, "engines": [], "reason": "Prospero's Hoard is not answering"}
    items = data.get("tts") if isinstance(data, dict) else data
    out: List[Dict[str, Any]] = []
    for e in items if isinstance(items, list) else []:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or "").strip()
        if not eid:
            continue
        out.append({"id": eid, "name": str(e.get("label") or eid), "ready": bool(e.get("installed")),
                    "cloning": bool((e.get("capabilities") or {}).get("cloning")),
                    "reason": str(e.get("reason") or "")})
    return {"reachable": True, "engines": out}


def synthesize_prospero(text: str, *, url: Optional[str] = None, engine: str = DEFAULT_ENGINE,
                        voice: str = "", speed: float = 1.0, language: str = "") -> Optional[bytes]:
    """WAV bytes for `text`, or None when the Hoard is down, refuses the engine, or the address is not local."""
    base = base_url(url)
    if not base:
        logger.error("Prospero TTS: tts_prospero_url is not a loopback address")
        return None
    spec: Dict[str, Any] = {"engine_id": (engine or DEFAULT_ENGINE).strip()}
    if voice and voice not in ("alloy",):  # "alloy" is the generic default of the other providers
        spec["voice_ref"] = voice
    if speed and abs(speed - 1.0) > 1e-6:
        spec["speed"] = speed
    if language:
        spec["language"] = language
    import httpx

    def _post(body_spec: Dict[str, Any]):
        return httpx.post(f"{base}/api/voice/speak", json={"text": text, "voice": body_spec},
                          timeout=_TIMEOUT_S, trust_env=False)

    try:
        r = _post(spec)
        if r.status_code in (400, 422) and "voice_ref" in spec:
            # tts_voice is shared by every provider: a name left over from Kokoro or Piper is not a
            # reference this Hoard knows, and it answers 400. Speak with the engine's own voice instead
            # of staying silent.
            logger.info("Prospero TTS: voice %r refused (HTTP %s); speaking with the engine's default",
                        spec["voice_ref"], r.status_code)
            r = _post({k: v for k, v in spec.items() if k != "voice_ref"})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Prospero TTS request failed: %s", exc)
        return None
    if r.status_code != 200 or not r.content:
        logger.warning("Prospero TTS: HTTP %s %s", r.status_code, r.text[:200])
        return None
    return r.content
