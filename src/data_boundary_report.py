"""On-demand configuration inventory. No probes, content reads or policy changes."""
from __future__ import annotations

import ipaddress
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def destination(value: str, kind: str = "auto") -> dict:
    """Classify the configured hop, without DNS; only loopback is this machine.

    privacy_policy.is_local_destination intentionally includes private networks
    for its existing policy. An inventory must distinguish those from loopback.
    Paths, credentials, queries and fragments never enter the report.
    """
    try:
        raw = str(value or "").strip()
        parsed = urlsplit(raw if "://" in raw else "//" + raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
        if not host or parsed.scheme not in ("", "http", "https", "ws", "wss"):
            return {"address": "", "scope": "unknown"}
        address = ("[" + host + "]") if ":" in host else host
        if port is not None:
            address += ":" + str(port)
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if host in {"localhost", "host.docker.internal"} or (ip and ip.is_loopback):
            scope = "machine"
        elif ip and (ip.is_private or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"))):
            scope = "network"
        elif host.endswith((".local", ".ts.net")) or "." not in host:
            scope = "network"
        else:
            scope = "external"
        # A configured proxy/API can forward beyond the visible hop. A label
        # of 'local' never proves that a remote host is this machine.
        if kind in {"api", "proxy"} and scope != "external":
            scope = "unknown"
        return {"address": address, "scope": scope}
    except (ValueError, TypeError, AttributeError):
        return {"address": "", "scope": "unknown"}


def _json_metadata(path, default, missing):
    try:
        parsed = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(parsed, type(default)):
            raise ValueError("Unexpected configuration shape")
        return parsed
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        missing.append(Path(path).name)
        return default


def collect_snapshot() -> dict:
    """Project only non-secret columns; never instantiate messages/documents.

    JSON configuration stores are parsed without decrypting secrets or invoking
    runtime loaders which can migrate stores, refresh tokens or start services.
    """
    from core.database import SessionLocal, Session, ModelEndpoint, McpServer, EmailAccount, CalendarCal
    from src import constants, settings
    from services.projects import ProjectError, get_store

    def rows(db, model, fields):
        return [dict(zip(fields, row)) for row in db.query(*(getattr(model, f) for f in fields)).all()]

    missing = []
    with SessionLocal() as db:
        endpoints = rows(db, ModelEndpoint, ("id", "name", "base_url", "endpoint_kind", "is_enabled", "cached_models", "pinned_models"))
        sessions = rows(db, Session, ("id", "name", "model", "endpoint_url", "project_id", "folder", "archived", "owner"))
        connectors = rows(db, McpServer, ("id", "name", "transport", "url", "is_enabled"))
        emails = rows(db, EmailAccount, ("id", "name", "enabled", "imap_host", "smtp_host"))
        calendars = rows(db, CalendarCal, ("id", "name", "source", "caldav_base_url"))
    # The store is read-only. Project memory, instructions and linked sources
    # are never accessed; only these fields are projected from its JSON rows.
    try:
        projects = [{k: row.get(k) for k in ("id", "name", "folder", "owner", "privacy_profile")}
                    for row in get_store().list() if row.get("id")]
    except ProjectError:
        missing.append("projects.json")
        projects = []
    prefs = _json_metadata(constants.USER_PREFS_FILE, {}, missing)
    integrations = _json_metadata(constants.INTEGRATIONS_FILE, [], missing)
    embed = _json_metadata(constants.EMBEDDING_ENDPOINT_FILE, {}, missing)
    saved = _json_metadata(settings.SETTINGS_FILE, {}, missing)
    config, _revision = settings._merge(saved)
    whatsapp_dir = Path(constants.DATA_DIR) / "whatsapp"
    whatsapp = (whatsapp_dir / "auth" / "creds.json").is_file()
    keep = _json_metadata(whatsapp_dir / "keep.json", {}, missing)
    return {"endpoints": endpoints, "sessions": sessions, "connectors": connectors,
            "emails": emails, "calendars": calendars, "projects": projects,
            "prefs": prefs, "integrations": integrations, "embedding": embed,
            "whatsapp_enabled": whatsapp and keep.get("wanted", True),
            "settings": config, "missing_sources": missing}


def _models(row) -> list:
    out = []
    for key in ("cached_models", "pinned_models"):
        raw = row.get(key) or []
        try:
            values = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            values = []
        if isinstance(values, list):
            out.extend(m for m in values if isinstance(m, str) and m not in out)
    return out


def build_report(snapshot: dict) -> dict:
    from src.endpoint_resolver import normalize_base

    cfg = snapshot.get("settings", {})
    endpoints = snapshot.get("endpoints", [])
    project_rows = snapshot.get("projects", [])
    projects = {p["id"]: p for p in project_rows}
    folders = {(p.get("owner"), p["folder"].casefold()): p for p in project_rows if p.get("folder")}

    def project_for(session):
        if session.get("project_id"):
            return projects.get(session["project_id"])
        folder = (session.get("folder") or "").casefold()
        return folders.get((session.get("owner"), folder)) or folders.get((None, folder))
    routes = []

    def add(category, identity, name, url="", *, data="", enabled=True, kind="auto", scope=None, **extra):
        target = destination(url, kind)
        if scope:
            target["scope"] = scope
        row = {"id": str(identity), "category": category, "name": name, **target,
               "enabled": bool(enabled), "data": data, **extra}
        routes.append(row)
        return row

    assigned = set()
    for ep in endpoints:
        usages = []
        for session in snapshot.get("sessions", []):
            # Full route, not merely hostname: two paths on a provider may be
            # different accounts/backends. Nothing sensitive is returned.
            endpoint_url = normalize_base(ep.get("base_url"))
            if not endpoint_url or normalize_base(session.get("endpoint_url")) != endpoint_url:
                continue
            assigned.add(session["id"])
            project = project_for(session)
            usages.append({"id": session["id"], "name": session["name"], "model": session["model"],
                           "archived": bool(session.get("archived")), "project_id": project.get("id") if project else None,
                           "project_name": project.get("name") if project else None})
        roles = [role for role in ("default", "utility", "task", "vision", "research", "research_blind_review", "rerank")
                 if cfg.get(role + "_endpoint_id") == ep["id"]]
        add("models", ep["id"], ep["name"], ep.get("base_url", ""), kind=ep.get("endpoint_kind"),
            enabled=ep.get("is_enabled", True), data="Conversation context, attachments and tool results used by this model.",
            models=sorted(set(_models(ep) + [u["model"] for u in usages])), conversations=usages, roles=roles)
    for session in snapshot.get("sessions", []):
        if session["id"] in assigned:
            continue
        project = project_for(session)
        add("models", "session:" + session["id"], session["model"], session.get("endpoint_url", ""),
            data="Conversation context, attachments and tool results used by this model.", models=[session["model"]], roles=[],
            conversations=[{"id": session["id"], "name": session["name"], "model": session["model"],
                            "archived": bool(session.get("archived")), "project_id": project.get("id") if project else None,
                            "project_name": project.get("name") if project else None}])
    # Per-user overrides are included as configuration usage, without returning
    # account names, unrelated preferences or credentials.
    prefs = snapshot.get("prefs", {})
    users = prefs.get("_users")
    pref_rows = list(users.values()) if isinstance(users, dict) else [prefs]
    for ep_row in routes:
        if ep_row["category"] == "models":
            ep_row["user_override_count"] = sum(1 for pref in pref_rows if isinstance(pref, dict)
                and any(pref.get(role + "_endpoint_id") == ep_row["id"] for role in ("default", "utility", "vision", "research", "rerank")))
    for row in snapshot.get("connectors", []):
        add("connectors", row["id"], row["name"], row.get("url", ""), enabled=row.get("is_enabled", True),
            scope="unknown" if row.get("transport") == "stdio" else None,
            data="Tool arguments and results; a local process or service may contact other services.", transport=row.get("transport"))
    for row in snapshot.get("integrations", []):
        if isinstance(row, dict):
            add("connectors", "integration:" + str(row.get("id", "")), row.get("name") or "API integration",
                row.get("base_url", ""), enabled=row.get("enabled", True), data="API requests and data explicitly sent through this integration.")
    for row in snapshot.get("emails", []):
        for key in ("imap_host", "smtp_host"):
            if row.get(key):
                add("channels", row["id"] + ":" + key, row["name"] + (" (IMAP)" if key == "imap_host" else " (SMTP)"), row[key],
                    enabled=row.get("enabled", True), data="Mail requests; outgoing messages and attachments when sending mail.")
    add("channels", "telegram", "Telegram", cfg.get("telegram_api_base", "https://api.telegram.org"),
        enabled=cfg.get("telegram_bridge_enabled", False), data="Messages and agent replies for the configured chats.")
    # WhatsApp's loopback bridge still communicates with the messaging service.
    add("channels", "whatsapp", "WhatsApp", "https://web.whatsapp.com", enabled=snapshot.get("whatsapp_enabled", False),
        data="Messages and attachments sent through the linked messaging session.")
    for row in snapshot.get("calendars", []):
        if row.get("source") != "local":
            add("sync", row["id"], row["name"], row.get("caldav_base_url", ""),
                data="Calendar events and changes exchanged with the calendar service.")
    for i, pref in enumerate(pref_rows):
        if not isinstance(pref, dict):
            continue
        accounts = pref.get("caldav_accounts") or []
        legacy = pref.get("caldav") or {}
        if "caldav_accounts" not in pref and legacy.get("url"):
            accounts = [legacy]
        for j, acc in enumerate(accounts):
            add("sync", f"caldav:{i}:{j}", "CalDAV", acc.get("url", ""),
                data="Calendar events and changes exchanged with the calendar service.")
        for j, acc in enumerate(pref.get("google_calendar_accounts") or []):
            add("sync", f"calendar:{i}:{j}", "Google Calendar", "https://www.googleapis.com", enabled=acc.get("status") != "needs_reauth",
                data="Calendar events and changes exchanged with the calendar service.")
    search = cfg.get("search_provider") or "searxng"
    research_search = cfg.get("research_search_provider")
    from src.constants import SEARXNG_INSTANCE

    providers = {
        "searxng": cfg.get("search_url") or SEARXNG_INSTANCE,
        "firecrawl": cfg.get("firecrawl_url") or os.environ.get("FIRECRAWL_API_URL") or "http://localhost:3002",
        "brave": "https://api.search.brave.com", "duckduckgo": "https://duckduckgo.com",
                 "google": "https://www.googleapis.com", "google_pse": "https://www.googleapis.com",
                 "tavily": "https://api.tavily.com", "serper": "https://google.serper.dev"}
    for provider in dict.fromkeys([search, *(cfg.get("search_fallback_chain") or []), research_search]):
        if not provider or provider == "disabled":
            continue
        add("web", "search:" + provider, "Web search: {provider}", providers.get(provider, ""),
            kind="proxy" if provider in {"searxng", "firecrawl"} else "auto",
            enabled=search != "disabled" or provider == research_search, provider=provider, data="Search queries; a self-hosted search engine still queries external engines.")
    add("web", "web_fetch", "Web pages", scope="external", data="Requested URLs and queries sent to the sites visited by web and browser tools.")
    add("web", "prior_art", "Source lookup", "https://api.github.com", enabled=cfg.get("prior_art_enabled", True), data="Search terms and source lookup requests.")
    for component in ("tts", "stt"):
        provider = cfg.get(component + "_provider", "disabled")
        ep = next((e for e in endpoints if e["id"] == str(provider).removeprefix("endpoint:")), None) if str(provider).startswith("endpoint:") else None
        add("auxiliary", component, component.upper(), ep.get("base_url", "") if ep else cfg.get("tts_prospero_url", "") if component == "tts" and provider == "prospero" else "",
            kind=ep.get("endpoint_kind") if ep else "auto", scope="machine" if provider in {"local", "piper", "parakeet", "system"} else None,
            enabled=cfg.get(component + "_enabled", True) and bool(provider and provider != "disabled") and (ep is None or ep.get("is_enabled", True)), data="Text to speak." if component == "tts" else "Audio to transcribe.")
    embed = snapshot.get("embedding", {})
    add("auxiliary", "embedding", "Embeddings", embed.get("url") or os.environ.get("EMBEDDING_URL") or f"http://{os.environ.get('LLM_HOST', 'localhost')}:11434",
        data="Text fragments used for retrieval; local embedding fallback stays on this machine.")
    add("auxiliary", "vectors", "Vector store", f"{os.environ.get('CHROMADB_HOST', 'localhost')}:{os.environ.get('CHROMADB_PORT', '8100')}",
        data="Vectors, text fragments and metadata used for retrieval.")
    return {"version": 1, "computed_at": datetime.now(timezone.utc).isoformat(), "profile": cfg.get("privacy_profile", "local_preferred"),
            "routes": routes, "projects": [{k: p.get(k) for k in ("id", "name", "privacy_profile")} for p in project_rows], "missing_sources": snapshot.get("missing_sources", []),
            "local": ["This report is built locally from configuration and metadata, without contacting the listed destinations.",
                      "Conversation history, uploads, project files and memory are stored locally; selected context can be sent to models or tools.",
                      "Local scorecards do not export telemetry. Local processing does not itself upload source files."],
            "limits": ["Configured destinations show what can leave, not evidence that data has been sent. Enabled does not mean connected or healthy.",
                       "Local proxies, connectors and command tools can forward data elsewhere; their downstream destinations cannot be inferred from configuration.",
                       "Per-turn routing, model fallback, tools, user overrides and external folder synchronizers can add destinations. This is not a packet capture or a guarantee of local-only operation."]}


def report() -> dict:
    return build_report(collect_snapshot())


def main():
    """Command uses the running installation's exact API and authentication."""
    import argparse
    import httpx
    from mcp_servers.app_dock_server import _headers
    from src.constants import internal_api_base
    parser = argparse.ArgumentParser(description="Report configured data destinations without changing them.")
    parser.add_argument("--url", default=os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base())
    args = parser.parse_args()
    try:
        response = httpx.get(args.url.rstrip("/") + "/api/privacy/report", headers=_headers(), timeout=20, trust_env=False)
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    except httpx.HTTPError as exc:
        parser.exit(1, f"Could not read the data destination report ({type(exc).__name__}).\n")


if __name__ == "__main__":
    main()
