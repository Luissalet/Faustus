"""openai_probes.py — capability probes for OpenAI-compatible chat endpoints.

What a server *declares* about a model and what it *does* are different facts,
and "it did not work once" is a third one. The native calibrator
(``src/model_calibration.py``) already keeps declared and tested apart for
Ollama's own API. This module does the same for the ``/chat/completions`` wire
format that most local servers and hosted providers speak, and adds the
distinction the native probes leave implicit:

* ``supported``: the server did the thing (a structured tool call came back,
  the reply was valid JSON, the image was accepted);
* ``unsupported``: the server said it does not do the thing, or answered in a way
  that shows it does not (HTTP 400/415/422 that names the feature, a reply
  without a tool call when one was asked for);
* ``unknown``: nothing was learned (timeout, connection error, 401/403, 404,
  429, 5xx, a 400 that does not name the feature). Never turned into a pass or a
  fail.

Results use the stored ``tested`` shape (``ok`` True / False / None, ``tested_at``,
``evidence``) so they go through the same revisioned writer
(``save_scoped_tested``) as every other probe: under the endpoint id, the
connection revision captured *before* the first request, and the protocol
``openai_chat_completions``. A configuration change while a probe is in flight
files its results under the old revision, where nothing current reads them.

Nothing here sends a credential anywhere but the endpoint the caller resolved,
stores a header, or keeps a prompt longer than a few hundred characters.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx

from src import model_calibration as mcal

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 25.0
BUDGET_S = 90.0
DEFAULT_PROBES: Tuple[str, ...] = (
    mcal.TEST_TOOL_CALLING, mcal.TEST_STREAMING_TOOL_CALLS, mcal.TEST_JSON_MODE,
)
SUPPORTED_PROBES: Tuple[str, ...] = (
    mcal.TEST_TOOL_CALLING, mcal.TEST_STREAMING_TOOL_CALLS, mcal.TEST_JSON_MODE,
    mcal.TEST_VISION, mcal.TEST_CONTEXT_LENGTH_EFFECTIVE,
)

# A rejection only counts as "not supported" when it names the feature.
_FEATURE_WORDS = {
    mcal.TEST_TOOL_CALLING: ("tool", "function"),
    mcal.TEST_STREAMING_TOOL_CALLS: ("tool", "function", "stream"),
    mcal.TEST_JSON_MODE: ("response_format", "json"),
    mcal.TEST_VISION: ("image", "vision", "multimodal", "content type", "image_url", "mmproj"),
}

_WEATHER_TOOL = mcal._WEATHER_TOOL


def _utcnow() -> str:
    return mcal._utcnow_iso()


class _Reply:
    """One HTTP exchange, reduced to what a probe may reason about."""

    def __init__(self, status: Optional[int] = None, body: Any = None, text: str = "",
                 error: str = "", chunks: Optional[List[Dict[str, Any]]] = None):
        self.status = status
        self.body = body
        self.text = text
        self.error = error
        self.chunks = chunks or []


def _error_text(body: Any, text: str) -> str:
    if isinstance(body, Mapping):
        err = body.get("error")
        if isinstance(err, Mapping):
            return str(err.get("message") or err.get("code") or err.get("type") or "")[:300]
        if isinstance(err, str):
            return err[:300]
        if body.get("message"):
            return str(body["message"])[:300]
    return (text or "")[:300]


def _post(client: Any, url: str, headers: Mapping[str, str], payload: Dict[str, Any],
          timeout: float) -> _Reply:
    try:
        response = client.post(url, json=payload, headers=dict(headers), timeout=timeout)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return _Reply(error=type(exc).__name__)
    try:
        body = response.json()
    except ValueError:
        body = None
    return _Reply(status=response.status_code, body=body, text=(response.text or "")[:2000])


def _post_stream(client: Any, url: str, headers: Mapping[str, str], payload: Dict[str, Any],
                 timeout: float) -> _Reply:
    chunks: List[Dict[str, Any]] = []
    try:
        with client.stream("POST", url, json=payload, headers=dict(headers), timeout=timeout) as response:
            if response.status_code >= 400:
                raw = response.read()
                text = (raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw))[:2000]
                try:
                    body = json.loads(text)
                except ValueError:
                    body = None
                return _Reply(status=response.status_code, body=body, text=text)
            for line in response.iter_lines():
                line = line if isinstance(line, str) else (line or b"").decode("utf-8", "ignore")
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    parsed = json.loads(data)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    chunks.append(parsed)
            return _Reply(status=response.status_code, chunks=chunks)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return _Reply(error=type(exc).__name__, chunks=chunks)


def _result(ok: Optional[bool], *, reason: str = "", reply: Optional[_Reply] = None,
            request: Any = None, **extra: Any) -> Dict[str, Any]:
    state = "supported" if ok is True else "unsupported" if ok is False else "unknown"
    evidence: Dict[str, Any] = {"state": state, "protocol": mcal.OPENAI_CHAT_PROTOCOL}
    if reason:
        evidence["reason"] = reason
    if reply is not None:
        if reply.status is not None:
            evidence["status"] = reply.status
        if reply.error:
            evidence["error"] = reply.error
        message = _error_text(reply.body, reply.text) if (reply.status or 0) >= 400 else ""
        if message:
            evidence["server_message"] = mcal._clip(message, 300)
    if request is not None:
        evidence["request"] = mcal._clip(request)
    evidence.update(extra)
    return {"ok": ok, "tested_at": _utcnow(), "evidence": evidence}


def _classify_failure(reply: _Reply, capability: str, *, request: Any) -> Dict[str, Any]:
    """Turn a non-200 (or no) answer into unsupported / unknown, never into a pass."""
    if reply.status is None:
        return _result(None, reason="no response from the server", reply=reply, request=request)
    status = reply.status
    message = _error_text(reply.body, reply.text).lower()
    if status in (400, 415, 422) and any(word in message for word in _FEATURE_WORDS.get(capability, ())):
        return _result(False, reason="the server rejected the request and named the feature",
                       reply=reply, request=request)
    reasons = {
        401: "not authorised (check the key)", 403: "not authorised (check the key)",
        404: "model or route not found", 408: "timed out", 429: "rate limited",
    }
    reason = reasons.get(status) or ("the server failed" if status >= 500 else "the rejection does not say why")
    return _result(None, reason=reason, reply=reply, request=request)


def _first_message(body: Any) -> Dict[str, Any]:
    try:
        message = body["choices"][0]["message"]
        return message if isinstance(message, dict) else {}
    except (KeyError, IndexError, TypeError):
        return {}


def _call_args(call: Mapping[str, Any]) -> Dict[str, Any]:
    fn = call.get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, dict):
        return args
    try:
        parsed = json.loads(args) if isinstance(args, str) else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _base_payload(model: str) -> Dict[str, Any]:
    return {"model": model, "temperature": 0, "max_tokens": 160}


def probe_tool_calling(client: Any, url: str, headers: Mapping[str, str], model: str) -> Dict[str, Any]:
    payload = dict(_base_payload(model), messages=[{
        "role": "user", "content": "What is the weather in Paris right now? Use the get_weather tool."}],
        tools=[_WEATHER_TOOL], tool_choice="auto")
    reply = _post(client, url, headers, payload, PROBE_TIMEOUT_S)
    if reply.status != 200:
        return _classify_failure(reply, mcal.TEST_TOOL_CALLING, request=payload)
    message = _first_message(reply.body)
    for call in message.get("tool_calls") or []:
        if str((call.get("function") or {}).get("name") or "") == "get_weather" \
                and str(_call_args(call).get("city") or "").strip():
            return _result(True, reply=reply, request=payload, response=mcal._clip(message))
    reason = ("the model answered in text instead of returning a tool call"
              if message.get("content") else "the reply carried no tool call")
    return _result(False, reason=reason, reply=reply, request=payload, response=mcal._clip(message))


def probe_streaming_tool_calls(client: Any, url: str, headers: Mapping[str, str], model: str) -> Dict[str, Any]:
    payload = dict(_base_payload(model), stream=True, messages=[{
        "role": "user", "content": "What is the weather in Paris right now? Use the get_weather tool."}],
        tools=[_WEATHER_TOOL], tool_choice="auto")
    reply = _post_stream(client, url, headers, payload, PROBE_TIMEOUT_S)
    if reply.status != 200:
        return _classify_failure(reply, mcal.TEST_STREAMING_TOOL_CALLS, request=payload)
    if reply.error and not reply.chunks:
        return _result(None, reason="the stream broke before any data", reply=reply, request=payload)
    names: Dict[int, str] = {}
    arguments: Dict[int, str] = {}
    chunks_with_calls = 0
    text_seen = False
    for chunk in reply.chunks:
        try:
            delta = chunk["choices"][0].get("delta") or {}
        except (KeyError, IndexError, TypeError, AttributeError):
            continue
        if delta.get("content"):
            text_seen = True
        calls = delta.get("tool_calls") or []
        if calls:
            chunks_with_calls += 1
        for call in calls:
            index = int(call.get("index") or 0)
            fn = call.get("function") or {}
            if fn.get("name"):
                names[index] = str(fn["name"])
            if fn.get("arguments"):
                arguments[index] = arguments.get(index, "") + str(fn["arguments"])
    for index, name in names.items():
        try:
            args = json.loads(arguments.get(index, "") or "{}")
        except ValueError:
            args = {}
        if name == "get_weather" and isinstance(args, dict) and str(args.get("city") or "").strip():
            return _result(True, reply=reply, request=payload, chunks_with_tool_calls=chunks_with_calls)
    if reply.error:
        return _result(None, reason="the stream broke before a tool call was complete", reply=reply,
                       request=payload)
    reason = ("the model streamed text instead of a tool call" if text_seen
              else "the stream carried no complete tool call")
    return _result(False, reason=reason, reply=reply, request=payload, chunks=len(reply.chunks))


def probe_json_mode(client: Any, url: str, headers: Mapping[str, str], model: str) -> Dict[str, Any]:
    payload = dict(_base_payload(model), response_format={"type": "json_object"}, messages=[{
        "role": "user", "content": 'Reply with strict JSON only, no prose: {"ok": true, "n": 2}'}])
    reply = _post(client, url, headers, payload, PROBE_TIMEOUT_S)
    if reply.status != 200:
        return _classify_failure(reply, mcal.TEST_JSON_MODE, request=payload)
    content = str(_first_message(reply.body).get("content") or "")
    try:
        parsed = json.loads(content)
    except ValueError:
        return _result(False, reason="the reply was not valid JSON", reply=reply, request=payload,
                       response=mcal._clip(content, 200))
    if not isinstance(parsed, dict):
        return _result(False, reason="the reply was JSON but not an object", reply=reply, request=payload)
    return _result(True, reply=reply, request=payload, response=mcal._clip(content, 200))


def probe_vision(client: Any, url: str, headers: Mapping[str, str], model: str) -> Dict[str, Any]:
    payload = dict(_base_payload(model), messages=[{"role": "user", "content": [
        {"type": "text", "text": "Describe this image in one short sentence."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + mcal._TINY_PNG_1X1_B64}},
    ]}])
    reply = _post(client, url, headers, payload, PROBE_TIMEOUT_S)
    if reply.status != 200:
        request = {"model": model, "image": "1x1 png"}
        return _classify_failure(reply, mcal.TEST_VISION, request=request)
    content = str(_first_message(reply.body).get("content") or "").strip()
    request = {"model": model, "image": "1x1 png"}
    if not content:
        return _result(None, reason="accepted the image but answered nothing", reply=reply, request=request)
    return _result(True, reply=reply, request=request, response=mcal._clip(content, 200))


def probe_context(client: Any, url: str, headers: Mapping[str, str], model: str, *,
                  announced_limit: int = 0) -> Dict[str, Any]:
    target = next((t for t in mcal._CONTEXT_TARGETS if announced_limit and announced_limit >= t + 512), None)
    if target is None:
        result = _result(None, reason="no declared context length that clears 8k with headroom")
        result["evidence"]["skipped"] = "context length not declared"
        return result
    needle = uuid.uuid4().hex[:12]
    unit = "the quick brown fox jumps over the lazy dog. "
    filler = (unit * ((target * 4) // len(unit) + 1))[: target * 4]
    content = (f"{filler}\nThe secret code is {needle}.\n"
               "What is the secret code mentioned above? Respond with only the code, nothing else.")
    payload = dict(_base_payload(model), messages=[{"role": "user", "content": content}])
    reply = _post(client, url, headers, payload, PROBE_TIMEOUT_S * 2)
    request = {"model": model, "target_tokens": target}
    if reply.status != 200:
        return _classify_failure(reply, "context", request=request)
    found = needle in str(_first_message(reply.body).get("content") or "")
    return _result(found, reason="" if found else "the model did not return the code placed at the end",
                   reply=reply, request=request, target_tokens=target, needle_found=found)


_RUNNERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    mcal.TEST_TOOL_CALLING: probe_tool_calling,
    mcal.TEST_STREAMING_TOOL_CALLS: probe_streaming_tool_calls,
    mcal.TEST_JSON_MODE: probe_json_mode,
    mcal.TEST_VISION: probe_vision,
}


def run_probes(client: Any, chat_url: str, headers: Mapping[str, str], model: str, *,
               include: Optional[Sequence[str]] = None, announced_context: int = 0,
               deadline_s: float = BUDGET_S) -> Dict[str, Dict[str, Any]]:
    """Run the chosen probes against one resolved chat URL.

    A probe not started before the budget is gone is recorded ``unknown``
    ("time budget exceeded"), never missing and never a pass or a fail. Unknown
    probe names are ignored here; the caller reports them.
    """
    chosen = [name for name in (include or DEFAULT_PROBES) if name in SUPPORTED_PROBES]
    start = time.monotonic()
    out: Dict[str, Dict[str, Any]] = {}
    for name in dict.fromkeys(chosen):
        if deadline_s - (time.monotonic() - start) <= 0:
            skipped = _result(None, reason="time budget exceeded")
            skipped["evidence"]["skipped"] = "time budget exceeded"
            out[name] = skipped
            continue
        if name == mcal.TEST_CONTEXT_LENGTH_EFFECTIVE:
            out[name] = probe_context(client, chat_url, headers, model, announced_limit=announced_context)
        else:
            out[name] = _RUNNERS[name](client, chat_url, headers, model)
    return out


# ---------------------------------------------------------------- endpoint --

class ProbeRefused(Exception):
    """The request cannot be probed; ``status`` is the HTTP status a route uses."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _declared_for(endpoint_id: str) -> Dict[str, Any]:
    """What the operator declared for the endpoint (today: native tool support)."""
    declared: Dict[str, Any] = {}
    try:
        from core.database import ModelEndpoint, SessionLocal
        db = SessionLocal()
        try:
            ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
            flag = getattr(ep, "supports_tools", None) if ep else None
        finally:
            db.close()
        if isinstance(flag, bool):
            declared["tools"] = flag
    except Exception:  # noqa: BLE001 - a declaration that cannot be read is no declaration
        logger.debug("openai probes: declared capabilities unreadable", exc_info=True)
    return {"capabilities": declared, "source": "endpoint_setting" if declared else "none"}


def resolve_target(endpoint_id: str, model: str, owner: Optional[str]) -> Tuple[str, Dict[str, str], str]:
    """``(chat_url, headers, connection_revision)`` from one read of the endpoint.

    The revision is the one belonging to the URL and credentials that will be
    used; it is captured here, before any request is made.
    """
    from src import endpoint_resolver
    resolved = endpoint_resolver._resolve_endpoint_by_id_with_descriptor(
        endpoint_id, model, owner=owner, require_exact_model=True)
    if not resolved:
        raise ProbeRefused("endpoint or model not available (disabled, unknown, hidden or not listed)", 404)
    (chat_url, resolved_model, headers), descriptor = resolved
    if resolved_model != model:
        raise ProbeRefused("the endpoint resolved a different model than the one asked for", 409)
    revision = str(descriptor.get("connection_revision") or "")
    if not revision:
        raise ProbeRefused("probing requires a persisted endpoint configuration", 409)
    return chat_url, dict(headers or {}), revision


def probe_endpoint(endpoint_id: str, model: str, *, owner: Optional[str] = None,
                   include: Optional[Sequence[str]] = None, save: bool = True,
                   client_factory: Optional[Callable[[float], Any]] = None,
                   data_dir: Optional[str] = None) -> Dict[str, Any]:
    """Probe one model of one endpoint and file the results under its revision.

    Blocking (it makes HTTP calls); routes run it in a thread. Raises
    :class:`ProbeRefused` for a request that cannot be probed.
    """
    model = str(model or "").strip()
    if not endpoint_id or not model:
        raise ProbeRefused("endpoint_id and model are required", 400)
    unknown = [name for name in (include or ()) if name not in SUPPORTED_PROBES]
    if unknown:
        raise ProbeRefused(f"unknown probe(s): {', '.join(unknown)}; choose from {', '.join(SUPPORTED_PROBES)}", 400)
    chat_url, headers, revision = resolve_target(endpoint_id, model, owner)
    protocol = mcal.explicit_openai_protocol(chat_url)
    if not protocol:
        raise ProbeRefused("this endpoint does not speak the OpenAI chat-completions protocol "
                           "(its native API has its own calibration)", 422)
    declared = _declared_for(endpoint_id)
    announced_context = 0
    factory = client_factory or (lambda timeout: httpx.Client(timeout=timeout))
    with factory(PROBE_TIMEOUT_S) as client:
        tested = run_probes(client, chat_url, headers, model, include=include,
                            announced_context=announced_context)
    identity = dict(vendor=mcal.OPENAI_COMPATIBLE_VENDOR, model_id=model, endpoint_id=endpoint_id,
                    protocol=protocol, endpoint_revision=revision)
    if save:
        manifest = mcal.save_scoped_tested(**identity, tested=tested, announced=declared, data_dir=data_dir)
    else:
        manifest = {"announced": declared, "tested": tested,
                    "degraded": mcal.compute_degraded(declared, tested),
                    "calibration_scope": None, "evidence_scope": "unsaved"}
    return {"endpoint_id": endpoint_id, "model": model, "protocol": protocol,
            "endpoint_revision": revision, "saved": bool(save), "manifest": manifest,
            "capabilities": mcal.capability_states(manifest)}


def read_endpoint(endpoint_id: str, model: str, *, owner: Optional[str] = None,
                  data_dir: Optional[str] = None) -> Dict[str, Any]:
    """Declared vs probed for one model of one endpoint, from the store. No network.

    Reads under the endpoint's *current* connection revision: what was probed
    before the endpoint was reconfigured is not shown as current.
    """
    model = str(model or "").strip()
    if not endpoint_id or not model:
        raise ProbeRefused("endpoint_id and model are required", 400)
    chat_url, _headers, revision = resolve_target(endpoint_id, model, owner)
    protocol = mcal.explicit_openai_protocol(chat_url)
    if not protocol:
        raise ProbeRefused("this endpoint does not speak the OpenAI chat-completions protocol", 422)
    declared = _declared_for(endpoint_id)
    manifest = mcal.get_effective_manifest(
        vendor=mcal.OPENAI_COMPATIBLE_VENDOR, model_id=model, endpoint_id=endpoint_id,
        protocol=protocol, endpoint_revision=revision, data_dir=data_dir)
    # the operator's declaration is read live; the stored one may be older
    if declared["capabilities"]:
        manifest = dict(manifest, announced=declared)
    return {"endpoint_id": endpoint_id, "model": model, "protocol": protocol,
            "endpoint_revision": revision, "saved": manifest.get("evidence_scope") == "exact",
            "manifest": manifest, "capabilities": mcal.capability_states(manifest)}


def probe_url(base_url: str, model: str, *, api_key: str = "",
              include: Optional[Sequence[str]] = None,
              client_factory: Optional[Callable[[float], Any]] = None) -> Dict[str, Any]:
    """Probe a server by address without touching any stored endpoint.

    Nothing is saved (there is no endpoint identity or revision to file it
    under): the answer is what the server did just now. Used to check a server
    before adding it and by ``scripts/probe_openai_endpoint.py``.
    """
    from src import endpoint_resolver
    model = str(model or "").strip()
    base_url = str(base_url or "").strip()
    if not base_url or not model:
        raise ProbeRefused("base_url and model are required", 400)
    unknown = [name for name in (include or ()) if name not in SUPPORTED_PROBES]
    if unknown:
        raise ProbeRefused(f"unknown probe(s): {', '.join(unknown)}; choose from {', '.join(SUPPORTED_PROBES)}", 400)
    chat_url = endpoint_resolver.build_chat_url(base_url)
    protocol = mcal.explicit_openai_protocol(chat_url)
    if not protocol:
        raise ProbeRefused("this address does not speak the OpenAI chat-completions protocol", 422)
    headers = endpoint_resolver.build_headers(api_key or None, base_url)
    factory = client_factory or (lambda timeout: httpx.Client(timeout=timeout))
    with factory(PROBE_TIMEOUT_S) as client:
        tested = run_probes(client, chat_url, headers, model, include=include)
    manifest = {"announced": {"capabilities": {}, "source": "none"}, "tested": tested,
                "degraded": [], "calibration_scope": None, "evidence_scope": "unsaved"}
    return {"endpoint_id": "", "model": model, "protocol": protocol, "endpoint_revision": "",
            "saved": False, "manifest": manifest, "capabilities": mcal.capability_states(manifest)}
