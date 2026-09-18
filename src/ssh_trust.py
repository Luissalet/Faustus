"""Faustus-owned SSH host trust: the one place that decides which remote host
keys the app will talk to, and the only place that spells the ssh/scp flags
that enforce it.

Why this exists (B-025). Every SSH call site in cookbook, shell and the serve
lifecycle used to hard-code ``StrictHostKeyChecking=no``. That flag makes the
first connection succeed no matter who answers, so a node's key never became a
relationship Faustus could check on the second connection -- and that same
channel carries runner scripts, private weights and ``HF_TOKEN``. The hostname
regexes elsewhere stop shell-option injection; they say nothing about who is
listening on the other end.

The model is deliberate pairing, not trust-on-first-use:

  1. ``scan_host_keys`` asks the node for its keys and derives the SHA256
     fingerprints a human can read out loud against the node's own console;
  2. that human confirms one and ``pair_host`` writes that exact line into a
     known_hosts file Faustus owns;
  3. from then on every argv carries ``StrictHostKeyChecking=yes`` plus
     ``UserKnownHostsFile=<that file>``, so ssh itself refuses an unpaired or
     swapped identity before a byte of payload moves.

``accept-new`` exists only as a transition and only for an attended action
(``attended=True``). It is unreachable from a background loop, and even then it
still refuses a CHANGED key -- ssh's own semantics, which is exactly why we
prefer it to writing keys ourselves.

A changed key is a stop, not a repair. ``pair_host`` refuses to overwrite an
existing entry; a human has to revoke it through ``forget_host`` first. Quietly
replacing the old key is the MITM-swallowing behaviour this module exists to
end.

Entries are keyed by the exact spelling used to connect, because that is what
ssh matches on. ``gpu-box`` paired on port 22 does not authorise ``10.0.0.5``
or ``gpu-box:2222``: an alternate spelling is an unpaired host and earns the
same refusal.

Pure stdlib, no app imports beyond DATA_DIR, so the shell/cookbook/tool layers
can all reach it without an import cycle.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)


class SshTrustError(RuntimeError):
    """Base class for every refusal raised here."""


class HostKeyChanged(SshTrustError):
    """A paired node now presents a different key. Never repaired in place."""


class HostKeyMismatch(SshTrustError):
    """The fingerprint the human confirmed is not one the node offers."""


class HostScanFailed(SshTrustError):
    """ssh-keyscan could not reach the node or returned nothing usable."""


# Mirrors routes/_validators._REMOTE_HOST_RE on purpose. This module sits below
# the route layer and must not raise HTTPException, so a host that fails here is
# a caller bug, not a 400 -- but the accepted shape has to stay identical or a
# value the route blessed could still be rejected down here.
_REMOTE_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9._-]*$")
_PORT_RE = re.compile(r"^\d{1,5}$")

_STRICT_YES = "StrictHostKeyChecking=yes"
_STRICT_ACCEPT_NEW = "StrictHostKeyChecking=accept-new"


def known_hosts_path() -> Path:
    """Location of the known_hosts file Faustus owns.

    Deliberately not ``~/.ssh/known_hosts``: that file is rewritten by every
    other ssh client on the box and wiped entry-by-entry by ``ssh-keygen -R``,
    so a key Faustus paired could disappear -- or be replaced -- without
    Faustus ever observing it. Resolved per call rather than at import so tests
    (and a relocated ODYSSEUS_DATA_DIR) can point it somewhere disposable.
    """
    override = (os.getenv("FAUSTUS_SSH_KNOWN_HOSTS") or "").strip()
    if override:
        return Path(override)
    try:  # pragma: no cover - constants always import inside the app
        from src.constants import DATA_DIR
    except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
        DATA_DIR = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
        )
    return Path(DATA_DIR) / "ssh" / "known_hosts"


def remote_host(remote: str) -> str:
    """Strip any ``user@`` prefix -- host keys belong to the host, not the login."""
    return str(remote or "").rsplit("@", 1)[-1]


def _validated_remote(remote: str) -> str:
    value = str(remote or "").strip()
    if not _REMOTE_RE.match(value):
        raise ValueError("invalid ssh remote (expected host or user@host)")
    return value


def _validated_port(ssh_port) -> str | None:
    """Normalize a port to its argv form, or None when it is the default.

    22 collapses to None so a host paired as ``gpu-box`` keeps matching whether
    the caller passed "", None or "22" -- OpenSSH only brackets non-default
    ports in known_hosts, and an entry that disagrees with ssh's own spelling
    would never match.
    """
    value = str(ssh_port if ssh_port is not None else "").strip()
    if value in ("", "22"):
        return None
    if not _PORT_RE.fullmatch(value) or not (1 <= int(value) <= 65535):
        raise ValueError("invalid ssh port")
    return str(int(value))


def host_pattern(remote: str, ssh_port=None) -> str:
    """known_hosts key for a target, in OpenSSH's own spelling."""
    host = remote_host(_validated_remote(remote)).casefold()
    port = _validated_port(ssh_port)
    return host if port is None else f"[{host}]:{port}"


def trust_options(*, attended: bool = False) -> list[str]:
    """The ``-o`` pair every ssh/scp invocation in this codebase must carry.

    ``attended`` is the transition valve from B-025: a human is watching a
    pairing action right now, so accepting a first-seen key is a decision they
    can undo. Background loops never set it, and even when set ssh still
    refuses a key that CHANGED -- accept-new only relaxes the unknown case.
    """
    return [
        "-o",
        _STRICT_ACCEPT_NEW if attended else _STRICT_YES,
        "-o",
        f"UserKnownHostsFile={known_hosts_path()}",
    ]


def ssh_argv(
    remote: str,
    ssh_port=None,
    remote_cmd: str | None = None,
    *,
    connect_timeout: int | None = None,
    attended: bool = False,
    extra_options: Sequence[str] = (),
) -> list[str]:
    """Build an ssh argv. The only sanctioned way to reach a node from Python."""
    value = _validated_remote(remote)
    port = _validated_port(ssh_port)
    argv = ["ssh"]
    if connect_timeout is not None:
        argv.extend(["-o", f"ConnectTimeout={int(connect_timeout)}"])
    argv.extend(trust_options(attended=attended))
    for opt in extra_options:
        argv.extend(["-o", str(opt)])
    if port is not None:
        argv.extend(["-p", port])
    argv.append(value)
    if remote_cmd is not None:
        argv.append(remote_cmd)
    return argv


def ssh_command(
    remote: str,
    remote_cmd: str | None = None,
    *,
    ssh_port=None,
    connect_timeout: int | None = None,
    attended: bool = False,
) -> str:
    """Same argv, rendered for the call sites that must hand a string to a shell."""
    argv = ssh_argv(
        remote,
        ssh_port,
        remote_cmd,
        connect_timeout=connect_timeout,
        attended=attended,
    )
    return " ".join(shlex.quote(part) for part in argv)


def option_flags(*, connect_timeout: int | None = None, attended: bool = False) -> str:
    """The trust ``-o`` flags as a shell fragment, already quoted.

    For the handful of call sites that assemble an ssh/scp line by hand around a
    payload whose own quoting is load-bearing (the PowerShell launch strings):
    re-flowing those through argv risks breaking the payload, while dropping the
    flags in keeps the single source of truth for what the flags ARE. ssh and
    scp read the same option names, so one fragment serves both.
    """
    parts: list[str] = []
    if connect_timeout is not None:
        parts.extend(["-o", f"ConnectTimeout={int(connect_timeout)}"])
    parts.extend(trust_options(attended=attended))
    return " ".join(shlex.quote(part) for part in parts)


def scp_command(
    local_path,
    remote: str,
    remote_path: str,
    *,
    ssh_port=None,
    quiet: bool = True,
    legacy_protocol: bool = True,
    attended: bool = False,
) -> str:
    """Build an scp push line carrying the same trust flags as ssh.

    scp is the leg that actually moves runner scripts and tokens onto the node,
    so it was the worst place to leave host checking off. ``legacy_protocol``
    keeps the ``-O`` that the existing call sites pass: OpenSSH 9 switched scp
    to SFTP by default and some of the boxes Faustus drives do not serve it.
    """
    dest_path = str(remote_path or "")
    if not dest_path or any(ch.isspace() for ch in dest_path):
        raise ValueError("invalid remote scp path")
    value = _validated_remote(remote)
    port = _validated_port(ssh_port)
    argv = ["scp"]
    if legacy_protocol:
        argv.append("-O")
    if quiet:
        argv.append("-q")
    argv.extend(trust_options(attended=attended))
    if port is not None:
        argv.extend(["-P", port])
    argv.extend([str(local_path), f"{value}:{dest_path}"])
    return " ".join(shlex.quote(part) for part in argv)


def fingerprint_for_key(key_b64: str) -> str:
    """SHA256 fingerprint in the exact form ssh prints, so a human can compare.

    No padding: ``ssh-keygen -l`` strips the base64 ``=`` and operators read the
    two strings side by side.
    """
    raw = base64.b64decode(str(key_b64).strip(), validate=True)
    digest = hashlib.sha256(raw).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def normalize_fingerprint(value: str) -> str:
    """Accept what a human pasted -- with or without the ``SHA256:`` prefix."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty fingerprint")
    if text.upper().startswith("SHA256:"):
        text = text[len("SHA256:"):]
    return "SHA256:" + text.strip().rstrip("=")


def _iter_entries() -> Iterable[tuple[str, str, str, str]]:
    """Yield (line, hosts, keytype, key) for each usable known_hosts entry."""
    try:
        text = known_hosts_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError as e:  # unreadable store: treat as empty, never as trusting
        logger.warning("ssh_trust: known_hosts unreadable (%s); treating as empty", e)
        return
    for raw in text.splitlines():
        line = raw.strip()
        # "@revoked"/"@cert-authority" shift every field along and mean something
        # this module does not model, so they are never counted as a pairing.
        if not line or line.startswith("#") or line.startswith("@"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        yield line, parts[0], parts[1], parts[2]


def stored_keys(remote: str, ssh_port=None) -> list[dict]:
    """Every key Faustus has paired for this exact target spelling."""
    pattern = host_pattern(remote, ssh_port)
    found: list[dict] = []
    for line, hosts, keytype, key in _iter_entries():
        names = {h.strip().casefold() for h in hosts.split(",")}
        if pattern not in names:
            continue
        try:
            fp = fingerprint_for_key(key)
        except Exception:  # noqa: BLE001 - a malformed line is not a pairing
            continue
        found.append({"type": keytype, "key": key, "fingerprint": fp, "line": line})
    return found


def stored_fingerprints(remote: str, ssh_port=None) -> list[str]:
    return [entry["fingerprint"] for entry in stored_keys(remote, ssh_port)]


def is_paired(remote: str, ssh_port=None) -> bool:
    return bool(stored_keys(remote, ssh_port))


def _parse_scan(stdout: str, pattern: str) -> list[dict]:
    """Turn ssh-keyscan output into entries keyed by OUR canonical pattern.

    keyscan echoes the host as it was asked, which may differ in case from the
    pattern we match on; rewriting the first field here keeps the stored line
    and the lookup in lockstep.
    """
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        keytype, key = parts[1], parts[2]
        if (keytype, key) in seen:
            continue
        try:
            fp = fingerprint_for_key(key)
        except Exception:  # noqa: BLE001 - keyscan noise, not a key
            continue
        seen.add((keytype, key))
        entries.append(
            {
                "type": keytype,
                "key": key,
                "fingerprint": fp,
                "line": f"{pattern} {keytype} {key}",
            }
        )
    return entries


def scan_host_keys(remote: str, ssh_port=None, *, timeout: int = 10) -> list[dict]:
    """Ask the node which host keys it offers, without trusting any of them.

    This is the read half of pairing: it never writes to the store, so calling
    it on a hostile node costs nothing beyond a TCP connection. The fingerprints
    it returns are what a human is meant to compare against the node's console.
    """
    pattern = host_pattern(remote, ssh_port)
    host = remote_host(_validated_remote(remote))
    port = _validated_port(ssh_port)
    argv = ["ssh-keyscan", "-T", str(int(timeout))]
    if port is not None:
        argv.extend(["-p", port])
    argv.append(host)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=int(timeout) + 5
        )
    except FileNotFoundError as e:
        raise HostScanFailed(
            "ssh-keyscan is not installed; it ships with OpenSSH client tools"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise HostScanFailed(f"ssh-keyscan timed out reaching {pattern}") from e
    entries = _parse_scan(proc.stdout, pattern)
    if not entries:
        detail = (proc.stderr or "").strip().splitlines()
        raise HostScanFailed(
            f"no host key offered by {pattern}"
            + (f": {detail[-1][:200]}" if detail else "")
        )
    return entries


def pairing_state(
    remote: str,
    ssh_port=None,
    *,
    offered: Sequence[dict] | None = None,
    timeout: int = 10,
) -> dict:
    """Classify a target as unpaired / paired / changed.

    ``offered`` lets a caller that just scanned reuse the result instead of
    hitting the node twice -- the second scan could legitimately return a
    different answer and turn one decision into two.
    """
    pattern = host_pattern(remote, ssh_port)
    stored = stored_fingerprints(remote, ssh_port)
    entries = list(offered) if offered is not None else scan_host_keys(
        remote, ssh_port, timeout=timeout
    )
    offered_fps = [entry["fingerprint"] for entry in entries]
    if not stored:
        state = "unpaired"
    elif set(stored) & set(offered_fps):
        state = "paired"
    else:
        state = "changed"
    return {
        "host": pattern,
        "state": state,
        "stored": stored,
        "offered": offered_fps,
    }


def _append_entry(line: str) -> None:
    path = known_hosts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line.rstrip("\n") + "\n")
    # 0600/0700 after the fact: the store names every node this install is
    # allowed to reach, and on POSIX ssh itself will not read a group-readable
    # known_hosts. chmod is a no-op on Windows and must not fail the pairing.
    for target, mode in ((path, 0o600), (path.parent, 0o700)):
        try:
            os.chmod(target, mode)
        except OSError:
            pass


def pair_host(
    remote: str,
    ssh_port=None,
    *,
    fingerprint: str,
    timeout: int = 10,
) -> dict:
    """Record a node's key after a human confirmed its fingerprint.

    ``fingerprint`` is the human's half of the handshake: it has to match a key
    the node offers RIGHT NOW, which is what makes this different from writing
    whatever answered. An already-paired host with a different key raises
    instead of being rewritten -- see ``forget_host`` for the deliberate exit.
    """
    pattern = host_pattern(remote, ssh_port)
    want = normalize_fingerprint(fingerprint)
    offered = scan_host_keys(remote, ssh_port, timeout=timeout)
    match = next((e for e in offered if e["fingerprint"] == want), None)
    stored = stored_keys(remote, ssh_port)
    if stored:
        stored_fps = {entry["fingerprint"] for entry in stored}
        if match is not None and want in stored_fps:
            return {
                "host": pattern,
                "state": "paired",
                "fingerprint": want,
                "type": match["type"],
                "added": False,
            }
        raise HostKeyChanged(
            f"{pattern} is already paired with {sorted(stored_fps)} but now offers "
            f"{[e['fingerprint'] for e in offered]}. Faustus will not replace a host "
            "key it already trusts; revoke the old pairing explicitly once a human "
            "has confirmed the change is legitimate."
        )
    if match is None:
        raise HostKeyMismatch(
            f"{pattern} offers {[e['fingerprint'] for e in offered]}, not {want}. "
            "Either the fingerprint was mistyped or something is answering for "
            "that address."
        )
    _append_entry(match["line"])
    logger.info("ssh_trust: paired %s with %s (%s)", pattern, want, match["type"])
    return {
        "host": pattern,
        "state": "paired",
        "fingerprint": want,
        "type": match["type"],
        "added": True,
    }


def forget_host(remote: str, ssh_port=None) -> int:
    """Drop every pairing for a target. Returns how many entries went.

    The only way a changed host key gets accepted: a human revokes here, then
    pairs again against a fingerprint they verified out of band. Keeping it a
    separate call is the point -- an automatic "remove then re-add" is exactly
    the repair that would swallow a key swap.
    """
    pattern = host_pattern(remote, ssh_port)
    path = known_hosts_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    kept: list[str] = []
    removed = 0
    for raw in text.splitlines():
        line = raw.strip()
        parts = line.split()
        if line and not line.startswith("#") and not line.startswith("@") and len(parts) >= 3:
            names = {h.strip().casefold() for h in parts[0].split(",")}
            if pattern in names:
                removed += 1
                continue
        kept.append(raw)
    if removed:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(kept) + ("\n" if kept else ""))
        logger.info("ssh_trust: revoked %d pairing(s) for %s", removed, pattern)
    return removed
