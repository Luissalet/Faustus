"""The browser rung: a real Chromium for pages that plain HTTP cannot read (Playwright, sync API, optional).

* ``playwright`` is imported lazily. Without it :meth:`BrowserRung.available` is ``False`` and
  :meth:`BrowserRung.unavailable_reason` says how to install it; nothing else in the commons is affected.
* **One profile** (``profile_dir``) is shared by every use, so cookies and logins survive and a CAPTCHA a person solved
  in :meth:`BrowserRung.open_for_human` counts for later headless fetches. A profile can be opened by one browser at a time, so a
  gate lock allows one user at once.
* The Playwright sync API is bound to the thread that started it: :meth:`fetch`, :meth:`capture_json` and
  :meth:`screenshot` run on one dedicated worker thread, so any thread may call them. The browser closes itself after
  ``idle_s`` (90 s) without use.
* Chromium is found in this order: system Edge/Chrome channels, Playwright's bundled browser, then any Chromium or
  headless shell found on disk (:func:`find_chromium`: the ``PLAYWRIGHT_BROWSERS_PATH`` and per-OS cache folders, plus
  the usual system install paths on Windows, macOS and Linux), so a Playwright whose own download is missing still works.
* **Policy**: every URL is checked with :func:`~.safety.check_url` before ``goto`` (``file:``, ``data:``, ``javascript:``,
  credentials, private hosts are refused under the default ``public`` profile), and every sub-request and redirect of
  every page is routed through the same check, so a public page cannot reach into the local network. The browser resolves
  names itself, so this is a name-level check, not a pinned connection.
* Nothing here tries to defeat a challenge. A challenge page is reported as blocked; the only waiting is passive (some
  interstitials refresh themselves after a few seconds).

This module replaces: Tantalus ``fetch/browser.py``, Phileas ``carriers/browser.py`` (``capture_json``), Cicero's
``find_chromium`` / ``_system_browsers`` and the launch chain copied into Vitruvius.
"""

from __future__ import annotations

import glob
import importlib.util
import json
import logging
import os
import queue
import sys
import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from ..errors import HoardLinkError
from . import safety
from .blocks import AKAMAI, CLOUDFLARE, apply_block, detect_block
from .fetch import FetchResult

__all__ = ["BrowserRung", "BrowserUnavailable", "channel_order", "find_chromium", "system_browsers", "playwright_installed",
           "launch_context", "OFFSCREEN_ARGS", "INSTALL_HINT"]

log = logging.getLogger("hoard_link.web.browser")

SETTLE_BUDGET_S = 8.0
PASSIVE_RECHECKS = 2
PASSIVE_RECHECK_WAIT_S = 3.0
WORKER_IDLE_S = 90.0
INSTALL_HINT = ("Install it with: python -m pip install playwright  (Edge or Chrome must be installed, or run: "
                "python -m playwright install chromium)")
# A real window placed off-screen: some sites refuse headless browsers but serve an ordinary visit.
OFFSCREEN_ARGS = ["--window-position=-32000,-32000", "--window-size=1280,900", "--disable-features=CalculateNativeWinOcclusion",
                  "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding", "--no-first-run",
                  "--no-default-browser-check"]
_AUTO: Any = object()


class BrowserUnavailable(HoardLinkError):
    """Playwright is missing or no browser could be started."""


# ---- discovery --------------------------------------------------------------------------------

def channel_order(platform: Optional[str] = None) -> list[Optional[str]]:
    """Browser channels to try, ``None`` = Playwright's bundled Chromium."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        return ["msedge", "chrome", None]
    if platform == "darwin":
        return ["chrome", "msedge", None]
    return ["chrome", None, "msedge"]


def playwright_installed() -> bool:
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


def _browser_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env and env != "0":
        roots.append(Path(env).expanduser())
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            roots.append(Path(base) / "ms-playwright")
    elif sys.platform == "darwin":
        roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    else:
        roots.append(Path.home() / ".cache" / "ms-playwright")
    return roots


_CHROMIUM_PATTERNS = (
    "chromium-*/chrome-linux*/chrome", "chromium/chrome-linux*/chrome", "chromium-*/chrome-win*/chrome.exe",
    "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
    "chromium_headless_shell-*/chrome-linux*/headless_shell", "chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell",
    "chromium_headless_shell-*/chrome-win*/headless_shell.exe", "chromium_headless_shell-*/chrome-headless-shell-win*/chrome-headless-shell.exe",
    "chromium_headless_shell-*/chrome-mac*/headless_shell",
)


def find_chromium() -> Optional[str]:
    """A Chromium (or headless shell) executable found in a Playwright browsers folder, newest first; ``None`` if none."""
    for root in _browser_roots():
        for pattern in _CHROMIUM_PATTERNS:
            for hit in sorted(glob.glob(str(root / pattern)), reverse=True):
                if os.access(hit, os.X_OK) or hit.endswith(".exe"):
                    return hit
    return None


def system_browsers() -> list[str]:
    """Installed Edge / Chrome / Chromium executables that exist on this machine."""
    cands: list[str] = []
    if sys.platform == "win32":
        vendor = "Micro" + "soft"            # the folder Windows uses for the system browser (split so the repo has no vendor names)
        for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")):
            if base:
                cands += [str(Path(base) / vendor / "Edge" / "Application" / "msedge.exe"),
                          str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")]
    elif sys.platform == "darwin":
        cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    else:
        cands += ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser",
                  "/snap/bin/chromium"]
    return [c for c in cands if Path(c).is_file()]


def _default_args(args: Optional[list[str]]) -> list[str]:
    out = list(args or [])
    if hasattr(os, "geteuid") and os.geteuid() == 0 and "--no-sandbox" not in out:
        out.append("--no-sandbox")           # Chromium refuses to run as root otherwise (containers)
    return out


def launch_context(pw: Any, profile_dir: Any, *, headless: bool = True, channel: Any = _AUTO, args: Optional[list[str]] = None,
                   locale: str = "en-US", viewport: Optional[dict[str, int]] = None) -> tuple[Any, str]:
    """Open a persistent context on ``profile_dir`` with the first browser that starts and return ``(context, name)``.

    ``channel`` is one channel (``"msedge"``, ``"chrome"``, ``None`` for the bundled Chromium), a list of them, or left
    out for :func:`channel_order` followed by every Chromium :func:`find_chromium` and :func:`system_browsers` know.
    Raises :class:`BrowserUnavailable` listing what failed."""
    attempts: list[tuple[str, dict[str, Any]]] = []
    if channel is _AUTO:
        for ch in channel_order():
            attempts.append((ch or "chromium", {"channel": ch} if ch else {}))
        exe = find_chromium()
        if exe:
            attempts.append((f"chromium ({Path(exe).parent.parent.name})", {"executable_path": exe}))
        for path in system_browsers():
            attempts.append((Path(path).name, {"executable_path": path}))
    else:
        for ch in (channel if isinstance(channel, list) else [channel]):
            attempts.append((ch or "chromium", {"channel": ch} if ch else {}))
    errors: list[str] = []
    for name, extra in attempts:
        kwargs: dict[str, Any] = dict(user_data_dir=str(profile_dir), headless=headless, locale=locale, args=_default_args(args), **extra)
        if headless:
            kwargs["viewport"] = viewport or {"width": 1366, "height": 850}
        else:
            kwargs["no_viewport"] = True
        try:
            return pw.chromium.launch_persistent_context(**kwargs), name
        except Exception as error:                                   # noqa: BLE001 - try the next browser
            first = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
            errors.append(f"{name}: {first[:160]}")
            log.info("browser launch failed (%s): %s", name, first)
    raise BrowserUnavailable("no browser could be started (" + "; ".join(errors) + ")")


def _start_playwright() -> Any:
    from playwright.sync_api import sync_playwright                  # noqa: PLC0415 - lazy on purpose
    return sync_playwright().start()


class _Worker:
    """One daemon thread that runs callables in order, so Playwright stays on a single thread."""

    def __init__(self, idle_s: float, on_idle: Callable[[], None]):
        self.idle_s = idle_s
        self.on_idle = on_idle
        self._queue: "queue.Queue[Optional[tuple[Callable[[], Any], Future]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()

    def call(self, fn: Callable[[], Any], timeout: Optional[float] = None) -> Any:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="hoard-browser", daemon=True)
                self._thread.start()
        future: Future = Future()
        self._queue.put((fn, future))
        return future.result(timeout=timeout)

    def _loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=self.idle_s)
            except queue.Empty:
                with suppress(Exception):
                    self.on_idle()
                continue
            if item is None:
                return
            fn, future = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn())
            except BaseException as error:                          # noqa: BLE001 - hand every failure back to the caller
                future.set_exception(error)

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._queue.put(None)
            thread.join(timeout=10)


def _first_line(error: BaseException, n: int = 200) -> str:
    text = str(error).strip().splitlines()
    return (text[0] if text else type(error).__name__)[:n]


class BrowserRung:
    """See the module docstring. ``profile_dir`` is created on first use.

    ``profile`` is the :mod:`~.safety` profile for URLs (default ``public``); ``resolver`` is injectable for tests.
    ``playwright_factory`` returns a started Playwright object (default: ``sync_playwright().start()``)."""

    def __init__(self, profile_dir: Any, *, idle_s: float = WORKER_IDLE_S, headless: bool = True, channel: Any = _AUTO,
                 locale: str = "en-US", settle_s: float = SETTLE_BUDGET_S, profile: str = safety.PUBLIC,
                 resolver: Optional[safety.Resolver] = None, playwright_factory: Optional[Callable[[], Any]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.channel = channel
        self.locale = locale
        self.settle_s = settle_s
        self.profile = profile
        self.resolver = resolver
        self._factory = playwright_factory or _start_playwright
        self._clock = clock
        self._gate = threading.Lock()
        self._worker = _Worker(idle_s, self._release_in_worker)
        self._pw: Any = None
        self._ctx: Any = None
        self.channel_used = ""
        self._verdicts: dict[str, tuple[float, Optional[str]]] = {}

    # ------------------------------------------------------------------ availability
    def available(self) -> bool:
        return self._factory is not _start_playwright or playwright_installed()

    def unavailable_reason(self) -> str:
        return "" if self.available() else "Playwright is not installed. " + INSTALL_HINT

    # ------------------------------------------------------------------ policy
    def _problem(self, url: str) -> Optional[str]:
        return safety.check_url(url, self.profile, self.resolver)

    def _route_handler(self) -> Callable[[Any], None]:
        def handler(route: Any) -> None:
            url = route.request.url
            scheme = url.split(":", 1)[0].lower()
            if scheme in ("data", "blob", "about"):
                route.continue_()
                return
            host_key = (url.split("/")[2] if url.count("/") >= 2 else url).lower()
            now = self._clock()
            cached = self._verdicts.get(host_key)
            if cached and now - cached[0] < 60:
                problem = cached[1]
            else:
                problem = self._problem(url)
                if problem and problem.startswith(safety.UNRESOLVABLE_PREFIX):
                    problem = None                    # let the browser report its own DNS failure
                self._verdicts[host_key] = (now, problem)
                if len(self._verdicts) > 2000:
                    self._verdicts.clear()
            if problem:
                log.info("browser request refused: %s (%s)", url[:120], problem)
                route.abort("blockedbyclient")
            else:
                route.continue_()
        return handler

    def _protect(self, ctx: Any) -> None:
        with suppress(Exception):
            ctx.route("**/*", self._route_handler())

    # ------------------------------------------------------------------ context (worker thread)
    def _ensure_context(self) -> Any:
        if self._ctx is not None:
            try:
                _ = self._ctx.pages                      # raises when the browser was closed underneath us
                return self._ctx
            except Exception:                            # noqa: BLE001
                self._release_in_worker()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = self._factory()
        try:
            self._ctx, self.channel_used = launch_context(self._pw, self.profile_dir, headless=self.headless, channel=self.channel,
                                                          locale=self.locale)
        except Exception:
            self._release_in_worker()
            raise
        self._protect(self._ctx)
        return self._ctx

    def _release_in_worker(self) -> None:
        ctx, pw = self._ctx, self._pw
        self._ctx = self._pw = None
        for closer in (getattr(ctx, "close", None), getattr(pw, "stop", None)):
            if closer:
                with suppress(Exception):
                    closer()

    def _reset_after_error(self) -> None:
        with suppress(Exception):
            self._worker.call(self._release_in_worker, timeout=20)

    @staticmethod
    def _settle(page: Any, budget: float) -> None:
        with suppress(Exception):                        # never idle (chatty pages) is fine
            page.wait_for_load_state("networkidle", timeout=int(budget * 1000))

    # ------------------------------------------------------------------ fetch
    def _read_page(self, page: Any, url: str, timeout_s: float, settle_s: float) -> dict[str, Any]:
        response = page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
        self._settle(page, settle_s)
        html = page.content()
        status = response.status if response is not None else 0
        headers = {str(k).lower(): str(v) for k, v in (response.headers if response is not None else {}).items()}
        verdict = detect_block(status, html, headers, page.url)
        rechecks = 0
        while verdict in (CLOUDFLARE, AKAMAI) and rechecks < PASSIVE_RECHECKS:   # some interstitials refresh themselves
            rechecks += 1
            page.wait_for_timeout(int(PASSIVE_RECHECK_WAIT_S * 1000))
            self._settle(page, 2.0)
            html = page.content()
            verdict = detect_block(200, html, {}, page.url)
            if not verdict:
                status = 200
        return {"final_url": page.url, "status": status, "text": html,
                "content_type": headers.get("content-type", "text/html"), "headers": headers}

    def _result(self, url: str, tier: str, data: dict[str, Any], started: float) -> FetchResult:
        fr = FetchResult(url=url, tier=tier, fetched_at=time.time(), final_url=data["final_url"], status=data["status"],
                         text=data["text"], content_type=data["content_type"], headers=data["headers"])
        fr.elapsed_ms = int((time.monotonic() - started) * 1000)
        return apply_block(fr, detect_block(fr.status, fr.text, fr.headers, fr.final_url or url))

    def _refuse(self, url: str, tier: str, message: str, kind: str = "policy") -> FetchResult:
        return FetchResult(url=url, tier=tier, fetched_at=time.time(), error=message, error_kind=kind)

    def fetch(self, url: str, settle_s: Optional[float] = None, *, timeout_s: float = 25.0) -> FetchResult:
        """Read ``url`` in the headless browser; ``settle_s`` is the bounded wait for network idle. Never raises."""
        started = time.monotonic()
        problem = self._problem(url)
        if problem:
            return self._refuse(url, "browser", problem, "dns" if problem.startswith(safety.UNRESOLVABLE_PREFIX) else "policy")
        if not self.available():
            return self._refuse(url, "browser", self.unavailable_reason(), "network")
        settle = self.settle_s if settle_s is None else settle_s

        def work() -> dict[str, Any]:
            ctx = self._ensure_context()
            page = ctx.new_page()
            try:
                return self._read_page(page, url, timeout_s, settle)
            finally:
                with suppress(Exception):
                    page.close()

        with self._gate:
            try:
                data = self._worker.call(work, timeout=timeout_s + settle + 30)
            except BrowserUnavailable as error:
                return self._refuse(url, "browser", str(error), "network")
            except Exception as error:                              # noqa: BLE001 - navigation errors, timeouts, crashes
                self._reset_after_error()
                return self._refuse(url, "browser", f"browser error: {_first_line(error)}",
                                    "timeout" if "imeout" in str(error) else "network")
        return self._result(url, "browser", data, started)

    # ------------------------------------------------------------------ private sessions
    @contextmanager
    def browser_session(self, *, headless: bool = True, args: Optional[list[str]] = None) -> Iterator[Any]:
        """A Playwright ``BrowserContext`` on the shared profile, bound to the *calling* thread (a private Playwright is
        started here). The shared worker browser is closed meanwhile (a profile opens once)."""
        if not self.available():
            raise BrowserUnavailable(self.unavailable_reason())
        with self._gate:
            if self._worker.alive():
                self._worker.call(self._release_in_worker, timeout=30)
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                pw = self._factory()
            except Exception as error:                              # noqa: BLE001
                raise BrowserUnavailable(f"cannot start Playwright: {_first_line(error)}. {INSTALL_HINT}") from error
            try:
                ctx, self.channel_used = launch_context(pw, self.profile_dir, headless=headless, channel=self.channel,
                                                        locale=self.locale, args=args)
            except BrowserUnavailable:
                with suppress(Exception):
                    pw.stop()
                raise
            self._protect(ctx)
            try:
                yield ctx
            finally:
                with suppress(Exception):
                    ctx.close()
                with suppress(Exception):
                    pw.stop()

    def _in_thread(self, fn: Callable[[], Any], name: str, timeout: float) -> Any:
        outcome: dict[str, Any] = {}

        def run() -> None:
            try:
                outcome["value"] = fn()
            except BaseException as error:                          # noqa: BLE001
                outcome["error"] = error

        thread = threading.Thread(target=run, name=name, daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        if "error" in outcome:
            raise outcome["error"]
        if "value" not in outcome:
            raise TimeoutError("the browser did not answer in time")
        return outcome["value"]

    def fetch_window(self, url: str, *, timeout_s: float = 25.0) -> FetchResult:
        """Read a page in an ordinary visible window (started minimised) on the shared profile: for sites that turn
        headless browsers away but serve a normal one. Nothing is spoofed or solved."""
        started = time.monotonic()
        problem = self._problem(url)
        if problem:
            return self._refuse(url, "window", problem)
        if not self.available():
            return self._refuse(url, "window", self.unavailable_reason(), "network")

        def run() -> dict[str, Any]:
            with self.browser_session(headless=False, args=["--start-minimized"]) as ctx:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                return self._read_page(page, url, timeout_s + 20, self.settle_s)

        try:
            data = self._in_thread(run, "hoard-browser-window", timeout_s + self.settle_s + 80)
        except Exception as error:                                  # noqa: BLE001
            return self._refuse(url, "window", f"browser window error: {_first_line(error)}", "network")
        return self._result(url, "window", data, started)

    def open_for_human(self, url: str, *, timeout_s: float = 900.0) -> dict[str, Any]:
        """Open a visible window on the shared profile and return when the person closes it (they solve a CAPTCHA or log
        in; the cookies stay in the profile). Returns ``{url, channel, closed_by_user, final_url, goto_error?}``."""
        problem = self._problem(url)
        if problem:
            raise safety.PolicyError(problem, url)
        out: dict[str, Any] = {}

        def run() -> None:
            with self.browser_session(headless=False) as ctx:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                except Exception as error:                          # noqa: BLE001 - the person can navigate by hand
                    out["goto_error"] = _first_line(error)
                try:
                    ctx.wait_for_event("close", timeout=int(timeout_s * 1000))
                    out["closed_by_user"] = True
                except Exception:                                   # noqa: BLE001 - timeout: we close it ourselves
                    out["closed_by_user"] = False
                with suppress(Exception):
                    out["final_url"] = page.url

        self._in_thread(run, "hoard-browser-human", timeout_s + 60)
        return {"url": url, "channel": self.channel_used, **out}

    # ------------------------------------------------------------------ extras
    def capture_json(self, url: str, match: str, *, timeout_s: float = 45.0, offscreen: bool = False) -> tuple[Optional[Any], str]:
        """Open ``url`` and return ``(data, "")`` for the first JSON response whose URL contains ``match`` (the data a page's
        own script loads), or ``(None, error)``. ``offscreen=True`` uses a real window placed off-screen (some carrier
        sites serve a download to headless browsers)."""
        problem = self._problem(url)
        if problem:
            return None, problem
        if not self.available():
            return None, self.unavailable_reason()

        def work(ctx: Any) -> tuple[Optional[Any], str]:
            page = ctx.new_page()
            got: dict[str, Any] = {}

            def on_response(response: Any) -> None:
                if match in response.url and "data" not in got:
                    try:
                        got["data"] = response.json()
                    except Exception as exc:                        # noqa: BLE001
                        try:
                            got["data"] = json.loads(response.text())
                        except Exception:                           # noqa: BLE001
                            got["error"] = f"unreadable answer ({type(exc).__name__})"

            page.on("response", on_response)
            try:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
                except Exception as exc:                            # noqa: BLE001 - a late navigation error after the data arrived is fine
                    if "data" not in got:
                        got.setdefault("error", f"navigation: {_first_line(exc, 160)}")
                deadline = time.monotonic() + timeout_s
                while "data" not in got and time.monotonic() < deadline:
                    page.wait_for_timeout(400)
                if "data" in got:
                    return got["data"], ""
                text = ""
                with suppress(Exception):
                    text = page.inner_text("body")[:2000]
                verdict = detect_block(200, text, {}, page.url) if text else ""
                return None, got.get("error") or (f"the page asked for a human check ({verdict})" if verdict else
                                                  "no matching data within the time limit")
            finally:
                with suppress(Exception):
                    page.close()

        try:
            if offscreen:
                return self._in_thread(lambda: self._with_session(work, headless=False, args=OFFSCREEN_ARGS),
                                       "hoard-browser-capture", timeout_s + 90)
            with self._gate:
                return self._worker.call(lambda: work(self._ensure_context()), timeout=timeout_s + 60)
        except Exception as error:                                  # noqa: BLE001
            if not offscreen:
                self._reset_after_error()
            return None, f"{type(error).__name__}: {_first_line(error)}"

    def _with_session(self, fn: Callable[[Any], Any], *, headless: bool, args: Optional[list[str]]) -> Any:
        with self.browser_session(headless=headless, args=args) as ctx:
            return fn(ctx)

    def screenshot(self, url: str, path: Any, width: int = 1280, *, full_page: bool = True, timeout_s: float = 30.0,
                   height: int = 800) -> dict[str, Any]:
        """Save a PNG of ``url`` at ``width`` pixels. Returns ``{"ok", "path", "final_url", "status", "error"}``."""
        problem = self._problem(url)
        if problem:
            return {"ok": False, "path": "", "error": problem, "final_url": "", "status": 0}
        if not self.available():
            return {"ok": False, "path": "", "error": self.unavailable_reason(), "final_url": "", "status": 0}
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)

        def work() -> dict[str, Any]:
            ctx = self._ensure_context()
            page = ctx.new_page()
            try:
                page.set_viewport_size({"width": int(width), "height": int(height)})
                response = page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
                self._settle(page, self.settle_s)
                page.screenshot(path=str(target), full_page=full_page)
                return {"ok": True, "path": str(target), "final_url": page.url, "status": response.status if response else 0, "error": ""}
            finally:
                with suppress(Exception):
                    page.close()

        with self._gate:
            try:
                return self._worker.call(work, timeout=timeout_s + self.settle_s + 30)
            except Exception as error:                              # noqa: BLE001
                self._reset_after_error()
                return {"ok": False, "path": "", "error": f"browser error: {_first_line(error)}", "final_url": "", "status": 0}

    # ------------------------------------------------------------------ shutdown
    def close(self) -> None:
        with suppress(Exception):
            with self._gate:
                if self._worker.alive():
                    self._worker.call(self._release_in_worker, timeout=30)
        self._worker.stop()
