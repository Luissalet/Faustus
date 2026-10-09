"""Bind product-configured coordination to the existing owner/project/chat.

Only route or agent runtime values enter this resolver. Model-written text
cannot create a binding or choose the connector the native client uses.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .authority import authorize
from .bindings import CoordinationError, READ_OPERATIONS
from .client import Client
from .store import Store


def get_store():
    from src.constants import DATA_DIR
    return Store(DATA_DIR)


def storage_owner(owner):
    from src.owner_identity import effective_storage_owner
    result = effective_storage_owner(owner)
    if result is None:
        raise CoordinationError("coordination needs an authenticated or local product owner")
    return result


def connector_fingerprint(connector_id):
    from core.database import McpServer, SessionLocal
    with SessionLocal() as db:
        row = db.query(McpServer).filter(McpServer.id == connector_id).first()
        if row is None or row.is_enabled is not True:
            raise CoordinationError("coordination connector is missing or disabled")
        # Configuration authority, not credentials themselves, is persisted.
        fields = {k: getattr(row, k, None) for k in ("transport", "command", "args", "env", "url", "inherit_env", "oauth_config", "declared_permissions", "disabled_tools")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate(binding, *, owner, session_id, project_id=None, manager=None):
    from core.database import Session as DbSession, SessionLocal
    from services.projects import project_for_session
    from src.connector_policy import resolve_allowed_servers_for_session
    from src.owner_identity import auth_disabled
    if binding.owner != storage_owner(owner) or binding.session_id != session_id:
        raise CoordinationError("coordination identity does not belong to this run")
    with SessionLocal() as db:
        session = db.query(DbSession).filter(DbSession.id == session_id).first()
        if session is None or (not auth_disabled() and session.owner != owner):
            raise CoordinationError("coordination session is unavailable for this owner")
        raw_connectors = session.connector_ids
        if raw_connectors is not None:
            try:
                selected_connectors = json.loads(raw_connectors)
            except (ValueError, TypeError) as exc:
                raise CoordinationError("coordination connector policy cannot be read") from exc
            if not isinstance(selected_connectors, list) or binding.connector_id not in selected_connectors:
                raise CoordinationError("this session does not allow the coordination connector")
    project = project_for_session(session_id, owner)
    if not project or project.get("id") != binding.project_id or project.get("enabled") is False:
        raise CoordinationError("coordination project is no longer attached to this session")
    if project_id is not None and project_id != binding.project_id:
        raise CoordinationError("coordination project changed during this run")
    if raw_connectors is None and "connectors" in project and project["connectors"] is not None:
        if not isinstance(project["connectors"], list) or binding.connector_id not in project["connectors"]:
            raise CoordinationError("this project does not allow the coordination connector")
    allowed = resolve_allowed_servers_for_session(session_id, owner)
    if allowed is not None and binding.connector_id not in allowed:
        raise CoordinationError("this session does not allow the coordination connector")
    if not binding.connector_fingerprint or connector_fingerprint(binding.connector_id) != binding.connector_fingerprint:
        raise CoordinationError("coordination connector configuration changed; configure a new binding")
    if manager is not None and manager.get_server_status(binding.connector_id).get("status") != "connected":
        raise CoordinationError("coordination connector is not connected")
    return project


@dataclass
class Grant:
    store: Store
    binding: object
    owner: object
    session_id: str
    project_id: str
    manager: object
    read_only: bool = False

    def live(self):
        self.store.live(self.binding)
        validate(self.binding, owner=self.owner, session_id=self.session_id,
                 project_id=self.project_id, manager=self.manager)

    def allows(self, name, content):
        try:
            self.live()
            from .tools import parse
            action, args = parse(name, content)
            observed = None
            if action in {"claim", "checkpoint", "submit", "review", "done"} or (action == "lock" and "task_id" in args) or (
                    action == "post" and args.get("thread_id") not in self.binding.thread_ids):
                observed = (self.store.reviewable_task if action == "review" else self.store.observed_task)(self.binding, args.get("task_id"))
            authorize(self.binding, action, args, task=observed, held_locks=[] if action == "heartbeat" else None,
                      read_only=self.read_only)
            return True
        except Exception:
            return False

    def delivered(self, result):
        if not isinstance(result, dict) or result.get("exit_code") not in (None, 0):
            return
        receipt = result.get("_coordination_delivery")
        if not isinstance(receipt, dict) or receipt.get("binding") != self.binding.id or receipt.get("revision") != self.binding.revision:
            return
        self.live()
        if receipt.get("receipt"):
            self.store.confirm_delivery(self.binding, receipt["receipt"], threads=receipt.get("threads", []))
        if receipt.get("task_receipt"):
            self.store.confirm_task_delivery(self.binding, receipt["task_receipt"])


def arguments(content):
    value = json.loads(content) if isinstance(content, str) else content
    if not isinstance(value, dict) or set(value) != {"action", "arguments"} or not isinstance(value["action"], str) or not isinstance(value["arguments"], dict):
        raise CoordinationError("provide coordination action and its arguments")
    return value["action"], value["arguments"]


def resolve_grant(owner, session_id, *, project_id=None, manager=None, read_only=False):
    from src.tool_utils import get_mcp_manager
    from services.projects import project_for_session
    store = get_store()
    project = project_for_session(session_id, owner)
    template = store.template(storage_owner(owner), project["id"]) if project else None
    if template and template.active:
        # A chat override must never inherit a connector it explicitly denied.
        validate(template.for_session(session_id), owner=owner, session_id=session_id,
                 project_id=project_id, manager=manager or get_mcp_manager())
        store.materialize(template, session_id)
    binding = store.for_session(storage_owner(owner), session_id)
    store.live(binding)
    manager = manager or get_mcp_manager()
    if manager is None:
        raise CoordinationError("coordination manager is unavailable")
    validate(binding, owner=owner, session_id=session_id, project_id=project_id, manager=manager)
    return Grant(store, binding, owner, session_id, binding.project_id, manager, read_only)


def client(grant, *, disabled_tools=(), read_only=None):
    from core.database import McpServer, SessionLocal
    with SessionLocal() as db:
        server = db.query(McpServer).filter(McpServer.id == grant.binding.connector_id).first()
        if server is None:
            raise CoordinationError("coordination connector is missing")
        stored_disabled = json.loads(server.disabled_tools) if server.disabled_tools else []
        if not isinstance(stored_disabled, list) or not all(isinstance(t, str) for t in stored_disabled):
            raise CoordinationError("coordination tool policy cannot be read")
    disabled_tools = {*disabled_tools, *stored_disabled}
    return Client(grant.store, grant.binding, grant.manager, revalidate=lambda _: grant.live(),
                  disabled_tools=disabled_tools, read_only=grant.read_only if read_only is None else read_only)
