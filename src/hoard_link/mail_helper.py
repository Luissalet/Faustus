"""Read and send mail through the account(s) configured in Faustus — the ONE helper of the whole family.

It runs with Faustus's own Python, inside the Faustus folder::

    [<faustus>/venv/Scripts/python.exe | venv/bin/python, mail_helper.py, <faustus root>]   (JSON on stdin, one JSON line on stdout)

so the mail password never leaves Faustus: the caller only receives messages. The file imports nothing from ``hoard_link``
and uses the standard library only, because it runs under another interpreter. Callers run it through
``hoard_link.fam_mail.FaustusHelper`` (Python) or their own spawn (Node); the hub's mail gateway runs it for the family.
Folders are opened read-only (EXAMINE) and every fetch uses BODY.PEEK: nothing is ever marked as read, moved, flagged or
deleted. It started as the Kafka / Phileas / Ledger / Tantalus helpers (same author, MIT) and is their superset.

Requests (``action``); ``owner`` (optional) picks the Faustus user whose accounts are used (without it, the only owner that
has accounts), ``account`` narrows a request to one account:

    {"action": "status"}
        which accounts would be read and what sending would use; nothing is fetched
    {"action": "fetch", "since": {"<account>": {"INBOX": <last uid>}}, "validity": {"<account>": {"INBOX": <uidvalidity>}},
     "since_days": 14, "max": 300, "folders": ["INBOX"], "attachments_dir": "/abs/folder", "categories": true}
        incremental: only the UIDs above the watermark (the first time, the last ``since_days`` days); messages come oldest
        first (newest last). Answer: ``{"ok", "error", "accounts": [{"account", "address", "folders": {"INBOX": {"last_uid",
        "validity", "new", "remaining"}}}], "messages": [...]}``
    {"action": "scan", "since_days": 30, "max": 60, "skip": ["<message-id>", ...], "skip_own": false,
     "query": "free text", "gmail_query": "X-GM-RAW expression", "subject_terms": ["factura", ...],
     "sender_domains": ["amazon.es", ...], "attachments_dir": "/abs/folder", "categories": false}
        candidate messages, newest first: those that match any of the subject words, the sender domains, the Gmail query (or the
        free text, which replaces them), minus the ``skip`` message ids. Without any criterion nothing is returned.
    {"action": "headers", "since_days": 30, "max": 4000}
        one small record per message (``message_id, from_address, ts, list_unsubscribe, one_click, category, from_self``):
        no subject, no body; for noise reports
    {"action": "read", "message_id": "<...>", "account": "?", "folder": "?", "uid": 0}
        one message (found by Message-ID in any folder searched, or directly by account + folder + uid)
    {"action": "send", "subject": "...", "text": "...", "html": "...", "to": [...], "from_name": "..."}
        a notification mail through the account's SMTP; no ``to`` = the account's own address

Every message (fetch, scan, read) is the record ``message_id, subject, from_name, from_address, date, ts, date_ts, to, cc,
in_reply_to, references, text, links, attachments[{name, mime, size, sha, path}], images[{alt, src}], headers{list_unsubscribe,
one_click, gmail_category, message_id}, account, account_id, account_address, folder, uid, from_self`` plus ``html`` (the raw
HTML part, at most 240000 characters) only when it carries structured markup (schema.org / JSON-LD) — or always with
``"html": "all"`` in the request, never with ``"html": "none"``.
"""

from __future__ import annotations

import os
import sys

if __name__ == "__main__":
    # Run as a script, this file's folder is sys.path[0]; next to the hub's own modules (config.py, types.py, mcp.py ...) they would
    # shadow Faustus's and the standard library's. Drop it before anything else is imported.
    _HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]

import contextlib
import email
import email.header
import email.utils
import hashlib
import html as _html
import io
import json
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

HELPER_VERSION = 2
MAX_TEXT = 24_000
MAX_LINKS = 60
MAX_HTML = 240_000
MAX_IMAGES = 40
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
MAX_ATTACHMENTS = 10
ATTACHMENT_EXT = {"pdf", "png", "jpg", "jpeg", "webp", "heic", "tif", "tiff"}
SEND_TIMEOUT_S = 25
HEADER_CHUNK = 250
GMAIL_CATEGORIES = {"promotions": "promotions", "social": "social", "updates": "updates", "forums": "forums"}


def _mask(address: str) -> str:
    address = str(address or "")
    if "@" not in address:
        return "***" if address else ""
    local, _, domain = address.partition("@")
    return (local[:3] + "***@" + domain) if local else "***@" + domain


# ------------------------------------------------------------------ Faustus plumbing
def _load_server(root: str):
    # This file lives next to the hub's own modules (mcp.py, config.py, events.py…): with its folder first on
    # sys.path they would shadow Faustus's packages (``import mcp`` would find the hub's mcp.py). Drop it.
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != here]
    sys.path.insert(0, root)
    os.chdir(root)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from mcp_servers import email_server  # type: ignore
    return email_server


def _pick_owner(server, requested: str) -> str:
    if requested:
        return requested
    for key in ("ODYSSEUS_MCP_EMAIL_OWNER", "ODYSSEUS_EMAIL_OWNER"):
        if os.environ.get(key, "").strip():
            return os.environ[key].strip()
    rows = [r for r in server._read_accounts_from_db() if r.get("enabled", 1)]
    owners = sorted({str(r.get("owner") or "").strip() for r in rows} - {""})
    return owners[0] if len(owners) == 1 else ""


def _accounts(server, wanted):
    rows = [r for r in server._read_accounts_from_db() if r.get("enabled", 1)]
    try:
        rows = server._filter_accounts_for_owner(rows)
    except Exception:  # noqa: BLE001 — older Faustus builds
        pass
    if wanted:
        rows = [r for r in rows if wanted in (str(r.get("id")), str(r.get("account_name") or ""), str(r.get("imap_user") or ""))]
    return rows


def _selector(row: dict):
    for key in ("account_name", "imap_user", "id"):
        if row.get(key):
            return str(row[key])
    return None


def account_key(row: dict) -> str:
    """The name the hub keeps this account's watermark and sphere under: the account's name, else its login."""
    return str(row.get("account_name") or row.get("imap_user") or row.get("id") or "")


def account_address(row: dict) -> str:
    for key in ("from_address", "imap_user", "smtp_user"):
        if row.get(key) and "@" in str(row[key]):
            return str(row[key])
    return str(row.get("imap_user") or "")


# ------------------------------------------------------------------ message -> text
class _Text:
    BLOCK = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h\d|/table|p|div|tr|li|h\d)\b[^>]*>", re.I)
    DROP = re.compile(r"<\s*(style|script|head|title)\b.*?<\s*/\s*\1\s*>", re.I | re.S)
    HREF = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a\s*>""", re.I | re.S)
    TAG = re.compile(r"<[^>]+>")
    IMG = re.compile(r"<img\b([^>]*)>", re.I | re.S)
    ATTR = re.compile(r"""([a-zA-Z_:][-\w:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")

    @classmethod
    def images(cls, raw: str) -> list:
        """``[{"alt", "src"}]`` of the pictures of an HTML part: alt text (3+ characters) and/or an http(s) source; tracking pixels
        (width or height 0 / 1) and empty entries are left out; at most 40."""
        out, seen = [], set()
        for found in cls.IMG.finditer(raw):
            attrs = {k.lower(): (a or b or c) for k, a, b, c in cls.ATTR.findall(found.group(1))}
            if attrs.get("width", "").strip() in ("0", "1") or attrs.get("height", "").strip() in ("0", "1"):
                continue
            alt = re.sub(r"\s+", " ", _html.unescape(attrs.get("alt", ""))).strip()[:160]
            alt = alt if len(alt) >= 3 else ""
            src = _html.unescape(attrs.get("src", "")).strip()
            src = src[:500] if src.lower().startswith(("http://", "https://")) else ""
            if (alt or src) and (alt, src) not in seen:
                seen.add((alt, src))
                out.append({"alt": alt, "src": src})
                if len(out) >= MAX_IMAGES:
                    break
        return out

    @classmethod
    def from_html(cls, raw: str):
        """``(text, links)`` of an HTML part."""
        text, links, _images = cls.from_html_ex(raw)
        return text, links

    @classmethod
    def from_html_ex(cls, raw: str):
        """``(text, links, images)`` of an HTML part."""
        raw = cls.DROP.sub(" ", raw)
        raw = re.sub(r"<!--.*?-->", " ", raw, flags=re.S)
        images = cls.images(raw)
        links = []
        for href, label in cls.HREF.findall(raw):
            label = re.sub(r"\s+", " ", _html.unescape(cls.TAG.sub(" ", label))).strip()
            href = _html.unescape(href).strip()
            if href.lower().startswith(("http://", "https://")):
                links.append({"url": href[:1500], "label": label[:160]})
        text = cls.BLOCK.sub("\n", raw)
        text = _html.unescape(cls.TAG.sub(" ", text))
        return cls.tidy(text), links, images

    @staticmethod
    def tidy(text: str) -> str:
        text = text.replace("\r", "").replace(" ", " ").replace("​", "")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
        out, blank = [], 0
        for line in lines:
            if not line:
                blank += 1
                if blank == 1 and out:
                    out.append("")
                continue
            blank = 0
            out.append(line)
        return "\n".join(out).strip()


def _decode(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    for charset in (part.get_content_charset(), "utf-8", "latin-1"):
        if not charset:
            continue
        try:
            return payload.decode(charset, errors="replace" if charset == "latin-1" else "strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _save_attachments(msg, directory: str, server=None) -> list:
    """Write every PDF / image attachment into ``directory`` as <sha256>.<ext> and describe it."""
    out: list = []
    if not directory:
        return out
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return out
    for part in msg.walk():
        if part.is_multipart() or len(out) >= MAX_ATTACHMENTS:
            continue
        ctype = part.get_content_type()
        raw_name = part.get_filename() or ""
        try:
            name = str(server._decode_header(raw_name)) if (server is not None and raw_name) \
                else str(email.header.make_header(email.header.decode_header(raw_name)))
        except Exception:  # noqa: BLE001
            name = raw_name
        name = os.path.basename(name.replace("\\", "/"))[:160]
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        if ctype == "application/pdf":
            ext = "pdf"
        elif ctype.startswith("image/"):
            if not name and "attachment" not in str(part.get("Content-Disposition", "")).lower():
                continue                                    # inline pictures without a name are logos
            ext = ext or ctype.split("/", 1)[1].replace("jpeg", "jpg")
        elif ctype in ("application/octet-stream", "application/force-download") and ext in ATTACHMENT_EXT:
            pass
        else:
            continue
        if ext not in ATTACHMENT_EXT:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001
            continue
        if not payload or len(payload) > MAX_ATTACHMENT_BYTES:
            continue
        sha = hashlib.sha256(payload).hexdigest()
        path = os.path.join(directory, f"{sha}.{ext}")
        try:
            if not os.path.exists(path):
                with open(path, "wb") as handle:
                    handle.write(payload)
        except OSError:
            continue
        out.append({"name": name or f"attachment.{ext}", "mime": ctype, "size": len(payload), "sha": sha, "path": path})
    return out


_MARKUP = re.compile(r"ld\+json|itemscope|itemtype|Reservation", re.I)


def has_markup(raw_html: str) -> bool:
    """Does this HTML carry structured data a reader may want (schema.org JSON-LD or microdata: reservations, orders, parcels)?"""
    return bool(raw_html) and "schema.org" in raw_html and bool(_MARKUP.search(raw_html))


def _one_line(value, width: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:width]


def message_to_record(msg, server=None, attachments_dir: str = "", html_mode: str = "markup") -> dict:
    """Subject, sender, recipients, date, plain text (HTML converted), links, images (alt text and source), the useful headers and
    (when asked) the saved attachments of one message. ``html_mode``: ``markup`` (the raw HTML only when it carries structured
    markup, the default), ``all`` or ``none``."""
    def header(name: str) -> str:
        value = msg.get(name, "") or ""
        if server is not None:
            try:
                return str(server._decode_header(value))
            except Exception:  # noqa: BLE001
                pass
        try:
            return str(email.header.make_header(email.header.decode_header(value)))
        except Exception:  # noqa: BLE001
            return str(value)

    plain, html_text, links, images, raw_html = "", "", [], [], ""
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.is_multipart() or "attachment" in str(part.get("Content-Disposition", "")).lower():
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and not plain:
            plain = _Text.tidy(_decode(part))
        elif ctype == "text/html" and not html_text:
            raw_html = _decode(part)
            html_text, links, images = _Text.from_html_ex(raw_html)
    text = html_text if len(html_text) > len(plain) * 0.6 or not plain else plain
    for url in re.findall(r"https?://[^\s<>\"')\]]+", plain):
        links.append({"url": url[:1500], "label": ""})
    seen, unique = set(), []
    for link in links:
        if link["url"] in seen:
            continue
        seen.add(link["url"])
        unique.append(link)
    sender = header("From")
    name, address = email.utils.parseaddr(sender)
    date_raw = msg.get("Date", "")
    try:
        when = email.utils.parsedate_to_datetime(date_raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        ts = when.timestamp()
    except (TypeError, ValueError, IndexError):
        ts = None

    def addresses(name_: str) -> list:
        found = []
        for _, addr in email.utils.getaddresses([header(name_)]):
            addr = addr.strip().lower()
            if addr and "@" in addr and addr not in found:
                found.append(addr[:200])
        return found[:30]

    refs = re.findall(r"<[^<>\s]+>", msg.get("References", "") or "")
    message_id = (msg.get("Message-ID", "") or "").strip()[:300]
    record = {"message_id": message_id, "subject": header("Subject")[:300],
              "from_name": name[:120], "from_address": address[:200], "date": date_raw[:80], "ts": ts, "date_ts": ts,
              "to": addresses("To"), "cc": addresses("Cc"), "in_reply_to": (msg.get("In-Reply-To", "") or "").strip()[:300],
              "references": [r[:300] for r in refs[:10]],
              "text": text[:MAX_TEXT], "links": unique[:MAX_LINKS], "images": images,
              "headers": {"list_unsubscribe": _one_line(msg.get("List-Unsubscribe", ""), 800),
                          "one_click": bool(msg.get("List-Unsubscribe-Post")), "gmail_category": "", "message_id": message_id},
              "attachments": _save_attachments(msg, attachments_dir, server)}
    if raw_html and html_mode != "none" and (html_mode == "all" or has_markup(raw_html)):
        record["html"] = raw_html[:MAX_HTML]
    return record


# ------------------------------------------------------------------ IMAP
def _uids(conn, criteria: list) -> list:
    try:
        status, data = conn.uid("SEARCH", None, *criteria)
    except Exception:  # noqa: BLE001
        return []
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _imap_since(days: int) -> str:
    when = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    return when.strftime("%d-%b-%Y")


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def plan_uids(uids, last_uid: int, limit: int):
    """Which UIDs to fetch. ``uids`` is what the IMAP search returned (bytes / str / int, any order, maybe including
    ones at or below the watermark: ``UID N:*`` always returns the last message). Incremental (``last_uid`` > 0): the
    OLDEST ``limit`` ones above the watermark, so the watermark moves without leaving a gap and the next pass gets
    the rest. First read (``last_uid`` 0): the NEWEST ``limit``. Returns ``(selected ascending, remaining)``."""
    found = set()
    for u in uids:
        try:
            n = int(u)
        except (TypeError, ValueError):
            continue
        if n > int(last_uid or 0):
            found.add(n)
    ordered = sorted(found)
    limit = max(0, int(limit))
    chosen = ordered[:limit] if last_uid else ordered[-limit:] if limit else []
    return chosen, len(ordered) - len(chosen)


def _validity(conn):
    try:
        typ, data = conn.response("UIDVALIDITY")
        if data and data[0]:
            return int(data[0])
    except Exception:  # noqa: BLE001
        pass
    return None


def _select(conn, server, folder: str) -> bool:
    try:
        status, _ = conn.select(server._q(folder) if hasattr(server, "_q") else folder, readonly=True)
    except Exception:  # noqa: BLE001
        return False
    return status == "OK"


def _fetch_raw(conn, uid) -> bytes:
    status, data = conn.uid("FETCH", str(uid) if not isinstance(uid, (bytes, str)) else uid, "(BODY.PEEK[])")
    if status != "OK" or not data or not isinstance(data[0], tuple):
        return b""
    return data[0][1]


def _own_addresses(row: dict) -> set:
    return {str(row.get(k) or "").lower() for k in ("imap_user", "from_address", "smtp_user")} - {""}


def _stamp(record: dict, row: dict, folder: str, uid: int) -> dict:
    record["account"] = account_key(row)
    record["account_id"] = str(row.get("id") or "")
    record["account_address"] = account_address(row)
    record["folder"] = folder
    record["uid"] = int(uid)
    record["from_self"] = record.get("from_address", "").lower() in _own_addresses(row)
    return record


def _html_mode(request: dict) -> str:
    mode = str(request.get("html") or "markup").strip().lower()
    return mode if mode in ("markup", "all", "none") else "markup"


def _is_gmail(host: str) -> bool:
    return "gmail" in host or "googlemail" in host


def _scan_folders(host: str) -> list:
    return ["[Gmail]/All Mail", "[Gmail]/Todos", "INBOX"] if _is_gmail(host) else ["INBOX"]


def _select_first(conn, server, folders) -> str:
    """Open the first folder that exists, READ-ONLY (EXAMINE): flags are never touched."""
    for folder in folders:
        try:
            status, _ = conn.select(server._q(folder) if hasattr(server, "_q") else folder, readonly=True)
        except Exception:  # noqa: BLE001
            continue
        if status == "OK":
            return folder
    try:  # localized Gmail "All Mail": look it up by its \All flag
        status, lines = conn.list()
        for line in lines or []:
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            if "\\All" in text:
                name = text.rsplit(' "', 1)[-1].rstrip('"') if '"' in text else text.split()[-1]
                if conn.select(f'"{name}"', readonly=True)[0] == "OK":
                    return name
    except Exception:  # noqa: BLE001
        pass
    conn.select("INBOX", readonly=True)
    return "INBOX"


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _fetch_headers(conn, uids, fields: str) -> dict:
    """{uid (bytes): parsed header message} for many uids at once, headers only, nothing marked as read."""
    out = {}
    for chunk in _chunks(uids, HEADER_CHUNK):
        spec = b",".join(u if isinstance(u, bytes) else str(u).encode() for u in chunk).decode()
        try:
            status, data = conn.uid("FETCH", spec, f"(UID BODY.PEEK[HEADER.FIELDS ({fields})])")
        except Exception:  # noqa: BLE001
            continue
        if status != "OK":
            continue
        for item in data or []:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            found = re.search(rb"UID (\d+)", item[0])
            if found:
                out[found.group(1)] = email.message_from_bytes(item[1])
    return out


def _gmail_category_uids(conn, days: int) -> dict:
    """{uid (int): category} from Gmail's own category search in the selected folder; empty when the server is not Gmail."""
    mapping: dict = {}
    for key, name in GMAIL_CATEGORIES.items():
        raw = f"category:{key} newer_than:{max(1, int(days))}d"
        for uid in _uids(conn, ["X-GM-RAW", '"' + raw + '"']):
            number = _to_int(uid)
            if number is not None:
                mapping.setdefault(number, name)
    return mapping


def _uids_newest(uids) -> list:
    try:
        return sorted(set(uids), key=lambda u: int(u), reverse=True)
    except ValueError:
        return list(reversed(list(uids)))


def _label(row: dict) -> str:
    return str(row.get("account_name") or _mask(row.get("imap_user") or ""))


def fetch(server, request: dict) -> dict:
    since_days = max(1, min(int(request.get("since_days") or 14), 365))
    limit = max(1, min(int(request.get("max") or 300), 2000))
    since = request.get("since") if isinstance(request.get("since"), dict) else {}
    validity_req = request.get("validity") if isinstance(request.get("validity"), dict) else {}
    folders = [str(f) for f in (request.get("folders") or ["INBOX"]) if str(f).strip()][:6] or ["INBOX"]
    attachments_dir = str(request.get("attachments_dir") or "").strip()
    mode = _html_mode(request)
    want_categories = bool(request.get("categories", True))
    out, errors, infos = [], [], []
    for row in _accounts(server, request.get("account")):
        key = account_key(row)
        info = {"account": key, "address": account_address(row), "folders": {}}
        infos.append(info)
        gmail = _is_gmail(str(row.get("imap_host") or "").lower())
        conn = None
        try:
            conn = server._imap_connect(_selector(row))
            for folder in folders:
                if not _select(conn, server, folder):
                    continue
                validity = _validity(conn)
                last = int((since.get(key) or {}).get(folder) or 0)
                old_validity = (validity_req.get(key) or {}).get(folder)
                if last and old_validity and validity and int(old_validity) != int(validity):
                    last = 0                                    # the server renumbered the folder: start over
                found = _uids(conn, ["UID", f"{last + 1}:*"]) if last else _uids(conn, ["SINCE", _imap_since(since_days)])
                chosen, remaining = plan_uids(found, last, max(0, limit - len(out)))
                seen_max = max([n for n in (_to_int(u) for u in found) if n is not None] + [last])
                done_max = last
                categories = _gmail_category_uids(conn, since_days) if (gmail and want_categories and chosen) else {}
                for uid in chosen:
                    raw = _fetch_raw(conn, uid)
                    done_max = max(done_max, uid)
                    if not raw:
                        continue
                    record = message_to_record(email.message_from_bytes(raw), server, attachments_dir, mode)
                    record["headers"]["gmail_category"] = categories.get(uid, "")
                    out.append(_stamp(record, row, folder, uid))
                info["folders"][folder] = {"last_uid": seen_max if not remaining or not last else done_max, "validity": validity,
                                           "new": len(chosen), "remaining": remaining if last else 0,
                                           "skipped_old": 0 if last else remaining}
        except Exception as exc:  # noqa: BLE001 — one bad account must not hide the others
            errors.append(f"{key}: {type(exc).__name__}: {str(exc)[:160]}")
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.logout()
    out.sort(key=lambda r: (r.get("ts") or 0, r.get("uid") or 0))          # oldest first, newest last
    return {"ok": not errors or bool(out) or any(i["folders"] for i in infos), "error": "; ".join(errors), "accounts": infos,
            "messages": out}


def _gmail_raw(query: str, gmail_query: str, terms: list, domains: list) -> str:
    """The X-GM-RAW expression for a scan (without the ``newer_than`` window): the free text, else the caller's own expression OR the
    subject words OR the sender domains."""
    if query:
        return query.replace("\\", "").replace('"', "")
    parts = []
    if gmail_query:
        parts.append(f"({gmail_query})")
    if terms:
        parts.append("subject:(" + " OR ".join('"' + t.replace('"', "") + '"' if " " in t else t for t in terms) + ")")
    if domains:
        parts.append("from:(" + " OR ".join(domains) + ")")
    return " OR ".join(parts)


def _search(conn, host: str, days: int, query: str, gmail_query: str, terms: list, domains: list) -> list:
    since = _imap_since(days)
    if _is_gmail(host):
        raw = _gmail_raw(query, gmail_query, terms, domains)
        if raw:
            found = _uids(conn, ["X-GM-RAW", '"' + f"({raw}) newer_than:{max(1, int(days))}d".replace("\\", "").replace('"', '\\"') + '"'])
            if found:
                return found
    if query:
        return _uids(conn, ["SINCE", since, "TEXT", '"' + query.replace("\\", "").replace('"', "") + '"'])
    seen: dict = {}
    for term in terms:
        for uid in _uids(conn, ["SINCE", since, "SUBJECT", f'"{term}"']):
            seen[uid] = None
    for domain in domains:
        for uid in _uids(conn, ["SINCE", since, "FROM", f'"{domain}"']):
            seen[uid] = None
    return list(seen)


def _clean_terms(value, width: int = 60, limit: int = 80) -> list:
    return [re.sub(r'["\\]', "", str(t)).strip()[:width] for t in (value or []) if str(t).strip()][:limit]


def scan(server, request: dict) -> dict:
    """Messages that match any of the subject words / sender domains / Gmail query of the request (or its free text), newest first."""
    days = max(1, min(int(request.get("since_days") or 30), 365))
    limit = max(1, min(int(request.get("max") or 60), 1000))
    skip = {str(x) for x in (request.get("skip") or [])}
    query = str(request.get("query") or "").strip()[:200]
    gmail_query = str(request.get("gmail_query") or "").strip()[:1500]
    terms = _clean_terms(request.get("subject_terms"))
    domains = [d for d in (re.sub(r"[^a-z0-9.\-]", "", str(d).lower()) for d in (request.get("sender_domains") or [])) if d][:80]
    attachments_dir = str(request.get("attachments_dir") or "").strip()
    mode = _html_mode(request)
    skip_own = bool(request.get("skip_own"))
    want_categories = bool(request.get("categories"))
    if not (query or gmail_query or terms or domains):
        return {"ok": True, "error": "", "accounts": [], "messages": []}
    out, errors, scanned = [], [], []
    for row in _accounts(server, request.get("account")):
        host = str(row.get("imap_host") or "").lower()
        conn = None
        try:
            conn = server._imap_connect(_selector(row))
            folder = _select_first(conn, server, _scan_folders(host))
            uids = _uids_newest(_search(conn, host, days, query, gmail_query, terms, domains))
            fresh = uids
            if skip:
                heads = _fetch_headers(conn, uids, "MESSAGE-ID")
                fresh = [u for u in uids if u not in heads or str(heads[u].get("Message-ID", "") or "").strip() not in skip]
            scanned.append({"account": _label(row), "folder": folder, "matches": len(uids), "new": len(fresh)})
            categories = _gmail_category_uids(conn, days) if (_is_gmail(host) and want_categories and fresh) else {}
            own = _own_addresses(row)
            for uid in fresh:
                if len(out) >= limit:
                    break
                raw = _fetch_raw(conn, uid)
                if not raw:
                    continue
                record = message_to_record(email.message_from_bytes(raw), server, attachments_dir, mode)
                if skip_own and record["from_address"].lower() in own:
                    continue
                number = _to_int(uid) or 0
                record["headers"]["gmail_category"] = categories.get(number, "")
                out.append(_stamp(record, row, folder, number))
        except Exception as exc:  # noqa: BLE001 — one bad account must not hide the others
            errors.append(f"{_label(row)}: {type(exc).__name__}: {str(exc)[:160]}")
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.logout()
    out.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return {"ok": not errors or bool(out) or bool(scanned), "error": "; ".join(errors), "accounts": scanned, "messages": out}


def headers(server, request: dict) -> dict:
    """One small record per message of the last ``since_days`` (sender, date, List-Unsubscribe, Gmail category): no subject, no body."""
    days = max(1, min(int(request.get("since_days") or 30), 365))
    limit = max(1, min(int(request.get("max") or 4000), 12000))
    messages, errors, scanned = [], [], []
    for row in _accounts(server, request.get("account")):
        host = str(row.get("imap_host") or "").lower()
        conn = None
        try:
            conn = server._imap_connect(_selector(row))
            folder = _select_first(conn, server, _scan_folders(host))
            uids = _uids_newest(_uids(conn, ["SINCE", _imap_since(days)]))
            total = len(uids)
            uids = uids[:limit]
            categories = _gmail_category_uids(conn, days) if _is_gmail(host) else {}
            own = _own_addresses(row)
            parsed = _fetch_headers(conn, uids, "FROM DATE MESSAGE-ID LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST")
            for uid, msg in parsed.items():
                value = msg.get("From", "") or ""
                try:
                    value = str(server._decode_header(value))
                except Exception:  # noqa: BLE001
                    pass
                address = email.utils.parseaddr(value)[1].lower()
                try:
                    when = email.utils.parsedate_to_datetime(msg.get("Date", ""))
                    ts = (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()
                except (TypeError, ValueError, IndexError):
                    ts = None
                messages.append({"message_id": (msg.get("Message-ID", "") or "").strip()[:300], "from_address": address[:200], "ts": ts,
                                 "list_unsubscribe": _one_line(msg.get("List-Unsubscribe", ""), 800),
                                 "one_click": bool(msg.get("List-Unsubscribe-Post")), "category": categories.get(_to_int(uid), ""),
                                 "from_self": address in own})
            scanned.append({"account": _label(row), "folder": folder, "in_window": total, "read": len(parsed),
                            "gmail_categories": bool(categories)})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{_label(row)}: {type(exc).__name__}: {str(exc)[:160]}")
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.logout()
    return {"ok": not errors or bool(messages) or bool(scanned), "error": "; ".join(errors), "accounts": scanned, "messages": messages}


def _folders_for_read(host: str) -> list:
    if "gmail" in host or "googlemail" in host:
        return ["INBOX", "[Gmail]/All Mail", "[Gmail]/Todos"]
    return ["INBOX"]


def read_one(server, request: dict) -> dict:
    mid = str(request.get("message_id") or "").strip()
    folder = str(request.get("folder") or "").strip()
    uid = request.get("uid")
    mode = _html_mode(request)
    attachments_dir = str(request.get("attachments_dir") or "").strip()
    if not mid and not (folder and uid):
        return {"ok": False, "error": "message_id (or account + folder + uid) required"}
    for row in _accounts(server, request.get("account")):
        conn = None
        try:
            conn = server._imap_connect(_selector(row))
            if folder and uid:
                if not _select(conn, server, folder):
                    continue
                raw = _fetch_raw(conn, int(uid))
                if raw:
                    return {"ok": True, "error": "", "message": _stamp(message_to_record(email.message_from_bytes(raw), server, attachments_dir, mode), row, folder, int(uid))}
                continue
            for fld in _folders_for_read(str(row.get("imap_host") or "").lower()):
                if not _select(conn, server, fld):
                    continue
                uids = _uids(conn, ["HEADER", "Message-ID", f'"{mid}"'])
                if not uids:
                    continue
                raw = _fetch_raw(conn, uids[-1])
                if raw:
                    return {"ok": True, "error": "", "message": _stamp(message_to_record(email.message_from_bytes(raw), server, attachments_dir, mode), row, fld, int(uids[-1]))}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.logout()
    return {"ok": False, "error": "message not found"}


# ------------------------------------------------------------------ SMTP
def _connect(cfg: dict):
    host, port = cfg["smtp_host"], int(cfg.get("smtp_port") or 465)
    security = str(cfg.get("smtp_security") or "").strip().lower()
    # A stored mode that contradicts the well-known port (implicit TLS on 587, STARTTLS on 465) cannot work: trust the port.
    if port == 587 and security == "ssl":
        security = "starttls"
    elif port == 465 and security == "starttls":
        security = "ssl"
    if security not in ("ssl", "starttls", "none"):
        security = "starttls" if port == 587 else "ssl"
    context = ssl.create_default_context()
    if security == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=SEND_TIMEOUT_S, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=SEND_TIMEOUT_S)
        if security == "starttls":
            client.starttls(context=context)
    client.login(cfg["smtp_user"], cfg["smtp_password"])
    return client


def _addresses(value) -> list:
    if isinstance(value, str):
        value = value.split(",")
    out = []
    for item in value or []:
        item = re.sub(r"[\r\n]+", "", str(item)).strip()
        if item and re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", item):
            out.append(item)
    return out[:10]


def send_info(server, request: dict) -> dict:
    _sel, cfg = server._resolve_send_config(request.get("account") or None)
    sender = str(cfg.get("from_address") or cfg.get("smtp_user") or "")
    to = _addresses(request.get("to")) or _addresses(sender)
    return {"cfg": cfg, "sender": sender, "to": to,
            "info": {"account": cfg.get("account_name") or "", "from": _mask(sender), "to": [_mask(a) for a in to],
                     "server": f"{cfg.get('smtp_host')}:{cfg.get('smtp_port')}"}}


def build_message(request: dict, sender: str, to: list) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = re.sub(r"[\r\n]+", " ", str(request.get("subject") or "Hoard Hub"))[:200]
    msg["From"] = formataddr((re.sub(r"[\r\n]+", " ", str(request.get("from_name") or "Hoard Hub"))[:80], sender))
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.partition("@")[2] or None)
    msg.set_content(str(request.get("text") or request.get("subject") or ""))
    if request.get("html"):
        msg.add_alternative(str(request["html"]), subtype="html")
    return msg


def send(server, request: dict) -> dict:
    try:
        meta = send_info(server, request)
    except Exception as exc:  # noqa: BLE001 — Faustus's messages carry no secrets
        return {"ok": False, "error": str(exc)[:200] or type(exc).__name__}
    cfg, sender, to, info = meta["cfg"], meta["sender"], meta["to"], meta["info"]
    if not to:
        return {"ok": False, "error": "no recipient", **info}
    msg = build_message(request, sender, to)
    client = None
    try:
        client = _connect(cfg)
        client.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "error": "authentication failed", **info}
    except (smtplib.SMTPException, OSError) as exc:
        return {"ok": False, "error": type(exc).__name__, **info}
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.quit()
    return {"ok": True, "error": "", **info}


# ------------------------------------------------------------------ entry
def handle(request: dict, root: str) -> dict:
    try:
        server = _load_server(root)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Faustus mail module not loadable ({type(exc).__name__}: {str(exc)[:160]})"}
    owner = _pick_owner(server, str(request.get("owner") or "").strip())
    if owner:
        os.environ["ODYSSEUS_MCP_EMAIL_OWNER"] = owner
    try:
        rows = _accounts(server, request.get("account"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"accounts not readable ({type(exc).__name__})"}
    if not rows:
        return {"ok": False, "error": "Faustus has no enabled mail account" + (f" for {owner}" if owner else "")}
    info = {"helper": HELPER_VERSION,
            "accounts": [{"account": account_key(r), "id": str(r.get("id") or ""), "address": account_address(r),
                          "user": _mask(r.get("imap_user") or ""), "server": f"{r.get('imap_host')}:{r.get('imap_port')}"} for r in rows]}
    try:
        info.update(send_info(server, request)["info"])
    except Exception:  # noqa: BLE001 — reading may work even when sending is not configured
        pass
    action = request.get("action")
    if action == "send":
        return send(server, request)
    if action == "fetch":
        return fetch(server, request)
    if action == "scan":
        return scan(server, request)
    if action == "headers":
        return headers(server, request)
    if action == "read":
        return read_one(server, request)
    return {"ok": True, "error": "", **info}


def main() -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        request = {}
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())
    real_stdout = sys.stdout
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        answer = handle(request if isinstance(request, dict) else {}, root)
    real_stdout.write(json.dumps(answer, ensure_ascii=False, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
