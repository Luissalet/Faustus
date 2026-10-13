"""Native channel operations using Faustus's existing integration credentials."""
from __future__ import annotations

import json
from pathlib import Path
import threading
from urllib.parse import quote
import uuid

from core.atomic_io import atomic_write_json
from src.constants import DATA_DIR
from src import integrations

PROVIDERS = {
    "slack": {"name": "Slack", "integration_preset": "slack_messages"},
    "discord": {"name": "Discord", "integration_preset": "discord_messages"},
    "matrix": {"name": "Matrix", "integration_preset": "matrix_messages"},
}
STORE = Path(DATA_DIR) / "channel_connections.json"
_LOCK = threading.RLock()


def _load():
    try:
        rows = json.loads(STORE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Channel connection store is not a list of records")
    return rows


def list_connections():
    with _LOCK:
        rows = _load()
    return [{**row, "credential_registered": integrations.get_integration(row["integration_id"]) is not None}
            for row in rows]


def save_connection(provider, integration_id, name="", connection_id=""):
    if provider not in PROVIDERS:
        raise ValueError("Supported providers: " + ", ".join(PROVIDERS))
    if integrations.get_integration(integration_id) is None:
        raise ValueError("Save the credential in Integrations first")
    with _LOCK:
        rows = _load()
        current = next((row for row in rows if row["id"] == connection_id), None) if connection_id else None
        if connection_id and current is None:
            raise ValueError("Channel connection not found")
        row = {**(current or {}), "id": connection_id or uuid.uuid4().hex,
               "provider": provider, "integration_id": integration_id,
               "name": name or PROVIDERS[provider]["name"]}
        if current is None:
            rows.append(row)
        else:
            rows[rows.index(current)] = row
        STORE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(str(STORE), rows, indent=2)
    return row


def _connection(connection_id):
    with _LOCK:
        row = next((item for item in _load() if item["id"] == connection_id), None)
    if row is None:
        raise ValueError("Channel connection not found")
    return row


def _id(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " is required")
    return value


async def _request(row, method, path, *, params=None, body=None, allow_no_content=False):
    write = method != "GET"
    attempt = {"attempt": {"id": uuid.uuid4().hex, "connection_id": row["id"],
                           "method": method, "path": path}} if write else {}
    result = await integrations.execute_api_call(row["integration_id"], method, path,
                                                params=params, body=body, return_data=True)
    if result.get("exit_code"):
        return {**result, **attempt}
    data = result.get("data")
    if allow_no_content and result.get("status_code") == 204:
        return {**result, **attempt, "untrusted_content": True}
    if row["provider"] == "slack" and isinstance(data, dict) and data.get("ok") is False:
        return {"exit_code": 1, "error": str(data.get("error") or "Slack request failed"),
                "data": data, "untrusted_content": True, **attempt}
    expected = list if row["provider"] == "discord" and not write else dict
    error = "The channel service returned no usable JSON response" if not isinstance(data, expected) else ""
    if write and not error:
        receipt = "ok" if path == "/api/reactions.add" else {"slack": "ts", "discord": "id", "matrix": "event_id"}[row["provider"]]
        if not data.get(receipt):
            error = "The channel service returned no write receipt"
    if error:
        return {"exit_code": 1, "error": error, "data": data, "untrusted_content": True,
                **attempt, **({"outcome_unknown": True, "status": "outcome_unknown",
                               "uncertainty": {"reconcile_action": "read the remote state before repeating the request"}} if write else {})}
    return {"exit_code": 0, "data": data, "untrusted_content": True, **attempt}


async def list_spaces(connection_id, parent_id="", cursor="", limit=100):
    row = _connection(connection_id)
    provider = row["provider"]
    kind = "channels"
    if provider == "slack":
        params = {"limit": limit, "types": "public_channel,private_channel,mpim,im"}
        if cursor:
            params["cursor"] = cursor
        result = await _request(row, "GET", "/api/conversations.list", params=params)
    elif provider == "discord":
        if parent_id:
            result = await _request(row, "GET", f"/api/v10/guilds/{quote(parent_id, safe='')}/channels")
        else:
            kind = "workspaces"
            params = {"limit": limit}
            if cursor:
                params["after"] = cursor
            result = await _request(row, "GET", "/api/v10/users/@me/guilds", params=params)
    else:
        result = await _request(row, "GET", "/_matrix/client/v3/joined_rooms")
    if result["exit_code"]:
        return result
    data = result["data"]
    if provider == "slack":
        items = data.get("channels", [])
        next_cursor = (data.get("response_metadata") or {}).get("next_cursor", "")
    elif provider == "discord":
        items = data
        next_cursor = str(items[-1]["id"]) if not parent_id and len(items) == limit else ""
    else:
        items = [{"id": room, "name": room} for room in data.get("joined_rooms", [])]
        next_cursor = ""
    return {"exit_code": 0, "provider": provider, "kind": kind, "items": items,
            "next_cursor": next_cursor, "untrusted_content": True}


async def history(connection_id, channel_id, cursor="", limit=100):
    row = _connection(connection_id)
    channel = _id(channel_id, "channel_id")
    provider = row["provider"]
    params = {"limit": limit}
    if provider == "slack":
        params["channel"] = channel
        if cursor:
            params["cursor"] = cursor
        result = await _request(row, "GET", "/api/conversations.history", params=params)
    elif provider == "discord":
        if cursor:
            params["before"] = cursor
        result = await _request(row, "GET", f"/api/v10/channels/{quote(channel, safe='')}/messages", params=params)
    else:
        params["dir"] = "b"
        if cursor:
            params["from"] = cursor
        result = await _request(row, "GET", f"/_matrix/client/v3/rooms/{quote(channel, safe='')}/messages", params=params)
    if result["exit_code"]:
        return result
    data = result["data"]
    native = data.get("messages", []) if provider == "slack" else data if provider == "discord" else data.get("chunk", [])
    messages = []
    for item in native:
        content = item.get("content") or {}
        messages.append({"id": str(item.get("ts") if provider == "slack" else item.get("id") if provider == "discord" else item.get("event_id")),
            "text": str(item.get("text", "") if provider == "slack" else item.get("content", "") if provider == "discord" else content.get("body", "")),
            "author": item.get("user") if provider == "slack" else (item.get("author") or {}).get("id") if provider == "discord" else item.get("sender"),
            "attachments": item.get("files", []) if provider == "slack" else item.get("attachments", []) if provider == "discord" else [content] if content.get("url") or content.get("file") else [],
            "raw": item})
    cursor_out = ((data.get("response_metadata") or {}).get("next_cursor", "") if provider == "slack" else
                  str(native[-1]["id"]) if provider == "discord" and len(native) == limit else
                  data.get("end", "") if provider == "matrix" else "")
    return {"exit_code": 0, "provider": provider, "channel_id": channel, "messages": messages,
            "next_cursor": cursor_out, "order": "newest_first", "untrusted_content": True}


async def write_message(connection_id, channel_id, text, message_id="", transaction_id=""):
    row = _connection(connection_id)
    channel = _id(channel_id, "channel_id")
    text = _id(text, "text")
    provider = row["provider"]
    if provider == "slack":
        body = {"channel": channel, "text": text}
        if message_id:
            body["ts"] = message_id
        result = await _request(row, "POST", "/api/chat.update" if message_id else "/api/chat.postMessage", body=body)
    elif provider == "discord":
        path = f"/api/v10/channels/{quote(channel, safe='')}/messages"
        if message_id:
            path += "/" + quote(message_id, safe="")
        result = await _request(row, "PATCH" if message_id else "POST", path, body={"content": text})
    else:
        body = {"msgtype": "m.text", "body": text}
        if message_id:
            body = {"msgtype": "m.text", "body": "* " + text,
                    "m.new_content": {"msgtype": "m.text", "body": text},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": message_id}}
        txn = transaction_id or uuid.uuid4().hex
        result = await _request(row, "PUT", f"/_matrix/client/v3/rooms/{quote(channel, safe='')}/send/m.room.message/{quote(txn, safe='')}", body=body)
    if result["exit_code"]:
        return result
    data = result["data"]
    identifier = data.get("ts") if provider == "slack" else data.get("id") if provider == "discord" else data.get("event_id")
    return {"exit_code": 0, "provider": provider, "channel_id": channel, "message_id": identifier,
            "action": "edit" if message_id else "send", "data": data, "untrusted_content": True,
            "attempt": result["attempt"]}


async def react(connection_id, channel_id, message_id, reaction, transaction_id=""):
    row = _connection(connection_id)
    channel = _id(channel_id, "channel_id")
    message = _id(message_id, "message_id")
    reaction = _id(reaction, "reaction")
    provider = row["provider"]
    if provider == "slack":
        return await _request(row, "POST", "/api/reactions.add", body={"channel": channel, "timestamp": message, "name": reaction})
    if provider == "discord":
        path = f"/api/v10/channels/{quote(channel, safe='')}/messages/{quote(message, safe='')}/reactions/{quote(reaction, safe='')}/@me"
        # Discord reaction success is an empty 204, unlike JSON message writes.
        return await _request(row, "PUT", path, allow_no_content=True)
    txn = transaction_id or uuid.uuid4().hex
    return await _request(row, "PUT", f"/_matrix/client/v3/rooms/{quote(channel, safe='')}/send/m.reaction/{quote(txn, safe='')}",
                          body={"m.relates_to": {"rel_type": "m.annotation", "event_id": message, "key": reaction}})
