"""Approved, owner-scoped SMTP delivery for durable workflows.

SMTP has no exactly-once primitive. Persist admission before sending, consume
the exact approval, and never resend after an uncertain transport outcome.
"""
from __future__ import annotations

import hashlib
import json
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, getaddresses

from src import approval_store
from src.contracts import ApprovalPlan
from src.workflows.handlers import resolve, _MISSING
from src.workflows.scope import validate_output_scope

MAX_BODY_BYTES = 1024 * 1024


def _addresses(value):
    entries = [value] if isinstance(value, str) else value
    if (not isinstance(entries, (list, tuple)) or not entries or len(entries) > 50
            or any(not isinstance(s, str) or len(s) > 1000
                   or any(c in s for c in "\r\n\x00") for s in entries)):
        raise ValueError("email recipients must be a bounded list of addresses")
    parsed = getaddresses(entries)
    addresses = []
    for _name, address in parsed:
        if (not address or len(address) > 320 or address.count("@") != 1
                or any(c.isspace() for c in address) or address.startswith("@")
                or address.endswith("@")):
            raise ValueError("invalid email recipient")
        if address not in addresses:
            addresses.append(address)
    if not addresses or len(addresses) > 50:
        raise ValueError("email delivery requires between 1 and 50 recipients")
    return addresses


def _prepare(config, context):
    if config.get("channel", "email") != "email":
        raise ValueError("this delivery handler supports channel=email")
    owner = str(context.get("owner") or "")
    run_id = str(context.get("run_id") or "")
    node_id = str(context.get("node_id") or "")
    if not owner or not run_id or not node_id or not callable(context.get("mark_effect")):
        raise ValueError("email delivery requires an owned, claimed workflow node")
    validate_output_scope(owner, str(context.get("project_id") or ""),
                          str((context.get("inputs") or {}).get("session_id") or ""))
    recipients = _addresses(config.get("to"))
    subject = config.get("subject", "Faustus workflow")
    if (not isinstance(subject, str) or not subject.strip() or len(subject) > 200
            or any(c in subject for c in "\r\n\x00")):
        raise ValueError("email subject must be a single line of at most 200 characters")
    reference = config.get("body_from")
    body = resolve(reference, context) if isinstance(reference, str) else config.get("body", _MISSING)
    if not isinstance(body, str) or not body.strip():
        raise ValueError("email delivery needs body or a text body_from reference")
    if len(body) > MAX_BODY_BYTES or len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("email body exceeds 1 MiB")
    # Attachments and additional recipients must never be silently ignored.
    if any(config.get(key) for key in ("attachments", "cc", "bcc", "html")):
        raise ValueError("this delivery supports plain text and to recipients only")
    account = config.get("account_id")
    if account is not None and (not isinstance(account, str) or len(account) > 200):
        raise ValueError("invalid email account_id")
    from routes.email_routes import _resolve_send_config
    cfg = _resolve_send_config(account or None, owner=owner)
    sender = _addresses(cfg.get("from_address") or cfg.get("smtp_user"))
    if len(sender) != 1:
        raise ValueError("email account must supply exactly one sender")
    # Bind the body, actual account/transport and this specific workflow node.
    # Password rotation does not change who/what/where was approved.
    bound = {"run_id": run_id, "node_id": node_id, "owner": owner,
             "to": recipients, "from": sender[0], "subject": subject, "body": body,
             "account_id": cfg.get("account_id") or account or "",
             "smtp_host": cfg.get("smtp_host"), "smtp_port": cfg.get("smtp_port"),
             "smtp_security": cfg.get("smtp_security"), "smtp_user": cfg.get("smtp_user")}
    digest = hashlib.sha256(json.dumps(bound, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    preview = body[:900] + ("\n[Preview truncated; review the workflow body before approving.]"
                           if len(body) > 900 else "")
    plan = ApprovalPlan.parse({"action": "deliver", "skill_id": "workflow.email",
                              "skill_version": "1.0.0", "backend": "smtp",
                              "recipients": recipients, "output_kinds": ["text"],
                              "detail": f"Email: {subject}\nFrom: {sender[0]}\n"
                                        f"SHA-256: {digest}\n\n{preview}"})
    message = EmailMessage(policy=SMTP)
    message["From"] = sender[0]
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message["Message-ID"] = f"<{digest}@faustus.local>"
    message.set_content(body)
    return owner, cfg, sender[0], recipients, plan, message


def send(config, context):
    owner, cfg, sender, recipients, plan, message = _prepare(config, context)
    previous = context.get("previous") or {}
    approval_id = str(previous.get("approval_id") or "")
    if not approval_id:
        card = approval_store.request(plan, owner=owner, run_id=context["run_id"],
                                      project_id=str(context.get("project_id") or ""))
        return {"status": "paused", "delivered": False, "approval_id": card.id,
                "reason": "Review and approve this exact email before delivery"}
    card = approval_store.get(approval_id)
    if card is None or card.owner != owner:
        return {"status": "failed", "delivered": False, "reason": "delivery approval unavailable"}
    verdict = card.covers(plan)
    if not verdict["ok"]:
        if card.status == "pending" and card.plan.fingerprint() == plan.fingerprint():
            from src.workflows.clock import due
            from src.contracts.base import now_iso
            if not card.expires_at or not due(card.expires_at, now_iso()):
                return {"status": "paused", "delivered": False, "approval_id": card.id,
                        "reason": "waiting on a person"}
        return {"status": "failed", "delivered": False, "approval_id": card.id,
                "reason": f"delivery approval rejected: {verdict['reason']}"}
    spent = approval_store.consume(card.id, plan, owner=owner)
    if not spent["ok"]:
        return {"status": "failed", "delivered": False, "approval_id": card.id,
                "reason": "delivery approval was no longer available at execution"}
    marker = context["mark_effect"]
    if not marker("pending"):
        return {"status": "failed", "delivered": False,
                "reason": "workflow stopped or lost its claim before delivery"}
    from routes.email_helpers import _send_smtp_message
    try:
        _send_smtp_message(cfg, sender, recipients, message.as_bytes(), timeout=30)
    except Exception as exc:
        marker("unknown")
        rejected = getattr(exc, "recipients", {})
        receipt = {}
        if isinstance(rejected, dict) and rejected:
            refused = [address for address in recipients if address in rejected]
            if refused:
                receipt = {"rejected_recipients": refused,
                           "accepted_recipients": [a for a in recipients if a not in refused]}
        # Do not echo a server response that could contain account secrets.
        return {"status": "failed", "delivered": False, "effect_state": "unknown",
                **receipt,
                "message_id": str(message["Message-ID"]),
                "reason": f"SMTP delivery outcome needs review ({type(exc).__name__}); not retried"}
    if not marker("confirmed"):
        return {"status": "failed", "delivered": False, "effect_state": "unknown",
                "message_id": str(message["Message-ID"]),
                "reason": "SMTP accepted the message but the local receipt could not be recorded; not retried"}
    return {"delivered": True, "effect_state": "confirmed",
            "message_id": str(message["Message-ID"]), "recipients": recipients,
            "detail": "SMTP accepted the message; inbox delivery is not guaranteed"}
