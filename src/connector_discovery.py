"""src/connector_discovery.py — find the apps a connector talks to, the way a
phone finds a Bluetooth speaker: look at what is listening on this machine,
ask each port who it is, and offer the ones we recognise.

Two jobs, both born on 17-09 from Jobhunter's Hoard:

* **Follow the app.** Jobhunter picks the first free port from 5178 up
  (`server/port.js`), so ``APP_URL`` in the connector goes stale the day
  something else takes 5178. `find_app(preset)` scans the listening ports,
  fingerprints each with the preset's own health endpoint and hands back the
  live URL; the routes persist it and respawn the bridge with the new env.
* **Nearby apps.** `discover()` lists every local web app that answers, with
  the process behind it (name, cwd — which is the app's install dir, the
  ``{X_DIR}`` the preset needs) and which preset it matches, if any. The
  Connectors screen renders that as a pairing list with one-click Add.

Only loopback ports are considered, only ``GET`` on ``/api/health`` and ``/``
are sent (the app's own bearer token only after a 401), and nothing is written anywhere: this module
observes. psutil is optional — without it the port list comes from
``netstat`` and the process column stays empty.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from src.connectors import PRESETS, ConnectorPreset, default_token_files, read_token_file

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 1.5
MAX_PARALLEL_PROBES = 16
#: Ports that are never a domain app (this server, Ollama, the usual system
#: services) — skipped so a scan does not bother them.
SKIP_PORTS = {11434, 5354, 5939, 27339}
#: Loopback range a preset's app may drift within (Jobhunter: 5178 + up to 100).
DRIFT_SPAN = 30

def fingerprints() -> Dict[str, Dict[str, List[str]]]:
    """How to recognise each plugin's app, read from the plugins themselves.

    This used to be a literal dict here, written by hand next to a SECOND
    hand-written list of the same plugins in `src/connectors.py`. Keeping
    two lists in step is a chore nobody signed up for, so it was not done:
    of the five plugins that shipped, three had no entry here at all and
    could never be recognised by a scan, however plainly their app was
    running. A plugin declares its own `app.identify` now, in the one file
    that is the plugin, and this function is a view of that.
    """
    from src import plugins as plugins_mod

    return {
        pid: {field: list(values) for field, values in plugin.identify.items() if values}
        for pid, plugin in plugins_mod.load_plugins().items()
        if plugin.identify
    }


def health_paths() -> List[str]:
    """Every distinct health path a plugin declares, most specific first.

    A probe that only ever asked for ``/api/health`` could not recognise an
    app whose health lives somewhere else — which is two of the five (one
    answers on ``/api/session``, one only has ``/``). Asking each declared
    path costs one request per path on a loopback port.
    """
    from src import plugins as plugins_mod

    paths: List[str] = []
    for plugin in plugins_mod.load_plugins().values():
        path = (plugin.health_path or "").strip()
        if path and path != "/" and path not in paths:
            paths.append(path)
    # `/api/health` first whether or not a plugin declares it: it is what
    # most apps answer on, and a scan should spend its first request where
    # the answer usually is rather than wherever a dict happened to order.
    if "/api/health" in paths:
        paths.remove("/api/health")
    paths.insert(0, "/api/health")
    return paths


@dataclass
class ListeningPort:
    port: int
    pid: Optional[int] = None
    process: str = ""
    cwd: str = ""
    cmdline: str = ""


@dataclass
class Candidate:
    port: int
    url: str
    pid: Optional[int]
    process: str
    cwd: str
    title: str
    health: Optional[Dict[str, Any]]
    preset_id: Optional[str]
    preset_name: Optional[str]
    latency_ms: Optional[int]
    values: Dict[str, str] = field(default_factory=dict)   # prefilled preset values

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# what is listening
# ---------------------------------------------------------------------------

def _own_ports() -> set:
    """This server's own port(s): never a candidate."""
    out = set()
    for key in ("FAUSTUS_PORT", "PORT"):
        try:
            v = int(os.environ.get(key) or 0)
            if v:
                out.add(v)
        except ValueError:
            pass
    return out


def listening_ports() -> List[ListeningPort]:
    """Loopback/any-address TCP listeners with the owning process when
    psutil can see it. Sorted by port. Never raises."""
    found: Dict[int, ListeningPort] = {}
    try:
        import psutil  # type: ignore
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status != psutil.CONN_LISTEN or not conn.laddr:
                continue
            ip = conn.laddr.ip
            if ip not in ("127.0.0.1", "0.0.0.0", "::", "::1", ""):
                continue
            port = int(conn.laddr.port)
            if port in found:
                continue
            lp = ListeningPort(port=port, pid=conn.pid)
            if conn.pid:
                try:
                    proc = psutil.Process(conn.pid)
                    lp.process = proc.name()
                    try:
                        lp.cwd = proc.cwd() or ""
                    except Exception:  # noqa: BLE001 - other user / access denied
                        lp.cwd = ""
                    try:
                        lp.cmdline = " ".join(proc.cmdline() or [])[:300]
                    except Exception:  # noqa: BLE001
                        lp.cmdline = ""
                except Exception:  # noqa: BLE001
                    pass
            found[port] = lp
    except Exception as exc:  # noqa: BLE001 - psutil missing or refused
        logger.debug("[connector-discovery] psutil listing failed (%s); falling back to netstat", exc)
        found.update({lp.port: lp for lp in _netstat_ports()})
    return sorted(found.values(), key=lambda lp: lp.port)


def _netstat_ports() -> List[ListeningPort]:
    args = ["netstat", "-ano" if sys.platform == "win32" else "-tln"]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False).stdout
    except Exception:  # noqa: BLE001
        return []
    ports: Dict[int, ListeningPort] = {}
    for line in out.splitlines():
        if "LISTEN" not in line.upper():
            continue
        m = re.search(r"(?:127\.0\.0\.1|0\.0\.0\.0|\[::\]|\[::1\]|\*):(\d+)", line)
        if not m:
            continue
        port = int(m.group(1))
        pid = None
        if sys.platform == "win32":
            tail = line.strip().split()
            if tail and tail[-1].isdigit():
                pid = int(tail[-1])
        ports.setdefault(port, ListeningPort(port=port, pid=pid))
    return list(ports.values())


# ---------------------------------------------------------------------------
# who is it
# ---------------------------------------------------------------------------

async def probe(url: str, health_path: Optional[str] = None,
                tokens: Optional[List[str]] = None) -> Dict[str, Any]:
    """``{"health": dict|None, "title": str, "latency_ms": int|None}`` for one
    loopback app. `health` is the parsed JSON of the first health path that
    answered 200 with an object; `title` the ``<title>`` of ``/`` when it is
    HTML. Never raises; a port that is not HTTP simply yields nothing.

    With no `health_path` given, every path a plugin declares is tried in
    turn. Hard-coding ``/api/health`` was fine while both known apps used it
    and silently blind to the ones that do not — the point of asking an app
    who it is is to ask where it answers, and each plugin says.
    """
    import httpx

    result: Dict[str, Any] = {"health": None, "title": "", "latency_ms": None}
    paths = [health_path] if health_path else health_paths()
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=PROBE_TIMEOUT_S) as client:
            reached = False
            for path in paths:
                try:
                    resp = await client.get(url.rstrip("/") + path)
                except Exception:  # noqa: BLE001 - not HTTP, refused, timed out
                    if reached:
                        continue
                    return result
                reached = True
                if result["latency_ms"] is None:
                    result["latency_ms"] = int((time.monotonic() - started) * 1000)
                if resp.status_code == 401:
                    # A bridge that wants its own token (Writer's Hoard): try
                    # the tokens the presets know how to find on this machine.
                    for tok in tokens or ():
                        try:
                            again = await client.get(url.rstrip("/") + path,
                                                     headers={"Authorization": f"Bearer {tok}"})
                        except Exception:  # noqa: BLE001
                            continue
                        if again.status_code == 200:
                            resp = again
                            break
                if resp.status_code == 200:
                    try:
                        body = resp.json()
                    except Exception:  # noqa: BLE001
                        body = None
                    if isinstance(body, dict):
                        result["health"] = body
                        break
            try:
                root = await client.get(url.rstrip("/") + "/")
                ctype = root.headers.get("content-type", "")
                if root.status_code == 200 and "html" in ctype:
                    m = re.search(r"<title[^>]*>(.*?)</title>", root.text[:20000], re.I | re.S)
                    if m:
                        result["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return result


def match_preset(health: Optional[Dict[str, Any]], title: str) -> Optional[str]:
    """Which plugin a probed app is, by its own health body first, then by
    the page title. None when nothing recognisable answered.

    Any field a plugin names in `app.identify` is matched against the health
    body, not just ``service``: the apps do not agree on what to call
    themselves (one says ``service``, one ``application``, one only has a
    mode), and a matcher that knows one key can only ever recognise the apps
    that happen to use it. ``title`` is the exception — it is the page title,
    matched as a substring, and it is what recognises an app with no health
    endpoint worth the name.
    """
    body = {str(k).strip().lower(): str(v).strip().lower()
            for k, v in (health or {}).items() if isinstance(v, (str, int, float))}
    low_title = (title or "").lower()
    prints = fingerprints()
    for preset_id, fp in prints.items():
        for field, values in fp.items():
            if field == "title":
                continue
            seen = body.get(field.strip().lower())
            if seen and any(seen == str(v).strip().lower() for v in values):
                return preset_id
    for preset_id, fp in prints.items():
        for candidate in fp.get("title") or ():
            if low_title and str(candidate).lower() in low_title:
                return preset_id
    return None


def _dir_placeholder(preset: ConnectorPreset) -> Optional[str]:
    for name in preset.placeholders:
        if name.endswith("_DIR"):
            return name
    return None


def _looks_like_app_dir(preset: ConnectorPreset, path: str) -> bool:
    """The process cwd is the install dir when the preset's bridge script
    lives under it (Jobhunter: server/mcp.js; Writer: dist-electron/...)."""
    if not path or not os.path.isdir(path):
        return False
    key = _dir_placeholder(preset)
    for arg in preset.args:
        if key and "{" + key + "}" in arg:
            rel = arg.replace("{" + key + "}", "").lstrip("/\\")
            if os.path.isfile(os.path.join(path, rel)):
                return True
    return False


def suggested_values(preset: ConnectorPreset, url: str, cwd: str) -> Dict[str, str]:
    """Prefilled `values` for a discovered app: its live URL and, when the
    process cwd is the install dir, the `{X_DIR}` placeholder too."""
    values = {"APP_URL": url}
    key = _dir_placeholder(preset)
    if key and _looks_like_app_dir(preset, cwd):
        values[key] = cwd
    return values


def _known_tokens() -> List[str]:
    out = []
    for path in default_token_files().values():
        tok = read_token_file(path)
        if tok:
            out.append(tok)
    return out


async def _probe_port(lp: ListeningPort, sem: asyncio.Semaphore, tokens: Optional[List[str]] = None) -> Optional[Candidate]:
    url = f"http://127.0.0.1:{lp.port}"
    async with sem:
        probed = await probe(url, tokens=tokens)
    if probed["health"] is None and not probed["title"]:
        return None
    preset_id = match_preset(probed["health"], probed["title"])
    preset = PRESETS.get(preset_id) if preset_id else None
    return Candidate(
        port=lp.port, url=url, pid=lp.pid, process=lp.process, cwd=lp.cwd,
        title=probed["title"] or str((probed["health"] or {}).get("service") or ""),
        health=probed["health"], preset_id=preset_id,
        preset_name=preset.name if preset else None, latency_ms=probed["latency_ms"],
        values=suggested_values(preset, url, lp.cwd) if preset else {},
    )


async def discover(*, ports: Optional[List[ListeningPort]] = None, exclude: Optional[set] = None) -> List[Candidate]:
    """Every loopback web app that answers, recognised presets first."""
    skip = set(SKIP_PORTS) | _own_ports() | set(exclude or ())
    listing = [lp for lp in (ports if ports is not None else listening_ports()) if lp.port not in skip]
    sem = asyncio.Semaphore(MAX_PARALLEL_PROBES)
    tokens = _known_tokens()
    results = await asyncio.gather(*(_probe_port(lp, sem, tokens) for lp in listing), return_exceptions=True)
    found = [r for r in results if isinstance(r, Candidate)]
    found.sort(key=lambda c: (c.preset_id is None, c.port))
    return found


async def find_app(preset: ConnectorPreset, *, current_url: str = "",
                   ports: Optional[List[ListeningPort]] = None) -> Optional[Candidate]:
    """The live instance of `preset`'s app, wherever it drifted to.

    Tries the ports nearest the preset's default first (that is where a
    "first free port from N" app lands), then everything else listening.
    Returns None when nothing on this machine fingerprints as that app."""
    listing = ports if ports is not None else listening_ports()
    try:
        from urllib.parse import urlsplit
        base = int(urlsplit(preset.app_url_default).port or 0)
    except Exception:  # noqa: BLE001
        base = 0
    near = [lp for lp in listing if base and 0 <= lp.port - base <= DRIFT_SPAN]
    far = [lp for lp in listing if lp not in near]
    for group in (near, far):
        if not group:
            continue
        for cand in await discover(ports=group):
            if cand.preset_id == preset.id:
                return cand
    return None
