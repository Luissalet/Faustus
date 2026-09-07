"""Approved, owner-scoped SMTP delivery for durable workflows.

SMTP has no exactly-once primitive. Persist admission before sending, consume
the exact approval, and never resend after an uncertain transport outcome.
"""
from __future__ import annotations

import hashlib
import json
import base64
import binascii
import re
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, getaddresses

from src import approval_store
from src.contracts import ApprovalPlan
from src.workflows.handlers import resolve, _MISSING
from src.workflows.scope import validate_output_scope

MAX_BODY_BYTES = 1024 * 1024
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def _attachments(config, context):
    entries = config.get("attachments", [])
    if not isinstance(entries, list) or len(entries) > 10:
        raise ValueError("email supports at most 10 attachments")
    result, total = [], 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {"filename", "content", "content_from", "encoding", "mime_type"}:
            raise ValueError("attachment needs inline content or a workflow content_from reference, never a disk path")
        filename = entry.get("filename")
        if (not isinstance(filename, str) or not filename.strip() or len(filename) > 120
                or filename in (".", "..") or any(c in filename for c in '/\\:' )
                or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
            raise ValueError("attachment filename must be a safe bare filename")
        if ("content" in entry) == ("content_from" in entry):
            raise ValueError("attachment needs exactly one content source")
        reference = entry.get("content_from")
        if "content_from" in entry and not isinstance(reference, str):
            raise ValueError("attachment content_from must be a workflow reference")
        content = resolve(reference, context) if reference is not None else entry.get("content")
        if not isinstance(content, str) or len(content) > (MAX_ATTACHMENT_BYTES * 4 // 3 + 4):
            raise ValueError("attachment content must be bounded text or base64")
        encoding = entry.get("encoding", "utf-8")
        if encoding == "utf-8":
            data = content.encode("utf-8")
        elif encoding == "base64":
            try:
                data = base64.b64decode(content, validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("invalid base64 attachment") from None
        else:
            raise ValueError("attachment encoding must be utf-8 or base64")
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise ValueError("email attachments exceed 10 MiB combined")
        mime = entry.get("mime_type", "application/octet-stream")
        if not isinstance(mime, str) or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]{1,64}/[A-Za-z0-9!#$&^_.+-]{1,64}", mime):
            raise ValueError("invalid attachment MIME type")
        result.append((filename, mime, data))
    return result


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
    cc = _addresses(config["cc"]) if config.get("cc") else []
    bcc = _addresses(config["bcc"]) if config.get("bcc") else []
    envelope_recipients = list(dict.fromkeys(recipients + cc + bcc))
    if len(envelope_recipients) > 50:
        raise ValueError("email delivery supports at most 50 combined recipients")
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
    html_ref = config.get("html_from")
    html = resolve(html_ref, context) if isinstance(html_ref, str) else config.get("html", "")
    if not isinstance(html, str) or len(html) > MAX_BODY_BYTES or len(html.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("email HTML must be text of at most 1 MiB")
    attachments = _attachments(config, context)
    attachment_summary = [{"filename": name, "mime_type": mime, "size": len(data),
                           "sha256": hashlib.sha256(data).hexdigest()} for name, mime, data in attachments]
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
             "cc": cc, "bcc": bcc, "html": html, "attachments": attachment_summary,
             "account_id": cfg.get("account_id") or account or "",
             "smtp_host": cfg.get("smtp_host"), "smtp_port": cfg.get("smtp_port"),
             "smtp_security": cfg.get("smtp_security"), "smtp_user": cfg.get("smtp_user")}
    digest = hashlib.sha256(json.dumps(bound, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    preview = body[:300] + ("\n[Preview truncated; review the workflow body before approving.]"
                           if len(body) > 300 else "")
    extras = "\n" + json.dumps({"cc": cc, "bcc": bcc, "attachments": attachment_summary,
                                "html": bool(html)}, ensure_ascii=False)
    # Full structured metadata remains in the approval, even if its short
    # textual summary needs truncation. Never render untrusted HTML here.
    if len(extras) > 850:
        extras = extras[:850] + "\n[Summary truncated; inspect approval permissions and workflow.]"
    plan = ApprovalPlan.parse({"action": "deliver", "skill_id": "workflow.email",
                              "skill_version": "1.0.0", "backend": "smtp",
                              "recipients": envelope_recipients, "output_kinds": ["text"],
                              "permissions": {"email": {"to": recipients, "cc": cc, "bcc": bcc,
                                                        "attachments": attachment_summary, "html": bool(html)}},
                              "detail": f"Email: {subject}\nFrom: {sender[0]}\n"
                                        f"SHA-256: {digest}{extras}\n\n{preview}"})
    message = EmailMessage(policy=SMTP)
    message["From"] = sender[0]
    message["To"] = ", ".join(recipients)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message["Message-ID"] = f"<{digest}@faustus.local>"
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")
    for name, mime, data in attachments:
        maintype, subtype = mime.split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    # BCC exists only in the SMTP envelope and owner's approval, never headers.
    return owner, cfg, sender[0], envelope_recipients, plan, message


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
