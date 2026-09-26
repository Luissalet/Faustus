# src/chat_helpers.py
"""URL extraction, message/upload validation, request parsing."""

import re
import os
import json
import time
import ipaddress
import logging
import httpx
from urllib.parse import urlparse
from fastapi import HTTPException
from fastapi import UploadFile
from typing import List, Optional

from src.upload_limits import format_byte_limit, get_chat_upload_max_bytes

logger = logging.getLogger(__name__)


def extract_urls(text: str) -> List[str]:
    """Extract URLs from text using regex pattern."""
    url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    urls = re.findall(url_pattern, text)
    cleaned_urls = []
    for url in urls:
        # Strip trailing sentence punctuation, but keep a balanced ')' so URLs
        # that legitimately end in one are preserved, e.g. the Wikipedia link
        # ".../Python_(programming_language)". A ')' is only dropped when it is
        # unbalanced (more ')' than '('), which is the prose-glued case such as
        # "(see https://example.com)".
        url = re.sub(r'[.,;:!?]+$', '', url)
        while url.endswith(')') and url.count(')') > url.count('('):
            url = re.sub(r'[.,;:!?]+$', '', url[:-1])
        cleaned_urls.append(url)
    return cleaned_urls


# Model-name substrings that signal native image input. A missed match here
# silently drops the image from the chat request (it gets swapped for a text
# caption), so the model never sees it. Keep this broad, especially for local
# models (Ollama/llama.cpp) that ship under many names. See issue #124.
_VISION_MODEL_KEYWORDS = (
    # hosted
    "gpt-4o", "gpt-4.1", "gpt-4.5", "gpt-4-turbo", "gpt-4-vision",
    "claude-sonnet", "claude-opus", "claude-haiku", "gemini",
    # open / local
    "vision", "multimodal", "llava", "bakllava", "moondream", "pixtral", "minicpm",
    "internvl", "cogvlm", "qwen-vl", "qwen2-vl", "qwen3-vl", "qwen3vl",
    # multimodal families whose names don't contain "vision"/"vl" but DO accept
    # images — without these the image is silently dropped for common Ollama tags
    # like gemma3:4b or gemma4:12b (issue #1274). Gemma 3/4 (4b+), Llama 4 (all),
    # Mistral Small 3.1/3.2, and Phi-4 multimodal are vision-capable; per the
    # err-toward-True policy (#124) a rare text-only tag being treated as vision is
    # the safer failure than silently dropping a real image.
    "gemma-3", "gemma3", "gemma-4", "gemma4",
    "llama-4", "llama4",
    "mistral-small-3.1", "mistral-small3.1", "mistral-small-3.2", "mistral-small3.2",
    # Microsoft Phi-4 ships a dedicated multimodal variant ("phi-4-multimodal-instruct")
    # but users often load it under the bare "phi-4" or "phi4" Ollama tag.
    "phi-4", "phi4",
    # zhipu / glm (glm-4.5v, glm-4.6v, glm-5v-turbo, etc.)
    "glm-4.5v", "glm-4.6v", "glm-5v",
    # Qwen 3.5 / 3.8 are natively multimodal (Ollama /api/show reports
    # "vision" for qwen3.5:9b). The Ollama probe below is the authority when
    # the server is reachable; this is the safety net for other runners.
    "qwen3.5", "qwen3.8",
)
# Catches the "*-VL-*" / "*VL*" family not covered by a literal keyword above
# (e.g. Qwen2.5-VL and various tags): a standalone "vl" token, plus "vlm".
_VISION_VL_RE = re.compile(r'(?<![a-z])vl(?![a-z])|vlm')


def is_vision_model(model_name: str) -> bool:
    """Best-effort check of whether a model can natively accept images.

    Decides whether image attachments get passed through to the model or
    swapped for a separate caption. Err toward True, since a false negative
    drops the image entirely. See issue #124.
    """
    m = (model_name or "").lower()
    if any(kw in m for kw in _VISION_MODEL_KEYWORDS):
        return True
    return bool(_VISION_VL_RE.search(m))


_PROVIDER_FINGERPRINT_TTL = 60.0
# (host, port) -> (models_list | None, expiry); list = LM Studio, None = not LM Studio.
_lmstudio_models_cache: dict = {}


def _is_local_host(host: Optional[str]) -> bool:
    """True for loopback/LAN/Tailscale hosts (never public domains)."""
    host = (host or "").lower()
    if not host:
        return False
    if host in {"localhost", "host.docker.internal"} or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    return ip in ipaddress.ip_network("100.64.0.0/10")


def _probe_lmstudio_models(url: str) -> Optional[list]:
    """Return LM Studio's native /api/v1/models list, or None when the endpoint
    isn't LM Studio or is unreachable (short-TTL cached; transient errors uncached)."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    key = (host, parsed.port)
    now = time.time()
    cached = _lmstudio_models_cache.get(key)
    if cached is not None and cached[1] > now:
        return cached[0]
    authority = host if parsed.port is None else f"{host}:{parsed.port}"
    probe_url = f"{parsed.scheme or 'http'}://{authority}/api/v1/models"
    try:
        r = httpx.get(probe_url, timeout=1.0)
    except Exception:
        return None
    try:
        data = r.json() if r.is_success else {}
    except Exception:
        data = {}
    models = data.get("models")
    valid = (
        isinstance(models, list) and bool(models)
        and isinstance(models[0], dict)
        and "key" in models[0] and "architecture" in models[0]
    )
    models = models if valid else None
    _lmstudio_models_cache[key] = (models, now + _PROVIDER_FINGERPRINT_TTL)
    return models


def lmstudio_supports_vision(url: str, model: str) -> Optional[bool]:
    """Read `model`'s capabilities.vision flag from LM Studio, or None when the
    endpoint isn't LM Studio or doesn't report it (so callers fall back)."""
    if not model:
        return None
    # Never probe a remote provider; LM Studio is always a local/LAN host.
    if not _is_local_host(urlparse(url).hostname):
        return None
    models = _probe_lmstudio_models(url)
    if not models:
        return None
    want = model.strip().lower()
    for m in models:
        if not isinstance(m, dict):
            continue
        names = {str(m.get("key", "")).lower(), str(m.get("display_name", "")).lower()}
        if want in names:
            caps = m.get("capabilities")
            if isinstance(caps, dict) and "vision" in caps:
                return bool(caps.get("vision"))
            return None
    return None


_llamacpp_props_cache: dict = {}


def llamacpp_supports_vision(url: str) -> Optional[bool]:
    """llama.cpp's `/props` reports `modalities.vision` (true only when the
    server was started with a projector, `--mmproj`). None when the endpoint
    is not a local llama-server or does not say. Seen live: a qwen3.x
    GGUF served without a projector passed the name check, a tool result
    image was attached, and the server answered HTTP 500 "image input is not
    supported", ending the turn with no answer."""
    parsed = urlparse(url or "")
    host = parsed.hostname or ""
    if not _is_local_host(host):
        return None
    key = (host, parsed.port)
    now = time.time()
    cached = _llamacpp_props_cache.get(key)
    if cached is not None and cached[1] > now:
        return cached[0]
    authority = host if parsed.port is None else f"{host}:{parsed.port}"
    try:
        r = httpx.get(f"{parsed.scheme or 'http'}://{authority}/props", timeout=3.0)
    except Exception:
        # A server busy generating can take longer than the probe (live,
        # exam run 31: the 27B mid-round). What it was a minute ago is a far
        # better answer than none, which fell through to Ollama's /api/show
        # on a llama-server and then to the family name ("qwen3.8" reads as
        # multimodal) and sent images to a text-only model.
        return cached[0] if cached is not None else None
    try:
        data = r.json() if r.is_success else {}
    except Exception:
        data = {}
    if isinstance(data, dict) and any(k in data for k in ("default_generation_settings", "modalities", "chat_template")):
        _llamacpp_identity[key] = True
    modalities = data.get("modalities") if isinstance(data, dict) else None
    answer = bool(modalities.get("vision")) if isinstance(modalities, dict) and "vision" in modalities else None
    if answer is None and cached is not None:
        answer = cached[0]
    _llamacpp_props_cache[key] = (answer, now + _PROVIDER_FINGERPRINT_TTL)
    return answer


#: host:port pairs that answered llama-server's /props at least once in this
#: process. A server does not change kind while running, so this is kept.
_llamacpp_identity: dict = {}


def is_known_llamacpp(url: str) -> bool:
    """Whether this local endpoint has identified itself as a llama-server."""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return False
    if _llamacpp_identity.get((parsed.hostname or "", parsed.port)):
        return True
    try:
        # A managed engine (src.engines) is always a llama-server; known
        # without any network, so a busy server is still recognised.
        from src.engine_swap import engine_for_url
        return engine_for_url(url) is not None
    except Exception:  # noqa: BLE001
        return False


_llamacpp_effort_cache: dict = {}
_EFFORT_CHECK_RE = re.compile(
    r"reasoning_effort[^\n]{0,400}?\bnot\s+in\s+\(([^)]*)\)", re.IGNORECASE | re.DOTALL)
_EFFORT_DEFAULT_RE = re.compile(r"reasoning_effort\s*\|\s*default\(\s*['\"]([\w-]+)['\"]")


def llamacpp_reasoning_efforts(url: str) -> Optional[tuple]:
    """The reasoning-effort values the loaded chat template accepts, read
    from llama-server's `/props` (`chat_template`), default first; None when
    the endpoint is not a local llama-server or its template does not check
    the value. Seen live: Qwen3.8's template accepts only xhigh/medium/low and
    answers "high" with HTTP 500 ("Unexpected reasoning effort high")."""
    parsed = urlparse(url or "")
    host = parsed.hostname or ""
    if not _is_local_host(host):
        return None
    key = (host, parsed.port)
    now = time.time()
    cached = _llamacpp_effort_cache.get(key)
    if cached is not None and cached[1] > now:
        return cached[0]
    authority = host if parsed.port is None else f"{host}:{parsed.port}"
    try:
        r = httpx.get(f"{parsed.scheme or 'http'}://{authority}/props", timeout=1.0)
        data = r.json() if r.is_success else {}
    except Exception:
        return None
    template = str((data or {}).get("chat_template") or "") if isinstance(data, dict) else ""
    answer = parse_reasoning_efforts(template)
    _llamacpp_effort_cache[key] = (answer, now + _PROVIDER_FINGERPRINT_TTL)
    return answer


def parse_reasoning_efforts(template: str) -> Optional[tuple]:
    """The values a chat template accepts for `reasoning_effort`, its default
    first, or None when the template does not restrict them."""
    m = _EFFORT_CHECK_RE.search(template or "")
    if not m:
        return None
    values = re.findall(r"['\"]([\w-]+)['\"]", m.group(1))
    if not values:
        return None
    d = _EFFORT_DEFAULT_RE.search(template)
    default = d.group(1) if d and d.group(1) in values else None
    if default:
        values = [default] + [v for v in values if v != default]
    return tuple(dict.fromkeys(values))


def _is_local_ollama_url(url: str) -> bool:
    """A local Ollama server, on its native `/api` surface or its OpenAI
    `/v1` surface (both answer `/api/show`). Never a public host."""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return False
    host = parsed.hostname or ""
    if not _is_local_host(host):
        return False
    path = (parsed.path or "").rstrip("/")
    if parsed.port == 11434:
        return True
    if is_known_llamacpp(url):
        # A llama-server on /v1 is not an Ollama: its /api/chat and /api/show
        # answer 404 (exam run 31: vision calls rewritten to 8081/api/chat).
        return False
    return path in ("", "/api", "/v1") or path.startswith("/api/") or path.startswith("/v1/")


def ollama_supports_vision(url: str, model: str) -> Optional[bool]:
    """Read `model`'s "vision" capability from Ollama's `/api/show` (through
    the cache in src.llm_core), or None when the endpoint is not a local
    Ollama, the server is unreachable, or it does not report capabilities."""
    if not model or not _is_local_ollama_url(url):
        return None
    try:
        from src.llm_core import _ollama_model_caps

        caps = _ollama_model_caps(url, model)
    except Exception as exc:  # noqa: BLE001 - never let a probe break a turn
        logger.debug("ollama vision probe failed for %s: %s", model, exc)
        return None
    # None = unknown (server down / model missing); an EMPTY set means an
    # Ollama too old to report capabilities at all — both fall back to the
    # name heuristic rather than declaring a real vision model text-only.
    if not caps:
        return None
    return "vision" in caps


def model_supports_vision(model_name: str, endpoint_url: str = "") -> bool:
    """Whether a model accepts images, using the endpoint's reported
    capability when available (LM Studio, Ollama `/api/show`) and falling
    back to name-based detection otherwise.

    The server's answer wins because names lie in both directions:
    `qwen3.5:9b` reports vision from Ollama but nothing in the name says so,
    and a text-only tag can carry "vl" in its name.
    """
    # The official Claude Code client route (src/cli_model.py) is not an HTTP
    # endpoint the probes below can reach, and its pinned model is usually the
    # opaque "client-default" — a name the keyword heuristic below would call
    # text-only. That route now forwards image blocks itself (Claude Code's
    # `--input-format stream-json`), so it is vision-capable independent of
    # both the probe and the model name; the Codex route is deliberately not
    # included here because cli_model.complete still refuses its images.
    try:
        from src.cli_model import PREFIX as _CLI_PREFIX
        if str(endpoint_url or '').lower().startswith(f'{_CLI_PREFIX}claude/'):
            return True
    except ImportError:  # pragma: no cover - cli_model always ships with this module
        pass
    if endpoint_url:
        try:
            advertised = lmstudio_supports_vision(endpoint_url, model_name or "")
        except Exception:
            advertised = None
        if advertised is not None:
            return advertised
        try:
            advertised = llamacpp_supports_vision(endpoint_url)
        except Exception:
            advertised = None
        if advertised is not None:
            return advertised
        if is_known_llamacpp(endpoint_url):
            # A llama-server that would not say this time: no projector
            # confirmed means no images, never the family name's guess.
            return False
        try:
            advertised = ollama_supports_vision(endpoint_url, model_name or "")
        except Exception:
            advertised = None
        if advertised is not None:
            return advertised
    return is_vision_model(model_name)


def validate_message(message: str) -> str:
    """Validate message input."""
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    message = message.strip()
    if len(message) == 0:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    if len(message) > 50000:
        raise HTTPException(status_code=400, detail="Message exceeds maximum length")

    return message


def validate_file_upload(file: UploadFile) -> UploadFile:
    """Validate uploaded file meets requirements."""
    if not file or not file.filename:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_FILE",
                "message": "No file uploaded or invalid filename"
            }
        )

    try:
        file.file.seek(0, 2)
        file_size = file.file.tell()
        file.file.seek(0)

        if file_size == 0:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "EMPTY_FILE",
                    "message": "File is empty"
                }
            )

        upload_limit = get_chat_upload_max_bytes()
        if file_size > upload_limit:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "FILE_TOO_LARGE",
                    "message": f"File size exceeds {format_byte_limit(upload_limit)} limit"
                }
            )
    except IOError as e:
        logger.error(f"Error reading file size for {file.filename}: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "error": "FILE_READ_ERROR",
                "message": "Error reading uploaded file"
            }
        )

    allowed_extensions = {'.txt', '.py', '.html', '.md', '.json', '.csv', '.js',
                         '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.pdf',
                         '.webm', '.wav', '.mp3', '.m4a', '.ogg'}

    _, ext = os.path.splitext(file.filename.lower())

    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "UNSUPPORTED_FILE_TYPE",
                "message": f"File type '{ext}' not allowed",
                "allowed_types": sorted(allowed_extensions)
            }
        )

    return file


def coerce_message_and_session(req_json: dict | None, message: str | None,
                               session: str | None, session_manager,
                               allow_empty: bool = False):
    """Extract message and session from request, with validation.

    If allow_empty=True (e.g. attachment-only sends), the message-required
    check is skipped and an empty/whitespace message is normalized to "".
    """
    try:
        if message is None or session is None:
            if req_json is None and (session is None or not allow_empty):
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "MISSING_PARAMETERS",
                        "message": "Missing 'message' and/or 'session' in request"
                    }
                )
            if req_json is not None:
                message = message if message is not None else req_json.get("message")
                session = session if session is not None else req_json.get("session")

        if allow_empty and (message is None or not str(message).strip()):
            message = ""
        else:
            message = validate_message(message)

        if not session:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "VALIDATION_ERROR",
                    "message": "Session ID is required"
                }
            )
        try:
            session_manager.get_session(session)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "SESSION_NOT_FOUND",
                    "message": f"Session '{session}' not found"
                }
            )

        return message, session
    except HTTPException:
        raise
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {e}")
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_JSON",
                "message": "Invalid JSON in request body"
            }
        )
    except Exception as e:
        logger.error(f"Unexpected error in coerce_message_and_session: {e}")
        raise HTTPException(
            status_code=400,
            detail={
                "error": "REQUEST_PROCESSING_ERROR",
                "message": "Error processing request"
            }
        )
