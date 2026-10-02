"""The app side of the hub's mail gateway: ask the hub for the mail, instead of running your own IMAP helper.

The hub reads the inbox once for the whole family (``/api/mail/*``, facet ``mailgate``). An app registers what it is
interested in, then asks for the messages that match, and says which ones it took (``claim``) so they stop showing up in
the person's "sin dueño" tray. Keep your own mail helper as the fallback for when the hub, or its gateway, is not there::

    from hoard_link import family, fam_mail

    family.configure("ledger", DATA_DIR)                     # the app's token is how the hub knows who asks
    if fam_mail.available():
        fam_mail.register_interest({"subject_terms": ["factura", "recibo"], "from_domains": ["amazon.es"], "has_attachment": True})
        page = fam_mail.messages(since_id=last_seen)         # {ok, messages, last_id}; resume from last_id
        for m in page["messages"]:
            ...                                              # m["subject"], m["text"], m["attachments"][i]["path"] …
            fam_mail.claim([m["id"]], "payment", "hoard://ledger/tx/12")
        last_seen = page["last_id"]
    else:
        ...                                                  # the app's own helper

The same file also carries what every app used to copy: :func:`faustus_dir` / :func:`faustus_python` (find Faustus), :class:`FaustusHelper`
(run the vendored ``mail_helper.py`` with Faustus's Python: fetch, scan, read, headers, send) and :class:`MailRouter` (hub gateway first,
helper as the fallback, one watermark, one interest, one claim)::

    helper = fam_mail.FaustusHelper(lambda: settings.get("mail.faustus_dir"))
    router = fam_mail.MailRouter(helper, source_getter=lambda: settings.get("mail.source", "auto"),
                                 interest={"subject_terms": TERMS, "has_attachment": True},
                                 watermark_get=lambda: int(settings.get("mail.hub_since_id", 0)),
                                 watermark_set=lambda v: settings.put("mail.hub_since_id", v), claim_kind="document")
    rows = router.scan(since_days=30, limit=60, skip=known_ids, subject_terms=TERMS)       # list of message dicts
    ...file them...
    router.commit()                                                                          # the watermark moves only now
    router.claim(rows, ref="hoard://kafka/doc/1")                                            # "this mail is mine"

Standard library only (runs inside apps that vendor ``hoard_link/``). Nothing here raises: when the hub cannot be reached the
answer is ``{"ok": False, "error": "hub unreachable"}``. The hub only returns mail of the spheres the app is allowed in.
"""

from __future__ import annotations

import email.utils
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import family as _f
from ._hubclient import fetch

CACHE_S = 30.0
_cache: dict[str, Any] = {"at": 0.0, "key": "", "ok": False}
_lock = threading.Lock()


def _get(path: str, timeout: float) -> tuple[Optional[int], Any]:
    return fetch(_f._hub() + path, method="GET", timeout=timeout, headers=_f._headers())


def _post(path: str, body: dict[str, Any], timeout: float) -> tuple[Optional[int], Any]:
    return _f._post(path, body, timeout)


def _answer(status: Optional[int], body: Any) -> dict[str, Any]:
    if status is None:
        return {"ok": False, "error": "hub unreachable"}
    if not isinstance(body, dict):
        return {"ok": False, "status": status, "error": f"HTTP {status}"}
    body.setdefault("ok", 200 <= status < 300)
    if status == 401:
        body["error"] = f"the hub refused this app's token ({_f.status().get('token_file') or 'no token file'})"
    return body


def available(timeout: float = 1.0) -> bool:
    """True when the hub is up AND its mail gateway is on and has read the inbox at least once (and recently).
    Cached for 30 seconds; when it is False, use the app's own mail helper."""
    key = _f._hub() + "|" + _f._token()
    with _lock:
        if _cache["key"] == key and time.time() - _cache["at"] < CACHE_S:
            return bool(_cache["ok"])
    status, body = _get("/api/mail/status", timeout)
    ok = False
    if status == 200 and isinstance(body, dict) and body.get("ready"):
        fresh, interval = body.get("fresh_s"), int(body.get("interval_min") or 0)
        ok = fresh is None or interval == 0 or float(fresh) <= interval * 180 + 900        # a stalled hub pass is "not available"
    with _lock:
        _cache.update(at=time.time(), key=key, ok=ok)
    return ok


def forget_availability() -> None:
    """Drop the 30-second cache (after changing the hub, or in tests)."""
    with _lock:
        _cache.update(at=0.0, key="", ok=False)


def register_interest(spec: dict[str, Any], sphere: Optional[str] = None, timeout: float = 10.0) -> dict[str, Any]:
    """Tell the hub which mail this app wants. ``spec``: ``subject_terms, from_domains, from_addresses, text_terms, regex,
    has_attachment`` — a message matches when ANY non-empty criterion matches (case-insensitive, accents folded). Optional extras
    (0.8): ``exclude`` (a spec with the same keys: a message that matches it is dropped), ``all_of`` (a list of specs that must ALL
    match, each with the any-of rule; with the plain criteria both must hold) and ``category`` (``promo|social|security|dev|other``,
    a string or a list: only messages of those categories). ``sphere`` limits it to one sphere. Registering again replaces the
    previous interest."""
    body: dict[str, Any] = {"spec": spec or {}}
    if sphere:
        body["sphere"] = sphere
    return _answer(*_post("/api/mail/interests", body, timeout))


def _kafka_shape(m: dict[str, Any]) -> dict[str, Any]:
    """The keys the Kafka-style mail helper's record has, next to the gateway's own."""
    name, addr = str(m.get("from_name") or ""), str(m.get("from_addr") or "")
    ts = m.get("date_ts") or None
    m["from"] = f"{name} <{addr}>" if name and addr else (addr or name)
    m["from_address"] = addr
    m["ts"] = ts
    m["date"] = email.utils.formatdate(ts) if ts else ""
    m["account"] = m.get("source") or ""
    m["from_self"] = "own mail" in (m.get("reasons") or [])
    m.setdefault("text", "")
    m.setdefault("links", [])
    m.setdefault("attachments", [])
    return m


def _fields_param(fields: Any) -> str:
    """``"html,images"`` from a list or a comma string; only the names the gateway knows."""
    items = re.split(r"[,\s]+", fields) if isinstance(fields, str) else list(fields or [])
    names = list(dict.fromkeys(f for f in (str(i).strip().lower() for i in items) if f in ("html", "images", "headers", "all")))
    return "html,images,headers" if "all" in names else ",".join(names)


def messages(since_id: int = 0, limit: int = 100, full: bool = True, interest: bool = True, timeout: float = 20.0,
             fields: Any = None) -> dict[str, Any]:
    """The messages the hub has stored with ``id`` > ``since_id`` (oldest first), for this app's spheres; with
    ``interest=True`` only those that match the interest it registered. ``{ok, messages, last_id}``: pass ``last_id`` as the
    next ``since_id``. Every message carries the gateway's keys (``id, source, sphere, from_addr, from_name, to, subject,
    snippet, priority, text, links, attachments[{name, mime, size, sha, path, url}]``) AND the Kafka helper's
    (``message_id, subject, from, from_address, date, ts, text, links, attachments`` with the local ``path``).

    ``fields`` (list or comma string) asks for what the default answer leaves out: ``html`` (the raw HTML part, only present for mail
    that carries structured markup), ``images`` (``[{alt, src}]``) and ``headers`` (``{list_unsubscribe, one_click, gmail_category,
    message_id}``); ``"all"`` is the three. Without it the answer is what it always was."""
    q = f"since_id={int(since_id)}&limit={int(limit)}&kind=mail&interest={1 if interest else 0}" + ("&full=1" if full else "")
    wanted = _fields_param(fields)
    if wanted:
        q += "&fields=" + wanted
    res = _answer(*_get("/api/mail/messages?" + q, timeout))
    if res.get("ok"):
        res["messages"] = [_kafka_shape(m) for m in res.get("messages") or [] if isinstance(m, dict)]
        if wanted:
            for m in res["messages"]:
                if "html" in wanted:
                    m.setdefault("html", "")
                if "images" in wanted:
                    m.setdefault("images", [])
                if "headers" in wanted:
                    m.setdefault("headers", {})
        res.setdefault("last_id", int(since_id))
    else:
        res.setdefault("messages", [])
        res.setdefault("last_id", int(since_id))
    return res


def claim(ids: list[int], kind: str, ref: str, timeout: float = 10.0) -> dict[str, Any]:
    """Record "this mail is mine" (``kind`` e.g. ``payment`` / ``document`` / ``shipment``; ``ref`` the ``hoard://`` uri of what the
    app made from it). A claimed message leaves the "needs you" and "sin dueño" lists."""
    return _answer(*_post("/api/mail/claim", {"ids": [int(i) for i in ids], "kind": kind, "ref": ref}, timeout))


def copy_attachment(att: dict[str, Any], dest_dir: str, timeout: float = 30.0) -> str:
    """Copy one attachment (an element of a message's ``attachments``) into ``dest_dir`` and return the new path ('' when it cannot
    be had). Uses the local file when the hub's ``path`` is readable from here, else downloads it through the hub."""
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError:
        return ""
    src = str(att.get("path") or "")
    name = str(att.get("sha") or "") or os.path.splitext(os.path.basename(src))[0]
    ext = os.path.splitext(src)[1] or os.path.splitext(str(att.get("name") or ""))[1] or ".bin"
    dest = os.path.join(dest_dir, (name or "attachment") + ext.lower())
    if os.path.isfile(dest):
        return dest
    try:
        if src and os.path.isfile(src):
            shutil.copyfile(src, dest)
            return dest
        url = str(att.get("url") or "")
        if not url:
            return ""
        req = urllib.request.Request(_f._hub() + url if url.startswith("/") else url, headers=_f._headers())
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as resp, open(dest + ".part", "wb") as out:
            shutil.copyfileobj(resp, out)
        os.replace(dest + ".part", dest)
        return dest
    except (OSError, urllib.error.URLError, ValueError):
        try:
            os.remove(dest + ".part")
        except OSError:
            pass
        return ""


# ---------------------------------------------------------------------------------------------------------------------
# Finding Faustus and running the vendored mail helper with its Python
# ---------------------------------------------------------------------------------------------------------------------

HELPER = Path(__file__).with_name("mail_helper.py")
HELPER_TIMEOUT_S = 180.0
STATUS_TTL_S = 300.0
FAUSTUS_ENV = ("FAUSTUS_DIR", "HOARD_FAUSTUS_DIR", "HOARD_HUB_FAUSTUS_DIR")
FAUSTUS_PYTHON_ENV = ("FAUSTUS_PYTHON", "HOARD_FAUSTUS_PYTHON", "HOARD_HUB_FAUSTUS_PYTHON")
PYTHON_CANDIDATES = ("venv/Scripts/python.exe", ".venv/Scripts/python.exe", "venv/bin/python", ".venv/bin/python")
COMMON_FAUSTUS_PATHS = (r"D:\LocalAI\faustus", r"C:\LocalAI\faustus", "~/LocalAI/faustus", "~/faustus")
_hub_hint: dict[str, Any] = {"at": 0.0, "key": "", "dir": ""}


def _is_faustus(path: Path) -> bool:
    try:
        return (path / "mcp_servers" / "email_server.py").is_file()
    except OSError:
        return False


def _sibling_candidates() -> list[Path]:
    """``faustus`` folders next to this package, next to the app that vendors it and next to every folder above that (up to five levels)."""
    out: list[Path] = []
    try:
        here = Path(__file__).resolve().parent
    except OSError:
        return out
    for ancestor in list(here.parents)[:5]:
        for name in ("faustus", "Faustus"):
            out.append(ancestor / name)
    return out


def _hub_faustus_hint(timeout: float = 1.0) -> str:
    """The Faustus folder the hub's mail gateway uses (``/api/mail/status``), remembered for a minute (misses too)."""
    key = _f._hub() + "|" + _f._token()
    with _lock:
        if _hub_hint["key"] == key and time.time() - _hub_hint["at"] < 60:
            return str(_hub_hint["dir"])
    status, body = _get("/api/mail/status", timeout)
    found = str(body.get("faustus_dir") or "") if status == 200 and isinstance(body, dict) else ""
    with _lock:
        _hub_hint.update(at=time.time(), key=key, dir=found)
    return found


def faustus_dir(setting: Any = None, *, ask_hub: bool = True, extra: Iterable[Any] = ()) -> Optional[Path]:
    """The Faustus folder (the one that has ``mcp_servers/email_server.py``), or None. The ONE implementation; first hit wins:

    1. ``setting`` (the app's own setting; a path or a callable returning one),
    2. the environment: ``FAUSTUS_DIR``, ``HOARD_FAUSTUS_DIR``, ``HOARD_HUB_FAUSTUS_DIR``,
    3. a ``faustus`` folder next to this package, the app that vendors it, or any folder above (up to five levels),
    4. the usual places: ``D:\\LocalAI\\faustus``, ``C:\\LocalAI\\faustus``, ``~/LocalAI/faustus``, ``~/faustus``, then ``extra``,
    5. what the hub's mail gateway says it uses (``ask_hub``; the hub itself passes False).
    """
    try:
        configured = setting() if callable(setting) else setting
    except Exception:  # noqa: BLE001
        configured = None
    raw: list[Any] = [configured, *(os.environ.get(k) for k in FAUSTUS_ENV)]
    candidates: list[Path] = []
    for item in raw:
        if item and str(item).strip():
            candidates.append(Path(str(item).strip()).expanduser())
    candidates += _sibling_candidates()
    candidates += [Path(p).expanduser() for p in COMMON_FAUSTUS_PATHS]
    candidates += [Path(str(e)).expanduser() for e in extra if e and str(e).strip()]
    for path in candidates:
        if _is_faustus(path):
            try:
                return path.resolve()
            except OSError:
                return path
    if ask_hub:
        hint = _hub_faustus_hint()
        if hint and _is_faustus(Path(hint)):
            return Path(hint)
    return None


def faustus_python(root: Any, setting: Any = None) -> Optional[str]:
    """Faustus's own interpreter: its ``venv`` / ``.venv`` (Windows or POSIX layout), else ``setting``, else ``FAUSTUS_PYTHON`` /
    ``HOARD_FAUSTUS_PYTHON`` / ``HOARD_HUB_FAUSTUS_PYTHON`` when that file exists."""
    if root:
        for rel in PYTHON_CANDIDATES:
            cand = Path(str(root)) / rel
            try:
                if cand.is_file():
                    return str(cand)
            except OSError:
                continue
    try:
        configured = setting() if callable(setting) else setting
    except Exception:  # noqa: BLE001
        configured = None
    for item in (configured, *(os.environ.get(k) for k in FAUSTUS_PYTHON_ENV)):
        if item and os.path.isfile(str(item)):
            return str(item)
    return None


def _value(source: Any) -> str:
    try:
        v = source() if callable(source) else source
    except Exception:  # noqa: BLE001
        v = None
    return str(v or "").strip()


class FaustusHelper:
    """Runs the vendored ``mail_helper.py`` with Faustus's Python (so the mail password never leaves Faustus).

    ``setting`` is where the person said Faustus lives (a path or a callable; empty = discover it with :func:`faustus_dir`);
    ``owner`` picks the Faustus user when several have accounts; ``python`` overrides the interpreter; ``env_drop_prefixes`` removes
    the app's own secrets (``("KAFKA_",)``) from the child's environment; ``runner`` replaces ``subprocess.run`` (tests)."""

    def __init__(self, setting: Any = None, *, owner: Any = None, python: Any = None, runner: Optional[Callable[..., Any]] = None,
                 env_drop_prefixes: Iterable[str] = (), ask_hub: bool = True, clock: Callable[[], float] = time.time):
        self.setting, self.owner, self.python_setting = setting, owner, python
        self.runner = runner or subprocess.run
        self.env_drop_prefixes = tuple(env_drop_prefixes)
        self.ask_hub = ask_hub
        self.clock = clock
        self._status: Optional[tuple[float, str, dict[str, Any]]] = None

    def faustus_dir(self) -> Optional[Path]:
        return faustus_dir(self.setting, ask_hub=self.ask_hub)

    def python(self) -> Optional[str]:
        root = self.faustus_dir()
        return faustus_python(root, self.python_setting) if root else None

    def available(self) -> bool:
        """True when Faustus's folder and its Python were found. Does not start anything."""
        return self.faustus_dir() is not None and self.python() is not None

    def run(self, action: str, payload: Optional[dict[str, Any]] = None, timeout: float = HELPER_TIMEOUT_S) -> dict[str, Any]:
        """Run one helper action (``status | fetch | scan | headers | read | send``, see ``mail_helper.py``) and return its answer.
        Never raises: failures come back as ``{"ok": False, "error": ...}``. No console window opens on Windows."""
        root = self.faustus_dir()
        if root is None:
            return {"ok": False, "error": "Faustus folder not found (set it in the app's mail settings, or FAUSTUS_DIR)"}
        python = faustus_python(root, self.python_setting)
        if python is None:
            return {"ok": False, "error": "Faustus has no venv with Python"}
        request = {**(payload or {}), "action": str(action)}
        owner = _value(self.owner)
        if owner and not request.get("owner"):
            request["owner"] = owner
        env = {k: v for k, v in os.environ.items() if not any(k.startswith(p) for p in self.env_drop_prefixes)}
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            done = self.runner([python, str(HELPER), str(root)], input=json.dumps(request, ensure_ascii=False), capture_output=True,
                               text=True, encoding="utf-8", timeout=timeout, cwd=str(root), env=env,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "the mail read took too long"}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": f"mail helper: {type(exc).__name__}"}
        lines = [ln for ln in (getattr(done, "stdout", "") or "").splitlines() if ln.strip().startswith("{")]
        try:
            answer = json.loads(lines[-1]) if lines else {}
        except ValueError:
            answer = {}
        if not isinstance(answer, dict) or "ok" not in answer:
            return {"ok": False, "error": f"mail helper exit {getattr(done, 'returncode', '?')}"}
        return answer

    def status(self, refresh: bool = False) -> dict[str, Any]:
        """``run("status")``, remembered for five minutes (thirty seconds when it failed). Adds ``faustus_dir``."""
        key = str(self.faustus_dir() or "") + "|" + _value(self.owner)
        hit = self._status
        if hit and not refresh and hit[1] == key and self.clock() - hit[0] < (STATUS_TTL_S if hit[2].get("ok") else 30.0):
            return hit[2]
        answer = self.run("status", timeout=60.0)
        answer["faustus_dir"] = key.split("|", 1)[0]
        self._status = (self.clock(), key, answer)
        return answer


# ---------------------------------------------------------------------------------------------------------------------
# The router: hub gateway first, the helper as the fallback
# ---------------------------------------------------------------------------------------------------------------------

SOURCES = ("auto", "hub", "faustus")
_ROUTER_KEYS = ("limit", "deep", "fields")


def _error_of(answer: Any) -> str:
    return str(answer.get("error") or "") if isinstance(answer, dict) else ""


def _domain(address: Any) -> str:
    a = str(address or "").strip().lower()
    return a.rsplit("@", 1)[1].strip(" >") if "@" in a else ""


class MailRouter:
    """The mail source of an app: the hub's gateway when it is ready, the Faustus helper otherwise.

    * ``helper``: a :class:`FaustusHelper` (anything with ``available()`` and ``run(action, payload, timeout)``).
    * ``source_getter()``: the person's setting, ``auto`` (hub when its gateway answers, else the helper; the default), ``hub``
      (hub only, errors are reported) or ``faustus`` (helper only; ``own`` and ``helper`` are accepted too).
    * ``interest``: what to register at the hub: a spec dict, or ``callable(criteria) -> spec``. It is registered again every
      ``register_every_s`` (6 h) and whenever the spec changes. ``sphere`` limits it to one sphere.
    * ``watermark_get()`` / ``watermark_set(id)``: where the app keeps "the hub's message id I have read up to". Without them the
      watermark lives in memory.
    * ``claim_kind``: the kind of the claims (``payment``, ``document``, ``shipment`` ...); ``auto_claim=True`` makes :meth:`scan` claim
      what it returns (off by default: most apps claim only what they filed, with a ``hoard://`` ref, through :meth:`claim`).
    * ``page`` / ``max_pages``: how the hub is read (100 messages a page, six pages a scan; a long backlog is finished by the next scans).
    * ``deep_uses_helper``: a ``deep=True`` scan (looking further back than the hub stored) goes to the helper when it is available.

    ``scan(**criteria)`` -> ``list[dict]``. Criteria: ``since_days`` (30), ``limit`` (100), ``skip`` (message ids already known),
    ``query`` (words that must all appear), ``deep`` (re-read from the start of what the hub holds and leave the watermark alone),
    ``fields`` (``html``, ``images``, ``headers`` to ask of the hub), ``sender_domains``, ``skip_own``, ``order`` (``asc`` | ``desc`` by
    date; default: hub oldest first, helper newest first); every other key (``subject_terms``, ``gmail_query``, ``attachments_dir``,
    ``account``, ...) goes to the helper as it is. Messages from the hub carry ``hub_id``; those from the helper do not.
    After the call: ``last_source`` (``hub`` | ``faustus`` | ``""``), ``last_error`` and ``last_meta``. The watermark moves only on
    :meth:`commit` (call it once the messages are filed); a scan that failed never moves it.
    """

    def __init__(self, helper: Any, *, source_getter: Optional[Callable[[], Any]] = None, interest: Any = None,
                 watermark_get: Optional[Callable[[], Any]] = None, watermark_set: Optional[Callable[[int], Any]] = None,
                 claim_kind: str = "", page: int = 100, max_pages: int = 6, register_every_s: float = 21600.0,
                 sphere: Optional[str] = None, hub: Any = None, clock: Callable[[], float] = time.time,
                 deep_uses_helper: bool = False, auto_claim: bool = False):
        self.helper = helper
        self.source_getter = source_getter
        self.interest = interest
        self._wm_get, self._wm_set = watermark_get, watermark_set
        self._wm_mem = 0
        self.claim_kind = str(claim_kind or "")
        self.page = max(1, int(page))
        self.max_pages = max(1, int(max_pages))
        self.register_every_s = float(register_every_s)
        self.sphere = sphere
        self._hub = hub
        self.clock = clock
        self.deep_uses_helper = deep_uses_helper
        self.auto_claim = auto_claim
        self.last_source = ""
        self.last_error = ""
        self.last_meta: dict[str, Any] = {}
        self._registered_at = 0.0
        self._registered_sig = ""
        self._pending: Optional[int] = None
        self._lock = threading.RLock()

    # -- the pieces ------------------------------------------------------------------------------------------
    @property
    def hub(self) -> Any:
        return self._hub if self._hub is not None else sys.modules[__name__]

    def mode(self) -> str:
        try:
            value = str(self.source_getter() if self.source_getter else "auto").strip().lower()
        except Exception:  # noqa: BLE001
            return "auto"
        value = "faustus" if value in ("own", "helper") else value
        return value if value in SOURCES else "auto"

    def hub_up(self) -> bool:
        try:
            return bool(self.hub.available())
        except Exception:  # noqa: BLE001
            return False

    def helper_up(self) -> bool:
        try:
            return bool(self.helper is not None and self.helper.available())
        except Exception:  # noqa: BLE001
            return False

    def source_now(self, *, deep: bool = False) -> str:
        """``hub`` or ``faustus``: where a scan would read now."""
        mode = self.mode()
        if mode == "faustus":
            return "faustus"
        if mode == "hub":
            return "hub"
        if deep and self.deep_uses_helper and self.helper_up():
            return "faustus"
        return "hub" if self.hub_up() else "faustus"

    def watermark(self) -> int:
        try:
            return int((self._wm_get() if self._wm_get else self._wm_mem) or 0)
        except (TypeError, ValueError):
            return 0

    def status(self) -> dict[str, Any]:
        up = self.hub_up() if self.mode() != "faustus" else False
        return {"setting": self.mode(), "effective": self.source_now(), "hub_available": up, "helper_available": self.helper_up(),
                "interest_registered": bool(self._registered_sig), "hub_since_id": self.watermark(),
                "last_source": self.last_source, "last_error": self.last_error}

    def forget_interest(self) -> None:
        with self._lock:
            self._registered_at, self._registered_sig = 0.0, ""

    def ensure_interest(self, criteria: Optional[dict[str, Any]] = None, *, force: bool = False) -> dict[str, Any]:
        """Register the interest at the hub: once, then every ``register_every_s``, and again when the spec changes."""
        spec = self.interest(criteria or {}) if callable(self.interest) else self.interest
        if not isinstance(spec, dict) or not spec:
            return {"ok": True, "skipped": "no interest"}
        sig = json.dumps(spec, sort_keys=True, default=str) + "|" + str(self.sphere or "")
        with self._lock:
            if not force and sig == self._registered_sig and self.clock() - self._registered_at < self.register_every_s:
                return {"ok": True, "cached": True}
        try:
            answer = self.hub.register_interest(spec, self.sphere) if self.sphere else self.hub.register_interest(spec)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"hub interest: {type(exc).__name__}"}
        if isinstance(answer, dict) and answer.get("ok"):
            with self._lock:
                self._registered_at, self._registered_sig = self.clock(), sig
            return {"ok": True}
        return {"ok": False, "error": _error_of(answer) or "the hub refused the interest"}

    # -- scanning --------------------------------------------------------------------------------------------
    def scan_ex(self, **criteria: Any) -> dict[str, Any]:
        """Like :meth:`scan` but returns the whole answer: ``{ok, source, error, messages, more, pending_since, read}``."""
        deep = bool(criteria.get("deep"))
        use = self.source_now(deep=deep)
        answer: Optional[dict[str, Any]] = None
        if use == "hub":
            answer = self._scan_hub(criteria)
            if not answer["ok"] and self.mode() == "auto":
                answer = self._scan_helper(criteria, fallback_from=answer.get("error", ""))
        else:
            answer = self._scan_helper(criteria)
        with self._lock:
            self._pending = answer.get("pending_since")
            self.last_source = answer["source"] if answer["ok"] else ""
            self.last_error = "" if answer["ok"] else str(answer.get("error") or "")
            self.last_meta = {k: v for k, v in answer.items() if k != "messages"}
        if answer["ok"] and self.auto_claim and answer["source"] == "hub":
            self.claim(answer["messages"])
        return answer

    def scan(self, **criteria: Any) -> list[dict[str, Any]]:
        """The messages that match ``criteria`` (see the class doc); ``[]`` when nothing matched or nobody could answer
        (``last_error`` says which)."""
        return list(self.scan_ex(**criteria).get("messages") or [])

    def commit(self) -> None:
        """Move the watermark to where the last hub scan read up to. Call it once the app has stored what the scan returned."""
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is None:
            return
        if self._wm_set:
            self._wm_set(pending)
        else:
            self._wm_mem = int(pending)

    def claim(self, messages: Any, ref: Any = "", kind: Optional[str] = None) -> dict[str, Any]:
        """Tell the hub "this mail is mine" for every message that came from the hub (those with ``hub_id``). ``ref`` is the
        ``hoard://`` uri of what the app made from it: a string, or ``callable(message) -> str``. A hint: it never raises."""
        rows = [messages] if isinstance(messages, dict) else [m for m in (messages or []) if isinstance(m, dict)]
        groups: dict[str, list[int]] = {}
        for m in rows:
            hub_id = m.get("hub_id")
            if hub_id in (None, ""):
                continue
            try:
                r = str(ref(m) if callable(ref) else ref or "")
                groups.setdefault(r, []).append(int(hub_id))
            except (TypeError, ValueError):
                continue
        claimed = 0
        for r, ids in groups.items():
            try:
                res = self.hub.claim(ids, str(kind or self.claim_kind), r)
                claimed += int((res or {}).get("claimed") or 0) if isinstance(res, dict) else 0
            except Exception:  # noqa: BLE001
                continue
        return {"ok": True, "claimed": claimed, "groups": len(groups)}

    def _scan_helper(self, criteria: dict[str, Any], fallback_from: str = "") -> dict[str, Any]:
        limit = int(criteria.get("limit") or 100)
        payload = {k: v for k, v in criteria.items() if k not in _ROUTER_KEYS and k != "order"}
        payload["max"] = limit
        payload.setdefault("since_days", 30)
        try:
            res = self.helper.run("scan", payload, HELPER_TIMEOUT_S) if self.helper is not None else \
                {"ok": False, "error": "no mail helper configured"}
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "error": f"mail helper: {type(exc).__name__}"}
        if not isinstance(res, dict) or not res.get("ok"):
            err = _error_of(res) or "the mail helper failed"
            return {"ok": False, "source": "faustus", "error": err, "messages": [], "more": False, "pending_since": None,
                    "fallback_from": fallback_from}
        rows = [m for m in res.get("messages") or [] if isinstance(m, dict)]
        if criteria.get("order") in ("asc", "desc"):
            rows.sort(key=lambda m: m.get("ts") or 0, reverse=criteria["order"] == "desc")
        return {"ok": True, "source": "faustus", "error": "", "messages": rows, "more": False, "pending_since": None,
                "accounts": res.get("accounts") or [], "read": len(rows), "fallback_from": fallback_from}

    def _scan_hub(self, criteria: dict[str, Any]) -> dict[str, Any]:
        def fail(error: str) -> dict[str, Any]:
            return {"ok": False, "source": "hub", "error": error, "messages": [], "more": False, "pending_since": None}

        registered = self.ensure_interest(criteria)
        if not registered.get("ok"):
            return fail(str(registered.get("error") or "hub interest failed")[:200])
        limit = max(1, int(criteria.get("limit") or 100))
        deep = bool(criteria.get("deep"))
        days = float(criteria.get("since_days") or 30)
        fields = criteria.get("fields")
        known = {str(x) for x in (criteria.get("skip") or [])}
        words = [w for w in str(criteria.get("query") or "").lower().split() if w]
        domains = [str(d).lower().lstrip("@") for d in (criteria.get("sender_domains") or []) if str(d).strip()]
        skip_own = bool(criteria.get("skip_own"))
        watermark = 0 if deep else self.watermark()
        cutoff = self.clock() - days * 86400.0 if (deep or watermark == 0) else 0.0
        out: list[dict[str, Any]] = []
        last, read, got_any, more = watermark, 0, False, False
        for _ in range(self.max_pages):
            kw: dict[str, Any] = {"since_id": last, "limit": self.page, "full": True}
            if fields:
                kw["fields"] = fields
            page = self.hub.messages(**kw)
            if not isinstance(page, dict) or not page.get("ok"):
                if not read:
                    return fail(_error_of(page) or "the hub did not answer")
                break
            rows = [m for m in page.get("messages") or [] if isinstance(m, dict)]
            if not rows:
                last = max(last, int(page.get("last_id") or last))
                break
            got_any = True
            read += len(rows)
            full = False
            for m in rows:
                last = max(last, int(m.get("id") or 0))
                if str(m.get("message_id") or "") in known:
                    continue
                ts = m.get("ts")
                if cutoff and ts and float(ts) < cutoff:
                    continue
                if skip_own and m.get("from_self"):
                    continue
                if domains:
                    dom = _domain(m.get("from_address") or m.get("from_addr"))
                    if not any(dom == d or dom.endswith("." + d) for d in domains):
                        continue
                if words:
                    hay = " ".join(str(m.get(k) or "") for k in ("subject", "text", "from_address", "from_name")).lower()
                    if not all(w in hay for w in words):
                        continue
                m["hub_id"] = m.get("id")
                m.setdefault("account", m.get("source") or "hub")
                out.append(m)
                if len(out) >= limit:
                    full = True
                    break
            if full:
                more = True
                break
            more = len(rows) >= self.page    # a full page means there may be more (when the page budget ends first, it stays True)
            if not more:
                break
        if criteria.get("order") in ("asc", "desc"):
            out.sort(key=lambda m: m.get("ts") or 0, reverse=criteria["order"] == "desc")
        pending = None if deep else (last if (got_any or last != watermark) else None)
        return {"ok": True, "source": "hub", "error": "", "messages": out, "more": more, "pending_since": pending, "read": read,
                "accounts": [{"account": "hub gateway", "matches": len(out)}]}
