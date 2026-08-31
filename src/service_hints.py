"""Actionable fix hints for degraded services.

FAUSTUS addition. Upstream's roadmap asks for "better degraded-state reporting
for ChromaDB, SearXNG, email, ntfy, and provider probes". `src/service_health`
answers *what* is broken; a status word on its own still leaves you guessing.
This module answers *what to do about it*: one concrete sentence plus, where a
single command fixes it, the command itself.

Design:

- **Pure and table-driven.** `hint_for` takes a probe dict exactly as
  `service_health` emits it and returns a plain dict — no I/O, no imports of
  live managers, so it unit-tests instantly and can never wedge the endpoint.
- **Category-keyed, with a fallback.** Probes already classify failures into
  secret-free categories (`connection_refused`, `no_models`, `timeout`, …).
  Hints key off the same tokens so a new category degrades to the service's
  `"*"` entry instead of raising.
- **No secrets.** Hints are static text; nothing here interpolates a URL,
  account name, or key from the probe meta.
"""

from typing import Any, Dict, List, Optional

OK = "ok"
DISABLED = "disabled"

# Commands assume the layout this fork runs on (ChromaDB in Docker, Ollama on
# the host). They are suggestions shown next to a Copy button, never executed.
_TABLE: Dict[str, Dict[str, Dict[str, str]]] = {
    "chromadb": {
        "not_initialized": {
            "text": "The vector stores were never created — ChromaDB was down when "
                    "Faustus started, so document RAG and vector memory are "
                    "keyword-only for this whole run. Start ChromaDB, then restart "
                    "Faustus; Reconnect cannot build a store that does not exist yet.",
            "command": "docker start odysseus-chromadb",
        },
        "partial": {
            "text": "One vector store lost its collection. Reconnect re-initializes "
                    "both in place — no restart needed.",
            "command": "",
        },
        "*": {
            "text": "ChromaDB is not answering, so document RAG and vector memory "
                    "silently fall back to keyword matching. Start the container, "
                    "then press Reconnect.",
            "command": "docker start odysseus-chromadb",
        },
    },
    "searxng": {
        "no_host": {
            "text": "No SearXNG instance is configured, so web search and Deep "
                    "Research have nowhere to query. Set the instance URL in "
                    "Settings, or pick another search provider.",
            "command": "",
        },
        "*": {
            "text": "SearXNG is not answering, so web search and Deep Research "
                    "return nothing. Start the container, or switch the search "
                    "provider in Settings.",
            "command": "docker compose up -d searxng",
        },
    },
    "providers": {
        "no_models": {
            "text": "The endpoint answered but listed no models. Check the base URL "
                    "(the OpenAI-compatible path ends in /v1) and that the model is "
                    "actually pulled.",
            "command": "ollama list",
        },
        "timeout": {
            "text": "The endpoint timed out. A model still being loaded into VRAM "
                    "does this — check what is resident and retry.",
            "command": "ollama ps",
        },
        "connection_refused": {
            "text": "The model endpoint refused the connection. If it is the local "
                    "Ollama, it is not running.",
            "command": "ollama serve",
        },
        "auth_or_protocol_error": {
            "text": "The endpoint rejected the request. Check the API key and base "
                    "URL for that endpoint in Settings.",
            "command": "",
        },
        "http_error": {
            "text": "The endpoint returned an error response. Check the base URL and "
                    "the key for that endpoint in Settings.",
            "command": "",
        },
        "*": {
            "text": "A model endpoint is unreachable. Chat falls back to the other "
                    "endpoints, if any are configured.",
            "command": "",
        },
    },
    "email": {
        "auth_or_protocol_error": {
            "text": "The mail server refused the login. Re-enter that account's app "
                    "password in Settings.",
            "command": "",
        },
        "*": {
            "text": "A mail account is unreachable, so its inbox will not refresh "
                    "and reminders by email will not go out.",
            "command": "",
        },
    },
    "ntfy": {
        "*": {
            "text": "ntfy is unreachable, so push reminders will not arrive. Check "
                    "the server and topic in Settings, or switch the reminder "
                    "channel to the browser.",
            "command": "",
        },
    },
}


def _first_item_error(meta: Dict[str, Any]) -> Optional[str]:
    """Return the first failing item's error category in a fan-out probe."""
    for key in ("endpoints", "accounts"):
        items = meta.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and not item.get("ok"):
                    err = item.get("error")
                    if err:
                        return str(err)
    return None


def _category(name: str, status: str, meta: Dict[str, Any]) -> str:
    """Map one probe result to the hint key that explains it."""
    if name == "chromadb":
        rag = meta.get("rag")
        mem = meta.get("memory")
        if rag is None and mem is None:
            return "not_initialized"
        if bool(rag) != bool(mem) and None not in (rag, mem):
            return "partial"
        return "*"
    err = meta.get("error")
    if not err:
        err = _first_item_error(meta)
    return str(err) if err else "*"


def hint_for(service: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Return {"text", "command"} for a failing probe, or None when nothing to say.

    `ok` and `disabled` never produce a hint: a turned-off feature is not a
    problem to fix, and nagging about it is how a status panel gets ignored.
    """
    if not isinstance(service, dict):
        return None
    status = service.get("status")
    if status == OK or status is None:
        return None
    name = str(service.get("name") or "")
    # `disabled` normally means "you turned it off" — nothing to fix. ChromaDB
    # is the exception: it reports disabled when the stores were never created,
    # which is exactly the case where retrieval quietly went keyword-only.
    if status == DISABLED and name != "chromadb":
        return None
    table = _TABLE.get(name)
    if not table:
        return None
    meta = service.get("meta") if isinstance(service.get("meta"), dict) else {}
    entry = table.get(_category(name, status, meta)) or table.get("*")
    if not entry:
        return None
    return {"text": entry["text"], "command": entry.get("command", "")}


def attach_hints(report: Dict[str, Any]) -> Dict[str, Any]:
    """Add a `hint` to every failing service in a health report, in place.

    Services that are ok/disabled are left untouched (no `hint` key at all), so
    a client can treat "has a hint" as "needs attention".
    """
    if not isinstance(report, dict):
        return report
    services: List[Any] = report.get("services") or []
    for svc in services:
        hint = hint_for(svc)
        if hint:
            svc["hint"] = hint
    return report
