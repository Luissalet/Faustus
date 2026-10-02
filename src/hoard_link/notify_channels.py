"""The ONE implementation of the notification channels: Windows toast, ntfy, Telegram and mail.

Until 0.8 every Hoard app carried its own copy of this code (``xml_escape``, ``build_toast_ps1``, the ntfy and Telegram
POSTs, the SMTP sender, the quiet-hours check) and so did the hub. They all call these functions now. Nothing here decides
*whether* to notify (that is the hub's job, or the app's when the hub is away): each sender takes what it needs, does one
delivery and answers ``{"ok": bool, "error": str?}``. No sender raises, none logs or returns a secret, every network call has
a timeout.

Standard library only (this file is vendored into apps). Network senders accept ``http=`` (``fn(url, payload, headers=,
timeout=) -> (status | None, data)``) so tests and apps with their own HTTP stack can replace the transport; the toast accepts
``runner=`` (``subprocess.run``-like) for the same reason.

Quick reference::

    from hoard_link import notify_channels as nc

    nc.send_toast("Payment failed", "Netflix 12.99 EUR", url="http://127.0.0.1:5199/#/x")
    nc.send_ntfy("https://ntfy.sh", "my-topic", "Disk full", "C: at 99%", priority="high", token=None)
    nc.send_telegram(bot_token, chat_id, nc.telegram_text("Disk full", "C: at 99%", url))
    nc.send_smtp({"host": "smtp.x.com", "port": 465, "user": "me", "password": "..."}, ["me@x.com"], "Subject", "text")
    nc.in_quiet_hours(datetime.now(), "23:00", "08:00", priority="normal")
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import shutil
import smtplib
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
HTTP_TIMEOUT_S = 10.0
TOAST_TIMEOUT_S = 20
SMTP_TIMEOUT_S = 20
NTFY_PRIORITY = {"urgent": 5, "high": 4, "normal": 3, "low": 2}
TELEGRAM_API = "https://api.telegram.org"
PRIORITIES = ("low", "normal", "high", "urgent")

Http = Callable[..., "tuple[Optional[int], Any]"]


# -- small pure helpers --------------------------------------------------------------------------------------------

def xml_escape(text: Any) -> str:
    """Escape for XML text and attribute values; also drops the control characters XML 1.0 forbids."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(text or ""))
    return _html.escape(text, quote=True)


def http_url(url: Any) -> str:
    """The URL when it is http(s), else ``""`` (a toast launch target or a ntfy click must not be anything else)."""
    u = str(url or "").strip()
    return u if urlsplit(u).scheme in ("http", "https") else ""


def clean_url(url: Any) -> str:
    """http(s) and ``hoard://`` links survive (at most 500 characters); anything else becomes ``""``."""
    u = str(url or "").strip()
    return u[:500] if urlsplit(u).scheme in ("http", "https", "hoard") else ""


def scrub(text: Any, secrets: Iterable[Any]) -> str:
    """``text`` with every secret (of four characters or more) replaced by ``***``. Use it on every error message that could
    carry a URL, a token or a password."""
    out = str(text or "")
    for s in secrets:
        s = str(s or "")
        if len(s) >= 4:
            out = out.replace(s, "***")
    return out


def is_loopback(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in ("127.0.0.1", "localhost", "::1")


def http_json(url: str, payload: Optional[dict[str, Any]] = None, headers: Optional[dict[str, str]] = None,
              timeout: float = HTTP_TIMEOUT_S) -> "tuple[Optional[int], Any]":
    """``(status, parsed json | None)``, or ``(None, "<ExceptionName>")`` when nothing answered. POST when ``payload`` is
    given, GET otherwise. Loopback targets never go through a proxy."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
                                          "User-Agent": "hoard-notify", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if is_loopback(url) else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__
    try:
        return status, json.loads(raw.decode("utf-8", "replace")) if raw else None
    except ValueError:
        return status, None


# -- Windows toast -------------------------------------------------------------------------------------------------

def build_toast_ps1(title: str, body: str = "", url: Optional[str] = None, app_name: Optional[str] = None,
                    app_id: str = POWERSHELL_APP_ID) -> str:
    """PowerShell that shows one toast through Windows.UI.Notifications (no module needed). Only an http(s) ``url`` becomes the
    click target. ``app_name`` adds a small attribution line under the text (the toast's source stays PowerShell: a custom
    application id needs a Start-menu shortcut). Title is cut at 120 characters, body at 300."""
    title, body = str(title or ""), str(body or "")
    launch = http_url(url)
    attrs = f' activationType="protocol" launch="{xml_escape(launch)}"' if launch else ""
    attribution = f'<text placement="attribution">{xml_escape(str(app_name)[:60])}</text>' if app_name else ""
    xml = (f'<toast{attrs}><visual><binding template="ToastGeneric"><text>{xml_escape(title[:120])}</text>'
           f'<text>{xml_escape(body[:300])}</text>{attribution}</binding></visual></toast>')
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\n"
        f"$xml = @'\n{xml}\n'@\n"
        "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        "$doc.LoadXml($xml)\n"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_id}').Show($toast)\n"
    )


def run_ps1(script: str, *, runner: Optional[Callable[..., Any]] = None, timeout: float = TOAST_TIMEOUT_S) -> dict[str, Any]:
    """Run a PowerShell script from a temporary ``.ps1`` (UTF-8 with BOM, which Windows PowerShell 5.1 needs to read accents),
    without a console window. ``{"ok": True}`` or ``{"ok": False, "error": "powershell exit N" | "<ExceptionName>"}``."""
    fd, path = tempfile.mkstemp(suffix=".ps1", prefix="hoard-toast-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig") as fh:
            fh.write(script)
        exe = shutil.which("powershell") or "powershell"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        done = (runner or subprocess.run)([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
                                          capture_output=True, text=True, timeout=timeout, creationflags=flags)
        code = getattr(done, "returncode", 1)
        return {"ok": True} if code == 0 else {"ok": False, "error": f"powershell exit {code}"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": type(exc).__name__}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def send_toast(title: str, body: str = "", url: Optional[str] = None, *, app_name: Optional[str] = None,
               runner: Optional[Callable[..., Any]] = None, platform: Optional[str] = None,
               timeout: float = TOAST_TIMEOUT_S) -> dict[str, Any]:
    """Show one Windows toast and wait for PowerShell. Off Windows: ``{"ok": False, "unsupported": True, "error": "unsupported"}``."""
    if not str(platform or sys.platform).startswith("win"):
        return {"ok": False, "unsupported": True, "error": "unsupported"}
    return run_ps1(build_toast_ps1(title, body, url, app_name), runner=runner, timeout=timeout)


# -- ntfy ----------------------------------------------------------------------------------------------------------

def ntfy_priority(priority: Any) -> int:
    """1-5 from a name (``low|normal|high|urgent``) or a number."""
    if isinstance(priority, (int, float)) and not isinstance(priority, bool):
        return max(1, min(5, int(priority)))
    return NTFY_PRIORITY.get(str(priority or "normal").lower(), 3)


def send_ntfy(server: str, topic: str, title: str, body: str = "", *, priority: Any = "normal", url: Optional[str] = None,
              token: Optional[str] = None, timeout: float = HTTP_TIMEOUT_S, tags: Optional[Iterable[str]] = None,
              attach: Optional[str] = None, http: Optional[Http] = None) -> dict[str, Any]:
    """Publish one message to a ntfy topic (JSON publish to the server root). ``url`` becomes the click target and ``attach`` an
    attached picture URL (http(s) only). Errors: ``not configured: missing topic``, ``invalid ntfy server``, ``http <status>``,
    or the name of the exception when nothing answered."""
    topic = str(topic or "").strip()
    server = str(server or "https://ntfy.sh").strip().rstrip("/")
    if not topic:
        return {"ok": False, "error": "not configured: missing topic"}
    if urlsplit(server).scheme not in ("http", "https"):
        return {"ok": False, "error": "invalid ntfy server"}
    title = str(title or "")
    payload: dict[str, Any] = {"topic": topic, "title": title[:250], "message": str(body or "") or title,
                               "priority": ntfy_priority(priority), "tags": list(tags or []) or ["bell"]}
    if clean_url(url):
        payload["click"] = clean_url(url)
    if http_url(attach):
        payload["attach"] = http_url(attach)
    headers = {"Authorization": "Bearer " + token} if token else {}
    status, data = (http or http_json)(server + "/", payload, headers=headers, timeout=timeout)
    if status is None:
        return {"ok": False, "error": scrub(data, [token])}
    return {"ok": True} if 200 <= status < 300 else {"ok": False, "error": f"http {status}"}


# -- Telegram ------------------------------------------------------------------------------------------------------

def telegram_text(title: str, body: str = "", url: Optional[str] = None, link_label: Optional[str] = None) -> str:
    """The HTML message ``send_telegram`` expects: bold title, the body, and the link (labelled ``link_label`` or the URL itself).
    Everything is HTML-escaped; only http(s) links are kept."""
    text = f"<b>{_html.escape(str(title or ''))}</b>"
    if body:
        text += "\n" + _html.escape(str(body))
    link = http_url(url)
    if link:
        text += f'\n<a href="{_html.escape(link, quote=True)}">{_html.escape(link_label or link)}</a>'
    return text


def send_telegram(token: str, chat_id: Any, text: str, *, timeout: float = HTTP_TIMEOUT_S, api_base: str = TELEGRAM_API,
                  parse_mode: Optional[str] = "HTML", http: Optional[Http] = None) -> dict[str, Any]:
    """Send one message with a bot. ``text`` is cut at 4000 characters; with ``parse_mode="HTML"`` build it with
    :func:`telegram_text`. Token and chat id never appear in the error text."""
    token, chat = str(token or ""), str(chat_id or "")
    if not token or not chat:
        return {"ok": False, "error": "not configured: missing bot token or chat id"}
    api = str(api_base or TELEGRAM_API).rstrip("/")
    payload: dict[str, Any] = {"chat_id": chat, "text": str(text or "")[:4000]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    status, data = (http or http_json)(f"{api}/bot{token}/sendMessage", payload, timeout=timeout)
    if status is None:
        return {"ok": False, "error": scrub(data, [token, chat])}
    if status == 200 and isinstance(data, dict) and data.get("ok"):
        return {"ok": True}
    desc = data.get("description") if isinstance(data, dict) else None
    return {"ok": False, "error": scrub(desc or f"http {status}", [token, chat])[:160]}


def telegram_discover_chat_id(token: str, *, api_base: str = TELEGRAM_API, timeout: float = HTTP_TIMEOUT_S,
                              http: Optional[Http] = None) -> dict[str, Any]:
    """The chat id of the bot's latest update (the person must have written to the bot first):
    ``{"ok": True, "chat_id", "name", "error": ""}`` or ``{"ok": False, "chat_id": "", "name": "", "error"}``."""
    fail = {"ok": False, "chat_id": "", "name": ""}
    token = str(token or "")
    if not token:
        return {**fail, "error": "save the bot token first"}
    api = str(api_base or TELEGRAM_API).rstrip("/")
    status, data = (http or http_json)(f"{api}/bot{token}/getUpdates?limit=20&timeout=0", None, timeout=timeout)
    if status is None:
        return {**fail, "error": scrub(data, [token])}
    if status != 200 or not isinstance(data, dict) or not data.get("ok"):
        desc = data.get("description") if isinstance(data, dict) else None
        return {**fail, "error": scrub(desc or f"http {status}", [token])[:160]}
    for update in reversed(data.get("result") or []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if isinstance(chat, dict) and chat.get("id") is not None:
                name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) \
                    or chat.get("username") or ""
                return {"ok": True, "chat_id": str(chat["id"]), "name": name, "error": ""}
    return {**fail, "error": "no messages yet: write to the bot first"}


# -- mail ----------------------------------------------------------------------------------------------------------

def email_parts(title: str, body: str = "", url: Optional[str] = None) -> "tuple[str, str, str]":
    """``(subject, plain text, html)`` of a notification mail (subject on one line, at most 200 characters)."""
    title, body = str(title or ""), str(body or "")
    subject = re.sub(r"[\r\n]+", " ", title)[:200]
    link = clean_url(url)
    text = (body + (f"\n\n{link}" if link else "")) or subject
    rows = "".join(f"<p>{_html.escape(line)}</p>" for line in body.splitlines() if line.strip())
    anchor = f'<p><a href="{_html.escape(link, quote=True)}">{_html.escape(link)}</a></p>' if link else ""
    return subject, text, f"<html><body><h3>{_html.escape(title)}</h3>{rows}{anchor}</body></html>"


def default_smtp(cfg: dict[str, Any]) -> Any:
    """An SMTP client for ``cfg`` (``host``, ``port``, ``tls``): implicit TLS on 465, STARTTLS on other ports, plain when
    ``tls`` is false."""
    host, port = cfg["host"], int(cfg.get("port") or 465)
    if cfg.get("tls", True):
        ctx = ssl.create_default_context()
        if port == 465:
            return smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_S, context=ctx)
        client = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_S)
        client.starttls(context=ctx)
        return client
    return smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_S)


def send_smtp(cfg: dict[str, Any], to: Any, subject: str, body: str = "", *, html: Optional[str] = None,
              smtp_factory: Optional[Callable[[dict[str, Any]], Any]] = None,
              default_from: str = "hoard@localhost") -> dict[str, Any]:
    """Send one mail with plain SMTP. ``cfg``: ``host, port, user, password, tls, from``. ``html`` adds an alternative part.
    Errors: ``not configured: missing SMTP host or recipient``, ``authentication failed``, or the exception name; the password
    is scrubbed from every error."""
    items = re.split(r"[,;]", to) if isinstance(to, str) else (to or [])
    rcpt = [str(a).strip() for a in items if str(a or "").strip()]
    if not cfg.get("host") or not rcpt:
        return {"ok": False, "error": "not configured: missing SMTP host or recipient"}
    msg = EmailMessage()
    msg["Subject"] = re.sub(r"[\r\n]+", " ", str(subject or ""))[:200]
    msg["From"] = cfg.get("from") or cfg.get("user") or default_from
    msg["To"] = ", ".join(rcpt)
    msg.set_content(str(body or subject or ""))
    if html:
        msg.add_alternative(str(html), subtype="html")
    secret = cfg.get("password") or ""
    try:
        client = (smtp_factory or default_smtp)(cfg)
    except (smtplib.SMTPException, OSError) as exc:
        return {"ok": False, "error": scrub(type(exc).__name__, [secret])}
    try:
        if cfg.get("user"):
            client.login(cfg["user"], secret)
        client.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "error": "authentication failed"}
    except (smtplib.SMTPException, OSError) as exc:
        return {"ok": False, "error": scrub(type(exc).__name__, [secret])}
    finally:
        try:
            client.quit()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True}


def send_via_helper(run: Callable[..., Any], subject: str, body: str = "", *, html: Optional[str] = None,
                    to: Optional[Iterable[str]] = None, timeout: float = 60.0) -> dict[str, Any]:
    """Send a notification mail through the Faustus account: ``run`` is ``fam_mail.FaustusHelper(...).run`` (or anything with the
    same ``run(action, payload, timeout)`` shape). Without ``to`` the account's own address receives it."""
    payload: dict[str, Any] = {"subject": str(subject or "")[:200], "text": str(body or subject or "")}
    if html:
        payload["html"] = str(html)
    if to:
        payload["to"] = [str(t) for t in to]
    try:
        answer = run("send", payload, timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}
    if not isinstance(answer, dict):
        return {"ok": False, "error": "mail helper: unexpected answer"}
    return {"ok": bool(answer.get("ok")), **({"error": str(answer.get("error"))[:200]} if answer.get("error") else {})}


# -- quiet hours ---------------------------------------------------------------------------------------------------

_HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def _minutes(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) % 1440
    m = _HHMM.match(str(value or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def in_quiet_hours(now: Optional[datetime], start: Any, end: Any, *, priority: str = "normal", allow_high: bool = True,
                   days: Optional[Iterable[int]] = None) -> bool:
    """Should a notification of ``priority`` be held back at ``now``? ``start`` / ``end``: ``"HH:MM"`` strings (or minutes since
    midnight). A window that crosses midnight belongs to the day it starts on; ``start == end`` or an unreadable time turns the
    window off. ``urgent`` is never held; ``high`` is not held either unless ``allow_high=False`` (the hub passes False: it holds
    everything but urgent). ``days``: weekday numbers (Monday = 0) the window applies to, default every day."""
    prio = str(priority or "normal").lower()
    if prio == "urgent" or (prio == "high" and allow_high):
        return False
    a, b = _minutes(start), _minutes(end)
    if a is None or b is None or a == b:
        return False
    now = now or datetime.now()
    wanted = set(days) if days is not None else set(range(7))
    minute = now.hour * 60 + now.minute
    if a < b:
        return a <= minute < b and now.weekday() in wanted
    if minute >= a:
        return now.weekday() in wanted
    if minute < b:
        return (now - timedelta(days=1)).weekday() in wanted
    return False
